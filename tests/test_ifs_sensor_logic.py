## Tests for the AD5X filament-sensing logic.
##
## The ADC readings here were measured on a live AD5X - see
## docs/AD5X_IFS_PROTOCOL.md and the T5 notes in the M7 plan.
##
## Copyright (C) 2026, Preston Brown
##
## This file may be distributed under the terms of the GNU GPLv3 license

import importlib.util
import pathlib
import sys
import unittest


PLUGIN_DIR = (pathlib.Path(__file__).parents[1] / ".py" / "klipper" / "plugins")
sys.path.insert(0, str(PLUGIN_DIR))


def _load(name):
    spec = importlib.util.spec_from_file_location(
        name, PLUGIN_DIR / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


L = _load("ifs_sensor_logic")
S = _load("ifs_status")

## Measured on the rig with an EMPTY toolhead, stable to +-0.0005.
EMPTY_TOOLHEAD = 0.0277


def status(silk=0, stall=0):
    return S.IfsStatus(state=S.READY, silk_mask=silk, active_channel=0,
                       insert_mask=0, stall_mask=stall)


class TestAnalogBands(unittest.TestCase):
    def test_the_three_bands(self):
        s = L.AnalogFilamentSensor(low=0.30, high=0.72, low_meaning=L.ABSENT)
        self.assertEqual(s.classify(0.10), L.ABSENT)
        self.assertEqual(s.classify(0.50), L.ABSENT)
        self.assertEqual(s.classify(0.90), L.PRESENT)

    def test_boundaries_are_inclusive(self):
        s = L.AnalogFilamentSensor(low=0.30, high=0.72, low_meaning=L.FAULT)
        self.assertEqual(s.classify(0.30), L.FAULT)
        self.assertEqual(s.classify(0.72), L.PRESENT)
        self.assertEqual(s.classify(0.3001), L.ABSENT)
        self.assertEqual(s.classify(0.7199), L.ABSENT)

    def test_no_reading_is_a_fault(self):
        self.assertEqual(L.AnalogFilamentSensor().classify(None), L.FAULT)

    def test_low_band_meaning_is_configurable(self):
        for meaning in (L.PRESENT, L.ABSENT, L.FAULT):
            s = L.AnalogFilamentSensor(low_meaning=meaning)
            self.assertEqual(s.classify(0.05), meaning)

    def test_thresholds_must_be_ordered(self):
        with self.assertRaises(ValueError):
            L.AnalogFilamentSensor(low=0.8, high=0.2)


class TestZmodCompatibility(unittest.TestCase):
    def test_zmods_rule_calls_an_empty_toolhead_present(self):
        ## Measured: an AD5X with nothing in the extruder reads ~0.0277, and
        ## zmod's `value >= 0.72 if value > 0.3 else True` returns True there.
        ## Recorded as behaviour, not endorsed - see the module docstring.
        s = L.AnalogFilamentSensor()          # zmod's defaults
        self.assertEqual(s.classify(EMPTY_TOOLHEAD), L.PRESENT)
        self.assertTrue(s.has_filament(EMPTY_TOOLHEAD))

    def test_treating_the_low_band_as_empty_fixes_it(self):
        s = L.AnalogFilamentSensor(low_meaning=L.ABSENT)
        self.assertEqual(s.classify(EMPTY_TOOLHEAD), L.ABSENT)
        self.assertFalse(s.has_filament(EMPTY_TOOLHEAD))

    def test_matching_zmod_exactly_across_the_range(self):
        def zmod(v):
            return v >= 0.72 if v > 0.3 else True
        s = L.AnalogFilamentSensor()
        for i in range(0, 101):
            v = i / 100.0
            self.assertEqual(s.has_filament(v), zmod(v), "adc=%.2f" % v)


class TestFailSafe(unittest.TestCase):
    def test_an_unreadable_sensor_does_not_cause_a_runout(self):
        ## A broken wire must not pause a running print.
        s = L.AnalogFilamentSensor(fail_safe=True)
        self.assertTrue(s.has_filament(None))
        self.assertEqual(s.classify(None), L.FAULT)

    def test_fail_safe_can_be_turned_off(self):
        self.assertFalse(L.AnalogFilamentSensor(fail_safe=False)
                         .has_filament(None))

    def test_a_fault_is_still_visible_even_when_failing_safe(self):
        ## has_filament() hides it deliberately; classify() must not.
        s = L.AnalogFilamentSensor(low_meaning=L.FAULT, fail_safe=True)
        self.assertTrue(s.has_filament(0.01))
        self.assertEqual(s.classify(0.01), L.FAULT)


class TestChannelSensor(unittest.TestCase):
    def test_reads_the_silk_bit(self):
        sensor = L.ChannelFilamentSensor(2)
        self.assertTrue(sensor.has_filament(status(silk=0b1011)))
        self.assertFalse(L.ChannelFilamentSensor(3)
                         .has_filament(status(silk=0b1011)))

    def test_no_status_is_none_not_false(self):
        ## False means "no filament"; None means "I do not know". Collapsing
        ## them would report a runout every time a poll is missed.
        self.assertIsNone(L.ChannelFilamentSensor(1).has_filament(None))

    def test_channels_start_at_one(self):
        with self.assertRaises(ValueError):
            L.ChannelFilamentSensor(0)


class TestMotionTracker(unittest.TestCase):
    def test_a_single_stall_sample_is_not_a_runout(self):
        ## Measured on hardware: a clean 20 mm retract sets the stall bit
        ## transiently and clears it. Tripping on one sample would fire on
        ## every normal move.
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
        ## Already tripped - must not keep re-firing.
        self.assertFalse(t.update(status(stall=0b10)))

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
        t = L.MotionTracker(2, required=1)
        self.assertFalse(t.update(status(stall=0b1000)))

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
