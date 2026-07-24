#!/usr/bin/env bash
# READ-ONLY test of the fan WMI method. Calls only the *read* method-ids
# (0x0C fan telemetry, 0x03 temp). Writes NOTHING to the fan/EC.
set -uo pipefail
[ "$(id -u)" -eq 0 ] || { echo "run: sudo bash $0"; exit 1; }
RUSER="${SUDO_USER:-root}"; RHOME="$(getent passwd "$RUSER" | cut -d: -f6)"
OUT="$RHOME/laptop-control/probe-out"; mkdir -p "$OUT"
trap 'chown -R "$RUSER" "$OUT" 2>/dev/null' EXIT

modprobe acpi_call 2>/dev/null || { echo "acpi_call missing. Install: paru -S acpi_call-dkms"; exit 2; }
CALL=/proc/acpi/call
call(){ printf '%s' "$1" > "$CALL"; tr -d '\0' < "$CALL"; echo; }

{
  echo "# fan-idle";   call '\_SB.WMI.WMBB 0x0 0xC b0x00'
  echo "# temp-idle";  call '\_SB.WMI.WMBB 0x0 0x3 b0x00'
} | tee "$OUT/wmi-idle.txt"

echo ">> loading all cores 35s (fans ramp slowly)..."
for i in $(seq "$(nproc)"); do timeout 38 sh -c 'while :; do :; done' & done
sleep 32
{
  echo "# fan-load";   call '\_SB.WMI.WMBB 0x0 0xC b0x00'
  echo "# temp-load";  call '\_SB.WMI.WMBB 0x0 0x3 b0x00'
} | tee "$OUT/wmi-load.txt"
wait 2>/dev/null

echo ">> saved wmi-idle.txt / wmi-load.txt in $OUT, nothing was written to the fans."
