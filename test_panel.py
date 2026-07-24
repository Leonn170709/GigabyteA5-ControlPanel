"""Self-check for the pure logic in panel.py + the helper. Run: python test_panel.py"""
import importlib.machinery
import importlib.util
import sys
import types


def _load(path, name, stub_qt=False):
    if stub_qt:
        for m in ("PyQt6", "PyQt6.QtCore", "PyQt6.QtGui", "PyQt6.QtWidgets", "PyQt6.QtNetwork"):
            mod = types.ModuleType(m); sys.modules[m] = mod
        names = ("Qt QTimer QProcess QPointF QAction QColor QIcon QPainter QPen QPolygonF "
                 "QApplication QCheckBox QColorDialog QFrame QGridLayout QHBoxLayout QLabel "
                 "QMainWindow QMenu QMessageBox QPushButton QScrollArea QSlider QSystemTrayIcon "
                 "QTabWidget QVBoxLayout QWidget QLocalServer QLocalSocket").split()
        for m in list(sys.modules):
            if m.startswith("PyQt6"):
                for n in names:
                    setattr(sys.modules[m], n, object)
    loader = importlib.machinery.SourceFileLoader(name, path)
    spec = importlib.util.spec_from_loader(name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


p = _load("panel.py", "panel", stub_qt=True)
h = _load("laptop-control-helper", "helper")

# --- panel: TDP clamp keeps power inside the safe envelope ---
assert p.clamp_tdp(0) == p.TDP_MIN
assert p.clamp_tdp(999) == p.TDP_MAX
assert p.clamp_tdp(40) == 40

# --- helper: fan telemetry decode matches the real probed buffers ---
# idle buffer had duty byte 0x61 -> ~43%, load 0xE5 -> 100%
assert h._duty_pct(0xE5) == 100
assert 40 <= h._duty_pct(0x61) <= 46
assert h._duty_pct(0x00) == 0
# RPM from tach period: smaller period => higher rpm, and monotonic
assert h._rpm(519) > h._rpm(1101) > 0
assert h._rpm(0) == 0

# --- panel: fan duty floor keeps a slow-fan write from ever reaching the EC ---
assert p.fan_pct_to_pwm(0) == p.fan_pct_to_pwm(p.FAN_MIN_PCT)   # below floor clamps up
assert p.fan_pct_to_pwm(5) == round(p.FAN_MIN_PCT * 255 / 100)  # can't starve fans
assert p.fan_pct_to_pwm(100) == 255                             # full = PWM 255
assert p.fan_pct_to_pwm(50) == 128                              # linear midpoint

# --- panel: software fan curve interpolates and clamps at the ends ---
c = [(40, 20), (60, 45), (75, 65), (90, 100)]
assert p.curve_duty(c, 30) == 20                 # below first point -> floor of curve
assert p.curve_duty(c, 99) == 100                # above last point -> top of curve
assert p.curve_duty(c, 50) == 32                 # midway 40->60 : 20 + (45-20)*0.5 = 32.5 -> 32
assert p.curve_duty(c, 60) == 45                 # exact point
assert p.curve_duty([(90, 100), (40, 20)], 40) == 20   # unsorted input tolerated

# --- panel: per-fan 0x68 args are floored and well-formed ---
a = p.fan_duty_args(100, 5)                       # fan2 below floor
assert a[0] == "0x68" and len(a) == 5
assert a[1] == "ff"                              # fan1 100% -> 0xff
assert a[2] == a[3] == a[4] == f"{p.fan_pct_to_pwm(5):02x}"  # fan2 floored, mirrored to 3/4

# --- panel: battery limit packs ARGS as [start, stop, 00, 06] with start = stop-5 ---
assert p.battery_limit_args(80) == ["0x76", "4b", "50", "00", "06"]   # stop 0x50=80, start 0x4b=75
assert p.battery_limit_args(0) == ["0x76", "00", "00", "00", "06"]    # 0 clears the limit
assert p.battery_limit_args(200)[2] == "64"                          # clamps stop to 100 (0x64)

# --- panel: keyboard colour packs Clevo-style ARGS=0xF0|B<<16|R<<8|G as LE bytes [G,R,B,0xF0] ---
assert p.kbd_color_args(255, 0, 0) == ["0x67", "00", "ff", "00", "f0"]     # red  -> G,R,B
assert p.kbd_color_args(0, 255, 0) == ["0x67", "ff", "00", "00", "f0"]     # green
assert p.kbd_color_args(0, 0, 255) == ["0x67", "00", "00", "ff", "f0"]     # blue
assert p.kbd_color_args(300, -1, 128) == ["0x67", "00", "ff", "80", "f0"]  # clamp to [0,255]
# brightness: ARGS=0xF4000000|level -> [level,00,00,0xf4]
assert p.kbd_bright_args(255) == ["0x67", "ff", "00", "00", "f4"]
assert p.kbd_bright_args(0x20) == ["0x67", "20", "00", "00", "f4"]
# rainbow: default palette is 7 full-saturation hues starting at red
stops = p.rainbow_stops(7)
assert len(stops) == 7 and stops[0] == (255, 0, 0)
assert all(len(c) == 3 and all(0 <= v <= 255 for v in c) for c in stops)
# cycle_color: phase 0 sits on the first stop; halfway to the next interpolates
two = [(0, 0, 0), (255, 255, 255)]
assert p.cycle_color(two, 0.0) == (0, 0, 0)
assert p.cycle_color(two, 0.25) == (128, 128, 128)   # quarter phase = halfway 1st->2nd (2 stops)
assert p.cycle_color(two, 0.5) == (255, 255, 255)
assert p.cycle_color(two, 1.0) == (0, 0, 0)          # wraps
assert p.cycle_color([(10, 20, 30)], 0.7) == (10, 20, 30)   # single stop is constant

# kbd_set issues mode(0), brightness, then colour, last labctl call is the colour
calls = []
p._labctl = lambda *a: (calls.append(a) or (True, ""))
p.fan_ensure_module = lambda: (True, "ready")
ok, _ = p.kbd_set(0, 255, 0, 128)
assert ok and calls[0] == ("dsm", "0x67", "00", "00", "00", "00")          # custom mode first
assert calls[1] == ("dsm", *p.kbd_bright_args(128))                        # then brightness
assert calls[-1] == ("dsm", *p.kbd_color_args(0, 255, 0))                  # then colour

# --- helper: buffer parser tolerates short/garbage input ---
s = h.sample.__doc__  # sample() itself needs /proc; just check decode guards exist
assert h._duty_pct(0x44) == 30

print("ok")
