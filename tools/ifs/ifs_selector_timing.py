#!/usr/bin/env python3
"""Time the AD5X IFS channel selector, to settle its step rate on hardware.

THIS TOOL MOVES THE SELECTOR. Its companion, ifs_probe.py, is read-only and
refuses every actuator; this one exists precisely to send F30, so it is a
separate file rather than a flag on that one. Run it with no filament loaded,
and pass --i-know-it-moves.

What it answers: how many steps per second the selector actually turns at.
docs/AD5X_IFS_PROTOCOL.md derives 3205 steps/s from the firmware's timer setup -
TIM6 at ARR 155 over an APB1 prescaler of 4, one STEP edge per interrupt - but
that chain rests on SystemCoreClock holding HCLK, and SystemCoreClock's
initialiser is not a plain constant anywhere in the image. So the figure is
derived, not measured. This measures it.

How it measures it. F30 D<n> drives the selector to an absolute step position
without homing, and its position counter is the same one every other selector
move uses. So F30 D0 puts the counter at a known zero from wherever it started,
and F30 D16384 is then exactly one turret revolution - 16384 steps, eight slot
pitches. Timing the second move gives steps/s directly. Homing would not do:
it leaves the counter at whichever slot was pending, so the distance would be
unknown.

Arrival is the selector driver's standstill bit, read with F63. F13's
stall_state is the FEEDER's motion bit and stays clear throughout a selector
move, so it cannot see this motor at all.

    scp tools/ifs/ifs_selector_timing.py root@<printer>:/tmp/
    ssh root@<printer>
    LD_LIBRARY_PATH=/usr/prog/libffi-3.4.4/lib:/usr/prog/openssl-1.0.2d/lib:\
/usr/prog/Python-3.8.2/lib /usr/prog/Python-3.8.2/bin/python3 \
/tmp/ifs_selector_timing.py --i-know-it-moves

Forge-X must be up and the stock UI stopped, so /dev/ttyS4 is free - the same
precondition ifs_probe.py documents. Stop klipper first, or its poller and this
script will split each other's replies.

Every run ends with F15 C whatever happened. F30 parks the state machine at 129
and it never leaves on its own; an unfreed 129 answers every later feed with
"FFS not ready.", which would look like a broken board.
"""
import argparse
import re
import sys
import time

## pyserial is imported inside main(), not here, so --help and the safety
## refusal work on a workstation that does not have it. The printer's
## Python 3.8 ships 3.4.
PORT, BAUD = "/dev/ttyS4", 115200

QUIET_GAP = 0.35      # a reply is over when the board is silent this long
OVERALL_CAP = 3.0

## The turret: eight stops 2048 steps apart, one revolution every 16384. Same
## coordinates the board's own moves use - D4096 is where F24 C2 parks it.
SLOT_PITCH = 2048
STEPS_PER_TURN = 8 * SLOT_PITCH

## What docs/AD5X_IFS_PROTOCOL.md derives. Printed alongside the measurement so
## a disagreement is obvious rather than something to look up afterwards.
DERIVED_STEPS_PER_SEC = 1000000.0 / 156.0 / 2.0

## Arrival: consecutive standstill readings. The bit lags the last step, and one
## sample of a bit that toggles is noise.
STANDSTILL_SAMPLES = 3
POLL_INTERVAL = 0.05
MOVE_TIMEOUT = 30.0


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
    return bytes(buf).decode("ascii", "replace")


def send(ser, cmd):
    ser.reset_input_buffer()
    ser.write((cmd + " \r\n").encode())
    return read_reply(ser)


def selector_moving(ser):
    """True/False from the selector driver's standstill bit; None if silent.

    F63 is DRV_STATUS on the F60-F64 bank, which is the selector - established
    by moving each motor and watching which bank lost its standstill bit.

    This is a second hand-written copy of ifs_diagnostics.read_driver_motion,
    which is normally the wrong answer. It is deliberate here: this file is
    scp'd to a printer on its own and cannot import the klipper plugin tree.
    If the standstill decode or the "None is not arrival" rule changes there,
    it has to change here too.
    """
    match = re.search(r"([0-9a-fA-F]{8})\b", send(ser, "F63"))
    if not match:
        return None
    return not (int(match.group(1), 16) >> 31) & 1


