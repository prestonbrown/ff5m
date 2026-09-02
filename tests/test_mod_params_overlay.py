## mod_params defaults-overlay tests: one board-dependent default without a
## second declaration file to keep in step.
##
## Copyright (C) 2026, Preston Brown
##
## This file may be distributed under the terms of the GNU GPLv3 license

import configparser
import importlib.util
import json
import pathlib
import tempfile
import unittest

from tests.gcode_macro_harness import _read_sections
from tests.ifs_klipper_fakes import FakeConfig, FakePrinter

ROOT = pathlib.Path(__file__).parents[1]
PLUGIN = ROOT / ".py" / "klipper" / "plugins" / "mod_params.py"
HW_AD5X = ROOT / "macros" / "hw_base.ad5x.cfg"

## The slice of the real declaration these tests need: two parameters the
## collision watchdog reads and one it does not.
DECLARATION = {
    "ui": {},
    "parameters": [
        {"key": "weight_check", "type": "bool", "default": 0,
         "label": "Bed collision protection", "order": 10},
        {"key": "weight_check_max", "type": "int", "default": 1200,
         "label": "Bed collision protection, weight", "order": 11},
        {"key": "display", "type": "int", "default": 1,
         "label": "Display mode", "order": 12},
    ],
}


class ModParamsPrinter(FakePrinter):
    class command_error(Exception):
        pass


def load_plugin():
    spec = importlib.util.spec_from_file_location("mod_params", PLUGIN)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class OverlayTestBase(unittest.TestCase):
    def setUp(self):
        self.module = load_plugin()
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.dir = pathlib.Path(directory.name)
        self.declaration = self.dir / "declaration.json"
        self.values = self.dir / "variables.cfg"
        self.declaration.write_text(
            json.dumps(DECLARATION), encoding="utf-8")

    def write_values(self, mapping):
        parser = configparser.ConfigParser()
        parser.add_section("Variables")
        for key, value in mapping.items():
            parser.set("Variables", key, repr(value))
        with open(self.values, "w", encoding="utf-8") as file:
            parser.write(file)

    def build(self, overlay=None, values=None):
        if values is not None:
            self.write_values(values)
        options = {
            "filename": str(self.values),
            "declaration": str(self.declaration),
        }
        if overlay is not None:
            path = self.dir / "overlay.json"
            path.write_text(json.dumps(overlay), encoding="utf-8")
            options["defaults_overlay"] = str(path)
        return self.module.ModParamManagement(FakeConfig(
            "mod_params", options, ModParamsPrinter()))


class DefaultsOverlayTest(OverlayTestBase):
    def test_the_overlay_overrides_only_the_named_default(self):
        manager = self.build(overlay={"weight_check": 1})
        self.assertEqual(manager.variables["weight_check"], 1)
        self.assertEqual(manager.variables["weight_check_max"], 1200)
        self.assertEqual(manager.variables["display"], 1)

    def test_a_stored_value_still_beats_the_overlaid_default(self):
        manager = self.build(
            overlay={"weight_check": 1}, values={"weight_check": 0})
        self.assertEqual(manager.variables["weight_check"], 0)

    def test_an_unknown_key_in_the_overlay_is_an_error(self):
        with self.assertRaises(ValueError):
            self.build(overlay={"weight_chek": 1})

    def test_a_missing_overlay_file_is_a_load_failure(self):
        options = {
            "filename": str(self.values),
            "declaration": str(self.declaration),
            "defaults_overlay": str(self.dir / "absent.json"),
        }
        with self.assertRaises(ModParamsPrinter.command_error):
            self.module.ModParamManagement(FakeConfig(
                "mod_params", options, ModParamsPrinter()))

    def test_without_the_option_nothing_changes(self):
        manager = self.build()
        self.assertEqual(manager.variables["weight_check"], 0)
        self.assertEqual(manager.variables["display"], 1)


class Ad5xOverlayCfgTest(unittest.TestCase):
    def test_hw_ad5x_points_mod_params_at_the_overlay(self):
        matches = [options for name, options in _read_sections(HW_AD5X)
                   if name == "mod_params"]
        self.assertEqual(len(matches), 1)
        self.assertEqual(
            matches[0].get("defaults_overlay"),
            "/opt/config/mod/mod_params.ad5x.json")

    def test_the_shipped_overlay_flips_weight_check_and_nothing_else(self):
        overlay = json.loads(
            (ROOT / "mod_params.ad5x.json").read_text(encoding="utf-8"))
        self.assertEqual(overlay, {"weight_check": 1, "weight_check_max": 2200})
        declared = json.loads(
            (ROOT / "mod_params.json").read_text(encoding="utf-8"))
        keys = {param["key"] for param in declared["parameters"]}
        self.assertLessEqual(set(overlay), keys)


if __name__ == "__main__":
    unittest.main()
