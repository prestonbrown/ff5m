## Behavioral tests for the AD5X IFS motion macros.
##
## Copyright (C) 2026, Preston Brown
##
## This file may be distributed under the terms of the GNU GPLv3 license
##
## These guard two things that break hardware rather than tests:
##   - X must never travel across the machine while Y is behind safe_y, and a
##     combined `G1 X.. Y..` interpolates diagonally into the back wall.
##   - the pre-travel Z move is a *lift* (ABSOLUTE=0), not a coordinate. As an
##     absolute it drives the bed up toward the nozzle instead of away.

import pathlib
import re
import unittest

from tests.gcode_macro_harness import (MacroActionError, load_macro,
                                       render_macro)


ROOT = pathlib.Path(__file__).parents[1]
HW = ROOT / "macros" / "hw_base.ad5x.cfg"

GEOMETRY = load_macro(HW, "_IFS_GEOMETRY").variables

## A combined XY move in one G1. Whitespace-tolerant, word-boundary anchored so
## `G1 X52.5 F12000` does not match.
COMBINED_XY = re.compile(r"^G1\b(?=[^;]*\bX[-\d.]+)(?=[^;]*\bY[-\d.]+)")


def at(x, y, z=220.0, homed="xyz"):
    return {
        "toolhead": {"homed_axes": homed},
        "gcode_move": {"gcode_position": {"x": x, "y": y, "z": z}},
        "gcode_macro _IFS_GEOMETRY": GEOMETRY,
    }


def station(target, **start):
    """Render _IFS_GOTO_STATION heading for `target` from a given start pose."""
    return render_macro(HW, "_IFS_GOTO_STATION",
                        printer=at(**start), params={"X": target}).commands


def index_of(commands, pattern):
    for i, command in enumerate(commands):
        if re.match(pattern, command):
            return i
    raise AssertionError("no command matching %r in %r" % (pattern, commands))


class GotoStationTest(unittest.TestCase):
    def test_never_combines_x_and_y_in_one_move(self):
        ## The back wall. Every reachable start state, not just the easy one.
        starts = [(0.0, 0.0), (225.0, 0.0), (52.5, 232.0),
                  (200.0, 229.0), (52.5, 100.0), (110.0, 221.0)]
        for x, y in starts:
            for target in (GEOMETRY["chute_x"], GEOMETRY["wipe_x_hi"]):
                for command in station(target, x=x, y=y):
                    self.assertIsNone(
                        COMBINED_XY.match(command),
                        "diagonal move %r from (%s, %s)" % (command, x, y))

    def test_retreats_in_y_before_moving_x_when_behind_safe_y(self):
        commands = station(GEOMETRY["chute_x"], x=200.0, y=229.0)
        retreat = index_of(commands, r"_IFS_LEAVE_PURGE\b")
        move_x = index_of(commands, r"G1 X")
        approach = index_of(commands, r"G1 Y%s\b" % GEOMETRY["station_y"])
        self.assertLess(retreat, move_x, commands)
        self.assertLess(move_x, approach, commands)

    def test_no_retreat_needed_when_already_clear_of_the_back(self):
        commands = station(GEOMETRY["chute_x"], x=200.0, y=100.0)
        self.assertNotIn("_IFS_LEAVE_PURGE", commands, commands)
        index_of(commands, r"G1 X")  # but X still moves

    def test_x_is_not_moved_at_all_when_already_on_station(self):
        commands = station(GEOMETRY["chute_x"], x=GEOMETRY["chute_x"], y=229.0)
        self.assertFalse([c for c in commands if re.match(r"G1 X", c)], commands)
        ## and with no X move there is no reason to come forward first
        self.assertNotIn("_IFS_LEAVE_PURGE", commands, commands)

    def test_the_station_never_lifts(self):
        ## zmod's _GOTO_TRASH_STANDARD has no Z move; the one lift of a change
        ## happens at IFS_SELECT entry (see ChangeLiftTest). Arriving at the
        ## back edge LOW is what keeps the Y entry below the wall hardware.
        for start in ((0.0, 0.0), (200.0, 100.0), (200.0, 229.0)):
            commands = station(GEOMETRY["chute_x"], x=start[0], y=start[1])
            self.assertFalse(
                [c for c in commands if "Z" in c], (start, commands))

    def test_homes_only_when_unhomed(self):
        self.assertIn("G28", station(GEOMETRY["chute_x"], x=0.0, y=0.0, homed=""))
        self.assertNotIn("G28", station(GEOMETRY["chute_x"], x=0.0, y=0.0))

    def test_approaches_the_station_slowly(self):
        commands = station(GEOMETRY["chute_x"], x=0.0, y=0.0)
        approach = commands[index_of(commands, r"G1 Y%s\b" % GEOMETRY["station_y"])]
        self.assertIn("F3000", approach)


class AutoinsertTest(unittest.TestCase):
    """IFS_AUTOINSERT is zmod's cmd_IFS_AUTOINSERT, the step we never had.

    It runs the moment the board reports filament pushed into a lane and it is
    what leaves that lane in a KNOWN position. Skipping it means every lane
    sits wherever a human left it, and a later load feeds a guessed distance
    into a tube whose contents nobody knows.
    """

    PARAMS = {"tube_mm": 1000.0, "ifs_speed": 1200.0,
              "load_empty_mm": 600.0, "load_full_mm": 550.0,
              "autoinsert_ret_mm": 90.0, "hub_clear_mm": 300.0}

    def printer(self, occupied=False, loaded=(1, 2, 4), at_hub=0):
        return {
            "ifs": {"connected": True, "error": None,
                    "loaded_channels": list(loaded), "params": self.PARAMS},
            "extruder": {"target": 0.0},
            "filament_switch_sensor toolhead": {"filament_detected": occupied},
            "save_variables": {"variables": {"ifs_at_hub": at_hub}},
        }

    def render(self, channel=2, **kwargs):
        return render_macro(HW, "IFS_AUTOINSERT",
                            printer=self.printer(**kwargs),
                            params={"CHANNEL": channel}).commands

    def feeds(self, commands):
        return [c for c in commands if c.startswith("IFS_FEED")]

    def test_an_empty_extruder_draws_the_lane_to_the_toolhead(self):
        ## zmod's filament_autoinsert_empty_length, waiting on the extruder
        ## sensor: this is the branch that actually threads the tube.
        feed = self.feeds(self.render())
        self.assertEqual(len(feed), 1, feed)
        self.assertIn("LENGTH=600", feed[0])
        self.assertIn("SPEED=1200", feed[0])
        self.assertIn("UNTIL=toolhead", feed[0])

    def test_threading_claims_the_shared_path_before_feeding(self):
        ## Same rule as IFS_LOAD: a threading feed that stalls leaves the lane
        ## in the path, and it has to be on record for the next load to clear.
        commands = self.render()
        claim = index_of(commands, r"SAVE_VARIABLE VARIABLE=ifs_at_hub VALUE=2")
        self.assertLess(claim, index_of(commands, r"IFS_FEED CHANNEL=2"))

    def test_a_lane_left_at_its_entrance_claims_nothing(self):
        ## It never enters the path, so recording it as the holder would make
        ## the next load retract a lane that is already parked.
        commands = self.render(occupied=True)
        self.assertFalse([c for c in commands
                          if c.startswith("SAVE_VARIABLE VARIABLE=ifs_at_hub")],
                         commands)

    def test_an_occupied_extruder_leaves_the_lane_at_its_entrance(self):
        """Only one lane fits in the shared path, so the rest do not move.

        zmod feeds filament_autoinsert_full_length here and packs the hub. This
        is the failure that cost the evening: threading lane 4 parked it 90mm
        below the sensor, then lanes 2 and 1 were told to thread into it, and
        all three jammed. Lane 4's next load stalled after exactly 90mm.
        """
        commands = self.render(occupied=True)
        self.assertEqual(self.feeds(commands), [], commands)
        self.assertEqual([c for c in commands if c.startswith("IFS_CLAMP")],
                         [], commands)
        self.assertTrue(any(c.startswith("IFS_MARK_INSERTED")
                            for c in commands), commands)

    def test_a_lane_already_at_the_hub_also_blocks_threading(self):
        ## And this is the case the toolhead sensor cannot see: a lane threaded
        ## but not loaded sits 90mm SHORT of the sensor, so the sensor reads
        ## empty while the shared path is taken.
        commands = self.render(channel=2, occupied=False, at_hub=4)
        self.assertEqual(self.feeds(commands), [], commands)

    def test_the_lane_already_at_the_hub_may_still_re_thread_itself(self):
        ## It is the one lane that cannot collide with itself.
        commands = self.render(channel=2, occupied=False, at_hub=2)
        self.assertEqual(len(self.feeds(commands)), 1, commands)

    def test_threading_records_that_the_lane_owns_the_hub(self):
        saves = [c for c in self.render() if c.startswith("SAVE_VARIABLE")]
        self.assertEqual(len(saves), 1, saves)
        self.assertIn("VARIABLE=ifs_at_hub", saves[0])
        self.assertIn("VALUE=2", saves[0])

    def test_it_backs_off_the_gear_once_the_tip_arrives(self):
        """filament_autoinsert_ret_length, asked for THROUGH the feed.

        A klipper macro is rendered once, before any of it runs, so a separate
        IFS_RETRACT written after the feed cannot be conditional on whether the
        feed reached the sensor - that read already happened. BACKOFF puts the
        decision where the outcome is known.
        """
        feed = self.feeds(self.render())
        self.assertEqual(len(feed), 1, feed)
        self.assertIn("BACKOFF=90", feed[0])
        self.assertEqual([c for c in self.render()
                          if c.startswith("IFS_RETRACT")], [])

    def test_the_blocked_branch_moves_nothing_at_all(self):
        ## No feed, no retract, no clamp. The lane stays where the board's own
        ## insertion left it, which is the one position known to be safe.
        commands = self.render(occupied=True)
        for verb in ("IFS_FEED", "IFS_RETRACT", "IFS_CLAMP"):
            self.assertEqual([c for c in commands if c.startswith(verb)], [],
                             commands)

    def test_it_stops_the_board_after_the_feed(self):
        ## UNTIL=toolhead returns while the board is still feeding - the sensor
        ## ends OUR wait, not the board's move - so the board has to be told.
        commands = self.render()
        self.assertLess(index_of(commands, r"IFS_FEED\b"),
                        index_of(commands, r"IFS_STOP\b"))

    def test_it_marks_the_lane_inserted_and_lets_go(self):
        ## zmod ends with F23 then F39. Without the release the lane stays
        ## clamped, which is how two of them sat gripped for hours.
        commands = self.render()
        self.assertLess(index_of(commands, r"IFS_MARK_INSERTED\b"),
                        index_of(commands, r"IFS_RELEASE\b"))
        self.assertIn("CHANNEL=2", commands[index_of(commands,
                                                     r"IFS_RELEASE\b")])

    def test_it_clamps_before_it_feeds(self):
        commands = self.render()
        self.assertLess(index_of(commands, r"IFS_CLAMP\b"),
                        index_of(commands, r"IFS_FEED\b"))

    def test_an_empty_lane_is_refused_rather_than_fed(self):
        with self.assertRaises(Exception) as caught:
            self.render(channel=3)
        self.assertIn("no filament", str(caught.exception))


