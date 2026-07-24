#!/usr/bin/env bash
# Installs the A5 control panel: deps, privileged helper, polkit policy, launcher.
set -euo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"

# The fan/RGB _DSM opcodes were decoded from *this model's* DSDT. On other firmware the
# same opcodes may mean something else, so refuse by default. --force to override.
MODEL="$(cat /sys/class/dmi/id/product_name 2>/dev/null)"
if [ "$MODEL" != "A5 K1" ] && [ "${1:-}" != "--force" ]; then
    echo "!! This machine reports '$MODEL', not a GIGABYTE 'A5 K1'."
    echo "   Verify your own DSDT first (see probe.sh + README), then re-run with --force."
    exit 1
fi

echo ">> Installing dependencies (pacman + AUR)..."
sudo pacman -S --needed --noconfirm python-pyqt6 lm_sensors dkms
# Install AUR/DKMS deps only if missing. `--needed` isn't enough for VCS (-git) packages:
# their pkgver comes from `git describe`, so an already-built newer build looks like a
# "downgrade" vs the AUR's stale .SRCINFO and paru prompts. Skip anything already present.
for pkg in ryzenadj acpi_call-dkms ryzen_smu-dkms-git; do
    pacman -Qq "$pkg" &>/dev/null && { echo "   $pkg already installed, skipping"; continue; }
    paru -S --needed "$pkg"
done
# ryzen_smu gives ryzenadj real SMU access (else it no-ops via /dev/mem)
# autoload ryzen_smu on boot, else TDP control silently no-ops after a reboot
echo "ryzen_smu" | sudo tee /etc/modules-load.d/ryzen_smu.conf >/dev/null

echo ">> Registering kernel module with DKMS (rebuilds on kernel updates; does NOT auto-load)..."
if command -v dkms >/dev/null; then
    sudo rm -rf /usr/src/a5ctl-0.1
    sudo cp -r "$DIR/kmod" /usr/src/a5ctl-0.1
    sudo dkms add    -m a5ctl -v 0.1 2>/dev/null || true
    sudo dkms install -m a5ctl -v 0.1 || echo "   (dkms build failed, load manually with: sudo $DIR/labctl modload)"
else
    echo "   dkms not installed; skipping. Module loads manually via: sudo $DIR/labctl modload"
fi

echo ">> Installing NOPASSWD sudo rule for labctl (so the panel never prompts for a password)..."
# The panel drives all privileged actions (keyboard RGB, fans, battery, TDP, live monitor)
# through this one root harness, no pkexec/polkit. Validate before installing so a bad rule
# can never lock sudo. ponytail: single narrow rule, one binary, root-owned + immutable path.
RULE="$(id -un) ALL=(root) NOPASSWD: $DIR/labctl"
TMP="$(mktemp)"; printf '%s\n' "$RULE" > "$TMP"
if sudo visudo -cf "$TMP" >/dev/null; then
    sudo install -m 440 -o root -g root "$TMP" /etc/sudoers.d/labctl
    echo "   installed /etc/sudoers.d/labctl"
else
    echo "   !! sudoers rule failed validation, NOT installed; the panel would prompt for a password"
fi
rm -f "$TMP"

echo ">> Installing launcher + desktop shortcut..."
# .desktop needs absolute paths, so bake in wherever this checkout actually lives
DESKTOP="$(mktemp)"; sed "s|@DIR@|$DIR|g" "$DIR/laptop-control.desktop" > "$DESKTOP"
install -Dm644 "$DESKTOP" "$HOME/.local/share/applications/laptop-control.desktop"
install -Dm755 "$DESKTOP" "$HOME/Desktop/laptop-control.desktop"
rm -f "$DESKTOP"
# KDE: mark the desktop copy trusted so it launches without the "untrusted" prompt
gio set "$HOME/Desktop/laptop-control.desktop" metadata::trusted true 2>/dev/null || true
update-desktop-database "$HOME/.local/share/applications" 2>/dev/null || true

echo ">> Done. Find 'A5 Control' in the launcher (pin it to the taskbar), or on the Desktop."
