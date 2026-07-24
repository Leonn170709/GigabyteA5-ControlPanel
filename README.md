# Gigabyte A5 Control Panel

Fan, keyboard-RGB, power and thermal control for the **GIGABYTE A5 K1** on Linux (KDE).

The Windows *Gigabyte Control Center* drives fans and keyboard RGB through undocumented
firmware methods, which is why neither works on Linux out of the box. This project decodes
those methods from the laptop's own ACPI/DSDT and puts them behind a Qt panel, so the
hardware is fully controllable without Windows.

| Tab | What it does |
|-----|--------------|
| **Performance** | Quiet / Balanced / Performance presets and a TDP slider via `ryzenadj`. Lowering TDP is the safe way to cut heat and fan noise. Asks for confirmation above the stock 45 W; hard-clamped to 15-54 W. |
| **Monitoring** | Live CPU temperature plus real fan duty % and RPM read straight from the firmware. GPU temperature from `sensors`. |
| **Fans** | Per-fan manual duty, presets, and a software CPU-temperature curve (`_DSM 0x68`). One click back to firmware auto (`0x69 0x0f`), plus a 90 °C watchdog that forces auto by itself. |
| **RGB** | Keyboard colour, brightness, and a software rainbow cycle over an editable palette (`\_SB.DCHU _DSM`). Single-zone, which is all the firmware exposes. |

The panel closes to a system-tray icon. Quitting it reverts the fans to firmware auto;
closing to tray leaves your settings running.

---

## Before you start