class LoadTest(unittest.TestCase):
    """IFS_LOAD renders, and renders the numbers it means to.

    check_macros.py parses templates but never renders them, so it cannot see a
    name that does not exist. IFS_LOAD referenced `p.load_empty_mm` without
    defining `p`, klipper raised UndefinedError, and the load failed before it
    even heated. The harness renders leniently - an undefined name becomes an
    empty string - so asserting the rendered *value* is what catches it.
    """

    def printer(self, loaded=False, connected=True, target=0.0, recorded=0):
        return {
            "ifs": {
                "connected": connected, "error": None,
                "loaded_channels": [1, 2, 4],
                "params": {"tube_mm": 1000.0, "ifs_speed": 1200.0,
                           "first_purge_mm": 100.0, "first_purge_speed": 300.0,
                           "first_fan": 0.0, "second_purge_mm": 30.0,
                           "second_purge_speed": 300.0, "second_fan": 255.0},
            },
            "extruder": {"target": target},
            "filament_switch_sensor toolhead": {"filament_detected": loaded},
            "save_variables": {"variables": {"ifs_loaded": recorded}},
        }

    def render(self, **kwargs):
        return render_macro(HW, "IFS_LOAD", printer=self.printer(**kwargs),
                            params={"SLOT": 1, "TEMP": 220}).commands

    def test_it_feeds_the_tube_length_at_ifs_speed(self):
        ## zmod's _INSERT_PRUTOK_IFS: LEN=filament_tube_length (1000, "the
        ## teflon tube from IFS to head") at filament_ifs_speed (1200). The
        ## bug this guards rendered "LENGTH=" with nothing after it.
        feed = [c for c in self.render() if c.startswith("IFS_FEED")]
        self.assertEqual(len(feed), 1, feed)
        self.assertIn("LENGTH=1000", feed[0])
        self.assertIn("SPEED=1200", feed[0])

    def test_the_feed_ends_on_the_toolhead_sensor(self):
        ## The length is a bound, not a target - without this a full tube of
        ## filament is pushed regardless of it arriving early.
        feed = [c for c in self.render() if c.startswith("IFS_FEED")]
        self.assertIn("UNTIL=toolhead", feed[0])

    def test_it_heats_before_parking(self):
        commands = self.render()
        self.assertLess(index_of(commands, r"M104 S220"),
                        index_of(commands, r"_IFS_PARK_FOR_PURGE"))

    def test_it_hands_the_slot_to_the_purge_and_marks_it_inserted(self):
        ## Without SLOT the purge cannot drive the lane, and the co-push is the
        ## whole point. IFS_MARK_INSERTED is zmod's F23, the last IFS step.
        commands = self.render()
        self.assertIn("_IFS_PURGE SLOT=1 EXTRA=0.0", commands)
        self.assertIn("IFS_MARK_INSERTED CHANNEL=1", commands)
        self.assertLess(index_of(commands, r"_IFS_PURGE"),
                        index_of(commands, r"IFS_MARK_INSERTED"))

    def test_it_clamps_before_feeding_and_stops_after(self):
        commands = self.render()
        assert_order = [index_of(commands, r"IFS_CLAMP"),
                        index_of(commands, r"IFS_FEED"),
                        index_of(commands, r"IFS_STOP")]
        self.assertEqual(assert_order, sorted(assert_order), commands)


