> Development log and decoded firmware ABI for [A5 Control](README.md). Kept verbatim, newest entry first, so the reverse-engineering steps are reproducible.

# GOAL, Full hardware control panel for GIGABYTE A5 K1 (Linux/KDE)

Own every hardware knob the laptop exposes in one sleek KDE app: fans (read + set + curve),
keyboard RGB, power/TDP, battery, monitoring. All control goes through the firmware `_DSM`
(mutex-protected) via a small kernel module, never raw EC pokes. Fan writes carry a temp watchdog.

## STATUS (2026-07-21d), Fans tab redesign + scrollable tabs + no em dashes
- **Fans tab redesigned:** split the one crammed card into three, Live telemetry (Duty/Speed stat
  tiles), Manual control (prominent toggle + presets + per-fan sliders + note), Auto curve (checkbox +
  graph). Much cleaner.
- **Every tab is now wrapped in a QScrollArea** (`_scroll`), so tall content (esp. Fans with the curve
  graph) scrolls instead of overlapping in a short window. Styled thin scrollbars. Default size 600x740.
- **Removed all em dashes** project-wide (spaced ones to commas, others to hyphens); 0 remain in any
  .py/.sh/.md/.desktop.
- Verified: test_panel.py green; offscreen renders of all three tabs look clean; fans reverted to auto.

## STATUS (2026-07-21c), Full visual rework + rainbow effect + single-instance
- **Visual redesign:** new dark theme (segmented pill tab bar, bordered cards, modern sliders/menus,
  refined header stat tiles). All in the `STYLE` sheet + object names; tab logic unchanged.
- **Rainbow / dynamic RGB:** RGB tab now has a Static | Rainbow segmented switch. Rainbow = software
  cycle through an **editable palette** (default = 7 rainbow hues; click a chip to edit, right-click to
  remove, ＋Add, Reset). Engine: `_rainbow_timer` @200ms → `kbd_color_only` (ONE _DSM 0x67 colour write
  per frame; mode+brightness set once up front). Speed slider = seconds/loop. `cycle_color()` +
  `rainbow_stops()` are pure + unit-tested. **Camera-verified** cycling on hardware (blue→amber…).
  ponytail ceiling: 5 fps steady _DSM, smooth + gentle on ACPI; keeps running while minimized to tray.
- **Single-instance guard (fixes duplicate taskbar icon):** `QLocalServer`/`QLocalSocket` named
  `a5control.singleton`. A 2nd launch pings the running one to `_show_normal()` and exits instead of
  starting a 2nd process/icon. `.desktop` gained `StartupWMClass=laptop-control` + `SingleMainWindow`;
  `app.setDesktopFileName("laptop-control")` aligns the Wayland app_id for taskbar grouping.
- Verified: test_panel.py green; offscreen app-logic smoke (all tabs, rainbow lifecycle, palette edit,
  tray, close-to-tray, second-launch-raise) clean. NB: offscreen `QWidget.grab()` segfaults on the
  rainbow layout, a Qt offscreen-render quirk, NOT an app bug (real compositor unaffected).

## STATUS (2026-07-21b), Panel UX pass: no-password, tray, polished RGB
- **No more password prompt.** Dropped `pkexec`/polkit entirely. TDP now goes through
  `sudo labctl ryzenadj …` and the live fan/CPU monitor polls `sudo labctl mon` (new subcommand →
  helper `read`) every 1s, all under the existing **NOPASSWD** labctl rule. `install.sh` now installs
  that sudoers rule (validated) and no longer installs the (now-dead) polkit policy/`/usr/local` helper.
- **Closes to a system-tray icon** (`QSystemTrayIcon`): X/close hides to tray (fans NOT reverted then -
  only on real Quit); left-click tray = open; right-click = quick menu (keyboard colour presets + Off,
  fan presets + Auto, Quit). `setQuitOnLastWindowClosed(False)`.
- **RGB tab polished:** big live-preview swatch (opens native `QColorDialog`), 9 preset colour chips,
  brightness slider **debounced 160ms** (dragging no longer machine-guns `_DSM`) with a live %.
- All self-checked (test_panel.py) + offscreen-rendered to confirm the look. See panel.py.

