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


## The board's `stall_state` field reports MOTION - bit set means that
## channel's filament is moving. So a healthy in-progress move has the bit SET,
## and its sustained absence is the jam. Defaulting to "moving" keeps every
## test that is not about jams reading naturally.
MOVING = 1 << (CH - 1)


def status(state=S.READY, silk=0, motion=MOVING):
    return S.IfsStatus(state=state, silk_mask=silk, active_channel=CH,
                       insert_mask=0, stall_mask=motion)


def stuck(state, silk=0):
    """A commanded move with no filament motion - the jam."""
    return status(state, silk=silk, motion=0)


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

    def test_ready_finishes_even_if_the_activity_was_never_seen(self):
        ## zmod's wait_for_state:  if state == FFS_STATUS_READY: return True
        ## - no precondition that the activity was observed first. It polls
        ## every 0.2s and still does not require it; this reads the poller's
        ## 1s snapshots, so requiring it meant a missed transition hung until
        ## the timeout and then blamed the board for never reaching the state.
        ## Both hardware failures - clamp and feed - were this.
        waiter = SEQ.StateWaiter(CH, S.LOADING)
        self.assertEqual(waiter.update(status()).kind, SEQ.FINISHED)

    def test_the_pre_command_ready_race_is_not_the_waiters_job(self):
        ## Reading a READY captured before the command landed would end a move
        ## instantly. That is guarded upstream in _await, which only feeds the
        ## waiter a status newer than the one it started with, so the waiter
        ## stays a pure function of the readings it is given.
        waiter = SEQ.StateWaiter(CH, S.LOADING)
        outcome = feed(waiter, status(LOADING), status(LOADING), status())
        self.assertEqual(outcome.kind, SEQ.FINISHED)

    def test_without_an_activity_ready_finishes_immediately(self):
        waiter = SEQ.StateWaiter(CH)
        self.assertEqual(waiter.update(status()).kind, SEQ.FINISHED)


class TestRunoutCondition(unittest.TestCase):
    """zmod's silk check: the LANE losing its filament aborts the move.

    It passes silk={'count': silk_count, 'status': False} on both its checked
    feed and its checked retract, and the answer is RET_SILK, "No filament N in
    IFS" - a failure, not an arrival. Modelling it as a success condition meant
    an empty lane read as a completed load.
    """

    def test_an_empty_lane_aborts_a_load(self):
        waiter = SEQ.StateWaiter(CH, S.LOADING, watch_runout=True,
                                 runout_confirmations=1)
        outcome = feed(waiter, status(LOADING, silk=0))
        self.assertEqual(outcome.kind, SEQ.RUNOUT)
        self.assertTrue(outcome.is_problem)

    def test_an_empty_lane_aborts_an_unload_too(self):
        waiter = SEQ.StateWaiter(CH, S.UNLOADING, watch_runout=True,
                                 runout_confirmations=1)
        outcome = feed(waiter, status(UNLOADING, silk=0))
        self.assertEqual(outcome.kind, SEQ.RUNOUT)

    def test_a_lane_with_filament_keeps_going(self):
        waiter = SEQ.StateWaiter(CH, S.LOADING, watch_runout=True,
                                 runout_confirmations=1)
        outcome = feed(waiter, status(LOADING, silk=0b10),
                       status(LOADING, silk=0b10))
        self.assertEqual(outcome.kind, SEQ.WAITING)

    def test_it_takes_consecutive_confirmations(self):
        waiter = SEQ.StateWaiter(CH, S.LOADING, watch_runout=True,
                                 runout_confirmations=3)
        loaded = status(LOADING, silk=0b10)
        outcome = feed(waiter, status(LOADING, silk=0), loaded,
                       status(LOADING, silk=0), status(LOADING, silk=0))
        self.assertEqual(outcome.kind, SEQ.WAITING)

    def test_the_condition_is_ignored_outside_the_activity(self):
        ## While the board is clamping rather than loading, the filament bit is
        ## describing something we did not ask about.
        waiter = SEQ.StateWaiter(CH, S.LOADING, watch_runout=True,
                                 runout_confirmations=1)
        clamped = status(S.state_value(S.CLAMPED, CH), silk=0)
        outcome = feed(waiter, clamped, clamped, clamped)
        self.assertEqual(outcome.kind, SEQ.WAITING)

    def test_another_channels_empty_lane_does_not_count(self):
        ## CH is 2, so silk 0b10 is CH's own bit set and every other clear.
        waiter = SEQ.StateWaiter(CH, S.LOADING, watch_runout=True,
                                 runout_confirmations=1)
        outcome = feed(waiter, status(LOADING, silk=0b10))
        self.assertEqual(outcome.kind, SEQ.WAITING)

    def test_runout_is_off_unless_asked_for(self):
        ## zmod only passes silk on its CHECK=1 calls; an unchecked move waits
        ## for READY and judges nothing.
        waiter = SEQ.StateWaiter(CH, S.LOADING, watch_stall=False)
        outcome = feed(waiter, status(LOADING, silk=0),
                       status(LOADING, silk=0))
        self.assertEqual(outcome.kind, SEQ.WAITING)


