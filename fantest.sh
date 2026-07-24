#!/usr/bin/env bash
# SAFE fan-write validation: forces both fans to 100% for 6s (can only cool),
# confirms they respond, then ALWAYS restores your exact stock curve (even on
# error/Ctrl-C via the trap). Run: sudo bash fantest.sh
set -uo pipefail
EC="$(dirname "$0")/ecmem.py"
CALL=/proc/acpi/call
modprobe acpi_call 2>/dev/null
wmb(){ printf '%s' "\\_SB.WMI.WMBB 0x0 $1 b0x00" > "$CALL"; tr -d '\0' < "$CALL"; echo; }
w(){ python3 "$EC" write "$1" "$2" >/dev/null; }

# stock duty bytes (read earlier): fan1@0x290=33 8c d1 ff  fan2@0x2a6=51 aa d1 ff
restore(){
  local i=0; for v in 0x33 0x8c 0xd1 0xff; do w $((0x290+i)) $v; i=$((i+1)); done
  i=0; for v in 0x51 0xaa 0xd1 0xff; do w $((0x2a6+i)) $v; i=$((i+1)); done
  echo ">> stock curve restored:"; python3 "$EC" read 0x290 4; python3 "$EC" read 0x2a6 4
}
trap restore EXIT

echo "=== baseline fan buffer (duty = byte[9]) ==="; wmb 0xC
echo ">> forcing both fans to 100% ..."
for o in 0x290 0x291 0x292 0x293 0x2a6 0x2a7 0x2a8 0x2a9; do w $o 0xff; done
sleep 6
echo "=== under forced-max (byte[9] should be higher, RPM up) ==="; wmb 0xC
# trap restores stock on exit