## STATUS (2026-07-21), KEYBOARD RGB DONE ✅ (camera-verified end-to-end)
- **The keyboard is single-zone RGB**, controlled entirely through the firmware's own mutex-protected
  `_DSM func 0x67` (Clevo SET_KB_LED, handled by ZEVT), NOT the EC shadow (writing 0x280 held on
  read-back but never drove the LEDs; the EC ignores the shadow for keyboard). ARGS = 4-byte LE buffer,
  top byte selects op:
  - `0x00000000` = custom/static mode
  - `0xF0000000 | (B<<16)|(R<<8)|G` = set colour (Clevo channel packing; zone0 = whole keyboard).
    LE bytes → `dsm 0x67 <G> <R> <B> f0`. Zones 1/2 (`0xF1/0xF2`) exist in the ABI but do nothing here.
  - `0xF4000000 | level` = brightness (level 0-255). LE → `dsm 0x67 <level> 00 00 f4`.
  Decoded from DSDT ZEVT case 0x67 (Local7==0x0F) + tuxedo clevo_leds.h packing, then **verified with
  the user's USB camera** (/dev/video2): drove green, red, blue, purple + bright/dim on the real board.
- **panel.py:** RGB tab rewritten to single colour picker + 6 preset swatches + brightness slider + Off,
  backed by `kbd_color_args`/`kbd_bright_args`/`kbd_set` → `labctl dsm 0x67` (needs a5ctl module, which
  kbd_set auto-loads). Self-checked (test_panel.py: colour/brightness packing, kbd_set call order).
- Verify a change yourself: `ffmpeg -f v4l2 -i /dev/video2 -frames:v 1 -vf transpose=2,transpose=2 x.jpg`.

## STATUS (2026-07-20, resumed), rebooted clean, module reloaded + re-validated
- Post-reboot: `a5ctl.ko` loads clean against 7.1.3-2-cachyos. `dsm 0x0c` == `wmb 0xC` byte-for-byte.
  Fans in auto (fan1 period 0x04c5, fan2 0x04ec, duty 0x59), Tctl 60°C. Ready for next live test.
- DONE this session: decoded `0x68` = 4× PWM 0-255 duty bytes (see ABI below). One spaced ramp test,
  auto-reverted, no freeze, temps fine.
- NEXT: build Quiet/Balanced/Max presets in panel.py with min-duty floor + temp watchdog; OR keyboard
  RGB `0x04`. Both need one spaced live test, checkpoint with user first.

## STATUS (2026-07-20), paused after a desktop freeze
- Reverse-engineering: **done**. Kernel module: **built + validated**. Then KDE/kwin compositor
  **froze** during rapid live fan-mode experiments (machine stayed healthy: 59°C, fans auto, no
  kernel crash; likely ACPI-contention/compositor hang). Reverted fans to auto, unloaded module.
  User will reboot. **Module does NOT auto-load** (manual insmod only; not in modules-load.d/dkms),
  so boot is unaffected.
- **Lesson / pacing:** stop rapid-fire live ACPI/EC writes. Build the clean control first, then
  test sparingly with spacing + watchdog + user checkpoints. See [[a5-control-panel]].

## Verified firmware ABI (from this laptop's own DSDT, probe-out/dsdt.dsl)
Two providers:
- `\_SB.WMI.WMBB(0, id, buf)`, callable via acpi_call for READS:
  - `0x0C` fan telemetry (42-byte buf): `buf[2:4]`/`buf[4:6]` = fan1/fan2 tach **period** (BE,
    smaller=faster), `buf[9]` = duty (0x44=30%..0xE5=100%). `0x03` = CPU temp byte °C.