class LoadedLaneTest(unittest.TestCase):
    """Which lane is in the nozzle has to survive a restart.

    The board cannot answer it: `ifs.active_channel` is the SELECTOR position,
    and it reads 0 after a power cycle with filament still loaded. Trusting it
    meant IFS_SELECT would load a second lane on top of the first. zmod reads
    the same fact out of FlashForge's config and writes it back with
    SET_CURRENT_PRUTOK; on Forge-X there is no stock config, so it lives in
    save_variables.

    These assert the rendered VALUE, not just that a command appeared: the
    harness renders leniently, so a name that does not exist becomes an empty
    string and "SLOT=" reads as a passing test.
    """

    PARAMS = {"tube_mm": 1000.0, "ifs_speed": 1200.0,
              "unload_extruder_mm": 60.0, "unload_ifs_mm": 70.0,
              "unload_speed": 600.0, "first_purge_mm": 100.0,
              "first_purge_speed": 300.0, "first_fan": 0.0,
              "second_purge_mm": 30.0, "second_purge_speed": 300.0,
              "second_fan": 255.0, "hub_clear_mm": 300.0}

    def printer(self, recorded=0, selector=0, occupied=False, at_hub=0,
                printing=True):
        return {
            "ifs": {"connected": True, "error": None,
                    "loaded_channels": [1, 2, 4],
                    "active_channel": selector, "params": self.PARAMS},
            "extruder": {"target": 220.0},
            "filament_switch_sensor toolhead": {"filament_detected": occupied},
            "save_variables": {"variables": {"ifs_loaded": recorded,
                                            "ifs_at_hub": at_hub}},
            "print_stats": {"state": printing and "printing" or "standby"},
            ## Always present on a real printer; a tool change has to know
            ## where the print was to put it back.
            "gcode_move": {"gcode_position": {"x": 100.0, "y": 90.0,
                                              "z": 5.0}},
            "fan_generic fanM106": {"speed": 0.6},
            "gcode_macro _IFS_GEOMETRY": GEOMETRY,
        }

    def render(self, macro, params, **kwargs):
        return render_macro(HW, macro, printer=self.printer(**kwargs),
                            params=params).commands

    def saved(self, commands):
        """{variable: value} for every SAVE_VARIABLE the macro emitted."""
        out = {}
        for c in commands:
            if not c.startswith("SAVE_VARIABLE"):
                continue
            bits = dict(w.split("=", 1) for w in c.split()[1:] if "=" in w)
            out[bits["VARIABLE"]] = bits["VALUE"]
        return out

    def test_a_load_records_the_lane_it_loaded(self):
        ## Both facts: what is in the nozzle, and who owns the shared path.
        commands = self.render("IFS_LOAD", {"SLOT": 2, "TEMP": 220})
        self.assertEqual(self.saved(commands),
                         {"ifs_loaded": "2", "ifs_at_hub": "2"})

    def test_the_shared_path_is_claimed_before_it_is_entered(self):
        """A feed that stalls part-way still leaves the lane in the path.

        Recorded only on success, ifs_at_hub reads 0 with a lane sitting in the
        tube, and the next load of a DIFFERENT lane feeds straight into it -
        the hub collision, reached by a different road. Claiming first is
        pessimistic and safe, and needs no recovery path of its own: the next
        load already retracts whoever holds the hub.
        """
        commands = self.render("IFS_LOAD", {"SLOT": 2, "TEMP": 220})
        claim = index_of(commands, r"SAVE_VARIABLE VARIABLE=ifs_at_hub VALUE=2")
        self.assertLess(claim, index_of(commands, r"IFS_FEED CHANNEL=2"))
        self.assertLess(claim, index_of(commands, r"IFS_CLAMP CHANNEL=2"))

    def test_the_nozzle_record_still_waits_for_success(self):
        ## "in the shared path" is true the moment we push; "in the nozzle" is
        ## not true until the whole load worked. They are different claims and
        ## must not be written at the same moment.
        commands = self.render("IFS_LOAD", {"SLOT": 2, "TEMP": 220})
        self.assertGreater(
            index_of(commands, r"SAVE_VARIABLE VARIABLE=ifs_loaded VALUE=2"),
            index_of(commands, r"_IFS_PURGE"))

    def test_an_unload_clears_both_records(self):
        ## The retract takes it out of the nozzle AND out of the shared path.
        commands = self.render("IFS_UNLOAD", {"SLOT": 2, "TEMP": 220},
                               recorded=2, occupied=True)
        self.assertEqual(self.saved(commands),
                         {"ifs_loaded": "0", "ifs_at_hub": "0"})

    def test_a_load_moves_a_parked_lane_out_of_the_shared_path(self):
        """The lane the toolhead sensor cannot see.

        A lane threaded but never loaded sits 90mm SHORT of the sensor, so the
        sensor reads empty while the hub is taken. Feeding into that jams both:
        measured, lane 1 stalled after exactly the 90mm lane 4 had backed off.
        """
        commands = self.render("IFS_LOAD", {"SLOT": 1, "TEMP": 220},
                               recorded=0, at_hub=4)
        back = [c for c in commands if c.startswith("IFS_RETRACT")]
        self.assertEqual(len(back), 1, commands)
        self.assertIn("CHANNEL=4", back[0])
        self.assertIn("LENGTH=300", back[0])
        self.assertLess(index_of(commands, r"IFS_RETRACT\b"),
                        index_of(commands, r"IFS_FEED\b"))

    def test_a_load_leaves_its_own_parked_lane_alone(self):
        ## Loading the lane that already holds the hub: nothing to move.
        commands = self.render("IFS_LOAD", {"SLOT": 4, "TEMP": 220},
                               recorded=0, at_hub=4)
        self.assertEqual([c for c in commands if c.startswith("IFS_RETRACT")],
                         [], commands)

    def test_the_load_feed_hands_a_stall_to_the_extruder(self):
        """The bug that cost a day, and it was never mechanical.

        A load feed ENDS by arriving at the extruder gear; the IFS cannot push
        filament past a gear that is not turning. Failing there aborted the
        load before _IFS_PURGE - the co-push - could finish it, and every
        symptom that produced (a lane that "jammed" only when feeding, only at
        the far end of its travel, and retracted perfectly) reads as broken
        hardware. zmod cannot fail here at all: print_result() only prints.
        """
        commands = self.render("IFS_LOAD", {"SLOT": 2, "TEMP": 220})
        feed = [c for c in commands if c.startswith("IFS_FEED")]
        self.assertEqual(len(feed), 1, commands)
        self.assertIn("SOFT=1", feed[0])

    def test_the_load_asks_the_toolhead_only_AFTER_the_purge(self):
        ## A soft feed cannot say whether the load worked - arriving at the gear
        ## and never arriving end it identically. Only the extruder can tell
        ## them apart, so the question has to come after it has had its turn,
        ## and before anything is recorded as loaded.
        commands = self.render("IFS_LOAD", {"SLOT": 2, "TEMP": 220})
        check = index_of(commands, r"IFS_REQUIRE_TOOLHEAD")
        self.assertGreater(check, index_of(commands, r"_IFS_PURGE"))
        self.assertLess(
            check,
            index_of(commands, r"SAVE_VARIABLE VARIABLE=ifs_loaded VALUE=2"))

    def test_a_thread_still_fails_loudly_on_a_stall(self):
        ## IFS_AUTOINSERT has nothing after it that could rescue a lane which
        ## never arrived, so there the stall IS the answer. SOFT belongs only
        ## where a co-push follows.
        commands = render_macro(
            HW, "IFS_AUTOINSERT", printer=self.printer(at_hub=0),
            params={"CHANNEL": 2}).commands
        feed = [c for c in commands if c.startswith("IFS_FEED")]
        self.assertEqual(len(feed), 1, commands)
        self.assertNotIn("SOFT", feed[0])

    def test_an_ordinary_swap_does_not_retract_the_outgoing_lane_twice(self):
        """The loaded lane and the lane at the hub are the same lane.

        IFS_UNLOAD already pulls it out of the shared path - 60mm through the
        extruder and 70mm more, and the extruder tip is only 150mm above the
        combiner. But a macro is rendered ONCE, before any of it runs, so the
        at_hub this block tests still holds what it held on entry: the unload's
        SAVE_VARIABLE ifs_at_hub=0 is invisible here. Retracting again cost
        hub_clear_mm on top, 430mm where 130 was needed, and the re-feed paid
        it back a second time. Every tool change, both directions.
        """
        commands = self.render("IFS_LOAD", {"SLOT": 1, "TEMP": 220},
                               recorded=4, at_hub=4, occupied=True)
        ## The unload is what moves lane 4, and it is the ONLY thing that does.
        self.assertEqual([c for c in commands if c.startswith("IFS_UNLOAD")],
                         ["IFS_UNLOAD SLOT=4 TEMP=220"], commands)
        self.assertEqual([c for c in commands if c.startswith("IFS_RETRACT")],
                         [], commands)
        self.assertNotIn("IFS: moving lane 4 out of the shared path",
                         " ".join(commands))

    def test_a_swap_still_clears_a_THIRD_lane_parked_at_the_hub(self):
        ## The narrow fix must stay narrow: lane 4 is loaded, lane 1 is parked
        ## in the shared path, and lane 2 is coming in. The unload only knows
        ## about lane 4, so lane 1 still has to be told to move.
        commands = self.render("IFS_LOAD", {"SLOT": 2, "TEMP": 220},
                               recorded=4, at_hub=1, occupied=True)
        back = [c for c in commands if c.startswith("IFS_RETRACT")]
        self.assertEqual(len(back), 1, commands)
        self.assertIn("CHANNEL=1", back[0])
        self.assertIn("LENGTH=300", back[0])

    def test_an_unload_hands_its_lane_to_the_extruder_withdraw(self):
        ## Without this the withdraw drags the strand back through a lane that
        ## is not driving. _IFS_CLEAR_EXTRUDER cannot work the lane out for
        ## itself: on IFS_LOAD's fallback path there genuinely is not one.
        commands = self.render("IFS_UNLOAD", recorded=2, params={"SLOT": 2,
                                                                 "TEMP": 220})
        clear = [c for c in commands if c.startswith("_IFS_CLEAR_EXTRUDER")]
        self.assertEqual(len(clear), 1, commands)
        self.assertIn("SLOT=2", clear[0])

    def test_a_load_with_no_lane_on_record_names_no_lane(self):
        commands = self.render("IFS_LOAD", recorded=0, params={"SLOT": 1,
                                                               "TEMP": 220})
        clear = [c for c in commands if c.startswith("_IFS_CLEAR_EXTRUDER")]
        self.assertEqual(len(clear), 1, commands)
        self.assertNotIn("SLOT=", clear[0])

    def test_an_unload_with_no_slot_uses_the_recorded_lane(self):
        commands = self.render("IFS_UNLOAD", {"TEMP": 220}, recorded=4,
                               occupied=True)
        clamp = [c for c in commands if c.startswith("IFS_CLAMP")]
        self.assertEqual(len(clamp), 1, commands)
        self.assertIn("CHANNEL=4", clamp[0])

    def test_the_selector_position_is_not_mistaken_for_the_loaded_lane(self):
        ## The regression this replaces: nothing recorded, but the board
        ## happens to be parked at lane 3. Unloading lane 3 would drag a lane
        ## that is not in the nozzle.
        with self.assertRaises(Exception) as caught:
            self.render("IFS_UNLOAD", {"TEMP": 220}, recorded=0, selector=3,
                        occupied=True)
        self.assertIn("nothing is currently loaded", str(caught.exception))

    def test_a_load_unloads_the_lane_it_is_replacing(self):
        """Clearing the extruder is not enough when the lane is known.

        zmod's load opens with IFS_REMOVE_CURRENT_PRUTOK, which takes the
        previous filament out of the NOZZLE and retracts the lane 70mm - not
        the 1000mm eject. Clearing only the extruder leaves the old strand at
        the hub, which on an AD5X is mounted on the toolhead and is exactly
        where the incoming lane arrives.
        """
        commands = self.render("IFS_LOAD", {"SLOT": 1, "TEMP": 220},
                               recorded=4, occupied=True)
        unload = [c for c in commands if c.startswith("IFS_UNLOAD")]
        self.assertEqual(len(unload), 1, commands)
        self.assertIn("SLOT=4", unload[0])
        self.assertLess(index_of(commands, r"IFS_UNLOAD\b"),
                        index_of(commands, r"IFS_CLAMP\b"))

    def test_a_load_with_no_lane_on_record_just_clears_the_extruder(self):
        ## Nothing to retract, and guessing a lane would drag the wrong one.
        commands = self.render("IFS_LOAD", {"SLOT": 1, "TEMP": 220},
                               recorded=0, occupied=True)
        self.assertEqual([c for c in commands if c.startswith("IFS_UNLOAD")],
                         [])
        self.assertTrue(any(c.startswith("_IFS_CLEAR_EXTRUDER")
                            for c in commands), commands)

    def test_an_unload_is_short_and_an_eject_is_the_whole_tube(self):
        """The distinction that ejected a lane clean out of the IFS mid-swap.

        zmod calls these _IFS_REMOVE_PRUTOK and _REMOVE_PRUTOK_IFS - the names
        differ only in word order, which is how they got conflated. Ours say
        what they do, and the numbers are what separate them.
        """
        unload = self.render("IFS_UNLOAD", {"SLOT": 2, "TEMP": 220},
                             recorded=2, occupied=True)
        eject = self.render("IFS_EJECT", {"SLOT": 2, "TEMP": 220},
                            recorded=2, occupied=True)
        back = lambda cs: [c for c in cs if c.startswith("IFS_RETRACT")]
        self.assertEqual(len(back(unload)), 1, unload)
        self.assertIn("LENGTH=70", back(unload)[0])
        self.assertEqual(len(back(eject)), 1, eject)
        self.assertIn("LENGTH=1000", back(eject)[0])

    def test_ejecting_an_idle_lane_never_touches_the_extruder(self):
        ## Lane 2 while lane 1 is loaded: nothing of lane 2 is in the nozzle,
        ## so there is nothing to cut and no reason to need heat.
        commands = self.render("IFS_EJECT", {"SLOT": 2}, recorded=1)
        self.assertEqual([c for c in commands
                          if c.startswith("_IFS_CLEAR_EXTRUDER")], [], commands)
        self.assertEqual(self.saved(commands), {})

    def test_ejecting_the_loaded_lane_does_clear_the_extruder(self):
        commands = self.render("IFS_EJECT", {"SLOT": 1, "TEMP": 220},
                               recorded=1, occupied=True)
        self.assertTrue(any(c.startswith("_IFS_CLEAR_EXTRUDER")
                            for c in commands), commands)
        self.assertEqual(self.saved(commands),
                         {"ifs_loaded": "0", "ifs_at_hub": "0"})

    def test_ejecting_a_parked_lane_gives_up_its_claim_on_the_hub(self):
        """The filament leaves the building; the claim must not stay behind.

        A lane parked at the hub but never loaded is precisely what a FAILED
        load leaves behind, so it is the state an eject is most likely to be
        called in. Measured: ejecting lane 1 from there left ifs_at_hub=1 with
        the filament on the bench, and the next insertion of lane 4 was refused
        with "lane 1 holds the hub" by a lane that was not in the printer.
        """
        commands = self.render("IFS_EJECT", {"SLOT": 1}, recorded=0, at_hub=1)
        self.assertEqual(self.saved(commands), {"ifs_at_hub": "0"})
        ## And only once the filament is actually out. A retract that fails
        ## part-way leaves the lane in the path, where the claim is still true.
        self.assertGreater(
            index_of(commands, r"SAVE_VARIABLE VARIABLE=ifs_at_hub VALUE=0"),
            index_of(commands, r"IFS_RETRACT CHANNEL=1"))

    def test_ejecting_an_idle_lane_leaves_another_lanes_claim_alone(self):
        ## Lane 2 comes out while lane 1 holds the hub. Not lane 2's claim to
        ## drop, and dropping it would feed the next lane into lane 1.
        commands = self.render("IFS_EJECT", {"SLOT": 2}, recorded=0, at_hub=1)
        self.assertEqual(self.saved(commands), {}, commands)

    def test_motion_calls_a_jam_a_jam_and_pauses(self):
        """zmod's cmd_IFS_MOTION: the LANE's filament bit tells them apart.

        Filament stopping is the same event either way. If the lane still holds
        filament something is stuck and the print has to stop; if it does not,
        the spool is finished, which the runout sensor already handles. Calling
        a runout a jam sends somebody looking for a blockage that is not there.
        """
        commands = self.render("IFS_MOTION", {}, recorded=2)
        self.assertTrue(any("jam" in c for c in commands), commands)
        self.assertIn("PAUSE", commands)

    def test_motion_does_not_pause_a_printer_that_is_not_printing(self):
        ## Run from the console it is a question, not an emergency. Pausing an
        ## idle printer just leaves a paused state for somebody to find.
        commands = self.render("IFS_MOTION", {}, recorded=2, printing=False)
        self.assertTrue(any("jam" in c for c in commands), commands)
        self.assertNotIn("PAUSE", commands)

    def test_motion_calls_an_empty_lane_a_runout_and_does_not_pause(self):
        ## Lane 3 is not in loaded_channels, so its filament is gone.
        commands = self.render("IFS_MOTION", {}, recorded=3)
        self.assertTrue(any("run out" in c for c in commands), commands)
        self.assertNotIn("PAUSE", commands)

    def test_motion_with_nothing_loaded_says_so(self):
        commands = self.render("IFS_MOTION", {}, recorded=0)
        self.assertNotIn("PAUSE", commands)
        self.assertTrue(any("no lane is loaded" in c for c in commands),
                        commands)

    def test_select_is_just_a_load(self):
        """zmod's tool change is literally INSERT_PRUTOK_IFS and nothing else.

        The load already takes the previous filament out of the nozzle, so a
        swap has no separate unload step to get wrong - and getting it wrong is
        what ejected a lane clean out of the IFS mid-swap.
        """
        commands = self.render("IFS_SELECT", {"SLOT": 1, "TEMP": 220},
                               recorded=4)
        self.assertEqual([c for c in commands if c.startswith("IFS_UNLOAD")],
                         [], commands)
        load = index_of(commands, r"IFS_LOAD\b")
        self.assertIn("SLOT=1", commands[load])

    def test_select_skips_the_unload_when_nothing_is_loaded(self):
        commands = self.render("IFS_SELECT", {"SLOT": 1, "TEMP": 220},
                               recorded=0)
        self.assertEqual([c for c in commands if c.startswith("IFS_UNLOAD")],
                         [])

    def test_select_does_nothing_when_the_lane_is_already_loaded(self):
        commands = self.render("IFS_SELECT", {"SLOT": 4, "TEMP": 220},
                               recorded=4)
        self.assertEqual([c for c in commands
                          if c.startswith(("IFS_LOAD", "IFS_UNLOAD"))], [])


