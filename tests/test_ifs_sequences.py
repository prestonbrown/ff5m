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

    def test_filament_beats_a_jam_on_the_same_poll(self):
        ## Filament stopping because it arrived at the sensor is success.
        waiter = SEQ.StateWaiter(CH, S.LOADING, expect_filament=True,
                                 confirmations=1)
        outcome = feed(waiter, stuck(LOADING, silk=0b10))
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


MULTICOLOUR = {
    "FristESpace": 100, "FristESpeed": 300, "FristFanSpeed": 0,
    "SecondESpace": 30, "SecondESpeed": 300, "SecondFanSpeed": 255,
    "UnloadESpace": 60, "UnloadIFSSpace": 70, "UnloadSpeed": 600,
}


def kinds(plan):
    return [step.kind for step in plan]


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


class TestLoadPlan(unittest.TestCase):
    def setUp(self):
        self.params = SEQ.Parameters.from_multicolour(MULTICOLOUR)
        self.plan = SEQ.load_plan(CH, self.params, temperature=220)

    def test_the_ifs_feeds_the_whole_way_by_itself(self):
        ## Regression: an earlier draft had the extruder pulling the filament
        ## past its own gear mid-feed. That was invented - zmod issues one F10
        ## for the whole tube and the extruder does not run until the purge.
        feed_at = kinds(self.plan).index(SEQ.FEED)
        first_extrude = kinds(self.plan).index(SEQ.EXTRUDE)
        purge_at = kinds(self.plan).index(SEQ.STOP)
        self.assertGreater(first_extrude, purge_at,
                           "the extruder must not run before the purge")
        self.assertLess(feed_at, purge_at)

    def test_the_feed_ends_on_the_toolhead_sensor(self):
        ## Distance is an upper bound, not the target: a part-fed lane must not
        ## overshoot.
        feed = self.plan[kinds(self.plan).index(SEQ.FEED)]
        self.assertEqual(feed.until, SEQ.UNTIL_TOOLHEAD_FILAMENT)
        self.assertEqual(feed.distance, self.params.tube_mm)
        self.assertEqual(feed.expect, S.LOADING)

    def test_it_clamps_before_feeding_and_releases_after(self):
        order = kinds(self.plan)
        self.assertLess(order.index(SEQ.CLAMP), order.index(SEQ.FEED))
        self.assertEqual(order[-1], SEQ.RELEASE)

    def test_it_heats_first_because_klipper_will_not(self):
        ## min_extrude_temp is 0 on this printer.
        self.assertEqual(self.plan[0].kind, SEQ.HEAT)
        self.assertEqual(self.plan[0].value, 220)

    def test_no_temperature_means_no_heat_step(self):
        plan = SEQ.load_plan(CH, self.params)
        self.assertNotIn(SEQ.HEAT, kinds(plan))

    def test_the_purge_uses_the_printers_own_distances(self):
        extrudes = [s for s in self.plan if s.kind == SEQ.EXTRUDE]
        self.assertEqual([s.distance for s in extrudes], [100.0, 30.0])

    def test_the_fan_ends_off(self):
        fans = [s for s in self.plan if s.kind == SEQ.FAN]
        self.assertEqual(fans[-1].value, 0.0)

    def test_every_step_names_its_channel_where_it_matters(self):
        for step in self.plan:
            if step.kind in (SEQ.CLAMP, SEQ.RELEASE, SEQ.FEED, SEQ.RETRACT):
                self.assertEqual(step.channel, CH, repr(step))


class TestUnloadPlan(unittest.TestCase):
    def setUp(self):
        self.params = SEQ.Parameters.from_multicolour(MULTICOLOUR)
        self.plan = SEQ.unload_plan(CH, self.params, temperature=220)

    def test_the_extruder_pulls_it_off_the_sensor_first(self):
        order = kinds(self.plan)
        self.assertLess(order.index(SEQ.EXTRUDE), order.index(SEQ.RETRACT))

    def test_the_extruder_move_is_backwards(self):
        extrude = self.plan[kinds(self.plan).index(SEQ.EXTRUDE)]
        self.assertLess(extrude.distance, 0)
        self.assertEqual(extrude.distance, -self.params.unload_extruder_mm)

    def test_the_ifs_retract_ends_when_the_sensor_clears(self):
        retract = self.plan[kinds(self.plan).index(SEQ.RETRACT)]
        self.assertEqual(retract.until, SEQ.UNTIL_TOOLHEAD_CLEAR)
        self.assertEqual(retract.expect, S.UNLOADING)

    def test_it_heats_because_cold_filament_snaps(self):
        self.assertEqual(self.plan[0].kind, SEQ.HEAT)

    def test_it_releases_last(self):
        self.assertEqual(kinds(self.plan)[-1], SEQ.RELEASE)
