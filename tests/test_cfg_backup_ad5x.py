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
## already removes. The two temperature_sensor blocks are copied verbatim from
## a device at lines 150-156, adjacent and near-identical, because the removal
## has to hit one and spare the other.
STOCK_BASE = """\
[printer]
max_accel: 20000
max_accel_to_decel: 5000

[heater_bed]
heater_pin: PD7

[led chamber_led]
white_pin: PA11

[temperature_sensor filamentValue]
sensor_type: Generic 3950
sensor_pin: eboard:PA3

[temperature_sensor cutValue]
sensor_type: Generic 3950
sensor_pin: eboard:PA2
"""

## The other half of the toolhead-sensor change: the overlay frees the pin,
## and this file is what claims it.
HOST = ROOT / "macros" / "hw_base.ad5x.cfg"
MODULE = ROOT / "macros" / "ifs.cfg"
TOOLHEAD_PIN = "eboard:PA3"


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

    def test_the_toolhead_sensors_adc_is_freed_for_the_ifs_plugin(self):
        """The pin has to be free before ifs_toolhead_sensor can claim it.

        klipper lets a pin be claimed once. Stock declares eboard:PA3 as a
        thermistor purely so the ADC gets sampled, and reports it every
        0.300s; the plugin takes the pin instead and samples every 0.015s.
        Leaving this section in place is what makes the claim fail.
        """
        restored = self._restore()
        self.assertNotIn("[temperature_sensor filamentValue]", restored)
        self.assertNotIn(TOOLHEAD_PIN, restored)

    def test_the_neighbouring_cut_sensor_survives(self):
        """cutValue sits directly after filamentValue and must not go with it.

        The two sections differ only in the last word of the header and one
        digit of the pin. A removal matching on prefix, or on the section
        type, would take both - and the symptom would be a cutter sensor
        that silently stopped reading, not a startup error.
        """
        restored = self._restore()
        self.assertIn("[temperature_sensor cutValue]", restored)
        self.assertIn("eboard:PA2", restored)

    def test_the_freed_pin_is_the_one_the_host_file_claims(self):
        """Both halves or neither, and they must name the SAME pin.

        The removal lives here; the claim lives in macros/hw_base.ad5x.cfg.
        Freeing a pin nobody claims leaves the sensor reading None - no
        runout detection, no load confirmation - and it fails silently,
        because nothing errors when an ADC simply is not there.
        """
        claim = re.search(r"^\[ifs_toolhead_sensor toolhead\]$(.*?)(?=^\[|\Z)",
                          HOST.read_text(encoding="utf-8"),
                          re.MULTILINE | re.DOTALL)
        self.assertIsNotNone(
            claim, "hw_base.ad5x.cfg no longer claims the toolhead sensor pin")
        self.assertRegex(claim.group(1),
                         r"sensor_pin:\s*%s\b" % re.escape(TOOLHEAD_PIN))

    def test_the_claim_comes_after_the_module_include(self):
        """Section merge is linear, so the override has to be the later one.

        klipper buffers the lines between includes and parses them as a unit,
        which makes a same-named section after [include ifs.cfg] win. Ahead
        of it the option would still survive today, but only because ifs.cfg
        happens to set no sensor_pin - a coincidence, not a rule.
        """
        text = HOST.read_text(encoding="utf-8")
        self.assertLess(text.index("[include ifs.cfg]"),
                        text.index("[ifs_toolhead_sensor toolhead]"))

    def test_the_portable_module_does_not_claim_the_pin(self):
        """ifs.cfg drops onto hosts that still have the stock section.

        The claim is a forge-x AD5X thing, because only here do we control
        the file the pin has to be freed from. In the module it would make
        every zmod and bare-klipper host log a failed claim on startup.
        """
        module = MODULE.read_text(encoding="utf-8")
        self.assertIn("[ifs_toolhead_sensor toolhead]", module)
        self.assertNotIn("sensor_pin", module)


if __name__ == "__main__":
    unittest.main()
