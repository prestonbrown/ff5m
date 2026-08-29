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
