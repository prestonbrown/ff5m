## cfg_backup restore-flow tests for the AD5X base-config overlay.
##
## The overlay files are consumed by cfg_backup.py in restore mode (init_lib.sh
## fix_config, batch step 5); these tests drive that same tool, off-rig, against
## a stock-shaped printer.base.cfg, so the directive files are exercised by the
## machinery that actually applies them.
##
## Copyright (C) 2026, Preston Brown
##
## This file may be distributed under the terms of the GNU GPLv3 license

import pathlib
import re
import subprocess
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).parents[1]
CFG_BACKUP = ROOT / ".py" / "cfg_backup.py"
INIT_BASE = ROOT / ".cfg.ad5x" / "init.base.cfg"
DATA_INIT_BASE = ROOT / ".cfg.ad5x" / "data.init.base.cfg"

## The stock shape the overlay is applied to: the same [printer] block the
## device's printer.base.cfg carries, including the deprecated accel-to-decel
## option AD5X's newer klippy warns about, plus the sections the overlay
## already removes.
STOCK_BASE = """\
[printer]
max_accel: 20000
max_accel_to_decel: 5000

[heater_bed]
heater_pin: PD7

[led chamber_led]
white_pin: PA11
"""


class Ad5xBaseOverlayTest(unittest.TestCase):
    def _restore(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        target = pathlib.Path(directory.name) / "printer.base.cfg"
        target.write_text(STOCK_BASE, encoding="utf-8")
        result = subprocess.run(
            [sys.executable, str(CFG_BACKUP),
             "--mode", "restore",
             "--config", str(target),
             "--params", str(INIT_BASE),
             "--data", str(DATA_INIT_BASE),
             "--avoid_writes"],
            capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        return target.read_text(encoding="utf-8")

    def test_the_deprecated_accel_option_is_replaced_by_the_cruise_ratio(self):
        restored = self._restore()
        self.assertNotIn("max_accel_to_decel", restored)
        self.assertRegex(restored, r"minimum_cruise_ratio")

    def test_the_ratio_value_preserves_the_old_decel_behaviour(self):
        ## 1 - min(1, 5000/20000) = 0.75: the same effective accel-to-decel as
        ## the stock option carried. Removing the option alone would leave
        ## klipper's default ratio of 0.5, silently doubling it to 10000.
        restored = self._restore()
        self.assertRegex(restored, r"minimum_cruise_ratio:\s*0\.75")

    def test_stock_options_and_the_existing_removals_are_untouched(self):
        restored = self._restore()
        self.assertIn("max_accel: 20000", restored)
        self.assertNotIn("[heater_bed]", restored)
        self.assertNotIn("[led chamber_led]", restored)


if __name__ == "__main__":
    unittest.main()
