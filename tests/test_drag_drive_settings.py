"""Resistance entry, unit conversion and run locking without real instruments."""
import os
import unittest

if os.environ.get("TRANSPORT_OFFLINE_VERIFICATION") != "1":
    raise unittest.SkipTest("Use verify_offline.py to isolate settings and hardware")

from PySide6 import QtCore, QtTest, QtWidgets

from app.settings import get_app_settings
from app.ui.drag_drive_settings import DragDriveSettings


class FakeManager(QtCore.QObject):
    resources_changed = QtCore.Signal()
    operation_changed = QtCore.Signal()

    def __init__(self):
        super().__init__()
        self.owned, self.busy = set(), False

    def current_in_use(self):
        return self.owned

    def is_busy(self):
        return self.busy


class DragDriveSettingsTests(unittest.TestCase):
    def setUp(self):
        self.settings = get_app_settings()
        self.saved = {key: self.settings.value(key) for key in self.settings.allKeys()
                      if key.startswith("drag_drive/")}
        self.settings.remove("drag_drive")
        self.settings.sync()
        self.addCleanup(self.restore_settings)

    def restore_settings(self):
        self.settings.remove("drag_drive")
        for key, value in self.saved.items():
            self.settings.setValue(key, value)
        self.settings.sync()

    def widget(self):
        widget = DragDriveSettings()
        self.addCleanup(widget.close)
        self.assertTrue(hasattr(widget, "cbo_rs_unit"), "Drive Rs needs a selectable unit")
        return widget

    def select_unit(self, widget, unit):
        index = widget.cbo_rs_unit.findText(unit)
        self.assertGreaterEqual(index, 0)
        widget.cbo_rs_unit.setCurrentIndex(index)

    def test_default_and_legacy_saved_ohms_keep_their_resistance(self):
        widget = self.widget()
        self.assertEqual(widget.cbo_rs_unit.currentText(), "kΩ")
        self.assertEqual(widget.sp_rs.value(), 100.)
        self.assertEqual(widget.configuration()["series_resistance_ohm"], 100000.)
        self.assertFalse(widget.configuration()["enabled"])
        self.settings.setValue("drag_drive/rs_ohm", 4700.)
        legacy = self.widget()
        self.assertEqual(legacy.cbo_rs_unit.currentText(), "kΩ")
        self.assertEqual(legacy.sp_rs.value(), 4.7)
        self.assertEqual(legacy.configuration()["series_resistance_ohm"], 4700.)

    def test_unit_switch_converts_value_without_changing_configuration(self):
        widget = self.widget()
        widget.cbo_mode.setCurrentIndex(1)
        original = widget.configuration()
        observed = []
        widget.changed.connect(lambda: observed.append(widget.configuration()))
        for unit, value in (("Ω", 100000.), ("MΩ", .1), ("kΩ", 100.)):
            with self.subTest(unit=unit):
                self.select_unit(widget, unit)
                self.assertEqual(widget.sp_rs.value(), value)
                self.assertEqual(widget.configuration(), original)
                self.assertEqual(self.settings.value("drag_drive/rs_ohm", type=float), 100000.)
        self.assertTrue(all(config == original for config in observed))

    def test_entered_value_uses_selected_unit_and_survives_reopen(self):
        widget = self.widget()
        widget.cbo_mode.setCurrentIndex(1)
        for unit, number, expected_ohms in (("Ω", 470., 470.), ("MΩ", 2.2, 2200000.), ("kΩ", 33., 33000.)):
            with self.subTest(unit=unit):
                self.select_unit(widget, unit)
                widget.sp_rs.setValue(number)
                self.assertEqual(widget.configuration()["series_resistance_ohm"], expected_ohms)
                self.assertEqual(self.settings.value("drag_drive/rs_ohm", type=float), expected_ohms)
                reopened = self.widget()
                self.assertEqual(reopened.cbo_rs_unit.currentText(), unit)
                self.assertEqual(reopened.sp_rs.value(), number)
                self.assertEqual(reopened.configuration()["series_resistance_ohm"], expected_ohms)

    def test_all_units_preserve_original_physical_range_and_precision(self):
        widget = self.widget()
        for ohms in (.001, 1.001, 100.123, 123456.789, 1e12):
            self.select_unit(widget, "Ω")
            widget.sp_rs.setValue(ohms)
            for _ in range(3):
                for unit, factor in (("MΩ", 1e6), ("kΩ", 1e3), ("Ω", 1.)):
                    with self.subTest(ohms=ohms, unit=unit):
                        self.select_unit(widget, unit)
                        self.assertAlmostEqual(widget.configuration()["series_resistance_ohm"], ohms, places=7)
                        self.assertAlmostEqual(widget.sp_rs.minimum() * factor, .001)
                        self.assertEqual(widget.sp_rs.maximum() * factor, 1e12)

    def test_unknown_saved_unit_falls_back_without_changing_saved_ohms(self):
        self.settings.setValue("drag_drive/rs_ohm", 680.)
        self.settings.setValue("drag_drive/rs_unit", "invalid")
        widget = self.widget()
        self.assertEqual(widget.cbo_rs_unit.currentText(), "kΩ")
        self.assertEqual(widget.configuration()["series_resistance_ohm"], 680.)

    def test_both_inputs_lock_in_ordinary_busy_and_owned_states(self):
        widget = self.widget()
        manager = FakeManager()
        widget.bind_manager(manager)
        self.assertFalse(widget.sp_rs.isEnabled())
        self.assertFalse(widget.cbo_rs_unit.isEnabled())
        widget.cbo_mode.setCurrentIndex(1)
        self.assertTrue(widget.sp_rs.isEnabled())
        self.assertTrue(widget.cbo_rs_unit.isEnabled())
        for busy in (False, True):
            manager.busy = busy
            manager.owned = set() if busy else {"daq", "lockin", "lockin_drive"}
            manager.resources_changed.emit()
            manager.operation_changed.emit()
            self.assertFalse(widget.sp_rs.isEnabled())
            self.assertFalse(widget.cbo_rs_unit.isEnabled())
        manager.busy, manager.owned = False, set()
        manager.operation_changed.emit()
        self.assertTrue(widget.sp_rs.isEnabled())
        self.assertTrue(widget.cbo_rs_unit.isEnabled())

    def test_keyboard_entry_and_unit_selection_preserve_pending_value(self):
        widget = self.widget()
        widget.cbo_mode.setCurrentIndex(1)
        widget.show()
        widget.activateWindow()
        QtWidgets.QApplication.processEvents()
        widget.sp_rs.setFocus()
        widget.sp_rs.selectAll()
        QtTest.QTest.keyClicks(widget.sp_rs, "4.7")
        QtTest.QTest.keyClick(widget.sp_rs, QtCore.Qt.Key.Key_Tab)
        self.assertTrue(widget.cbo_rs_unit.hasFocus())
        QtTest.QTest.keyClick(widget.cbo_rs_unit, QtCore.Qt.Key.Key_Down)
        self.assertEqual(widget.cbo_rs_unit.currentText(), "MΩ")
        self.assertEqual(widget.sp_rs.value(), .0047)
        self.assertEqual(widget.configuration()["series_resistance_ohm"], 4700.)
        self.assertTrue(widget.sp_rs.accessibleName())
        self.assertTrue(widget.cbo_rs_unit.accessibleName())

    def test_fractional_resistance_round_trips_in_comma_decimal_locale(self):
        widget = self.widget()
        widget.cbo_mode.setCurrentIndex(1)
        self.select_unit(widget, "MΩ")
        widget.sp_rs.setLocale(QtCore.QLocale("de_DE"))
        widget.sp_rs.setValue(2.2)
        self.assertEqual(widget.sp_rs.text(), "2,2")
        widget.sp_rs.interpretText()
        self.assertEqual(widget.configuration()["series_resistance_ohm"], 2200000.)
        # Commit a typed decimal and change units: neither formatting nor focus
        # loss may reinterpret the decimal separator as a thousands separator.
        widget.sp_rs.lineEdit().setText("0,47")
        widget.sp_rs.interpretText()
        self.select_unit(widget, "kΩ")
        self.assertEqual(widget.sp_rs.value(), 470.)
        self.assertEqual(widget.configuration()["series_resistance_ohm"], 470000.)


if __name__ == "__main__":
    unittest.main()
