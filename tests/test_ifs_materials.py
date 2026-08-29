## Tests for the printer's own settings file and the IFS materials view of it.
##
## The fixture is the real shape of /usr/prog/config/Adventurer5M.json as read
## off an AD5X, trimmed to the sections that matter here. It is STOCK FlashForge
## state shared with the stock UI, which is why the write path is tested for
## preserving everything it does not own.
##
## Copyright (C) 2026, Preston Brown
##
## This file may be distributed under the terms of the GNU GPLv3 license

import json
import os
import pathlib
import tempfile
import types
import unittest

import ifs_modules
import ifs_klipper_fakes as fakes

FF = ifs_modules.load("flashforge_config")
MATERIALS = ifs_modules.load("ifs_materials")


## Verbatim shape from the rig: lanes 1/2/4 filled, lane 3 empty, and a pile of
## unrelated sections that a write must not disturb.
FIXTURE = {
    "FFMInfo": {
        "MileageEnable": False, "OdometerEnable": False, "channel": 4,
        "ffmColor0": "", "ffmColor1": "#A03CF7", "ffmColor2": "#898989",
        "ffmColor3": "", "ffmColor4": "#FFFFFF", "ffmEnable": True,
        "ffmType0": "?", "ffmType1": "PLA", "ffmType2": "ABS",
        "ffmType3": "?", "ffmType4": "PLA",
    },
    "Multicolour": {
        "FristESpace": 100, "FristESpeed": 300, "FristFanSpeed": 0,
        "SecondESpace": 30, "SecondESpeed": 300, "SecondFanSpeed": 255,
        "UnloadESpace": 60, "UnloadIFSSpace": 70, "UnloadSpeed": 600,
    },
    "general": {"FilamentSenserMax": 0.7, "FilamentSenserMin": 0.3,
                "brightNess": 100, "buzzerStatus": True},
    "network": {"ethernetStatus": True},
    "webConsole": {"userID": "admin", "userPass": "ff123456"},
}


class ConfigFileTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.path = os.path.join(self.dir.name, "Adventurer5M.json")
        with open(self.path, "w") as handle:
            json.dump(FIXTURE, handle)
        self.config = FF.FlashForgeConfig(self.path)

    def read(self):
        with open(self.path) as handle:
            return json.load(handle)


class TestReading(ConfigFileTest):
    def test_channel_count_is_counted_from_the_keys(self):
        """Not read out of FFMInfo.channel, which is ambiguous.

        That field reads 4 on a four-lane machine with lane 4 loaded, which fits
        "the lane count" and "the current lane" equally well - and zmod reads it
        as the current lane. Deriving the count from it would report a single
        slot the day it means what zmod thinks it means.
        """
        self.assertEqual(self.config.channel_count(), 4)

    def test_the_count_survives_channel_meaning_the_current_lane(self):
        ## The disambiguating case: channel says 1, four lanes are described.
        document = self.read()
        document["FFMInfo"]["channel"] = 1
        self.assertEqual(self.config.channel_count(document), 4)
        self.assertEqual(sorted(self.config.materials(document)), [1, 2, 3, 4])

    def test_materials_are_indexed_from_one(self):
        slots = self.config.materials()
        self.assertEqual(sorted(slots), [1, 2, 3, 4])
        self.assertEqual(slots[1], {"type": "PLA", "color": "#A03CF7"})
        self.assertEqual(slots[2], {"type": "ABS", "color": "#898989"})
        self.assertEqual(slots[4], {"type": "PLA", "color": "#FFFFFF"})

    def test_the_printers_empty_markers_become_none(self):
        ## "?" and "" are FlashForge's empties; passing them through would put
        ## a literal question mark in the UI.
        self.assertEqual(self.config.materials()[3],
                         {"type": None, "color": None})

    def test_slot_zero_is_never_a_lane(self):
        self.assertNotIn(0, self.config.materials())

    def test_the_loaded_material_follows_the_named_lane(self):
        """Slot 0 is where a single-material AD5M records this.

        On a machine with an IFS it stays empty, so reading it meant IFS_MATERIALS
        answered "loaded: none" no matter what was actually in the extruder.
        """
        document = self.read()
        document["FFMInfo"]["channel"] = 2
        self.assertEqual(self.config.loaded_material(document),
                         {"type": "ABS", "color": "#898989"})

    def test_with_no_lane_named_it_falls_back_to_slot_zero(self):
        ## An AD5M has no lanes at all, and slot 0 is the whole answer there.
        document = self.read()
        document["FFMInfo"]["channel"] = 0
        self.assertEqual(self.config.loaded_material(document),
                         {"type": None, "color": None})

    def test_enabled_flag(self):
        self.assertTrue(self.config.is_enabled())

    def test_factory_motion_parameters(self):
        ## zmod's "defaults" are these numbers copied out.
        params = self.config.multicolour()
        self.assertEqual(params["UnloadIFSSpace"], 70)
        self.assertEqual(params["UnloadESpace"], 60)
        self.assertEqual(params["FristESpeed"], 300)

    def test_stock_sensor_thresholds_are_exposed_but_not_used(self):
        ## Almost certainly where zmod's 0.3/0.72 came from. Our measured raw
        ## ADC is an order of magnitude below them, so they are reference only.
        self.assertEqual(self.config.stock_sensor_thresholds(), (0.3, 0.7))


