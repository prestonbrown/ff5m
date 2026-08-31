##
## The shared macros are written in the AD5M's frame; the AD5X overrides them.
##
## Copyright (C) 2026, Preston Brown
##
## This file may be distributed under the terms of the GNU GPLv3 license
##
## The AD5M is centre-origin: X and Y run -110..110 with the plate's centre at
## 0,0. The AD5X is corner-origin: 0..220 with 0,0 at the front left. Both
## machines have the same 220x220 plate, so an AD5M constant of -110 means
## "the left edge of the plate" and its AD5X equivalent is 0 - but the numbers
## are not interchangeable, and a shared macro that drives to X-20 on an AD5X
## lands 20mm off the left of the bed, at the X hard minimum, against the
## frame. That is not theoretical: CLEAR_NOZZLE's park did exactly that at
## 6000mm/min on a plain sliced print.
##
## Outside the plate on this machine sits hardware - the filament cutter at the
## front left, the purge chute and wiper along the back wall - so a coordinate
## that merely stays inside the kinematic limits is not safe. These tests hold
## every AD5X-reachable coordinate to the plate, and hold the park retarget to
## a Y the machine can be driven to in a straight line.

import ast
import configparser
import pathlib
import re
import unittest

from tests.gcode_macro_harness import load_macro, render_macro

ROOT = pathlib.Path(__file__).parents[1]
BASE = ROOT / "macros" / "base.cfg"
IFS = ROOT / "macros" / "ifs.cfg"
HW_AD5X = ROOT / "macros" / "hw_base.ad5x.cfg"

## The plate, in this machine's frame. Everything beyond it is hardware.
PLATE_MIN = 0.0
PLATE_MAX = 220.0

GEOMETRY = load_macro(IFS, "_IFS_GEOMETRY").variables

MOVE = re.compile(r"^G[01]\b")


def axis(command, letter):
    """The value of one axis word, or None if it is absent.

    Accepts both spellings the macros use: `G1 X-20` and the parameter form
    `_CHECK_BED_MESH_PROBE X=-5.0`.
    """
    found = re.search(r"\b%s=?(-?\d+(?:\.\d+)?)" % letter, command)
    return float(found.group(1)) if found else None


def moves(commands):
    """Every (x, y) a rendered macro commands, absent axes as None."""
    return [(axis(c, "X"), axis(c, "Y")) for c in commands if MOVE.match(c)]


def index_of(commands, pattern):
    """Where a command appears, so order can be asserted. Raises if absent."""
    for position, command in enumerate(commands):
        if re.match(pattern, command):
            return position
    raise AssertionError("no command matching %r in %r" % (pattern, commands))


def effective(name):
    """The macro as the AD5X actually runs it.

    base.cfg includes hw_base.ad5x.cfg last, so a same-named section there
    wins. Load the override when one exists, the shared body otherwise.
    """
    text = HW_AD5X.read_text()
    if re.search(r"^\[gcode_macro %s\]" % re.escape(name), text, re.M):
        return HW_AD5X
    return BASE


def merged_variables(name):
    """variable_* as klipper sees them after the duplicate-section merge.

    Read straight from the merged config rather than through load_macro: an
    override that restates only bounds carries no gcode option, which the
    harness rejects and klipper is perfectly happy with.
    """
    section = sections()["gcode_macro %s" % name]
    return {key[len("variable_"):]: ast.literal_eval(value)
            for key, value in section.items() if key.startswith("variable_")}


def sections():
    """Every config section, merged the way klipper merges duplicates.

    Klipper parses with RawConfigParser(strict=False) (klippy/configfile.py),
    so a duplicate section merges option by option with the later file
    winning. Reproduce that for the non-macro sections.
    """
    parser = configparser.RawConfigParser(strict=False,
                                          inline_comment_prefixes=(";", "#"))
    parser.read([str(BASE), str(HW_AD5X)])
    return parser


def printer_state(**overrides):
    state = {
        "toolhead": {"homed_axes": "xyz",
                     "axis_maximum": {"x": 225.0, "y": 232.0, "z": 230.0},
                     "axis_minimum": {"x": -20.0, "y": -20.0, "z": -10.0}},
        "gcode_move": {"gcode_position": {"x": 110.0, "y": 110.0, "z": 10.0}},
        "mod_params": {"variables": {"safe_z": 10.0}},
        "gcode_macro _IFS_GEOMETRY": GEOMETRY,
    }
    state.update(overrides)
    return state


