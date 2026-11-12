"""Reusable optional second-bias controls; editing never writes hardware."""
from PySide6 import QtCore, QtWidgets

from app.ui.widgets.safe_combo import SafeComboBox
from app.ui.widgets.safe_spinbox import SafeDoubleSpinBox


class Bias2Editor(QtWidgets.QGroupBox):
    changed = QtCore.Signal()

    def __init__(self, parent=None):
        super().__init__("Bias 2 · G4 / Keithley", parent)
        form = QtWidgets.QFormLayout(self)
        form.setRowWrapPolicy(QtWidgets.QFormLayout.RowWrapPolicy.WrapLongRows)
        self.mode = SafeComboBox()
        self.mode.addItems(["Disabled", "Fixed", "Follow"])
        self.voltage = SafeDoubleSpinBox()
        self.offset = SafeDoubleSpinBox()
        self.ramp = SafeDoubleSpinBox()
        for control in (self.voltage, self.offset):
            control.setDecimals(6)
            control.setRange(-40, 40)
        self.voltage.setRange(-20, 20)
        self.ramp.setDecimals(6)
        self.ramp.setRange(.000001, 1.)
        self.ramp.setValue(.05)
        self.controls = dict(bias2_mode=self.mode, bias2_voltage=self.voltage,
                             bias2_offset=self.offset, bias2_ramp=self.ramp)
        for key, title in (("bias2_mode", "Mode:"), ("bias2_voltage", "Fixed voltage (V):"),
                           ("bias2_offset", "Bias 2 − Bias 1 (V):"), ("bias2_ramp", "Ramp step (V):")):
            self.controls[key].setObjectName(key)
            self.controls[key].setAccessibleName(title)
            form.addRow(title, self.controls[key])
        self.hint = QtWidgets.QLabel()
        self.hint.setWordWrap(True)
        form.addRow(self.hint)
        self.mode.currentTextChanged.connect(self._edited)
        for control in (self.voltage, self.offset, self.ramp):
            control.valueChanged.connect(self._edited)
        self._edited()

    def values(self):
        return dict(bias2_mode=self.mode.currentText(), bias2_voltage=self.voltage.value(),
                    bias2_offset=self.offset.value(), bias2_ramp=self.ramp.value())

    def set_values(self, params):
        blockers = [QtCore.QSignalBlocker(c) for c in self.controls.values()]
        self.mode.setCurrentText(getattr(params, "bias2_mode", "Disabled"))
        for key in ("bias2_voltage", "bias2_offset", "bias2_ramp"):
            self.controls[key].setValue(getattr(params, key, .05 if key == "bias2_ramp" else 0.))
        del blockers
        self._edited()

    def _edited(self, *_):
        from app.quick.parameters import bias2_summary
        mode = self.mode.currentText()
        self.voltage.setEnabled(mode == "Fixed")
        self.offset.setEnabled(mode == "Follow")
        self.ramp.setEnabled(mode != "Disabled")
        self.hint.setText(bias2_summary(self.values()))
        self.changed.emit()