class TestWriting(ConfigFileTest):
    def test_setting_a_slot_preserves_everything_else(self):
        ## This file belongs to the stock UI. Losing a section it owns would be
        ## the worst kind of bug: invisible until the printer misbehaves.
        self.config.set_material(3, "PETG", "#00FF00")
        document = self.read()
        self.assertEqual(document["webConsole"], FIXTURE["webConsole"])
        self.assertEqual(document["Multicolour"], FIXTURE["Multicolour"])
        self.assertEqual(document["network"], FIXTURE["network"])
        self.assertEqual(document["FFMInfo"]["ffmType1"], "PLA")
        self.assertEqual(document["FFMInfo"]["channel"], 4)

    def test_setting_a_slot_writes_both_fields(self):
        self.config.set_material(3, "PETG", "#00FF00")
        self.assertEqual(self.config.materials()[3],
                         {"type": "PETG", "color": "#00FF00"})

    def test_clearing_a_slot_uses_the_printers_own_markers(self):
        ## An emptied slot must look to the stock UI exactly like one it
        ## emptied itself.
        self.config.set_material(1, None, None)
        info = self.read()["FFMInfo"]
        self.assertEqual(info["ffmType1"], "?")
        self.assertEqual(info["ffmColor1"], "")

    def test_the_write_is_atomic(self):
        self.config.set_material(2, "TPU", "#123456")
        leftovers = [n for n in os.listdir(self.dir.name)
                     if n.startswith(".ffconfig-")]
        self.assertEqual(leftovers, [])

    def test_a_failed_write_leaves_no_temp_file(self):
        def explode(document):
            raise RuntimeError("boom")
        with self.assertRaises(RuntimeError):
            self.config.update(explode)
        leftovers = [n for n in os.listdir(self.dir.name)
                     if n.startswith(".ffconfig-")]
        self.assertEqual(leftovers, [])
        self.assertEqual(self.read(), FIXTURE)

    def test_load_quietly_on_a_missing_file(self):
        self.assertIsNone(FF.load_quietly(self.path + ".nope"))