- **This is for a GIGABYTE A5 K1.** Check with:
  ```bash
  cat /sys/class/dmi/id/product_name      # must print: A5 K1
  ```
  The firmware opcodes were decoded from this exact model. On other firmware the same
  opcode can mean something completely different, so `install.sh` refuses to run elsewhere.
  See [Other models](#other-models) if you want to port it.
- **Arch-based distro.** The installer uses `pacman` and an AUR helper (`paru` or `yay`).
  Nothing else is Arch-specific; on other distros install the dependencies by hand and the
  rest works unchanged.
- **Kernel headers for your running kernel** must be installed, otherwise the kernel module
  cannot be built:
  ```bash
  pacman -Qq | grep -- '-headers'         # linux-headers, linux-cachyos-headers, ...
  ```

## Install

```bash
git clone https://github.com/Leonn170709/GigabyteA5-ControlPanel.git
cd GigabyteA5-ControlPanel
bash install.sh
```

The installer runs from wherever you cloned it, so keep the folder (don't clone to `/tmp`).
It will:

1. Install `python-pyqt6`, `lm_sensors`, `dkms` from the repos, and `ryzenadj`,
   `acpi_call-dkms`, `ryzen_smu-dkms-git` from the AUR.
2. Set `ryzen_smu` to load at boot, so `ryzenadj` gets real SMU access instead of silently
   doing nothing.
3. Register the `a5ctl` kernel module with DKMS, so it is **rebuilt automatically on every
   kernel update**.
4. Install one validated NOPASSWD sudo rule for `labctl`, so the panel never asks for a
   password (see [Security](#security) for exactly what that allows).
5. Add an "A5 Control" launcher entry and a shortcut on your desktop.

Then launch **A5 Control** from your app launcher (right-click → *Pin to Task Manager* if you
want it in the taskbar) or from the desktop shortcut. That's it.

The `a5ctl` module is deliberately **not** loaded at boot, so fan control is never engaged
unattended. You don't have to load it by hand either: the panel loads it on demand the
first time you use manual fans, RGB or the battery limit. To load it yourself anyway:

```bash
sudo ./labctl modload      # or: echo a5ctl | sudo tee /etc/modules-load.d/a5ctl.conf
```

### Verify it works

```bash
sudo ./labctl mon        # {"cpu": 68, "duty": 39, "rpm1": 1801, "rpm2": 1797}
python test_panel.py     # prints: ok
```

If `labctl mon` prints live RPM values, the firmware path is working.

## Troubleshooting

**`Invalid module format` after a kernel update, panel says "Kernel module not available"**

DKMS rebuilds the module for the new kernel, but a stale build cannot be loaded. Reload it:

```bash
sudo ./labctl modload
dkms status                 # a5ctl/0.1, <your kernel>: installed
```

If DKMS has no build for your running kernel, install that kernel's headers and rebuild:

```bash
sudo dkms install -m a5ctl -v 0.1
```

**Fans / RGB do nothing, `/sys/kernel/a5ctl/` missing** — the module isn't loaded.
`sudo ./labctl modload`, then `sudo ./labctl dmesg` to see why if it failed.

**TDP slider has no effect** — `ryzenadj` needs `ryzen_smu`, otherwise it falls back to
`/dev/mem` and silently no-ops. Check with `lsmod | grep ryzen_smu`.

**Panel asks for a password** — the sudoers rule is tied to the absolute path of `labctl`.
If you moved or renamed the folder after installing, re-run `bash install.sh`.

**Fans stuck at a manual duty after a crash** — reset them from a terminal:

```bash
sudo ./labctl dsm 0x69 0x0f
```

## Security

Every privileged action goes through one root harness, `labctl`, which exposes a fixed set
of subcommands and nothing else. The GUI itself never runs as root.

```
panel.py  (Qt6, unprivileged)
   |
   +-- sudo labctl <subcommand>
         +-- /sys/kernel/a5ctl/   kmod/a5ctl.c -> \_SB.DCHU _DSM   (fans + keyboard RGB)
         +-- acpi_call            \_SB.WMI.WMBB                    (fan telemetry)
         +-- ryzenadj             CPU power limits
```

- Writes go through the firmware's **own mutex-protected handlers**, never raw EC register
  pokes, so a bad call cannot corrupt the embedded controller.
- `ecmem.py` (a decoding aid) is hard-clamped to the shared-memory window the firmware
  itself declares, `SystemMemory 0xFE500100 + 0x400`. It cannot touch anything outside it.
- `install.sh` validates the sudoers rule with `visudo -c` **before** installing it, so a
  malformed rule can never lock you out of sudo.
- Fan writes carry a temperature watchdog and always have a one-call revert to firmware auto.
- `python test_panel.py` covers the TDP clamp, the temperature parser, the curve
  interpolation, and that the helper rejects out-of-range input.

The rule is required: without it the GUI has no tty for sudo to prompt on, so every
privileged action just fails. Read `labctl` first, it is 30 lines, and decide whether you
are comfortable with that. Removing the rule (`sudo rm /etc/sudoers.d/labctl`) leaves the
panel with only what it can read unprivileged: CPU and GPU temperature from `sensors`, no
fan telemetry, no fan/RGB/TDP control.

## Uninstall

```bash
bash uninstall.sh
```

Reverts the fans to firmware auto, unloads and deregisters the kernel module, and removes
the sudoers rule, the autoload entries and both shortcuts. Dependencies are left installed;
the script prints the command to remove them too.

## Other models

The opcodes were read out of this laptop's DSDT, so `install.sh` refuses any machine that
doesn't report `A5 K1`. To investigate your own:

```bash
sudo bash probe.sh          # dumps your ACPI tables and decompiles the DSDT into probe-out/
```

Look for `DCHU` and `WMBB` in `probe-out/dsdt.dsl` and compare them against
[NOTES.md](NOTES.md), which documents the ABI opcode by opcode. Only then override with
`bash install.sh --force`.

If it works on your model, a PR adding it to the DMI check is welcome. Don't commit your
`probe-out/` though: an ACPI dump includes the OEM Windows license table.

## Files

| | |
|---|---|
| `panel.py` | the whole GUI |
| `labctl` | root harness, the only privileged entry point |
| `laptop-control-helper` | reads one JSON fan/CPU sample via `acpi_call` |
| `kmod/a5ctl.c` | 143-line kernel module exposing `\_SB.DCHU _DSM` at `/sys/kernel/a5ctl/` |
| `ecmem.py` | clamped EC shared-memory reader/writer (decoding aid) |
| `probe.sh`, `wmi-test.sh`, `fantest.sh` | the reverse-engineering scratch tools |
| `NOTES.md` | decoded firmware ABI and development log |

## License

GPL-2.0. The kernel module makes this a derivative work of the Linux kernel.

**No warranty.** This drives undocumented firmware methods on your own hardware. It is
built to be conservative (firmware handlers only, temperature watchdog, one-click revert),
but you run it at your own risk.
