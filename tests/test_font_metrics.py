"""Typer manifest loading and word-v1 parity tests."""

import json
import pathlib
import subprocess
import sys
import unittest
from unittest import mock


PLUGINS = (pathlib.Path(__file__).parents[1] / ".py" / "klipper" /
           "plugins")
sys.path.insert(0, str(PLUGINS))

from ui import font_metrics  # noqa: E402


def manifest(advance=16):
    return {
        "schema": "font-metrics/v1",
        "wrap_algorithm": "word-v1",
        "fonts": [{
            "name": "JetBrainsMono 12pt",
            "advance_x": advance,
            "monospaced": True,
            "advance_y": 33,
            "glyph_bounds": {"top": -26, "bottom": 5},
            "unicode_ranges": [
                [32, 126], [1025, 1025], [1040, 1103], [1105, 1105],
            ],
        }],
    }


class FontManifestTest(unittest.TestCase):
    def test_runtime_manifest_overrides_packaged_fallback(self):
        fallback = font_metrics.parse_manifest(manifest(advance=16))
        completed = subprocess.CompletedProcess(
            ["typer", "--font-manifest"], 0,
            stdout=json.dumps(manifest(advance=19)).encode("utf-8"), stderr=b"")

        with mock.patch("subprocess.run", return_value=completed):
            loaded = font_metrics.load_runtime_metrics(
                "/real/typer", fallback=fallback)

        self.assertEqual(loaded.metric("JetBrainsMono 12pt").advance_x, 19)

    def test_corrupt_manifest_is_rejected(self):
        damaged = manifest()
        damaged["fonts"][0]["unicode_ranges"] = [[1040, 1103], [32, 126]]

        with self.assertRaisesRegex(ValueError, "sorted and disjoint"):
            font_metrics.parse_manifest(damaged)

        fallback = font_metrics.parse_manifest(manifest(advance=17))
        completed = subprocess.CompletedProcess(
            ["typer", "--font-manifest"], 0,
            stdout=json.dumps(damaged).encode("utf-8"), stderr=b"")
        with mock.patch("subprocess.run", return_value=completed):
            loaded = font_metrics.load_runtime_metrics(
                "/damaged/typer", fallback=fallback)
        self.assertIs(loaded, fallback)

    def test_old_typer_uses_packaged_fallback(self):
        fallback = font_metrics.parse_manifest(manifest(advance=17))
        completed = subprocess.CompletedProcess(
            ["typer", "--font-manifest"], 2, stdout=b"", stderr=b"old")

        with mock.patch("subprocess.run", return_value=completed):
            loaded = font_metrics.load_runtime_metrics(
                "/old/typer", fallback=fallback)

        self.assertIs(loaded, fallback)

    def test_packaged_manifest_is_valid_and_sorted(self):
        loaded = font_metrics.load_fallback_metrics()

        self.assertTrue(loaded.fonts)
        self.assertEqual(tuple(sorted(loaded.names)), loaded.names)
        self.assertTrue(loaded.metric("JetBrainsMono 12pt").monospaced)
        self.assertEqual(
            loaded.normalize_font("Roboto 16pt"),
            "Roboto 16pt")
        self.assertIn("Roboto Bold 8pt", loaded.names)
        self.assertFalse(loaded.fonts["Roboto 16pt"].monospaced)
        self.assertEqual(loaded.default_font, "JetBrainsMono 12pt")
        self.assertEqual(
            loaded.fonts["Roboto 8pt"].fallback,
            "JetBrainsMono 8pt")
        self.assertFalse(hasattr(loaded, "catalog"))

        with open(font_metrics.FALLBACK_PATH, "r", encoding="utf-8") as stream:
            generated = json.load(stream)
        self.assertNotIn("default_font", generated)
        for entry in generated["fonts"]:
            self.assertNotIn("fallback", entry)
            self.assertNotIn("preview", entry)

    def test_runtime_metrics_inherit_project_fallback_policy(self):
        policy_value = {
            "schema": "font-metrics/v1",
            "wrap_algorithm": "word-v1",
            "fonts": [
                {
                    "name": "Display 12pt",
                    "advance_x": 8,
                    "monospaced": True,
                    "advance_y": 16,
                    "glyph_bounds": {"top": -12, "bottom": 3},
                    "unicode_ranges": [[32, 126]],
                },
                {
                    "name": "Text 12pt",
                    "advance_x": 9,
                    "monospaced": True,
                    "advance_y": 17,
                    "glyph_bounds": {"top": -13, "bottom": 3},
                    "unicode_ranges": [[32, 126], [1040, 1103]],
                },
            ],
        }
        runtime_value = json.loads(json.dumps(policy_value))
        completed = subprocess.CompletedProcess(
            ["typer", "--font-manifest"], 0,
            stdout=json.dumps(runtime_value).encode("utf-8"), stderr=b"")

        with mock.patch("subprocess.run", return_value=completed):
            loaded = font_metrics.load_runtime_metrics(
                "/real/typer",
                fallback=font_metrics.apply_font_policy(
                    font_metrics.parse_manifest(policy_value),
                    default_font="Text 12pt",
                    fallbacks={"Display 12pt": "Text 12pt"}))

        self.assertEqual(loaded.default_font, "Text 12pt")
        self.assertEqual(loaded.fonts["Display 12pt"].fallback, "Text 12pt")

    def test_project_policy_constants_can_be_overridden(self):
        value = {
            "schema": "font-metrics/v1",
            "wrap_algorithm": "word-v1",
            "fonts": [
                {
                    "name": "Display 12pt",
                    "advance_x": 8,
                    "monospaced": True,
                    "advance_y": 16,
                    "glyph_bounds": {"top": -12, "bottom": 3},
                    "unicode_ranges": [[32, 126]],
                },
                {
                    "name": "Text 12pt",
                    "advance_x": 9,
                    "monospaced": True,
                    "advance_y": 17,
                    "glyph_bounds": {"top": -13, "bottom": 3},
                    "unicode_ranges": [[32, 126], [1040, 1103]],
                },
            ],
        }
        with mock.patch.object(font_metrics, "DEFAULT_FONT", "Text 12pt"), \
                mock.patch.object(
                    font_metrics, "FONT_FALLBACKS",
                    {"Display 12pt": "Text 12pt"}):
            parsed = font_metrics.parse_manifest(value)
            configured = font_metrics.parse_project_manifest(value)

        self.assertIs(font_metrics.apply_font_policy(
            parsed, default_font="Text 12pt",
            fallbacks={"Display 12pt": "Text 12pt"}), parsed)
        self.assertEqual(configured.default_font, "Text 12pt")
        self.assertEqual(
            configured.fonts["Display 12pt"].fallback, "Text 12pt")

    def test_text_fallback_is_declared_by_project_policy(self):
        value = {
            "schema": "font-metrics/v1",
            "wrap_algorithm": "word-v1",
            "fonts": [
                {
                    "name": "Display 12pt",
                    "advance_x": 8,
                    "monospaced": True,
                    "advance_y": 16,
                    "glyph_bounds": {"top": -12, "bottom": 3},
                    "unicode_ranges": [[32, 126]],
                },
                {
                    "name": "Text 12pt",
                    "advance_x": 9,
                    "monospaced": True,
                    "advance_y": 17,
                    "glyph_bounds": {"top": -13, "bottom": 3},
                    "unicode_ranges": [[32, 126], [1040, 1103]],
                },
            ],
        }
        metrics = font_metrics.apply_font_policy(
            font_metrics.parse_manifest(value),
            default_font="Text 12pt",
            fallbacks={"Display 12pt": "Text 12pt"})

        self.assertEqual(
            metrics.normalize_for_text("Display 12pt", "HELLO"),
            "Display 12pt")
        self.assertEqual(
            metrics.normalize_for_text("Display 12pt", "ПРИВЕТ"),
            "Text 12pt")
        self.assertEqual(
            metrics.normalize_font("Missing 12pt"),
            "Text 12pt")


class WordV1ParityTest(unittest.TestCase):
    def setUp(self):
        self.metrics = font_metrics.load_fallback_metrics()
        self.font = "JetBrainsMono 12pt"

    def assert_wrap(self, value, width, expected):
        self.assertEqual(
            self.metrics.wrap_text(value, self.font, width), expected)

    def test_short_line(self):
        self.assert_wrap("SHORT", 200, ["SHORT"])

    def test_word_boundaries(self):
        self.assert_wrap(
            "ONE TWO THREE FOUR", 7 * 16,
            ["ONE TWO", "THREE", "FOUR"])

    def test_long_word(self):
        self.assert_wrap("ABCDE", 2 * 16, ["AB", "CD", "E"])

    def test_cyrillic(self):
        self.assert_wrap("АБВГД", 2 * 16, ["АБ", "ВГ", "Д"])

    def test_explicit_line_breaks(self):
        self.assert_wrap(
            "ONE\nTWO THREE\n\nFOUR", 9 * 16,
            ["ONE", "TWO THREE", "", "FOUR"])


if __name__ == "__main__":
    unittest.main()
