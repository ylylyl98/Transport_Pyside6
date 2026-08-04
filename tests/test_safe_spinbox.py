from __future__ import annotations

import os
import re
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6 import QtCore, QtGui, QtTest, QtWidgets

from app.ui.widgets.safe_combo import SafeComboBox
from app.ui.widgets.safe_spinbox import (
    SafeDoubleSpinBox,
    SafeSpinBox,
    ScientificDoubleSpinBox,
    TrimmedDoubleSpinBox,
)


class SafeSpinBoxTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    @staticmethod
    def _wheel_event(delta: int = 120) -> QtGui.QWheelEvent:
        return QtGui.QWheelEvent(
            QtCore.QPointF(1, 1),
            QtCore.QPointF(1, 1),
            QtCore.QPoint(0, 0),
            QtCore.QPoint(0, delta),
            QtCore.Qt.MouseButton.NoButton,
            QtCore.Qt.KeyboardModifier.NoModifier,
            QtCore.Qt.ScrollPhase.ScrollUpdate,
            False,
        )

    def test_wheel_and_step_keys_do_not_change_values(self):
        for spinbox in (SafeSpinBox(), SafeDoubleSpinBox()):
            spinbox.setRange(0, 10)
            spinbox.setValue(5)

            event = self._wheel_event()
            QtWidgets.QApplication.sendEvent(spinbox, event)
            self.assertEqual(spinbox.value(), 5)
            self.assertFalse(event.isAccepted())

            spinbox.lineEdit().setFocus()
            for key in (
                QtCore.Qt.Key.Key_Up,
                QtCore.Qt.Key.Key_Down,
                QtCore.Qt.Key.Key_PageUp,
                QtCore.Qt.Key.Key_PageDown,
            ):
                QtTest.QTest.keyClick(spinbox.lineEdit(), key)
                self.assertEqual(spinbox.value(), 5)

    def test_wheel_does_not_change_combo_selection(self):
        combo = SafeComboBox()
        combo.addItems(["first", "second", "third"])
        combo.setCurrentIndex(1)

        event = self._wheel_event()
        QtWidgets.QApplication.sendEvent(combo, event)

        self.assertEqual(combo.currentIndex(), 1)
        self.assertFalse(event.isAccepted())

    def test_read_only_combo_blocks_user_keys_but_allows_programmatic_updates(self):
        combo = SafeComboBox()
        combo.addItems(["first", "second", "third"])
        combo.setCurrentIndex(1)
        combo.setReadOnly(True)

        QtTest.QTest.keyClick(combo, QtCore.Qt.Key.Key_Down)
        self.assertEqual(combo.currentIndex(), 1)
        self.assertTrue(combo.isReadOnly())

        combo.setCurrentIndex(2)
        self.assertEqual(combo.currentIndex(), 2)

    def test_step_buttons_are_hidden(self):
        for spinbox in (SafeSpinBox(), SafeDoubleSpinBox(), ScientificDoubleSpinBox()):
            self.assertEqual(
                spinbox.buttonSymbols(),
                QtWidgets.QAbstractSpinBox.ButtonSymbols.NoButtons,
            )

    def test_typed_and_programmatic_values_still_work(self):
        spinbox = SafeDoubleSpinBox()
        spinbox.setDecimals(3)
        spinbox.setRange(0, 10)
        spinbox.lineEdit().selectAll()
        QtTest.QTest.keyClicks(spinbox.lineEdit(), "2.75")
        QtTest.QTest.keyClick(spinbox.lineEdit(), QtCore.Qt.Key.Key_Return)
        self.assertAlmostEqual(spinbox.value(), 2.75)

        spinbox.setValue(4.5)
        self.assertAlmostEqual(spinbox.value(), 4.5)

    def test_scientific_notation_remains_supported(self):
        spinbox = ScientificDoubleSpinBox()
        spinbox.setDecimals(12)
        spinbox.setRange(1e-12, 1.0)
        spinbox.lineEdit().selectAll()
        QtTest.QTest.keyClicks(spinbox.lineEdit(), "500e-9")
        QtTest.QTest.keyClick(spinbox.lineEdit(), QtCore.Qt.Key.Key_Return)
        self.assertAlmostEqual(spinbox.value(), 500e-9)

    def test_trimmed_voltage_entry_uses_decimal_not_scientific_notation(self):
        spinbox = TrimmedDoubleSpinBox()
        spinbox.setDecimals(3)
        spinbox.setRange(0.001, 200.0)
        spinbox.setSuffix(" V")

        for value, expected in ((2.0, "2 V"), (21.0, "21 V"), (200.0, "200 V"), (0.125, "0.125 V")):
            spinbox.setValue(value)
            self.assertEqual(spinbox.text(), expected)

    def test_application_has_no_raw_spinbox_constructors(self):
        app_root = Path(__file__).resolve().parents[1] / "app"
        raw_constructor = re.compile(r"(?:QtWidgets\.)?Q(?:Double)?SpinBox\s*\(")
        offenders = []
        for path in app_root.rglob("*.py"):
            for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                if raw_constructor.search(line):
                    offenders.append(f"{path.relative_to(app_root)}:{line_number}")
        self.assertEqual([], offenders)

    def test_application_has_no_raw_combobox_constructors(self):
        app_root = Path(__file__).resolve().parents[1] / "app"
        raw_constructor = re.compile(r"QtWidgets\.QComboBox\s*\(")
        offenders = []
        for path in app_root.rglob("*.py"):
            for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                if raw_constructor.search(line):
                    offenders.append(f"{path.relative_to(app_root)}:{line_number}")
        self.assertEqual([], offenders)


if __name__ == "__main__":
    unittest.main()