class TestBootstrap(unittest.TestCase):
    """An image without FlashForge's settings file still gets a registry.

    The module claims to run on any base klipper; a host whose stock UI never
    existed has no Adventurer5M.json, and IFS_SET_MATERIAL is the only way
    to tell it what a slot holds. A plain miss starts the file; a file that
    exists but will not read is never overwritten.
    """

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.path = os.path.join(self.dir.name, "Adventurer5M.json")
        self.obj, self.printer = make_materials(self.path)

    def test_set_material_starts_the_file_when_it_is_absent(self):
        self.obj.cmd_IFS_SET_MATERIAL(
            fakes.FakeGcmd({"SLOT": 1, "TYPE": "PLA", "COLOR": "FF0000"}))
        self.assertEqual(self.obj.material(1),
                         {"type": "PLA", "color": "#FF0000"})
        with open(self.path) as handle:
            self.assertEqual(json.load(handle)["FFMInfo"]["ffmType1"], "PLA")

    def test_the_slot_range_check_has_no_answer_without_the_file(self):
        ## Not a crash: nothing is countable yet, so every slot is plausible.
        self.obj.cmd_IFS_SET_MATERIAL(
            fakes.FakeGcmd({"SLOT": 4, "TYPE": "PETG", "COLOR": "101010"}))

    def test_a_file_that_exists_but_will_not_read_is_never_overwritten(self):
        with open(self.path, "w") as handle:
            handle.write("{not json")
        with self.assertRaises(fakes.FakeGcmd.error):
            self.obj.cmd_IFS_SET_MATERIAL(
                fakes.FakeGcmd({"SLOT": 1, "TYPE": "PLA"}))
        with open(self.path) as handle:
            self.assertEqual(handle.read(), "{not json")


def make_materials(path):
    printer = fakes.FakePrinter()
    config = fakes.FakeConfig("ifs_materials", {"path": path}, printer)
    return MATERIALS.IfsMaterials(config), printer


class TestIfsMaterialsObject(ConfigFileTest):
    def setUp(self):
        ConfigFileTest.setUp(self)
        self.obj, self.printer = make_materials(self.path)

    def test_the_live_record_wins_over_the_stale_one(self):
        """FlashForge's FFMInfo.channel is not maintained under Forge-X.

        Nothing writes it once the stock UI is gone, so it is whatever it was
        the last time stock ran - correct until the first tool change, then
        quietly wrong. save_variables is the live record.
        """
        class FakeSaveVariables:
            allVariables = {"ifs_loaded": 2}

        self.printer.add_object("save_variables", FakeSaveVariables())
        ## The file says lane 4 (white); we know it is lane 2 (grey ABS).
        self.assertEqual(self.obj.get_status()["loaded"],
                         {"type": "ABS", "color": "#898989", "temp": 250.0})

    def test_with_no_record_it_falls_back_to_the_file(self):
        ## An IFS that has never been driven, or a machine with no
        ## save_variables at all, still gets FlashForge's answer.
        self.assertEqual(self.obj.get_status()["loaded"],
                         {"type": "PLA", "color": "#FFFFFF", "temp": 220.0})

    def test_a_record_of_nothing_loaded_is_not_mistaken_for_a_lane(self):
        class FakeSaveVariables:
            allVariables = {"ifs_loaded": 0}

        self.printer.add_object("save_variables", FakeSaveVariables())
        self.assertEqual(self.obj.get_status()["loaded"],
                         {"type": "PLA", "color": "#FFFFFF", "temp": 220.0})

    def test_status_shape(self):
        info = self.obj.get_status()
        self.assertTrue(info["available"])
        self.assertEqual(info["channel_count"], 4)
        self.assertTrue(info["enabled"])
        ## Moonraker serialises keys as strings; be explicit rather than let a
        ## consumer discover it.
        self.assertEqual(sorted(info["slots"]), ["1", "2", "3", "4"])
        self.assertEqual(info["slots"]["2"]["type"], "ABS")

    def test_an_unreadable_file_is_reported_not_raised(self):
        obj, _ = make_materials(self.path + ".nope")
        info = obj.get_status()
        self.assertFalse(info["available"])
        self.assertEqual(info["slots"], {})

    def test_the_cache_follows_the_file(self):
        ## The stock UI writes this file too, so a cache that never expires
        ## would go stale the moment someone uses the panel.
        self.assertEqual(self.obj.material(1)["type"], "PLA")
        document = self.read()
        document["FFMInfo"]["ffmType1"] = "PETG"
        os.utime(self.path, None)
        with open(self.path, "w") as handle:
            json.dump(document, handle)
        self.assertEqual(self.obj.material(1)["type"], "PETG")

    def test_set_material_command(self):
        gcmd = fakes.FakeGcmd({"SLOT": 3, "TYPE": "PETG", "COLOR": "#00FF00"})
        self.obj.cmd_IFS_SET_MATERIAL(gcmd)
        self.assertEqual(self.obj.material(3),
                         {"type": "PETG", "color": "#00FF00"})

    def test_setting_only_the_colour_keeps_the_type(self):
        gcmd = fakes.FakeGcmd({"SLOT": 1, "COLOR": "#111111"})
        self.obj.cmd_IFS_SET_MATERIAL(gcmd)
        self.assertEqual(self.obj.material(1),
                         {"type": "PLA", "color": "#111111"})

    def test_a_bare_hex_colour_is_normalised(self):
        ## '#' cannot survive klipper's gcode parser - it starts a comment, so
        ## COLOR=#FF8800 arrives as COLOR= (empty) and COLOR="#FF8800" is a
        ## malformed command. The spellable form is bare hex; storage keeps
        ## the '#' prefix that the rest of the world uses.
        gcmd = fakes.FakeGcmd({"SLOT": 1, "COLOR": "101010"})
        self.obj.cmd_IFS_SET_MATERIAL(gcmd)
        self.assertEqual(self.obj.material(1),
                         {"type": "PLA", "color": "#101010"})
        gcmd = fakes.FakeGcmd({"SLOT": 1, "COLOR": "F80"})
        self.obj.cmd_IFS_SET_MATERIAL(gcmd)
        self.assertEqual(self.obj.material(1)["color"], "#F80")

    def test_a_bad_colour_is_refused(self):
        gcmd = fakes.FakeGcmd({"SLOT": 1, "COLOR": "green"})
        with self.assertRaises(fakes.FakeGcmd.error):
            self.obj.cmd_IFS_SET_MATERIAL(gcmd)
        self.assertEqual(self.obj.material(1)["color"], "#A03CF7")

    def test_a_slot_beyond_the_printers_channels_is_refused(self):
        gcmd = fakes.FakeGcmd({"SLOT": 9, "TYPE": "PLA"})
        with self.assertRaises(fakes.FakeGcmd.error):
            self.obj.cmd_IFS_SET_MATERIAL(gcmd)

    def test_nothing_to_set_is_refused(self):
        gcmd = fakes.FakeGcmd({"SLOT": 1})
        with self.assertRaises(fakes.FakeGcmd.error):
            self.obj.cmd_IFS_SET_MATERIAL(gcmd)

    def test_report_command_lists_every_slot(self):
        gcmd = fakes.FakeGcmd({})
        self.obj.cmd_IFS_MATERIALS(gcmd)
        text = "\n".join(gcmd.responses)
        self.assertIn("PLA", text)
        self.assertIn("ABS", text)
        self.assertIn("empty", text)

    def test_both_commands_are_registered(self):
        gcode = self.printer.lookup_object("gcode")
        self.assertIn("IFS_MATERIALS", gcode.commands)
        self.assertIn("IFS_SET_MATERIAL", gcode.commands)


