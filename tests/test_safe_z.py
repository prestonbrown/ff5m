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
    def test_pause_parks_at_the_shared_minimum_or_the_normal_lift(self):
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
                         if item["key"] == "pause_z_min")
        self.assertEqual(parameter["default"], 50.0)
        # PAUSE owns the minimum park height; M600 inherits it through PAUSE.
        self.assertIn(
            "params.Z_MIN | default(pause_z_min)", pause)
        self.assertNotIn("Z_MIN", m600)
        self.assertIn("params.Z_MIN | default(0) | float", park_macro)
        self.assertIn(
            "[(act.z + park_dz), z_min]|max", park_macro)
        self.assertIn('printer["gcode_macro MOVE_SAFE"]', park_macro)
        self.assertIn(
            "move_limits.z_max_margin | float", park_macro)
        # The park target is a G-code coordinate, so the shared machine
        # ceiling holds only with the Z offset subtracted from it.
        self.assertIn("(max.z - origin.z)", park_macro)

        def park(current, axis_max, offset=0.0):
            z_max = axis_max - 10
            return min(max(current + 10, 50), z_max - offset, z_max)

        # Lower the bed to the minimum, never raise one that is already lower.
        self.assertEqual(park(10, 230), 50)
        self.assertEqual(park(35, 230), 50)
        self.assertEqual(park(45, 230), 55)
        self.assertEqual(park(150, 230), 160)
        self.assertEqual(park(215, 230), 220)
        self.assertEqual(park(195, 210), 200)
        # A Z offset moves the bed no lower than the reachable maximum.
        self.assertEqual(park(215, 230, 2.0) + 2.0, 220)

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
