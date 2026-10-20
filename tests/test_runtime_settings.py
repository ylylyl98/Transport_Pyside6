import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

if os.environ.get("TRANSPORT_OFFLINE_VERIFICATION") != "1":
    raise unittest.SkipTest("Requires offline wrapper")

from PySide6 import QtWidgets
from app.runtime_settings import apply_runtime_settings, validate_values
from app.ui.runtime_settings_dialog import RuntimeSettingsDialog
from utils.config import AppConfig


class RuntimeSettingsTests(unittest.TestCase):
    def test_large_font_in_small_dialog_scrolls_settings_and_keeps_close_visible(self):
        from PySide6 import QtCore
        from app.ui.style_manager import apply_app_style
        application = QtWidgets.QApplication.instance()
        original_style = application.styleSheet()
        config = AppConfig()
        config.font_size_pt = 18
        dialog = None
        try:
            result = apply_app_style(font_size_pt=18)
            self.assertTrue(result.ok, result.error)
            dialog = RuntimeSettingsDialog(None, config, lambda: True)
            dialog.resize(520, 400)
            dialog.show()
            application.processEvents()
            scroll = dialog.findChild(QtWidgets.QScrollArea)
            self.assertIsNotNone(scroll)
            self.assertGreater(scroll.verticalScrollBar().maximum(), 0)
            self.assertLessEqual(dialog.height(), 400)
            buttons = dialog.findChild(QtWidgets.QDialogButtonBox)
            close = buttons.button(QtWidgets.QDialogButtonBox.StandardButton.Close)
            self.assertFalse(scroll.isAncestorOf(close))
            for target in (dialog.appearance_button, dialog.run_button):
                scroll.ensureWidgetVisible(target, 0, 0)
                application.processEvents()
                target_center = target.mapTo(scroll.viewport(), target.rect().center())
                self.assertTrue(scroll.viewport().rect().contains(target_center))
                self.assertTrue(close.isVisible())
                close_rect = QtCore.QRect(close.mapTo(dialog, QtCore.QPoint()), close.size())
                self.assertTrue(dialog.rect().contains(close_rect))
            close.click()
            self.assertFalse(dialog.isVisible())
        finally:
            if dialog is not None:
                dialog.close()
                dialog.deleteLater()
            application.setStyleSheet(original_style)
            application.processEvents()

    def test_active_run_retains_config_and_does_not_save(self):
        config = AppConfig()
        saved = Mock()
        with self.assertRaisesRegex(RuntimeError, "Wait for measurements"):
            apply_runtime_settings(config, {"mcd.endpoint_stable_reads": 7}, is_idle=lambda: False, save=saved)
        self.assertEqual(config.mcd.endpoint_stable_reads, 3)
        saved.assert_not_called()

    def test_idle_apply_persists_without_replacing_shared_config(self):
        config = AppConfig()
        original = config.mcd
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            changes = apply_runtime_settings(config, {"mcd.endpoint_stable_reads": 7,
                "mcd.transport_endpoint_settling_enabled": True}, is_idle=lambda: True,
                save=lambda: config.save(path))
            restored = AppConfig()
            restored.load(path)
        self.assertIs(config.mcd, original)
        self.assertEqual(restored.mcd.endpoint_stable_reads, 7)
        self.assertTrue(restored.mcd.transport_endpoint_settling_enabled)
        self.assertEqual(len(changes), 2)
        self.assertEqual(restored.magnet, config.magnet)

    def test_appearance_can_apply_during_measurement(self):
        config = AppConfig()
        font, save = Mock(), Mock()
        apply_runtime_settings(config, {"font_size_pt": 12}, is_idle=lambda: False,
                               apply_font=font, rollback_appearance=Mock(), save=save)
        self.assertEqual(config.font_size_pt, 12)
        font.assert_called_once_with(12)
        save.assert_called_once_with()

    def test_failed_save_restores_live_font_and_settings(self):
        config = AppConfig()
        font, rollback = Mock(), Mock()
        with self.assertRaises(OSError):
            apply_runtime_settings(config, {"font_size_pt": 12, "mcd.endpoint_stable_reads": 9},
                is_idle=lambda: True, apply_font=font, rollback_appearance=rollback,
                save=Mock(side_effect=OSError("disk full")))
        self.assertEqual(config.font_size_pt, 9)
        self.assertEqual(config.mcd.endpoint_stable_reads, 3)
        font.assert_called_once_with(12)
        rollback.assert_called_once_with()

    def test_font_update_requires_a_complete_appearance_rollback(self):
        config = AppConfig()
        font, save = Mock(), Mock()
        with self.assertRaisesRegex(ValueError, "rollback callback"):
            apply_runtime_settings(config, {"font_size_pt": 12}, is_idle=lambda: True,
                                   apply_font=font, save=save)
        self.assertEqual(config.font_size_pt, 9)
        font.assert_not_called()
        save.assert_not_called()

    def test_checkbox_apply_preserves_unedited_precision_and_clamped_config(self):
        config = AppConfig()
        config.mcd.transport_endpoint_settling_timeout_s = 120.125
        config.mcd.sweep_progress_epsilon_t = 0.000100000125
        config.mcd.endpoint_stable_reads = 1500
        original_mcd = config.mcd
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            save = config.save
            config.save = Mock(side_effect=lambda: save(path))
            dialog = RuntimeSettingsDialog(None, config, lambda: True)
            try:
                self.assertEqual(dialog.controls["mcd.transport_endpoint_settling_timeout_s"].value(), 120.13)
                self.assertEqual(dialog.controls["mcd.endpoint_stable_reads"].value(), 1000)
                dialog.controls["mcd.transport_endpoint_settling_enabled"].setChecked(True)
                dialog.run_button.click()
                restored = AppConfig()
                restored.load(path)
                self.assertTrue(restored.mcd.transport_endpoint_settling_enabled)
                self.assertEqual(restored.mcd.transport_endpoint_settling_timeout_s, 120.125)
                self.assertEqual(restored.mcd.sweep_progress_epsilon_t, 0.000100000125)
                self.assertEqual(restored.mcd.endpoint_stable_reads, 1500)
                self.assertIs(config.mcd, original_mcd)
                config.save.assert_called_once_with()
            finally:
                dialog.close()
                dialog.deleteLater()
                QtWidgets.QApplication.instance().processEvents()

    def test_edit_then_revert_display_preserves_original_raw_values(self):
        config = AppConfig()
        config.mcd.transport_endpoint_settling_timeout_s = 120.125
        config.mcd.endpoint_stable_reads = 1500
        config.save = Mock()
        dialog = RuntimeSettingsDialog(None, config, lambda: True)
        try:
            timeout = dialog.controls["mcd.transport_endpoint_settling_timeout_s"]
            reads = dialog.controls["mcd.endpoint_stable_reads"]
            timeout.setValue(121)
            timeout.setValue(120.13)
            reads.setValue(999)
            reads.setValue(1000)
            dialog.run_button.click()
            config.save.assert_not_called()
            self.assertEqual(config.mcd.transport_endpoint_settling_timeout_s, 120.125)
            self.assertEqual(config.mcd.endpoint_stable_reads, 1500)
            self.assertEqual(dialog.message.text(), "No settings changed.")
        finally:
            dialog.close()
            dialog.deleteLater()
            QtWidgets.QApplication.instance().processEvents()

    def test_failed_save_restores_exact_live_style_and_preserves_pending_font(self):
        application = QtWidgets.QApplication.instance()
        previous_style = application.styleSheet()
        live_style = "QWidget { background: #112233; color: #ddeeff; font-size: 9pt; }"
        application.setStyleSheet(live_style)
        config = AppConfig()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            style = root / "edited.qss"
            style.write_text("QWidget { background: #334455; color: white; font-size: @BASE_FONT_PT@pt; }", encoding="utf-8")
            saved_config = root / "settings.json"
            persist = config.save
            attempts = []

            def save():
                attempts.append(application.styleSheet())
                if len(attempts) == 1:
                    raise OSError("simulated disk failure")
                persist(saved_config)

            config.save = save
            dialog = RuntimeSettingsDialog(None, config, lambda: True)
            try:
                with patch("app.ui.style.STYLE_PATH", style):
                    dialog.controls["font_size_pt"].setValue(12)
                    dialog.appearance_button.click()
                    self.assertIn("#334455", attempts[0])
                    self.assertIn("12pt", attempts[0])
                    self.assertEqual(application.styleSheet(), live_style)
                    self.assertEqual(config.font_size_pt, 9)
                    self.assertEqual(dialog.controls["font_size_pt"].value(), 12)
                    self.assertIn("simulated disk failure", dialog.message.text())
                    self.assertFalse(saved_config.exists())

                    dialog.appearance_button.click()
                    self.assertEqual(config.font_size_pt, 12)
                    self.assertIn("#334455", application.styleSheet())
                    restored = AppConfig()
                    restored.load(saved_config)
                    self.assertEqual(restored.font_size_pt, 12)
                    dialog.appearance_button.click()
                    self.assertEqual(len(attempts), 2)
                    self.assertEqual(dialog.message.text(), "No settings changed.")
                    dialog.controls["font_size_pt"].setValue(9)
                    dialog.appearance_button.click()
                    self.assertEqual(config.font_size_pt, 9)
                    self.assertEqual(len(attempts), 3)
            finally:
                dialog.close()
                dialog.deleteLater()
                application.setStyleSheet(previous_style)
                application.processEvents()

    def test_unsupported_and_invalid_values_are_rejected_before_changes(self):
        for values in ({"magnet.maximum_field_t": 100}, {"mcd.endpoint_stable_reads": True},
                       {"mcd.endpoint_stable_reads": 0}, {"mcd.transport_monitor_recovery_timeout_s": float("nan")},
                       {"mcd.transport_endpoint_settling_enabled": "false"}, {"font_size_pt": 30}):
            with self.subTest(values=values), self.assertRaises(ValueError):
                validate_values(values)

    def test_dialog_rechecks_idle_on_apply_even_if_button_was_enabled(self):
        config = AppConfig()
        config.save = Mock()
        idle = [True]
        dialog = RuntimeSettingsDialog(None, config, lambda: idle[0])
        try:
            dialog.controls["mcd.endpoint_stable_reads"].setValue(8)
            idle[0] = False
            dialog.run_button.click()
            self.assertEqual(config.mcd.endpoint_stable_reads, 3)
            config.save.assert_not_called()
            self.assertIn("not applied", dialog.message.text())
            dialog._refresh_availability()
            self.assertFalse(dialog.run_button.isEnabled())
            self.assertTrue(dialog.appearance_button.isEnabled())
            self.assertEqual(dialog.controls["mcd.endpoint_stable_reads"].value(), 8)
            idle[0] = True
            dialog._refresh_availability()
            dialog.run_button.click()
            self.assertEqual(config.mcd.endpoint_stable_reads, 8)
            dialog.controls["mcd.endpoint_stable_reads"].setValue(3)
            dialog.run_button.click()
            self.assertEqual(config.mcd.endpoint_stable_reads, 3)
            self.assertEqual(config.save.call_count, 2)
        finally:
            dialog.close()
            dialog.deleteLater()
            QtWidgets.QApplication.instance().processEvents()
