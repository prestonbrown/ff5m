## Tests for the AD5X filament-sensing decisions.
##
## The ADC readings here were measured on a live AD5X, twice, in both
## directions - see docs/AD5X_IFS_PROTOCOL.md.
##
## Copyright (C) 2026, Preston Brown
##
## This file may be distributed under the terms of the GNU GPLv3 license

import unittest

import ifs_modules

L = ifs_modules.load("ifs_sensor_logic")
S = ifs_modules.load("ifs_status")


## Measured. Engaged in the sensor, and not.
ENGAGED = (0.0075, 0.0081, 0.0082, 0.008089)
DISENGAGED = (0.0249, 0.0432, 0.0484, 0.053205)


def status(silk=0, stall=0):
    return S.IfsStatus(state=S.READY, silk_mask=silk, active_channel=0,
                       insert_mask=0, stall_mask=stall)


def sensor(bands=None, **kwargs):
    return L.AnalogFilamentSensor(bands or L.AD5X_TOOLHEAD, **kwargs)


class TestBandTable(unittest.TestCase):
    def test_a_value_takes_the_first_band_it_fits(self):
        s = L.AnalogFilamentSensor(((1.0, "a"), (2.0, "b"), (None, "c")))
        self.assertEqual(s.classify(0.5), "a")
        self.assertEqual(s.classify(1.0), "a")     # bounds are inclusive
        self.assertEqual(s.classify(1.5), "b")
        self.assertEqual(s.classify(2.0), "b")
        self.assertEqual(s.classify(99), "c")

    def test_the_last_band_must_be_open(self):
        ## Otherwise some readings classify as nothing at all.
        with self.assertRaises(ValueError):
            L.AnalogFilamentSensor(((1.0, L.PRESENT), (2.0, L.ABSENT)))

    def test_only_the_last_band_may_be_open(self):
        with self.assertRaises(ValueError):
            L.AnalogFilamentSensor(((None, L.PRESENT), (None, L.ABSENT)))

    def test_bounds_must_ascend(self):
        with self.assertRaises(ValueError):
            L.AnalogFilamentSensor(((2.0, L.PRESENT), (1.0, L.ABSENT),
                                    (None, L.FAULT)))

    def test_an_empty_table_is_refused(self):
        with self.assertRaises(ValueError):
            L.AnalogFilamentSensor(())

    def test_no_reading_is_a_fault(self):
        self.assertEqual(sensor().classify(None), L.FAULT)

    def test_describe_renders_the_table(self):
        text = sensor().describe(0.008)
        self.assertIn("present", text)
        self.assertIn("0.008", text)
        self.assertIn("0.015", text)


class TestMeasuredAD5X(unittest.TestCase):
    def test_engaged_readings_are_present(self):
        s = sensor()
        for v in ENGAGED:
            self.assertEqual(s.classify(v), L.PRESENT, "adc=%s" % v)
            self.assertTrue(s.has_filament(v), "adc=%s" % v)

    def test_disengaged_readings_are_absent(self):
        s = sensor()
        for v in DISENGAGED:
            self.assertEqual(s.classify(v), L.ABSENT, "adc=%s" % v)
            self.assertFalse(s.has_filament(v), "adc=%s" % v)

    def test_the_thresholds_sit_between_the_measured_clusters(self):
        ## If they do not, the sensor is being asked to resolve something that
        ## was never measured.
        present_max, absent_min = L.AD5X_TOOLHEAD[0][0], L.AD5X_TOOLHEAD[1][0]
        self.assertLess(max(ENGAGED), present_max)
        self.assertGreater(min(DISENGAGED), absent_min)
        self.assertLess(present_max, absent_min)

    def test_the_gap_between_clusters_is_a_fault(self):
        ## Nothing was ever observed here: a half-inserted strand or a failing
        ## sensor, not a clean state.
        s = sensor()
        self.assertEqual(s.classify(0.017), L.FAULT)
        self.assertTrue(s.has_filament(0.017))   # fail-safe: no false runout

    def test_polarity_is_inverted(self):
        ## Low means PRESENT. Backwards inverts every runout on the machine.
        s = sensor()
        self.assertTrue(s.has_filament(0.008))
        self.assertFalse(s.has_filament(0.048))


