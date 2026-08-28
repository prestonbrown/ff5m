## Filament-sensing logic for the AD5X, with no klipper in it.
##
## Two unrelated sensors get conflated because ZMOD puts them in one file:
##
##   * The TOOLHEAD sensor is an analog input on the extruder board
##     (`temperature_sensor filamentValue`, eboard:PA3, declared as a thermistor
##     purely so klipper will sample the ADC). It has nothing to do with the IFS
##     serial board. The AD5M's equivalent is a plain digital microswitch on
##     PB14, which is why the AD5M needs no code for this and the AD5X does.
##
##   * The PER-CHANNEL sensors are the IFS board's own `silk_state` bits, read
##     over the serial link. The AD5M has no counterpart at all.
##
## Both end up registered as stock `filament_switch_sensor` objects, which is
## the surface HelixScreen subscribes to. The klipper-facing shims are thin; the
## decisions live here so they can be tested without a printer.
##
## Copyright (C) 2026, Preston Brown
## Portions derived from zmod (C) 2025-2026 ghzserg <https://github.com/ghzserg/zmod/>
##
## This file may be distributed under the terms of the GNU GPLv3 license


## What an ADC reading means. FAULT is deliberately distinct from ABSENT: an
## analog line can tell "no filament" apart from "sensor unplugged", and a
## digital pin cannot. That distinction is the whole reason this input is analog,
## and collapsing it to a boolean throws it away.
PRESENT = "present"
ABSENT = "absent"
FAULT = "fault"

## ZMOD's numbers, from `value >= 0.72 if value > 0.3 else True`. Its low branch
## returns "filament present", which on this printer is what an EMPTY toolhead
## reads - see docs. Kept as the compatibility default and nothing more; the
## thresholds are constructor arguments precisely so a measured set can replace
## them without touching logic.
ZMOD_LOW = 0.30
ZMOD_HIGH = 0.72


class AnalogFilamentSensor(object):
    """Classifies a raw 0..1 ADC reading from the toolhead filament sensor.

    Three bands, low to high: below `low` is `low_meaning`, between `low` and
    `high` is ABSENT, at or above `high` is PRESENT.

    `low_meaning` exists because what the bottom band signifies is a property of
    the hardware, not of this code. ZMOD treats it as PRESENT. If it is instead
    the open-circuit reading of a disconnected sensor, FAULT is correct and a
    runout must not be inferred from it. Set it from measurement, and until
    measured, `fail_safe` decides which way an unknown reading errs.
    """

    def __init__(self, low=ZMOD_LOW, high=ZMOD_HIGH, low_meaning=PRESENT,
                 fail_safe=True):
        if low > high:
            raise ValueError("low threshold %r is above high %r" % (low, high))
        self.low = low
        self.high = high
        self.low_meaning = low_meaning
        self.fail_safe = fail_safe

    def classify(self, value):
        """PRESENT / ABSENT / FAULT for one reading, or FAULT for no reading."""
        if value is None:
            return FAULT
        if value <= self.low:
            return self.low_meaning
        if value >= self.high:
            return PRESENT
        return ABSENT

    def has_filament(self, value):
        """The boolean klipper's RunoutHelper wants.

        A FAULT is not a runout. With `fail_safe` set, an unreadable sensor
        reports filament present, so a broken wire cannot pause a running print;
        the fault is still visible through `classify()` for anything that wants
        to surface it.
        """
        state = self.classify(value)
        if state == FAULT:
            return bool(self.fail_safe)
        return state == PRESENT

    def describe(self, value):
        return "%s (adc=%s, bands: <=%.2f %s, >=%.2f present)" % (
            self.classify(value),
            "none" if value is None else "%.4f" % value,
            self.low, self.low_meaning, self.high)


class ChannelFilamentSensor(object):
    """Filament presence for one IFS channel, from the board's silk bitmask.

    Reads an IfsStatus rather than the wire, so it costs nothing per channel -
    four of these share one F13 poll.
    """

    def __init__(self, channel):
        if channel < 1:
            raise ValueError("channel numbers start at 1, got %r" % channel)
        self.channel = channel

    def has_filament(self, status):
        """None when there is no reading, so callers can tell it from False."""
        if status is None:
            return None
        return status.has_filament(self.channel)


class MotionTracker(object):
    """Detects filament that has stopped moving while it was told to move.

    The board reports a per-channel stall bit. A single set bit is not a runout:
    it goes high transiently at the start and end of normal moves, which is
    visible on real hardware - a clean 20 mm retract sets and clears it. ZMOD
    guards against that with a consecutive-sample count, and so does this.

    `required` consecutive stalled samples trip it. Any unstalled sample resets
    the run, so noise cannot accumulate across a whole move.
    """

    def __init__(self, channel, required=3):
        if required < 1:
            raise ValueError("required must be at least 1")
        self.channel = channel
        self.required = required
        self._run = 0
        self.tripped = False

    def update(self, status, moving=True):
        """Feed one poll. Returns True on the sample that trips it.

        `moving` is whether the channel was commanded to move. A stall while
        nothing was asked to move is not interesting and does not count.
        """
        if status is None or not moving:
            self._run = 0
            return False
        if not status.is_stalled(self.channel):
            self._run = 0
            return False
        self._run += 1
        if self._run >= self.required and not self.tripped:
            self.tripped = True
            return True
        return False

    def reset(self):
        self._run = 0
        self.tripped = False
