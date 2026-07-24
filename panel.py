#!/usr/bin/env python3
"""GIGABYTE A5 K1 control panel, monitoring, power (ryzenadj), fans, keyboard RGB.

Thin GUI over the firmware. All privileged actions go through one NOPASSWD root
harness (labctl -> firmware _DSM / acpi_call), so the app never runs as root and
never prompts for a password. Closes to a system-tray icon instead of quitting.
"""
import json
import shutil
import subprocess
import sys
from pathlib import Path

from PyQt6.QtCore import Qt, QTimer, QProcess, QPointF
from PyQt6.QtNetwork import QLocalServer, QLocalSocket
from PyQt6.QtGui import QAction, QColor, QIcon, QPainter, QPen, QPolygonF
from PyQt6.QtWidgets import (
    QApplication, QCheckBox, QColorDialog, QFrame, QGridLayout, QHBoxLayout,
    QLabel, QMainWindow, QMenu, QMessageBox, QPushButton, QScrollArea, QSlider,
    QSystemTrayIcon, QTabWidget, QVBoxLayout, QWidget,
)

# --- power limits, in watts. Ryzen 5 5600H: stock ~45W, cTDP up to 54W. ---
TDP_MIN, TDP_MAX, TDP_STOCK = 15, 54, 45
PRESETS = {"Quiet": 25, "Balanced": 45, "Performance": 54}
# each TDP preset also nudges KDE's power-profiles-daemon to the matching profile
PPD_FOR = {"Quiet": "power-saver", "Balanced": "balanced", "Performance": "performance"}


def set_power_profile(name: str):
    """Best-effort: sync power-profiles-daemon. No root needed; failures are non-fatal."""
    if name and shutil.which("powerprofilesctl"):
        subprocess.run(["powerprofilesctl", "set", name], capture_output=True, timeout=5)


def clamp_tdp(watts: int) -> int:
    """Clamp a requested TDP into the safe hardware range. Pure, unit-tested below."""
    return max(TDP_MIN, min(TDP_MAX, int(watts)))


def read_temps() -> dict:
    """Return {'cpu': float|None, 'gpu': float|None} from `sensors -j`. Tolerant of layout."""
    out = {"cpu": None, "gpu": None}
    if not shutil.which("sensors"):
        return out
    try:
        data = json.loads(subprocess.run(
            ["sensors", "-j"], capture_output=True, text=True, timeout=4).stdout)
    except Exception:
        return out
    for chip, body in data.items():
        if not isinstance(body, dict):
            continue
        if chip.startswith("k10temp"):
            out["cpu"] = _first_temp(body.get("Tctl") or body.get("temp1"))
        elif chip.startswith("amdgpu"):
            out["gpu"] = _first_temp(body.get("edge") or body.get("temp1"))
    return out


def _first_temp(feature):
    if isinstance(feature, dict):
        for k, v in feature.items():
            if k.endswith("_input"):
                return round(float(v), 1)
    return None


def set_tdp(watts: int) -> tuple[bool, str]:
    """Apply TDP via the NOPASSWD labctl->ryzenadj path (no pkexec / no password)."""
    w = clamp_tdp(watts)
    mw = w * 1000
    ok, msg = _labctl("ryzenadj", f"--stapm-limit={mw}",
                      f"--fast-limit={mw}", f"--slow-limit={mw}")
    return ok, (msg or f"TDP set to {w}W")


def _exists(path):
    import os
    return os.path.exists(path)


# --- Keyboard RGB via the firmware's own _DSM func 0x67 (Clevo SET_KB_LED / ZEVT). ---
# Decoded from this laptop's DSDT and camera-verified on-device. This A5's keyboard is a
# SINGLE-zone RGB backlight (the EC exposes 3 zone regs but only zone 0 drives the LEDs).
# Everything goes through the mutex-protected _DSM 0x67 (same blessed path as the fans) -
# no raw EC pokes. ARGS is a 4-byte little-endian buffer whose top byte selects the op:
#   0x00000000                     -> custom/static mode
#   0xF0000000 | (B<<16)|(R<<8)|G  -> set colour (Clevo channel packing)
#   0xF4000000 | level             -> set brightness (level 0..255)
def kbd_color_args(r, g, b) -> list[str]:
    """_DSM 0x67 bytes to set keyboard colour. ARGS=0xF0000000|B<<16|R<<8|G; the buffer is
    little-endian so the bytes are [G, R, B, 0xF0]. Pure, tested below."""
    r, g, b = (max(0, min(255, int(x))) for x in (r, g, b))
    return ["0x67", f"{g:02x}", f"{r:02x}", f"{b:02x}", "f0"]


def kbd_bright_args(level) -> list[str]:
    """_DSM 0x67 bytes for brightness: ARGS=0xF4000000|level -> [level,00,00,0xf4]. Pure."""
    level = max(0, min(255, int(level)))
    return ["0x67", f"{level:02x}", "00", "00", "f4"]


def kbd_set(r, g, b, level=255) -> tuple[bool, str]:
    """Custom mode, then set colour + brightness. Ensures the a5ctl module is loaded."""
    ok, msg = fan_ensure_module()
    if not ok:
        return False, f"kernel module not available: {msg}"
    _labctl("dsm", "0x67", "00", "00", "00", "00")     # custom/static mode
    _labctl("dsm", *kbd_bright_args(level))
    return _labctl("dsm", *kbd_color_args(r, g, b))


def kbd_color_only(r, g, b) -> tuple[bool, str]:
    """Set only the colour, one _DSM call. The rainbow cycle uses this after kbd_set has
    already put the keyboard in custom mode + set brightness, so each frame is a single write."""
    return _labctl("dsm", *kbd_color_args(r, g, b))


def rainbow_stops(n=7) -> list[tuple[int, int, int]]:
    """Default cycle palette: n evenly spaced full-saturation hues. Pure, tested below."""
    import colorsys
    return [tuple(round(c * 255) for c in colorsys.hsv_to_rgb(i / n, 1.0, 1.0)) for i in range(n)]


def cycle_color(stops, phase) -> tuple[int, int, int]:
    """Colour at a point in a looping cycle. phase in [0,1) maps around the stop ring with linear
    RGB interpolation between neighbours (so it flows smoothly, then wraps). Pure, tested below."""
    n = len(stops)
    if n == 1:
        return tuple(stops[0])
    x = (phase % 1.0) * n
    i = int(x) % n
    t = x - int(x)
    a, b = stops[i], stops[(i + 1) % n]
    return tuple(round(a[k] + (b[k] - a[k]) * t) for k in range(3))


