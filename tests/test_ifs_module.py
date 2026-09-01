## The standalone-module gate.
##
## ifs.cfg and the ifs_*.py plugins are meant to drop onto any base klipper,
## not just this fork: the same files must run on a zmod image or a future
## base image that has none of the fork's macros. These tests hold that
## property: every macro renders on a host WITHOUT the fork's conveniences,
## every plugin import stays inside the module family, and the host file
## includes the module rather than duplicating it.
##
## Copyright (C) 2026, Preston Brown
##
## This file may be distributed under the terms of the GNU GPLv3 license

import pathlib
import re
import unittest

from tests.gcode_macro_harness import render_macro
from tests.test_ifs_macros import GEOMETRY


ROOT = pathlib.Path(__file__).parents[1]
MODULE = ROOT / "macros" / "ifs.cfg"
HOST = ROOT / "macros" / "hw_base.ad5x.cfg"
PLUGINS = ROOT / ".py" / "klipper" / "plugins"

## Conveniences this fork provides and a bare host does not. None of these
## may reach the serial line on a host that lacks them.
FORK_ONLY = ("MOVE_SAFE", "_CLIENT_VARIABLE", "LOAD_CELL_TARE")

## What every macro renders against: core klipper objects and the module's
## own, nothing else. No fan_generic, no MOVE_SAFE macro, no client variables.
BARE_HOST = {
    "ifs": {"connected": True, "error": None, "loaded_channels": [1, 2, 4],
            "tool_map": {"0": 1, "1": 2, "2": 3, "3": 4},
            "params": {"tube_mm": 1000.0, "ifs_speed": 1200.0,
              "ifs_fast_speed": 3600.0, "approach_mm": 150.0,
                       "purge_extra_mm": 90.0, "first_purge_mm": 100.0,
                       "first_purge_speed": 300.0, "first_fan": 0.0,
                       "second_purge_mm": 30.0, "second_purge_speed": 300.0,
                       "second_fan": 255.0, "unload_extruder_mm": 60.0,
                       "unload_ifs_mm": 70.0, "unload_speed": 600.0,
                       "cut_before_mm": 0.0, "cut_after_mm": 5.0,
                       "load_empty_mm": 600.0, "autoinsert_ret_mm": 90.0,
                       "hub_clear_mm": 300.0}},
    "ifs_materials": {"slots": {}, "purge_first_mm": {}},
    "extruder": {"target": 220.0, "temperature": 220.0},
    "filament_switch_sensor toolhead": {"filament_detected": False,
                                        "enabled": True},
    "save_variables": {"variables": {"ifs_loaded": 4, "ifs_at_hub": 0}},
    "print_stats": {"state": "printing"},
    "gcode_move": {"gcode_position": {"x": 100.0, "y": 90.0, "z": 5.0}},
    "toolhead": {"homed_axes": "xyz"},
    "fan": {"speed": 0.6},
    "gcode_macro _IFS_SENSOR_HOLD": {"was_enabled": 1},
    "gcode_macro _IFS_GEOMETRY": GEOMETRY,
    "gcode_macro _IFS_CUT": {"cut_x": -2.5, "cut_y": -7.5, "clear_x": 20.0},
    ## _IFS_RESTORE_AFTER_CHANGE reads what IFS_SELECT saved; give it a real
    ## save rather than the refusal sentinel.
    "gcode_macro IFS_SELECT": {"restore_x": 100.0, "restore_y": 90.0,
                               "restore_z": 5.0, "restore_temp": 205.0,
                               "restore_fan": 0.6},
}

## Params without which a render is not a render: the macro's own guards
## would refuse, or a missing value cannot be floated.
REQUIRED_PARAMS = {
    "IFS_SELECT": {"SLOT": 1},
    "IFS_LOAD": {"SLOT": 1},
    "IFS_AUTOINSERT": {"CHANNEL": 2},
    "_IFS_GOTO_STATION": {"X": 55.0},
    "_IFS_PURGE": {"SLOT": 1},
    "_IFS_CLEAR_EXTRUDER": {"TEMP": 220},
    "_IFS_PART_FAN": {"S": 255},
}