def wait_for_standstill(ser, deadline):
    """Seconds until the selector settled, or None if it never confirmed."""
    started = time.time()
    still = 0
    while time.time() < deadline:
        moving = selector_moving(ser)
        ## None is "the driver did not answer", not "it arrived". Treating
        ## silence as arrival would report a move far shorter than it was.
        still = still + 1 if moving is False else 0
        if still >= STANDSTILL_SAMPLES:
            return time.time() - started
        time.sleep(POLL_INTERVAL)
    return None


def jog(ser, position, label):
    """Send one F30 and time it to standstill. Returns seconds, or None."""
    print("  %-28s F30 D%-6d" % (label, position), end="", flush=True)
    reply = send(ser, "F30 D%d" % position)
    if "F30 ok" not in reply:
        print("  UNEXPECTED REPLY %r" % reply.strip())
        return None
    elapsed = wait_for_standstill(ser, time.time() + MOVE_TIMEOUT)
    if elapsed is None:
        print("  never reported standstill in %gs" % MOVE_TIMEOUT)
    else:
        print("  %6.2f s" % elapsed)
    return elapsed


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--i-know-it-moves", action="store_true",
                    help="required; this tool turns the selector")
    ap.add_argument("--steps", type=int, default=STEPS_PER_TURN,
                    help="distance to time, in selector steps "
                         "(default %d, one full revolution)" % STEPS_PER_TURN)
    ap.add_argument("--repeat", type=int, default=3,
                    help="timed runs to average (default 3)")
    args = ap.parse_args()

    if not args.i_know_it_moves:
        raise SystemExit(
            "This tool MOVES THE SELECTOR. Unload filament, stop klipper, then\n"
            "re-run with --i-know-it-moves. For a read-only sweep use\n"
            "tools/ifs/ifs_probe.py instead.")
    if not 0 < args.steps <= STEPS_PER_TURN:
        raise SystemExit("--steps must be within 1..%d" % STEPS_PER_TURN)

    import serial

    ser = serial.Serial(PORT, BAUD, timeout=0.1)
    timings = []
    try:
        print("=" * 62)
        print("The board, before anything")
        print("  F13 %s" % send(ser, "F13").strip())
        print("  F19 %s" % send(ser, "F19").strip())

        print("\n" + "=" * 62)
        print("Timing %d steps, %d run(s)" % (args.steps, args.repeat))
        for run in range(args.repeat):
            ## Zero first, so the timed move's distance is exactly args.steps
            ## whatever the turret was doing beforehand.
            jog(ser, 0, "run %d: to zero" % (run + 1))
            elapsed = jog(ser, args.steps, "run %d: TIMED" % (run + 1))
            if elapsed:
                timings.append(elapsed)

        print("\n" + "=" * 62)
        print("Result")
        if not timings:
            print("  nothing timed - the selector never reported standstill.")
            print("  Check F63 answers at all: an all-zero DRV_STATUS means")
            print("  the read path is broken, not that the motor is running.")
            return 1
        average = sum(timings) / len(timings)
        measured = args.steps / average
        print("  runs           : %s"
              % ", ".join("%.2f s" % t for t in timings))
        print("  average        : %.3f s for %d steps" % (average, args.steps))
        print("  MEASURED       : %.0f steps/s" % measured)
        print("  derived (docs) : %.0f steps/s" % DERIVED_STEPS_PER_SEC)
        ratio = measured / DERIVED_STEPS_PER_SEC
        print("  ratio          : %.3f" % ratio)
        if 0.95 <= ratio <= 1.05:
            print("\n  Within 5%. The derivation in AD5X_IFS_PROTOCOL.md holds.")
        else:
            print("\n  Off by more than 5%%. The timer chain needs re-reading;")
            print("  a ratio near %.2f or %.2f would point at the APB1"
                  % (0.5, 2.0))
            print("  prescaler or SystemCoreClock being other than assumed.")
        print("\n  At this rate a slot-to-slot move (%d steps) is %.2f s, and"
              % (SLOT_PITCH * 2, (SLOT_PITCH * 2) / measured))
        print("  the worst-case F24 (%d steps, re-home included) is %.1f s."
              % (STEPS_PER_TURN * 2, (STEPS_PER_TURN * 2) / measured))
    finally:
        ## F30 leaves the state machine at 129 for ever. Always, on every path.
        print("\nfreeing the state machine: F15 C -> %s"
              % send(ser, "F15 C").strip())
        ser.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