class TestZmodCannotDetectRunout(unittest.TestCase):
    """The finding this exercise produced, pinned so it cannot drift."""

    def test_zmod_reports_present_in_every_measured_state(self):
        ## Every reading on this printer is below 0.055, so zmod's table puts
        ## them all in its first band - loaded, empty, or disconnected.
        z = sensor(L.ZMOD_TOOLHEAD)
        for v in ENGAGED + DISENGAGED:
            self.assertTrue(z.has_filament(v), "adc=%s" % v)

    def test_the_two_tables_disagree_where_it_matters(self):
        empty = 0.0432
        self.assertFalse(sensor().has_filament(empty))
        self.assertTrue(sensor(L.ZMOD_TOOLHEAD).has_filament(empty))

    def test_zmod_needs_a_reading_this_printer_never_produces(self):
        ## For zmod's table to report absent, the sensor would have to land
        ## between 0.30 and 0.72. The measured range tops out near 0.05.
        z = sensor(L.ZMOD_TOOLHEAD)
        self.assertFalse(z.has_filament(0.5))
        self.assertGreater(L.ZMOD_TOOLHEAD[0][0], max(DISENGAGED) * 5)

    def test_zmod_reproduces_the_original_expression(self):
        ## Band bounds here are uniformly inclusive; zmod's expression is not
        ## (`<= 0.30` present, but `>= 0.72` present), so the two differ at
        ## exactly 0.72 and nowhere else. Matching that boundary would mean
        ## contorting the table for a threshold that is an order of magnitude
        ## wrong for this hardware. Asserted everywhere else, and the one
        ## difference is pinned below rather than hidden.
        def original(v):
            return v >= 0.72 if v > 0.3 else True
        z = sensor(L.ZMOD_TOOLHEAD)
        for i in range(101):
            v = i / 100.0
            if v == 0.72:
                continue
            self.assertEqual(z.has_filament(v), original(v), "adc=%.2f" % v)

    def test_the_one_boundary_where_the_model_differs(self):
        def original(v):
            return v >= 0.72 if v > 0.3 else True
        self.assertTrue(original(0.72))
        self.assertFalse(sensor(L.ZMOD_TOOLHEAD).has_filament(0.72))


class TestFailSafe(unittest.TestCase):
    def test_an_unreadable_sensor_does_not_cause_a_runout(self):
        s = sensor()
        self.assertTrue(s.has_filament(None))
        self.assertEqual(s.classify(None), L.FAULT)

    def test_fail_safe_can_be_turned_off(self):
        self.assertFalse(sensor(fail_safe=False).has_filament(None))

    def test_a_fault_is_still_visible_when_failing_safe(self):
        ## has_filament() hides it deliberately; classify() must not.
        s = sensor()
        self.assertTrue(s.has_filament(0.017))
        self.assertEqual(s.classify(0.017), L.FAULT)


class TestChannelSensor(unittest.TestCase):
    def test_reads_the_silk_bit(self):
        self.assertTrue(L.ChannelFilamentSensor(2)
                        .has_filament(status(silk=0b1011)))
        self.assertFalse(L.ChannelFilamentSensor(3)
                         .has_filament(status(silk=0b1011)))

    def test_no_status_is_none_not_false(self):
        ## False means "no filament"; None means "I do not know". Collapsing
        ## them reports a runout every time a poll is missed.
        self.assertIsNone(L.ChannelFilamentSensor(1).has_filament(None))

    def test_channels_start_at_one(self):
        with self.assertRaises(ValueError):
            L.ChannelFilamentSensor(0)


class TestMotionTracker(unittest.TestCase):
    def test_a_single_stall_sample_is_not_a_runout(self):
        ## Measured: a clean 20 mm retract sets the stall bit in passing.
        ## Tripping on one sample would fire on every normal move.
        t = L.MotionTracker(2, required=3)
        self.assertFalse(t.update(status(stall=0b10)))
        self.assertFalse(t.update(status(stall=0)))
        self.assertFalse(t.tripped)

    def test_consecutive_stalls_trip_it_once(self):
        t = L.MotionTracker(2, required=3)
        self.assertFalse(t.update(status(stall=0b10)))
        self.assertFalse(t.update(status(stall=0b10)))
        self.assertTrue(t.update(status(stall=0b10)))
        self.assertTrue(t.tripped)
        self.assertFalse(t.update(status(stall=0b10)))   # not again

    def test_a_clear_sample_resets_the_run(self):
        t = L.MotionTracker(2, required=3)
        t.update(status(stall=0b10))
        t.update(status(stall=0b10))
        t.update(status(stall=0))
        self.assertFalse(t.update(status(stall=0b10)))
        self.assertFalse(t.tripped)

    def test_a_stall_while_not_moving_does_not_count(self):
        t = L.MotionTracker(2, required=2)
        for _ in range(5):
            self.assertFalse(t.update(status(stall=0b10), moving=False))
        self.assertFalse(t.tripped)

    def test_another_channels_stall_is_ignored(self):
        self.assertFalse(L.MotionTracker(2, required=1)
                         .update(status(stall=0b1000)))

    def test_missing_status_resets_rather_than_trips(self):
        t = L.MotionTracker(2, required=2)
        t.update(status(stall=0b10))
        self.assertFalse(t.update(None))
        self.assertFalse(t.update(status(stall=0b10)))

    def test_reset(self):
        t = L.MotionTracker(2, required=1)
        t.update(status(stall=0b10))
        self.assertTrue(t.tripped)
        t.reset()
        self.assertFalse(t.tripped)

    def test_required_must_be_sane(self):
        with self.assertRaises(ValueError):
            L.MotionTracker(1, required=0)


if __name__ == "__main__":
    unittest.main()
