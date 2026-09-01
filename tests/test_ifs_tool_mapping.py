## Tool remapping: which IFS lane a slicer's T<n> actually loads.
##
## Copyright (C) 2026, Preston Brown
##
## This file may be distributed under the terms of the GNU GPLv3 license
##
## The tool map is per-print state owned by the [ifs] object and echoed in
## printer.ifs.tool_map, the shape a status subscriber parses. These pin the
## contracts around it: the table starts as the identity and only
## IFS_MAP_TOOL changes it; a tool may not be aimed at a lane with nothing
## in it; the T macros route through the table while a hand-typed
## IFS_SELECT SLOT= does not; and the table dies with the print, so one
## job's colours cannot silently retarget the next one's.

import json
import pathlib
import types
import unittest

from tests.gcode_macro_harness import render_macro
from tests.test_ifs_macros import GEOMETRY
from tests.test_ifs_klipper import make_ifs, f13
import ifs_klipper_fakes as fakes


ROOT = pathlib.Path(__file__).parents[1]
IFS = ROOT / "macros" / "ifs.cfg"

## The identity: tool n owns lane n+1, which is the numbering the T macros
## had before the map existed.
IDENTITY = {"0": 1, "1": 2, "2": 3, "3": 4}
## silk 0b1011: lanes 1, 2 and 4 hold filament; lane 3 is empty.
LOADED = (1, 2, 4)
EMPTY_LANE = 3


class ToolMapStatusTest(unittest.TestCase):
    """The plugin half: the verb, the validation, the table's lifetime."""

    def make_connected(self):
        obj, printer, _ = make_ifs(replies=[f13(silk=0b1011)])
        obj._connect()
        obj._poll_once()
        return obj, printer

    def map_tool(self, obj, **params):
        return obj.cmd_IFS_MAP_TOOL(fakes.FakeGcmd(params))

    def test_the_default_map_is_the_identity(self):
        obj, _, _ = make_ifs()
        self.assertEqual(obj.get_status()["tool_map"], IDENTITY)

    def test_the_table_is_json_with_string_keys_and_int_lanes(self):
        """The wire shape, pinned at the source.

        A status consumer parses this dict after JSON serialization, and a
        JSON object's keys are strings whatever Python held, so the keys are
        strings here too - what we publish and what a WebSocket client sees
        are then one and the same shape. The lanes are the 1-based numbers
        IFS_LOAD SLOT= and loaded_channels speak, never tool indices.
        """
        obj, _ = self.make_connected()
        self.map_tool(obj, TOOL=2, SLOT=4)
        table = obj.get_status()["tool_map"]
        self.assertEqual(table, {"0": 1, "1": 2, "2": 4, "3": 4})
        self.assertEqual(json.loads(json.dumps(table)), table)
        self.assertTrue(all(isinstance(key, str) for key in table))
        self.assertTrue(all(isinstance(lane, int) for lane in table.values()))

    def test_mapping_a_tool_changes_only_that_entry(self):
        obj, _ = self.make_connected()
        self.map_tool(obj, TOOL=0, SLOT=4)
        self.assertEqual(obj.get_status()["tool_map"],
                         {"0": 4, "1": 2, "2": 3, "3": 4})

    def test_a_tool_cannot_be_aimed_at_an_empty_lane(self):
        obj, _ = self.make_connected()
        gcmd = fakes.FakeGcmd({"TOOL": 0, "SLOT": EMPTY_LANE})
        with self.assertRaises(gcmd.error) as caught:
            obj.cmd_IFS_MAP_TOOL(gcmd)
        self.assertIn("lane %d" % EMPTY_LANE, str(caught.exception))
        self.assertEqual(obj.get_status()["tool_map"], IDENTITY)

    def test_without_a_reading_no_lane_can_be_verified(self):
        """Before the first F13 nothing is known about any lane.

        Accepting the mapping anyway would make an unconnected start the one
        path that skips the empty-lane refusal.
        """
        obj, _, _ = make_ifs()
        gcmd = fakes.FakeGcmd({"TOOL": 0, "SLOT": 1})
        with self.assertRaises(gcmd.error):
            obj.cmd_IFS_MAP_TOOL(gcmd)
        self.assertEqual(obj.get_status()["tool_map"], IDENTITY)

    def test_RESET_restores_the_identity(self):
        obj, _ = self.make_connected()
        self.map_tool(obj, TOOL=1, SLOT=4)
        self.map_tool(obj, RESET=1)
        self.assertEqual(obj.get_status()["tool_map"], IDENTITY)

    def test_the_tool_and_lane_numbers_are_checked(self):
        obj, _ = self.make_connected()
        for params in ({"TOOL": 4, "SLOT": 1}, {"TOOL": -1, "SLOT": 1},
                       {"TOOL": 0, "SLOT": 0}, {"TOOL": 0, "SLOT": 5}):
            with self.subTest(params=params):
                gcmd = fakes.FakeGcmd(params)
                with self.assertRaises(gcmd.error):
                    obj.cmd_IFS_MAP_TOOL(gcmd)
        self.assertEqual(obj.get_status()["tool_map"], IDENTITY)

    def test_the_map_dies_with_the_print_not_with_a_pause(self):
        """A pause resumes the job it interrupted.

        Clearing there would retarget every tool change after a RESUME; only
        a state that ends the print may clear the table.
        """
        obj, _ = self.make_connected()
        self.map_tool(obj, TOOL=0, SLOT=4)
        remapped = {"0": 4, "1": 2, "2": 3, "3": 4}
        obj._note_print_state("printing")
        obj._note_print_state("paused")
        self.assertEqual(obj.get_status()["tool_map"], remapped)
        for ended in ("complete", "cancelled", "error", "standby"):
            with self.subTest(state=ended):
                self.map_tool(obj, TOOL=0, SLOT=4)
                obj._note_print_state("printing")
                obj._note_print_state(ended)
                self.assertEqual(obj.get_status()["tool_map"], IDENTITY)

    def test_the_watcher_reads_print_stats_state(self):
        """The timer is the print-end detector; these are its wiring.

        print_stats on stock klipper sends no event at all (kalico's
        print_stats:*_printing events are fork additions), so the map's
        lifetime is watched by polling the state on the reactor.
        """
        obj, printer = self.make_connected()
        states = ["printing"]
        printer.add_object("print_stats", types.SimpleNamespace(
            get_status=lambda eventtime: {"state": states[0]}))
        printer.fire("klippy:ready")
        self.map_tool(obj, TOOL=0, SLOT=4)
        watchers = [timer for timer in printer.reactor.timers
                    if timer.__name__ == "_watch_print_state"]
        self.assertEqual(len(watchers), 1)
        watchers[0](100.0)
        self.assertEqual(obj.get_status()["tool_map"],
                         {"0": 4, "1": 2, "2": 3, "3": 4})
        states[0] = "complete"
        watchers[0](101.0)
        self.assertEqual(obj.get_status()["tool_map"], IDENTITY)


