## moonraker platform-seam tests: one shared moonraker.conf, board-flavoured
## through a symlinked include.
##
## Copyright (C) 2026, Preston Brown
##
## This file may be distributed under the terms of the GNU GPLv3 license

import pathlib
import unittest

from tests.gcode_macro_harness import _read_sections

ROOT = pathlib.Path(__file__).parents[1]
MOONRAKER = ROOT / "moonraker.conf"
PLATFORM_BASE = ROOT / ".cfg" / "moonraker.conf"
PLATFORM_AD5X = ROOT / ".cfg.ad5x" / "moonraker.conf"


class SharedConfigTest(unittest.TestCase):
    def test_the_platform_include_precedes_the_user_include(self):
        """Includes resolve relative to printer_data/config and later files
        win, so the user's overrides must come after the platform's."""
        sections = [name for name, _ in _read_sections(MOONRAKER)]
        self.assertIn("include mod_data/platform.moonraker.conf", sections)
        self.assertIn("include mod_data/user.moonraker.conf", sections)
        self.assertLess(
            sections.index("include mod_data/platform.moonraker.conf"),
            sections.index("include mod_data/user.moonraker.conf"))

    def test_no_board_specific_update_manager_remains_in_the_shared_file(self):
        """The guppyscreen entry names an AD5M rootfs path (/root/guppyscreen)
        that does not exist in the AD5X rootfs: the extension fails to load
        and its repo:/managed_services: options surface as three Fluidd
        warnings. It belongs to the AD5M platform variant only."""
        body = MOONRAKER.read_text(encoding="utf-8")
        self.assertNotIn("guppyscreen", body)


class PlatformVariantTest(unittest.TestCase):
    def test_the_ad5m_variant_carries_the_guppyscreen_entry(self):
        body = PLATFORM_BASE.read_text(encoding="utf-8")
        self.assertIn("[update_manager guppyscreen]", body)
        self.assertIn("path: /root/guppyscreen", body)

    def test_the_ad5x_variant_relaxes_the_config_path_check(self):
        """klippy reports host spellings (/usr/data/config) that Moonraker's
        chroot-side resolve() can never match, so Moonraker's own documented
        escape hatch applies. On the AD5M the check is legitimate - its host
        and chroot spellings coincide - and the shared default stays."""
        body = PLATFORM_AD5X.read_text(encoding="utf-8")
        self.assertIn("check_klipper_config_path: False", body)
        self.assertNotIn("[update_manager guppyscreen]", body)
        self.assertNotIn(
            "check_klipper_config_path",
            PLATFORM_BASE.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