# --- Manual fan control via the firmware _DSM (kernel module + NOPASSWD harness) ---
# _DSM 0x68 = manual: 4 duty bytes [fan1 fan2 fan3 fan4], each raw PWM 0-255 (=0-100%).
# _DSM 0x69 0x0f = restore all fans to firmware auto (the safe revert). Decoded on-device.
LABCTL = str(Path(__file__).resolve().parent / "labctl")
FAN_MIN_PCT = 20          # floor: never let the slider starve the fans
FAN_WATCHDOG_C = 90       # if CPU hits this in manual mode, force auto
FAN_PRESETS = {"Quiet": 35, "Balanced": 60, "Max": 100}
# software curve: (CPU °C, duty %). Interpolated in-app and pushed via 0x68, no risky
# blind FEVT register writes. Editable in the UI.
FAN_CURVE_DEFAULT = [(40, 20), (60, 45), (75, 65), (90, 100)]


def fan_pct_to_pwm(pct: int) -> int:
    """Clamp a requested duty % to [FAN_MIN_PCT,100] and map to PWM 0-255. Pure, tested below."""
    pct = max(FAN_MIN_PCT, min(100, int(pct)))
    return round(pct * 255 / 100)


def curve_duty(points, temp) -> int:
    """Duty % for temp by linear interpolation over sorted (temp,duty) points, clamped at the ends.
    Pure, tested below. The FAN_MIN_PCT floor is applied later by fan_pct_to_pwm on write."""
    pts = sorted(points)
    if temp <= pts[0][0]:
        return pts[0][1]
    if temp >= pts[-1][0]:
        return pts[-1][1]
    for (t0, d0), (t1, d1) in zip(pts, pts[1:]):
        if t0 <= temp <= t1:
            return round(d0 + (d1 - d0) * (temp - t0) / (t1 - t0))
    return pts[-1][1]


def _labctl(*args, timeout=10) -> tuple[bool, str]:
    """Run the NOPASSWD root harness. Returns (ok, output)."""
    try:
        r = subprocess.run(["sudo", LABCTL, *args],
                           capture_output=True, text=True, timeout=timeout)
        return (r.returncode == 0), (r.stdout or r.stderr).strip()
    except Exception as e:
        return False, str(e)


def fan_module_ready() -> bool:
    import os
    return os.path.exists("/sys/kernel/a5ctl/call")


def fan_ensure_module() -> tuple[bool, str]:
    if fan_module_ready():
        return True, "ready"
    ok, msg = _labctl("modload")
    return (ok and fan_module_ready()), msg


def fan_duty_args(pct1: int, pct2: int) -> list[str]:
    """4 hex duty bytes for _DSM 0x68: [fan1 fan2 fan2 fan2] (this A5 has 2 fans; 3/4 mirror fan2).
    Each floored via fan_pct_to_pwm so a curve/slider can never starve a fan. Pure, tested below."""
    h1, h2 = f"{fan_pct_to_pwm(pct1):02x}", f"{fan_pct_to_pwm(pct2):02x}"
    return ["0x68", h1, h2, h2, h2]


def fan_set_auto() -> tuple[bool, str]:
    """Restore all fans to firmware auto via _DSM 0x69 (bitmask 0x0f)."""
    return _labctl("dsm", "0x69", "0f")


# --- Battery charge limit via _DSM 0x76/0x77 (EC BTCP=stop%, BSCP=start%). Decoded on-device. ---
# ARGS for 0x76 = (0x06<<24)|(stop<<8)|start ; buf bytes = [start, stop, 0x00, 0x06].
def battery_limit_args(stop_pct: int) -> list[str]:
    """_DSM 0x76 args to stop charging at stop_pct (0 clears the limit). Pure, tested below."""
    stop = 0 if stop_pct <= 0 else max(50, min(100, int(stop_pct)))
    start = 0 if stop == 0 else max(40, stop - 5)   # resume charging 5% below the cap
    return ["0x76", f"{start:02x}", f"{stop:02x}", "00", "06"]


def battery_set_limit(stop_pct: int) -> tuple[bool, str]:
    return _labctl("dsm", *battery_limit_args(stop_pct))


def battery_read_limit():
    """Current stop-charge % from _DSM 0x77 (byte2 = BTCP), or None if unset/unreadable."""
    ok, out = _labctl("dsm", "0x77")
    if not ok:
        return None
    parts = out.split()
    if len(parts) < 3:
        return None
    try:
        return int(parts[2], 16) or None   # 0 = no limit
    except ValueError:
        return None


# ------------------------------- UI -------------------------------
ACCENT = "#6c8cff"
SINGLETON = "a5control.singleton"   # QLocalServer name for the single-instance guard
STYLE = """
* { color: #eceef4; font-family: 'Inter','Segoe UI','Noto Sans',sans-serif; font-size: 14px; }
QMainWindow, QWidget { background: #0f1116; }
QToolTip { background: #191c24; color: #eceef4; border: 1px solid #2a2f3c; padding: 6px; border-radius: 6px; }

/* top navigation as a segmented control */
QTabWidget::pane { border: none; top: 4px; }
QTabBar { qproperty-drawBase: 0; }
QTabBar::tab {
    background: #171a22; color: #9aa0b0; padding: 9px 22px; margin-right: 6px;
    border: 1px solid transparent; border-radius: 10px; font-weight: 600;
}
QTabBar::tab:hover { color: #eceef4; background: #1d212b; }
QTabBar::tab:selected { color: #ffffff; background: #6c8cff; }

#card { background: #191c24; border-radius: 16px; border: 1px solid rgba(255,255,255,0.05); }
#h1 { font-size: 15px; font-weight: 700; }
#temp { font-size: 34px; font-weight: 800; }
#big { font-size: 30px; font-weight: 800; }
#muted { color: #9aa0b0; }
#chip { color: #9aa0b0; background: #12141a; border: 1px solid rgba(255,255,255,0.06);
        border-radius: 8px; padding: 3px 10px; }

QPushButton { background: #232833; border: none; border-radius: 10px; padding: 10px 16px; color: #eceef4; }
QPushButton:hover { background: #2c3240; }
QPushButton:pressed { background: #333a49; }
QPushButton#accent { background: #6c8cff; color: #0b0d12; font-weight: 700; }
QPushButton#accent:hover { background: #829dff; }
QPushButton#seg { background: #171a22; border: 1px solid rgba(255,255,255,0.06); color: #9aa0b0; font-weight: 600; }
QPushButton#seg:checked { background: #6c8cff; color: #0b0d12; }
QPushButton#toggle:checked { background: #3ddc84; color: #0b0d12; font-weight: 700; }
QPushButton#toggle:checked:hover { background: #55e896; }

QSlider::groove:horizontal { height: 6px; background: #232833; border-radius: 3px; }
QSlider::handle:horizontal { width: 18px; height: 18px; background: #ffffff; border: 3px solid #6c8cff;
    border-radius: 9px; margin: -7px 0; }
QSlider::handle:horizontal:hover { border-color: #829dff; }
QSlider::sub-page:horizontal { background: #6c8cff; border-radius: 3px; }
QSlider::groove:horizontal:disabled { background: #1a1d25; }
QSlider::handle:horizontal:disabled { background: #2a2f3c; border-color: #2a2f3c; }
QSlider::sub-page:horizontal:disabled { background: #2a2f3c; }

QMenu { background: #191c24; border: 1px solid #2a2f3c; border-radius: 10px; padding: 6px; }
QMenu::item { padding: 7px 22px; border-radius: 6px; }
QMenu::item:selected { background: #6c8cff; color: #0b0d12; }
QMenu::separator { height: 1px; background: #2a2f3c; margin: 5px 8px; }

QScrollArea { border: none; background: transparent; }
QScrollArea > QWidget > QWidget { background: transparent; }
QScrollBar:vertical { background: transparent; width: 10px; margin: 2px 2px 2px 0; }
QScrollBar::handle:vertical { background: #2c3240; border-radius: 5px; min-height: 32px; }
QScrollBar::handle:vertical:hover { background: #3a4150; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { background: transparent; }
"""


