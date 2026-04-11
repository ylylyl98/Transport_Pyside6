from __future__ import annotations

from PyQt6 import QtCore, QtWidgets
from PyQt6.QtCore import Qt

from app.constants import V_LIMIT


def configure_volt_spinbox(sp: QtWidgets.QDoubleSpinBox, val: float):
    sp.setDecimals(3)
    sp.setRange(-V_LIMIT, V_LIMIT)
    sp.setSingleStep(0.1)
    sp.setValue(val)


def style_form_layout(form: QtWidgets.QFormLayout):
    form.setContentsMargins(10, 18, 10, 10)
    form.setVerticalSpacing(8)
    form.setHorizontalSpacing(10)
    form.setLabelAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)


def apply_tooltip(text: str, *widgets: QtWidgets.QWidget):
    for widget in widgets:
        widget.setToolTip(text)


def set_standard_input_height(widget: QtWidgets.QWidget, height: int = 28):
    if hasattr(widget, "setFixedHeight"):
        widget.setFixedHeight(height)


def clear_button_flash(btn: QtWidgets.QPushButton):
    btn.setProperty("flash", None)
    btn.style().unpolish(btn)
    btn.style().polish(btn)


def flash_button_success(btn: QtWidgets.QPushButton):
    btn.setProperty("flash", "ok")
    btn.style().unpolish(btn)
    btn.style().polish(btn)
    QtCore.QTimer.singleShot(500, lambda: clear_button_flash(btn))
