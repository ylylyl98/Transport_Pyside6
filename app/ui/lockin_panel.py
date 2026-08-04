from __future__ import annotations

import math
from typing import Any

from PyQt6 import QtCore, QtWidgets
from PyQt6.QtCore import Qt

from app.device_manager import DeviceManager
from app.settings import get_app_settings
from app.ui.helpers import apply_tooltip, set_standard_input_height, style_form_layout
from app.ui.widgets.collapsible_section import CollapsibleSection
from app.ui.widgets.safe_combo import SafeComboBox
from app.ui.widgets.safe_spinbox import SafeDoubleSpinBox, SafeSpinBox
from instruments.SR830 import (
    FILTER_SLOPE_LABELS,
    INPUT_CONFIG_LABELS,
    INPUT_COUPLING_LABELS,
    INPUT_GROUND_LABELS,
    LINE_FILTER_LABELS,
    LOCKIN_PROFILES,
    REFERENCE_SOURCE_LABELS,
    RESERVE_LABELS,
    SENSITIVITY_LABELS,
    TIME_CONSTANT_LABELS,
    detect_lockin_model,
    sensitivity_value,
)


class LockinWorker(QtCore.QThread):
    data_ready = QtCore.pyqtSignal(dict, str)
    failed = QtCore.pyqtSignal(str)

    def __init__(self, session: object, action: str = "refresh", payload: dict | None = None, parent=None):
        super().__init__(parent)
        self.session = session
        self.action = action
        self.payload = dict(payload or {})

    def run(self):
        try:
            message = "Lock-in panel refreshed."
            if self.action == "apply":
                self.session.apply_settings(self.payload)
                message = f"{self.session.model} settings applied."
            elif self.action != "refresh":
                action_fn = getattr(self.session, self.action)
                action_fn()
                message = self._action_message(self.action)
            data = self.session.read_front_panel()
            self.data_ready.emit(data, message)
        except Exception as ex:
            self.failed.emit(str(ex))

    @staticmethod
    def _action_message(action: str) -> str:
        return {
            "auto_phase": "Auto Phase command sent.",
            "auto_gain": "Auto Gain command sent.",
            "auto_reserve": "Auto Reserve command sent.",
            "auto_offset_x": "Auto Offset X command sent.",
            "auto_offset_y": "Auto Offset Y command sent.",
            "auto_offset_r": "Auto Offset R command sent.",
        }.get(action, "Lock-in command sent.")