class CurveGraph(QWidget):
    """Interactive fan-curve editor + live CPU-temp marker.
    X = CPU °C (T_MIN..T_MAX), Y = duty % (FAN_MIN_PCT..100).
    Drag a point to move it · double-click empty space to add · double-click a point to remove.
    `on_change(points)` fires whenever the curve is edited. Data logic (add/move/remove) is pure
    and unit-tested; only the pixel<->data mapping lives in the mouse handlers."""
    T_MIN, T_MAX = 30, 100
    PAD = 24
    HIT = 14   # px radius to grab a point

    def __init__(self):
        super().__init__()
        self.setMinimumHeight(180)
        self.points = [list(p) for p in FAN_CURVE_DEFAULT]  # mutable [temp, duty]
        self.temp = None
        self.on_change = None
        self._drag = None

    def set_points(self, pts):
        self.points = sorted([list(p) for p in pts]); self.update()

    def set_temp(self, t):
        self.temp = t; self.update()

    # --- pure data ops (tested) ---
    def add_point(self, temp, duty):
        self.points.append([self._clampT(temp), self._clampD(duty)])
        self.points.sort()
        self._changed()

    def remove_point(self, idx):
        if len(self.points) > 2 and 0 <= idx < len(self.points):
            del self.points[idx]
            self._changed()

    def move_point(self, idx, temp, duty):
        self.points[idx] = [self._clampT(temp), self._clampD(duty)]
        self._changed()

    def _clampT(self, t): return int(max(self.T_MIN, min(self.T_MAX, round(t))))
    def _clampD(self, d): return int(max(FAN_MIN_PCT, min(100, round(d))))

    def _changed(self):
        self.update()
        if self.on_change:
            self.on_change([tuple(p) for p in self.points])

    # --- pixel <-> data mapping ---
    def _plot(self):
        return self.width() - 2 * self.PAD, self.height() - 2 * self.PAD

    def _xy(self, t, d):
        pw, ph = self._plot()
        x = self.PAD + (t - self.T_MIN) / (self.T_MAX - self.T_MIN) * pw
        y = self.PAD + ph - d / 100 * ph
        return QPointF(x, y)

    def _data(self, pos):
        pw, ph = self._plot()
        t = self.T_MIN + (pos.x() - self.PAD) / max(pw, 1) * (self.T_MAX - self.T_MIN)
        d = (self.PAD + ph - pos.y()) / max(ph, 1) * 100
        return t, d

    def _point_at(self, pos):
        for i, (t, d) in enumerate(self.points):
            c = self._xy(t, d)
            if (c.x() - pos.x()) ** 2 + (c.y() - pos.y()) ** 2 <= self.HIT ** 2:
                return i
        return None

    # --- mouse ---
    def mousePressEvent(self, e):
        self._drag = self._point_at(e.position())

    def mouseMoveEvent(self, e):
        if self._drag is not None:
            self.move_point(self._drag, *self._data(e.position()))

    def mouseReleaseEvent(self, e):
        self._drag = None

    def mouseDoubleClickEvent(self, e):
        idx = self._point_at(e.position())
        if idx is not None:
            self.remove_point(idx)
        else:
            self.add_point(*self._data(e.position()))

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = self.width(), self.height()
        p.setPen(QPen(QColor("#2a2f3c"), 1))            # horizontal grid at 0/25/50/75/100%
        for frac in (0, .25, .5, .75, 1):
            y = self.PAD + (h - 2 * self.PAD) * (1 - frac)
            p.drawLine(int(self.PAD), int(y), int(w - self.PAD), int(y))
        pts = sorted(self.points)
        p.setPen(QPen(QColor("#5b8cff"), 2))
        p.drawPolyline(QPolygonF([self._xy(t, d) for t, d in pts]))
        p.setBrush(QColor("#5b8cff")); p.setPen(QPen(QColor("#cbd6ff"), 1))
        for t, d in self.points:
            p.drawEllipse(self._xy(t, d), 5, 5)
        if self.temp is not None:                       # live temp line + duty dot
            tc = max(self.T_MIN, min(self.T_MAX, self.temp))
            duty = curve_duty(self.points, self.temp)
            x = self._xy(tc, 0).x()
            p.setPen(QPen(QColor("#3ddc84"), 1, Qt.PenStyle.DashLine))
            p.drawLine(int(x), int(self.PAD), int(x), int(h - self.PAD))
            p.setBrush(QColor("#3ddc84")); p.setPen(Qt.PenStyle.NoPen)
            p.drawEllipse(self._xy(tc, duty), 5, 5)
            p.setPen(QColor("#3ddc84"))
            p.drawText(int(min(x + 6, w - self.PAD - 62)), int(self.PAD + 12), f"{self.temp:.0f}° → {duty}%")
        p.setPen(QColor("#8a90a0"))
        p.drawText(int(self.PAD), h - 6, f"{self.T_MIN}°")
        p.drawText(int(w - self.PAD - 22), h - 6, f"{self.T_MAX}°")


