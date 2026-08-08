## Tests for the shared safe Z height.
##
## Copyright (C) 2026, Alexander K <https://github.com/drA1ex>
##
## This file may be distributed under the terms of the GNU GPLv3 license

import json
import pathlib
import re
import unittest


ROOT = pathlib.Path(__file__).parents[1]


class SafeZContractTest(unittest.TestCase):
    def test_filament_change_parks_at_fifty_or_ten_mm_lower(self):
        base = (ROOT / "macros" / "base.cfg").read_text(encoding="utf-8")
        client = (ROOT / "macros" / "client.cfg").read_text(encoding="utf-8")
        declaration = json.loads(
            (ROOT / "mod_params.json").read_text(encoding="utf-8"))

        m600 = base.split("[gcode_macro M600]", 1)[1].split(
            "[gcode_macro", 1)[0]
        pause = client.split("[gcode_macro PAUSE]", 1)[1].split(
            "[gcode_macro", 1)[0]
        park_macro = client.split(
            "[gcode_macro _TOOLHEAD_PARK_PAUSE_CANCEL]", 1)[1].split(
                "[gcode_macro", 1)[0]
        parameter = next(item for item in declaration["parameters"]
                         if item["key"] == "m600_z_min")
        self.assertEqual(parameter["default"], 50.0)
        self.assertIn(
            "printer.mod_params.variables.m600_z_min", m600)
        self.assertIn(
            "PAUSE { X } { Y } { Z } Z_MIN={m600_z_min}", m600)
        self.assertIn("_TOOLHEAD_PARK_PAUSE_CANCEL {rawparams}", pause)
        self.assertIn("params.Z_MIN | default(0) | float", park_macro)
        self.assertIn(
            "[(act.z + park_dz), z_min]|max", park_macro)
        self.assertIn('printer["gcode_macro MOVE_SAFE"]', park_macro)
        self.assertIn(
            "move_limits.z_max_margin | float", park_macro)

        def park(current, limit):
            return min(max(current + 10, 50), limit - 10)

        self.assertEqual(park(45, 230), 55)
        self.assertEqual(park(30, 230), 50)
        self.assertEqual(park(60, 230), 70)
        self.assertEqual(park(215, 230), 220)
        self.assertEqual(park(195, 210), 200)

    def test_macro_safety_moves_use_the_shared_parameter(self):
        base = (ROOT / "macros" / "base.cfg").read_text(encoding="utf-8")
        client = (ROOT / "macros" / "client.cfg").read_text(encoding="utf-8")
        headless = (ROOT / "macros" / "headless.cfg").read_text(
            encoding="utf-8")

        self.assertGreaterEqual(
            base.count("printer.mod_params.variables.safe_z"), 8)
        self.assertIn("printer.mod_params.variables.safe_z", client)
        self.assertIn("[custom_park_dz, safe_z] | max", client)
        self.assertNotIn("variable_custom_park_dz", headless)

        literal_safe_move = re.compile(
            r"^\s*(?:G0|G1|MOVE_SAFE)\b[^\n]*\bZ(?:=)?(?:5|10)(?:\.0+)?\b",
            re.MULTILINE | re.IGNORECASE)
        self.assertEqual(literal_safe_move.findall(base), [])

    def test_python_safety_paths_do_not_restore_literal_five_mm_lifts(self):
        paths = (
            ROOT / ".py" / "klipper" / "plugins" /
            "feather_z_calibration.py",
            ROOT / ".py" / "klipper" / "plugins" / "feather_screen.py",
            ROOT / ".py" / "klipper" / "plugins" / "load_cell_tare.py",
        )
        sources = "\n".join(path.read_text(encoding="utf-8")
                            for path in paths)

        self.assertIsNone(re.search(
            r'MOVE_SAFE Z=[0-9]+(?:\.[0-9]+)? ABSOLUTE=1 F=600', sources))
        self.assertNotIn('"G1 Z10 F6000"', sources)
        self.assertIn('get("safe_z", 10.0)', sources)

if __name__ == "__main__":
    unittest.main()
