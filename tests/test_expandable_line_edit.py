import os
import unittest
from unittest.mock import patch

if os.environ.get("TRANSPORT_OFFLINE_VERIFICATION") != "1":
    raise unittest.SkipTest("Requires isolated offline wrapper")

from PySide6 import QtCore, QtGui, QtTest, QtWidgets
from app.ui.widgets.expandable_line_edit import ExpandableLineEdit, ExpandedValueDialog


class ExpandableLineEditTests(unittest.TestCase):
    def setUp(self):
        self.app = QtWidgets.QApplication.instance()
        self.field = ExpandableLineEdit("original", title="Edit filename stem")
        self.wrapper = self.field.field_widget()
        self.wrapper.resize(300, 50)
        self.wrapper.show()
        self.app.processEvents()
        self.dialog = None

    def tearDown(self):
        if self.dialog is not None:
            self.dialog.hide()
            self.dialog.deleteLater()
        self.wrapper.hide()
        self.wrapper.deleteLater()
        self.app.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)

    def editor(self):
        self.dialog = ExpandedValueDialog(self.field)
        return self.dialog

    def test_unbroken_unicode_preview_wraps_without_changing_value(self):
        value = "sample_" + "器件标识_" * 24 + "__17Hz_200mV"
        self.field.setText(value)
        self.app.processEvents()
        preview = self.field._preview
        self.assertTrue(preview.isVisible())
        self.assertEqual(preview.toPlainText(), value)
        self.assertEqual(self.field.text(), value)
        self.assertEqual(preview.horizontalScrollBar().maximum(), 0)
        self.assertLessEqual(preview.height(), preview.fontMetrics().lineSpacing() * 5 + 10)
        self.field.setText("short")
        self.assertTrue(preview.isHidden())

    def test_signal_blocked_restore_updates_full_preview(self):
        value = "D:\\Instrument control v3\\" + "实验目录_" * 25
        blocker = QtCore.QSignalBlocker(self.field)
        self.field.setText(value)
        del blocker
        self.assertEqual(self.field._preview.toPlainText(), value)
        self.assertEqual(self.field.text(), value)

    def test_cancel_preserves_original_and_emits_no_edit_signals(self):
        edited = QtTest.QSignalSpy(self.field.textEdited)
        finished = QtTest.QSignalSpy(self.field.editingFinished)
        dialog = self.editor()
        dialog.editor.setPlainText("draft_" * 100)
        dialog.reject()
        self.assertEqual(self.field.text(), "original")
        self.assertEqual(edited.count(), 0)
        self.assertEqual(finished.count(), 0)

    def test_save_preserves_exact_path_and_notifies_existing_consumers_once(self):
        value = "D:\\Data root\\实验 样品\\" + "long_name_" * 30
        changed = QtTest.QSignalSpy(self.field.textChanged)
        edited = QtTest.QSignalSpy(self.field.textEdited)
        finished = QtTest.QSignalSpy(self.field.editingFinished)
        dialog = self.editor()
        dialog.editor.setPlainText(value)
        dialog.apply_value()
        self.assertEqual(self.field.text(), value)
        self.assertEqual(dialog.result(), QtWidgets.QDialog.DialogCode.Accepted)
        self.assertEqual((changed.count(), edited.count(), finished.count()), (1, 1, 1))

    def test_visual_wrap_is_allowed_but_actual_newlines_are_rejected_for_names(self):
        dialog = self.editor()
        for draft in ("first\nsecond", "first\u2028second", "first\u2029second"):
            dialog.editor.setPlainText(draft)
            dialog.apply_value()
            self.assertIn("Remove line breaks", dialog.error_label.text())
            self.assertEqual(self.field.text(), "original")

    def test_multiline_field_preserves_newlines_and_requires_ctrl_enter(self):
        from app.engine.gate_scan_field_batch import GateScanFieldBatch
        self.field.allow_newlines = True
        dialog = self.editor()
        dialog.editor.setPlainText("-2\n-0.5\n1:-1:-1")
        dialog.editor.moveCursor(QtGui.QTextCursor.MoveOperation.End)
        QtTest.QTest.keyClick(dialog.editor, QtCore.Qt.Key.Key_Return)
        self.assertEqual(self.field.text(), "original")
        draft = dialog.editor.toPlainText()
        QtTest.QTest.keyClick(dialog.editor, QtCore.Qt.Key.Key_Return, QtCore.Qt.KeyboardModifier.ControlModifier)
        self.assertEqual(self.field.text(), draft)
        self.assertEqual(GateScanFieldBatch.parse_fields(self.field.text()), (-2.0, -0.5, 1.0, 0.0, -1.0))

    def test_oversize_utf16_draft_is_rejected_instead_of_truncated(self):
        self.field.setMaxLength(8)
        dialog = self.editor()
        dialog.editor.setPlainText("😀" * 5)
        dialog.apply_value()
        self.assertEqual(self.field.text(), "original")
        self.assertIn("8 character limit", dialog.error_label.text())
        dialog.editor.setPlainText("😀" * 4)
        dialog.apply_value()
        self.assertEqual(self.field.text(), "😀" * 4)

    def test_disabled_or_readonly_source_cannot_be_changed_by_open_editor(self):
        dialog = self.editor()
        dialog.editor.setPlainText("new value")
        self.field.setEnabled(False)
        dialog.apply_value()
        self.assertEqual(self.field.text(), "original")
        self.field.setEnabled(True)
        self.field.setReadOnly(True)
        dialog.apply_value()
        self.assertEqual(self.field.text(), "original")
        self.assertIn("locked", dialog.error_label.text())

    def test_stale_draft_does_not_overwrite_reloaded_value(self):
        dialog = self.editor()
        dialog.editor.setPlainText("draft")
        self.field.setText("reloaded")
        dialog.apply_value()
        self.assertEqual(self.field.text(), "reloaded")
        self.assertIn("changed while", dialog.error_label.text())

    def test_validator_still_applies_in_expanded_editor(self):
        self.field.setValidator(QtGui.QRegularExpressionValidator(QtCore.QRegularExpression(r"\d+"), self.field))
        dialog = self.editor()
        dialog.editor.setPlainText("invalid")
        dialog.apply_value()
        self.assertEqual(self.field.text(), "original")
        self.assertIn("required format", dialog.error_label.text())
        dialog.editor.setPlainText("12345")
        dialog.apply_value()
        self.assertEqual(self.field.text(), "12345")

    def test_alt_enter_opens_editor_and_return_applies_single_value(self):
        def fill_and_apply():
            dialog = self.field._dialog
            self.assertIsNotNone(dialog)
            dialog.editor.setPlainText("expanded draft")
            QtTest.QTest.keyClick(dialog.editor, QtCore.Qt.Key.Key_Return)
        QtCore.QTimer.singleShot(0, fill_and_apply)
        QtTest.QTest.keyClick(self.field, QtCore.Qt.Key.Key_Return, QtCore.Qt.KeyboardModifier.AltModifier)
        self.assertEqual(self.field.text(), "expanded draft")
        self.assertIsNone(self.field._dialog)

    def test_folder_picker_cancel_and_reload_do_not_overwrite_original(self):
        with patch.object(QtWidgets.QFileDialog, "getExistingDirectory", return_value=""):
            self.field._browse_directory()
        self.assertEqual(self.field.text(), "original")
        with patch.object(QtWidgets.QFileDialog, "getExistingDirectory", return_value=r"D:\New root"):
            self.field._browse_directory()
        self.assertEqual(self.field.text(), r"D:\New root")
        def reload(*_):
            self.field.setText("reloaded")
            return r"D:\Stale selection"
        with patch.object(QtWidgets.QFileDialog, "getExistingDirectory", side_effect=reload):
            self.field._browse_directory()
        self.assertEqual(self.field.text(), "reloaded")


class LongInputIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from app.ui.main_window import MainWindow
        cls.app = QtWidgets.QApplication.instance()
        with patch("app.ui.dock.ConnDock._start_scan"):
            cls.window = MainWindow()
        cls.window.show()
        cls.app.processEvents()

    @classmethod
    def tearDownClass(cls):
        cls.window.close()
        cls.window.deleteLater()
        cls.app.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)

    def test_all_filename_inputs_preserve_recipe_and_settings_interfaces(self):
        w = self.window
        names = ("tab_dual", "tab_cosweep", "tab_gate_scan", "tab_bfield_gate_scan",
                 "tab_bfield_transport", "tab_photocurrent")
        from app.settings import get_app_settings
        settings = get_app_settings()
        for name in names:
            with self.subTest(tab=name):
                tab = getattr(w, name)
                field = tab.ed_base
                self.assertIsInstance(field, ExpandableLineEdit)
                original = field.text()
                draft = "device_" + "long_id_" * 16
                dialog = ExpandedValueDialog(field)
                dialog.editor.setPlainText(draft)
                dialog.apply_value()
                self.assertEqual(tab._widget_setting_value(field), draft)
                tab._save_tab_widget_settings("tests/long_input", [("filename", field)])
                field.setText("other")
                tab._load_tab_widget_settings("tests/long_input", [("filename", field)])
                self.assertEqual(field.text(), draft)
                self.assertEqual(field._preview.toPlainText(), draft)
                tab.collect_params()
                params = getattr(tab, "p", getattr(tab, "params", None))
                self.assertEqual(params.base_name, draft)
                field.setText(original)
                dialog.deleteLater()
        settings.remove("tests/long_input")

    def test_sample_paths_keep_models_and_reload_exact_values(self):
        dock = self.window.conn_dock
        values = ("operator_name_" * 15, "device_identifier_" * 15,
                  "D:\\Data root\\" + "directory_" * 20)
        original = tuple(field.text() for field in (dock.ed_user, dock.ed_device_id, dock.ed_base))
        for field, value in zip((dock.ed_user, dock.ed_device_id, dock.ed_base), values):
            self.assertIsInstance(field, ExpandableLineEdit)
            field.setText(value)
        model = dock.to_models()[1]
        self.assertEqual((model.user, model.device_id, model.base), values)
        dock.save_settings()
        for field in (dock.ed_user, dock.ed_device_id, dock.ed_base):
            field.setText("")
        dock.load_settings()
        self.assertEqual(tuple(field.text() for field in (dock.ed_user, dock.ed_device_id, dock.ed_base)), values)
        self.assertIsNotNone(dock.ed_base.browse_action)
        for field, value in zip((dock.ed_user, dock.ed_device_id, dock.ed_base), original):
            field.setText(value)
        dock.save_settings()

    def test_multiline_bfield_and_expanded_condition_name_keep_existing_logic(self):
        tab = self.window.tab_bfield_gate_scan
        from app.engine.gate_scan_field_batch import GateScanFieldBatch
        original = tab.bfield_fields.text()
        dialog = ExpandedValueDialog(tab.bfield_fields)
        dialog.editor.setPlainText("-2\n-0.5\n0.5, 1:-1:-1")
        dialog.apply_value()
        self.assertEqual(GateScanFieldBatch.parse_fields(tab.bfield_fields.text()), (-2, -.5, .5, 1, 0, -1))
        self.assertIn("Preview (6 fields)", tab.bfield_preview.text())
        tab._save_tab_widget_settings("tests/long_fields", [("fields", tab.bfield_fields)])
        tab.bfield_fields.setText("0")
        tab._load_tab_widget_settings("tests/long_fields", [("fields", tab.bfield_fields)])
        self.assertEqual(tab.bfield_fields.text(), "-2\n-0.5\n0.5, 1:-1:-1")
        tab._update_bfield_preview()
        self.assertIn("Preview (6 fields)", tab.bfield_preview.text())
        dialog.deleteLater()
        dialog = ExpandedValueDialog(tab.condition_name)
        name = "condition_" * 20
        dialog.editor.setPlainText(name)
        dialog.apply_value()
        tab.condition_update.click()
        self.assertEqual(tab._conditions[tab._selected_condition].name, name)
        tab.bfield_fields.setText(original)
        dialog.deleteLater()
        from app.settings import get_app_settings
        get_app_settings().remove("tests/long_fields")

    def test_transport_condition_series_keep_order_and_scalar_broadcast(self):
        tab = self.window.tab_bfield_transport
        original_mode = tab.cbo_condition_add_mode.currentIndex()
        tab.cbo_condition_add_mode.setCurrentIndex(tab.cbo_condition_add_mode.findData("gates"))
        fields = (tab.ed_condition_add_first, tab.ed_condition_add_second, tab.ed_condition_add_vds)
        originals = tuple(field.text() for field in fields)
        for field, draft in zip(fields, ("-1\n0\n1", "0.25", "0.01\n0.02\n0.03")):
            self.assertIsInstance(field, ExpandableLineEdit)
            dialog = ExpandedValueDialog(field)
            dialog.editor.setPlainText(draft)
            dialog.apply_value()
            self.assertEqual(field.text(), draft)
            dialog.deleteLater()
        conditions = tab._previewed_add_conditions()
        self.assertEqual([round(condition.vtg, 6) for condition in conditions], [-1, 0, 1])
        self.assertEqual([round(condition.vbg, 6) for condition in conditions], [.25, .25, .25])
        self.assertEqual([condition.vds for condition in conditions], [.01, .02, .03])
        for field, original in zip(fields, originals):
            field.setText(original)
        tab.cbo_condition_add_mode.setCurrentIndex(original_mode)

    def test_expanded_contact_edit_keeps_existing_signal_chain_handler(self):
        from app.settings import get_app_settings
        field = self.window.conn_dock.ed_ac_contact
        original = field.text()
        self.assertIsInstance(field, ExpandableLineEdit)
        dialog = ExpandedValueDialog(field)
        draft = "Sample_electrode_" * 2
        dialog.editor.setPlainText(draft)
        dialog.apply_value()
        self.assertEqual(field.text(), draft)
        self.assertEqual(get_app_settings().value("signal_chain/ac_contact"), draft)
        dialog.deleteLater()
        field.setText(original)
        field.textEdited.emit(original)