class PurgeLineTest(unittest.TestCase):
    """The four prime lines must land on the plate.

    As inherited these draw at Y-110, which is 90mm below the AD5X's
    position_min of -20: klipper refuses the move and the print dies at the
    purge step. _CLEAR4 is worse than a clean refusal - its first two moves
    are legal, so it purges 9mm of filament off the front of the plate before
    erroring on the third.
    """

    def test_every_prime_line_stays_on_the_plate(self):
        for name in ("_CLEAR1", "_CLEAR2", "_CLEAR3", "_CLEAR4"):
            with self.subTest(macro=name):
                rendered = render_macro(effective(name), name,
                                        printer=printer_state())
                for x, y in moves(rendered.commands):
                    if x is not None:
                        self.assertGreaterEqual(x, PLATE_MIN, name)
                        self.assertLessEqual(x, PLATE_MAX, name)
                    if y is not None:
                        self.assertGreaterEqual(y, PLATE_MIN, name)
                        self.assertLessEqual(y, PLATE_MAX, name)


class PurgeLineDerivationTest(unittest.TestCase):
    """Each prime line is the shared one, moved into this frame.

    The whole difference between the frames is +110 on both axes, so the
    override draws the same line on the same part of the plate that the AD5M
    draws it on. Pinning the arithmetic keeps the two from drifting into
    separately maintained purge routines, and makes a hand-edited coordinate
    show up as a failure rather than as a line in the wrong place.
    """

    OFFSET = 110.0

    def test_each_line_is_the_shared_body_shifted(self):
        for name in ("_CLEAR1", "_CLEAR2", "_CLEAR3", "_CLEAR4"):
            with self.subTest(macro=name):
                shared = moves(render_macro(BASE, name,
                                            printer=printer_state()).commands)
                ours = moves(render_macro(HW_AD5X, name,
                                          printer=printer_state()).commands)
                self.assertEqual(len(ours), len(shared),
                                 "%s has a different number of moves" % name)
                for (their_x, their_y), (our_x, our_y) in zip(shared, ours):
                    for theirs, ours_ in ((their_x, our_x), (their_y, our_y)):
                        if theirs is None:
                            self.assertIsNone(ours_)
                        else:
                            ## places=3: the shared bodies carry a 0.4mm
                            ## second pass, and 110 - 109.6 does not land on
                            ## it exactly in binary.
                            self.assertAlmostEqual(ours_, theirs + self.OFFSET,
                                                   places=3)


class MoveSafeEnvelopeTest(unittest.TestCase):
    """MOVE_SAFE's envelope is the plate, not the kinematic limits.

    It is the clamp for client jog, SMART_PARK and the end-of-print park. The
    AD5M's +-110 is that machine's plate. Left at +-110 here it hides the
    entire right and rear half of the AD5X bed, and silently truncates the
    end park to the middle of the finished print. Widened to the kinematic
    limits instead, it would let a clamped client move reach the cutter.
    """

    def test_envelope_is_the_plate(self):
        limits = merged_variables("MOVE_SAFE")
        self.assertEqual(limits["x_min"], PLATE_MIN)
        self.assertEqual(limits["y_min"], PLATE_MIN)
        self.assertEqual(limits["x_max"], PLATE_MAX)
        self.assertEqual(limits["y_max"], PLATE_MAX)

    def test_clamps_a_target_beyond_the_plate_back_onto_it(self):
        rendered = render_macro(BASE, "MOVE_SAFE",
                                printer=printer_state(**{
                                    "gcode_macro MOVE_SAFE":
                                        merged_variables("MOVE_SAFE")}),
                                params={"X": 260, "Y": 260, "ABSOLUTE": 1})
        for x, y in moves(rendered.commands):
            self.assertEqual(x, PLATE_MAX)
            self.assertEqual(y, PLATE_MAX)