## The module's own plugins, plus the two things a plugin may reach for:
## FlashForge's settings file (a data format, not a fork dependency) and
## stock klipper's filament_switch_sensor, which sits in the same extras
## directory on every klipper.
MODULE_IMPORTS = frozenset([
    "flashforge_config", "filament_switch_sensor",
    "ifs", "ifs_channel_sensor", "ifs_diagnostics", "ifs_link",
    "ifs_materials", "ifs_operations", "ifs_sensor_base", "ifs_sensor_logic",
    "ifs_sequences", "ifs_status", "ifs_toolhead_sensor",
])


def macro_names(path=MODULE):
    """Every [gcode_macro NAME] the module defines."""
    text = pathlib.Path(path).read_text(encoding="utf-8")
    return re.findall(r"^\s*\[gcode_macro\s+([^\]]+)\]",
                      text, re.MULTILINE)


class BareHostTest(unittest.TestCase):
    def test_every_macro_renders_without_the_forks_conveniences(self):
        """The module on a host that has no MOVE_SAFE, no client variables,
        no named part fan. A template that reaches for any of them raises at
        render time - which on a real host is a refused command, and on this
        fork's console path a shutdown.
        """
        names = macro_names()
        self.assertGreater(len(names), 20, names)
        rendered = {}
        for name in names:
            rendered[name] = render_macro(
                MODULE, name, printer=BARE_HOST,
                params=REQUIRED_PARAMS.get(name, {})).commands
        ## Variable holders (_IFS_GEOMETRY) legitimately render nothing; the
        ## gate is that every macro RENDERS. This count catches a names list
        ## that silently went vacuous.
        self.assertGreater(
            len([commands for commands in rendered.values() if commands]),
            20, sorted(rendered))
        return rendered

    def test_no_fork_only_command_reaches_the_stream(self):
        for name in macro_names():
            commands = render_macro(
                MODULE, name, printer=BARE_HOST,
                params=REQUIRED_PARAMS.get(name, {})).commands
            for command in commands:
                for forbidden in FORK_ONLY:
                    self.assertNotIn(forbidden, command,
                                     (name, command))

    def test_the_lift_falls_back_to_core_commands(self):
        ## The one place a bare host would notice: the change entry still
        ## lifts, without MOVE_SAFE there to do it.
        commands = render_macro(MODULE, "IFS_SELECT", printer=BARE_HOST,
                                params={"SLOT": 1}).commands
        self.assertIn("G91", commands, commands)
        self.assertIn("G1 Z5.0 F3000", commands, commands)

    def test_the_part_fan_falls_back_to_m106(self):
        commands = render_macro(MODULE, "_IFS_PART_FAN", printer=BARE_HOST,
                                params={"S": 255}).commands
        self.assertEqual(commands, ("M106 S255",))


def split_outside_braces(line):
    """Whitespace-split that keeps {...} interpolation groups atomic.

    A written `MINIMUM={(temp - 2)|int}` renders to one value before klipper
    ever sees the line, so the spaces inside the braces are not argument
    separators and must not read as separate tokens.
    """
    tokens, depth, current = [], 0, ""
    for char in line:
        if char == "{":
            depth += 1
        elif char == "}":
            depth = max(0, depth - 1)
        if char.isspace() and depth == 0:
            if current:
                tokens.append(current)
                current = ""
        else:
            current += char
    if current:
        tokens.append(current)
    return tokens


