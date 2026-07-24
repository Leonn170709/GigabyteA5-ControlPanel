#!/usr/bin/env bash
# Removes everything install.sh put on the system. Leaves this checkout alone.
set -uo pipefail

echo ">> Reverting fans to firmware auto (in case manual mode is active)..."
DIR="$(cd "$(dirname "$0")" && pwd)"
sudo "$DIR/labctl" dsm 0x69 0x0f >/dev/null 2>&1 || true

echo ">> Removing kernel module + DKMS registration..."
sudo rmmod a5ctl 2>/dev/null
sudo dkms remove -m a5ctl -v 0.1 --all 2>/dev/null
sudo rm -rf /usr/src/a5ctl-0.1

echo ">> Removing sudoers rule and autoloads..."
sudo rm -f /etc/sudoers.d/labctl /etc/modules-load.d/ryzen_smu.conf

echo ">> Removing launcher + desktop shortcut..."
rm -f "$HOME/.local/share/applications/laptop-control.desktop" \
      "$HOME/Desktop/laptop-control.desktop"
update-desktop-database "$HOME/.local/share/applications" 2>/dev/null || true

echo ">> Done. Deps (ryzenadj, acpi_call-dkms, ryzen_smu-dkms-git) left installed;"
echo "   remove with: paru -Rns ryzenadj acpi_call-dkms ryzen_smu-dkms-git"