class MeshValidationBoundsTest(unittest.TestCase):
    """Mesh validation must not probe off the front of the plate.

    _CHECK_BED_MESH pushes its probe points outward from the loaded mesh so
    that any drool lands at the bed edge rather than in the print, and bounds
    that expansion by the plate. With the AD5M's +-110 left in place and this
    machine's mesh configured 0..215, the expansion produces -5, and X-5/Y-5
    is a downward probe alongside the filament cutter.
    """

    def test_bounds_are_the_plate(self):
        bounds = merged_variables("_CHECK_BED_MESH")
        self.assertEqual(bounds["bed_min_x"], PLATE_MIN)
        self.assertEqual(bounds["bed_min_y"], PLATE_MIN)
        self.assertEqual(bounds["bed_max_x"], PLATE_MAX)
        self.assertEqual(bounds["bed_max_y"], PLATE_MAX)

    def test_probe_points_stay_on_the_plate(self):
        mesh = [[0.0] * 5 for _ in range(5)]
        state = printer_state(**{
            "bed_mesh": {"profile_name": "MESH_DATA", "mesh_matrix": mesh,
                         "mesh_min": [0.0, 0.0], "mesh_max": [215.0, 215.0]},
            "gcode_macro _CHECK_BED_MESH": merged_variables("_CHECK_BED_MESH"),
        })
        state["mod_params"]["variables"]["bed_mesh_validation_tolerance"] = 0.2
        rendered = render_macro(BASE, "_CHECK_BED_MESH", printer=state)

        probes = [c for c in rendered.commands
                  if c.startswith("_CHECK_BED_MESH_PROBE")]
        self.assertTrue(probes, "no probe points were emitted")
        for probe in probes:
            for letter in ("X", "Y"):
                value = axis(probe, letter)
                self.assertIsNotNone(value, probe)
                self.assertGreaterEqual(value, PLATE_MIN, probe)
                self.assertLessEqual(value, PLATE_MAX, probe)


class BedScrewPositionTest(unittest.TestCase):
    """The bed screws are where this machine's screws are.

    Inherited at +-94 in the AD5M's frame, every one of them is below the
    AD5X's position_min of -20, so SCREWS_TILT_CALCULATE - one tap from
    HelixScreen's bed screw panel - refuses on the first screw.
    """

    def test_all_four_screws_are_on_the_plate(self):
        screws = sections()["screws_tilt_adjust"]
        for key in ("screw1", "screw2", "screw3", "screw4"):
            with self.subTest(screw=key):
                x, y = [float(v) for v in screws[key].split(",")]
                self.assertGreaterEqual(x, PLATE_MIN)
                self.assertLessEqual(x, PLATE_MAX)
                self.assertGreaterEqual(y, PLATE_MIN)
                self.assertLessEqual(y, PLATE_MAX)

    def test_thread_is_left_as_inherited(self):
        """CW-M4 is deliberate and must not drift.

        HelixScreen carries a printer-database override declaring this family
        physically responds CCW, established empirically (helixscreen
        640d61193, and d3d329597 for this machine). Correcting the thread
        string here without dropping that override double-flips it and starts
        telling users to turn the screw the wrong way.
        """
        self.assertEqual(sections()["screws_tilt_adjust"]["screw_thread"],
                         "CW-M4")


class ParkRetargetTest(unittest.TestCase):
    """Parks must be reachable in a straight line.

    _TOOLHEAD_PARK_PAUSE_CANCEL emits one combined `G1 X.. Y..`, so whatever
    the retarget writes is approached diagonally. station_y is behind the back
    wall, and ifs.cfg says so in as many words: X before Y, and they must not
    be combined. safe_y is by definition the lane where X can be crossed, so
    it is the only Y a diagonal may end on. PAUSE, M600 and a runout all reach
    this, one tap from the screen.
    """

    def setUp(self):
        self.commands = render_macro(
            IFS, "_IFS_RETARGET_END_PARK",
            printer=printer_state(**{
                "gcode_macro _CLIENT_VARIABLE": {"custom_park_x": 105.0,
                                                 "custom_park_y": 105.0,
                                                 "park_at_cancel_x": 105.0,
                                                 "park_at_cancel_y": 105.0},
            })).commands

    def _value(self, variable):
        for command in self.commands:
            if "VARIABLE=%s " % variable in command + " ":
                return float(command.rsplit("VALUE=", 1)[1])
        raise AssertionError("%s is never retargeted: %r"
                             % (variable, self.commands))

    def test_pause_park_is_not_behind_the_back_wall(self):
        self.assertLessEqual(self._value("custom_park_y"), GEOMETRY["safe_y"])

    def test_cancel_park_is_retargeted_too(self):
        """Cancel is a separate pair of variables and was never covered.

        Left at the inherited 105/105 the head parks in the middle of the
        abandoned print and cools there.
        """
        self.assertEqual(self._value("park_at_cancel_x"), GEOMETRY["chute_x"])
        self.assertLessEqual(self._value("park_at_cancel_y"),
                             GEOMETRY["safe_y"])