class LockinPanel(QtWidgets.QWidget):
    sensitivity_read = QtCore.pyqtSignal(float, str)
    settings_changed = QtCore.pyqtSignal()
    stop_sweep_requested = QtCore.pyqtSignal()

    SETTINGS_PREFIX = "lockin"

    def __init__(self, device_manager: DeviceManager):
        super().__init__()
        self.device_manager = device_manager
        self._worker: LockinWorker | None = None
        self._claimed_device = False
        self._locked_by_sweep = False
        self._stop_requested_for_settings = False
        self._suppress_updates = False
        self._capabilities = dict(LOCKIN_PROFILES["SR830"])
        self._build()
        self.load_panel_settings()
        self._bind_panel_settings()
        self.device_manager.status_changed.connect(self._on_device_status_changed)
        self.device_manager.operation_changed.connect(lambda _busy, _message: self._update_enabled())
        self.device_manager.resources_changed.connect(lambda _resources: self._update_enabled())
        self._on_device_status_changed("lockin", self.device_manager.state("lockin"), self.device_manager.detail("lockin"))
        self._update_enabled()

    def _build(self):
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(8)

        self.header_group = QtWidgets.QGroupBox("SRS Lock-in")
        header_layout = QtWidgets.QVBoxLayout(self.header_group)
        header_layout.setContentsMargins(10, 18, 10, 10)
        header_layout.setSpacing(6)
        top_row = QtWidgets.QHBoxLayout()
        top_row.setContentsMargins(0, 0, 0, 0)
        self.lbl_status = QtWidgets.QLabel("Disconnected")
        self.lbl_status.setProperty("role", "hint")
        self.btn_refresh = QtWidgets.QPushButton("Refresh Panel")
        self.btn_refresh.clicked.connect(self.refresh_panel)
        top_row.addWidget(self.lbl_status, 1)
        top_row.addWidget(self.btn_refresh)
        self.lbl_identity = QtWidgets.QLabel("")
        self.lbl_identity.setProperty("role", "hint")
        self.lbl_identity.setWordWrap(True)
        header_layout.addLayout(top_row)
        header_layout.addWidget(self.lbl_identity)
        layout.addWidget(self.header_group)

        readouts = QtWidgets.QGroupBox("Readouts")
        readout_grid = QtWidgets.QGridLayout(readouts)
        readout_grid.setContentsMargins(10, 18, 10, 10)
        readout_grid.setHorizontalSpacing(8)
        readout_grid.setVerticalSpacing(8)
        self.readout_labels: dict[str, QtWidgets.QLabel] = {}
        for index, (key, label_text) in enumerate((("x", "X"), ("y", "Y"), ("r", "R"), ("theta", "Theta"))):
            label = QtWidgets.QLabel(label_text)
            value = QtWidgets.QLabel("--")
            value.setProperty("role", "digital-readout")
            value.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            value.setMinimumWidth(150)
            readout_grid.addWidget(label, index // 2 * 2, index % 2)
            readout_grid.addWidget(value, index // 2 * 2 + 1, index % 2)
            self.readout_labels[key] = value
        self.lbl_range = QtWidgets.QLabel("R range: --")
        self.lbl_range.setProperty("role", "hint")
        self.lbl_range.setWordWrap(True)
        self.lbl_raw_snap = QtWidgets.QLabel("SNAP raw: --")
        self.lbl_raw_snap.setProperty("role", "hint")
        self.lbl_raw_snap.setWordWrap(True)
        readout_grid.addWidget(self.lbl_range, 4, 0, 1, 2)
        readout_grid.addWidget(self.lbl_raw_snap, 5, 0, 1, 2)
        layout.addWidget(readouts)

        indicators = QtWidgets.QGroupBox("Status Indicators")
        indicator_grid = QtWidgets.QGridLayout(indicators)
        indicator_grid.setContentsMargins(10, 18, 10, 10)
        indicator_grid.setHorizontalSpacing(6)
        indicator_grid.setVerticalSpacing(6)
        self.lamp_labels: dict[str, QtWidgets.QLabel] = {}
        lamp_items = (
            ("input_overload", "Input OVLD"),
            ("filter_overload", "Filter OVLD"),
            ("output_overload", "Output OVLD"),
            ("unlock", "Unlock"),
            ("range_change", "Range"),
            ("time_constant_change", "TC Change"),
        )
        for index, (key, text) in enumerate(lamp_items):
            lamp = QtWidgets.QLabel(text)
            lamp.setAlignment(Qt.AlignmentFlag.AlignCenter)
            lamp.setProperty("role", "lamp")
            lamp.setProperty("state", "off")
            indicator_grid.addWidget(lamp, index // 2, index % 2)
            self.lamp_labels[key] = lamp
        layout.addWidget(indicators)

        settings = QtWidgets.QGroupBox("Front Panel Settings")
        settings_form = QtWidgets.QFormLayout(settings)
        style_form_layout(settings_form)
        self.cbo_sensitivity = self._make_combo(SENSITIVITY_LABELS)
        self.cbo_time_constant = self._make_combo(TIME_CONSTANT_LABELS)
        self.cbo_reserve = self._make_combo(RESERVE_LABELS)
        self.cbo_filter_slope = self._make_combo(FILTER_SLOPE_LABELS)
        self.cbo_ref_source = self._make_combo(REFERENCE_SOURCE_LABELS)
        self.cbo_input_config = self._make_combo(INPUT_CONFIG_LABELS)
        self.cbo_current_gain = self._make_combo([])
        self.cbo_input_coupling = self._make_combo(INPUT_COUPLING_LABELS)
        self.cbo_input_ground = self._make_combo(INPUT_GROUND_LABELS)
        self.cbo_line_filter = self._make_combo(LINE_FILTER_LABELS)
        self.sp_phase = SafeDoubleSpinBox()
        self.sp_phase.setDecimals(2)
        self.sp_phase.setRange(-360.0, 729.99)
        self.sp_phase.setSingleStep(1.0)
        self.sp_frequency = SafeDoubleSpinBox()
        self.sp_frequency.setDecimals(4)
        self.sp_frequency.setRange(0.001, 102000.0)
        self.sp_frequency.setSingleStep(1.0)
        self.sp_sine_out = SafeDoubleSpinBox()
        self.sp_sine_out.setDecimals(3)
        self.sp_sine_out.setRange(0.004, 5.0)
        self.sp_sine_out.setSingleStep(0.01)
        self.sp_harmonic = SafeSpinBox()
        self.sp_harmonic.setRange(1, 19999)
        self.sp_harmonic.setValue(1)
        self.cbo_ref_source.currentIndexChanged.connect(self._update_frequency_enabled)
        self.cbo_ref_source.currentIndexChanged.connect(self._update_reference_help)
        self.cbo_input_config.currentIndexChanged.connect(self._update_current_gain_enabled)
        settings_form.addRow("Sensitivity:", self.cbo_sensitivity)
        settings_form.addRow("Time Constant:", self.cbo_time_constant)
        settings_form.addRow("Reserve:", self.cbo_reserve)
        settings_form.addRow("Filter Slope:", self.cbo_filter_slope)
        settings_form.addRow("Reference:", self.cbo_ref_source)
        self.lbl_reference_help = QtWidgets.QLabel()
        self.lbl_reference_help.setWordWrap(True)
        self.lbl_reference_help.setProperty("role", "hint")
        settings_form.addRow(self.lbl_reference_help)
        settings_form.addRow("Phase (deg):", self.sp_phase)
        settings_form.addRow("Internal Frequency (Hz):", self.sp_frequency)
        settings_form.addRow("Sine Out (V):", self.sp_sine_out)
        settings_form.addRow("Harmonic:", self.sp_harmonic)
        settings_form.addRow("Input:", self.cbo_input_config)
        settings_form.addRow("Current Gain:", self.cbo_current_gain)
        self.lbl_current_gain = settings_form.labelForField(self.cbo_current_gain)
        settings_form.addRow("Coupling:", self.cbo_input_coupling)
        settings_form.addRow("Shield:", self.cbo_input_ground)
        settings_form.addRow("Line Filter:", self.cbo_line_filter)
        self.btn_apply = QtWidgets.QPushButton("Apply Settings")
        self.btn_apply.clicked.connect(self.apply_settings)
        settings_form.addRow("", self.btn_apply)
        self.exp_settings = CollapsibleSection("Front Panel Settings", settings, expanded=False)
        layout.addWidget(self.exp_settings)

        actions = QtWidgets.QGroupBox("Auto Functions")
        action_grid = QtWidgets.QGridLayout(actions)
        action_grid.setContentsMargins(10, 18, 10, 10)
        action_grid.setHorizontalSpacing(6)
        action_grid.setVerticalSpacing(6)
        self.action_buttons: dict[str, QtWidgets.QPushButton] = {}
        for index, (action, text) in enumerate(
            (
                ("auto_phase", "Auto Phase"),
                ("auto_gain", "Auto Gain"),
                ("auto_reserve", "Auto Reserve"),
                ("auto_offset_x", "Auto Offset X"),
                ("auto_offset_y", "Auto Offset Y"),
                ("auto_offset_r", "Auto Offset R"),
            )
        ):
            btn = QtWidgets.QPushButton(text)
            btn.clicked.connect(lambda _checked=False, a=action: self.run_action(a))
            action_grid.addWidget(btn, index // 2, index % 2)
            self.action_buttons[action] = btn
        self.exp_actions = CollapsibleSection("Auto Functions", actions, expanded=False)
        layout.addWidget(self.exp_actions)

        self.lbl_message = QtWidgets.QLabel("Connect an SR830 or SR850 from Instrument Setup, then refresh this panel.")
        self.lbl_message.setWordWrap(True)
        self.lbl_message.setProperty("role", "hint")
        layout.addWidget(self.lbl_message)
        self.btn_stop_for_settings = QtWidgets.QPushButton("Stop Sweep Safely to Change Settings")
        self.btn_stop_for_settings.clicked.connect(self._request_safe_sweep_stop)
        self.btn_stop_for_settings.hide()
        layout.addWidget(self.btn_stop_for_settings)
        layout.addStretch()

        for widget in self._setting_widgets():
            set_standard_input_height(widget, 26)
        apply_tooltip("Read lock-in X, Y, R, Theta, status indicators, and settings once.", self.btn_refresh)
        apply_tooltip("Apply settings supported by the detected SR830 or SR850 over GPIB.", self.btn_apply)
        for button in self.action_buttons.values():
            apply_tooltip("Send this lock-in auto function command over GPIB.", button)
        self._apply_capabilities(self._capabilities)
        self.header_group.setTitle("SRS Lock-in")
        self._update_frequency_enabled()
        self._update_reference_help()

    def refresh_panel(self):
        self._start_worker("refresh", message="Refreshing lock-in panel...")

    def apply_settings(self):
        self._start_worker("apply", self._collect_settings(), "Applying lock-in settings...")

    def run_action(self, action: str):
        self._start_worker(action, message="Sending lock-in command...")

    def _start_worker(self, action: str, payload: dict | None = None, message: str = ""):
        if self._worker is not None and self._worker.isRunning():
            return
        if not self.device_manager.is_connected("lockin"):
            self._set_message("Connect an SR830 or SR850 before using the lock-in panel.", warning=True)
            return
        if self._is_locked_by_sweep():
            self._show_sweep_lock_message()
            return
        ok, blocked = self.device_manager.mark_in_use(["lockin"])
        if not ok:
            self._set_message("Lock-in panel is waiting for: " + ", ".join(blocked), warning=True)
            return
        session = self.device_manager.get_session("lockin")
        self._claimed_device = True
        self._worker = LockinWorker(session, action, payload, self)
        self._worker.data_ready.connect(self._on_worker_data)
        self._worker.failed.connect(self._on_worker_failed)
        self._worker.finished.connect(self._on_worker_finished)
        self._set_message(message or "Working...", warning=False)
        self._update_enabled()
        self._worker.start()

    def _on_worker_data(self, data: dict, message: str):
        self._apply_data(data)
        self._set_message(message, warning=False)

    def _on_worker_failed(self, message: str):
        self._set_message(f"Lock-in operation failed: {message}", warning=True)

    def _on_worker_finished(self):
        if self._claimed_device:
            self.device_manager.release(["lockin"])
            self._claimed_device = False
        if self._worker is not None:
            self._worker.deleteLater()
            self._worker = None
        self._update_enabled()

    def _apply_data(self, data: dict):
        outputs = data.get("outputs", {})
        settings = data.get("settings", {})
        capabilities = data.get("capabilities")
        if isinstance(capabilities, dict):
            self._apply_capabilities(capabilities)
        signal_unit = self._signal_unit(settings)
        sensitivity_limit = self._sensitivity_limit(settings)
        self.readout_labels["x"].setText(self._format_engineering_value(outputs.get("x"), signal_unit))
        self.readout_labels["y"].setText(self._format_engineering_value(outputs.get("y"), signal_unit))
        self.readout_labels["r"].setText(self._format_engineering_value(outputs.get("r"), signal_unit))
        self.readout_labels["theta"].setText(self._format_theta(outputs.get("theta")))
        self.lbl_range.setText(self._format_range_status(outputs.get("r"), sensitivity_limit, signal_unit))
        self.lbl_raw_snap.setText(self._format_raw_snap(data.get("raw_snap")))
        identity = str(data.get("identity") or "").strip()
        self.lbl_identity.setText(identity)

        status = data.get("status", {})
        for key, lamp in self.lamp_labels.items():
            self._set_lamp(lamp, bool(status.get(key)))

        self._suppress_updates = True
        try:
            self._set_combo(self.cbo_sensitivity, settings.get("sensitivity"))
            self._set_combo(self.cbo_time_constant, settings.get("time_constant"))
            self._set_combo(self.cbo_reserve, settings.get("reserve"))
            self._set_combo(self.cbo_filter_slope, settings.get("filter_slope"))
            self._set_combo(self.cbo_ref_source, settings.get("ref_source"))
            self._set_combo(self.cbo_input_config, settings.get("input_config"))
            self._set_combo(self.cbo_current_gain, settings.get("current_gain"))
            self._set_combo(self.cbo_input_coupling, settings.get("input_coupling"))
            self._set_combo(self.cbo_input_ground, settings.get("input_ground"))
            self._set_combo(self.cbo_line_filter, settings.get("line_filter"))
            self._set_spinbox(self.sp_phase, settings.get("phase_deg"))
            self._set_spinbox(self.sp_frequency, settings.get("frequency_hz"))
            self._set_spinbox(self.sp_sine_out, settings.get("sine_out_v"))
            self._set_spinbox(self.sp_harmonic, settings.get("harmonic"))
        finally:
            self._suppress_updates = False
        self._update_frequency_enabled()
        self._update_reference_help()
        self._update_current_gain_enabled()
        self.save_panel_settings()
        self._emit_voltage_sensitivity(settings)

    def _collect_settings(self) -> dict[str, Any]:
        ref_source = self.cbo_ref_source.currentData()
        settings: dict[str, Any] = {
            "sensitivity": self.cbo_sensitivity.currentData(),
            "time_constant": self.cbo_time_constant.currentData(),
            "reserve": self.cbo_reserve.currentData(),
            "filter_slope": self.cbo_filter_slope.currentData(),
            "ref_source": ref_source,
            "phase_deg": self.sp_phase.value(),
            "sine_out_v": self.sp_sine_out.value(),
            "harmonic": self.sp_harmonic.value(),
            "input_config": self.cbo_input_config.currentData(),
            "input_coupling": self.cbo_input_coupling.currentData(),
            "input_ground": self.cbo_input_ground.currentData(),
            "line_filter": self.cbo_line_filter.currentData(),
        }
        if self._capabilities.get("current_gain_labels") and settings["input_config"] == 2:
            settings["current_gain"] = self.cbo_current_gain.currentData()
        if ref_source == self._capabilities.get("internal_reference_code"):
            settings["frequency_hz"] = self.sp_frequency.value()
        return settings

    def set_verified_sensitivity(self, sensitivity_v: float, label: str = "") -> None:
        index = self.cbo_sensitivity.findText(str(label)) if label else -1
        if index < 0:
            try:
                target = float(sensitivity_v)
                candidates = [sensitivity_value(i, use_current=False) for i in range(len(SENSITIVITY_LABELS))]
                index = min(
                    range(len(candidates)),
                    key=lambda i: abs(float(candidates[i]) - target),
                )
            except (TypeError, ValueError):
                return
        previous = self._suppress_updates
        self._suppress_updates = True
        try:
            self.cbo_sensitivity.setCurrentIndex(index)
        finally:
            self._suppress_updates = previous

    def load_panel_settings(self):
        settings = get_app_settings()
        self._suppress_updates = True
        try:
            self._set_combo(self.cbo_sensitivity, self._saved_value(settings, "sensitivity", 18, int))
            self._set_combo(self.cbo_time_constant, self._saved_value(settings, "time_constant", 10, int))
            self._set_combo(self.cbo_reserve, self._saved_value(settings, "reserve", 1, int))
            self._set_combo(self.cbo_filter_slope, self._saved_value(settings, "filter_slope", 1, int))
            self._set_combo(
                self.cbo_ref_source,
                self._saved_value(settings, "ref_source", self._capabilities["internal_reference_code"], int),
            )
            self._set_combo(self.cbo_input_config, self._saved_value(settings, "input_config", 0, int))
            self._set_combo(self.cbo_current_gain, self._saved_value(settings, "current_gain", 0, int))
            self._set_combo(self.cbo_input_coupling, self._saved_value(settings, "input_coupling", 0, int))
            self._set_combo(self.cbo_input_ground, self._saved_value(settings, "input_ground", 0, int))
            self._set_combo(self.cbo_line_filter, self._saved_value(settings, "line_filter", 0, int))
            self._set_spinbox(self.sp_phase, self._saved_value(settings, "phase_deg", 0.0, float))
            self._set_spinbox(self.sp_frequency, self._saved_value(settings, "frequency_hz", 1000.0, float))
            self._set_spinbox(self.sp_sine_out, self._saved_value(settings, "sine_out_v", 0.004, float))
            self._set_spinbox(self.sp_harmonic, self._saved_value(settings, "harmonic", 1, int))
        finally:
            self._suppress_updates = False
        self._update_frequency_enabled()
        self._update_current_gain_enabled()

    def save_panel_settings(self, *_args):
        if self._suppress_updates or self._is_locked_by_sweep():
            return
        settings = get_app_settings()
        values = self._collect_settings()
        if self.cbo_ref_source.currentData() != self._capabilities.get("internal_reference_code"):
            values["frequency_hz"] = self.sp_frequency.value()
        for key, value in values.items():
            settings.setValue(self._settings_key(key), value)
        settings.sync()
        self.settings_changed.emit()

    def _bind_panel_settings(self):
        for combo in (
            self.cbo_sensitivity,
            self.cbo_time_constant,
            self.cbo_reserve,
            self.cbo_filter_slope,
            self.cbo_ref_source,
            self.cbo_input_config,
            self.cbo_current_gain,
            self.cbo_input_coupling,
            self.cbo_input_ground,
            self.cbo_line_filter,
        ):
            combo.currentIndexChanged.connect(self.save_panel_settings)
        for spinbox in (self.sp_phase, self.sp_frequency, self.sp_sine_out, self.sp_harmonic):
            spinbox.valueChanged.connect(self.save_panel_settings)

    def _saved_value(self, settings, key: str, default, cast):
        model_key = self._settings_key(key)
        legacy_key = f"{self.SETTINGS_PREFIX}/{key}"
        model_specific = {"ref_source", "reserve", "input_config", "current_gain"}
        fallback = default if key in model_specific else settings.value(legacy_key, default)
        value = settings.value(model_key, fallback)
        try:
            return cast(value)
        except (TypeError, ValueError):
            return default

    def _on_device_status_changed(self, name: str, state: str, detail: str):
        if name != "lockin":
            return
        if state == "ok":
            self.lbl_status.setText("Connected")
            self.lbl_status.setProperty("role", "hint")
            try:
                model = detect_lockin_model(detail)
            except ValueError:
                model = ""
            if model:
                self._apply_capabilities(LOCKIN_PROFILES[model])
                self.lbl_identity.setText(detail)
                self.load_panel_settings()
        elif state == "err":
            self.lbl_status.setText("Connection error")
            self.lbl_status.setProperty("role", "warning-hint")
            self.lbl_identity.setText(detail)
        else:
            self.lbl_status.setText("Disconnected")
            self.lbl_status.setProperty("role", "hint")
            self.lbl_identity.setText("")
            self._clear_readouts()
        self.lbl_status.style().unpolish(self.lbl_status)
        self.lbl_status.style().polish(self.lbl_status)
        self._update_enabled()

    def _update_enabled(self):
        worker_running = self._worker is not None
        connected = self.device_manager.is_connected("lockin")
        locked_by_sweep = self._is_locked_by_sweep()
        commands_available = (
            connected
            and not worker_running
            and not self.device_manager.is_busy()
            and not locked_by_sweep
        )
        self.btn_refresh.setEnabled(commands_available)
        self.btn_apply.setEnabled(commands_available)
        for button in self.action_buttons.values():
            button.setEnabled(commands_available)
        for widget in self._setting_widgets():
            widget.setEnabled(connected)
        self._update_frequency_enabled()
        self._update_current_gain_enabled()
        settings_read_only = worker_running or locked_by_sweep
        for widget in self._setting_widgets():
            self._set_setting_read_only(widget, settings_read_only)
        self.btn_stop_for_settings.setVisible(locked_by_sweep)
        self.btn_stop_for_settings.setEnabled(
            locked_by_sweep and not self._stop_requested_for_settings
        )
        was_locked = self._locked_by_sweep
        self._locked_by_sweep = locked_by_sweep
        if locked_by_sweep:
            if self._stop_requested_for_settings:
                self._show_safe_stop_message()
            else:
                self._show_sweep_lock_message()
        elif was_locked and not worker_running:
            self._stop_requested_for_settings = False
            self._set_message("Sweep finished - SR830 settings are available again.", warning=False)

    def _request_safe_sweep_stop(self) -> None:
        if not self._is_locked_by_sweep():
            return
        self._stop_requested_for_settings = True
        self.btn_stop_for_settings.setEnabled(False)
        self._show_safe_stop_message()
        self.stop_sweep_requested.emit()

    def _show_safe_stop_message(self) -> None:
        self._set_message(
            "Safe stop requested - SR830 settings will unlock after outputs finish ramping.",
            warning=True,
        )

    def _is_locked_by_sweep(self) -> bool:
        return self.device_manager.is_in_use("lockin") and not self._claimed_device

    def _show_sweep_lock_message(self) -> None:
        sensitivity = self.cbo_sensitivity.currentText() or "the verified value"
        self._set_message(
            f"Locked by active sweep - sensitivity frozen at {sensitivity}. "
            "Stop the sweep safely before changing SR830 settings.",
            warning=True,
        )

    @staticmethod
    def _set_setting_read_only(widget: QtWidgets.QWidget, read_only: bool) -> None:
        widget.setProperty("sweepLocked", bool(read_only))
        set_read_only = getattr(widget, "setReadOnly", None)
        if callable(set_read_only):
            set_read_only(bool(read_only))
        widget.style().unpolish(widget)
        widget.style().polish(widget)

    def _update_frequency_enabled(self):
        if self._suppress_updates:
            return
        self.sp_frequency.setEnabled(
            self.cbo_ref_source.currentData() == self._capabilities.get("internal_reference_code")
            and self.cbo_ref_source.isEnabled()
        )

    def _update_reference_help(self):
        ref_source = self.cbo_ref_source.currentData()
        internal_code = self._capabilities.get("internal_reference_code")
        model = self._capabilities.get("model", "lock-in")
        if ref_source == internal_code:
            message = "The fixed internal oscillator frequency is editable below."
        elif model == "SR850" and ref_source == 1:
            message = (
                "Internal Sweep uses the SR850 sweep configuration, so the fixed frequency "
                "field is disabled. Choose Internal (Fixed) to set one frequency."
            )
        else:
            message = (
                "The external reference determines frequency. Choose Internal"
                + (" (Fixed)" if model == "SR850" else "")
                + " to edit it."
            )
        self.lbl_reference_help.setText(message)

    def _update_current_gain_enabled(self):
        supported = bool(self._capabilities.get("current_gain_labels"))
        current_input = self.cbo_input_config.currentData() == 2
        self.cbo_current_gain.setEnabled(supported and current_input and self.cbo_input_config.isEnabled())

    def _apply_capabilities(self, capabilities: dict[str, Any]):
        model = str(capabilities.get("model") or "").upper()
        if model not in LOCKIN_PROFILES:
            return
        selected = LOCKIN_PROFILES[model]
        self._capabilities = {
            key: list(value) if isinstance(value, list) else value
            for key, value in selected.items()
        }
        self._suppress_updates = True
        try:
            self._replace_combo(self.cbo_ref_source, self._capabilities["reference_source_labels"])
            self._replace_combo(self.cbo_reserve, self._capabilities["reserve_labels"])
            self._replace_combo(self.cbo_input_config, self._capabilities["input_config_labels"])
            self._replace_combo(self.cbo_current_gain, self._capabilities["current_gain_labels"])
            self.sp_phase.setDecimals(self._capabilities["phase_decimals"])
            self.sp_phase.setRange(self._capabilities["phase_min"], self._capabilities["phase_max"])
            self.sp_harmonic.setRange(1, self._capabilities["harmonic_max"])
        finally:
            self._suppress_updates = False
        supports_current_gain = bool(self._capabilities["current_gain_labels"])
        self.cbo_current_gain.setVisible(supports_current_gain)
        if self.lbl_current_gain is not None:
            self.lbl_current_gain.setVisible(supports_current_gain)
        self.header_group.setTitle(f"{model} Lock-in")
        self._update_frequency_enabled()
        self._update_reference_help()
        self._update_current_gain_enabled()

    def _settings_key(self, key: str) -> str:
        model = self._capabilities.get("model", "SR830")
        return f"{self.SETTINGS_PREFIX}/{model}/{key}"

    def _clear_readouts(self):
        for label in self.readout_labels.values():
            label.setText("--")
        self.lbl_range.setText("R range: --")
        self.lbl_raw_snap.setText("SNAP raw: --")
        for lamp in self.lamp_labels.values():
            self._set_lamp(lamp, False)

    def _set_message(self, message: str, warning: bool):
        self.lbl_message.setText(message)
        self.lbl_message.setProperty("role", "warning-hint" if warning else "hint")
        self.lbl_message.style().unpolish(self.lbl_message)
        self.lbl_message.style().polish(self.lbl_message)

    @staticmethod
    def _make_combo(labels: list[str]) -> QtWidgets.QComboBox:
        combo = SafeComboBox()
        for index, label in enumerate(labels):
            combo.addItem(label, index)
        return combo

    @staticmethod
    def _replace_combo(combo: QtWidgets.QComboBox, labels: list[str]):
        previous = combo.currentData()
        combo.clear()
        for index, label in enumerate(labels):
            combo.addItem(label, index)
        try:
            restored = combo.findData(int(previous))
        except (TypeError, ValueError):
            restored = -1
        if restored >= 0:
            combo.setCurrentIndex(restored)

    @staticmethod
    def _set_combo(combo: QtWidgets.QComboBox, value):
        try:
            index = combo.findData(int(value))
        except (TypeError, ValueError):
            index = -1
        if index >= 0:
            combo.setCurrentIndex(index)

    @staticmethod
    def _set_spinbox(spinbox: QtWidgets.QAbstractSpinBox, value):
        if value is None:
            return
        if isinstance(spinbox, QtWidgets.QSpinBox):
            spinbox.setValue(int(value))
        else:
            spinbox.setValue(float(value))

    @staticmethod
    def _set_lamp(label: QtWidgets.QLabel, active: bool):
        label.setProperty("state", "on" if active else "off")
        label.style().unpolish(label)
        label.style().polish(label)

    @staticmethod
    def _format_engineering_value(value, unit: str, signed: bool = True) -> str:
        try:
            number = float(value)
        except (TypeError, ValueError):
            return "--"
        if not math.isfinite(number):
            return "--"
        scales = {
            "V": ((1.0, "V"), (1e-3, "mV"), (1e-6, "uV"), (1e-9, "nV"), (1e-12, "pV")),
            "A": ((1.0, "A"), (1e-3, "mA"), (1e-6, "uA"), (1e-9, "nA"), (1e-12, "pA"), (1e-15, "fA")),
        }.get(unit, ((1.0, unit),))
        magnitude = abs(number)
        scale, label = scales[-1]
        for candidate_scale, candidate_label in scales:
            if magnitude >= candidate_scale:
                scale, label = candidate_scale, candidate_label
                break
        scaled = number / scale
        decimals = 3 if abs(scaled) < 10 else 2 if abs(scaled) < 100 else 1
        sign = "+" if signed else ""
        return f"{scaled:{sign}.{decimals}f} {label}"

    @classmethod
    def _format_range_status(cls, value, limit, unit: str) -> str:
        try:
            number = abs(float(value))
            full_scale = float(limit)
        except (TypeError, ValueError):
            return "R range: --"
        if not math.isfinite(number) or not math.isfinite(full_scale) or full_scale <= 0.0:
            return "R range: --"
        percent = 100.0 * number / full_scale
        measured = cls._format_engineering_value(number, unit, signed=False)
        range_text = cls._format_engineering_value(full_scale, unit, signed=False)
        return f"R range: {measured} / {range_text} ({percent:.1f}%)"

    @staticmethod
    def _format_theta(value) -> str:
        try:
            theta = float(value)
        except (TypeError, ValueError):
            return "--"
        if not math.isfinite(theta) or abs(theta) > 3600.0:
            return "Invalid"
        normalized = ((theta + 180.0) % 360.0) - 180.0
        if math.isclose(normalized, -180.0, abs_tol=1e-9) and theta > 0.0:
            normalized = 180.0
        return f"{normalized:+.2f} deg"

    @staticmethod
    def _format_raw_snap(raw) -> str:
        text = str(raw or "").strip()
        if not text:
            return "SNAP raw: --"
        if len(text) > 90:
            text = text[:87] + "..."
        return f"SNAP raw: {text}"

    @staticmethod
    def _signal_unit(settings: dict) -> str:
        try:
            input_config = int(settings.get("input_config", 0))
        except (TypeError, ValueError):
            input_config = 0
        return "A" if input_config in (2, 3) else "V"

    @classmethod
    def _sensitivity_limit(cls, settings: dict):
        try:
            index = int(settings.get("sensitivity"))
        except (TypeError, ValueError):
            return None
        if index < 0 or index >= len(SENSITIVITY_LABELS):
            return None
        use_current = cls._signal_unit(settings) == "A"
        return sensitivity_value(index, use_current=use_current)

    def _emit_voltage_sensitivity(self, settings: dict):
        try:
            index = int(settings.get("sensitivity"))
        except (TypeError, ValueError):
            return
        value = sensitivity_value(index, use_current=False)
        if value is None or not math.isfinite(float(value)):
            return
        label = SENSITIVITY_LABELS[index] if 0 <= index < len(SENSITIVITY_LABELS) else f"Code {index}"
        self.sensitivity_read.emit(float(value), label)

    def _setting_widgets(self) -> list[QtWidgets.QWidget]:
        return [
            self.cbo_sensitivity,
            self.cbo_time_constant,
            self.cbo_reserve,
            self.cbo_filter_slope,
            self.cbo_ref_source,
            self.sp_phase,
            self.sp_frequency,
            self.sp_sine_out,
            self.sp_harmonic,
            self.cbo_input_config,
            self.cbo_current_gain,
            self.cbo_input_coupling,
            self.cbo_input_ground,
            self.cbo_line_filter,
        ]