class ShakeTest(unittest.TestCase):
    """zmod's _SBROS_TRASH: out, in, out, no extrusion.

    The point is the sudden stop, which snaps the purge string off the nozzle so
    it drops down the chute instead of being dragged onto the pad for the wiper
    to smear around.
    """

    def render(self):
        return render_macro(HW, "_IFS_SHAKE", printer=at(52.5, 229.0)).commands

    def test_it_never_combines_x_and_y(self):
        ## Same back-wall rule as every other move behind safe_y.
        for command in self.render():
            self.assertIsNone(COMBINED_XY.match(command), command)

    def test_it_does_not_extrude(self):
        for command in self.render():
            self.assertNotIn("E", command.split("G1")[-1].split("F")[0]
                             if command.startswith("G1") else "")

    def test_it_dips_into_the_chute_and_comes_back_out(self):
        ys = [c for c in self.render() if c.startswith("G1 Y")]
        self.assertEqual(len(ys), 3, ys)
        self.assertIn(str(GEOMETRY["safe_y"]), ys[0])
        self.assertIn(str(GEOMETRY["station_y"]), ys[1])
        self.assertIn(str(GEOMETRY["safe_y"]), ys[2])

    def test_it_ends_clear_of_the_back_edge(self):
        ## So the next X move is legal.
        ys = [c for c in self.render() if c.startswith("G1 Y")]
        self.assertIn(str(GEOMETRY["safe_y"]), ys[-1])


class WipeTest(unittest.TestCase):
    def setUp(self):
        self.commands = render_macro(
            HW, "_IFS_WIPE", printer=at(52.5, 229.0)).commands

    def test_sweeps_across_the_pad_in_x(self):
        ## The pad is a strip at station_y; a wipe that only pokes in Y at the
        ## chute's X touches nothing.
        swept = [c for c in self.commands if re.match(r"G1 X", c)]
        self.assertGreaterEqual(len(swept), 5, self.commands)
        span = {GEOMETRY["wipe_x_lo"], GEOMETRY["wipe_x_mid"], GEOMETRY["wipe_x_hi"]}
        seen = {float(re.search(r"X([-\d.]+)", c).group(1)) for c in swept}
        self.assertTrue(span <= seen, seen)

    def test_stays_off_the_chute_x_while_wiping(self):
        for command in self.commands:
            match = re.match(r"G1 X([-\d.]+)", command)
            if match:
                self.assertGreaterEqual(float(match.group(1)),
                                        GEOMETRY["wipe_x_lo"], command)

    def test_leaves_the_head_clear_of_the_back_edge(self):
        self.assertEqual(self.commands[-1], "_IFS_LEAVE_PURGE", self.commands)