class NozzleCleanTest(unittest.TestCase):
    """The clean flushes into the chute; it does not scrape the plate.

    The inherited routine is shaped by hardware the AD5M has and this machine
    does not, and the reverse. An AD5M cannot put molten plastic anywhere, so
    its only cleaning mechanism is mechanical: probe a surface, then drag the
    nozzle across it at 0.15mm. That is why it needs two probes and a height
    reference at all. This machine flushes through the nozzle into the purge
    chute, freezes the blob with the part fan, snaps it off and takes the
    residue on the rubber wiper - so there is no height reference anywhere in
    the sequence, and nothing it does needs to touch the bed.

    Translating the AD5M's coordinates would have produced a machine that
    scrapes its own print surface to do a job the wiper is sitting there to
    do. These tests hold the override to delegation: every move goes through
    ifs.cfg's routed station macros, and no raw XY leaves this macro.
    """

    def clean(self, loaded=True, mesh="MESH_DATA"):
        state = printer_state(**{
            "bed_mesh": {"profile_name": mesh},
            "filament_switch_sensor toolhead": {"filament_detected": loaded},
        })
        state["mod_params"]["variables"]["clear_cooldown_temp"] = 120
        return render_macro(HW_AD5X, "CLEAR_NOZZLE", printer=state,
                            params={"EXTRUDER_TEMP": 220,
                                    "BED_TEMP": 55}).commands

    def test_commands_no_xy_of_its_own(self):
        ## Every station approach has to go through _IFS_GOTO_STATION, which
        ## owns the X-before-Y rule for the back wall. A raw G1 here is how
        ## that rule gets bypassed.
        for command in self.clean():
            if MOVE.match(command):
                self.assertIsNone(axis(command, "X"), command)
                self.assertIsNone(axis(command, "Y"), command)

    def test_never_probes(self):
        ## There is no surface to probe: this machine has no cleaning board,
        ## and a PROBE with nothing under the nozzle either fails or dives.
        for command in self.clean():
            self.assertFalse(command.startswith("PROBE"), command)
            self.assertFalse(command.startswith("_CLEAR_NOZZLE_PROBE"),
                             command)

    def test_parks_at_the_chute_before_flushing(self):
        commands = self.clean()
        self.assertLess(index_of(commands, r"_IFS_PARK_FOR_PURGE"),
                        index_of(commands, r"_IFS_PURGE\b"))

    def test_does_not_zero_the_gcode_offset(self):
        """The inherited clean zeroes the offset and never puts it back.

        RESTORE_GCODE_STATE does not restore homing_origin, so the shared
        routine leaves the applied Z offset on the floor for whatever runs
        next. Nothing here needs a zeroed offset, because nothing here
        references the bed, so the bug cannot arise rather than being fixed.
        """
        for command in self.clean():
            self.assertNotIn("_SET_GCODE_OFFSET", command)

    def test_restores_the_mesh_it_cleared(self):
        commands = self.clean(mesh="MESH_DATA")
        self.assertLess(index_of(commands, r"BED_MESH_CLEAR"),
                        index_of(commands, r'BED_MESH_PROFILE LOAD="MESH_DATA"'))

    def test_wipes_without_flushing_when_nothing_is_loaded(self):
        ## Driving the extruder against no filament cleans nothing; the wiper
        ## still has a job.
        commands = self.clean(loaded=False)
        self.assertFalse([c for c in commands if c.startswith("_IFS_PURGE")],
                         commands)
        index_of(commands, r"_IFS_WIPE")