class TestStall(unittest.TestCase):
    """A jam is motion ABSENT during a move, not a bit coming on.

    Measured with an empty channel as the control: the board sets the bit while
    filament moves and clears it when it stops. zmod's wait agrees - it
    declares a jam when the bit reads clear for several polls.
    """

    def test_sustained_stillness_stops_the_move(self):
        waiter = SEQ.StateWaiter(CH, S.LOADING, confirmations=3)
        outcome = feed(waiter, stuck(LOADING), stuck(LOADING), stuck(LOADING))
        self.assertEqual(outcome.kind, SEQ.STALLED)
        self.assertTrue(outcome.is_problem)

    def test_a_lane_that_never_moved_says_so(self):
        """The first question anyone asks of a stall, answered in the message.

        A lane bound at the IFS itself never starts; one that hit something
        ahead of it stops part-way. Those are different repairs, and working
        the difference out by hand cost a night of reading the motion bit off
        the board a poll at a time.
        """
        waiter = SEQ.StateWaiter(CH, S.LOADING, confirmations=3)
        outcome = feed(waiter, stuck(LOADING), stuck(LOADING), stuck(LOADING))
        self.assertEqual(outcome.kind, SEQ.STALLED)
        self.assertIn("never started moving", outcome.detail)

    def test_a_lane_that_moved_first_says_that_instead(self):
        waiter = SEQ.StateWaiter(CH, S.LOADING, confirmations=3)
        outcome = feed(waiter, status(LOADING), stuck(LOADING),
                       stuck(LOADING), stuck(LOADING))
        self.assertEqual(outcome.kind, SEQ.STALLED)
        self.assertIn("stopped moving", outcome.detail)
        self.assertNotIn("never started", outcome.detail)

    def test_healthy_motion_is_never_a_jam(self):
        ## The inversion that would have broken every normal load.
        waiter = SEQ.StateWaiter(CH, S.LOADING, confirmations=2)
        outcome = feed(waiter, status(LOADING), status(LOADING),
                       status(LOADING), status(LOADING))
        self.assertEqual(outcome.kind, SEQ.WAITING)

    def test_a_brief_pause_is_not_a_jam(self):
        waiter = SEQ.StateWaiter(CH, S.LOADING, confirmations=3)
        outcome = feed(waiter, stuck(LOADING), stuck(LOADING),
                       status(LOADING), stuck(LOADING))
        self.assertEqual(outcome.kind, SEQ.WAITING)

    def test_stall_watching_can_be_turned_off(self):
        waiter = SEQ.StateWaiter(CH, S.LOADING, watch_stall=False,
                                 confirmations=1)
        outcome = feed(waiter, stuck(LOADING), stuck(LOADING))
        self.assertEqual(outcome.kind, SEQ.WAITING)

    def test_an_empty_lane_beats_a_jam_on_the_same_poll(self):
        ## Both are true at once when a spool runs out - the filament stops
        ## moving because there is none left. zmod checks silk first, and it is
        ## the more useful answer: "no filament" tells you what to do next,
        ## "stalled" sends you looking for a jam that is not there.
        waiter = SEQ.StateWaiter(CH, S.LOADING, watch_runout=True,
                                 confirmations=1, runout_confirmations=1)
        outcome = feed(waiter, stuck(LOADING, silk=0))
        self.assertEqual(outcome.kind, SEQ.RUNOUT)