class PurgeTest(unittest.TestCase):
    """The purge drives the lane and the extruder together.

    The IFS cannot push filament past a gripping extruder gear; it stalls
    against it, which is what "feed channel 1 failed: stalled" was on the
    printer with the filament held fast at the gear. zmod's _SBROS_TRASH_DAVIM
    issues G1 E<n> and then IFS_F10 at the same length and speed - G1 is queued
    and returns at once, so both drive the filament at the same time.
    """

    PARAMS = {"first_purge_mm": 100.0, "first_purge_speed": 300.0,
              "first_fan": 0.0, "second_purge_mm": 30.0,
              "second_purge_speed": 300.0, "second_fan": 255.0}

    def render(self, slot=None):
        printer = {"ifs": {"params": self.PARAMS},
                   "gcode_macro _IFS_SENSOR_HOLD": {"was_enabled": 1}}
        params = {"SLOT": slot} if slot is not None else {}
        return render_macro(HW, "_IFS_PURGE", printer=printer,
                            params=params).commands

    def test_every_pass_is_shaken_off_and_wiped(self):
        """zmod follows EVERY _SBROS_TRASH_DAVIM with _SBROS_TRASH and
        _CLEAR_REZINA, not just the last one.

        Leaving the first blob attached to the nozzle carries it into the second
        pass and then onto the wiper, which smears it rather than removing it.
        """
        commands = self.render(slot=1)
        shakes = [i for i, c in enumerate(commands)
                  if c.startswith("_IFS_SHAKE")]
        wipes = [i for i, c in enumerate(commands)
                 if c.startswith("_IFS_WIPE")]
        self.assertEqual(len(shakes), 2, commands)
        self.assertEqual(len(wipes), 2, commands)
        ## And each shake comes before its wipe: snap it off, then clean up.
        for shake, wipe in zip(shakes, wipes):
            self.assertLess(shake, wipe, commands)

    def test_every_purge_pass_happens_over_the_chute(self):
        """Both passes, because _IFS_WIPE moves the head away between them.

        zmod's _SBROS_TRASH_DAVIM opens with _GOTO_TRASH and is called once per
        pass, so both of its purges land in the bin. Copying only the extrusion
        out of it left the second pass wherever the wipe had parked the head -
        measured at X78.0 Y220.0, which is ON the wiper pad, 26mm from the
        chute at X52.5 Y229. Purging onto the wiper is how a wiper stops
        working, and it is visible from across the room.
        """
        commands = self.render(slot=1)
        parks = [i for i, c in enumerate(commands)
                 if c.startswith("_IFS_PARK_FOR_PURGE")]
        drops = [i for i, c in enumerate(commands) if c.startswith("G1 E")]
        self.assertEqual(len(parks), 2, commands)
        self.assertEqual(len(drops), 2, commands)
        ## Each pass is parked before it extrudes...
        for park, drop in zip(parks, drops):
            self.assertLess(park, drop, commands)
        ## ...and the second park is AFTER the wipe that moved us off the chute,
        ## which is the whole point - parking before the wipe would not help.
        wipes = [i for i, c in enumerate(commands) if c.startswith("_IFS_WIPE")]
        self.assertLess(wipes[0], parks[1], commands)
        self.assertLess(parks[1], drops[1], commands)

    def test_the_lane_drives_alongside_the_extruder(self):
        commands = self.render(slot=1)
        extrude = index_of(commands, r"G1 E100\.0 F300")
        feed = index_of(commands, r"IFS_FEED CHANNEL=1")
        ## The extruder move must be issued FIRST: it is queued and returns, so
        ## the blocking IFS feed then overlaps it. Reversed, the feed blocks
        ## before the gear ever turns and stalls exactly as before.
        self.assertLess(extrude, feed, commands)
        self.assertIn("LENGTH=100", commands[feed])
        self.assertIn("SPEED=300", commands[feed])

    def test_the_lane_is_released_once_the_extruder_has_it(self):
        commands = self.render(slot=1)
        self.assertLess(index_of(commands, r"IFS_FEED"),
                        index_of(commands, r"IFS_RELEASE CHANNEL=1"))

    def test_without_a_slot_nothing_drives_the_lane(self):
        ## A standalone purge must not command a channel it was not given.
        commands = self.render()
        self.assertFalse([c for c in commands if c.startswith("IFS_FEED")],
                         commands)
        self.assertFalse([c for c in commands if c.startswith("IFS_RELEASE")],
                         commands)

    def test_every_extruder_move_is_bracketed_by_the_sensor_hold(self):
        ## pause_on_runout is on, so filament moving under our own command
        ## would read as a runout and pause the print.
        commands = self.render(slot=1)
        held = False
        for command in commands:
            if command == "_IFS_SENSOR_HOLD":
                held = True
            elif command == "_IFS_SENSOR_RESUME":
                held = False
            elif command.startswith("G1 E"):
                self.assertTrue(held, "unbracketed %r in %r"
                                % (command, commands))
        self.assertFalse(held, "sensor left muted: %r" % (commands,))

    def test_it_purges_twice_and_ends_at_the_wiper(self):
        commands = self.render(slot=1)
        self.assertEqual(len([c for c in commands if c.startswith("G1 E")]), 2,
                         commands)
        self.assertEqual(commands[-1], "_IFS_WIPE", commands)


class StandalonePurgeTest(unittest.TestCase):
    """zmod's PURGE_PRUTOK_IFS, for when a colour has not fully changed over."""

    def printer(self, recorded=1, target=220.0):
        return {
            "ifs": {"params": {"first_purge_mm": 100.0,
                               "first_purge_speed": 300.0, "first_fan": 0.0,
                               "second_purge_mm": 30.0,
                               "second_purge_speed": 300.0,
                               "second_fan": 255.0}},
            "extruder": {"target": target},
            "save_variables": {"variables": {"ifs_loaded": recorded}},
        }

    def render(self, params=None, **kwargs):
        ## `params or {...}` would swallow an EMPTY dict, which is exactly the
        ## case the cold-nozzle test needs - no TEMP at all.
        if params is None:
            params = {"TEMP": 220}
        return render_macro(HW, "IFS_PURGE", printer=self.printer(**kwargs),
                            params=params).commands

    def test_it_parks_over_the_chute_before_extruding(self):
        commands = self.render()
        self.assertLess(index_of(commands, r"_IFS_PARK_FOR_PURGE\b"),
                        index_of(commands, r"_IFS_PURGE\b"))

    def test_it_waits_for_temperature(self):
        commands = self.render()
        self.assertTrue(any(c.startswith("TEMPERATURE_WAIT") for c in commands),
                        commands)

    def test_it_does_not_drive_the_lane(self):
        ## The filament is already through the gear; this is the extruder's job
        ## alone, which is zmod's _SBROS_TRASH_DAVIM PRUTOK=0.
        inner = [c for c in self.render() if c.startswith("_IFS_PURGE")]
        self.assertEqual(len(inner), 1, inner)
        self.assertNotIn("SLOT=", inner[0])

    def test_it_refuses_when_nothing_is_loaded(self):
        with self.assertRaises(Exception) as caught:
            self.render(recorded=0)
        self.assertIn("nothing is loaded", str(caught.exception))

    def test_it_refuses_a_cold_nozzle(self):
        with self.assertRaises(Exception) as caught:
            self.render(params={}, target=0.0)
        self.assertIn("TEMP", str(caught.exception))


class CutTest(unittest.TestCase):
    """The cutter is a fixed blade off the front-left of the bed."""

    PARAMS = {"cut_before_mm": 0.0, "cut_after_mm": 5.0, "unload_speed": 600.0,
              "unload_extruder_mm": 60.0}

    def render(self):
        return render_macro(HW, "_IFS_CUT", printer={
            "ifs": {"params": self.PARAMS},
            "gcode_macro _IFS_CUT": load_macro(HW, "_IFS_CUT").variables,
            "gcode_macro _IFS_SENSOR_HOLD": {"was_enabled": 1},
        }).commands

    def test_y_moves_before_x_and_the_cut_is_the_slow_x(self):
        ## zmod: G1 Y-7.5 F1800 then G1 X-2.5 F600. Reversed, the head would
        ## cross the front of the bed at the cutter's depth.
        commands = self.render()
        y = index_of(commands, r"G1 Y-7\.5")
        x = index_of(commands, r"G1 X-2\.5")
        self.assertLess(y, x, commands)
        self.assertIn("F600", commands[x])

    def test_the_cut_coordinates_are_inside_the_machine(self):
        ## Negative on purpose, but axis_minimum is (-20, -20): outside that and
        ## klipper refuses the move.
        g = load_macro(HW, "_IFS_CUT").variables
        self.assertGreater(g["cut_x"], -20.0)
        self.assertGreater(g["cut_y"], -20.0)
        self.assertLess(g["cut_x"], 0.0)
        self.assertLess(g["cut_y"], 0.0)

    def test_it_withdraws_the_stub_and_leaves_the_corner(self):
        commands = self.render()
        retract = index_of(commands, r"G1 E-5\.0")
        leave = index_of(commands, r"G1 X20\.0")
        self.assertLess(index_of(commands, r"G1 X-2\.5"), retract)
        self.assertLess(retract, leave)

    def test_every_extruder_move_is_bracketed(self):
        commands = self.render()
        held = False
        for command in commands:
            if command == "_IFS_SENSOR_HOLD":
                held = True
            elif command == "_IFS_SENSOR_RESUME":
                held = False
            elif command.startswith("G1 E"):
                self.assertTrue(held, "unbracketed %r" % command)
        self.assertFalse(held, "sensor left muted")

    def test_the_blade_moves_are_bracketed_too(self):
        """Severing the filament is the biggest present-to-absent step there is.

        zmod brackets only its two G1 E moves and runs the shear itself with the
        sensor live. On a sensor declared pause_on_runout that fires a runout:
        measured, a soak run paused itself mid-cut with nothing wrong. The hold
        has to span the blade moves, not just the extruder ones.
        """
        commands = self.render()
        held = False
        for command in commands:
            if command == "_IFS_SENSOR_HOLD":
                held = True
            elif command == "_IFS_SENSOR_RESUME":
                held = False
            elif command.startswith("G1 Y-") or command.startswith("G1 X-"):
                self.assertTrue(held, "cut move with a live sensor: %r"
                                % command)

    def test_the_sensor_is_muted_exactly_once(self):
        ## Not hold/resume/hold: the gap between them is the shear.
        commands = self.render()
        self.assertEqual(commands.count("_IFS_SENSOR_HOLD"), 1, commands)
        self.assertEqual(commands.count("_IFS_SENSOR_RESUME"), 1, commands)


