## Tests for the IFS state waiter.
##
## State values here are the ones confirmed on hardware: 22 is loading on
## channel 2, 26 unloading, 18 clamped, 5 ready, 127 a driver fault.
##
## Copyright (C) 2026, Preston Brown
##
## This file may be distributed under the terms of the GNU GPLv3 license

import unittest

import ifs_modules

SEQ = ifs_modules.load("ifs_sequences")
S = ifs_modules.load("ifs_status")

CH = 2
LOADING = S.state_value(S.LOADING, CH)      # 22, confirmed on the rig
UNLOADING = S.state_value(S.UNLOADING, CH)  # 26


def status(state=S.READY, silk=0, stall=0):
    return S.IfsStatus(state=state, silk_mask=silk, active_channel=CH,
                       insert_mask=0, stall_mask=stall)


def feed(waiter, *statuses):
    """Run a sequence of polls, returning the outcome that stopped it."""
    outcome = None
    for value in statuses:
        outcome = waiter.update(value)
        if outcome.kind != SEQ.WAITING:
            return outcome
    return outcome


class TestExpectedState(unittest.TestCase):
    def test_it_knows_the_wire_value_for_its_move(self):
        ## 22 = loading on channel 2, observed on hardware.
        self.assertEqual(
            SEQ.StateWaiter(CH, S.LOADING).expected_state, 22)
        self.assertEqual(
            SEQ.StateWaiter(CH, S.UNLOADING).expected_state, 26)

    def test_no_activity_means_no_expected_state(self):
        self.assertIsNone(SEQ.StateWaiter(CH).expected_state)


class TestFinishing(unittest.TestCase):
    def test_returning_to_ready_after_the_move_is_a_finish(self):
        waiter = SEQ.StateWaiter(CH, S.LOADING)
        outcome = feed(waiter, status(LOADING), status(LOADING), status())
        self.assertEqual(outcome.kind, SEQ.FINISHED)

    def test_ready_before_the_move_starts_is_not_a_finish(self):
        ## The board answers the command and only then starts working. Calling
        ## that first ready poll a finish would end every move instantly.
        waiter = SEQ.StateWaiter(CH, S.LOADING)
        self.assertEqual(waiter.update(status()).kind, SEQ.WAITING)
        self.assertEqual(waiter.update(status()).kind, SEQ.WAITING)
        outcome = feed(waiter, status(LOADING), status())
        self.assertEqual(outcome.kind, SEQ.FINISHED)

    def test_without_an_activity_ready_finishes_immediately(self):
        waiter = SEQ.StateWaiter(CH)
        self.assertEqual(waiter.update(status()).kind, SEQ.FINISHED)


class TestFilamentCondition(unittest.TestCase):
    def test_a_load_stops_when_filament_arrives(self):
        waiter = SEQ.StateWaiter(CH, S.LOADING, expect_filament=True,
                                 confirmations=3)
        loaded = status(LOADING, silk=0b10)
        outcome = feed(waiter, loaded, loaded, loaded)
        self.assertEqual(outcome.kind, SEQ.FILAMENT)
        self.assertIn("present", outcome.detail)

    def test_an_unload_stops_when_filament_leaves(self):
        waiter = SEQ.StateWaiter(CH, S.UNLOADING, expect_filament=False,
                                 confirmations=2)
        empty = status(UNLOADING, silk=0)
        outcome = feed(waiter, empty, empty)
        self.assertEqual(outcome.kind, SEQ.FILAMENT)
        self.assertIn("gone", outcome.detail)

    def test_it_takes_consecutive_confirmations(self):
        waiter = SEQ.StateWaiter(CH, S.LOADING, expect_filament=True,
                                 confirmations=3)
        loaded = status(LOADING, silk=0b10)
        outcome = feed(waiter, loaded, loaded, status(LOADING, silk=0),
                       loaded, loaded)
        self.assertEqual(outcome.kind, SEQ.WAITING)

    def test_the_condition_is_ignored_outside_the_activity(self):
        ## While the board is clamping rather than loading, the filament bit is
        ## describing something we did not ask about.
        waiter = SEQ.StateWaiter(CH, S.LOADING, expect_filament=True,
                                 confirmations=2)
        clamped = status(S.state_value(S.CLAMPED, CH), silk=0b10)
        outcome = feed(waiter, clamped, clamped, clamped)
        self.assertEqual(outcome.kind, SEQ.WAITING)

    def test_another_channels_filament_does_not_count(self):
        waiter = SEQ.StateWaiter(CH, S.LOADING, expect_filament=True,
                                 confirmations=1)
        outcome = feed(waiter, status(LOADING, silk=0b1000))
        self.assertEqual(outcome.kind, SEQ.WAITING)