def card(*widgets):
    f = QFrame(); f.setObjectName("card")
    lay = QVBoxLayout(f); lay.setContentsMargins(18, 16, 18, 16); lay.setSpacing(10)
    for w in widgets:
        lay.addWidget(w) if isinstance(w, QWidget) else lay.addLayout(w)
    return f


class Panel(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("A5 Control")
        self.setMinimumSize(520, 480)
        self.resize(600, 740)
        self.setStyleSheet(STYLE)
        import os
        _icon = os.path.join(os.path.dirname(os.path.abspath(__file__)), "icon.svg")
        if os.path.exists(_icon):
            self.setWindowIcon(QIcon(_icon))
        self._last_cpu = None          # latest CPU temp, for the software fan curve
        self._last_curve_duty = None   # throttle: last duty the curve wrote

        root = QWidget(); self.setCentralWidget(root)
        outer = QVBoxLayout(root); outer.setContentsMargins(16, 16, 16, 16); outer.setSpacing(14)

        # header: live temps
        self.cpu_lbl = QLabel("–°"); self.cpu_lbl.setObjectName("temp")
        self.gpu_lbl = QLabel("–°"); self.gpu_lbl.setObjectName("temp")
        outer.addWidget(card(self._stat_row()))

        tabs = QTabWidget()
        tabs.addTab(self._scroll(self._perf_tab()), "Performance")
        tabs.addTab(self._scroll(self._rgb_tab()), "RGB")
        tabs.addTab(self._scroll(self._fan_tab()), "Fans")
        outer.addWidget(tabs, 1)

        self.timer = QTimer(self); self.timer.timeout.connect(self._refresh_temps)
        self.timer.start(2000); self._refresh_temps()

        # live fan/CPU sample: poll the NOPASSWD labctl (acpi_call -> firmware) every 1s.
        # No pkexec/polkit -> no password prompt. Non-blocking QProcess so the UI never stalls.
        self._mon_proc = None
        self.mon_timer = QTimer(self); self.mon_timer.timeout.connect(self._poll_monitor)
        self.mon_timer.start(1000); self._poll_monitor()

        self._setup_tray()
        self._setup_singleton()

    # ---- single instance: a second launch just raises this window (no duplicate icon) ----
    def _setup_singleton(self):
        self._srv = QLocalServer(self)
        QLocalServer.removeServer(SINGLETON)      # clear any stale socket from a crash
        self._srv.newConnection.connect(self._on_second_launch)
        self._srv.listen(SINGLETON)

    def _on_second_launch(self):
        c = self._srv.nextPendingConnection()
        if c is not None:
            c.readAll()
            c.disconnectFromServer()
        self._show_normal()

    # ---- system tray: keep running when the window is closed ----
    def _setup_tray(self):
        self._really_quit = False
        self._tray_hinted = False
        self.tray = None
        if not QSystemTrayIcon.isSystemTrayAvailable():
            return
        icon = self.windowIcon()
        if icon.isNull():
            icon = QIcon.fromTheme("input-keyboard")
        self.tray = QSystemTrayIcon(icon, self)
        self.tray.setToolTip("A5 Control")
        menu = QMenu()
        menu.addAction("Open A5 Control", self._show_normal)
        menu.addSeparator()
        kb = menu.addMenu("Keyboard colour")
        for name, rgb in self.KBD_PRESETS:
            act = QAction(name, self)
            act.triggered.connect(lambda _=False, c=rgb: self._tray_set_color(c))
            kb.addAction(act)
        kb.addSeparator()
        rain = QAction("Rainbow", self)
        rain.triggered.connect(lambda: (self._seg_rainbow.setChecked(True), self._set_effect("rainbow")))
        kb.addAction(rain)
        off = QAction("Off", self); off.triggered.connect(self._kbd_off)
        kb.addAction(off)
        fanm = menu.addMenu("Fan preset")
        for name, pct in FAN_PRESETS.items():
            act = QAction(name, self)
            act.triggered.connect(lambda _=False, p=pct: self._fan_preset(p))
            fanm.addAction(act)
        auto = QAction("Auto", self)
        auto.triggered.connect(lambda: self.fan_manual_btn.setChecked(False))
        fanm.addAction(auto)
        menu.addSeparator()
        menu.addAction("Quit", self._quit)
        self.tray.setContextMenu(menu)          # right-click -> quick options
        self.tray.activated.connect(self._tray_activated)
        self.tray.show()

    def _tray_set_color(self, rgb):
        if self._kbd_effect == "rainbow":       # a solid colour means leave the cycle
            self._seg_static.setChecked(True); self._set_effect("static")
        self._apply_kbd(rgb, max(1, self._kbd_level))

    def _tray_activated(self, reason):
        if reason == QSystemTrayIcon.ActivationReason.Trigger:   # left click -> open
            self._show_normal()

    def _show_normal(self):
        self.showNormal(); self.raise_(); self.activateWindow()

    def _quit(self):
        self._really_quit = True
        self.close()
        QApplication.quit()

    def _poll_monitor(self):
        if self._mon_proc is not None and \
           self._mon_proc.state() != QProcess.ProcessState.NotRunning:
            return   # previous sample still running; skip this tick
        self._mon_proc = QProcess(self)
        self._mon_proc.finished.connect(self._on_monitor)
        self._mon_proc.start("sudo", [LABCTL, "mon"])

    def _on_monitor(self, *args):
        import json
        out = bytes(self._mon_proc.readAllStandardOutput()).decode(errors="ignore")
        for line in out.splitlines():
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                d = json.loads(line)
            except ValueError:
                continue
            if d.get("cpu") is not None:
                self._last_cpu = d["cpu"]
                self._set_temp(self.cpu_lbl, d["cpu"])
                self.curve_graph.set_temp(d["cpu"])   # live marker tracks temp in any mode
                self._fan_watchdog(d["cpu"])
                self._curve_tick(d["cpu"])
            self.fan_duty.setText(f"{d['duty']}%" if d.get("duty") is not None else "--")
            r1, r2 = d.get("rpm1"), d.get("rpm2")
            self.fan_rpm.setText(f"{r1} / {r2}" if r1 else "--")

    def _scroll(self, inner):
        """Wrap a tab so long content scrolls instead of overlapping in a short window."""
        sa = QScrollArea(); sa.setWidgetResizable(True); sa.setFrameShape(QFrame.Shape.NoFrame)
        sa.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        sa.setWidget(inner)
        return sa

    def _stat_row(self):
        row = QHBoxLayout(); row.setSpacing(12)
        for title, lbl in (("CPU", self.cpu_lbl), ("GPU", self.gpu_lbl)):
            box = QVBoxLayout(); box.setSpacing(2)
            t = QLabel(title.upper()); t.setObjectName("muted")
            t.setStyleSheet("font-size:11px; font-weight:700; letter-spacing:1px;")
            box.addWidget(t); box.addWidget(lbl)
            row.addLayout(box)
        row.addStretch()
        title = QLabel("A5 Control"); title.setObjectName("muted")
        title.setStyleSheet("font-size:12px; font-weight:700; letter-spacing:1px;")
        row.addWidget(title, 0, Qt.AlignmentFlag.AlignBottom)
        return row

    def _set_temp(self, lbl, val):
        lbl.setText(f"{val:.0f}°" if val else "–°")
        col = "#ff5b6e" if val and val >= 85 else "#ffb45b" if val and val >= 75 else "#e8eaf0"
        lbl.setStyleSheet(f"color:{col};")

    def _refresh_temps(self):
        # GPU always from sensors; CPU from sensors as fallback until the stream lands
        t = read_temps()
        self._set_temp(self.gpu_lbl, t["gpu"])
        if self.cpu_lbl.text() == "–°":
            self._set_temp(self.cpu_lbl, t["cpu"])

    # ---- Performance ----
    def _perf_tab(self):
        w = QWidget(); lay = QVBoxLayout(w); lay.setSpacing(14)
        h = QLabel("Power limit (TDP)"); h.setObjectName("h1")

        preset_row = QHBoxLayout()
        for name, watts in PRESETS.items():
            b = QPushButton(name)
            b.clicked.connect(lambda _, x=watts, n=name: self._apply_tdp(x, PPD_FOR.get(n)))
            preset_row.addWidget(b)

        self.tdp_slider = QSlider(Qt.Orientation.Horizontal)
        self.tdp_slider.setRange(TDP_MIN, TDP_MAX); self.tdp_slider.setValue(TDP_STOCK)
        self.tdp_val = QLabel(f"{TDP_STOCK} W"); self.tdp_val.setObjectName("h1")
        self.tdp_slider.valueChanged.connect(lambda v: self.tdp_val.setText(f"{v} W"))
        apply_btn = QPushButton("Apply"); apply_btn.setObjectName("accent")
        apply_btn.clicked.connect(lambda: self._apply_tdp(self.tdp_slider.value()))

        note = QLabel("Lower TDP = cooler & quieter. Above %dW exceeds stock." % TDP_STOCK)
        note.setObjectName("muted"); note.setWordWrap(True)

        lay.addWidget(card(h, preset_row))
        srow = QHBoxLayout(); srow.addWidget(self.tdp_slider, 1); srow.addWidget(self.tdp_val)
        lay.addWidget(card(srow, apply_btn, note))

        # battery charge limit (EC BTCP via _DSM 0x76), protects longevity on AC
        bh = QLabel("Battery charge limit"); bh.setObjectName("h1")
        self.bat_lbl = QLabel("current: –"); self.bat_lbl.setObjectName("muted")
        brow = QHBoxLayout()
        for name, stop in (("60%", 60), ("80%", 80), ("100% (off)", 0)):
            b = QPushButton(name); b.clicked.connect(lambda _, x=stop: self._apply_bat(x))
            brow.addWidget(b)
        bnote = QLabel("Caps charging to spare the battery when mostly on AC. Stored in the EC, "
                       "persists across reboots until changed. Needs the kernel module loaded.")
        bnote.setObjectName("muted"); bnote.setWordWrap(True)
        lay.addWidget(card(bh, brow, self.bat_lbl, bnote))
        lay.addStretch()
        self._refresh_bat()
        return w

    def _refresh_bat(self):
        lim = battery_read_limit()
        self.bat_lbl.setText(f"current: {'no limit (100%)' if not lim else str(lim) + '%'}")

    def _apply_bat(self, stop):
        if not fan_module_ready():
            ok, msg = fan_ensure_module()   # same a5ctl module drives 0x76
            if not ok:
                QMessageBox.critical(self, "Battery", f"Kernel module not available.\n{msg}")
                return
        ok, msg = battery_set_limit(stop)
        if ok:
            self._refresh_bat()
        else:
            QMessageBox.critical(self, "Battery", f"Could not set limit.\n{msg}")

    def _apply_tdp(self, watts, profile=None):
        w = clamp_tdp(watts)
        if w > TDP_STOCK:
            if QMessageBox.warning(
                self, "Above stock power",
                f"Set {w}W? This exceeds the stock {TDP_STOCK}W limit and will run hotter.",
                QMessageBox.StandardButton.Ok | QMessageBox.StandardButton.Cancel) != QMessageBox.StandardButton.Ok:
                return
        self.tdp_slider.setValue(w)
        ok, msg = set_tdp(w)
        if ok and profile:
            set_power_profile(profile)   # keep KDE's profile in step with the preset
        (QMessageBox.information if ok else QMessageBox.critical)(self, "Power", msg)

    # ---- RGB ----
    KBD_PRESETS = [("Red", (255, 0, 0)), ("Orange", (255, 70, 0)), ("Yellow", (255, 200, 0)),
                   ("Green", (0, 255, 0)), ("Cyan", (0, 255, 200)), ("Blue", (0, 40, 255)),
                   ("Purple", (160, 0, 255)), ("Pink", (255, 0, 140)), ("White", (255, 255, 255))]

    def _rgb_tab(self):
        w = QWidget(); lay = QVBoxLayout(w); lay.setSpacing(14)
        self._kbd_color = (91, 140, 255)          # working colour
        self._kbd_level = 255                     # working brightness 0-255
        self._kbd_effect = "static"
        self._rainbow_stops = rainbow_stops(7)    # editable cycle palette (default = rainbow)
        self._rainbow_phase = 0.0
        self._rainbow_secs = 8.0                  # seconds per full loop
        self._rainbow_timer = QTimer(self)
        self._rainbow_timer.timeout.connect(self._rainbow_tick)

        # big live preview
        self._kbd_swatch = QPushButton(); self._kbd_swatch.setObjectName("kbdswatch")
        self._kbd_swatch.setFixedHeight(66)
        self._kbd_swatch.setCursor(Qt.CursorShape.PointingHandCursor)
        self._kbd_swatch.clicked.connect(self._pick_kbd_color)

        # Static | Rainbow segmented switch
        seg = QHBoxLayout(); seg.setSpacing(0)
        self._seg_static = QPushButton("Static"); self._seg_rainbow = QPushButton("Rainbow")
        for b, mode in ((self._seg_static, "static"), (self._seg_rainbow, "rainbow")):
            b.setObjectName("seg"); b.setCheckable(True); b.setAutoExclusive(True)
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.clicked.connect(lambda _=False, m=mode: self._set_effect(m))
            seg.addWidget(b)
        self._seg_static.setChecked(True)

        # --- static controls: preset swatches ---
        self._static_box = QWidget(); sb = QVBoxLayout(self._static_box)
        sb.setContentsMargins(0, 0, 0, 0)
        grid = QGridLayout(); grid.setSpacing(8)
        for i, (name, rgb) in enumerate(self.KBD_PRESETS):
            b = QPushButton(); b.setToolTip(name); b.setFixedSize(46, 34)
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.setStyleSheet(f"background:#{rgb[0]:02x}{rgb[1]:02x}{rgb[2]:02x};"
                            "border:1px solid rgba(255,255,255,0.15); border-radius:8px;")
            b.clicked.connect(lambda _=False, c=rgb: self._apply_kbd(c, max(1, self._kbd_level)))
            grid.addWidget(b, i // 5, i % 5)
        sb.addLayout(grid)

        # --- rainbow controls: editable palette + speed ---
        self._rainbow_box = QWidget(); rb = QVBoxLayout(self._rainbow_box)
        rb.setContentsMargins(0, 0, 0, 0); rb.setSpacing(10)
        hint = QLabel("Cycles through these colours. Click one to edit · right-click to remove.")
        hint.setObjectName("muted"); hint.setWordWrap(True)
        self._stops_row = QHBoxLayout(); self._stops_row.setSpacing(8)
        self._stops_row.setContentsMargins(0, 0, 0, 0)
        stops_holder = QWidget(); stops_holder.setLayout(self._stops_row)
        stops_holder.setMinimumHeight(40)
        editrow = QHBoxLayout()
        addb = QPushButton("＋ Add colour"); addb.clicked.connect(self._rainbow_add)
        resb = QPushButton("Reset to rainbow"); resb.clicked.connect(self._rainbow_reset)
        editrow.addWidget(addb); editrow.addWidget(resb); editrow.addStretch()
        self.rain_speed = QSlider(Qt.Orientation.Horizontal)
        self.rain_speed.setRange(2, 30); self.rain_speed.setValue(int(self._rainbow_secs))
        self.rain_speed.setInvertedAppearance(True)   # right = faster
        self.rain_speed.valueChanged.connect(self._rain_speed_changed)
        srow = QHBoxLayout(); srow.addWidget(QLabel("Speed")); srow.addWidget(self.rain_speed, 1)
        rb.addWidget(hint); rb.addWidget(stops_holder); rb.addLayout(editrow); rb.addLayout(srow)
        self._rebuild_stops()
        self._rainbow_box.setVisible(False)

        # brightness (shared), debounced so dragging never machine-guns the firmware
        self.kbd_bright = QSlider(Qt.Orientation.Horizontal)
        self.kbd_bright.setRange(0, 255); self.kbd_bright.setValue(255)
        self.kbd_bright_pct = QLabel("100%"); self.kbd_bright_pct.setObjectName("muted")
        self.kbd_bright_pct.setFixedWidth(44)
        self._kbd_bright_timer = QTimer(self); self._kbd_bright_timer.setSingleShot(True)
        self._kbd_bright_timer.setInterval(160)
        self._kbd_bright_timer.timeout.connect(self._apply_bright)
        self.kbd_bright.valueChanged.connect(self._on_bright_slide)
        brow = QHBoxLayout(); brow.addWidget(QLabel("Brightness"))
        brow.addWidget(self.kbd_bright, 1); brow.addWidget(self.kbd_bright_pct)

        offbtn = QPushButton("Off"); offbtn.clicked.connect(self._kbd_off)
        self.kbd_status = QLabel(); self.kbd_status.setObjectName("muted"); self.kbd_status.setWordWrap(True)

        lay.addWidget(card(QLabel("<b>Keyboard backlight</b>"), self._kbd_swatch, seg,
                           self._static_box, self._rainbow_box, brow, offbtn, self.kbd_status))
        lay.addStretch()
        self._refresh_swatch()
        return w

    # ---- effect switching ----
    def _set_effect(self, mode):
        self._kbd_effect = mode
        self._static_box.setVisible(mode == "static")
        self._rainbow_box.setVisible(mode == "rainbow")
        if mode == "rainbow":
            self._start_rainbow()
        else:
            self._rainbow_timer.stop()
            self._apply_kbd(self._kbd_color, max(1, self._kbd_level))

    def _start_rainbow(self):
        col = cycle_color(self._rainbow_stops, self._rainbow_phase)
        ok, msg = kbd_set(*col, max(1, self._kbd_level))   # mode + brightness once
        if not ok:
            self.kbd_status.setText(f"Failed: {msg}"); self._seg_static.setChecked(True)
            self._set_effect("static"); return
        self._kbd_color = col; self._refresh_swatch()
        self._rainbow_timer.start(200)                     # ~5 fps; one _DSM colour write per frame
        self.kbd_status.setText("Rainbow cycling. ponytail: 5 fps steady, plenty smooth, gentle on ACPI.")

    def _rainbow_tick(self):
        self._rainbow_phase = (self._rainbow_phase + 0.2 / max(1.0, self._rainbow_secs)) % 1.0
        col = cycle_color(self._rainbow_stops, self._rainbow_phase)
        self._kbd_color = col
        kbd_color_only(*col)                               # single colour write per frame
        self._refresh_swatch()

    def _rain_speed_changed(self, v):
        self._rainbow_secs = float(v)

    # ---- editable rainbow palette ----
    def _rebuild_stops(self):
        while self._stops_row.count():
            it = self._stops_row.takeAt(0)
            if it.widget():
                it.widget().deleteLater()
        for i, rgb in enumerate(self._rainbow_stops):
            chip = QPushButton(); chip.setFixedSize(40, 32)
            chip.setCursor(Qt.CursorShape.PointingHandCursor)
            chip.setStyleSheet(f"background:#{rgb[0]:02x}{rgb[1]:02x}{rgb[2]:02x};"
                               "border:1px solid rgba(255,255,255,0.2); border-radius:8px;")
            chip.clicked.connect(lambda _=False, idx=i: self._rainbow_edit(idx))
            chip.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
            chip.customContextMenuRequested.connect(lambda _=None, idx=i: self._rainbow_remove(idx))
            self._stops_row.addWidget(chip)
        self._stops_row.addStretch()

    def _rainbow_edit(self, idx):
        c = QColorDialog.getColor(QColor(*self._rainbow_stops[idx]), self, "Cycle colour")
        if c.isValid():
            self._rainbow_stops[idx] = (c.red(), c.green(), c.blue())
            self._rebuild_stops()

    def _rainbow_add(self):
        c = QColorDialog.getColor(QColor(255, 255, 255), self, "Add cycle colour")
        if c.isValid():
            self._rainbow_stops.append((c.red(), c.green(), c.blue()))
            self._rebuild_stops()

    def _rainbow_remove(self, idx):
        if len(self._rainbow_stops) > 2:      # keep at least two to cycle between
            del self._rainbow_stops[idx]
            self._rebuild_stops()

    def _rainbow_reset(self):
        self._rainbow_stops = rainbow_stops(7)
        self._rebuild_stops()

    # ---- shared colour/brightness ----
    def _refresh_swatch(self):
        r, g, b = self._kbd_color
        fg = "#000" if (r * 0.299 + g * 0.587 + b * 0.114) > 150 else "#fff"
        self._kbd_swatch.setStyleSheet(
            f"QPushButton#kbdswatch{{background:#{r:02x}{g:02x}{b:02x};color:{fg};"
            f"border:none;border-radius:14px;font-weight:700;font-size:15px;}}")
        label = "rainbow" if self._kbd_effect == "rainbow" else "click to pick a colour"
        self._kbd_swatch.setText(f"#{r:02X}{g:02X}{b:02X}   ·   {label}")

    def _pick_kbd_color(self):
        c = QColorDialog.getColor(QColor(*self._kbd_color), self, "Keyboard colour")
        if c.isValid():
            if self._kbd_effect != "static":     # picking a solid colour means static
                self._seg_static.setChecked(True); self._rainbow_timer.stop()
                self._kbd_effect = "static"
                self._static_box.setVisible(True); self._rainbow_box.setVisible(False)
            self._apply_kbd((c.red(), c.green(), c.blue()), max(1, self._kbd_level))

    def _on_bright_slide(self, v):
        self.kbd_bright_pct.setText(f"{round(v / 255 * 100)}%")
        self._kbd_bright_timer.start()          # apply 160ms after the last move

    def _apply_bright(self):
        lvl = self.kbd_bright.value(); self._kbd_level = lvl
        if self._kbd_effect == "rainbow":
            fan_ensure_module(); _labctl("dsm", *kbd_bright_args(lvl))   # brightness only; cycle keeps colour
        else:
            self._apply_kbd(self._kbd_color, lvl)

    def _kbd_off(self):
        self._rainbow_timer.stop()
        self.kbd_bright.blockSignals(True); self.kbd_bright.setValue(0)
        self.kbd_bright.blockSignals(False); self.kbd_bright_pct.setText("0%")
        self._kbd_level = 0
        if self._kbd_effect == "rainbow":
            fan_ensure_module(); _labctl("dsm", *kbd_bright_args(0))
        else:
            self._apply_kbd(self._kbd_color, 0)

    def _apply_kbd(self, rgb, level):
        ok, msg = kbd_set(*rgb, level)
        if ok:
            self._kbd_color = tuple(rgb); self._kbd_level = level
            if self.kbd_bright.value() != level:
                self.kbd_bright.blockSignals(True); self.kbd_bright.setValue(level)
                self.kbd_bright.blockSignals(False)
                self.kbd_bright_pct.setText(f"{round(level / 255 * 100)}%")
            self._refresh_swatch()
            self.kbd_status.setText(
                f"Applied #{rgb[0]:02X}{rgb[1]:02X}{rgb[2]:02X} at {round(level / 255 * 100)}% brightness.")
        else:
            self.kbd_status.setText(f"Failed: {msg}")

    # ---- Fans ----
    def _fan_tab(self):
        w = QWidget(); lay = QVBoxLayout(w); lay.setSpacing(14)

        # live telemetry as two stat tiles, matching the header
        self.fan_duty = QLabel("--"); self.fan_duty.setObjectName("big")
        self.fan_rpm = QLabel("--"); self.fan_rpm.setObjectName("big")
        tele = QHBoxLayout(); tele.setSpacing(12)
        for title, lbl in (("Duty", self.fan_duty), ("Speed (rpm)", self.fan_rpm)):
            box = QVBoxLayout(); box.setSpacing(2)
            t = QLabel(title.upper()); t.setObjectName("muted")
            t.setStyleSheet("font-size:11px; font-weight:700; letter-spacing:1px;")
            box.addWidget(t); box.addWidget(lbl); tele.addLayout(box); tele.addSpacing(18)
        tele.addStretch()
        live = card(QLabel("<b>Live telemetry</b>"), tele)

        # manual control: a prominent toggle, then presets and per-fan sliders unlock
        self.fan_manual_btn = QPushButton("Manual control:  OFF")
        self.fan_manual_btn.setObjectName("toggle"); self.fan_manual_btn.setCheckable(True)
        self.fan_manual_btn.setMinimumHeight(42)
        self.fan_manual_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.fan_manual_btn.toggled.connect(self._toggle_manual)

        preset_lbl = QLabel("PRESETS"); preset_lbl.setObjectName("muted")
        preset_lbl.setStyleSheet("font-size:11px; font-weight:700; letter-spacing:1px;")
        preset_row = QHBoxLayout(); preset_row.setSpacing(8)
        for name, pct in FAN_PRESETS.items():
            b = QPushButton(name); b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.clicked.connect(lambda _, x=pct: self._fan_preset(x))
            preset_row.addWidget(b)

        # apply slider moves 150ms after the last change: covers drag, groove-click AND
        # keyboard (sliderReleased missed the last two), and throttles rapid drags.
        self._fan_debounce = QTimer(self); self._fan_debounce.setSingleShot(True)
        self._fan_debounce.setInterval(150)
        self._fan_debounce.timeout.connect(self._fan_apply)

        # one slider per fan (0x68 addresses fans independently)
        self.fan_sliders, self.fan_slbls = [], []
        slider_box = QVBoxLayout(); slider_box.setSpacing(10)
        for i in (1, 2):
            s = QSlider(Qt.Orientation.Horizontal)
            s.setRange(FAN_MIN_PCT, 100); s.setValue(50); s.setEnabled(False)
            lbl = QLabel("50%"); lbl.setFixedWidth(44)
            s.valueChanged.connect(lambda v, L=lbl: self._fan_slider_changed(v, L))
            r = QHBoxLayout()
            name = QLabel(f"Fan {i}"); name.setObjectName("muted"); name.setMinimumWidth(46)
            r.addWidget(name); r.addWidget(s, 1); r.addWidget(lbl)
            slider_box.addLayout(r)
            self.fan_sliders.append(s); self.fan_slbls.append(lbl)

        manual_note = QLabel(
            f"Duty is floored at {FAN_MIN_PCT}% and reverts to firmware control automatically if the "
            f"CPU reaches {FAN_WATCHDOG_C}°C or the app closes. All writes go through the mutex-protected "
            f"firmware method, never raw EC pokes.")
        manual_note.setObjectName("muted"); manual_note.setWordWrap(True)

        manual = card(self.fan_manual_btn, preset_lbl, preset_row, slider_box, manual_note)

        # auto curve: its own card with the interactive graph editor
        self.curve_chk = QCheckBox("Auto curve (CPU temp to duty)")  # always clickable; turns on manual
        self.curve_chk.toggled.connect(self._curve_toggled)
        self.curve_graph = CurveGraph()   # always editable; applying it depends on curve mode
        self.curve_graph.on_change = self._on_curve_changed
        hint = QLabel("Drag a point to move it, double-click empty space to add one, "
                      "double-click a point to remove it.")
        hint.setObjectName("muted"); hint.setWordWrap(True)
        curve = card(self.curve_chk, self.curve_graph, hint)

        lay.addWidget(live); lay.addWidget(manual); lay.addWidget(curve)
        lay.addStretch()
        return w

    def _fan_slider_changed(self, v, lbl):
        lbl.setText(f"{v}%")
        self._fan_debounce.start()   # apply shortly after the last move

    def _on_curve_changed(self, _pts=None):
        if self.fan_manual_btn.isChecked() and self.curve_chk.isChecked():
            self._last_curve_duty = None            # force re-apply with the edited curve
            self._curve_tick(self._last_cpu)

    def _sync_fan_controls(self):
        on = self.fan_manual_btn.isChecked()
        curve = on and self.curve_chk.isChecked()
        for s in self.fan_sliders:
            s.setEnabled(on and not curve)   # curve drives the sliders; user drives them otherwise

    def _toggle_manual(self, on):
        self.fan_manual_btn.setText(f"Manual control:  {'ON' if on else 'OFF'}")
        if on:
            ok, msg = fan_ensure_module()
            if not ok:
                self.fan_manual_btn.setChecked(False)  # re-enters here with on=False
                QMessageBox.critical(self, "Fans", f"Kernel module not available.\n{msg}")
                return
            self._sync_fan_controls()
            if self.curve_chk.isChecked():           # resume the curve if it's on
                self._last_curve_duty = None
                self._curve_tick(self._last_cpu)
            else:
                self._fan_apply()
        else:
            self._sync_fan_controls()
            ok, msg = fan_set_auto()
            if not ok:
                QMessageBox.critical(self, "Fans", f"Could not restore auto.\n{msg}")

    def _apply_duties(self, f1, f2):
        # non-blocking: a blocking sudo call freezes the UI mid-drag/tick. Module is already
        # loaded (toggle ensured it), so fire-and-forget is fine. NOTE the "dsm" subcommand -
        # omitting it makes labctl a no-op (the bug that silently broke all GUI fan writes).
        QProcess.startDetached("sudo", [LABCTL, "dsm", *fan_duty_args(f1, f2)])

    def _fan_apply(self):
        """Push the two slider values (fixed mode only)."""
        if not self.fan_manual_btn.isChecked() or self.curve_chk.isChecked():
            return
        self._apply_duties(self.fan_sliders[0].value(), self.fan_sliders[1].value())

    def _fan_preset(self, pct):
        if not self.fan_manual_btn.isChecked():
            self.fan_manual_btn.setChecked(True)      # loads module + enables controls
            if not self.fan_manual_btn.isChecked():   # load failed (already warned)
                return
        self.curve_chk.setChecked(False)              # a preset is a fixed duty
        for s in self.fan_sliders:
            s.setValue(pct)
        self._fan_apply()

    def _curve_toggled(self, on):
        if on and not self.fan_manual_btn.isChecked():
            self.fan_manual_btn.setChecked(True)      # switch to manual, like the presets do
            if not self.fan_manual_btn.isChecked():   # module load failed (already warned)
                self.curve_chk.setChecked(False)
                return
        self._sync_fan_controls()
        if not self.fan_manual_btn.isChecked():
            return
        if on:
            self._last_curve_duty = None      # force the next tick to write
            self._curve_tick(self._last_cpu)  # apply immediately if a temp is known
        else:
            self._fan_apply()                 # back to the slider values

    def _curve_tick(self, cpu):
        """Drive both fans from the curve for the current CPU temp (curve mode only)."""
        if cpu is None or not self.fan_manual_btn.isChecked() or not self.curve_chk.isChecked():
            return
        duty = curve_duty(self.curve_graph.points, cpu)
        if duty == self._last_curve_duty:
            return                            # throttle: only write when the target changes
        self._last_curve_duty = duty
        for s, L in zip(self.fan_sliders, self.fan_slbls):  # reflect on the disabled sliders
            s.blockSignals(True); s.setValue(duty); s.blockSignals(False); L.setText(f"{duty}%")
        self._apply_duties(duty, duty)

    def _fan_watchdog(self, cpu):
        """Force auto if CPU is too hot while under manual control."""
        if cpu and cpu >= FAN_WATCHDOG_C and self.fan_manual_btn.isChecked():
            self.fan_manual_btn.setChecked(False)  # triggers revert-to-auto
            QMessageBox.warning(self, "Fans",
                f"CPU hit {cpu:.0f}°C, reverted fans to firmware control for safety.")

    def closeEvent(self, e):
        # X / close -> hide to tray and keep running (unless the user chose Quit)
        if self.tray is not None and not self._really_quit:
            e.ignore(); self.hide()
            if not self._tray_hinted:
                self.tray.showMessage(
                    "A5 Control", "Still running in the tray, left-click to open, "
                    "right-click for quick options.",
                    QSystemTrayIcon.MessageIcon.Information, 3000)
                self._tray_hinted = True
            return
        if self.fan_manual_btn.isChecked():
            fan_set_auto()
        super().closeEvent(e)


def _raise_existing() -> bool:
    """If another instance is already running, ping it to show itself and return True."""
    s = QLocalSocket()
    s.connectToServer(SINGLETON)
    if s.waitForConnected(200):
        s.write(b"show"); s.flush(); s.waitForBytesWritten(200)
        s.disconnectFromServer()
        return True
    return False


if __name__ == "__main__":
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)   # tray keeps the app alive after the window closes
    if _raise_existing():                  # don't start a second copy / second taskbar icon
        sys.exit(0)
    app.setDesktopFileName("laptop-control")   # let the taskbar group window + launcher as one
    w = Panel(); w.show()
    sys.exit(app.exec())
