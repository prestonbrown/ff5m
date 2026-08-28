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

## ZMOD's numbers, from `value >= 0.72 if value > 0.3 else True`.
ZMOD_LOW = 0.30
ZMOD_HIGH = 0.72

## MEASURED on an AD5X toolhead, twice, in both directions:
##
##     filament engaged in the sensor   0.0075 - 0.0082
##     no filament, resting at the gear 0.025  - 0.049
##
## So a LOW value means filament PRESENT - the opposite of the intuitive
## reading, and the same inversion the IFS channel sensors show. It saturates
## within 3 mm of travel and is fully reversible.
##
## The consequence for ZMOD: every one of those readings is below 0.055, so its
## rule takes the `else True` branch in every state and reports filament present
## whether the toolhead is loaded, empty, or the sensor is disconnected. Its
## runout detection cannot fire on this hardware. Its thresholds look chosen for
## a sensor scaled an order of magnitude differently.
AD5X_PRESENT_MAX = 0.015
AD5X_ABSENT_MIN = 0.020


class AnalogFilamentSensor(object):
    """Classifies a raw 0..1 ADC reading from the toolhead filament sensor.

    Three bands, and what each MEANS is a constructor argument, because the
    polarity is a property of the hardware. The AD5X reads low when filament is
    present; ZMOD's thresholds assume the opposite and are an order of magnitude
    out besides. Both shapes fit this one class:

        AnalogFilamentSensor.for_ad5x()   measured, low = present
        AnalogFilamentSensor.zmod()       bug-compatible with zmod_ifs.py

    The middle band is where a real sensor should never sit. On the AD5X it is
    the gap between the two measured clusters, so a reading there means
    something is wrong - a half-inserted strand, or a failing sensor.
    """

    def __init__(self, low=ZMOD_LOW, high=ZMOD_HIGH, low_meaning=PRESENT,
                 mid_meaning=ABSENT, high_meaning=PRESENT, fail_safe=True):
        if low > high:
            raise ValueError("low threshold %r is above high %r" % (low, high))
        self.low = low
        self.high = high
        self.low_meaning = low_meaning
        self.mid_meaning = mid_meaning
        self.high_meaning = high_meaning
        self.fail_safe = fail_safe

    @classmethod
    def for_ad5x(cls):
        """The measured AD5X toolhead sensor. Low is present."""
        return cls(low=AD5X_PRESENT_MAX, high=AD5X_ABSENT_MIN,
                   low_meaning=PRESENT, mid_meaning=FAULT,
                   high_meaning=ABSENT)

    @classmethod
    def zmod(cls):
        """Bug-compatible with zmod_ifs.py, which never reports a runout here."""
        return cls(low=ZMOD_LOW, high=ZMOD_HIGH, low_meaning=PRESENT,
                   mid_meaning=ABSENT, high_meaning=PRESENT)

    def classify(self, value):
        """PRESENT / ABSENT / FAULT for one reading, or FAULT for no reading."""
        if value is None:
            return FAULT
        if value <= self.low:
            return self.low_meaning
        if value >= self.high:
            return self.high_meaning
        return self.mid_meaning

    def has_filament(self, value):
        """The boolean klipper's RunoutHelper wants.

        A FAULT is not a runout. With `fail_safe` set, an unreadable or
        ambiguous sensor reports filament present, so a broken wire cannot pause
        a running print; the fault stays visible through `classify()`.
        """
        state = self.classify(value)
        if state == FAULT:
            return bool(self.fail_safe)
        return state == PRESENT

    def describe(self, value):
        return "%s (adc=%s, <=%.3f %s, >=%.3f %s)" % (
            self.classify(value),
            "none" if value is None else "%.4f" % value,
            self.low, self.low_meaning, self.high, self.high_meaning)


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