class TestStall(unittest.TestCase):
    def test_a_sustained_stall_stops_the_move(self):
        waiter = SEQ.StateWaiter(CH, S.LOADING, confirmations=3)
        stalled = status(LOADING, stall=0b10)
        outcome = feed(waiter, stalled, stalled, stalled)
        self.assertEqual(outcome.kind, SEQ.STALLED)
        self.assertTrue(outcome.is_problem)

    def test_a_passing_stall_does_not(self):
        ## Measured: a clean 20 mm retract sets the stall bit in passing.
        waiter = SEQ.StateWaiter(CH, S.LOADING, confirmations=3)
        outcome = feed(waiter, status(LOADING, stall=0b10),
                       status(LOADING, stall=0), status(LOADING, stall=0b10))
        self.assertEqual(outcome.kind, SEQ.WAITING)

    def test_stall_watching_can_be_turned_off(self):
        waiter = SEQ.StateWaiter(CH, S.LOADING, watch_stall=False,
                                 confirmations=1)
        outcome = feed(waiter, status(LOADING, stall=0b10))
        self.assertEqual(outcome.kind, SEQ.WAITING)

    def test_filament_beats_a_stall_on_the_same_poll(self):
        ## Reaching the sensor is the goal; the filament stopping because it
        ## got there is success, not a fault.
        waiter = SEQ.StateWaiter(CH, S.LOADING, expect_filament=True,
                                 confirmations=1)
        outcome = feed(waiter, status(LOADING, silk=0b10, stall=0b10))
        self.assertEqual(outcome.kind, SEQ.FILAMENT)


class TestFaults(unittest.TestCase):
    def test_a_driver_fault_stops_everything(self):
        waiter = SEQ.StateWaiter(CH, S.LOADING, expect_filament=True)
        outcome = waiter.update(status(S.DRIVER_ERROR))
        self.assertEqual(outcome.kind, SEQ.DRIVER_ERROR)
        self.assertTrue(outcome.is_problem)
        self.assertIn("F15", outcome.detail)

    def test_a_missing_reading_is_not_a_fault(self):
        ## A dropped poll must not abort a load.
        outcome = SEQ.StateWaiter(CH, S.LOADING).update(None)
        self.assertEqual(outcome.kind, SEQ.WAITING)
        self.assertFalse(outcome.is_problem)

    def test_timed_out_is_a_problem(self):
        outcome = SEQ.StateWaiter(CH, S.LOADING).timed_out()
        self.assertEqual(outcome.kind, SEQ.TIMED_OUT)
        self.assertTrue(outcome.is_problem)

    def test_finishing_is_not_a_problem(self):
        self.assertFalse(SEQ.Outcome(SEQ.FINISHED).is_problem)
        self.assertFalse(SEQ.Outcome(SEQ.FILAMENT).is_problem)

    def test_confirmations_must_be_sane(self):
        with self.assertRaises(ValueError):
            SEQ.StateWaiter(CH, S.LOADING, confirmations=0)


if __name__ == "__main__":
    unittest.main()
