## Backlight tool tests: argument parsing, and graceful degrade.
##
## Two independent suites that arrived from two directions - upstream 1.4.2
## added the command-line behaviour tests, this fork added the ones covering a
## board whose /dev/disp rejects the ioctls. Both are kept: they cover
## different halves of the same tool.
##
## Copyright (C) 2026, Alexander K <https://github.com/drA1ex>
##
## This file may be distributed under the terms of the GNU GPLv3 license

import contextlib
import errno
import importlib.util
import io
import pathlib
import unittest
from unittest import mock


ROOT = pathlib.Path(__file__).parents[1]
BACKLIGHT = ROOT / ".py" / "backlight.py"


def load_backlight():
    # backlight.py lives under .py/ (not an importable package) and guards its
    # main() with __name__ == "__main__", so loading it by path does not run it.
    spec = importlib.util.spec_from_file_location("backlight", BACKLIGHT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class BacklightArgumentsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.backlight = load_backlight()

    def test_no_argument_queries_current_brightness(self):
        self.assertIsNone(self.backlight.parse_args([]))

    def test_numeric_brightness_accepts_complete_range(self):
        self.assertEqual(self.backlight.parse_args(["0"]), 0.0)
        self.assertEqual(self.backlight.parse_args(["37.5"]), 37.5)
        self.assertEqual(self.backlight.parse_args(["100"]), 100.0)

    def test_invalid_brightness_is_rejected_by_cli_parser(self):
        for value in ("invalid", "-1", "101", "nan"):
            with self.subTest(value=value):
                with contextlib.redirect_stderr(io.StringIO()):
                    with self.assertRaises(SystemExit):
                        self.backlight.parse_args([value])

    def test_numeric_main_applies_parsed_brightness(self):
        with mock.patch.object(self.backlight, "backlight_enable") as enable:
            with mock.patch.object(self.backlight, "backlight_set") as set_value:
                self.backlight.main(["37.5"])

        enable.assert_called_once_with()
        set_value.assert_called_once_with(37.5)


class BacklightGracefulDegradeTest(unittest.TestCase):
    # Run main() with a given argv while every /dev/disp ioctl raises OSError
    # with the given errno. Returns (raised_exc_or_None, stderr_text).
    def _run(self, argv, ioctl_errno):
        module = load_backlight()

        def fake_ioctl(_f, _request, _arg=b""):
            raise OSError(ioctl_errno, "mock ioctl")

        stderr = io.StringIO()
        raised = None
        with mock.patch("builtins.open", mock.mock_open()), \
                mock.patch.object(module, "ioctl", side_effect=fake_ioctl), \
                mock.patch("sys.argv", argv), \
                mock.patch("sys.stdout", io.StringIO()), \
                mock.patch("sys.stderr", stderr):
            try:
                module.main()
            except OSError as exc:
                raised = exc
        return raised, stderr.getvalue()

    def test_enotty_degrades_to_noop_on_set(self):
        # The print-flow path: screen.sh backlight <value> -> enable + set. A
        # board with no controllable backlight (AD5X: /dev/disp is a plain file)
        # rejects the ioctl with ENOTTY. main() must not raise, or the caller
        # (a print macro) sees "Error running command {screen}".
        raised, err = self._run(["backlight.py", "100"], errno.ENOTTY)
        self.assertIsNone(raised, "ENOTTY must be swallowed, not raised")
        self.assertIn("no controllable backlight", err)

    def test_enodev_degrades_to_noop_on_disable(self):
        raised, _ = self._run(["backlight.py", "0"], errno.ENODEV)
        self.assertIsNone(raised, "ENODEV must be swallowed, not raised")

    def test_get_prints_zero_when_no_backlight(self):
        # backlight.py with no value reads brightness; with no backlight it must
        # still exit cleanly and report a value rather than crash.
        raised, err = self._run(["backlight.py"], errno.ENXIO)
        self.assertIsNone(raised)
        self.assertIn("no controllable backlight", err)

    def test_unrelated_oserror_still_raises(self):
        # A real fault (e.g. EACCES) is not "no backlight" and must propagate,
        # so genuine failures on a board that HAS a backlight are not hidden.
        raised, _ = self._run(["backlight.py", "100"], errno.EACCES)
        self.assertIsNotNone(raised, "a non-backlight errno must propagate")
        self.assertEqual(raised.errno, errno.EACCES)


if __name__ == "__main__":
    unittest.main()
