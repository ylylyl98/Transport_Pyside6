from __future__ import annotations

import re

from PyQt6 import QtGui, QtWidgets
from PyQt6.QtCore import Qt


class _TypedEntryOnlyMixin:
    """Remove all interactive stepping while retaining typed and API values."""

    _step_keys = {
        Qt.Key.Key_Up,
        Qt.Key.Key_Down,
        Qt.Key.Key_PageUp,
        Qt.Key.Key_PageDown,
    }

    def _configure_typed_entry_only(self) -> None:
        self.setButtonSymbols(QtWidgets.QAbstractSpinBox.ButtonSymbols.NoButtons)

    def wheelEvent(self, event: QtGui.QWheelEvent) -> None:
        # Ignoring the event prevents a value change and lets a parent scroll area
        # handle the user's scrolling gesture instead.
        event.ignore()

    def keyPressEvent(self, event: QtGui.QKeyEvent) -> None:
        if event.key() in self._step_keys:
            event.ignore()
            return
        super().keyPressEvent(event)


class SafeSpinBox(_TypedEntryOnlyMixin, QtWidgets.QSpinBox):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._configure_typed_entry_only()


class SafeDoubleSpinBox(_TypedEntryOnlyMixin, QtWidgets.QDoubleSpinBox):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._configure_typed_entry_only()


class TrimmedDoubleSpinBox(SafeDoubleSpinBox):
    """Fixed-point numeric entry without scientific notation or trailing zeros."""

    def textFromValue(self, value: float) -> str:
        text = f"{float(value):.{self.decimals()}f}"
        return text.rstrip("0").rstrip(".") if "." in text else text


class ScientificDoubleSpinBox(SafeDoubleSpinBox):
    _complete_number = re.compile(
        r"^[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?$"
    )
    _partial_number = re.compile(
        r"^[+-]?(?:(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?)?|\.?\d*)$"
    )

    def validate(self, text: str, pos: int):
        stripped = text.strip()
        if not self._complete_number.fullmatch(stripped):
            state = (
                QtGui.QValidator.State.Intermediate
                if self._partial_number.fullmatch(stripped)
                else QtGui.QValidator.State.Invalid
            )
            return (state, text, pos)
        try:
            value = float(stripped)
        except ValueError:
            return (QtGui.QValidator.State.Invalid, text, pos)
        if value < self.minimum() or value > self.maximum():
            # An exponent typed later can bring the value back into range.
            return (QtGui.QValidator.State.Intermediate, text, pos)
        return (QtGui.QValidator.State.Acceptable, text, pos)

    def valueFromText(self, text: str) -> float:
        try:
            return float(text)
        except ValueError:
            return self.minimum()

    def textFromValue(self, value: float) -> str:
        return f"{value:.0e}"