class MaterialTest(unittest.TestCase):
    """What the slot holds decides the temperature and the purge volume.

    The one axis zmod varies by material is the handling temperature; the other
    is a flat bonus when the TYPE changes, not merely the colour. Both live in
    [ifs_materials] and are only read here.
    """

    PARAMS = {"tube_mm": 1000.0, "ifs_speed": 1200.0, "purge_extra_mm": 90.0,
              "first_purge_mm": 100.0, "first_purge_speed": 300.0,
              "first_fan": 0.0, "second_purge_mm": 30.0,
              "second_purge_speed": 300.0, "second_fan": 255.0,
              "hub_clear_mm": 300.0, "unload_ifs_mm": 70.0,
              "unload_extruder_mm": 60.0, "unload_speed": 600.0,
              "cut_before_mm": 0.0, "cut_after_mm": 5.0}

    def printer(self, slots, recorded=0, target=0.0):
        return {
            "ifs": {"connected": True, "error": None,
                    "loaded_channels": [1, 2, 4], "params": self.PARAMS},
            "ifs_materials": {"slots": slots},
            "extruder": {"target": target},
            "filament_switch_sensor toolhead": {"filament_detected": False},
            "save_variables": {"variables": {"ifs_loaded": recorded,
                                             "ifs_at_hub": 0}},
            "gcode_macro _IFS_SENSOR_HOLD": {"was_enabled": 1},
        }

    def render(self, slots, params=None, **kwargs):
        return render_macro(
            HW, "IFS_LOAD", printer=self.printer(slots, **kwargs),
            params=params if params is not None else {"SLOT": 1}).commands

    PLA = {"type": "PLA", "color": "#FFFFFF", "temp": 220.0}
    ABS = {"type": "ABS", "color": "#898989", "temp": 250.0}
    RED_PLA = {"type": "PLA", "color": "#FF0000", "temp": 220.0}
    UNLABELLED = {"type": None, "color": None, "temp": None}

    def test_a_load_with_no_temp_uses_the_materials_own(self):
        ## The whole point: IFS_SELECT SLOT=1 on a cold machine has to work.
        commands = self.render({"1": self.ABS})
        self.assertIn("M104 S250", commands)

    def test_an_explicit_temp_still_wins(self):
        ## A user who types 240 means 240, whatever the slot is labelled.
        commands = self.render({"1": self.PLA},
                               params={"SLOT": 1, "TEMP": 240})
        self.assertIn("M104 S240", commands)

    def test_an_unlabelled_slot_falls_back_to_the_nozzle(self):
        ## Not to PLA. zmod substitutes PLA here, which runs an unknown
        ## material at 220 and snaps it off in the heatbreak.
        commands = self.render({"1": self.UNLABELLED}, target=245.0)
        self.assertIn("M104 S245", commands)

    def test_an_unlabelled_slot_on_a_cold_nozzle_is_refused(self):
        ## Nothing below this macro enforces a temperature: min_extrude_temp is
        ## 0 on this printer, so klipper will happily extrude cold.
        with self.assertRaises(MacroActionError) as caught:
            self.render({"1": self.UNLABELLED}, target=0.0)
        self.assertIn("TEMP", str(caught.exception))

    def test_a_material_change_purges_more(self):
        commands = self.render({"1": self.PLA, "4": self.ABS}, recorded=4)
        purge = [c for c in commands if c.startswith("_IFS_PURGE")]
        self.assertEqual(len(purge), 1, commands)
        self.assertIn("EXTRA=90.0", purge[0])

    def test_a_colour_change_does_not(self):
        ## Same material, different colour: stock's own purge volume already
        ## covers it, and 90mm a swap adds up fast on a multicolour print.
        commands = self.render({"1": self.PLA, "4": self.RED_PLA}, recorded=4)
        purge = [c for c in commands if c.startswith("_IFS_PURGE")]
        self.assertIn("EXTRA=0.0", purge[0])

    def test_an_unknown_outgoing_type_is_not_a_change(self):
        ## Otherwise every load on a machine with unlabelled slots pays the
        ## bonus, which is a guess dressed up as a measurement.
        commands = self.render({"1": self.PLA, "4": self.UNLABELLED},
                               recorded=4, target=220.0)
        purge = [c for c in commands if c.startswith("_IFS_PURGE")]
        self.assertIn("EXTRA=0.0", purge[0])

    def test_the_bonus_lands_on_both_passes(self):
        ## zmod passes filament_drop_length_add to both of its purge calls.
        commands = render_macro(
            HW, "_IFS_PURGE",
            printer={"ifs": {"params": self.PARAMS},
                     "gcode_macro _IFS_SENSOR_HOLD": {"was_enabled": 1}},
            params={"SLOT": 1, "EXTRA": 90.0}).commands
        extrudes = [c for c in commands if c.startswith("G1 E")]
        self.assertIn("G1 E190.0 F300.0", extrudes)
        self.assertIn("G1 E120.0 F300.0", extrudes)

    def test_the_lane_co_push_matches_the_longer_first_pass(self):
        ## The IFS cannot push past a gripping gear - it has to drive the same
        ## distance as the extruder or it stalls against it.
        commands = render_macro(
            HW, "_IFS_PURGE",
            printer={"ifs": {"params": self.PARAMS},
                     "gcode_macro _IFS_SENSOR_HOLD": {"was_enabled": 1}},
            params={"SLOT": 1, "EXTRA": 90.0}).commands
        feed = [c for c in commands if c.startswith("IFS_FEED")]
        self.assertEqual(len(feed), 1, commands)
        self.assertIn("LENGTH=190.0", feed[0])

    def test_a_printer_without_the_materials_object_still_loads(self):
        ## [ifs_materials] is optional; a template that raises on its absence
        ## takes the whole load down with it.
        printer = self.printer({"1": self.PLA}, target=220.0)
        del printer["ifs_materials"]
        commands = render_macro(HW, "IFS_LOAD", printer=printer,
                                params={"SLOT": 1}).commands
        self.assertIn("M104 S220", commands)


