## Filament-sensing decisions for the AD5X, with no klipper in them.
##
## Two unrelated sensors, easily conflated:
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


## A sensor's calibration is a table of ascending upper bounds. Reading it top
## to bottom is reading the measurement that produced it, which is the point.
##
## THE MEASUREMENT, and the only place it is written down. Anything else that
## needs these numbers refers here rather than restating them - an earlier
## version had them copied into three files and a doc, and they all went stale
## together the moment the measurement was redone.
##
##   MEASURED on an AD5X toolhead, via query_adc on `temperature_sensor
##   filamentValue` (eboard:PA3):
##
##     filament at the sensor          0.0075 - 0.0082
##     filament in the path, but not
##       at the sensor                 0.045  - 0.050
##     nothing in the path at all      0.3979 - 0.3986   (n=14, spread 0.0007)
##
##   Low means PRESENT, inverted from the intuitive reading and matching the
##   inversion the IFS channel sensors show. It saturates within 3 mm of travel
##   and is fully reversible.
##
##   The empty figure was originally recorded as 0.025-0.049 and that was wrong:
##   it was taken with filament still sitting in the path, which is the middle
##   row above. A genuinely empty toolhead - cut, purged and retracted - reads
##   0.398, and the true span is therefore 50x, not 6x.
##
##   SWEPT 2026-08-29, loaded lane retracted 1 mm at a time off the sensor and
##   pushed back, n=4 per step. This is the shape the rows above only sampled:
##
##     0 - 8 mm back    0.0074 - 0.0085     flat; the sensor saturates
##         9 mm back    0.0094 - 0.0158     the knee, in one millimetre
##        10 mm back    0.0170 - 0.0179
##     resting after a
##       completed load 0.0225 - 0.0236     stable, reproducible
##
##   The reading is a PROXIMITY CURVE, not two clusters: flat while the strand
##   covers the sensor, then climbing steeply once it clears. So "at the sensor"
##   and "in the path" are the same physical thing at different distances, and
##   there is no gap between them to put a fault band in. The only real gap is
##   the 8x span from 0.050 to 0.398 - filament anywhere near the extruder
##   against nothing there at all - and that is the only place a threshold
##   belongs.
def toolhead_bands(present_max, absent_min):
    """The AD5X toolhead sensor's shape, given where its thresholds sit.

    PRESENT below, ABSENT above, and FAULT only above `absent_min`, which is
    higher than an empty toolhead ever reads - a disconnected or shorted sensor
    rails, and that is the one reading no filament position can produce.
    Config-supplied thresholds build the table through this same function, so a
    tuned sensor cannot end up a different shape.
    """
    return ((present_max, PRESENT), (absent_min, ABSENT), (None, FAULT))


## Stock firmware's own thresholds: `value >= 0.72 if value > 0.3 else True`.
## They are the defaults here because the sweep showed the
## narrow table this used to carry was WRONG ON HARDWARE, twice over:
##
##   - A completed load rests at 0.023, which that table called ABSENT. The next
##     load then read "extruder already empty", skipped the cut and the 60 mm
##     withdraw, and drove the incoming lane into a strand still gripped by the
##     gear. That is the stall that looked like broken hardware.
##   - Its FAULT band, 0.015 to 0.020, sat squarely on the knee of the curve, so
##     an ordinary tip 10 mm off the sensor read as a failing sensor.
##
## 0.30 sits in the empty 8x span between "filament somewhere near the extruder"
## and "nothing in the path", six times above the highest present reading and a
## quarter below the lowest absent one. The narrow thresholds had 0.005 of
## margin against a 0.001 spread and were chosen from cluster endpoints before
## anything had swept between them.
AD5X_PRESENT_MAX = 0.30
AD5X_ABSENT_MIN = 0.72
AD5X_TOOLHEAD = toolhead_bands(AD5X_PRESENT_MAX, AD5X_ABSENT_MIN)

## The stock classifier as data (same bounds as above, PRESENT above 0.72
## rather than FAULT), for a test that pins where ours differs. Same call either way with
## `fail_safe` on - a railed sensor must not pause a running print - but ours
## keeps the fault visible through classify() instead of reporting filament
## that is not there.
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

    The board's motion bit is SET while a channel's filament is moving -
    measured with an empty channel as a control, and the reason this watches
    for its ABSENCE. `required` consecutive samples with no motion, while the
    channel was commanded to move, trip it; any sample showing motion resets
    the run, so a brief pause mid-move is not a jam.
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
        if status is None or not moving:
            self._run = 0
            return False
        if status.is_moving(self.channel):
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