class ModuleShapeTest(unittest.TestCase):
    def test_every_extended_command_argument_is_key_value(self):
        """Multi-character commands take KEY=VALUE arguments, always.

        Anything not shaped like G1/M106/T0 goes through klipper's extended
        parser, which rejects bare word arguments outright - and on a
        console-driven host that refusal is a shutdown, not an error dialog.
        Measured twice on the printer: MOVE_SAFE Z5 (missing '=') and
        _IFS_PART_FAN S0 both died there. Quoted lines are skipped: a RESPOND
        message body may contain anything. Both gcode-bearing files are
        scanned: the module and the host file that carries its platform
        overrides.
        """
        traditional = re.compile(r"^[A-Za-z][0-9]*$")
        bad = []
        for label, path in (("module", MODULE), ("host", HOST)):
            for number, line in enumerate(
                    path.read_text(encoding="utf-8").splitlines(), 1):
                stripped = line.strip()
                if (not stripped or stripped[0] in "#{;*"
                        or '"' in stripped or "'" in stripped):
                    continue
                tokens = split_outside_braces(stripped)
                if (traditional.match(tokens[0])
                        or not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$",
                                        tokens[0])):
                    continue
                for token in tokens[1:]:
                    if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", token):
                        bad.append(("%s:%d" % (label, number), stripped))
                        break
        self.assertEqual(bad, [])

    def test_the_host_file_includes_the_module(self):
        self.assertIn("[include ifs.cfg]",
                      HOST.read_text(encoding="utf-8"))

    def test_renames_target_builtins_never_shared_macros(self):
        """rename_existing parks an existing BUILTIN command under a new
        name (TONE to _TONE, M24 to M24.1, SET_LED to _SET_LED). It cannot
        override another gcode_macro: klippy merges same-named macro
        sections, the merged object takes the rename path, the original
        never registers, and klippy dies at connect demanding a command
        nobody defined. Measured on the printer across three macro families
        before the rule was pinned. Macro-over-macro overrides are plain
        same-named sections in the platform file; this gate bans every
        other shape of attempt.
        """
        shared = set()
        for path in (ROOT / "macros" / "base.cfg",
                     ROOT / "config" / "material.cfg",
                     ROOT / "macros" / "client.cfg",
                     ROOT / "macros" / "headless.cfg"):
            for line in path.read_text(encoding="utf-8").splitlines():
                header = re.match(r"^\[gcode_macro (\S+)\]", line.strip())
                if header:
                    shared.add(header.group(1))
        bad = []
        for label, path in (("module", MODULE), ("host", HOST)):
            macro = None
            for number, line in enumerate(
                    path.read_text(encoding="utf-8").splitlines(), 1):
                header = re.match(r"^\[gcode_macro (\S+)\]", line.strip())
                if header:
                    macro = header.group(1)
                    continue
                if re.match(r"^rename_existing:", line.strip()):
                    if macro is not None and macro in shared:
                        bad.append(("%s:%d" % (label, number), macro))
                    macro = None
        self.assertEqual(bad, [])

    def test_the_shared_filament_verbs_route_to_the_ifs_lanes(self):
        """config/material.cfg's LOAD/UNLOAD/PURGE_FILAMENT are extruder-only
        moves. Every shared caller of them - M600's change prompt,
        LOAD_MATERIAL's action menu, slicer stop-gcode, typed gcode - must
        land in the lane-aware IFS implementations instead, without the
        caller knowing an IFS is fitted.
        """
        text = HOST.read_text(encoding="utf-8")
        for verb in ("LOAD_FILAMENT", "UNLOAD_FILAMENT", "PURGE_FILAMENT"):
            self.assertIn("[gcode_macro %s]" % verb, text)
        for target in ("IFS_LOAD", "IFS_UNLOAD", "IFS_PURGE"):
            self.assertIn(target, text)

    def test_the_module_declares_its_own_sections(self):
        text = MODULE.read_text(encoding="utf-8")
        for section in ("[ifs]", "[save_variables]", "[ifs_materials]",
                        "[ifs_toolhead_sensor toolhead]"):
            self.assertIn(section, text)

    def test_every_plugin_import_stays_inside_the_module(self):
        """No ifs_*.py may import a fork plugin - that is the difference
        between a drop-in module and a fork feature.
        """
        offenders = []
        for path in sorted(PLUGINS.glob("ifs*.py")):
            for line in path.read_text(encoding="utf-8").splitlines():
                match = re.match(r"from \. import (\w+)", line)
                if match and match.group(1) not in MODULE_IMPORTS:
                    offenders.append((path.name, line.strip()))
                match = re.match(r"from \.(\w+) import", line)
                if match and match.group(1) not in MODULE_IMPORTS:
                    offenders.append((path.name, line.strip()))
        self.assertEqual(offenders, [])


if __name__ == "__main__":
    unittest.main()
