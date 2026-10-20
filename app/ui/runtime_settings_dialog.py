"""Small settings surface with explicit application boundaries."""
from PySide6 import QtCore, QtWidgets

from app.runtime_settings import APPEARANCE, NEXT_RUN_SETTINGS, get_value, apply_runtime_settings
from app.ui.style_manager import apply_app_style


class RuntimeSettingsDialog(QtWidgets.QDialog):
    def __init__(self, parent, config, is_idle):
        super().__init__(parent)
        self.config = config
        self.is_idle = is_idle
        self.controls = {}
        self._display_baseline = {}
        self.setWindowTitle("Settings — when changes apply")
        self.resize(680, 610)
        outer_layout = QtWidgets.QVBoxLayout(self)
        scroll = QtWidgets.QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)
        content = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(content)
        layout.setContentsMargins(0, 0, 0, 0)
        scroll.setWidget(content)
        outer_layout.addWidget(scroll)
        intro = QtWidgets.QLabel(
            "Appearance applies immediately. B-field timing applies to the next measurement, "
            "once measurements and instrument operations are idle. Saved values survive restart.")
        intro.setWordWrap(True)
        layout.addWidget(intro)
        appearance = QtWidgets.QGroupBox("Apply immediately")
        appearance_form = QtWidgets.QFormLayout(appearance)
        appearance_form.setRowWrapPolicy(QtWidgets.QFormLayout.RowWrapPolicy.WrapLongRows)
        self._add_control(appearance_form, APPEARANCE)
        self.appearance_button = QtWidgets.QPushButton("Apply appearance")
        self.appearance_button.clicked.connect(lambda: self._apply((APPEARANCE,)))
        appearance_form.addRow(self.appearance_button)
        layout.addWidget(appearance)
        next_run = QtWidgets.QGroupBox("B-field Sweep — apply before the next measurement")
        next_form = QtWidgets.QFormLayout(next_run)
        next_form.setRowWrapPolicy(QtWidgets.QFormLayout.RowWrapPolicy.WrapLongRows)
        for spec in NEXT_RUN_SETTINGS:
            self._add_control(next_form, spec)
        self.run_button = QtWidgets.QPushButton("Apply next-run settings")
        self.run_button.clicked.connect(lambda: self._apply(NEXT_RUN_SETTINGS))
        next_form.addRow(self.run_button)
        self.availability = QtWidgets.QLabel()
        self.availability.setWordWrap(True)
        next_form.addRow(self.availability)
        layout.addWidget(next_run)
        boundaries = QtWidgets.QLabel(
            "Reconnect one device: change addresses or Keithley mode in Devices. "
            "Use the existing protection controls for voltage/current limits.\n"
            "Restart while idle: driver/SDK changes and other startup configuration. "
            "Reload analysis updates the independent analysis and PNG processes only.")
        boundaries.setWordWrap(True)
        layout.addWidget(boundaries)
        self.message = QtWidgets.QLabel()
        self.message.setWordWrap(True)
        self.message.setTextFormat(QtCore.Qt.TextFormat.PlainText)
        layout.addWidget(self.message)
        buttons = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        outer_layout.addWidget(buttons)
        self.timer = QtCore.QTimer(self)
        self.timer.setInterval(250)
        self.timer.timeout.connect(self._refresh_availability)
        self.timer.start()
        self._refresh_availability()

    def _add_control(self, form, spec):
        if spec.kind is bool:
            control = QtWidgets.QCheckBox()
            control.setChecked(get_value(self.config, spec.path))
        else:
            control = QtWidgets.QSpinBox() if spec.kind is int else QtWidgets.QDoubleSpinBox()
            if spec.kind is float:
                control.setDecimals(9 if spec.suffix == " T" else 2)
            control.setRange(spec.minimum, spec.maximum)
            control.setSuffix(spec.suffix)
            control.setValue(get_value(self.config, spec.path))
        control.setObjectName("runtime_" + spec.path.replace(".", "_"))
        control.setAccessibleName(spec.label)
        self.controls[spec.path] = control
        # Spin boxes can round or clamp an existing config value. Compare
        # edits to that initial display, leaving untouched raw values intact.
        self._display_baseline[spec.path] = self._control_value(spec)
        form.addRow(spec.label, control)

    def _control_value(self, spec):
        control = self.controls[spec.path]
        return control.isChecked() if spec.kind is bool else control.value()

    def _refresh_availability(self):
        idle = bool(self.is_idle())
        self.run_button.setEnabled(idle)
        self.availability.setText("Ready to apply for the next measurement." if idle else
                                  "Measurement or instrument operation active. Edits stay here until it is idle.")

    def showEvent(self, event):
        super().showEvent(event)
        self.timer.start()
        self._refresh_availability()

    def hideEvent(self, event):
        self.timer.stop()
        super().hideEvent(event)

    @staticmethod
    def _apply_font(size):
        result = apply_app_style(font_size_pt=size)
        if not result.ok:
            raise RuntimeError(result.error)

    def _apply(self, specs):
        displayed = {spec.path: self._control_value(spec) for spec in specs}
        values = {path: value for path, value in displayed.items()
                  if value != self._display_baseline[path]}
        application = QtWidgets.QApplication.instance()
        previous_style = application.styleSheet()
        try:
            changed = apply_runtime_settings(self.config, values, is_idle=self.is_idle,
                                             apply_font=self._apply_font,
                                             rollback_appearance=lambda: application.setStyleSheet(previous_style))
        except Exception as exc:
            self.message.setText("Settings were not applied: " + str(exc))
            return
        self._display_baseline.update(displayed)
        self.message.setText("Saved and applied. " + ("Appearance updated." if specs == (APPEARANCE,)
                                                      else "The next B-field measurement will use these values.")
                             if changed else "No settings changed.")