class ToolMacroTest(unittest.TestCase):
    """The macro half: T<n> loads the lane the table aims it at."""

    def render_tool(self, name, tool_map):
        return render_macro(IFS, name,
                            printer={"ifs": {"tool_map": tool_map}}).commands

    def test_the_identity_maps_each_tool_to_its_own_lane(self):
        for tool, lane in (("T0", 1), ("T1", 2), ("T2", 3), ("T3", 4)):
            with self.subTest(tool=tool):
                self.assertEqual(self.render_tool(tool, IDENTITY),
                                 ("IFS_SELECT SLOT=%d" % lane,))

    def test_a_remapped_table_redirects_the_tool(self):
        swapped = {"0": 4, "1": 3, "2": 2, "3": 1}
        for tool, lane in (("T0", 4), ("T1", 3), ("T2", 2), ("T3", 1)):
            with self.subTest(tool=tool):
                self.assertEqual(self.render_tool(tool, swapped),
                                 ("IFS_SELECT SLOT=%d" % lane,))


class DirectSlotBypassTest(unittest.TestCase):
    """IFS_SELECT SLOT= names a lane, and the map does not get a vote.

    The console and the host's shared verbs (LOAD_FILAMENT and friends)
    resolve a slot themselves; routing those through the table would turn a
    hand that said "slot 2" into whatever a print job last mapped.
    """

    def host(self, tool_map):
        return {
            "ifs": {"tool_map": tool_map},
            "save_variables": {"variables": {"ifs_loaded": 0}},
            "print_stats": {"state": "standby"},
            "gcode_move": {"gcode_position": {"x": 100.0, "y": 90.0,
                                              "z": 5.0}},
            "toolhead": {"homed_axes": "xyz"},
            "gcode_macro _IFS_GEOMETRY": GEOMETRY,
        }

    def test_a_direct_slot_is_not_translated(self):
        swapped = {"0": 4, "1": 3, "2": 2, "3": 1}
        commands = render_macro(IFS, "IFS_SELECT", printer=self.host(swapped),
                                params={"SLOT": 2}).commands
        loads = [c for c in commands if c.startswith("IFS_LOAD")]
        self.assertEqual(loads, ["IFS_LOAD SLOT=2"], commands)


if __name__ == "__main__":
    unittest.main()
