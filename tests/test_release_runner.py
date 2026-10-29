"""CI must run every offline module once and reject missing child evidence."""
import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

if os.environ.get("TRANSPORT_OFFLINE_VERIFICATION") != "1":
    raise unittest.SkipTest("Run through verify_offline.py")

from tools import verify_quick_release as runner


class ReleaseRunnerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.output = self.root / "output"
        self.output.mkdir()

    def test_discovery_includes_both_test_trees_without_generated_copies(self):
        for name in ("tests/test_main.py", "tests/nested/test_nested.py", "spectra_app/test_adapter.py",
                     "tmp/backup/tests/test_old.py", "tests/helper.py"):
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.touch()
        self.assertEqual(runner.discover_modules(self.root),
                         ["spectra_app.test_adapter", "tests.nested.test_nested", "tests.test_main"])

    def test_shards_are_disjoint_complete_and_independent_of_input_order(self):
        modules = runner.discover_modules()
        self.assertIn("spectra_app.test_attodry2100_controller", modules)
        self.assertIn("spectra_app.test_attodry2100_adapter", modules)
        self.assertIn("spectra_app.test_attodry2100_commissioning", modules)
        self.assertIn("spectra_app.test_aps100_adapter", modules)
        selections = [runner.select_modules(list(reversed(modules)), shard_index=index, shard_count=4)[1]
                      for index in range(4)]
        combined = [module for shard in selections for module in shard]
        self.assertEqual(sorted(combined), modules)
        self.assertEqual(len(set(combined)), len(modules))
        self.assertLessEqual(max(map(len, selections)) - min(map(len, selections)), 1)

    def test_invalid_or_empty_selection_cannot_succeed(self):
        for modules, options in (([], {}), (["tests.test_one"], {"shard_count": 0}),
                                 (["tests.test_one"], {"shard_count": 2}),
                                 (["tests.test_one"], {"shard_index": -1}),
                                 (["tests.test_one"], {"shard_index": 1}),
                                 (["tests.test_one", "tests.test_one"], {}),
                                 (["../outside"], {}),
                                 (["tests.test_quick_one"], {"legacy_only": True})):
            with self.subTest(modules=modules, options=options), self.assertRaises(ValueError):
                runner.select_modules(modules, **options)

    def test_list_mode_does_not_run_children_or_create_evidence(self):
        with patch.object(runner, "run_module") as run, contextlib.redirect_stdout(io.StringIO()) as output:
            result = runner.main(["tests.test_two", "tests.test_one", "--list", "--shard-count", "2",
                                  "--shard-index", "1", "--output", str(self.root / "unused")])
        self.assertEqual(result, 0)
        self.assertEqual(output.getvalue().strip(), "tests.test_two")
        run.assert_not_called()
        self.assertFalse((self.root / "unused").exists())

    def report(self, **changes):
        path = self.root / "tmp/verification/reports/offline_results_tests_test_example.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        data = dict(tests=3, failures=0, errors=0, skipped=0, success=True,
                    hardware="blocked", settings="temporary")
        data.update(changes)
        path.write_text(json.dumps(data), encoding="utf-8")
        return path

    def run_child(self, callback):
        with patch.object(runner.subprocess, "run", side_effect=callback):
            return runner.run_module("tests.test_example", self.output, root=self.root)

    def test_child_runs_through_guard_and_propagates_evidence(self):
        def child(command, **kwargs):
            self.assertEqual(command[-2:], [str(self.root / "verify_offline.py"), "tests.test_example"])
            self.assertIn("-B", command)
            self.assertEqual(kwargs["cwd"], self.root)
            self.report()
            return SimpleNamespace(returncode=0, stdout=b"ok\n", stderr=b"")
        result = self.run_child(child)
        self.assertEqual(result["exit_code"], 0)
        self.assertEqual(result["tests"], 3)

    def test_missing_empty_or_nonisolated_report_fails_even_if_child_exits_zero(self):
        stale = self.report()
        result = self.run_child(lambda *a, **kw: SimpleNamespace(returncode=0, stdout=b"", stderr=b""))
        self.assertFalse(stale.exists())
        self.assertEqual(result["exit_code"], 125)
        for changes in ({"tests": 0}, {"skipped": 3}, {"tests": True}, {"errors": 1},
                        {"hardware": "enabled"}, {"settings": "user"}, {"success": False}):
            def child(*args, **kwargs):
                self.report(**changes)
                return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")
            with self.subTest(changes=changes):
                self.assertEqual(self.run_child(child)["exit_code"], 125)

    def test_timeout_and_start_failure_are_reported_as_failures(self):
        for exception, expected in ((subprocess.TimeoutExpired("child", 180, output=b"partial"), 124),
                                    (OSError("missing interpreter"), 127)):
            with self.subTest(exception=exception), patch.object(runner.subprocess, "run", side_effect=exception):
                result = runner.run_module("tests.test_example", self.output, root=self.root)
                self.assertEqual(result["exit_code"], expected)

    def test_main_records_selection_and_fails_when_any_module_fails(self):
        def run(module, output):
            return dict(module=module, exit_code=1 if module.endswith("two") else 0, elapsed_s=1.0,
                        tests=2, failures=1 if module.endswith("two") else 0, errors=0, skipped=0)
        github_summary = self.root / "github.md"
        with patch.object(runner, "run_module", side_effect=run), patch.dict(os.environ, {"GITHUB_STEP_SUMMARY": str(github_summary)}), contextlib.redirect_stdout(io.StringIO()):
            result = runner.main(["tests.test_two", "tests.test_one", "--output", str(self.output)])
        self.assertEqual(result, 1)
        selection = json.loads((self.output / "selection.json").read_text())
        self.assertEqual(selection["modules"], ["tests.test_one", "tests.test_two"])
        results = json.loads((self.output / "summary.json").read_text())
        self.assertEqual(len(results), 2)
        self.assertIn("1/2 modules passed", github_summary.read_text(encoding="utf-8"))
        self.assertIn("FAIL (1)", (self.output / "summary.md").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