class StartPrintParkTest(unittest.TestCase):
    """The heat soak waits over the chute, not over the plate.

    Whatever is left in the nozzle softens and drips while the bed comes up.
    The shared park sits at X110 Y110, which is the AD5M's far corner and this
    machine's dead centre, so on an AD5X the soak drools onto the middle of
    the sheet the print is about to go on. There is a bin here; use it.

    This is also why the shared macro holds the nozzle at its current
    temperature through the soak rather than driving it to the print
    temperature (base.cfg, "if extruder hot enough don't waste heat"). Heating
    early over a plate means oozing onto it. Parked over the chute that
    tradeoff is different, which is a follow-on change and not this one.
    """

    def test_start_print_delegates_its_park(self):
        ## Both park sites - the pre-soak park and the return after mesh
        ## validation - have to go through the seam, or the platform override
        ## silently only covers one of them.
        body = load_macro(BASE, "_START_PRINT").gcode
        self.assertNotIn("X110 Y110", body)
        self.assertIn("_START_PRINT_PARK", body)

    def test_the_shared_park_is_unchanged(self):
        ## Centre-origin machines must render exactly what they did before.
        commands = render_macro(BASE, "_START_PRINT_PARK",
                                printer=printer_state()).commands
        self.assertEqual([(110.0, 110.0)],
                         [m for m in moves(commands) if m != (None, None)])

    def test_the_ad5x_park_goes_to_the_chute(self):
        commands = render_macro(HW_AD5X, "_START_PRINT_PARK",
                                printer=printer_state()).commands
        index_of(commands, r"_IFS_PARK_FOR_PURGE")
        for command in commands:
            if MOVE.match(command):
                self.assertIsNone(axis(command, "X"), command)
                self.assertIsNone(axis(command, "Y"), command)


class StartPrintCleanTest(unittest.TestCase):
    """Print start flushes at the chute, and does not leave a retracted tip.

    Cleaning only during leveling means a print that loads a stored mesh gets
    no clean at all, and the first layer starts through whatever the last job
    left in the nozzle. This machine can flush at print start because it has
    somewhere to put it, so it does.

    The nozzle must NOT be retracted afterwards. A purge that ends retracted
    starts the first extrusion behind the pressure it needs, which shows up as
    a gap at the start of the first perimeter. _IFS_PURGE ends with the fan
    off, G92 E0, a shake and a wipe - no retract - so delegating to it gives
    us that; a hand-rolled sequence would have had to remember.
    """

    def clean(self, loaded=True):
        return render_macro(HW_AD5X, "_START_PRINT_CLEAN", printer=printer_state(**{
            "filament_switch_sensor toolhead": {"filament_detected": loaded},
        })).commands

    def test_flushes_at_the_chute(self):
        index_of(self.clean(), r"_IFS_PURGE\b")

    def test_never_ends_retracted(self):
        for command in self.clean():
            if MOVE.match(command):
                extrude = axis(command, "E")
                self.assertFalse(extrude is not None and extrude < 0, command)

    def test_skips_the_flush_with_nothing_loaded(self):
        self.assertFalse([c for c in self.clean(loaded=False)
                          if c.startswith("_IFS_PURGE")])

    def test_the_shared_default_does_nothing(self):
        ## A machine with no chute has nowhere to put a print-start flush; it
        ## cleans mechanically during leveling instead. The seam must be inert
        ## there, not merely harmless.
        self.assertEqual((), render_macro(BASE, "_START_PRINT_CLEAN",
                                          printer=printer_state()).commands)

    def test_start_print_cleans_after_heating_and_before_priming(self):
        body = load_macro(BASE, "_START_PRINT").gcode
        self.assertIn("_START_PRINT_CLEAN", body)
        self.assertLess(body.index("_START_PRINT_CLEAN"),
                        body.index("NAME=PRIMING"))


class WipeDelegationTest(unittest.TestCase):
    """_CLEAR_NOZZLE's drag becomes the wiper pass.

    The shared body drags the nozzle at probe+0.15mm between X-10 and X20 at
    Y107..111 - on this machine, straight across the middle of the print
    surface. Same intent, different hardware: the wiper does it.
    """

    def test_delegates_to_the_wiper(self):
        commands = render_macro(HW_AD5X, "_CLEAR_NOZZLE",
                                printer=printer_state()).commands
        index_of(commands, r"_IFS_WIPE")
        for command in commands:
            if MOVE.match(command):
                self.assertIsNone(axis(command, "X"), command)
                self.assertIsNone(axis(command, "Y"), command)