class ClearExtruderTest(unittest.TestCase):
    PARAMS = {"cut_before_mm": 0.0, "cut_after_mm": 5.0, "unload_speed": 600.0,
              "unload_extruder_mm": 60.0}

    def render(self, detected, slot=None):
        params = {"TEMP": 220}
        if slot is not None:
            params["SLOT"] = slot
        return render_macro(HW, "_IFS_CLEAR_EXTRUDER", printer={
            "ifs": {"params": self.PARAMS},
            "extruder": {"target": 220.0},
            "filament_switch_sensor toolhead": {"filament_detected": detected},
            "gcode_macro _IFS_SENSOR_HOLD": {"was_enabled": 1},
        }, params=params).commands

    def test_an_empty_extruder_is_left_alone(self):
        ## zmod's IFS_REMOVE_CURRENT_PRUTOK returns on the same test. Cutting
        ## air and retracting 60mm of nothing would strip the gear.
        commands = self.render(False)
        self.assertFalse([c for c in commands if c.startswith("_IFS_CUT")],
                         commands)
        self.assertFalse([c for c in commands if c.startswith("G1 E")],
                         commands)

    def test_a_loaded_extruder_is_cut_then_withdrawn(self):
        commands = self.render(True)
        self.assertLess(index_of(commands, r"_IFS_CUT"),
                        index_of(commands, r"G1 E-60\.0"))

    def test_it_heats_and_waits_before_cutting(self):
        ## Cold filament snaps in the heatbreak instead of shearing.
        commands = self.render(True)
        self.assertLess(index_of(commands, r"M104 S220"),
                        index_of(commands, r"TEMPERATURE_WAIT"))
        self.assertLess(index_of(commands, r"TEMPERATURE_WAIT"),
                        index_of(commands, r"_IFS_CUT"))

    def test_the_lane_pulls_with_the_extruder(self):
        """Both ends move the SAME strand, so they move together or they fight.

        The mirror of the co-push in _IFS_PURGE, and zmod's own shape: G1 E of
        nozzle_cleaning_length beside an IFS retract of the same length at the
        same speed. Dragging 60 mm back through a released lane works against
        the idle drive, and a lane that comes out of a swap buckled will not
        feed OR retract afterwards - which reads as a dead lane and is not one.
        """
        commands = self.render(True, slot=2)
        self.assertIn("IFS_RETRACT CHANNEL=2 UNTIL=done LENGTH=60.0 SPEED=600.0",
                      commands)

    def test_the_lane_is_clamped_before_it_is_asked_to_pull(self):
        commands = self.render(True, slot=2)
        self.assertLess(index_of(commands, r"IFS_CLAMP CHANNEL=2"),
                        index_of(commands, r"IFS_RETRACT CHANNEL=2"))

    def test_the_retract_is_issued_after_the_extruder_move(self):
        ## G1 is queued and returns immediately, so ordering it second is what
        ## makes the two run ALONGSIDE each other. Issued first, the lane would
        ## finish its pull before the extruder started.
        commands = self.render(True, slot=2)
        self.assertLess(index_of(commands, r"G1 E-60\.0"),
                        index_of(commands, r"IFS_RETRACT CHANNEL=2"))

    def test_the_two_pulls_are_the_same_distance_and_speed(self):
        ## Different numbers here is the fight, just quieter.
        commands = self.render(True, slot=2)
        extrude = [c for c in commands if c.startswith("G1 E-")][0]
        retract = [c for c in commands if c.startswith("IFS_RETRACT")][0]
        self.assertIn("60.0", extrude)
        self.assertIn("600.0", extrude)
        self.assertIn("LENGTH=60.0", retract)
        self.assertIn("SPEED=600.0", retract)

    def test_with_no_lane_on_record_the_extruder_works_alone(self):
        ## IFS_LOAD's fallback when save_variables knows of nothing loaded:
        ## there is no lane to clamp, and clamping a guess would grip the wrong
        ## strand.
        commands = self.render(True)
        self.assertIn("G1 E-60.0 F600.0", commands)
        self.assertFalse([c for c in commands if c.startswith("IFS_RETRACT")],
                         commands)
        self.assertFalse([c for c in commands if c.startswith("IFS_CLAMP")],
                         commands)


class ChangeLiftTest(unittest.TestCase):
    """zmod's _A_CHANGE_FILAMENT lifts the bed 5 mm, ONCE, before any travel.

    Its own log line says so ("Moving the bed down 5 mm"). Anything bigger
    measured as clipping the back-wall hardware at the Y229 station entries -
    zmod arrives at part-height +5 and its stations never lift at all. And the
    50 mm we used to lift here was zmod's custom_park_dz, which belongs to
    PAUSE/CANCEL parking (_TOOLHEAD_PARK_PAUSE_CANCEL), not to tool changes.
    """

    PARAMS = {"tube_mm": 1000.0, "ifs_speed": 1200.0, "purge_extra_mm": 90.0,
              "first_purge_mm": 100.0, "first_purge_speed": 300.0,
              "first_fan": 0.0, "second_purge_mm": 30.0,
              "second_purge_speed": 300.0, "second_fan": 255.0,
              "hub_clear_mm": 300.0, "unload_ifs_mm": 70.0,
              "unload_extruder_mm": 60.0, "unload_speed": 600.0,
              "cut_before_mm": 0.0, "cut_after_mm": 5.0}

    def select(self, slot=1, current=4):
        return render_macro(HW, "IFS_SELECT", printer={
            "ifs": {"connected": True, "error": None,
                    "loaded_channels": [1, 2, 4], "params": self.PARAMS},
            "extruder": {"target": 205.0},
            "filament_switch_sensor toolhead": {"filament_detected": True},
            "save_variables": {"variables": {"ifs_loaded": current,
                                             "ifs_at_hub": current}},
            "print_stats": {"state": "printing"},
            "gcode_move": {"gcode_position": {"x": 100.0, "y": 90.0,
                                              "z": 5.0}},
            "fan_generic fanM106": {"speed": 0.6},
            "gcode_macro _IFS_SENSOR_HOLD": {"was_enabled": 1},
            "gcode_macro _IFS_GEOMETRY": GEOMETRY,
        }, params={"SLOT": slot}).commands

    def lifts(self, commands):
        return [c for c in commands if c.startswith("MOVE_SAFE")]

    def test_it_lifts_once_relative_at_change_entry(self):
        commands = self.select()
        lifts = self.lifts(commands)
        self.assertEqual(len(lifts), 1, commands)
        self.assertIn("ABSOLUTE=0", lifts[0])
        self.assertIn("Z=%s" % GEOMETRY["lift_dz"], lifts[0])

    def test_the_lift_precedes_every_move_and_save(self):
        ## Before the load chain, before even the save: the head must be off
        ## the part before anything else travels.
        commands = self.select()
        self.assertEqual(commands.index(self.lifts(commands)[0]), 0,
                         commands)

    def test_the_noop_select_does_not_lift(self):
        commands = self.select(slot=4, current=4)
        self.assertEqual(self.lifts(commands), [])


class ToolChangeTest(unittest.TestCase):
    """A swap during a print has to put the print back exactly as it was.

    zmod does this in _A_CHANGE_FILAMENT / _RESTORE_POSITION_AFTER_FILAMENT_-
    CHANGE, wrapped around the same load. Its elaborate edge-walk exists
    because it lifts only 5 mm and must not clip the BACK WALL hardware; ours
    lifts on the way out through _IFS_GOTO_STATION and enforces the same one
    rule that actually matters - X never travels while the head is behind
    safe_y.
    """

    PARAMS = {"tube_mm": 1000.0, "ifs_speed": 1200.0, "purge_extra_mm": 90.0,
              "first_purge_mm": 100.0, "first_purge_speed": 300.0,
              "first_fan": 0.0, "second_purge_mm": 30.0,
              "second_purge_speed": 300.0, "second_fan": 255.0,
              "hub_clear_mm": 300.0, "unload_ifs_mm": 70.0,
              "unload_extruder_mm": 60.0, "unload_speed": 600.0,
              "cut_before_mm": 0.0, "cut_after_mm": 5.0}

    def printer(self, printing=True, recorded=4, target=205.0):
        return {
            "ifs": {"connected": True, "error": None,
                    "loaded_channels": [1, 2, 4], "params": self.PARAMS},
            "extruder": {"target": target},
            "filament_switch_sensor toolhead": {"filament_detected": True},
            "save_variables": {"variables": {"ifs_loaded": recorded,
                                             "ifs_at_hub": recorded}},
            "print_stats": {"state": "printing" if printing else "standby"},
            "gcode_move": {"gcode_position": {"x": 100.0, "y": 90.0,
                                              "z": 5.0}},
            "fan_generic fanM106": {"speed": 0.6},
            "gcode_macro _IFS_SENSOR_HOLD": {"was_enabled": 1},
            "gcode_macro _IFS_GEOMETRY": GEOMETRY,
        }

    def select(self, **kwargs):
        return render_macro(HW, "IFS_SELECT", printer=self.printer(**kwargs),
                            params={"SLOT": 1}).commands

    def saved(self, commands):
        out = {}
        for c in commands:
            if not c.startswith("SET_GCODE_VARIABLE"):
                continue
            bits = dict(w.split("=", 1) for w in c.split()[1:] if "=" in w)
            out[bits["VARIABLE"]] = bits["VALUE"]
        return out

    def test_a_swap_mid_print_saves_where_the_print_was(self):
        saved = self.saved(self.select())
        self.assertEqual(saved.get("restore_x"), "100.0")
        self.assertEqual(saved.get("restore_y"), "90.0")
        self.assertEqual(saved.get("restore_z"), "5.0")

    def test_it_saves_the_prints_temperature_not_the_materials(self):
        ## The load is about to set the nozzle to the incoming material's
        ## handling temperature. That is not the number the slicer chose, and
        ## printing on at 220 what was sliced for 205 ruins the part.
        self.assertEqual(self.saved(self.select()).get("restore_temp"), "205.0")

    def test_it_saves_the_part_fan(self):
        ## The purge drives it to full and then to zero.
        self.assertEqual(self.saved(self.select()).get("restore_fan"), "0.6")

    def test_it_saves_the_extrusion_state(self):
        ## The purge runs M83 and G92 E0; without this the print's extrusion
        ## accounting continues from the purge's zero.
        commands = self.select()
        self.assertIn("SAVE_GCODE_STATE NAME=ifs_tool_change", commands)

    def test_the_save_happens_before_the_load(self):
        commands = self.select()
        self.assertLess(index_of(commands, r"SAVE_GCODE_STATE"),
                        index_of(commands, r"IFS_LOAD"))

    def test_it_restores_after_the_load(self):
        commands = self.select()
        self.assertLess(index_of(commands, r"IFS_LOAD"),
                        index_of(commands, r"_IFS_RESTORE_AFTER_CHANGE"))

    def test_an_idle_swap_saves_and_restores_nothing(self):
        ## There is no print to put back, and parking at the chute afterwards
        ## is what somebody standing at the machine wants.
        commands = self.select(printing=False)
        self.assertEqual(self.saved(commands), {})
        self.assertNotIn("SAVE_GCODE_STATE NAME=ifs_tool_change", commands)
        self.assertFalse([c for c in commands
                          if c.startswith("_IFS_RESTORE_AFTER_CHANGE")],
                         commands)

    def test_selecting_the_loaded_lane_mid_print_moves_nothing(self):
        ## A slicer emits T<n> at every change including redundant ones. Saving
        ## a position and never restoring it would strand the sentinel.
        commands = self.select(recorded=1)
        self.assertEqual(self.saved(commands), {})
        self.assertFalse([c for c in commands if c.startswith("IFS_LOAD")],
                         commands)


