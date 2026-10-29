import os
import unittest
from email.message import Message
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import Mock, patch

if os.environ.get("TRANSPORT_OFFLINE_VERIFICATION") != "1":
    raise unittest.SkipTest("Run through verify_offline.py")

from tools.check_transport_startup import cached_preflight, dependency_state


class StartupPreflightTests(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        (self.root / "requirements.txt").write_text("Example==1.0\n", encoding="utf-8")
        (self.root / "app/quick/qml").mkdir(parents=True)
        (self.root / "app/quick/qml/Main.qml").write_text("Item {}", encoding="utf-8")
        self.runner = Mock(return_value=SimpleNamespace(returncode=0))

    def check(self, **options):
        return cached_preflight(self.root, "quick", {"example": "1.0"}, runner=self.runner, **options)

    def test_unchanged_success_skips_child_but_edits_and_force_recheck(self):
        self.assertEqual(self.check(), 0)
        self.assertEqual(self.check(), 0)
        self.assertEqual(self.runner.call_count, 1)
        (self.root / "app/quick/qml/Main.qml").write_text("Item { width: 10 }", encoding="utf-8")
        self.assertEqual(self.check(), 0)
        self.assertEqual(self.runner.call_count, 2)
        self.assertEqual(self.check(force=True), 0)
        self.assertEqual(self.runner.call_count, 3)

    def test_changed_dependencies_and_frontend_invalidate_cache(self):
        self.check()
        cached_preflight(self.root, "quick", {"example": "2.0"}, runner=self.runner)
        cached_preflight(self.root, "widgets", {"example": "2.0"}, runner=self.runner)
        self.assertEqual(self.runner.call_count, 3)
        self.assertIn("install_hardware_guards", self.runner.call_args.args[0][-1])

    def test_renderer_environment_change_invalidates_cache(self):
        with patch.dict(os.environ, {"QSG_RHI_BACKEND": "opengl"}):
            self.check()
            self.check()
        with patch.dict(os.environ, {"QSG_RHI_BACKEND": "d3d11"}):
            self.check()
        self.assertEqual(self.runner.call_count, 2)

    def test_failure_and_concurrent_edits_cannot_certify_source(self):
        self.runner.return_value.returncode = 7
        self.assertEqual(self.check(), 7)
        self.assertFalse(list(self.root.glob(".venv/*.json")))
        def edit(*args, **kwargs):
            path = self.root / "app/quick/qml/Main.qml"
            path.write_text(path.read_text() + " ", encoding="utf-8")
            return SimpleNamespace(returncode=0)
        self.runner.side_effect = edit
        self.assertEqual(self.check(), 1)
        self.assertFalse(list(self.root.glob(".venv/*.json")))

    def test_corrupt_cache_is_not_reused(self):
        cache = self.root / ".venv/.transport-quick-preflight.json"
        cache.parent.mkdir()
        for contents in ("{broken", "[]"):
            cache.write_text(contents, encoding="utf-8")
            self.check()
        self.assertEqual(self.runner.call_count, 2)

    @staticmethod
    def distribution(version, *dependencies):
        data = Message()
        data["Version"] = version
        for dependency in dependencies:
            data["Requires-Dist"] = dependency
        return SimpleNamespace(metadata=data)

    def test_dependency_probe_checks_transitive_versions_and_ignores_inactive_extras(self):
        values = {"Example": self.distribution("1.0", "Child>=2", "Optional; extra == 'optional'"),
                  "Child": self.distribution("1.5")}
        with patch("tools.check_transport_startup.metadata.distribution", side_effect=values.__getitem__) as lookup:
            versions, errors = dependency_state(self.root)
        self.assertEqual(versions, {"child": "1.5", "example": "1.0"})
        self.assertEqual(len(errors), 1)
        self.assertIn("Child 1.5", errors[0])
        self.assertEqual(lookup.call_count, 2)

    def test_missing_dependency_and_active_extra_are_detected(self):
        from importlib.metadata import PackageNotFoundError
        (self.root / "requirements.txt").write_text("Example[optional]==1.0", encoding="utf-8")
        def lookup(name):
            if name == "Example":
                return self.distribution("1.0", "Optional; extra == 'optional'")
            raise PackageNotFoundError(name)
        with patch("tools.check_transport_startup.metadata.distribution", side_effect=lookup):
            _, errors = dependency_state(self.root)
        self.assertEqual(errors, ["Optional is missing"])
