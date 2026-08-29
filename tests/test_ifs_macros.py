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

from tests.gcode_macro_harness import load_macro, render_macro


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

    def test_lift_is_relative_not_an_absolute_z(self):
        commands = station(GEOMETRY["chute_x"], x=0.0, y=0.0)
        lift = commands[index_of(commands, r"MOVE_SAFE ")]
        self.assertIn("ABSOLUTE=0", lift)
        self.assertIn("Z=%s" % GEOMETRY["lift_dz"], lift)
        ## it must precede all travel
        self.assertLess(commands.index(lift), index_of(commands, r"G1 [XY]"))

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
              "autoinsert_ret_mm": 90.0}

    def printer(self, occupied=False, loaded=(1, 2, 4)):
        return {
            "ifs": {"connected": True, "error": None,
                    "loaded_channels": list(loaded), "params": self.PARAMS},
            "extruder": {"target": 0.0},
            "filament_switch_sensor toolhead": {"filament_detected": occupied},
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

    def test_an_occupied_extruder_only_comes_up_to_the_hub(self):
        ## filament_autoinsert_full_length, waiting on READY - the sensor is
        ## already tripped by whatever is loaded, so it cannot be the signal.
        feed = self.feeds(self.render(occupied=True))
        self.assertEqual(len(feed), 1, feed)
        self.assertIn("LENGTH=550", feed[0])
        self.assertIn("UNTIL=done", feed[0])

    def test_it_backs_off_the_gear_once_the_tip_arrives(self):
        ## filament_autoinsert_ret_length. Leaving the tip inside the extruder
        ## gear means the next load has nowhere to push it.
        commands = self.render()
        back = [c for c in commands if c.startswith("IFS_RETRACT")]
        self.assertEqual(len(back), 1, commands)
        self.assertIn("LENGTH=90", back[0])
        self.assertLess(index_of(commands, r"IFS_FEED\b"),
                        index_of(commands, r"IFS_RETRACT\b"))

    def test_the_occupied_branch_does_not_back_off(self):
        ## Nothing arrived at a sensor, so there is nothing to back away from.
        self.assertEqual(
            [c for c in self.render(occupied=True)
             if c.startswith("IFS_RETRACT")], [])

    def test_it_stops_the_board_before_touching_the_lane_again(self):
        ## UNTIL=toolhead returns while the board is still feeding - the sensor
        ## ends OUR wait, not the board's move. Retracting into a running feed
        ## fights it.
        commands = self.render()
        self.assertLess(index_of(commands, r"IFS_STOP\b"),
                        index_of(commands, r"IFS_RETRACT\b"))

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

    def printer(self, loaded=False, connected=True, target=0.0):
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
        self.assertIn("_IFS_PURGE SLOT=1", commands)
        self.assertIn("IFS_MARK_INSERTED CHANNEL=1", commands)
        self.assertLess(index_of(commands, r"_IFS_PURGE"),
                        index_of(commands, r"IFS_MARK_INSERTED"))

    def test_it_clamps_before_feeding_and_stops_after(self):
        commands = self.render()
        assert_order = [index_of(commands, r"IFS_CLAMP"),
                        index_of(commands, r"IFS_FEED"),
                        index_of(commands, r"IFS_STOP")]
        self.assertEqual(assert_order, sorted(assert_order), commands)


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


class ClearExtruderTest(unittest.TestCase):
    PARAMS = {"cut_before_mm": 0.0, "cut_after_mm": 5.0, "unload_speed": 600.0,
              "unload_extruder_mm": 60.0}

    def render(self, detected):
        return render_macro(HW, "_IFS_CLEAR_EXTRUDER", printer={
            "ifs": {"params": self.PARAMS},
            "extruder": {"target": 220.0},
            "filament_switch_sensor toolhead": {"filament_detected": detected},
            "gcode_macro _IFS_SENSOR_HOLD": {"was_enabled": 1},
        }, params={"TEMP": 220}).commands

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
