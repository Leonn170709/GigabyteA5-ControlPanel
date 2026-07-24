#!/usr/bin/env python3
"""Bounded /dev/mem accessor for the A5 EC shared-memory window ONLY.

The firmware's RAM OperationRegion is (SystemMemory, 0xFE500100, 0x400). We
refuse any offset outside [0, 0x400) so this can never poke arbitrary memory.
Usage: ecmem.py read <hexoff> <len> | ecmem.py write <hexoff> <hexbyte>
"""
import mmap
import os
import sys

BASE = 0xFE500100
SIZE = 0x400
PAGE = 0x1000
MAP_BASE = BASE & ~(PAGE - 1)          # page-align down
INNER = BASE - MAP_BASE                # offset of BASE inside the mapped page(s)


def _check(off, length):
    if off < 0 or length < 1 or off + length > SIZE:
        sys.exit(f"offset 0x{off:x}+{length} outside EC window [0,0x{SIZE:x})")


def main():
    if len(sys.argv) < 3:
        sys.exit("usage: read <off> <len> | write <off> <byte>")
    cmd = sys.argv[1]
    off = int(sys.argv[2], 0)   # base 0: accepts 0x290 (hex) or 656 (decimal)
    fd = os.open("/dev/mem", os.O_RDWR | os.O_SYNC)
    span = ((INNER + SIZE + PAGE - 1) // PAGE) * PAGE
    m = mmap.mmap(fd, span, mmap.MAP_SHARED,
                  mmap.PROT_READ | mmap.PROT_WRITE, offset=MAP_BASE)
    try:
        if cmd == "read":
            length = int(sys.argv[3])
            _check(off, length)
            data = m[INNER + off: INNER + off + length]
            print(" ".join(f"{b:02x}" for b in data))
        elif cmd == "write":
            vals = [int(a, 16) & 0xFF for a in sys.argv[3:]]  # one or more consecutive bytes
            _check(off, len(vals))
            for i, val in enumerate(vals):
                m[INNER + off + i] = val
            print(f"wrote {len(vals)} byte(s) at +0x{off:x}: " +
                  " ".join(f"{v:02x}" for v in vals))
        else:
            sys.exit(f"unknown cmd {cmd}")
    finally:
        m.close()
        os.close(fd)


if __name__ == "__main__":
    main()
