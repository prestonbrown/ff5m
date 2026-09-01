## start_hint: does the running job begin with a tool change?
##
## Copyright (C) 2026, Preston Brown
##
## This file may be distributed under the terms of the GNU GPLv3 license

import importlib.util
import pathlib
import tempfile
import unittest

from tests.ifs_klipper_fakes import FakePrinter

ROOT = pathlib.Path(__file__).parents[1]
PLUGIN = ROOT / ".py" / "klipper" / "plugins" / "start_hint.py"


def load_plugin():
    spec = importlib.util.spec_from_file_location("start_hint", PLUGIN)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeSdcard:
    def __init__(self, path):
        self.file_path = path


class HintPrinter(FakePrinter):
    def __init__(self, path):
        FakePrinter.__init__(self)
        self.sdcard = FakeSdcard(path)

    def lookup_object(self, name):
        if name == "virtual_sdcard":
            return self.sdcard
        return FakePrinter.lookup_object(self, name)


def make_hint(job_gcode):
    module = load_plugin()
    handle = tempfile.NamedTemporaryFile(
        "w", suffix=".gcode", delete=False, encoding="utf-8")
    handle.write(job_gcode)
    handle.close()
    printer = HintPrinter(handle.name)
    return module.load_config(FakeConfigFor(printer)), handle.name


class FakeConfigFor:
    def __init__(self, printer):
        self.printer = printer

    def get_printer(self):
        return self.printer

    def get_name(self):
        return "start_hint"


class StartHintTest(unittest.TestCase):
    """The first tool command before the first extrusion, if there is one.

    A start flow that flushes the nozzle before a cut-and-load throws the
    flush away - the cut discards whatever the flush pushed. The hint lets
    the flow skip that work for jobs that open with a swap, without the
    slicer having to say so.
    """

    def test_a_job_that_opens_with_a_tool_change_reports_it(self):
        hint, _ = make_hint(
            "; header\n"
            "START_PRINT EXTRUDER_TEMP=220 BED_TEMP=55\n"
            "T3\n"
            "G1 X50 Y50 E3 F1800\n")
        self.assertEqual(3, hint.get_status()["first_tool"])

    def test_a_job_that_extrudes_first_reports_no_starting_swap(self):
        hint, _ = make_hint(
            "START_PRINT EXTRUDER_TEMP=220 BED_TEMP=55\n"
            "G1 X50 Y50 E3 F1800\n"
            "T2\n")
        self.assertIsNone(hint.get_status()["first_tool"])

    def test_a_single_tool_job_reports_none(self):
        hint, _ = make_hint(
            "START_PRINT BED_TEMP=55\nG1 X1 Y1 E1 F1200\n")
        self.assertIsNone(hint.get_status()["first_tool"])

    def test_comments_and_start_parameters_are_not_extrusions(self):
        ## BED_TEMP=55 and EXTRUDER_TEMP=220 carry E-words that must not
        ## read as the first extrusion, or every job would report none.
        hint, _ = make_hint(
            "START_PRINT EXTRUDER_TEMP=220 BED_TEMP=55\n"
            "M104 S220\n"
            "T1\n"
            "G1 E2 F300\n")
        self.assertEqual(1, hint.get_status()["first_tool"])

    def test_no_open_file_reports_none(self):
        module = load_plugin()
        printer = HintPrinter("")
        hint = module.load_config(FakeConfigFor(printer))
        self.assertIsNone(hint.get_status()["first_tool"])

    def test_a_changed_file_is_rescanned(self):
        hint, path = make_hint("START_PRINT\nG1 E1 F100\n")
        self.assertIsNone(hint.get_status()["first_tool"])
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("START_PRINT\nT2\nG1 E1 F100\n")
        self.assertEqual(2, hint.get_status()["first_tool"])


if __name__ == "__main__":
    unittest.main()
