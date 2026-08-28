## Filament-sensing decisions for the AD5X, with no klipper in them.
##
## Two unrelated sensors get conflated because ZMOD puts them in one file:
##
##   * The TOOLHEAD sensor is an analog input on the extruder board
##     (`temperature_sensor filamentValue`, eboard:PA3, declared as a thermistor
##     purely so klipper will sample the ADC). It has nothing to do with the IFS
##     serial board. The AD5M's equivalent is a plain digital microswitch, which
##     is why the AD5M needs no code for this and the AD5X does.
##
##   * The PER-CHANNEL sensors are the IFS board's own `silk_state` bits, read
##     over the serial link. The AD5M has no counterpart at all.
##
## Copyright (C) 2026, Preston Brown
## Portions derived from zmod (C) 2025-2026 ghzserg <https://github.com/ghzserg/zmod/>
##
## This file may be distributed under the terms of the GNU GPLv3 license


## What a reading means. FAULT is deliberately distinct from ABSENT: an analog
## line can tell "no filament" apart from "sensor unplugged", and a digital pin
## cannot. That distinction is the only reason to put this on an ADC at all.
PRESENT = "present"
ABSENT = "absent"
FAULT = "fault"


## A sensor's calibration is a table of ascending upper bounds. Reading it top to
## bottom is reading the measurement that produced it, which is the point:
##
##   MEASURED on an AD5X toolhead, twice, in both directions -
##       filament engaged    0.0075 - 0.0082
##       no filament         0.025  - 0.049
##
##   so low means PRESENT, inverted from the intuitive reading and matching the
##   inversion the IFS channel sensors show. It saturates within 3 mm of travel
##   and is fully reversible. The middle band is the gap between those two
##   clusters, where nothing was ever observed: a reading there is a
##   half-inserted strand or a failing sensor, not a clean state.
def toolhead_bands(present_max, absent_min):
    """The AD5X toolhead sensor's shape, given where its two clusters sit.

    Between the clusters is FAULT rather than a state: nothing was ever
    observed there, so a reading in the gap is a half-inserted strand or a
    failing sensor. Config-supplied thresholds build the table through this
    same function, so a tuned sensor cannot end up a different shape.
    """
    return ((present_max, PRESENT), (absent_min, FAULT), (None, ABSENT))


AD5X_PRESENT_MAX = 0.015
AD5X_ABSENT_MIN = 0.020
AD5X_TOOLHEAD = toolhead_bands(AD5X_PRESENT_MAX, AD5X_ABSENT_MIN)

##   ZMOD's `value >= 0.72 if value > 0.3 else True`, as data. Every reading this
##   printer produces is below 0.055, so it lands in the first band always and
##   reports filament present whether the toolhead is loaded, empty, or the
##   sensor is unplugged - its runout detection cannot fire here. Kept so the
##   two can be compared, and so a test can pin the difference.
ZMOD_TOOLHEAD = ((0.30, PRESENT), (0.72, ABSENT), (None, PRESENT))


class AnalogFilamentSensor(object):
    """Maps an analog reading to PRESENT / ABSENT / FAULT through a band table.

    `bands` is ascending `(upper_bound, meaning)` pairs, the last with a bound
    of None to catch everything above. A value belongs to the first band whose
    bound it does not exceed. Polarity is not baked in anywhere - it falls out
    of the table, which is why the same class serves a sensor that reads low
    for present and one that reads high.
    """

    def __init__(self, bands=AD5X_TOOLHEAD, fail_safe=True):
        bands = tuple(bands)
        if not bands:
            raise ValueError("a band table needs at least one band")
        if bands[-1][0] is not None:
            raise ValueError("the last band must have an open upper bound "
                             "(None), or some readings classify as nothing")
        bounds = [b for b, _ in bands[:-1]]
        if any(b is None for b in bounds):
            raise ValueError("only the last band may have an open bound")
        if bounds != sorted(bounds):
            raise ValueError("band bounds must ascend, got %r" % (bounds,))
        self.bands = bands
        self.fail_safe = fail_safe

    def classify(self, value):
        """PRESENT / ABSENT / FAULT, or FAULT when there is no reading."""
        if value is None:
            return FAULT
        for bound, meaning in self.bands:
            if bound is None or value <= bound:
                return meaning
        raise AssertionError("unreachable: the last band is open")

    def has_filament(self, value):
        """The boolean klipper's RunoutHelper wants.

        A FAULT is not a runout. With `fail_safe` set, an unreadable or
        ambiguous sensor reports filament present so a broken wire cannot pause
        a running print; the fault stays visible through `classify()`.
        """
        state = self.classify(value)
        if state == FAULT:
            return bool(self.fail_safe)
        return state == PRESENT

    def describe(self, value):
        return "%s (adc=%s; %s)" % (
            self.classify(value),
            "none" if value is None else "%.4f" % value,
            ", ".join("<=%.3f %s" % (b, m) if b is not None else "above %s" % m
                      for b, m in self.bands))


class ChannelFilamentSensor(object):
    """Filament presence for one IFS channel, from the board's silk bitmask.

    Reads an IfsStatus rather than the wire, so four of these share one poll.
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
    """Detects filament that stopped moving while it was told to move.

    A single set stall bit is not a runout: measured on hardware, a clean 20 mm
    retract sets and clears it in passing. `required` consecutive stalled
    samples trip it, and any unstalled sample resets the run, so transients
    cannot accumulate across a move.
    """

    def __init__(self, channel, required=3):
        if required < 1:
            raise ValueError("required must be at least 1, got %r" % required)
        self.channel = channel
        self.required = required
        self._run = 0
        self.tripped = False

    def update(self, status, moving=True):
        """Feed one poll. True on the sample that trips it, once.

        `moving` is whether the channel was commanded to move; a stall while
        nothing was asked to move is not interesting.
        """
        if status is None or not moving or not status.is_stalled(self.channel):
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