class RestoreAfterChangeTest(unittest.TestCase):
    """The way back. Order is the whole content of this macro."""

    def render(self, x=100.0, y=90.0, z=5.0, temp=205.0, fan=0.6):
        return render_macro(HW, "_IFS_RESTORE_AFTER_CHANGE", printer={
            "gcode_macro IFS_SELECT": {
                "restore_x": x, "restore_y": y, "restore_z": z,
                "restore_temp": temp, "restore_fan": fan},
            "gcode_macro _IFS_GEOMETRY":
                load_macro(HW, "_IFS_GEOMETRY").variables,
        }).commands

    def test_it_leaves_the_back_edge_before_moving_x(self):
        """The one rule that matters: X never travels behind safe_y.

        The wipe pad is at Y=229 and the wall hardware is back there. A single
        G1 X from the pad drags the head across it.
        """
        commands = self.render()
        self.assertLess(index_of(commands, r"_IFS_LEAVE_PURGE"),
                        index_of(commands, r"G1 X100\.0"))

    def test_z_comes_down_last(self):
        ## Every XY travel happens at the height the outbound trip lifted to.
        commands = self.render()
        self.assertGreater(index_of(commands, r"G1 Z5\.0"),
                           index_of(commands, r"G1 Y90\.0"))

    def test_it_puts_the_prints_temperature_back_and_waits(self):
        commands = self.render()
        self.assertIn("M104 S205", commands)
        self.assertLess(index_of(commands, r"M104 S205"),
                        index_of(commands, r"TEMPERATURE_WAIT"))

    def test_a_cold_saved_temperature_is_not_restored(self):
        ## 0 means nothing was heating; M104 S0 would be right but the wait
        ## would never finish.
        commands = self.render(temp=0.0)
        self.assertFalse([c for c in commands if c.startswith("TEMPERATURE_WAIT")],
                         commands)

    def test_it_puts_the_part_fan_back(self):
        self.assertIn("SET_FAN_SPEED FAN=fanM106 SPEED=0.6", self.render())

    def test_the_gcode_state_is_restored_without_a_second_trip(self):
        ## MOVE=1 here would send the head on a diagonal to a position the
        ## moves above already reached - across the bed, through the part.
        commands = self.render()
        restore = [c for c in commands if c.startswith("RESTORE_GCODE_STATE")]
        self.assertEqual(len(restore), 1, commands)
        self.assertIn("MOVE=0", restore[0])
        self.assertIn("NAME=ifs_tool_change", restore[0])

    def test_the_state_restore_comes_after_the_moves(self):
        ## It carries the extrusion mode and E position the print needs for its
        ## very next line, so nothing of ours may run after it.
        commands = self.render()
        self.assertGreater(index_of(commands, r"RESTORE_GCODE_STATE"),
                           index_of(commands, r"G1 Z5\.0"))

    def test_the_sentinel_is_cleared(self):
        ## Otherwise a second restore replays a stale position.
        commands = self.render()
        cleared = [c for c in commands
                   if c.startswith("SET_GCODE_VARIABLE") and "-1000" in c]
        self.assertEqual(len(cleared), 3, commands)

    def test_an_unsaved_position_is_refused_not_driven_to(self):
        ## -1000 is not a coordinate. Moving there is a crash into the frame.
        with self.assertRaises(MacroActionError):
            self.render(x=-1000.0, y=-1000.0, z=-1000.0)


class ToolMacroTest(unittest.TestCase):
    """T0..T3 are how a sliced multi-material file asks for a change."""

    def test_each_tool_maps_to_its_lane(self):
        ## The slicer counts extruders from 0; the IFS counts lanes from 1.
        for tool, slot in enumerate((1, 2, 3, 4)):
            commands = render_macro(HW, "T%d" % tool, printer={}).commands
            self.assertEqual(list(commands), ["IFS_SELECT SLOT=%d" % slot],
                             "T%d" % tool)


class SensorHealTest(unittest.TestCase):
    """A leaked mute must not survive into the next command.

    Klipper macros have no finally, so an error between _IFS_SENSOR_HOLD and
    its resume leaves the toolhead sensor muted - and the printer then runs
    with no runout detection at all, silently, because a muted sensor looks
    exactly like a sensor with filament in front of it. Every public entry
    point heals it before it moves anything. zmod has the same hole and no
    heal.
    """

    PARAMS = {"tube_mm": 1000.0, "ifs_speed": 1200.0, "purge_extra_mm": 90.0,
              "first_purge_mm": 100.0, "first_purge_speed": 300.0,
              "first_fan": 0.0, "second_purge_mm": 30.0,
              "second_purge_speed": 300.0, "second_fan": 255.0,
              "hub_clear_mm": 300.0, "unload_ifs_mm": 70.0,
              "unload_extruder_mm": 60.0, "unload_speed": 600.0,
              "cut_before_mm": 0.0, "cut_after_mm": 5.0,
              "load_empty_mm": 600.0, "autoinsert_ret_mm": 90.0}

    ## macro -> params that reach its first moving command
    ENTRY_POINTS = {
        "IFS_LOAD": {"SLOT": 1, "TEMP": 220},
        "IFS_UNLOAD": {"SLOT": 1, "TEMP": 220},
        "IFS_EJECT": {"SLOT": 1, "TEMP": 220},
        "IFS_PURGE": {"TEMP": 220},
        "IFS_AUTOINSERT": {"CHANNEL": 1},
    }

    def printer(self):
        return {
            "ifs": {"connected": True, "error": None,
                    "loaded_channels": [1, 2, 4], "active_channel": 0,
                    "params": self.PARAMS},
            "extruder": {"target": 220.0},
            "filament_switch_sensor toolhead": {"filament_detected": True},
            "save_variables": {"variables": {"ifs_loaded": 1,
                                             "ifs_at_hub": 1}},
            "gcode_macro _IFS_SENSOR_HOLD": {"was_enabled": 1},
        }

    def test_every_public_entry_point_heals_first(self):
        for macro, params in sorted(self.ENTRY_POINTS.items()):
            commands = render_macro(HW, macro, printer=self.printer(),
                                    params=params).commands
            self.assertIn("_IFS_SENSOR_RESUME", commands,
                          "%s never heals a leaked mute" % macro)
            heal = commands.index("_IFS_SENSOR_RESUME")
            movers = [i for i, c in enumerate(commands)
                      if c.startswith(("G1 ", "IFS_FEED", "IFS_RETRACT",
                                       "IFS_CLAMP", "_IFS_CUT"))]
            if movers:
                self.assertLess(heal, movers[0],
                                "%s moves before healing: %r"
                                % (macro, commands[:movers[0] + 1]))

    def test_the_heal_is_the_idempotent_restore_not_a_forced_enable(self):
        ## SET_FILAMENT_SENSOR ENABLE=1 here would turn on a sensor the
        ## operator had deliberately switched off. _IFS_SENSOR_RESUME only
        ## restores what a hold actually took.
        for macro, params in sorted(self.ENTRY_POINTS.items()):
            commands = render_macro(HW, macro, printer=self.printer(),
                                    params=params).commands
            self.assertFalse(
                [c for c in commands if c.startswith("SET_FILAMENT_SENSOR")],
                "%s forces the sensor on" % macro)


class LeavePurgeTest(unittest.TestCase):
    def test_comes_forward_to_safe_y_only(self):
        commands = render_macro(HW, "_IFS_LEAVE_PURGE",
                                printer=at(52.5, 229.0)).commands
        target = index_of(commands, r"G1 Y%s\b" % GEOMETRY["safe_y"])
        self.assertIsNone(COMBINED_XY.match(commands[target]), commands)

    def test_safe_y_is_inside_the_machine_and_ahead_of_the_stations(self):
        self.assertLess(GEOMETRY["safe_y"], GEOMETRY["station_y"])
        self.assertLess(GEOMETRY["station_y"], 232.0)  # klipper axis_maximum


if __name__ == "__main__":
    unittest.main()