- `\_SB.DCHU._DSM(guid, 1, func, PACKAGE{buffer})`, needs Package arg → **kernel module only**
  (acpi_call can't send packages). guid = `93f224e4-fbdc-4bbf-add6-db71bdc0afad`. Functions:
  - `0x0C` DEVT: read fan telemetry (matches WMBB 0x0C) ✓ validated via module
  - `0x69` ZEVT: **restore fans to AUTO**, arg = fan bitmask (`0x0f` = all fans). ✓ CONFIRMED, this
    is the safe revert.
  - `0x68` ZEVT: **manual fan mode**. arg = 4 duty bytes `[fan1 fan2 fan3 fan4]`, each raw **PWM 0-255
    (255=100%)**. DECODED 2026-07-20: `dsm 0x68 E0 E0 E0 E0` → telemetry `buf[9]` echoed 0xE0 exactly,
    fan1 period 0x04bc→0x0213 (spun up hard), fan2 0x0000→0x021f (started), Tctl 54.6→53.4°C. Telemetry
    readback WAS reliable this test. `0x69 0f` reverted to auto cleanly. Duty scale is linear PWM
    (matches curve table: 0x33≈20%, 0xFF=100%). NOTE: low bytes are the risk (0x01 nearly stops a fan
   , the pre-freeze `[01 ff ff ff]` test); presets must keep a min-duty floor + temp watchdog.
  - `0x04` PEVT: **keyboard RGB** (3-zone). Not yet driven via module.
  - `0x0E` FEVT: fan curve write (Arg3 = plain buffer, not package).
- Keyboard RGB is 3-zone at EC MMIO `0xFE500100+0x280` (KLCR/KLCG/KLCB, KMCR.., KRCR..; KBLD
  brightness). **Direct MMIO write does NOT apply**, EC needs the PEVT commit (FCMD handshake).
- Fan curve table at MMIO `+0x28C`: 4 pts/fan (temp°C, duty 0-255). Stock fan1 = 40°C→20%,60→55%,
  80→82%,100→100%; fan2 = 45→32%,60→67%,80→82%,96→100%. Direct MMIO write does NOT activate
  (needs custom mode via _DSM). Restore bytes: fan1@0x290=`33 8c d1 ff`, fan2@0x2a6=`51 aa d1 ff`.
- Secure Boot OFF → unsigned modules load fine. Kernel is **clang-built** → build with `LLVM=1`.

## Assets in ~/laptop-control/
- `panel.py` (PyQt6 GUI, works headless), `laptop-control-helper` (pkexec monitor/set-tdp),
  `ecmem.py` (bounded /dev/mem to EC window), `labctl` (root harness, NOPASSWD-allowed),
  `kmod/a5ctl.c` + Makefile (module: generic `_DSM` caller via `/sys/kernel/a5ctl/call|last`),
  `probe-out/` (dsdt.dsl + EC dumps), install.sh, README.md, test_panel.py.
- Build module:  `make -C /lib/modules/$(uname -r)/build M=~/laptop-control/kmod LLVM=1 modules`
- Access harness (already permitted): `sudo ~/laptop-control/labctl {wmb|dsm|modload|modunload|memread|memwrite|dmesg|...}`
- Permission rule added: NOPASSWD sudoers: /etc/sudoers.d/labctl.

## Roadmap
- [x] RE fan + RGB ABI from DSDT; live telemetry; PyQt6 UI + launcher; kernel module + _DSM validated
- [x] Confirm fan AUTO restore (_DSM 0x69)
- [x] Decode `0x68` fan-speed encoding → 4× PWM 0-255 bytes, one per fan.
- [x] Manual fan control in panel.py Fans tab: toggle unlocks a duty slider (20-100%, floored) +
      Quiet/Balanced/Max preset buttons (35/60/100%). Applies via `dsm 0x68`; toggle-off/close/90°C
      watchdog → `dsm 0x69 0f` auto. Uses NOPASSWD `sudo labctl`. Slider writes are QProcess
      startDetached (non-blocking, a blocking sudo froze the UI mid-drag). Self-checked.
- [x] Per-fan control (Fan 1 / Fan 2 sliders → `0x68 f1 f2 f2 f2`) + **software** fan curve: 4 editable
      (temp,duty) points, interpolated in-app (`curve_duty`), driven off the live CPU-temp monitor
      stream, throttled to write only on change. Floor + 90°C watchdog + auto-revert all still apply.
      **Chose software curve over firmware FEVT `0x0E` on purpose:** FEVT writes an under-labeled
      3-fan + 3×3 SH/SL slope-matrix (30 bytes, temp-vs-duty/units not self-evident), a wrong blind
      write can under-cool. Software curve reuses the *proven* `0x68` primitive → safe by construction.
      Self-checked (curve_duty, fan_duty_args). Slider apply is **debounced valueChanged** (150ms) -
      `sliderReleased` missed groove-clicks/keyboard so "the number moved but the fan didn't"; fixed.
- [x] Curve **graph** is now the interactive editor: drag points, double-click to add, double-click a
      point to remove (min 2). Live green temp marker ("72° → 65%"). Spinboxes removed. `on_change`
      re-applies the curve. add/move/remove/clamp exercised headless.
- [x] **BUGFIX (was silently broken):** GUI fan writes did nothing since the per-fan refactor -
      `_apply_duties` called `labctl <fan_duty_args>` where fan_duty_args starts with "0x68", i.e.
      `sudo labctl 0x68 …` with the **"dsm" subcommand missing** → labctl usage-error no-op. Fixed to
      `labctl dsm 0x68 …`; regression-checked (asserts "dsm" is in the emitted command). Sliders also
      debounced. (Battery path was fine, it already passed "dsm".)
- [x] TDP: already works (ryzenadj + ryzen_smu, SMU access confirmed via `ryzenadj -i`). Added
      `/etc/modules-load.d/ryzen_smu.conf` autoload (in install.sh) so it survives reboot, + ppd sync
      (TDP presets nudge power-profiles-daemon: Quiet→power-saver, Balanced→balanced, Perf→performance).
- [x] Battery charge limit, via **`_DSM 0x76`** (subcmd 0x06: BTCP=stop%, BSCP=start%), read `0x77`.
      (GOAL's old "BDC0" was wrong, BDC0 = design capacity.) Verified live: set 80→readback
      `00 4b 50 00`, clear→`00 00 00 00`. Panel: 60/80/100% buttons in Performance tab. Left cleared.
- [x] Package module as DKMS (`kmod/dkms.conf` + install.sh `dkms add/install`, LLVM=1). Auto-LOAD
      still OFF (gated on the fan tab being proven live), DKMS only rebuilds on kernel updates.
- [x] Keyboard RGB, **DONE 2026-07-21 (camera-verified, see STATUS):** single-zone, via `_DSM 0x67`
      (colour `0xF0|B<<16|R<<8|G`, brightness `0xF4|level`, custom mode `0x0`). panel.py RGB tab drives
      colour + brightness. The earlier EC-shadow (0x280) write was a dead end, EC ignores it for the LEDs.
- [ ] (obsolete note) Keyboard RGB via `0x04`/PEVT, the DSDT PEVT Case(1) is only a
      *generic* EC-command passthrough (FCMD=ZE06, FDAT/FBUF/FBF1-3 = ZE01..ZE05). Zone regs
      KLCR/KLCG/KLCB (L), KMCx (M), KRCx (R) + KBLD are only ever *read* (EEVT), never written in AML -
      so the actual "set keyboard color" EC command number lives in the EC firmware / Windows app, NOT
      the DSDT. Decoding it needs a Windows GCC USB/WMI capture OR brute-forcing EC command bytes live
      (risky rapid EC pokes, the thing that froze the desktop). Deferred until a capture is available.
- [ ] Mouse RGB (Holtek 04d9:a09f), **BLOCKER:** mouse IS connected but OpenRGB does NOT detect it,
      so there's no known protocol. Needs hidraw HID-report RE (or a Windows capture), same class of
      problem as the keyboard. Deferred; won't ship blind HID pokes.
- [ ] Keyboard RGB, LAST, per user. (blocker above)

## RESUME AFTER REBOOT
1. Rebuild+load module: `make ... LLVM=1 modules` then `sudo ~/laptop-control/labctl modload`.
2. Sanity read: `sudo ~/laptop-control/labctl dsm 0x0c` (should match `wmb 0xC`).
3. Continue decoding `0x68` fan speed via ONE spaced audible test, then keyboard `0x04`.
4. Keep the temp watchdog + revert-to-`0x69` on every fan write. Checkpoint before live tests.

## Safety rules (non-negotiable)
- Control only via firmware `_DSM` (mutex), never raw EC command writes from the app.
- Every fan write: min-duty floor, temp watchdog (revert to `0x69` auto if Tctl high), auto-revert
  on exit/crash. Space out live ACPI experiments; do not rapid-fire them.
