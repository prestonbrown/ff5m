## Tests for the zupdate.py model table and derived download naming.
##
## Copyright (C) 2026, Alexander K <https://github.com/drA1ex>
##
## This file may be distributed under the terms of the GNU GPLv3 license

import importlib.util
import pathlib
import unittest


ROOT = pathlib.Path(__file__).parents[1]
ZUPDATE = ROOT / ".py" / "zupdate.py"


def load_zupdate():
    # .py is a dot-directory, not an importable package, so load by file path.
    spec = importlib.util.spec_from_file_location("zupdate", ZUPDATE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


zupdate = load_zupdate()


class ZupdateModelTableTest(unittest.TestCase):
    def test_exactly_the_three_supported_models(self):
        self.assertEqual(
            set(zupdate.MODELS),
            {"Adventurer5M", "Adventurer5MPro", "AD5X"},
        )

    def test_ad5m_glob_and_name_prefix_unchanged(self):
        # These are the literals the AD5M-only code used before the table; they
        # must not drift, or existing AD5M installs stop finding their image.
        for model in ("Adventurer5M", "Adventurer5MPro"):
            self.assertEqual(zupdate.MODELS[model]["glob"], "Adventurer5M*.tgz")
            self.assertEqual(
                zupdate.MODELS[model]["name_prefix"], "Adventurer5M-")
            self.assertEqual(
                zupdate.MODELS[model]["asset_prefix"], "Adventurer5M-ForgeX-")

    def test_ad5x_table_entry(self):
        self.assertEqual(zupdate.MODELS["AD5X"]["glob"], "AD5X-*.tgz")
        self.assertEqual(zupdate.MODELS["AD5X"]["name_prefix"], "AD5X-")
        self.assertEqual(zupdate.MODELS["AD5X"]["asset_prefix"], "AD5X-ForgeX-")


class ZupdateAssetPatternTest(unittest.TestCase):
    def test_ad5m_pattern_matches_ad5m_and_rejects_ad5x(self):
        pattern = zupdate.asset_pattern("Adventurer5M")
        self.assertTrue(pattern.fullmatch("Adventurer5M-ForgeX-1.2.3.tgz"))
        self.assertTrue(pattern.fullmatch("Adventurer5M-ForgeX-nightly_2026.01.tgz"))
        self.assertIsNone(pattern.fullmatch("AD5X-ForgeX-1.2.3.tgz"))
        self.assertIsNone(pattern.fullmatch("Adventurer5M-ForgeX-1.2.3.tgz.bak"))
        self.assertIsNone(pattern.fullmatch("prefix-Adventurer5M-ForgeX-1.2.3.tgz"))

    def test_ad5m_pro_shares_the_ad5m_pattern(self):
        self.assertEqual(
            zupdate.asset_pattern("Adventurer5MPro").pattern,
            zupdate.asset_pattern("Adventurer5M").pattern,
        )

    def test_ad5x_pattern_matches_ad5x_and_rejects_ad5m(self):
        pattern = zupdate.asset_pattern("AD5X")
        self.assertTrue(pattern.fullmatch("AD5X-ForgeX-1.2.3.tgz"))
        self.assertTrue(pattern.fullmatch("AD5X-ForgeX-nightly_2026.01.tgz"))
        self.assertIsNone(pattern.fullmatch("Adventurer5M-ForgeX-1.2.3.tgz"))
        self.assertIsNone(pattern.fullmatch("AD5X-ForgeX-1.2.3.tgz.part"))


class ZupdateSelectAssetTest(unittest.TestCase):
    def _release(self, *names):
        return {"assets": [{"name": name} for name in names]}

    def test_selects_the_single_matching_ad5x_asset(self):
        release = self._release(
            "AD5X-ForgeX-1.2.3.tgz",
            "Adventurer5M-ForgeX-1.2.3.tgz",
            "checksums.txt",
        )
        asset = zupdate.select_asset(release, zupdate.asset_pattern("AD5X"))
        self.assertEqual(asset["name"], "AD5X-ForgeX-1.2.3.tgz")

    def test_selects_the_single_matching_ad5m_asset(self):
        release = self._release(
            "AD5X-ForgeX-1.2.3.tgz",
            "Adventurer5M-ForgeX-1.2.3.tgz",
        )
        asset = zupdate.select_asset(
            release, zupdate.asset_pattern("Adventurer5M"))
        self.assertEqual(asset["name"], "Adventurer5M-ForgeX-1.2.3.tgz")

    def test_rejects_when_no_asset_matches(self):
        release = self._release("Adventurer5M-ForgeX-1.2.3.tgz")
        with self.assertRaises(RuntimeError):
            zupdate.select_asset(release, zupdate.asset_pattern("AD5X"))

    def test_rejects_when_multiple_assets_match(self):
        release = self._release(
            "AD5X-ForgeX-1.2.3.tgz",
            "AD5X-ForgeX-1.2.4.tgz",
        )
        with self.assertRaises(RuntimeError):
            zupdate.select_asset(release, zupdate.asset_pattern("AD5X"))


class ZupdateFinalNameTest(unittest.TestCase):
    def test_ad5m_final_name_is_unchanged(self):
        self.assertEqual(
            zupdate.final_asset_name(
                "Adventurer5M", "Adventurer5M-ForgeX-1.2.3.tgz"),
            "Adventurer5M-ForgeX-1.2.3.tgz",
        )

    def test_ad5m_pro_final_name_gets_the_pro_prefix(self):
        self.assertEqual(
            zupdate.final_asset_name(
                "Adventurer5MPro", "Adventurer5M-ForgeX-1.2.3.tgz"),
            "Adventurer5MPro-ForgeX-1.2.3.tgz",
        )

    def test_ad5x_final_name_round_trips(self):
        self.assertEqual(
            zupdate.final_asset_name("AD5X", "AD5X-ForgeX-1.2.3.tgz"),
            "AD5X-ForgeX-1.2.3.tgz",
        )

    def test_forgex_marker_is_preserved_in_the_final_name(self):
        # Guards the family-prefix strip against being widened to the full
        # asset_prefix, which would drop "ForgeX-" from the saved filename.
        for model, asset in (
            ("Adventurer5M", "Adventurer5M-ForgeX-9.9.9.tgz"),
            ("AD5X", "AD5X-ForgeX-9.9.9.tgz"),
        ):
            self.assertIn("ForgeX-", zupdate.final_asset_name(model, asset))


if __name__ == "__main__":
    unittest.main()
