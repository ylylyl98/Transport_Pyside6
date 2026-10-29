"""The release gate must reject source runs and stale package evidence."""
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

if os.environ.get("TRANSPORT_OFFLINE_VERIFICATION") != "1":
    raise unittest.SkipTest("Run with verify_offline.py")

from tools.verify_frozen import isolated_environment, main
from tools.build_windows import build_environment


class PackageVerificationTests(unittest.TestCase):
    def test_build_search_path_cannot_collect_dlls_from_unrelated_tools(self):
        source = {"PATH": "unrelated-poppler;unrelated-Qt", "PYTHONPATH": "another-project",
                  "QT_PLUGIN_PATH": "local-Qt", "GITHUB_SHA": "test-commit"}
        actual = build_environment(source)
        self.assertNotIn("unrelated-poppler", actual["PATH"])
        self.assertNotIn("unrelated-Qt", actual["PATH"])
        self.assertNotIn("PYTHONPATH", actual)
        self.assertNotIn("QT_PLUGIN_PATH", actual)
        self.assertEqual(actual["GITHUB_SHA"], "test-commit")
        self.assertIn("pyinstaller-cache", actual["PYINSTALLER_CONFIG_DIR"])
        self.assertEqual(source["PYTHONPATH"], "another-project")

    def test_clean_environment_does_not_inherit_development_module_paths(self):
        source = {"PYTHONPATH": "checkout", "PYTHONHOME": "venv", "VIRTUAL_ENV": "venv",
                  "QT_PLUGIN_PATH": "local Qt", "QML_IMPORT_PATH": "local QML",
                  "QT_SCALE_FACTOR": "2", "TRANSPORT_UI_PREVIEW": "1", "TEMP": "scratch"}
        actual = isolated_environment(source)
        for key in ("PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV", "QT_PLUGIN_PATH",
                    "QML_IMPORT_PATH", "QT_SCALE_FACTOR", "TRANSPORT_UI_PREVIEW"):
            self.assertNotIn(key, actual)
        self.assertEqual(actual["QT_QPA_PLATFORM"], "offscreen")
        self.assertEqual(actual["TEMP"], "scratch")
        self.assertEqual(source["PYTHONPATH"], "checkout")

    def _gate(self, frozen, publish=True):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bundle, output = root / "bundle", root / "results"
            bundle.mkdir()
            for name in ("Transport.exe", "TransportWorker.exe"):
                (bundle / name).write_bytes(b"fake fixture")
            for mode in ("quick", "widgets"):
                (output / mode).mkdir(parents=True)
                (output / mode / "report.json").write_text(
                    json.dumps({"success": True, "frozen": True}), encoding="utf-8")
            commands = []
            def run(command, **kwargs):
                commands.append(command)
                self.assertNotEqual(Path(command[0]).parent, bundle)
                self.assertNotEqual(kwargs["cwd"], bundle)
                self.assertNotIn("PYTHONPATH", kwargs["env"])
                if publish:
                    destination = Path(command[2])
                    (destination / "report.json").write_text(
                        json.dumps({"success": True, "frozen": frozen}), encoding="utf-8")
                return SimpleNamespace(returncode=0)
            with patch("tools.verify_frozen.subprocess.run", side_effect=run):
                code = main(["--bundle", str(bundle), "--output", str(output)])
            summary = json.loads((output / "summary.json").read_text(encoding="utf-8"))
            self.assertEqual(len(commands), 2)
            self.assertEqual([item["mode"] for item in summary], ["quick", "widgets"])
            return code, summary

    def test_successful_source_execution_is_not_frozen_package_evidence(self):
        code, summary = self._gate(False)
        self.assertEqual(code, 1)
        self.assertTrue(all(not item["success"] for item in summary))

    def test_stale_report_cannot_mask_missing_report_from_new_execution(self):
        code, summary = self._gate(True, publish=False)
        self.assertEqual(code, 1)
        self.assertTrue(all(not item["success"] for item in summary))

    def test_both_relocated_executables_must_publish_fresh_frozen_success(self):
        code, summary = self._gate(True)
        self.assertEqual(code, 0)
        self.assertTrue(all(item["success"] for item in summary))
