# A5 Control

Fan, keyboard-RGB, power and thermal control panel for the **GIGABYTE A5 K1** on Linux/KDE.

The Windows *Gigabyte Control Center* drives fans and keyboard RGB through undocumented
firmware methods. This decodes them from the laptop's own ACPI/DSDT and puts them behind a
Qt panel, so the hardware is controllable without Windows.

## Requirements

- A **GIGABYTE A5 K1** (`/sys/class/dmi/id/product_name` == `A5 K1`). See
  [Other models](#other-models) before trying anything else.
- Arch-based distro (uses `pacman` + `paru`). On other distros install the deps by hand;
  nothing else is Arch-specific.
- `dkms` plus kernel headers for your running kernel.

## Install

```bash
git clone <this repo> ~/a5-control
bash ~/a5-control/install.sh
```

Installs deps (`python-pyqt6`, `lm_sensors`, `dkms`; `ryzenadj`,
`acpi_call-dkms`, `ryzen_smu-dkms-git` via AUR), registers the `a5ctl` kernel module with
DKMS, adds one NOPASSWD sudo rule for `labctl`, and drops a launcher entry plus a Desktop
shortcut. It runs from wherever you cloned it, no fixed path.

Then load the module and start the panel:

```bash
sudo ~/a5-control/labctl modload     # module does NOT auto-load, see Safety
# launch "A5 Control" from the app launcher or the Desktop shortcut
```

`bash uninstall.sh` reverses all of it.

## What works

| Tab | Status |
|-----|--------|
| **Performance** | Quiet/Balanced/Performance presets + TDP slider via `ryzenadj`. Lowering TDP is the safe way to cut heat and fan noise. Confirms before exceeding stock 45 W; clamped 15-54 W. |
| **Monitoring** | Live CPU temp + fan duty % and RPM read from the firmware. GPU temp from `sensors`. |
| **Fans** | Manual duty per fan, presets, and a software CPU-temp curve, via `_DSM 0x68`. One-click revert to firmware auto (`0x69 0x0f`), plus a 90 °C watchdog that forces auto. |
| **RGB** | Keyboard colour, brightness, and a software rainbow cycle over an editable palette, via `_SB.DCHU _DSM`. Mouse (Holtek `04d9:a09f`) is a separate hidraw path, not done. |

## How it works

```
panel.py  (Qt6, unprivileged)
   |
   +-- sudo labctl ...          one NOPASSWD root harness, fixed subcommand set
         +-- /sys/kernel/a5ctl/ kmod/a5ctl.c -> \_SB.DCHU _DSM  (fans + keyboard RGB)
         +-- acpi_call          \_SB.WMI.WMBB                   (fan telemetry)
         +-- ryzenadj           CPU power limits (needs ryzen_smu for real SMU access)
```

Every write goes through the firmware's **own mutex-protected handlers**, not raw EC
register pokes, so a bad call cannot corrupt the EC. The decoded ABI, opcode by opcode, is
in [NOTES.md](NOTES.md).

## Safety

- The kernel module is **not** auto-loaded. DKMS rebuilds it on kernel updates but loading
  stays a deliberate `labctl modload`.
- Fan writes carry a temperature watchdog and always have a one-call revert to firmware
  auto. Quitting the panel reverts the fans; closing to tray does not.
- `ecmem.py` is hard-clamped to the EC shared-memory window the firmware itself declares
  (`SystemMemory, 0xFE500100, 0x400`); it cannot touch memory outside it.
- `install.sh` validates the sudoers rule with `visudo -c` before installing it, so a bad
  rule can never lock you out of sudo.
- `python test_panel.py` covers the TDP clamp, the temp parser, the curve interpolation,
  and that the helper rejects out-of-range input.

## Other models

The opcodes here were read out of *this* laptop's DSDT. On different firmware the same
opcode can mean something else entirely, so `install.sh` refuses to run on any machine that
doesn't report `A5 K1`. To port it:

```bash
sudo bash probe.sh          # dumps your ACPI tables + decompiles the DSDT to probe-out/
```

Look for `DCHU` and `WMBB` in `probe-out/dsdt.dsl` and compare against NOTES.md before
overriding with `install.sh --force`. Don't commit your `probe-out/` in a fork; the ACPI
dump includes an OEM Windows license table.

## Files

| | |
|---|---|
| `panel.py` | the whole GUI |
| `labctl` | root harness, the only privileged entry point |
| `laptop-control-helper` | reads one JSON fan/CPU sample via `acpi_call` |
| `kmod/a5ctl.c` | 143-line module exposing `\_SB.DCHU _DSM` at `/sys/kernel/a5ctl/` |
| `ecmem.py` | clamped EC shared-memory reader/writer (decoding aid) |
| `probe.sh`, `wmi-test.sh`, `fantest.sh` | the reverse-engineering scratch tools |
| `NOTES.md` | decoded firmware ABI + development log |

## License

GPL-2.0 (the kernel module makes this a derivative work of the kernel).
