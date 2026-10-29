import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

if os.environ.get("TRANSPORT_OFFLINE_VERIFICATION") != "1":
    raise unittest.SkipTest("Run through verify_offline.py")

from PySide6 import QtCore, QtWidgets
from app.offline_sandbox import OfflineSandbox, audit_policy, blocked
from app.ui.style import APP_STYLE, read_app_style
from app.ui.style_manager import apply_app_style, install_style_reload
import transport_ui_preview


class StyleReloadTests(unittest.TestCase):
    def setUp(self):
        self.app = QtWidgets.QApplication.instance()
        self.old_style = self.app.styleSheet()
        self.addCleanup(self.app.setStyleSheet, self.old_style)
        self.scratch = tempfile.TemporaryDirectory()
        self.addCleanup(self.scratch.cleanup)
        self.path = Path(self.scratch.name) / "test.qss"

    def test_valid_style_reloads_existing_widgets_and_font_token(self):
        label = QtWidgets.QLabel("Still the same object")
        self.addCleanup(label.deleteLater)
        self.path.write_text("QWidget { color: #123456; font-size: @BASE_FONT_PT@pt; }", encoding="utf-8")
        result = apply_app_style(path=self.path, font_size_pt=13)
        self.assertTrue(result.ok, result.error)
        self.assertIn("font-size: 13pt", self.app.styleSheet())
        self.assertEqual(label.text(), "Still the same object")
        label.ensurePolished()
        self.assertEqual(label.font().pointSize(), 13)

    def test_bad_or_missing_style_keeps_last_applied_sheet(self):
        self.app.setStyleSheet("QWidget { color: #123456; }")
        expected = self.app.styleSheet()
        for content in ("", "QWidget { color: red;", "QWidget { color red; }", "/* unfinished"):
            self.path.write_text(content, encoding="utf-8")
            result = apply_app_style(path=self.path)
            self.assertFalse(result.ok, content)
            self.assertEqual(self.app.styleSheet(), expected)
        self.assertFalse(apply_app_style(path=self.path.with_name("missing.qss")).ok)
        self.assertEqual(self.app.styleSheet(), expected)

    def test_font_bounds_do_not_replace_style(self):
        self.app.setStyleSheet("QWidget { color: #123456; }")
        expected = self.app.styleSheet()
        for size in (6, 19, 10.5, True, None, float("nan")):
            self.assertFalse(apply_app_style(font_size_pt=size).ok)
            self.assertEqual(self.app.styleSheet(), expected)
        self.assertNotIn("@BASE_FONT_PT@", APP_STYLE)
        self.assertIn("font-size: 18pt;", read_app_style(font_size_pt=18))

    def test_invalid_widget_and_inactive_rules_preserve_existing_appearance(self):
        button = QtWidgets.QPushButton("Existing button")
        self.addCleanup(button.deleteLater)
        self.app.setStyleSheet("QPushButton { color: #123456; }")
        button.ensurePolished()
        previous = self.app.styleSheet()
        color = button.palette().color(button.foregroundRole())
        for content in (
            "QPushButton { color: red background-color: blue; }",
            'QPushButton[role="future"]:hover { imaginary-property: 1; }',
            "QSpinBox::up-button { imaginary-property: 1; }",
        ):
            with self.subTest(content=content):
                self.path.write_text(content, encoding="utf-8")
                result = apply_app_style(path=self.path)
                self.assertFalse(result.ok)
                self.assertEqual(self.app.styleSheet(), previous)
                self.assertEqual(button.palette().color(button.foregroundRole()), color)

    def test_quoted_values_and_property_selectors_remain_valid(self):
        self.path.write_text('QPushButton[role="primary"]:hover { font-family: "Segoe UI"; color: #123456; }',
                             encoding="utf-8")
        result = apply_app_style(path=self.path)
        self.assertTrue(result.ok, result.error)

    def test_late_application_parse_failure_restores_exact_live_style(self):
        previous = "QWidget { color: #123456; background-color: #fedcba; }"
        candidate = "QWidget { color: red; }"
        self.app.setStyleSheet(previous)
        self.path.write_text(candidate, encoding="utf-8")
        native_set_style = self.app.setStyleSheet

        def set_style(sheet):
            native_set_style(sheet)
            if sheet == candidate:
                QtCore.qWarning("Could not parse application stylesheet")

        with patch.object(self.app, "setStyleSheet", side_effect=set_style):
            result = apply_app_style(path=self.path)
        self.assertFalse(result.ok)
        self.assertIn("Could not parse", result.error)
        self.assertEqual(self.app.styleSheet(), previous)

    def test_action_reads_current_font_provider_each_time(self):
        window = QtWidgets.QMainWindow()
        self.addCleanup(window.deleteLater)
        menu = window.menuBar().addMenu("View")
        size = [11]
        action = install_style_reload(window, menu, font_size_provider=lambda: size[0])
        action.trigger()
        self.assertIn("font-size: 11pt;", self.app.styleSheet())
        size[0] = 14
        action.trigger()
        self.assertIn("font-size: 14pt;", self.app.styleSheet())
        self.assertEqual(action.shortcut().toString(), "Ctrl+Alt+R")
        self.assertIn("reloaded", window.statusBar().currentMessage())


