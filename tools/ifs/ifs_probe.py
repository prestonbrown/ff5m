#!/usr/bin/env python3
"""Probe the AD5X IFS board directly. Read-only opcodes only.

Runs ON THE PRINTER under Forge-X, where the stock UI is stopped and /dev/ttyS4
is free (`uart4` reads tx:0 rx:0). Every opcode here is a query; the four whose
effect is undocumented (F12/F20/F30/F43) and every actuator are refused by the
allowlist below, so this cannot move filament.

    scp tools/ifs/ifs_probe.py root@<printer>:/tmp/
    ssh root@<printer>
    LD_LIBRARY_PATH=/usr/prog/libffi-3.4.4/lib:/usr/prog/openssl-1.0.2d/lib:\
/usr/prog/Python-3.8.2/lib /usr/prog/Python-3.8.2/bin/python3 /tmp/ifs_probe.py

pyserial 3.4 ships in that interpreter. Without LD_LIBRARY_PATH it dies on
libpython3.8.so.1.0; lift the value from the running klippy's
/proc/<pid>/environ if these paths ever change.

What it answers, in order: does the board reply at all, does it care about the
trailing space or the 0xFF commit byte (on firmware 3.0.6 it does not), what
every read-only opcode returns, whether F19 really works, and whether F21 really
sends three lines. An F13 snapshot brackets the sweep so an unexpected state
change is caught.

Results from the first run are recorded in docs/AD5X_IFS_PROTOCOL.md. Re-run it
against any other board or firmware revision - the table in that document is one
board at 3.0.6 and F19 exists precisely because that is not guaranteed.
"""
import sys, time, re
import serial

PORT, BAUD = "/dev/ttyS4", 115200

# Queries only. F12/F20/F30/F43 acknowledge but their effect is undocumented,
# and every actuator (F10 F11 F15 F18 F23 F24 F39 F112) is deliberately absent.
QUERIES = ["F13", "F14", "F19", "F21", "F22", "F40", "F41", "F42",
           "F44", "F45",
           "F50", "F51", "F52", "F53", "F54",
           "F60", "F61", "F62", "F63", "F64"]
FORBIDDEN = {"F10", "F11", "F12", "F15", "F18", "F20", "F23", "F24",
             "F30", "F39", "F43", "F112"}

QUIET_GAP = 0.35     # a reply is over when the board is silent this long
OVERALL_CAP = 3.0


def read_reply(ser):
    """Read until the board goes quiet. There is no terminator to wait for."""
    buf, last, start = bytearray(), None, time.time()
    while time.time() - start < OVERALL_CAP:
        chunk = ser.read(64)
        if chunk:
            buf += chunk
            last = time.time()
        elif last is not None and time.time() - last >= QUIET_GAP:
            break
        elif last is None and time.time() - start > 1.0:
            break
    return bytes(buf)


def send(ser, cmd, trailing_space=True, commit=False, settle=0.2):
    op = cmd.split()[0]
    if op in FORBIDDEN:
        raise SystemExit("REFUSED: %s is not a query" % op)
    ser.reset_input_buffer()
    wire = (cmd + " \r\n") if trailing_space else (cmd + "\r\n")
    ser.write(wire.encode())
    if commit:
        time.sleep(settle)
        ser.write(b"\xff")
    t0 = time.time()
    reply = read_reply(ser)
    return wire, reply, time.time() - t0


def f13_fields(text):
    return {k: int(v) for k, v in
            re.findall(r"(FFS_state|silk_state|chan|ffs_channels_insert|"
                       r"stall_state):\s*(\d+)", text)}


def main():
    ser = serial.Serial(PORT, BAUD, timeout=0.1)
    try:
        print("=" * 62)
        print("STEP 1  basic comms, stock framing (trailing space, NO 0xFF)")
        wire, reply, dt = send(ser, "F13")
        print("  sent  %r" % wire)
        print("  got   %r" % reply)
        print("  %.0f ms, %d bytes" % (dt * 1000, len(reply)))
        if not reply:
            raise SystemExit("no reply - aborting before anything else")
        before = f13_fields(reply.decode("ascii", "replace"))
        print("  state %s" % before)

        print("\n" + "=" * 62)
        print("STEP 2  framing A/B - does the board care?")
        for label, kw in [
            ("stock  (space, no commit)", dict(trailing_space=True,  commit=False)),
            ("zmod   (no space, commit)", dict(trailing_space=False, commit=True)),
            ("neither(no space, no cmt)", dict(trailing_space=False, commit=False)),
            ("both   (space + commit)  ", dict(trailing_space=True,  commit=True)),
        ]:
            _, reply, dt = send(ser, "F13", **kw)
            ok = b"F13 ok" in reply
            print("  %s -> %s  %d bytes  %.0f ms"
                  % (label, "REPLY" if ok else "SILENT", len(reply), dt * 1000))
            time.sleep(0.3)

        print("\n" + "=" * 62)
        print("STEP 3  every read-only opcode (%d)" % len(QUERIES))
        results = {}
        for op in QUERIES:
            _, reply, dt = send(ser, op)
            text = reply.decode("ascii", "replace")
            results[op] = text
            shown = text.replace("\r", "\\r").replace("\n", "\\n").strip()
            print("  %-5s %4d B %5.0f ms  %s"
                  % (op, len(reply), dt * 1000,
                     shown[:96] if shown else "<SILENT>"))
            time.sleep(0.25)

        print("\n" + "=" * 62)
        print("STEP 4  the two questions the firmware could not answer")
        f19 = results.get("F19", "")
        print("  F19 answered      : %s" % ("YES" if f19.strip() else "NO"))
        if f19.strip():
            print("    -> %r" % f19.strip())
        f21 = results.get("F21", "")
        print("  F21 line count    : %d  (firmware string implies 3)"
              % len([l for l in f21.replace("\r\n", "\n").split("\n") if l.strip()]))
        print("    -> %r" % f21.strip())

        print("\n" + "=" * 62)
        print("STEP 5  safety - did anything move?")
        _, reply, _ = send(ser, "F13")
        after = f13_fields(reply.decode("ascii", "replace"))
        print("  before %s" % before)
        print("  after  %s" % after)
        for k in ("silk_state", "ffs_channels_insert"):
            if before.get(k) != after.get(k):
                print("  !! %s CHANGED - investigate before going further" % k)
        if before.get("silk_state") == after.get("silk_state"):
            print("  silk_state unchanged - no filament moved")
    finally:
        ser.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
