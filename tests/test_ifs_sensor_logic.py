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


## Measured, and the labels matter more than the numbers.
##
## AT is the tip covering the sensor. NEAR is the tip off the sensor but still
## in the extruder - a completed load rests at 0.023, and a strand ten
## millimetres back reads 0.017. Those used to be called "disengaged" and the
## table called them ABSENT, which is how a load came to skip the cut and drive
## the next lane into filament the gear was still holding.
##
## EMPTY is the only state with no filament near the extruder at all: cut,
## purged and retracted, n=14, spread 0.0007. The gap from NEAR to EMPTY is 8x
## and there is nothing in it.
AT = (0.0074, 0.0077, 0.0082, 0.008089, 0.0085)
NEAR = (0.0158, 0.0174, 0.0227, 0.0249, 0.0432, 0.0484, 0.053205)
EMPTY = (0.3979, 0.3983, 0.3986)


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
        self.assertIn("0.300", text)


class TestMeasuredAD5X(unittest.TestCase):
    def test_a_tip_on_the_sensor_is_present(self):
        s = sensor()
        for v in AT:
            self.assertEqual(s.classify(v), L.PRESENT, "adc=%s" % v)
            self.assertTrue(s.has_filament(v), "adc=%s" % v)

    def test_a_tip_off_the_sensor_but_still_in_the_extruder_is_present(self):
        """The regression that cost an evening, and the reason for this table.

        A completed load rests at 0.023. Reading that as ABSENT makes the next
        load say "extruder already empty", skip the cut and the 60 mm withdraw,
        and feed the incoming lane into a strand the gear is still gripping -
        which stalls, and looks exactly like broken hardware.
        """
        s = sensor()
        for v in NEAR:
            self.assertEqual(s.classify(v), L.PRESENT, "adc=%s" % v)
            self.assertTrue(s.has_filament(v), "adc=%s" % v)

    def test_an_empty_toolhead_is_absent(self):
        s = sensor()
        for v in EMPTY:
            self.assertEqual(s.classify(v), L.ABSENT, "adc=%s" % v)
            self.assertFalse(s.has_filament(v), "adc=%s" % v)

    def test_the_threshold_sits_in_the_one_real_gap(self):
        ## Everything with filament near the extruder on one side, an empty
        ## toolhead on the other, and nothing measured in between. A threshold
        ## anywhere else is resolving a difference that was never observed.
        present_max = L.AD5X_TOOLHEAD[0][0]
        self.assertGreater(present_max, max(AT + NEAR) * 5)
        self.assertLess(present_max, min(EMPTY))

    def test_the_curve_has_no_gap_to_put_a_fault_band_in(self):
        """Swept 1mm at a time: flat to 8mm, then a knee, then a climb.

        The old table called 0.015-0.020 a fault, on the theory that the
        readings formed two clusters with nothing between. They do not - it is
        one continuous proximity curve, and 0.017 is an ordinary tip ten
        millimetres back.
        """
        s = sensor()
        for v in (0.0085, 0.0094, 0.0158, 0.0174, 0.0227):
            self.assertEqual(s.classify(v), L.PRESENT, "adc=%s" % v)

    def test_only_an_impossible_reading_is_a_fault(self):
        ## Above every filament position AND above an empty toolhead: the
        ## sensor is disconnected or shorted, not reporting a strand.
        s = sensor()
        self.assertEqual(s.classify(0.9), L.FAULT)
        self.assertTrue(s.has_filament(0.9))   # fail-safe: no false runout

    def test_polarity_is_inverted(self):
        ## Low means PRESENT. Backwards inverts every runout on the machine.
        s = sensor()
        self.assertTrue(s.has_filament(0.008))
        self.assertFalse(s.has_filament(0.398))


class TestZmodAgrees(unittest.TestCase):
    """Stock's thresholds against ours, now that ours ARE stock's.

    This class used to be called TestZmodCannotDetectRunout and asserted the
    opposite. That claim came from an empty-toolhead figure taken with filament
    still in the path; once the real one was measured, and once the curve
    between them was swept, zmod's thresholds turned out to be the right ones
    and the narrow table was the mistake.
    """

    def test_it_agrees_with_us_in_every_measured_state(self):
        z = sensor(L.ZMOD_TOOLHEAD)
        ours = sensor()
        for v in AT + NEAR + EMPTY:
            self.assertEqual(z.has_filament(v), ours.has_filament(v),
                             "adc=%s" % v)

    def test_it_detects_a_real_runout(self):
        ## The claim that it could not was the wrong-measurement artefact.
        z = sensor(L.ZMOD_TOOLHEAD)
        for v in EMPTY:
            self.assertFalse(z.has_filament(v), "adc=%s" % v)

    def test_the_two_tables_differ_only_on_an_impossible_reading(self):
        ## Above 0.72 no filament position can produce a reading, so zmod calls
        ## it present and we call it a fault. Same runout decision either way
        ## while fail_safe is on; ours keeps the fault visible.
        self.assertTrue(sensor(L.ZMOD_TOOLHEAD).has_filament(0.9))
        self.assertTrue(sensor().has_filament(0.9))
        self.assertEqual(sensor(L.ZMOD_TOOLHEAD).classify(0.9), L.PRESENT)
        self.assertEqual(sensor().classify(0.9), L.FAULT)

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
        self.assertTrue(s.has_filament(0.9))
        self.assertEqual(s.classify(0.9), L.FAULT)


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
    """A jam is the ABSENCE of motion during a move we asked for.

    The board's bit is SET while filament moves - measured with an empty
    channel as the control - so this watches for it going quiet, not for it
    coming on.
    """

    def moving(self):
        return status(stall=0b10)      # channel 2 filament in motion

    def still(self):
        return status(stall=0)         # nothing moving

    def test_motion_is_not_a_jam(self):
        t = L.MotionTracker(2, required=3)
        for _ in range(5):
            self.assertFalse(t.update(self.moving()))
        self.assertFalse(t.tripped)

    def test_sustained_stillness_during_a_move_trips_once(self):
        t = L.MotionTracker(2, required=3)
        self.assertFalse(t.update(self.still()))
        self.assertFalse(t.update(self.still()))
        self.assertTrue(t.update(self.still()))
        self.assertTrue(t.tripped)
        self.assertFalse(t.update(self.still()))   # not again

    def test_a_brief_pause_is_not_a_jam(self):
        t = L.MotionTracker(2, required=3)
        t.update(self.still())
        t.update(self.still())
        t.update(self.moving())
        self.assertFalse(t.update(self.still()))
        self.assertFalse(t.tripped)

    def test_stillness_while_not_moving_does_not_count(self):
        ## Nothing was asked to move, so of course nothing is moving.
        t = L.MotionTracker(2, required=2)
        for _ in range(5):
            self.assertFalse(t.update(self.still(), moving=False))
        self.assertFalse(t.tripped)

    def test_another_channels_motion_does_not_excuse_this_one(self):
        t = L.MotionTracker(2, required=1)
        self.assertTrue(t.update(status(stall=0b1000)))

    def test_missing_status_resets_rather_than_trips(self):
        t = L.MotionTracker(2, required=2)
        t.update(self.still())
        self.assertFalse(t.update(None))
        self.assertFalse(t.update(self.still()))

    def test_reset(self):
        t = L.MotionTracker(2, required=1)
        t.update(self.still())
        self.assertTrue(t.tripped)
        t.reset()
        self.assertFalse(t.tripped)

    def test_required_must_be_sane(self):
        with self.assertRaises(ValueError):
            L.MotionTracker(1, required=0)


if __name__ == "__main__":
    unittest.main()
