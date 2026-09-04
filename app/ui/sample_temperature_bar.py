"""Compact shared attoDRY1000/Lake Shore sample-temperature control."""
from __future__ import annotations

import math
import time
from decimal import Decimal, InvalidOperation
from PyQt6 import QtCore, QtWidgets

from app.ui.widgets.safe_spinbox import SafeDoubleSpinBox


class SampleTemperatureBar(QtWidgets.QFrame):
    """Small persistent control shared by every measurement tab.

    The bar is deliberately independent of the attoDRY2100 controller.  It
    exposes a readiness query so measurement starts can gate on one common
    state instead of duplicating temperature controls in each tab.
    """
    readiness_changed = QtCore.pyqtSignal(bool)

    def __init__(self, controller, config, parent=None, *, clock=time.monotonic):
        super().__init__(parent)
        self.controller, self.config = controller, config
        self._clock = clock
        self._last_ready = None
        self._age_timer = QtCore.QTimer(self)
        self._age_timer.setInterval(250)
        self._age_timer.timeout.connect(self._on_age_timer)
        self._age_timer.start()
        self._backend = "2100"
        self._target = None
        self._snapshot = None
        self._stable_since = None
        self._wait = True
        layout = QtWidgets.QHBoxLayout(self)
        layout.setContentsMargins(8, 4, 8, 4)
        layout.addWidget(QtWidgets.QLabel("Sample T (1000):"))
        self.target = SafeDoubleSpinBox()
        self.target.setRange(0.0, 400.0); self.target.setDecimals(3); self.target.setSuffix(" K")
        self.target.setValue(4.2)
        layout.addWidget(self.target)
        self.set_button = QtWidgets.QPushButton("Set")
        self.set_button.clicked.connect(self._set_target)
        layout.addWidget(self.set_button)
        self.current = QtWidgets.QLabel("—")
        layout.addWidget(self.current)
        self.wait_check = QtWidgets.QCheckBox("Wait until stable")
        self.wait_check.setChecked(False); self.wait_check.toggled.connect(self._wait_changed)
        layout.addWidget(self.wait_check)
        self.off_button = QtWidgets.QPushButton("Heater Off")
        self.off_button.clicked.connect(controller.heater_off)
        layout.addWidget(self.off_button)
        layout.addStretch()
        controller.snapshot_updated.connect(self.on_snapshot)
        controller.control_result.connect(self._on_control_result)
        controller.error.connect(lambda message: self._set_status(f"Fault: {message}"))
        controller.fault.connect(self._on_fault)
        controller.disconnected.connect(self._on_disconnected)
        self.set_backend("2100")

    def set_backend(self, backend):
        self._backend = str(backend or "")
        self.setVisible(self._backend == "1000")
        self._update_ready()

    def _wait_changed(self, enabled):
        self._wait = bool(enabled); self._update_ready()

    def _set_target(self):
        self._target = float(self.target.value())
        self._stable_since = None
        self.controller.set_sample_setpoint(self._target)
        self._set_status(f"Target {self._target:.3f} K; waiting")
        self._update_ready()

    def on_snapshot(self, snapshot):
        self._snapshot = snapshot
        value = getattr(snapshot, "sample_temperature_k", None)
        valid, _reason = self._valid_sample_snapshot(snapshot)
        tolerance = float(getattr(self.config, "sample_stability_tolerance_k", 0.05))
        if valid and self._target is not None and abs(float(value) - self._target) <= tolerance:
            if self._stable_since is None:
                self._stable_since = self._clock()
            dwell = float(getattr(self.config, "sample_stability_dwell_s", 5.0))
            state = "Stable" if self._clock() - self._stable_since >= dwell else "Settling"
            self._set_status(f"{float(value):.3f} K — {state}")
        else:
            self._stable_since = None
            try:
                display = "—" if value is None else f"{float(value):.3f} K — Not stable"
            except (TypeError, ValueError):
                display = "Invalid sample-temperature reading"
            self._set_status(display)
        self._update_ready()

    def _on_fault(self, message):
        self._snapshot = None
        self._stable_since = None
        self._set_status(f"Fault: {message}")
        self._update_ready()

    def _on_disconnected(self):
        self._snapshot = None
        self._stable_since = None
        self._set_status("Lake Shore disconnected")
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
        if not math.isfinite(value):
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
        if result.get("operation") == "heater_off" and result.get("ok"):
            self._set_status("Heater OFF — confirmed")
        elif result.get("operation") == "setpoint" and result.get("ok"):
            range_value = str(result.get("range", "")).strip().upper()
            if range_value in {"0", "0.0", "OFF"}:
                self._set_status(f"Target {result['setpoint']:.3f} K; heater range OFF")
            else:
                self._set_status(f"Target {result['setpoint']:.3f} K; waiting")

    def _set_status(self, text):
        self.current.setText(str(text))

    def _update_ready(self):
        ready = self.is_ready()
        if ready != self._last_ready:
            self._last_ready = ready
            self.readiness_changed.emit(ready)

    def is_ready(self):
        if self._backend != "1000" or not self._wait: return True
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