class TestFaults(unittest.TestCase):
    def test_a_driver_fault_stops_everything(self):
        waiter = SEQ.StateWaiter(CH, S.LOADING, watch_runout=True)
        outcome = waiter.update(status(S.DRIVER_ERROR))
        self.assertEqual(outcome.kind, SEQ.DRIVER_ERROR)
        self.assertTrue(outcome.is_problem)
        self.assertIn("F15", outcome.detail)

    def test_a_timeout_says_what_actually_ran_out(self):
        """The wording sent one investigation down a hole.

        What times out is the wait for the board to come back to READY. The
        activity is only the window conditions are judged in, and it can be
        entered and left between two polls, so "never reached loading" sent
        somebody looking for a transition that had already happened.
        """
        outcome = SEQ.StateWaiter(CH, S.LOADING).timed_out(status())
        self.assertEqual(outcome.kind, SEQ.TIMED_OUT)
        self.assertTrue(outcome.is_problem)
        self.assertIn("came back to ready", outcome.detail)
        self.assertNotIn("never reached", outcome.detail)

    def test_a_timeout_with_no_activity_still_reads(self):
        ## A settle wait has no activity to name.
        outcome = SEQ.StateWaiter(CH).timed_out(status())
        self.assertIn("came back to ready", outcome.detail)

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


MULTICOLOUR = {
    "FristESpace": 100, "FristESpeed": 300, "FristFanSpeed": 0,
    "SecondESpace": 30, "SecondESpeed": 300, "SecondFanSpeed": 255,
    "UnloadESpace": 60, "UnloadIFSSpace": 70, "UnloadSpeed": 600,
}


class TestParameters(unittest.TestCase):
    def test_it_reads_the_printers_own_numbers(self):
        ## zmod's "defaults" are these values copied out; read the source.
        params = SEQ.Parameters.from_multicolour(MULTICOLOUR)
        self.assertEqual(params.first_purge_mm, 100.0)
        self.assertEqual(params.unload_ifs_mm, 70.0)
        self.assertEqual(params.unload_speed, 600.0)
        self.assertEqual(params.second_fan, 255.0)

    def test_missing_keys_fall_back_to_the_defaults(self):
        params = SEQ.Parameters.from_multicolour({"UnloadESpace": 42})
        self.assertEqual(params.unload_extruder_mm, 42.0)
        self.assertEqual(params.first_purge_mm,
                         SEQ.Parameters.DEFAULTS["first_purge_mm"])

    def test_a_junk_value_does_not_poison_a_parameter(self):
        params = SEQ.Parameters.from_multicolour({"UnloadESpace": "sixty"})
        self.assertEqual(params.unload_extruder_mm,
                         SEQ.Parameters.DEFAULTS["unload_extruder_mm"])

    def test_an_unknown_parameter_is_refused(self):
        with self.assertRaises(TypeError):
            SEQ.Parameters(nonsense=1)


class TestParametersExport(unittest.TestCase):
    def test_as_dict_carries_everything_a_macro_needs(self):
        values = SEQ.Parameters.from_multicolour(MULTICOLOUR).as_dict()
        self.assertEqual(values["first_purge_mm"], 100.0)
        self.assertEqual(values["unload_ifs_mm"], 70.0)
        self.assertIn("tube_mm", values)
        self.assertIn("ifs_speed", values)

    def test_every_value_is_a_number(self):
        ## These land in Jinja, where a stray string is a runtime surprise.
        for value in SEQ.Parameters.from_multicolour(MULTICOLOUR).as_dict().values():
            self.assertIsInstance(value, float)


if __name__ == "__main__":
    unittest.main()
