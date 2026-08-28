#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Recover the AD5X IFS wire protocol from the board's own firmware.

The IFS board is an STM32 on /dev/ttyS4. Stock FlashForge ships its application
image as `/usr/prog/PROGRAM/control/ifs.hex`, and that image contains the printf
format string for every response the board can send. Those strings are ground
truth for the protocol - better than transcribing a driver, because they also
reveal opcodes no driver exposes.

Two traps, both of which cost time the first time round:

  * `strings(1)` does NOT find them. Use an explicit printable-run regex.
  * A "does it look like a word" filter drops short responses such as `F15 ok.`
    and `F18 ok`, which is how a first pass under-reported 20 opcodes instead of
    32. Filter as little as possible, then group.

Usage:
    python3 tools/ifs/extract_ifs_protocol.py <ifs.hex> [--bin OUT.bin]
"""

import argparse
import re
import sys


def load_ihex(path):
    """Intel HEX -> (base_address, flat bytes). Handles record types 00/01/02/04."""
    mem, base, lo, hi = {}, 0, None, 0
    with open(path, "r") as fh:
        for line in fh:
            line = line.strip()
            if not line.startswith(":"):
                continue
            raw = bytes.fromhex(line[1:])
            count, addr, rtype, data = raw[0], (raw[1] << 8) | raw[2], raw[3], raw[4:4 + raw[0]]
            if rtype == 0x00:
                start = base + addr
                for i, byte in enumerate(data):
                    mem[start + i] = byte
                lo = start if lo is None else min(lo, start)
                hi = max(hi, start + count)
            elif rtype == 0x04:
                base = ((data[0] << 8) | data[1]) << 16
            elif rtype == 0x02:
                base = ((data[0] << 8) | data[1]) << 4
            elif rtype == 0x01:
                break
    if lo is None:
        raise SystemExit("no data records in %s" % path)
    flat = bytearray(b"\xff" * (hi - lo))
    for address, value in mem.items():
        flat[address - lo] = value
    return lo, bytes(flat)


def responses(blob):
    """Every distinct `F<n> ok...` response string, in opcode order."""
    seen, found = set(), []
    for run in re.finditer(rb"[\x20-\x7e\r\n\t]{4,}", blob):
        text = run.group().decode("ascii", "replace")
        for hit in re.finditer(r"F\d{1,3} ok[^\x00]*", text):
            value = hit.group().strip()
            if value not in seen:
                seen.add(value)
                found.append(value)
    return sorted(found, key=lambda s: (int(re.match(r"F(\d+)", s).group(1)), s))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("hex")
    ap.add_argument("--bin", help="also write the flat binary here")
    args = ap.parse_args()

    base, blob = load_ihex(args.hex)
    if args.bin:
        with open(args.bin, "wb") as fh:
            fh.write(blob)

    sp = int.from_bytes(blob[0:4], "little")
    reset = int.from_bytes(blob[4:8], "little")
    print("image  : 0x%08X..0x%08X (%d bytes)" % (base, base + len(blob), len(blob)))
    print("vectors: SP=0x%08X reset=0x%08X%s"
          % (sp, reset, "  (Cortex-M: SP in SRAM, reset odd/Thumb)"
             if sp >> 24 == 0x20 and reset & 1 else "  (UNEXPECTED - not a Cortex-M image?)"))

    found = responses(blob)
    opcodes = sorted({re.match(r"F(\d+)", s).group(1) for s in found}, key=int)
    print("opcodes: %d -> %s" % (len(opcodes), " ".join("F" + o for o in opcodes)))
    print("strings: %d\n" % len(found))
    for value in found:
        print("  " + value.replace("\r", "\\r").replace("\n", "\\n"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