class TestHandlingTemperatures(ConfigFileTest):
    """The one thing zmod varies by material: what to heat to in order to move it.

    Everything else - purge lengths, feed speeds, tube length - is one global
    set in zmod too, so this table is the whole of per-material behaviour.
    """

    def setUp(self):
        ConfigFileTest.setUp(self)
        self.obj, self.printer = make_materials(self.path)

    def test_each_material_gets_its_own_number(self):
        self.assertEqual(self.obj.temperature("PLA"), 220.0)
        self.assertEqual(self.obj.temperature("PETG"), 250.0)
        self.assertEqual(self.obj.temperature("ABS"), 250.0)
        self.assertEqual(self.obj.temperature("TPU"), 230.0)

    def test_the_lookup_is_case_insensitive(self):
        ## The stock UI writes "PLA"; a human typing IFS_SET_MATERIAL TYPE=pla
        ## should not silently lose the temperature.
        self.assertEqual(self.obj.temperature("pla"), 220.0)
        self.assertEqual(self.obj.temperature("PeTg-Cf"), 250.0)

    def test_an_unknown_material_is_unknown_not_pla(self):
        """zmod substitutes PLA here, which runs ABS at 220 and snaps it off.

        Answering None instead makes the caller insist on an explicit TEMP=,
        which is the difference between a refusal and a broken heatbreak.
        """
        self.assertIsNone(self.obj.temperature("ASA"))
        self.assertIsNone(self.obj.temperature(None))
        self.assertIsNone(self.obj.temperature(""))

    def test_a_slot_carries_its_materials_temperature(self):
        slots = self.obj.get_status()["slots"]
        self.assertEqual(slots["1"]["temp"], 220.0)     # PLA
        self.assertEqual(slots["2"]["temp"], 250.0)     # ABS
        self.assertIsNone(slots["3"]["temp"])           # empty

    def test_the_whole_table_is_published(self):
        ## So a UI can offer the list, and so IFS_MATERIALS is not the only way
        ## to find out what this printer will accept.
        table = self.obj.get_status()["temperatures"]
        self.assertEqual(table["SILK"], 230.0)
        self.assertEqual(sorted(table), sorted(MATERIALS.TEMPERATURES))

    def test_the_table_survives_an_unreadable_config_file(self):
        ## The materials file and the temperature table are independent: losing
        ## the printer's own JSON must not also lose the lookup.
        obj, _ = make_materials(self.path + ".nope")
        info = obj.get_status()
        self.assertFalse(info["available"])
        self.assertEqual(info["temperatures"]["PLA"], 220.0)

    def test_config_can_add_a_material(self):
        printer = fakes.FakePrinter()
        config = fakes.FakeConfig("ifs_materials",
                                  {"path": self.path, "temp_ASA": 260},
                                  printer)
        obj = MATERIALS.IfsMaterials(config)
        self.assertEqual(obj.temperature("ASA"), 260.0)
        ## and the built-ins are still there
        self.assertEqual(obj.temperature("PLA"), 220.0)

    def test_config_can_override_a_built_in(self):
        printer = fakes.FakePrinter()
        config = fakes.FakeConfig("ifs_materials",
                                  {"path": self.path, "temp_PLA": 205},
                                  printer)
        obj = MATERIALS.IfsMaterials(config)
        self.assertEqual(obj.temperature("PLA"), 205.0)

    def test_a_lowercase_config_key_still_matches(self):
        ## Klipper lowercases option names, so `temp_PLA:` arrives as `temp_pla`
        ## and an override that only matched the written case would never fire.
        printer = fakes.FakePrinter()
        config = fakes.FakeConfig("ifs_materials",
                                  {"path": self.path, "temp_pla": 205},
                                  printer)
        obj = MATERIALS.IfsMaterials(config)
        self.assertEqual(obj.temperature("PLA"), 205.0)

    def test_the_report_names_the_temperature(self):
        gcmd = fakes.FakeGcmd({})
        self.obj.cmd_IFS_MATERIALS(gcmd)
        text = "\n".join(gcmd.responses)
        self.assertIn("@ 220C", text)
        self.assertIn("@ 250C", text)

    def test_the_report_says_nothing_for_an_unlabelled_slot(self):
        ## Slot 3 is empty; "empty @ 220C" would be a lie about a lane that has
        ## no filament in it at all.
        gcmd = fakes.FakeGcmd({})
        self.obj.cmd_IFS_MATERIALS(gcmd)
        line = [r for r in gcmd.responses if r.strip().startswith("slot 3")]
        self.assertEqual(len(line), 1, gcmd.responses)
        self.assertNotIn("@", line[0])