class PurgeLengthTest(unittest.TestCase):
    """A clean asks for a shorter flush than a colour change.

    _IFS_PURGE's lengths are sized for changing colour - 100mm then 30mm, from
    the vendor's own multicolour figures. A clean only has to move enough
    material to carry the residue out. FIRST_MM was already a parameter;
    SECOND_MM is its twin, so the clean can ask rather than fork the macro.
    """

    def rendered(self, **params):
        state = printer_state(**{
            "ifs": {"params": {"first_purge_mm": 100.0, "first_fan": 0,
                               "first_purge_speed": 300,
                               "second_purge_mm": 30.0, "second_fan": 255,
                               "second_purge_speed": 300}},
        })
        return render_macro(IFS, "_IFS_PURGE", printer=state,
                            params=params).commands

    def extrusions(self, commands):
        return [axis(c, "E") for c in commands
                if MOVE.match(c) and axis(c, "E") is not None]

    def test_second_pass_length_is_overridable(self):
        self.assertIn(10.0, self.extrusions(self.rendered(FIRST_MM=20,
                                                          SECOND_MM=10)))

    def test_second_pass_falls_back_to_the_configured_length(self):
        self.assertIn(30.0, self.extrusions(self.rendered(FIRST_MM=20)))


class LoadCellTareTest(unittest.TestCase):
    """The probe reads through a cell that has to be zeroed first.

    Klipper only sees the conditioning MCU's verdict as a digital pin, and
    that MCU compares the cell against a stored zero which drifts - measured
    at 140 g with the nozzle in free air, which holds the pin triggered
    permanently and fails every probing move before it moves. The AD5M has a
    tare plugin for the same reason; this board needs its own because the
    AD5M's looks up sections that do not exist here.
    """

    def plugin(self):
        import importlib.util
        path = ROOT / ".py" / "klipper" / "plugins" / "load_cell_tare.py"
        spec = importlib.util.spec_from_file_location("load_cell_tare", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_reads_the_weight_out_of_a_reply(self):
        parse = self.plugin().parse_weight
        self.assertEqual(0.0, parse(b"command H7 ok. 8511562 0 g \r\n"))
        self.assertEqual(140.0, parse(b"command H7 ok. 8511865 140 g \r\n"))
        self.assertEqual(-3.0, parse("command H7 ok. 8511865 -3 g"))

    def test_a_reply_without_a_weight_is_not_a_weight(self):
        ## The tare reply carries a raw count and no unit. Reading it as a
        ## weight would report a tare of eight million grams as success.
        parse = self.plugin().parse_weight
        self.assertIsNone(parse(b"command H1 ok. 8511666 \r\n"))
        self.assertIsNone(parse(b""))
        self.assertIsNone(parse(None))
        self.assertIsNone(parse(b"garbage"))
        self.assertIsNone(parse(b"command H7 ok. 8511865 x g"))

    def test_the_unit_locates_the_value_not_a_fixed_column(self):
        ## An added field must not silently shift which number is the weight.
        parse = self.plugin().parse_weight
        self.assertEqual(12.0, parse(b"command H7 ok. 1 2 3 12 g \r\n"))

    def test_the_platform_declares_the_shared_plugin_on_this_transport(self):
        ## The port reuses Forge-X's own tare plugin rather than a parallel
        ## one, so every caller and every safety check is the shared code.
        self.assertIn("load_cell_tare", sections().sections())
        self.assertEqual("serial",
                         sections()["load_cell_tare"]["transport"])

    def test_no_macro_shadows_the_plugin_command(self):
        """A macro and the plugin cannot both register LOAD_CELL_TARE.

        The no-op macro this replaced would win or collide at config time,
        depending on parse order, and a silently-winning no-op is exactly the
        failure we just spent an afternoon on.
        """
        self.assertNotIn("gcode_macro LOAD_CELL_TARE",
                         sections().sections())

    def test_the_clean_tares_on_its_way_out(self):
        body = load_macro(HW_AD5X, "CLEAR_NOZZLE").gcode
        self.assertIn("LOAD_CELL_TARE", body)
        ## After the cooldown, because the cell reads differently hot and the
        ## number that matters is the one held during the probing that
        ## follows.
        self.assertLess(body.index("clear_cooldown_temp"),
                        body.index("LOAD_CELL_TARE"))


if __name__ == "__main__":
    unittest.main()
