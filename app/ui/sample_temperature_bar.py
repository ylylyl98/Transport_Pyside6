"""Compact shared attoDRY1000/Lake Shore sample-temperature control."""
from __future__ import annotations

import math
import time
from decimal import Decimal, InvalidOperation
from PySide6 import QtCore, QtWidgets

from app.ui.widgets.safe_spinbox import SafeDoubleSpinBox
from app.ui.widgets.safe_combo import SafeComboBox
from app.sample_heater_ranges import select_heater_range, validate_heater_ranges


class SampleTemperatureBar(QtWidgets.QFrame):
    """One LS335 sample control and readiness state for all measurements.

    The bar is deliberately independent of the attoDRY2100 controller.  It
    exposes a readiness query so measurement starts can gate on one common
    state instead of duplicating temperature controls in each tab.
    """
    readiness_changed = QtCore.Signal(bool)

    def __init__(self, controller, config, parent=None, *, clock=time.monotonic, save_config=None, compact=False):
        super().__init__(parent)
        self.controller, self.config = controller, config
        self._clock = clock
        self._save_config = save_config
        self._last_auto_info = None
        self._last_ready = None
        self._age_timer = QtCore.QTimer(self)
        self._age_timer.setInterval(250)
        self._age_timer.timeout.connect(self._on_age_timer)
        self._age_timer.start()
        self._backend = "2100"
        self._target = None
        self._snapshot = None
        self._stable_since = None
        self._wait = False  # Must match the initially unchecked checkbox.
        self._connected = bool(getattr(controller, "is_connected", False))
        self._pending_operation = None
        self._requested_target = None
        self._control_failed = False
        self._control_state = None
        self._controls_seen = False
        self._target_edited = False
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(8, 4, 8, 4)
        self.target = SafeDoubleSpinBox()
        self.target.setRange(float(getattr(config, "sample_minimum_temperature_k", 0.001)),
                             float(getattr(config, "sample_maximum_temperature_k", 300.0)))
        self.target.setDecimals(3)
        self.target.setSuffix(" K")
        self.target.setValue(4.2)
        self.target.valueChanged.connect(self._target_changed)
        self.heater_range = SafeComboBox()
        self.heater_range.addItem("Keep heater range", None)
        for label, value in (("Heater OFF", 0), ("Low", 1), ("Medium", 2), ("High", 3)):
            self.heater_range.addItem(label, value)
        self.heater_range.addItem("Auto by temperature", "auto")
        self.heater_range.setToolTip("Apply changes only the selected sample output; PID and wiring stay as commissioned.")
        self.ramp_mode = SafeComboBox()
        self.ramp_mode.addItem("Keep ramp", None)
        self.ramp_mode.addItem("Ramp OFF", False)
        self.ramp_mode.addItem("Ramp ON", True)
        self.ramp_rate = SafeDoubleSpinBox()
        self.ramp_rate.setRange(0.1, 100.0)
        self.ramp_rate.setDecimals(3)
        self.ramp_rate.setValue(1.0)
        self.ramp_rate.setSuffix(" K/min")
        self.ramp_rate.setToolTip("Setpoint ramp rate, for heating or cooling; cooling speed depends on the cryostat.")
        self.ramp_mode.currentIndexChanged.connect(lambda *_: self._update_controls())
        self.set_button = QtWidgets.QPushButton("Apply temperature")
        self.set_button.clicked.connect(self._set_target)
        self.current = QtWidgets.QLabel("—")
        self.wait_check = QtWidgets.QCheckBox("Wait until stable")
        self.wait_check.setChecked(False); self.wait_check.toggled.connect(self._wait_changed)
        self.off_button = QtWidgets.QPushButton("Heater Off")
        self.off_button.clicked.connect(self._heater_off)
        self.current.setWordWrap(True)
        self.control_status = QtWidgets.QLabel("Connect LS335 to read and control sample temperature")
        self.control_status.setWordWrap(True)
        self.auto_button = QtWidgets.QPushButton("Auto ranges…")
        self.auto_button.clicked.connect(self._edit_auto_ranges)
        if compact:
            controls = QtWidgets.QFormLayout()
            controls.setRowWrapPolicy(QtWidgets.QFormLayout.RowWrapPolicy.WrapLongRows)
            controls.setFieldGrowthPolicy(QtWidgets.QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
            controls.addRow("Sample target", self.target)
            controls.addRow("Heater range", self.heater_range)
            controls.addRow("Setpoint ramp", self.ramp_mode)
            controls.addRow("Ramp rate", self.ramp_rate)
            controls.addRow(self.set_button)
            controls.addRow(self.wait_check)
            controls.addRow(self.off_button)
            layout.addLayout(controls)
            layout.addWidget(self.current)
            layout.addWidget(self.control_status)
            layout.addWidget(self.auto_button)
        else:
            controls = QtWidgets.QHBoxLayout()
            controls.addWidget(QtWidgets.QLabel("Sample T (1000):"))
            for widget in (self.target, self.heater_range, self.ramp_mode, self.ramp_rate,
                           self.set_button, self.wait_check, self.off_button):
                controls.addWidget(widget)
            controls.addStretch()
            layout.addLayout(controls)
            status = QtWidgets.QHBoxLayout()
            status.addWidget(self.current)
            status.addWidget(self.control_status, 1)
            status.addWidget(self.auto_button)
            layout.addLayout(status)
        self.auto_preview = QtWidgets.QLabel()
        self.auto_preview.setWordWrap(True)
        layout.addWidget(self.auto_preview)
        self.heater_range.currentIndexChanged.connect(self._update_auto_preview)
        self.message = QtWidgets.QLabel()
        self.message.setWordWrap(True)
        layout.addWidget(self.message)
        controller.snapshot_updated.connect(self.on_snapshot)
        controller.control_result.connect(self._on_control_result)
        controller.error.connect(self._on_error)
        controller.fault.connect(self._on_fault)
        controller.disconnected.connect(self._on_disconnected)
        controller.connected.connect(self._on_connected)
        self._update_controls()
        self.set_backend("2100")

    def set_backend(self, backend):
        self._backend = str(backend or "")
        self.setVisible(self._backend == "1000")
        self._update_controls()
        self._update_ready()

    def _wait_changed(self, enabled):
        self._wait = bool(enabled); self._update_ready()

    def measurement_settings(self):
        target = self._target
        if self._pending_operation or self._control_failed or (self._controls_seen and self._control_state is None):
            target = None
        return self.wait_check.isChecked(), target

    def _set_target(self):
        if self._backend != "1000" or not self._connected or self._pending_operation:
            return
        self._requested_target = float(self.target.value())
        self._pending_operation = "sample_control"
        self._control_failed = False
        self._stable_since = None
        self.message.setText("Applying temperature settings; awaiting instrument readback")
        self._update_controls()
        self._update_ready()
        try:
            self.controller.set_sample_control(
                self._requested_target, heater_range=self.heater_range.currentData(),
                ramp_enabled=self.ramp_mode.currentData(),
                ramp_rate_k_per_min=self.ramp_rate.value(),
            )
        except Exception as exc:
            self._on_control_result({"operation": "sample_control", "ok": False, "error": str(exc)})

    def _target_changed(self, *_):
        self._target_edited = True
        self._update_auto_preview()

    def _edit_auto_ranges(self):
        from app.ui.sample_heater_range_dialog import SampleHeaterRangeDialog
        dialog = SampleHeaterRangeDialog(
            getattr(self.config, "sample_auto_heater_ranges", []), self._save_auto_ranges, self)
        dialog.exec()

    def _save_auto_ranges(self, bands):
        bands = validate_heater_ranges(bands) if bands else []
        previous = getattr(self.config, "sample_auto_heater_ranges", [])
        self.config.sample_auto_heater_ranges = bands
        try:
            if self._save_config is not None:
                self._save_config()
        except Exception:
            self.config.sample_auto_heater_ranges = previous
            raise
        self._last_auto_info = None
        self._update_auto_preview()

    def _update_auto_preview(self, *_):
        automatic = self.heater_range.currentData() == "auto"
        self.auto_preview.setVisible(automatic)
        if not automatic:
            return
        voltage = self._control_state is not None and self._control_state.get("output_type") == "voltage"
        labels = ("OFF", "ON" if voltage else "Low", "Medium", "High")
        bands = getattr(self.config, "sample_auto_heater_ranges", [])
        try:
            if bands:
                band = select_heater_range(self.target.value(), bands)
                value = band["heater_range"]
                if value > int(getattr(self.config, "sample_maximum_heater_range", 3)) or (voltage and value > 1):
                    raise ValueError("Selected automatic range exceeds the permitted output range")
                text = f"Auto: target {self.target.value():g} K → {labels[value]} (band ≤ {band['upper_temperature_k']:g} K)"
            elif self._last_auto_info and self._last_auto_info["setpoint"] == self.target.value():
                info = self._last_auto_info
                suffix = "; native Zone control" if info.get("auto_native_zone") else ""
                text = f"Auto: LS335 band ≤ {info['auto_upper_temperature_k']:g} K → {labels[info['auto_target_range']]}{suffix}"
            else:
                text = "Auto: use commissioned LS335 Zone table; range resolved when Apply temperature is clicked"
            self.auto_preview.setText(text)
        except ValueError as exc:
            self.auto_preview.setText(f"Auto unavailable: {exc}")

    def _heater_off(self):
        if self._backend != "1000" or not self._connected or self._pending_operation == "heater_off":
            return
        self._pending_operation = "heater_off"
        self._stable_since = None
        self.message.setText("Requesting heater OFF; awaiting readback")
        self._update_controls()
        self._update_ready()
        try:
            self.controller.heater_off()
        except Exception as exc:
            self._on_control_result({"operation": "heater_off", "ok": False, "error": str(exc)})

    def _on_connected(self, *_):
        self._connected = True
        self._update_controls()

    def _update_controls(self):
        selected = self._backend == "1000"
        available = selected and self._connected and not self._pending_operation
        for widget in (self.target, self.heater_range, self.ramp_mode, self.set_button):
            widget.setEnabled(available)
        self.ramp_rate.setEnabled(available and self.ramp_mode.currentData() is not None)
        # OFF remains available while an apply request is being processed.
        self.off_button.setEnabled(selected and self._connected and self._pending_operation != "heater_off")
        self.auto_button.setEnabled(selected and not self._pending_operation)
        voltage = self._control_state is not None and self._control_state.get("output_type") == "voltage"
        maximum = int(getattr(self.config, "sample_maximum_heater_range", 3))
        for index in range(1, self.heater_range.count()):
            value = self.heater_range.itemData(index)
            if value == "auto":
                continue
            self.heater_range.model().item(index).setEnabled(value <= maximum and (not voltage or value <= 1))
        self.heater_range.setItemText(2, "Heater ON" if voltage else "Low")

    def on_snapshot(self, snapshot):
        self._snapshot = snapshot
        value = getattr(snapshot, "sample_temperature_k", None)
        valid, _reason = self._valid_sample_snapshot(snapshot)
        tolerance = float(getattr(self.config, "sample_stability_tolerance_k", 0.05))
        if valid and not self._pending_operation and not self._control_failed and self._target is not None and abs(float(value) - self._target) <= tolerance:
            if self._stable_since is None:
                self._stable_since = self._clock()
            dwell = float(getattr(self.config, "sample_stability_dwell_s", 5.0))
            state = "Stable" if self._clock() - self._stable_since >= dwell else "Settling"
            self._set_status(f"{float(value):.3f} K — {state}")
        else:
            self._stable_since = None
            try:
                state = "No target set" if self._target is None else "Not stable"
                display = "—" if value is None else f"{float(value):.3f} K — {state}"
            except (TypeError, ValueError):
                display = "Invalid sample-temperature reading"
            self._set_status(display)
        self._update_ready()

    def _on_fault(self, message):
        self._snapshot = None
        self._stable_since = None
        self._set_status(f"Fault: {message}")
        self._update_ready()

    def _on_error(self, message):
        self.message.setText(f"Fault: {message}")

    def _on_disconnected(self):
        self._last_auto_info = None
        self._connected = False
        self._pending_operation = None
        self._target = None
        self._control_state = None
        self._controls_seen = False
        self._snapshot = None
        self._stable_since = None
        self._set_status("Lake Shore disconnected")
        self.control_status.setText("Heater state unknown — disconnected")
        self._update_controls()
        self._update_auto_preview()
        self._update_ready()

    @staticmethod
    def _status_is_clear(status):
        try:
            value = Decimal(str(status).strip())
        except (InvalidOperation, ValueError):
            return False
        return value.is_finite() and value == 0

    def _valid_sample_snapshot(self, snapshot):
        if snapshot is None:
            return False, "unavailable"
        if not (getattr(snapshot, "connected", False)
                and getattr(snapshot, "communication_valid", False)):
            return False, "communication fault"
        value = getattr(snapshot, "sample_temperature_k", None)
        try:
            value = float(value)
        except (TypeError, ValueError):
            return False, "invalid temperature"
        if not math.isfinite(value) or value <= 0:
            return False, "invalid temperature"
        if not self._status_is_clear(getattr(snapshot, "sample_sensor_status", None)):
            return False, "sensor fault"
        try:
            acquired = float(getattr(snapshot, "monotonic_s"))
            now = float(self._clock())
            age = now - acquired
            maximum_age = float(self.config.maximum_reading_age_s)
        except (AttributeError, TypeError, ValueError):
            return False, "invalid timing"
        if (not math.isfinite(acquired) or not math.isfinite(now)
                or not math.isfinite(maximum_age) or maximum_age <= 0
                or age < 0 or age > maximum_age):
            return False, "stale telemetry"
        return True, "valid"

    def _on_age_timer(self):
        valid, reason = self._valid_sample_snapshot(self._snapshot)
        if not valid and self._stable_since is not None:
            self._stable_since = None
            self._set_status(f"Not ready — {reason}")
        self._update_ready()

    def _on_control_result(self, result):
        operation = result.get("operation")
        if operation not in {"heater_off", "setpoint", "sample_control", "read_control"}:
            return
        self._controls_seen = True
        if operation == self._pending_operation:
            self._pending_operation = None
        if not result.get("ok"):
            self._stable_since = None
            self._control_state = None
            if operation != "read_control":
                self._control_failed = True
                self.message.setText(f"{operation.replace('_', ' ')} failed: {result.get('error', 'unconfirmed')}")
            self.control_status.setText(f"Heater state unknown: {result.get('error', 'readback failed')}")
        elif operation == "heater_off":
            self._control_failed = False
            self._stable_since = None
            self.control_status.setText(f"Output {result['output']}: Heater OFF — confirmed")
            self.message.setText("Heater OFF — confirmed")
            # Force the next periodic readback to refresh the complete state.
            self._control_state = None
        else:
            self._control_state = result
            if "auto_range_source" in result:
                self._last_auto_info = result
            setpoint = float(result["setpoint"])
            if not self._pending_operation and (operation != "read_control" or not self._control_failed):
                if self._target != setpoint:
                    self._stable_since = None
                self._target = setpoint
            if operation != "read_control":
                self._control_failed = False
                self._stable_since = None
                self.message.setText(self._target_notice(setpoint))
            if not self._target_edited:
                self.target.blockSignals(True)
                self.target.setValue(setpoint)
                self.target.blockSignals(False)
            heater_range = int(float(result.get("range", 0)))
            voltage = result.get("output_type") == "voltage"
            range_label = ("OFF", "ON" if voltage else "Low", "Medium", "High")[heater_range]
            ramp = (f"Ramp {result['ramp_rate_k_per_min']:g} K/min" if result.get("ramp_enabled") else "Ramp OFF")
            power = f"; output {result['heater_output']:.1f}%" if "heater_output" in result else ""
            self.control_status.setText(f"Output {result['output']}: Heater {range_label}{power}; target {setpoint:.3f} K; {ramp}")
        self._update_controls()
        self._update_auto_preview()
        self._update_ready()

    def _target_notice(self, target):
        warning = getattr(self.config, "sample_warning_temperature_k", None)
        if warning is not None and target >= float(warning):
            return (f"Target confirmed. Sample ≥ {float(warning):g} K blocks magnet workflows under the current thermal limits.")
        return "Target confirmed; waiting for stable sample readings"

    def _set_status(self, text):
        self.current.setText(str(text))

    def summary_text(self):
        """Read-only display from cached, fresh telemetry and confirmed settings."""
        if not self._connected:
            return "Disconnected"
        valid, reason = self._valid_sample_snapshot(self._snapshot)
        sample = f"{float(self._snapshot.sample_temperature_k):.3f} K" if valid else f"Unavailable ({reason})"
        _, target = self.measurement_settings()
        target_text = "unconfirmed" if target is None else f"{target:.3f} K"
        if self._pending_operation:
            state = "Heater OFF pending" if self._pending_operation == "heater_off" else "Applying settings"
        elif self._control_failed:
            state = "Control failed"
        elif self._wait:
            state = "Ready for measurement" if self.is_ready() else "Waiting for stability"
        else:
            state = "Stability wait off"
        return f"Sample: {sample} · target: {target_text} · {state}"

    def _update_ready(self):
        ready = self.is_ready()
        if ready != self._last_ready:
            self._last_ready = ready
            self.readiness_changed.emit(ready)

    def is_ready(self):
        if self._backend != "1000" or not self._wait: return True
        if self._pending_operation or self._control_failed: return False
        if self._controls_seen and self._control_state is None: return False
        if self._target is None or self._snapshot is None: return False
        valid, _reason = self._valid_sample_snapshot(self._snapshot)
        if not valid:
            return False
        value = getattr(self._snapshot, "sample_temperature_k", None)
        dwell = float(getattr(self.config, "sample_stability_dwell_s", 5.0))
        tolerance = float(getattr(self.config, "sample_stability_tolerance_k", 0.05))
        return bool(abs(float(value) - self._target) <= tolerance
                    and self._stable_since is not None
                    and self._clock() - self._stable_since >= dwell)