class TestColourDistance(unittest.TestCase):
    def test_identical_colours_are_zero(self):
        self.assertEqual(
            MATERIALS.colour_distance("#A03CF7", "#A03CF7"), 0.0)

    def test_black_and_white_is_one(self):
        self.assertEqual(MATERIALS.colour_distance("#000000", "#FFFFFF"), 1.0)

    def test_an_unknown_colour_on_either_side_is_none(self):
        ## None is the caller's cue to purge the full length; it must never be
        ## mistaken for "close to everything" (a small distance).
        for pair in ((None, "#FFFFFF"), ("#FFFFFF", None),
                     ("", "#FFFFFF"), ("#FFFFFF", "not-a-colour")):
            self.assertIsNone(MATERIALS.colour_distance(*pair), pair)

    def test_three_digit_hex_expands_to_six(self):
        self.assertEqual(MATERIALS.colour_distance("#F80", "#FF8800"), 0.0)

    def test_it_is_symmetric(self):
        self.assertAlmostEqual(MATERIALS.colour_distance("#A03CF7", "#FFFFFF"),
                               MATERIALS.colour_distance("#FFFFFF", "#A03CF7"))


class TestPurgeTable(ConfigFileTest):
    """The first purge pass, scaled by how far apart the colours are.

    Floor 50 mm: the hotend holds ~35 mm, so anything shorter never pushes
    new colour out of the nozzle. Ceiling: the printer's own first_purge_mm.
    """

    def setUp(self):
        ConfigFileTest.setUp(self)
        self.obj, self.printer = make_materials(self.path)
        self.set_full_purge(100.0)

    def set_full_purge(self, mm):
        ## The ceiling is [ifs]'s own setting; the table is built against it.
        self.printer.add_object("ifs", types.SimpleNamespace(
            params=types.SimpleNamespace(first_purge_mm=mm)))

    def table(self):
        return self.obj.get_status()["purge_first_mm"]

    def test_close_colours_bottom_out_at_the_floor(self):
        self.obj.cmd_IFS_SET_MATERIAL(
            fakes.FakeGcmd({"SLOT": 2, "COLOR": "FFFFFF"}))
        ## Slot 4 is #FFFFFF: identical colours in different lanes scale to
        ## exactly the floor - the hotend still has to be filled and flushed.
        ## The literal, not PURGE_FLOOR_MM: 50 is load-bearing (the hotend
        ## holds ~35 mm), so changing it must be a conscious decision that
        ## walks past this number.
        self.assertAlmostEqual(self.table()["2>4"], 50.0, places=6)

    def test_opposite_colours_get_the_full_length(self):
        self.obj.cmd_IFS_SET_MATERIAL(
            fakes.FakeGcmd({"SLOT": 2, "COLOR": "000000"}))
        self.assertAlmostEqual(self.table()["2>4"], 100.0, places=2)

    def test_mid_colours_land_between_the_two(self):
        ## Grey #898989 to white #FFFFFF: neither floor nor ceiling. Literals
        ## for the same reason as the floor test - a mutation of the constant
        ## must move the answer out of this window, not the window with it.
        self.assertGreater(self.table()["2>4"], 55.0)
        self.assertLess(self.table()["2>4"], 99.0)

    def test_the_ordering_follows_the_distance(self):
        self.obj.cmd_IFS_SET_MATERIAL(
            fakes.FakeGcmd({"SLOT": 2, "COLOR": "000000"}))
        self.obj.cmd_IFS_SET_MATERIAL(
            fakes.FakeGcmd({"SLOT": 3, "COLOR": "FEFEFE"}))
        table = self.table()
        self.assertLess(table["3>4"], table["1>4"], table)   # near-white
        self.assertLess(table["1>4"], table["2>4"], table)   # violet
        self.assertGreater(table["2>4"], table["3>4"], table)  # black

    def test_an_unknown_colour_pair_is_absent(self):
        ## Slot 3 is empty in the fixture. Absent means "purge full length"
        ## to the macro - unknown colours are never guessed close.
        table = self.table()
        self.assertNotIn("3>4", table)
        self.assertNotIn("4>3", table)

    def test_a_pair_with_itself_is_not_listed(self):
        ## IFS_SELECT no-ops on the loaded slot; the entry could only mislead.
        self.assertNotIn("1>1", self.table())

    def test_without_an_ifs_object_there_is_no_table(self):
        del self.printer.objects["ifs"]
        self.assertEqual(self.table(), {})

    def test_the_floor_never_rises_above_the_full_setting(self):
        ## A printer configured to purge 40 mm in total must not be scaled UP
        ## to our 50 mm floor by the colour table.
        self.set_full_purge(40.0)
        table = self.table()
        for value in table.values():
            self.assertLessEqual(value, 40.0 + 1e-9, table)

    def test_an_unreadable_file_publishes_an_empty_table(self):
        obj, _ = make_materials(self.path + ".nope")
        self.assertEqual(obj.get_status()["purge_first_mm"], {})


if __name__ == "__main__":
    unittest.main()
