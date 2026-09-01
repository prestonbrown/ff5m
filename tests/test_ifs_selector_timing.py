## Tests for the selector timing probe.
##
## This tool produces the number that decides whether the step rate derived in
## docs/AD5X_IFS_PROTOCOL.md is right, so the decode and the arrival rule are
## worth holding still. Everything here runs off-rig: the serial port is never
## opened, and `send` is replaced with a script.
##
## The tool deliberately re-implements ifs_diagnostics.read_driver_motion,
## because it is scp'd to a printer on its own and cannot import the plugin
## tree. These tests are what keeps the copy honest - they assert the same two
## rules test_ifs_diagnostics.py asserts for the original.
##
## Copyright (C) 2026, Preston Brown
##
## This file may be distributed under the terms of the GNU GPLv3 license

import contextlib
import importlib.util
import io
import pathlib
import sys
import time
import unittest


TOOL = (pathlib.Path(__file__).parents[1] / "tools" / "ifs"
        / "ifs_selector_timing.py")


def load_tool():
    """Import the tool by path. It imports pyserial only inside main()."""
    spec = importlib.util.spec_from_file_location("ifs_selector_timing", TOOL)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


T = load_tool()


class Script(object):
    """Stands in for `send`, answering F63 from a list."""

    def __init__(self, answers):
        self.answers = list(answers)
        self.asked = []

    def __call__(self, ser, command):
        self.asked.append(command)
        if not self.answers:
            return ""
        return self.answers.pop(0)


class ToolTest(unittest.TestCase):
    def setUp(self):
        self._send = T.send
        self._interval = T.POLL_INTERVAL
        ## The rule under test is "three consecutive", not how long it waits.
        T.POLL_INTERVAL = 0.0
        ## The tool reports progress to a human on a printer; here it would
        ## just interleave with the test runner's own output.
        self._quiet = contextlib.redirect_stdout(io.StringIO())
        self._quiet.__enter__()

    def tearDown(self):
        self._quiet.__exit__(None, None, None)
        T.send = self._send
        T.POLL_INTERVAL = self._interval

    def script(self, answers):
        T.send = Script(answers)
        return T.send


class TestStandstillDecode(ToolTest):
    def test_the_standstill_bit_means_stopped(self):
        ## 80000000 is bit 31, stst - what the board reports at idle.
        self.script(["DRV_STATUS: 80000000"])
        self.assertIs(T.selector_moving(None), False)

    def test_a_clear_standstill_bit_means_moving(self):
        ## 00090000 is what the selector read while turning: stst gone,
        ## CS_ACTUAL 9 in bits 16-20.
        self.script(["DRV_STATUS: 00090000"])
        self.assertIs(T.selector_moving(None), True)

    def test_a_silent_driver_is_unknown_not_stopped(self):
        for answer in ("", "F63 ok.", "nonsense"):
            self.script([answer])
            self.assertIsNone(T.selector_moving(None))

    def test_it_asks_the_selector_bank(self):
        ## F53 is the feeder's DRV_STATUS. Reading the wrong bank would time
        ## every move as instant, because the feeder never moves during a jog.
        send = self.script(["DRV_STATUS: 80000000"])
        T.selector_moving(None)
        self.assertEqual(send.asked, ["F63"])


class TestArrival(ToolTest):
    def test_one_standstill_sample_is_not_arrival(self):
        ## The bit toggles as the motor steps; a single clear sample is noise.
        self.script(["DRV_STATUS: 80000000",
                     "DRV_STATUS: 00090000"] * 20)
        self.assertIsNone(
            T.wait_for_standstill(None, time.time() + 0.3))

    def test_three_consecutive_samples_are_arrival(self):
        self.script(["DRV_STATUS: 00090000"]
                    + ["DRV_STATUS: 80000000"] * T.STANDSTILL_SAMPLES)
        self.assertIsNotNone(
            T.wait_for_standstill(None, time.time() + 5.0))

    def test_silence_does_not_count_towards_arrival(self):
        ## Two standstills, a non-answer, then two more. If silence counted,
        ## this would confirm; it must not.
        self.script(["DRV_STATUS: 80000000", "DRV_STATUS: 80000000",
                     "", "DRV_STATUS: 80000000", "DRV_STATUS: 80000000"])
        self.assertIsNone(
            T.wait_for_standstill(None, time.time() + 0.3))

    def test_a_motor_that_never_stops_times_out(self):
        self.script(["DRV_STATUS: 00090000"] * 500)
        self.assertIsNone(
            T.wait_for_standstill(None, time.time() + 0.2))


class TestJog(ToolTest):
    def test_an_unexpected_reply_is_not_timed(self):
        ## Nothing refuses F30 in the firmware, so an unexpected answer means a
        ## desynced stream. Timing it anyway would report a fabricated number.
        self.script(["FFS not ready."])
        self.assertIsNone(T.jog(None, 4096, "test"))

    def test_a_good_jog_is_timed(self):
        self.script(["F30 ok."]
                    + ["DRV_STATUS: 80000000"] * T.STANDSTILL_SAMPLES)
        self.assertIsNotNone(T.jog(None, 4096, "test"))


class TestGeometry(unittest.TestCase):
    def test_the_turret_constants_match_the_operations_layer(self):
        ## The tool cannot import the plugin, so the constants are duplicated.
        ## This is the assertion that catches them drifting apart.
        import ifs_modules
        ops = ifs_modules.load("ifs_operations")
        self.assertEqual(T.SLOT_PITCH, ops.SELECTOR_SLOT_PITCH)
        self.assertEqual(T.STEPS_PER_TURN, ops.SELECTOR_STEPS_PER_TURN)

    def test_the_derived_rate_is_the_one_the_docs_state(self):
        ## TIM6 counts at 1 MHz with ARR 155, so it fires every 156 counts, and
        ## one STEP edge per interrupt makes a step every two. 3205 steps/s.
        self.assertAlmostEqual(T.DERIVED_STEPS_PER_SEC, 3205.13, places=1)


if __name__ == "__main__":
    unittest.main()