class OfflinePolicyTests(unittest.TestCase):
    def test_frozen_preview_is_unavailable_while_source_launch_is_preserved(self):
        from app.ui.main_window import MainWindow
        root = Path(__file__).resolve().parents[1]
        for frozen in (True, False):
            with self.subTest(frozen=frozen), patch.object(sys, "frozen", frozen, create=True), \
                    patch.dict(os.environ, {"TRANSPORT_UI_PREVIEW": "0"}), \
                    patch("app.ui.dock.ConnDock._start_scan"), \
                    patch.object(QtCore.QProcess, "startDetached", return_value=(True, 123)) as start:
                window = MainWindow()
                try:
                    self.assertEqual(window.preview_action.isEnabled(), not frozen)
                    window._open_ui_preview()
                    if frozen:
                        self.assertIn("source checkout", window.preview_action.text())
                        self.assertIn("source checkout", window.statusBar().currentMessage())
                        start.assert_not_called()
                    else:
                        self.assertEqual(window.preview_action.text(), "Offline UI preview…")
                        start.assert_called_once_with(sys.executable, [str(root / "transport_ui_preview.py")], str(root))
                finally:
                    window.close()
                    window.deleteLater()
                    QtWidgets.QApplication.instance().sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)
                    QtWidgets.QApplication.instance().processEvents()

    def test_preview_isolates_settings_and_blocks_native_children_and_sdk(self):
        from contextlib import ExitStack
        from utils.config import cfg
        root = Path(__file__).resolve().parents[1]
        with ExitStack() as cleanup:
            directory = cleanup.enter_context(tempfile.TemporaryDirectory())
            cleanup.enter_context(patch.dict(os.environ))
            audit = cleanup.enter_context(patch("sys.addaudithook"))
            for target in ("pyvisa.ResourceManager", "nidaqmx.Task", "nidaqmx.system.System.local",
                           "app.hw_discovery.scan_all", "app.devices.attodry2100_adapter._load_sdk",
                           "PySide6.QtCore.QProcess.start", "PySide6.QtCore.QProcess.startDetached",
                           "PySide6.QtCore.QProcess.execute", "PySide6.QtCore.QSettings.setPath"):
                cleanup.enter_context(patch(target))
            cleanup.enter_context(patch.object(cfg.filename, "base_out"))
            sandbox = OfflineSandbox(root, preview=True, state_directory=directory)
            sandbox.install_hardware_guards()
            self.assertEqual(os.environ["TRANSPORT_UI_PREVIEW"], "1")
            self.assertEqual(Path(os.environ["SPECTRALSWEEP_CONFIG_PATH"]), Path(directory) / "config.json")
            self.assertEqual((Path(directory) / "config.json").read_text(), "{}")
            self.assertNotEqual(Path(os.environ["MPLCONFIGDIR"]).parent, Path(directory))
            self.assertIs(QtCore.QProcess.start, blocked)
            import app.devices.attodry2100_adapter
            self.assertIs(app.devices.attodry2100_adapter._load_sdk, blocked)
            with self.assertRaises(RuntimeError):
                audit.call_args.args[0]("socket.connect", ())

    def test_only_exact_renderer_child_is_allowed_in_tests(self):
        root = Path(__file__).resolve().parents[1]
        executable = sys.executable
        args = [executable, str(root / "transport_png_exporter.py"), "--spool",
                str(Path(tempfile.gettempdir()) / "transport-png-test"), "--owner-pid", str(os.getpid())]
        policy = audit_policy(root)
        policy("subprocess.Popen", (executable, args, str(root), {}))
        policy("subprocess.Popen", (executable, args + ["--after-pid", "123"], str(root), {}))
        with self.assertRaises(RuntimeError):
            audit_policy(root, preview=True)("subprocess.Popen", (executable, args, str(root), {}))
        for changed in (args + ["--extra"], args[:1] + ["elsewhere.py"] + args[2:],
                        args[:-1] + [str(os.getpid() + 1)]):
            with self.assertRaises(RuntimeError):
                policy("subprocess.Popen", (executable, changed, str(root), {}))
        for event in ("socket.connect", "socket.bind", "socket.getaddrinfo", "os.system"):
            with self.assertRaises(RuntimeError):
                policy(event, ())

    def test_runner_uses_temporary_font_cache(self):
        # Reusable preview fonts must not leak into normal verification runs.
        self.assertEqual(Path(os.environ["MPLCONFIGDIR"]).parent, Path(os.environ["APPDATA"]))

    def test_preview_rebuild_replaces_only_child_and_restores_selected_tab(self):
        calls = []
        def child(command, **kwargs):
            calls.append((list(command), kwargs))
            return transport_ui_preview.REBUILD_EXIT_CODE if len(calls) == 1 else 0
        with patch("subprocess.call", side_effect=child):
            result = transport_ui_preview.main(["--tab", "3", "--offscreen"])
        self.assertEqual(result, 0)
        self.assertEqual(len(calls), 2)
        first, second = calls[0][0], calls[1][0]
        self.assertIn("--child", first)
        self.assertIn("--tab", first)
        self.assertNotIn("--tab", second)
        self.assertEqual(first[first.index("--state-directory") + 1], second[second.index("--state-directory") + 1])


if __name__ == "__main__":
    unittest.main()
