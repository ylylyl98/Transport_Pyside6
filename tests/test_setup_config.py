import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from utils.config import AppConfig, SetupConfig


class SetupConfigTests(unittest.TestCase):
    def test_supported_and_invalid_pairs(self):
        for magnet, temperature in (("1000", "ls335"), ("2100", "2100"), ("none", "external")):
            with self.subTest(magnet=magnet):
                SetupConfig(magnet, temperature).validate()
        for magnet, temperature in (("unknown", "2100"), ("1000", "2100"), (None, "external")):
            with self.subTest(magnet=magnet, temperature=temperature):
                with self.assertRaisesRegex(ValueError, "supported magnet_backend"):
                    SetupConfig(magnet, temperature).validate()

    def test_load_legacy_and_profile_without_replacing_subconfig(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            config = AppConfig()
            original = config.setup
            path.write_text(json.dumps({"setup": {"magnet_backend": "none", "temperature_source": "external"}}), encoding="utf-8")
            config.load(path)
            self.assertIs(config.setup, original)
            self.assertEqual(config.setup.magnet_backend, "none")
            path.write_text(json.dumps({"font_size_pt": 11}), encoding="utf-8")
            config.load(path)
            self.assertEqual((config.setup.magnet_backend, config.setup.temperature_source), ("1000", "ls335"))
            self.assertEqual(config.font_size_pt, 11)

    def test_invalid_load_is_rejected_before_other_updates(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            config = AppConfig()
            for setup in ({"magnet_backend": "bluefors"}, [], {"unexpected": "value"}):
                path.write_text(json.dumps({"setup": setup, "font_size_pt": 12}), encoding="utf-8")
                with self.subTest(setup=setup), self.assertRaisesRegex(ValueError, "Invalid setup"):
                    config.load(path)
                self.assertEqual(config.font_size_pt, 9)

    def test_missing_file_defaults_but_invalid_existing_file_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            config = AppConfig()
            config.load(path)
            self.assertEqual(config.setup, SetupConfig())
            for contents in ('{"setup":', '[]', 'null', '"not an object"'):
                path.write_text(contents, encoding="utf-8")
                with self.subTest(contents=contents), self.assertRaises(ValueError) as error:
                    config.load(path)
                self.assertIn(str(path), str(error.exception))
                self.assertEqual(config.setup, SetupConfig())
            with patch.object(Path, "open", side_effect=PermissionError("private details")):
                with self.assertRaises(ValueError) as error:
                    config.load(path)
                self.assertIn(str(path), str(error.exception))
                self.assertNotIn("private details", str(error.exception))


if __name__ == "__main__":
    unittest.main()
