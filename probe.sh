#!/usr/bin/env bash
# READ-ONLY probe for the GIGABYTE A5 K1. Writes nothing to hardware.
# Dumps the ACPI WMI methods (fan/RGB control ABI) and empirically maps the
# EC fan/temp registers by diffing an idle vs. under-load snapshot.
# Run:  sudo bash probe.sh
set -uo pipefail
[ "$(id -u)" -eq 0 ] || { echo "run with sudo: sudo bash $0"; exit 1; }
# resolve the REAL invoking user's home (sudo sets HOME=/root otherwise)
RUSER="${SUDO_USER:-root}"
RHOME="$(getent passwd "$RUSER" | cut -d: -f6)"
OUT="$(cd "$(dirname "$0")" && pwd)/probe-out"   # next to the script, gitignored
mkdir -p "$OUT"
trap 'chown -R "$RUSER" "$OUT" 2>/dev/null' EXIT

echo "[1/4] Decompiling ACPI/DSDT to read the WMI control methods..."
if ! command -v iasl >/dev/null || ! command -v acpidump >/dev/null; then
  echo "  need 'acpica', install with: pacman -S acpica   then re-run"
else
  ( cd "$OUT" && acpidump -b >/dev/null 2>&1 && iasl -d dsdt.dat >/dev/null 2>&1 )
  if [ -f "$OUT/dsdt.dsl" ]; then
    # pull the _WDG map + the WMI method bodies that drive fans/leds/charge
    awk '/_WDG/{f=1} f&&/}/{print;if(--n<=0)f=0;next} f{print}' "$OUT/dsdt.dsl" > "$OUT/wdg.txt" 2>/dev/null
    grep -nE "Method \(WM(AA|BB|BA|BC|BD)" "$OUT/dsdt.dsl" > "$OUT/wmi-methods.txt"
    csplit -sz -f "$OUT/wm_" -b '%02d.asl' "$OUT/dsdt.dsl" '/Method (WM/' '{*}' 2>/dev/null || true
    echo "  -> $OUT/dsdt.dsl  (+ wmi-methods.txt, wm_*.asl slices)"
  fi
fi

echo "[2/4] Loading ec_sys (read-only) and snapshotting EC at idle..."
modprobe ec_sys 2>/dev/null
EC=/sys/kernel/debug/ec/ec0/io
if [ -r "$EC" ]; then
  xxd "$EC" > "$OUT/ec-idle.txt"
  echo "  idle snapshot saved."
else
  echo "  EC io not available (ec_sys missing?). Skipping EC map."
fi

echo "[3/4] Loading all CPU cores for 20s while snapshotting EC..."
NP=$(nproc)
pids=()
for i in $(seq 1 "$NP"); do timeout 22 sh -c 'while :; do :; done' & pids+=($!); done
sleep 12   # let fans spin up
[ -r "$EC" ] && xxd "$EC" > "$OUT/ec-load.txt"
sensors > "$OUT/sensors-load.txt" 2>/dev/null
sleep 8
for p in "${pids[@]}"; do kill "$p" 2>/dev/null; done
wait 2>/dev/null

echo "[4/4] Diffing idle vs load to find fan/temp/duty registers..."
if [ -f "$OUT/ec-idle.txt" ] && [ -f "$OUT/ec-load.txt" ]; then
  diff "$OUT/ec-idle.txt" "$OUT/ec-load.txt" > "$OUT/ec-diff.txt"
  echo "  EC lines that changed under load (< idle / > load):"
  cat "$OUT/ec-diff.txt"
fi

echo
echo "DONE. Everything is in: $OUT"
echo "Nothing was written to hardware. Tell Claude it's done and it'll read these files."
