"""Small, controller-only magnet control panel.

This widget intentionally knows nothing about VISA or the vendor SDK.  Both
backends are created by ``MainWindow`` and all calls below are routed through
their respective threaded controllers.
"""

from __future__ import annotations

from datetime import datetime
import time
from typing import Any, Optional

from PyQt6 import QtCore, QtWidgets

from utils.config import cfg
from app.ui.widgets.safe_combo import SafeComboBox
from app.ui.widgets.safe_spinbox import SafeDoubleSpinBox


class MagnetPanel(QtWidgets.QWidget):
    """UI for one selected magnet backend, sharing two application controllers."""

    backend_changed = QtCore.pyqtSignal(str)

    def __init__(self, magnet1000, magnet2100, parent=None, lakeshore335=None, thermal_safety=None):
        super().__init__(parent)
        self.magnet1000 = magnet1000
        self.magnet2100 = magnet2100
        self.lakeshore335 = lakeshore335
        self.thermal_safety = thermal_safety
        self._backend = "1000"
        self._connected = {"1000": False, "2100": False}
        self._reviewed = False
        self._pending_2100_start = False
        self._busy_1000 = False
        self._busy_2100 = False
        self._review_fingerprint = None
        self._temp_request_pending = False
        self._aps_heater_state: Optional[bool] = None
        self._aps_latest_snapshot = None
        self._aps_connect_pending = False
        self._aps_exclusive = False
        self._refresh_pending_backend: Optional[str] = None
        self._last_update_at = {"1000": None, "2100": None}
        self._last_capabilities = None
        self._last_telemetry_log_at = None
        self._last_telemetry_log_key = None
        self._last_telemetry_phase_key = None
        self._last_progress_log_at = None
        self._last_progress_log_key = None
        self._last_progress_label = None
        self._heater_progress_milestones = {}
        self._heater_progress_last_value = {}
        self._heater_progress_active_label = None
        self._refresh_timeout_timer = QtCore.QTimer(self)
        self._refresh_timeout_timer.setSingleShot(True)
        self._refresh_timeout_timer.setInterval(10_000)
        self._refresh_timeout_timer.timeout.connect(self._on_refresh_timeout)
        self._build_ui()
        self._connect_signals()
        self._select_backend(0)

    def _build_ui(self):
        root = QtWidgets.QVBoxLayout(self)
        root.setContentsMargins(8, 8, 8, 8)

        selector_row = QtWidgets.QHBoxLayout()
        selector_row.addWidget(QtWidgets.QLabel("Magnet"))
        self.backend_combo = SafeComboBox()
        self.backend_combo.addItem("attoDRY1000 (APS100)", "1000")
        self.backend_combo.addItem("attoDRY2100 (SDK)", "2100")
        self.backend_combo.currentIndexChanged.connect(self._select_backend)
        selector_row.addWidget(self.backend_combo, 1)
        root.addLayout(selector_row)

        self.review = QtWidgets.QCheckBox(
            "I reviewed the configured limits and connection settings"
        )
        self.review.setToolTip(
            "Required before connecting or commanding either production magnet."
        )
        self.review.toggled.connect(self._set_reviewed)
        root.addWidget(self.review)
        self.review_details = QtWidgets.QLabel(
            "APS100: resource {resource}, coil {coil:g} T/A, field ±{field:g} T, "
            "rate ≤{rate:g} A/s, heater {warm:g}/{cool:g} s\n"
            "2100: SDK {sdk}, host {host}:{channel}, field ±{field2100:g} T, "
            "temperature {tmin}–{tmax} K".format(
                resource=cfg.magnet.visa_resource,
                coil=cfg.magnet.coil_constant_t_per_a,
                field=cfg.magnet.safe_control_max_field_t,
                rate=cfg.magnet.maximum_rate_a_per_s,
                warm=cfg.magnet.heater_warm_s,
                cool=cfg.magnet.heater_cool_s,
                sdk=cfg.attodry2100.sdk_directory,
                host=cfg.attodry2100.host,
                channel=cfg.attodry2100.channel,
                field2100=cfg.attodry2100.maximum_field_t or 6.0,
                tmin=(cfg.attodry2100.minimum_temperature_k
                      if cfg.attodry2100.minimum_temperature_k is not None else "unset"),
                tmax=(cfg.attodry2100.maximum_temperature_k
                      if cfg.attodry2100.maximum_temperature_k is not None else "unset"),
            )
        )
        self.review_details.setWordWrap(True)
        self.review_details.setStyleSheet("color: #777; font-size: 10px;")
        root.addWidget(self.review_details)

        connection = QtWidgets.QGroupBox("Connection")
        conn_form = QtWidgets.QFormLayout(connection)
        self.connect_button = QtWidgets.QPushButton("Connect")
        self.disconnect_button = QtWidgets.QPushButton("Disconnect")
        self.connect_button.clicked.connect(self._connect_selected)
        self.disconnect_button.clicked.connect(self._disconnect_selected)
        buttons = QtWidgets.QHBoxLayout()
        buttons.addWidget(self.connect_button)
        buttons.addWidget(self.disconnect_button)
        conn_form.addRow("", buttons)
        root.addWidget(connection)

        status = QtWidgets.QGroupBox("Status")
        status_form = QtWidgets.QFormLayout(status)
        self.connection_label = QtWidgets.QLabel("Disconnected")
        self.fault_label = QtWidgets.QLabel("None")
        self.state_label = QtWidgets.QLabel("Idle")
        self.last_update_label = QtWidgets.QLabel("Never")
        self.current_field_label = QtWidgets.QLabel("—")
        self.output_field_label = QtWidgets.QLabel("—")
        self.output_current_label = QtWidgets.QLabel("—")
        self.sweep_label = QtWidgets.QLabel("—")
        self.heater_label = QtWidgets.QLabel("—")
        self.voltage_label = QtWidgets.QLabel("—")
        self.target_field = SafeDoubleSpinBox()
        self.target_field.setDecimals(6)
        self.target_field.setRange(-9.0, 9.0)
        self.target_field.setSuffix(" T")
        status_form.addRow("Connection", self.connection_label)
        status_form.addRow("State", self.state_label)
        status_form.addRow("Last successful update", self.last_update_label)
        status_form.addRow("Fault", self.fault_label)
        status_form.addRow("Current field", self.current_field_label)
        status_form.addRow("Output field", self.output_field_label)
        status_form.addRow("Output current", self.output_current_label)
        status_form.addRow("Sweep / standby", self.sweep_label)
        status_form.addRow("Heater", self.heater_label)
        status_form.addRow("Magnet / output voltage", self.voltage_label)
        status_form.addRow("Target field", self.target_field)
        root.addWidget(status)

        action_row = QtWidgets.QHBoxLayout()
        self.move_button = QtWidgets.QPushButton("Move to target")
        self.move_button.clicked.connect(self._move_to_target)
        self.stop_button = QtWidgets.QPushButton("STOP / PAUSE")
        self.stop_button.setMinimumHeight(38)
        self.stop_button.setStyleSheet("font-weight: bold; background: #a51d2d; color: white;")
        self.stop_button.clicked.connect(self._stop_selected)
        self.refresh_button = QtWidgets.QPushButton("Refresh")
        self.refresh_button.clicked.connect(self._refresh_selected)
        action_row.addWidget(self.move_button)
        action_row.addWidget(self.stop_button)
        action_row.addWidget(self.refresh_button)
        root.addLayout(action_row)

        self.aps_group = QtWidgets.QGroupBox("APS100 controls")
        aps_form = QtWidgets.QFormLayout(self.aps_group)
        self.mode_combo = SafeComboBox()
        self.mode_combo.addItem("Driven", "driven")
        self.mode_combo.addItem("Persistent", "persistent")
        self.mode_combo.setCurrentIndex(1)
        aps_form.addRow("Final mode", self.mode_combo)
        aps_form.addRow("", QtWidgets.QLabel(
            "Persistent mode always cools the heater and zeros the leads."
        ))
        root.addWidget(self.aps_group)

        self.temp_group = QtWidgets.QGroupBox("attoDRY2100 temperature")
        temp_form = QtWidgets.QFormLayout(self.temp_group)
        self.magnet_temperature = QtWidgets.QLabel("—")
        self.sample_temperature = QtWidgets.QLabel("Unavailable")
        self.vti_temperature = QtWidgets.QLabel("Unavailable")
        self.sample_target = SafeDoubleSpinBox()
        self.sample_target.setRange(1.8, 300.0)
        self.sample_target.setDecimals(3)
        self.sample_target.setSuffix(" K")
        self.sample_rate = SafeDoubleSpinBox()
        self.sample_rate.setRange(0.1, 100.0)
        self.sample_rate.setValue(1.0)
        self.sample_rate.setDecimals(2)
        self.sample_rate.setSuffix(" K/min")
        self.temp_control_button = QtWidgets.QPushButton("Set temperature")
        self.temp_stop_button = QtWidgets.QPushButton("Stop temperature control")
        self.temp_control_button.clicked.connect(self._set_temperature)
        self.temp_stop_button.clicked.connect(self._stop_temperature)
        temp_form.addRow("Magnet temperature", self.magnet_temperature)
        temp_form.addRow("Sample temperature", self.sample_temperature)
        temp_form.addRow("VTI temperature", self.vti_temperature)
        temp_form.addRow("Sample target", self.sample_target)
        temp_form.addRow("Ramp rate", self.sample_rate)
        temp_form.addRow("", self.temp_control_button)
        temp_form.addRow("", self.temp_stop_button)
        root.addWidget(self.temp_group)

        self.lakeshore_group = QtWidgets.QGroupBox("Lake Shore 335 Temperature Safety")
        ls_form = QtWidgets.QFormLayout(self.lakeshore_group)
        self.lakeshore_sample_temperature = QtWidgets.QLabel("—")
        self.lakeshore_reservoir_temperature = QtWidgets.QLabel("—")
        self.lakeshore_sample_slope = QtWidgets.QLabel("—")
        self.lakeshore_reservoir_slope = QtWidgets.QLabel("—")
        self.lakeshore_age = QtWidgets.QLabel("—")
        self.lakeshore_status = QtWidgets.QLabel("—")
        self.lakeshore_state = QtWidgets.QLabel("DISARMED")
        self.lakeshore_permission = QtWidgets.QLabel("No")
        self.lakeshore_resource = QtWidgets.QLabel(getattr(cfg.lakeshore335, "visa_resource", "ASRL6::INSTR"))
        self.lakeshore_connection = QtWidgets.QLabel("Disconnected")
        self.lakeshore_mapping = QtWidgets.QLabel("Not verified")
        self.lakeshore_armed = QtWidgets.QLabel("No")
        self.lakeshore_connect_button = QtWidgets.QPushButton("Connect LS335")
        self.lakeshore_disconnect_button = QtWidgets.QPushButton("Disconnect LS335")
        self.lakeshore_connect_button.clicked.connect(self._connect_lakeshore)
        self.lakeshore_disconnect_button.clicked.connect(self._disconnect_lakeshore)
        self.lakeshore_thresholds = QtWidgets.QLabel()
        self.lakeshore_thresholds.setWordWrap(True)
        self.lakeshore_thresholds.setText(
            "Sample W/T/R: {}/{}/{} K; Reservoir W/T/R: {}/{}/{} K".format(
                cfg.lakeshore335.sample_warning_temperature_k or "unset",
                cfg.lakeshore335.sample_trip_temperature_k or "unset",
                cfg.lakeshore335.sample_recovery_temperature_k or "unset",
                cfg.lakeshore335.reservoir_warning_temperature_k or "unset",
                cfg.lakeshore335.reservoir_trip_temperature_k or "unset",
                cfg.lakeshore335.reservoir_recovery_temperature_k or "unset",
            )
        )
        for label, value in (
            ("Sample temperature", self.lakeshore_sample_temperature),
            ("Reservoir temperature", self.lakeshore_reservoir_temperature),
            ("Sample dT/dt", self.lakeshore_sample_slope),
            ("Reservoir dT/dt", self.lakeshore_reservoir_slope),
            ("Reading age", self.lakeshore_age),
            ("Sensor status", self.lakeshore_status),
            ("Thermal state", self.lakeshore_state),
            ("Magnet permission", self.lakeshore_permission),
            ("VISA resource", self.lakeshore_resource),
            ("Connection", self.lakeshore_connection),
            ("Mapping verified", self.lakeshore_mapping),
            ("Interlock armed", self.lakeshore_armed),
            ("Configured thresholds", self.lakeshore_thresholds),
        ):
            ls_form.addRow(label, value)
        ls_buttons = QtWidgets.QHBoxLayout()
        ls_buttons.addWidget(self.lakeshore_connect_button)
        ls_buttons.addWidget(self.lakeshore_disconnect_button)
        ls_form.addRow("", ls_buttons)
        root.addWidget(self.lakeshore_group)

        self.progress = QtWidgets.QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.progress.setFormat("%p%")
        root.addWidget(self.progress)
        self.message_label = QtWidgets.QLabel("Ready — review configuration before connecting")
        self.message_label.setWordWrap(True)
        root.addWidget(self.message_label)

        activity = QtWidgets.QGroupBox("Magnet activity log")
        activity_layout = QtWidgets.QVBoxLayout(activity)
        self.activity_log = QtWidgets.QPlainTextEdit()
        self.magnet_log = self.activity_log
        self.log = self.activity_log
        self.activity_log.setReadOnly(True)
        self.activity_log.setMaximumBlockCount(2000)
        self.activity_log.setMinimumHeight(130)
        self.activity_log.setPlaceholderText("Magnet connection and control events will appear here.")
        activity_layout.addWidget(self.activity_log)
        activity_buttons = QtWidgets.QHBoxLayout()
        self.copy_log_button = QtWidgets.QPushButton("Copy log")
        self.clear_log_button = QtWidgets.QPushButton("Clear display")
        self.copy_log_button.clicked.connect(self._copy_activity_log)
        self.clear_log_button.clicked.connect(self._clear_activity_log)
        activity_buttons.addStretch(1)
        activity_buttons.addWidget(self.copy_log_button)
        activity_buttons.addWidget(self.clear_log_button)
        activity_layout.addLayout(activity_buttons)
        root.addWidget(activity)
        root.addStretch(1)

    def _append_activity(self, message: str, category: str = "INFO"):
        """Append a bounded, timestamped UI event without affecting saved logs."""
        # Aware local time follows the PC's configured timezone, including DST.
        stamp = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %z")
        self.activity_log.appendPlainText(f"{stamp} [{category}] {str(message)}")
        self.activity_log.verticalScrollBar().setValue(
            self.activity_log.verticalScrollBar().maximum()
        )

    def _copy_activity_log(self):
        QtWidgets.QApplication.clipboard().setText(self.activity_log.toPlainText())

    def _clear_activity_log(self):
        # This only clears the on-screen bounded view; controller/run files are
        # intentionally never touched.
        self.activity_log.clear()

    def _connect_signals(self):
        self.magnet1000.connected.connect(lambda identity: self._on_connected("1000", identity))
        self.magnet1000.disconnected.connect(lambda: self._on_disconnected("1000"))
        self.magnet1000.snapshot_updated.connect(lambda snapshot: self._on_snapshot("1000", snapshot))
        self.magnet1000.transition_progress.connect(self._on_progress)
        self.magnet1000.operation_finished.connect(self._on_operation)
        self.magnet1000.error.connect(lambda message: self._on_1000_error(message))
        self.magnet1000.fault.connect(self._on_fault)
        if hasattr(self.magnet1000, "exclusive_changed"):
            self.magnet1000.exclusive_changed.connect(self._on_aps_exclusive_changed)

        self.magnet2100.connected.connect(lambda identity: self._on_connected("2100", identity))
        self.magnet2100.disconnected.connect(lambda: self._on_disconnected("2100"))
        self.magnet2100.snapshot_updated.connect(lambda snapshot: self._on_snapshot("2100", snapshot))
        self.magnet2100.temperature_updated.connect(self._on_temperature)
        self.magnet2100.operation_finished.connect(self._on_2100_operation)
        self.magnet2100.error.connect(lambda message: self._on_2100_error(message))
        self.magnet2100.fault.connect(self._on_fault)
        if self.lakeshore335 is not None:
            self.lakeshore335.connected.connect(lambda *_: self.lakeshore_connection.setText("Connected"))
            self.lakeshore335.disconnected.connect(lambda: self._on_lakeshore_disconnected())
            self.lakeshore335.snapshot_updated.connect(self._on_lakeshore_snapshot)
            self.lakeshore335.fault.connect(lambda message: self._on_lakeshore_fault(message))

        self._temp_timer = QtCore.QTimer(self)
        self._temp_timer.setInterval(2000)
        self._temp_timer.timeout.connect(self._poll_temperature)

    def _on_lakeshore_disconnected(self):
        self.lakeshore_connection.setText("Disconnected")
        self.lakeshore_permission.setText("No")
        self.lakeshore_state.setText("MONITOR_FAULT")

    def _connect_lakeshore(self):
        if self.lakeshore335 is not None:
            self.lakeshore335.connect_instrument(cfg.lakeshore335.visa_resource)

    def _disconnect_lakeshore(self):
        if self.lakeshore335 is not None:
            self.lakeshore335.disconnect_instrument()

    def _on_lakeshore_fault(self, message):
        self.lakeshore_connection.setText("Fault")
        self.lakeshore_permission.setText("No")
        self.lakeshore_state.setText("MONITOR_FAULT")
        self._append_activity(message, "FAULT")

    def _on_lakeshore_snapshot(self, snapshot):
        sample = getattr(snapshot, "sample_temperature_k", None)
        reservoir = getattr(snapshot, "reservoir_temperature_k", None)
        self.lakeshore_sample_temperature.setText("—" if sample is None else f"{float(sample):.3f} K")
        self.lakeshore_reservoir_temperature.setText("—" if reservoir is None else f"{float(reservoir):.3f} K")
        for widget, value in ((self.lakeshore_sample_slope, getattr(snapshot, "sample_slope_k_per_min", None)),
                              (self.lakeshore_reservoir_slope, getattr(snapshot, "reservoir_slope_k_per_min", None))):
            widget.setText("—" if value is None else f"{float(value):+.3f} K/min")
        try:
            age = float(getattr(snapshot, "reading_age_s"))
            self.lakeshore_age.setText(f"{age:.1f} s")
        except (TypeError, ValueError):
            self.lakeshore_age.setText("—")
        self.lakeshore_status.setText(f"{getattr(snapshot, 'sample_sensor_status', '—')} / {getattr(snapshot, 'reservoir_sensor_status', '—')}")
        self.lakeshore_connection.setText("Connected" if getattr(snapshot, "connected", False) else "Disconnected")
        if self.thermal_safety is not None:
            decision = self.thermal_safety.evaluate(snapshot)
            self.lakeshore_state.setText(decision.state.value)
            self.lakeshore_permission.setText("Yes" if decision.magnet_permission else "No")
            self.lakeshore_armed.setText("Yes" if self.thermal_safety.is_armed else "No")
        self.lakeshore_mapping.setText("Verified" if cfg.lakeshore335.verified_channel_mapping else "Not verified")

    def _set_reviewed(self, checked: bool):
        self._reviewed = bool(checked)
        self._review_fingerprint = self._configuration_fingerprint() if checked else None
        self.message_label.setText(
            "Configuration review acknowledged for this session"
            if checked else "Review configured coil/field/ramp/heater/SDK/temperature limits before real control"
        )
        if checked and self.fault_label.text() == "Commissioning review required":
            self.fault_label.setText("None")

    def _configuration_fingerprint(self):
        return (
            cfg.magnet.visa_resource, cfg.magnet.baud_rate, cfg.magnet.timeout_ms,
            cfg.magnet.coil_constant_t_per_a, cfg.magnet.maximum_field_t,
            cfg.magnet.maximum_current_a, cfg.magnet.maximum_rate_a_per_s,
            cfg.magnet.safe_control_max_field_t, cfg.magnet.heater_warm_s,
            cfg.magnet.heater_cool_s, cfg.magnet.persistent_zero_max_magnet_voltage_v,
            cfg.attodry2100.sdk_directory, cfg.attodry2100.host, cfg.attodry2100.channel,
            cfg.attodry2100.maximum_field_t, cfg.attodry2100.minimum_temperature_k,
            cfg.attodry2100.maximum_temperature_k,
        )

    def _invalidate_review(self, *_args):
        if self._reviewed and self._review_fingerprint != self._configuration_fingerprint():
            self._reviewed = False
            self._review_fingerprint = None
            self.review.blockSignals(True)
            self.review.setChecked(False)
            self.review.blockSignals(False)
            self._guard_message()

    def _review_valid(self):
        return bool(self._reviewed and self._review_fingerprint == self._configuration_fingerprint())

    def _select_backend(self, index: int):
        self._backend = str(self.backend_combo.itemData(index) or "1000")
        self.backend_changed.emit(self._backend)
        aps = self._backend == "1000"
        self.aps_group.setVisible(aps)
        self.temp_group.setVisible(not aps)
        self.lakeshore_group.setVisible(aps)
        self.target_field.setRange(
            -float(cfg.magnet.safe_control_max_field_t if aps else (cfg.attodry2100.maximum_field_t or 6.0)),
            float(cfg.magnet.safe_control_max_field_t if aps else (cfg.attodry2100.maximum_field_t or 6.0)),
        )
        self.connection_label.setText("Connected" if self._connected[self._backend] else "Disconnected")
        updated = self._last_update_at.get(self._backend)
        self.last_update_label.setText(updated or "Never")
        self._update_buttons()

    def _controller(self):
        return self.magnet1000 if self._backend == "1000" else self.magnet2100

    def _connect_selected(self):
        if self._connected[self._backend] or (
            self._busy_1000 if self._backend == "1000" else self._busy_2100
        ):
            return
        if self._backend == "1000":
            if self._aps_connect_pending:
                return
            if not self._review_valid():
                self._guard_message()
                return
            self._aps_connect_pending = True
            self._append_activity(
                f"Connecting APS100 ({cfg.magnet.visa_resource})", "CONNECTION"
            )
            self._update_buttons()
            self.magnet1000.connect_instrument(cfg.magnet.visa_resource, use_mock=False)
        else:
            if not self._review_valid():
                self._guard_message()
                return
            self._append_activity("Connecting attoDRY2100", "CONNECTION")
            self.magnet2100.connect_async()

    def _disconnect_selected(self):
        self._append_activity(
            f"Disconnect requested ({'APS100' if self._backend == '1000' else 'attoDRY2100'})",
            "CONNECTION",
        )
        if self._backend == "1000":
            self.magnet1000.disconnect_instrument()
        else:
            self.magnet2100.disconnect_async()

    def _refresh_selected(self):
        if self._refresh_pending_backend is not None:
            return
        backend = self._backend
        name = "APS100" if backend == "1000" else "attoDRY2100"
        self._refresh_pending_backend = backend
        self._refresh_timeout_timer.start()
        self.message_label.setText(f"{name} telemetry refresh pending…")
        self._append_activity(f"{name} telemetry refresh requested", "TELEMETRY")
        self._update_buttons()
        if self._backend == "1000":
            self.magnet1000.refresh_snapshot()
        else:
            self.magnet2100.read_snapshot_async()
            self._poll_temperature()

    def _clear_refresh_pending(self, backend: str):
        if self._refresh_pending_backend != str(backend):
            return False
        self._refresh_pending_backend = None
        self._refresh_timeout_timer.stop()
        self._update_buttons()
        return True

    def _on_refresh_timeout(self):
        backend = self._refresh_pending_backend
        if backend is None:
            return
        self._refresh_pending_backend = None
        name = "APS100" if backend == "1000" else "attoDRY2100"
        message = f"{name} telemetry refresh timed out; no successful snapshot was received"
        self.message_label.setText(message)
        self._append_activity(message, "ERROR")
        self._update_buttons()

    def _stop_selected(self):
        self._append_activity("Stop / pause requested by user", "STOP")
        if self._backend == "1000":
            self.magnet1000.pause()
        else:
            self._pending_2100_start = False
            self.magnet2100.request_stop()
        self.state_label.setText("Stopping…")
        self.message_label.setText("Stop/Pause requested")

    def _move_to_target(self):
        target = float(self.target_field.value())
        if self._backend == "1000":
            if not self._connected["1000"]:
                self._show_error("APS100 is not connected")
                return
            mode = str(self.mode_combo.currentData())
            persistent = mode == "persistent"
            if self._busy_1000:
                self._show_error("APS100 operation already in progress")
                return
            if self._aps_exclusive:
                self._show_error("APS100 is reserved by the active Gate Scan B-field batch")
                return
            if not self._review_valid():
                self._guard_message()
                return
            if self._aps_heater_state is None:
                self._show_error("APS100 heater state is unknown; Refresh telemetry before moving")
                return
            if abs(target) > float(cfg.magnet.safe_control_max_field_t):
                self._show_error(f"Target exceeds configured safe limit ±{cfg.magnet.safe_control_max_field_t:g} T")
                return
            confirmed = False
            confirmation_text = None
            if not self._aps_heater_state:
                current = getattr(self._aps_latest_snapshot, "field_t", None)
                current_text = "unknown" if current is None else f"{float(current):+.6f} T"
                confirmation_text = (
                    f"The APS100 heater is currently OFF and the displayed stored field "
                    f"is {current_text}.\n\n"
                    "Confirm the displayed field and polarity are correct. The supply "
                    "will match the persistent leads before turning the heater on."
                )
                if persistent:
                    confirmation_text += (
                        "\n\nAfter reaching the target, the heater will be switched off, "
                        "the configured cool timing observed, and the leads swept with ZERO."
                    )
            elif persistent:
                confirmation_text = (
                    "The heater will be switched off after reaching the target.\n\n"
                    "The configured cool timing will be observed, then the supply "
                    "leads will be swept with ZERO."
                )
            if confirmation_text is not None:
                answer = QtWidgets.QMessageBox.question(
                    self,
                    "Confirm persistent-field move",
                    confirmation_text + "\n\nContinue?",
                    QtWidgets.QMessageBox.StandardButton.Yes
                    | QtWidgets.QMessageBox.StandardButton.No,
                    QtWidgets.QMessageBox.StandardButton.No,
                )
                if answer != QtWidgets.QMessageBox.StandardButton.Yes:
                    self.message_label.setText("Persistent move cancelled")
                    self._append_activity("Persistent move cancelled by user", "COMMAND")
                    return
                confirmed = True
            self._append_activity(
                f"Move requested: {target:+.6f} T, final mode={mode}", "COMMAND"
            )
            self._busy_1000 = True
            self._update_buttons()
            self._reset_activity_throttles()
            self.magnet1000.safe_move_to_field(
                target, final_mode=mode, zero_leads=True,
                persistent_field_confirmed=confirmed,
            )
            return
        if not self._connected["2100"]:
            self._show_error("attoDRY2100 is not connected")
            return
        maximum = min(float(cfg.attodry2100.maximum_field_t or 6.0), 6.0)
        if abs(target) > maximum:
            self._show_error(f"Target exceeds configured 2100 limit ±{maximum:g} T")
            return
        if self._busy_2100:
            self._show_error("attoDRY2100 operation already in progress")
            return
        if not self._review_valid():
            self._guard_message()
            return
        self._pending_2100_start = True
        self._busy_2100 = True
        self._append_activity(f"Field setpoint requested: {target:+.6f} T", "COMMAND")
        self._reset_activity_throttles()
        self._update_buttons()
        self.magnet2100.set_h_setpoint_async(target)
        self.state_label.setText("Arming…")

    def _set_temperature(self):
        if not self._connected["2100"]:
            self._on_2100_error("attoDRY2100 is not connected")
            return
        if self._temp_request_pending:
            self._show_error("2100 temperature operation already in progress")
            return
        if not self._review_valid():
            self._guard_message()
            return
        if self._last_capabilities is None or not getattr(self._last_capabilities, "sample_temperature_control", False):
            self._show_error("2100 sample temperature control is not available")
            return
        self._temp_request_pending = True
        self._update_buttons()
        self.magnet2100.configure_sample_temperature_async(
            self.sample_target.value(), self.sample_rate.value()
        )

    def _stop_temperature(self):
        self.magnet2100.stop_sample_temperature_control_async()

    def _poll_temperature(self):
        if (
            self._backend == "2100"
            and self._connected["2100"]
            and bool(getattr(self._last_capabilities, "sample_temperature_readback", False))
        ):
            self.magnet2100.read_temperature_snapshot_async()

    def _on_connected(self, backend: str, identity: Any):
        self._connected[backend] = True
        if backend == "1000":
            self._aps_connect_pending = False
        self.connection_label.setText("Connected")
        self.message_label.setText(str(getattr(identity, "display_name", identity)))
        self._append_activity(
            f"{('APS100' if backend == '1000' else 'attoDRY2100')} connected: "
            f"{getattr(identity, 'display_name', identity)}", "CONNECTION"
        )
        if backend == "2100":
            self.magnet2100.set_polling_enabled(True)
            self._temp_timer.start()
        self._update_buttons()

    def _on_disconnected(self, backend: str):
        self._clear_refresh_pending(backend)
        self._connected[backend] = False
        if backend == self._backend:
            self.connection_label.setText("Disconnected")
        if backend == "2100":
            self._temp_timer.stop()
            self._temp_request_pending = False
        else:
            self._aps_connect_pending = False
            self._aps_heater_state = None
            self._aps_latest_snapshot = None
        self._append_activity(
            f"{('APS100' if backend == '1000' else 'attoDRY2100')} disconnected",
            "CONNECTION",
        )
        self._update_buttons()

    def _on_snapshot(self, backend: str, snapshot: Any):
        updated = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %z")
        self._last_update_at[backend] = updated
        if backend == self._backend:
            self.last_update_label.setText(updated)
        if self._clear_refresh_pending(backend):
            name = "APS100" if backend == "1000" else "attoDRY2100"
            self.message_label.setText(f"{name} telemetry refreshed successfully")
            self._append_activity(
                f"{name} telemetry refreshed successfully", "TELEMETRY"
            )
        if backend == "1000":
            self._aps_latest_snapshot = snapshot
            heater_state = getattr(snapshot, "heater_on", None)
            self._aps_heater_state = heater_state if isinstance(heater_state, bool) else None
        if backend != self._backend:
            return
        self.current_field_label.setText(f"{float(snapshot.field_t):+.6f} T")
        self.magnet_temperature.setText(
            "—" if getattr(snapshot, "temperature_k", None) is None
            else f"{float(snapshot.temperature_k):.3f} K"
        )
        if backend == "1000":
            self.output_field_label.setText(
                f"{float(snapshot.output_field_t):+.6f} T"
            )
            self.output_current_label.setText(
                f"{float(snapshot.output_current_a):+.4f} A"
            )
            status = getattr(snapshot, "status", None)
            standby = "standby" if getattr(status, "standby", False) else "active"
            sweep = str(getattr(snapshot, "sweep_state", "—"))
            self.sweep_label.setText(f"{sweep} / {standby}")
            self.heater_label.setText(
                "Unknown" if self._aps_heater_state is None
                else ("On" if self._aps_heater_state else "Off")
            )
            self.voltage_label.setText(
                f"{float(snapshot.magnet_voltage_v):.3f} / "
                f"{float(snapshot.output_voltage_v):.3f} V"
            )
        status = getattr(snapshot, "status", None)
        # APS100 faults are emitted by MagnetController with session-level
        # deduplication; do not re-log every fast-poll snapshot here.
        if backend != "1000" and status is not None and getattr(status, "quench", False):
            self._on_fault("Quench reported")
        if backend == "1000" and (self._busy_1000 or self._aps_exclusive):
            values = (
                round(float(getattr(snapshot, "field_t", 0.0)), 5),
                round(float(getattr(snapshot, "output_field_t", 0.0)), 5),
                round(float(getattr(snapshot, "output_current_a", 0.0)), 4),
                str(getattr(snapshot, "sweep_state", "—")),
                self._aps_heater_state,
            )
            now = time.monotonic()
            phase_key = (
                values[3],
                bool(getattr(status, "standby", False)) if status is not None else None,
                values[4],
                (bool(getattr(status, "quench", False)),
                 bool(getattr(status, "power_module_failure", False)))
                if status is not None else None,
            )
            if (
                self._last_telemetry_log_at is None
                or phase_key != self._last_telemetry_phase_key
                or now - self._last_telemetry_log_at >= float(getattr(cfg.magnet, "telemetry_heartbeat_interval_s", 30.0))
            ):
                self._append_activity(
                    f"{('APS100' if backend == '1000' else '2100')} telemetry: "
                    f"field={float(snapshot.field_t):+.6f} T"
                    + (
                        f", output={float(snapshot.output_field_t):+.6f} T, "
                        f"current={float(snapshot.output_current_a):+.4f} A, "
                        f"sweep={getattr(snapshot, 'sweep_state', '—')}, "
                        f"heater={'on' if self._aps_heater_state else 'off' if self._aps_heater_state is False else 'unknown'}"
                        if backend == "1000" else ""
                    ),
                    "TELEMETRY",
                )
                self._last_telemetry_log_key = values
                self._last_telemetry_phase_key = phase_key
                self._last_telemetry_log_at = now
        elif backend != "1000" and self._busy_2100:
            # Preserve the attoDRY2100's existing busy telemetry cadence.
            values = (
                round(float(getattr(snapshot, "field_t", 0.0)), 5),
                round(float(getattr(snapshot, "output_field_t", 0.0)), 5),
                round(float(getattr(snapshot, "output_current_a", 0.0)), 4),
                str(getattr(snapshot, "sweep_state", "—")),
                self._aps_heater_state,
            )
            now = time.monotonic()
            phase = (values[3], values[4])
            if (self._last_telemetry_log_at is None or
                    phase != self._last_telemetry_phase_key or
                    now - self._last_telemetry_log_at >= 1.0):
                self._append_activity(
                    "2100 telemetry: "
                    f"field={float(snapshot.field_t):+.6f} T",
                    "TELEMETRY",
                )
                self._last_telemetry_log_key = values
                self._last_telemetry_phase_key = phase
                self._last_telemetry_log_at = now
        self._last_capabilities = getattr(snapshot, "capabilities", None)
        if self._backend == "2100":
            self._update_buttons()

    def _on_temperature(self, snapshot: Any):
        sample = getattr(snapshot, "sample_temperature_k", None)
        vti = getattr(snapshot, "vti_temperature_k", None)
        self.sample_temperature.setText("Unavailable" if sample is None else f"{float(sample):.3f} K")
        self.vti_temperature.setText("Unavailable" if vti is None else f"{float(vti):.3f} K")

    def _on_progress(self, label: str, value: float):
        label = str(label)
        numeric = float(value)
        lower = label.lower()
        field_phase = any(token in lower for token in ("field", "leads", "driven"))
        seconds_phase = any(token in lower for token in ("heater", "settling", "cooling"))
        unit = "T" if field_phase and not seconds_phase else "s" if seconds_phase else ""
        self.progress.setRange(0, 0)
        self.progress.setFormat(
            f"{label}: {numeric:.3f} {unit}".rstrip()
        )
        self.state_label.setText(label)
        now = time.monotonic()
        key = (label, round(numeric, 3))
        if seconds_phase and "heater" in lower:
            # Countdown remains live in the status bar, while durable logs
            # contain only meaningful milestones rather than every poll.
            if self._heater_progress_active_label != label:
                # A new transition (or a new run after a field/lead phase)
                # gets its own started/milestone record.  Repeated polls for
                # one transition remain deduplicated.
                self._heater_progress_milestones.pop(label, None)
                self._heater_progress_last_value.pop(label, None)
                self._heater_progress_active_label = label
            milestones = (120, 90, 60, 30, 10, 0)
            logged = self._heater_progress_milestones.setdefault(label, set())
            previous = self._heater_progress_last_value.get(label)
            if previous is None:
                self._append_activity(
                    f"{label}: {numeric:.3f} s (started)", "PROGRESS"
                )
            for mark in milestones:
                crossed = (
                    previous is None and abs(numeric - mark) <= 1e-6
                ) or (
                    previous is not None and numeric <= mark + 1e-6 < previous
                )
                if crossed and mark not in logged:
                    self._append_activity(
                        f"{label}: {mark:.0f} s milestone", "PROGRESS"
                    )
                    logged.add(mark)
            self._heater_progress_last_value[label] = numeric
            self._last_progress_log_key = key
            self._last_progress_label = label
            self._last_progress_log_at = now
            return
        self._heater_progress_active_label = None
        if (
            self._last_progress_log_at is None
            or label != self._last_progress_label
            or now - self._last_progress_log_at >= 10.0
        ):
            self._append_activity(
                f"{label}: {numeric:.3f} {unit}".rstrip(), "PROGRESS"
            )
            self._last_progress_log_key = key
            self._last_progress_label = label
            self._last_progress_log_at = now

    def _reset_activity_throttles(self):
        self._last_telemetry_log_at = None
        self._last_telemetry_log_key = None
        self._last_telemetry_phase_key = None
        self._last_progress_log_at = None
        self._last_progress_log_key = None
        self._last_progress_label = None
        self._heater_progress_milestones = {}
        self._heater_progress_last_value = {}
        self._heater_progress_active_label = None

    def _on_operation(self, name: str):
        self._busy_1000 = False
        failed = str(name).startswith("failed:")
        self.state_label.setText("Faulted" if failed else "Complete")
        self.progress.setRange(0, 100)
        self.progress.setValue(0 if failed else 100)
        self.progress.setFormat("Faulted" if failed else "Complete")
        self._append_activity(
            f"APS100 operation {'failed' if failed else 'complete'}: {name}",
            "ERROR" if failed else "COMPLETE",
        )
        if not failed:
            self.message_label.setText(f"APS100 operation complete: {name}")
        self._update_buttons()

    def _on_2100_operation(self, name: str, success: bool, error: Any):
        self._append_activity(
            f"attoDRY2100 operation {'complete' if success else 'failed'}: {name}"
            + (f" ({error})" if not success and error else ""),
            "COMPLETE" if success else "ERROR",
        )
        if name == "setpoint":
            should_start = success and self._pending_2100_start
            self._pending_2100_start = False
            if should_start:
                self.magnet2100.start_field_control_async()
                self.state_label.setText("Starting field control…")
                return
            if not success:
                self._busy_2100 = False
                self._show_error(f"attoDRY2100 setpoint failed: {error}")
                self._update_buttons()
                return
        if name in {"start", "stop", "shutdown", "disconnect"}:
            self._busy_2100 = False
            self.state_label.setText("Complete" if success else "Faulted")
        if name in {"configure_temperature", "stop_temperature"}:
            self._temp_request_pending = False
        if not success:
            self._show_error(f"attoDRY2100 {name} failed: {error}")
        self._update_buttons()

    def _show_error(self, message: str):
        self.fault_label.setText(str(message))
        self.state_label.setText("Faulted")
        self.message_label.setText(str(message))
        self._append_activity(str(message), "ERROR")

    def _on_1000_error(self, message: str):
        self._clear_refresh_pending("1000")
        self._aps_connect_pending = False
        self._busy_1000 = False
        self._show_error(message)
        self._update_buttons()

    def _on_aps_exclusive_changed(self, active: bool, owner: str):
        self._aps_exclusive = bool(active)
        if active and owner:
            self.message_label.setText(f"APS100 reserved by {owner}")
            self._append_activity(f"APS100 reserved by {owner}", "RESERVATION")
        else:
            self._append_activity("APS100 reservation released", "RESERVATION")
        self._update_buttons()

    def _on_2100_error(self, message: str):
        # Telemetry/read errors must not release an active SETPOINT→START
        # guard.  Operation-terminal signals own mutation busy state.
        self._clear_refresh_pending("2100")
        self._temp_request_pending = False
        self._show_error(message)
        self._update_buttons()

    def _on_fault(self, message: str):
        self.fault_label.setText(str(message))
        self._append_activity(str(message), "FAULT")

    def _guard_message(self):
        self.message_label.setText("Review the configured safety limits before real hardware control")
        self.fault_label.setText("Commissioning review required")

    def _update_buttons(self):
        connected = self._connected[self._backend]
        busy = self._busy_1000 if self._backend == "1000" else self._busy_2100
        if self._backend == "1000":
            busy = busy or self._aps_exclusive
        self.move_button.setEnabled(connected and not busy)
        self.refresh_button.setEnabled(
            connected and self._refresh_pending_backend is None
        )
        self.disconnect_button.setEnabled(connected)
        self.connect_button.setEnabled(not connected and not busy)
        if self._backend == "1000":
            self.connect_button.setEnabled(
                not connected and not busy and not self._aps_connect_pending
            )
        self.stop_button.setEnabled(True)
        self.mode_combo.setEnabled(self._backend == "1000" and not busy)
        capable = bool(
            self._backend == "2100"
            and connected
            and getattr(self._last_capabilities, "sample_temperature_control", False)
        )
        temp_mutation_allowed = capable and not self._temp_request_pending
        self.sample_target.setEnabled(temp_mutation_allowed)
        self.sample_rate.setEnabled(temp_mutation_allowed)
        self.temp_control_button.setEnabled(temp_mutation_allowed)
        self.temp_stop_button.setEnabled(capable)
