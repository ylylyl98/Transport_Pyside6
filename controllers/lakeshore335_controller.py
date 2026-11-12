"""Threaded controller for Lake Shore Model 335 telemetry and sample setpoint."""

from __future__ import annotations

import threading
import math
import traceback
from PySide6.QtCore import QObject, QMetaObject, QThread, QTimer, Qt, Signal, Slot

from app.devices.lakeshore335_adapter import LakeShore335Adapter, MockLakeShore335Adapter, LakeShore335Snapshot
import time
from utils.config import cfg
from app.sample_heating_interlock import SampleHeatingInterlock




class _LakeShoreWorker(QObject):
    connected = Signal(object)
    disconnected = Signal()
    snapshot_updated = Signal(object)
    error = Signal(str)
    fault = Signal(str)
    control_result = Signal(object)

    def __init__(self, interlock=None):
        super().__init__()
        self.heating_interlock = interlock or SampleHeatingInterlock()
        self.adapter = None
        self._last_control_poll = None
        self._timer = QTimer(self)
        self._timer.timeout.connect(self.refresh_snapshot)

    @Slot(str, bool)
    def connect_instrument(self, resource, use_mock=False):
        self.disconnect_instrument()
        try:
            options = dict(resource_name=resource)
            self.adapter = MockLakeShore335Adapter(**options) if use_mock else LakeShore335Adapter(**options)
            identity = self.adapter.connect()
            self.connected.emit(identity)
            self.refresh_snapshot()
            self._timer.setInterval(max(100, int(float(cfg.lakeshore335.polling_interval_s) * 1000)))
            self._timer.start()
        except Exception as exc:
            self.adapter = None
            self.error.emit(f"Lake Shore 335 connection failed: {exc}\n{traceback.format_exc()}")

    @Slot()
    def disconnect_instrument(self):
        self.heating_interlock.invalidate_control()
        self._timer.stop()
        self._last_control_poll = None
        adapter, self.adapter = self.adapter, None
        if adapter is not None:
            adapter.close()
        self.disconnected.emit()

    @Slot()
    def refresh_snapshot(self):
        if self.adapter is None:
            return
        try:
            snapshot = self.adapter.read_snapshot(
                cfg.lakeshore335.sample_channel, cfg.lakeshore335.reservoir_channel
            )
            self.snapshot_updated.emit(snapshot)
        except Exception as exc:
            # Publish an explicit invalid snapshot so an earlier fresh SAFE
            # reading cannot authorize a move during a communication fault.
            invalid = LakeShore335Snapshot(
                None, None, "COMMUNICATION_FAULT", "COMMUNICATION_FAULT",
                time.time(), time.monotonic(), False, False,
                getattr(self.adapter, "identity", None), None, None, str(exc),
            )
            self.snapshot_updated.emit(invalid)
            self.heating_interlock.invalidate_control()
            self._enforce_heating_interlock()
            self.fault.emit(f"Lake Shore 335 telemetry unavailable: {exc}")
            return
        interval = max(0.5, float(cfg.lakeshore335.control_polling_interval_s))
        if self._last_control_poll is None or time.monotonic() - self._last_control_poll >= interval:
            self.refresh_control_configuration()

    @Slot()
    def refresh_control_configuration(self):
        self._last_control_poll = time.monotonic()
        try:
            if self.adapter is None:
                raise RuntimeError("Lake Shore 335 is not connected")
            state = self.adapter.read_control_configuration(cfg.lakeshore335.sample_channel)
            self.heating_interlock.note_control(state)
            self.control_result.emit({"operation": "read_control", "ok": True, **state})
            self._enforce_heating_interlock()
        except Exception as exc:
            self.heating_interlock.invalidate_control()
            # Control mapping/configuration failure is distinct from valid
            # sample/reservoir telemetry and must not disarm thermal protection.
            self.control_result.emit({"operation": "read_control", "ok": False, "error": str(exc)})
            self._enforce_heating_interlock()
        # Input readout failures are reported independently of heater safety.
        self.refresh_input_configurations()

    def _enforce_heating_interlock(self):
        """Cut only the LS335 sample output on unsafe/unknown APS observations.

        Polling catches external changes too. OFF remains possible without a
        magnet connection and never operates the APS persistent-switch heater.
        """
        reason = self.heating_interlock.heating_block_reason()
        if reason and self.heating_interlock.magnet_block_reason():
            self.heater_off()
            self.fault.emit(f"Sample heating interlock: {reason}; sample Heater OFF requested")

    @Slot(bool)
    def set_polling_enabled(self, enabled):
        if enabled and self.adapter is not None:
            self._timer.start()
        else:
            self._timer.stop()

    @Slot(float)
    def set_sample_setpoint(self, target):
        if self.adapter is None:
            self.control_result.emit({"operation": "setpoint", "ok": False, "error": "Not connected"})
            self.error.emit("Lake Shore 335 is not connected")
            return
        reserved = False
        try:
            config = cfg.lakeshore335
            if not config.verified_channel_mapping:
                raise RuntimeError("Sample/reservoir channel mapping must be verified")
            self.heating_interlock.begin_sample_control()
            reserved = True
            result = self.adapter.set_sample_control(target, config.sample_channel,
                minimum_temperature_k=config.sample_minimum_temperature_k,
                maximum_temperature_k=config.sample_maximum_temperature_k,
                maximum_heater_range=config.sample_maximum_heater_range)
            self.heating_interlock.note_control(result)
            reason = self.heating_interlock.heating_block_reason()
            if reason:
                self.heater_off()
                raise RuntimeError(reason)
            self.control_result.emit({"operation": "setpoint", "ok": True, **result})
        except Exception as exc:
            if reserved:
                self.heating_interlock.invalidate_control()
            self.control_result.emit({"operation": "setpoint", "ok": False, "error": str(exc)})
            self.error.emit(f"Lake Shore setpoint failed: {exc}")
        finally:
            if reserved:
                self.heating_interlock.end_sample_control()

    @Slot(object)
    def set_sample_control(self, settings):
        reserved = False
        try:
            if self.adapter is None:
                raise RuntimeError("Lake Shore 335 is not connected")
            config = cfg.lakeshore335
            if not config.verified_channel_mapping:
                raise RuntimeError("Sample/reservoir channel mapping must be verified")
            self.heating_interlock.begin_sample_control()
            reserved = True
            result = self.adapter.set_sample_control(
                settings["temperature_k"], config.sample_channel,
                heater_range=settings.get("heater_range"),
                ramp_enabled=settings.get("ramp_enabled"),
                ramp_rate_k_per_min=settings.get("ramp_rate_k_per_min"),
                minimum_temperature_k=config.sample_minimum_temperature_k,
                maximum_temperature_k=config.sample_maximum_temperature_k,
                maximum_heater_range=config.sample_maximum_heater_range,
                auto_heater_ranges=config.sample_auto_heater_ranges,
            )
            self.heating_interlock.note_control(result)
            reason = self.heating_interlock.heating_block_reason()
            if reason:
                self.heater_off()
                raise RuntimeError(reason)
            self.control_result.emit({"operation": "sample_control", "ok": True, **result})
        except Exception as exc:
            if reserved:
                self.heating_interlock.invalidate_control()
            self.control_result.emit({"operation": "sample_control", "ok": False, "error": str(exc)})
            self.error.emit(f"Lake Shore sample control failed: {exc}")
        finally:
            if reserved:
                self.heating_interlock.end_sample_control()

    @Slot()
    def refresh_input_configurations(self):
        try:
            if self.adapter is None:
                raise RuntimeError("Lake Shore 335 is not connected")
            state = self.adapter.read_input_configurations()
            self.control_result.emit({"operation": "read_input_configurations", "ok": True,
                                      "monotonic_s": time.monotonic(), **state})
        except Exception as exc:
            self.control_result.emit({"operation": "read_input_configurations", "ok": False, "error": str(exc)})

    @Slot(object)
    def set_current_reversal(self, request):
        reserved = False
        try:
            if self.adapter is None:
                raise RuntimeError("Lake Shore 335 is not connected")
            self.heating_interlock.begin_input_configuration()
            reserved = True
            state = self.adapter.set_current_reversal(request["channel"], request["enabled"])
            inputs = self.adapter.read_input_configurations()["inputs"]
            self.control_result.emit({"operation": "set_current_reversal", "ok": True,
                                      "channel": state["channel"], "inputs": inputs,
                                      "monotonic_s": time.monotonic()})
        except Exception as exc:
            self.control_result.emit({"operation": "set_current_reversal", "ok": False, "error": str(exc)})
        finally:
            if reserved:
                self.heating_interlock.end_sample_control()
                self.refresh_control_configuration()
                # Publish a fresh measurement after the operation result so
                # consumers can invalidate old-mode data before accepting it.
                self.refresh_snapshot()

    @Slot(object)
    def set_measurement_sample_reversal(self, request):
        """Owner-only sample input switch while APS output is stationary."""
        owner, request_id = request["owner"], request["request_id"]
        reserved = False
        result = {"operation": "measurement_current_reversal", "owner": owner,
                  "request_id": request_id, "ok": False}
        try:
            if self.adapter is None:
                raise RuntimeError("Lake Shore 335 is not connected")
            config = cfg.lakeshore335
            if (not config.verified_channel_mapping or config.sample_channel != "B"
                    or config.reservoir_channel != "A"):
                raise RuntimeError("Verified sample B and reservoir A mapping is required")
            self.heating_interlock.begin_measurement_input_change(owner)
            reserved = True
            self.snapshot_updated.emit(self.adapter.read_snapshot(config.sample_channel, config.reservoir_channel))
            before = self.adapter.read_input_configurations()["inputs"]
            a, b = before["A"], before["B"]
            if not a.get("supported") or a.get("current_reversal") is not True:
                raise RuntimeError("Reservoir A current reversal must be ON")
            if not b.get("supported"):
                raise RuntimeError("Sample B must be a PTC or NTC RTD input")
            self.heating_interlock.check_measurement_input_change(owner)
            self.adapter.set_current_reversal("B", request["enabled"])
            self.snapshot_updated.emit(self.adapter.read_snapshot(config.sample_channel, config.reservoir_channel))
            after = self.adapter.read_input_configurations()["inputs"]
            a_after, b_after = after["A"], after["B"]
            if (not a_after.get("supported") or a_after.get("current_reversal") is not True
                    or any(a_after.get(k) != a.get(k) for k in ("sensor_type", "autorange", "units", "compensation"))
                    or (not a.get("autorange") and a_after.get("range") != a.get("range"))):
                raise RuntimeError("Reservoir A input configuration changed or is not ON")
            if b_after.get("current_reversal") is not request["enabled"]:
                raise RuntimeError("Sample B current reversal readback mismatch")
            result.update(ok=True, inputs=after)
        except Exception as exc:
            result["error"] = str(exc)
        finally:
            if reserved:
                self.heating_interlock.end_sample_control()
                # Re-establish verified heater-OFF control before the engine may
                # command magnet motion. This also publishes input readbacks.
                self.refresh_control_configuration()
            result["monotonic_s"] = time.monotonic()
            self.control_result.emit(result)
            if reserved:
                self.refresh_snapshot()

    @Slot()
    def heater_off(self):
        self.heating_interlock.invalidate_control()
        if self.adapter is None:
            self.control_result.emit({"operation": "heater_off", "ok": False, "error": "Not connected; OFF unconfirmed"})
            self.error.emit("Lake Shore 335 is not connected; heater-off is unconfirmed")
            return
        try:
            result = self.adapter.heater_off(cfg.lakeshore335.sample_channel)
            # RANGE OFF confirmation remains useful even if other configuration
            # queries fail; magnet starts additionally need a full fresh readback.
            try:
                state = self.adapter.read_control_configuration(cfg.lakeshore335.sample_channel)
                self.heating_interlock.note_control(state)
            except Exception:
                self.heating_interlock.invalidate_control()
            self.control_result.emit({"operation": "heater_off", "ok": True, **result})
        except Exception as exc:
            self.control_result.emit({"operation": "heater_off", "ok": False, "error": str(exc)})
            self.error.emit(f"Lake Shore heater-off failed (unconfirmed): {exc}")

    @Slot()
    def shutdown(self):
        self.disconnect_instrument()


class LakeShore335Controller(QObject):
    connected = Signal(object)
    disconnected = Signal()
    snapshot_updated = Signal(object)
    error = Signal(str)
    fault = Signal(str)
    control_result = Signal(object)
    input_configuration_pending_changed = Signal(bool)

    _connect_requested = Signal(str, bool)
    _disconnect_requested = Signal()
    _refresh_requested = Signal()
    _polling_requested = Signal(bool)
    _setpoint_requested = Signal(float)
    _sample_control_requested = Signal(object)
    _control_refresh_requested = Signal()
    _heater_off_requested = Signal()
    _input_refresh_requested = Signal()
    _current_reversal_requested = Signal(object)
    _measurement_current_reversal_requested = Signal(object)

    def __init__(self, parent=None, *, interlock=None):
        super().__init__(parent)
        self._thread = QThread(self)
        self._worker = _LakeShoreWorker(interlock)
        self.heating_interlock = self._worker.heating_interlock
        self._input_pending_lock = threading.Lock()
        self._input_configuration_pending = False
        self._input_temperature_cutoff = None
        self._input_configurations = None
        self._input_configurations_monotonic_s = None
        self._measurement_original_reversal = None
        self._measurement_confirmed_reversal = None
        from app.engine.transport_tasks import LatestTelemetry
        self.telemetry_cache = LatestTelemetry()
        self._worker.snapshot_updated.connect(self._cache_valid_snapshot, Qt.ConnectionType.DirectConnection)
        self._worker.control_result.connect(self._record_input_worker_result, Qt.ConnectionType.DirectConnection)
        self._worker.disconnected.connect(self.telemetry_cache.clear, Qt.ConnectionType.DirectConnection)
        self._worker.moveToThread(self._thread)
        self._connect_requested.connect(self._worker.connect_instrument, Qt.ConnectionType.QueuedConnection)
        self._disconnect_requested.connect(self._worker.disconnect_instrument, Qt.ConnectionType.QueuedConnection)
        self._refresh_requested.connect(self._worker.refresh_snapshot, Qt.ConnectionType.QueuedConnection)
        self._polling_requested.connect(self._worker.set_polling_enabled, Qt.ConnectionType.QueuedConnection)
        self._setpoint_requested.connect(self._worker.set_sample_setpoint, Qt.ConnectionType.QueuedConnection)
        self._sample_control_requested.connect(self._worker.set_sample_control, Qt.ConnectionType.QueuedConnection)
        self._control_refresh_requested.connect(self._worker.refresh_control_configuration, Qt.ConnectionType.QueuedConnection)
        self._heater_off_requested.connect(self._worker.heater_off, Qt.ConnectionType.QueuedConnection)
        self._input_refresh_requested.connect(self._worker.refresh_input_configurations, Qt.ConnectionType.QueuedConnection)
        self._current_reversal_requested.connect(self._worker.set_current_reversal, Qt.ConnectionType.QueuedConnection)
        self._measurement_current_reversal_requested.connect(self._worker.set_measurement_sample_reversal, Qt.ConnectionType.QueuedConnection)
        self._worker.connected.connect(self.connected)
        self._worker.disconnected.connect(self._on_disconnected)
        self._last_delivered_snapshot = None
        self._worker.snapshot_updated.connect(self._deliver_latest_snapshot)
        self._worker.error.connect(self.error)
        self._worker.fault.connect(self.fault)
        self._worker.control_result.connect(self._on_control_result)
        self._thread.start()

    def _record_input_worker_result(self, result):
        """Fence old samples synchronously on the worker before later reads."""
        if isinstance(result, dict) and result.get("operation") == "set_current_reversal":
            with self._input_pending_lock:
                self._input_temperature_cutoff = time.monotonic()
                self.telemetry_cache.clear()

    def _cache_valid_snapshot(self, snapshot):
        with self._input_pending_lock:
            cutoff = self._input_temperature_cutoff
            if cutoff is not None:
                stamp = getattr(snapshot, "monotonic_s", None)
                if not isinstance(stamp, (int, float)) or not math.isfinite(stamp) or stamp <= cutoff:
                    return
                self._input_temperature_cutoff = None
            self.telemetry_cache.publish(snapshot)

    def _on_control_result(self, result):
        if isinstance(result, dict):
            operation = result.get("operation")
            if operation in {"read_input_configurations", "set_current_reversal", "measurement_current_reversal"}:
                if result.get("ok") and isinstance(result.get("inputs"), dict):
                    self._input_configurations = {k: dict(v) for k, v in result["inputs"].items()}
                    self._input_configurations_monotonic_s = result.get("monotonic_s")
                else:
                    self._input_configurations = None
                    self._input_configurations_monotonic_s = None
            if operation in {"set_current_reversal", "measurement_current_reversal"}:
                with self._input_pending_lock:
                    if operation == "measurement_current_reversal":
                        b = result.get("inputs", {}).get("B", {}) if result.get("ok") else {}
                        value = b.get("current_reversal")
                        self._measurement_confirmed_reversal = value if isinstance(value, bool) else None
                    self._input_configuration_pending = False
                if operation == "set_current_reversal":
                    self.input_configuration_pending_changed.emit(False)
        self.control_result.emit(result)

    def _on_disconnected(self):
        self._input_configurations = None
        self._input_configurations_monotonic_s = None
        self.disconnected.emit()

    @property
    def input_configurations(self):
        return None if self._input_configurations is None else {k: dict(v) for k, v in self._input_configurations.items()}

    @property
    def input_configurations_monotonic_s(self):
        return self._input_configurations_monotonic_s

    @property
    def input_configuration_pending(self):
        with self._input_pending_lock:
            return self._input_configuration_pending

    def refresh_input_configurations(self):
        self._input_refresh_requested.emit()

    def set_current_reversal(self, channel, enabled):
        channel = str(channel).strip().upper()
        if channel not in {"A", "B"} or not isinstance(enabled, bool):
            raise ValueError("Current reversal requires input A/B and a boolean")
        with self._input_pending_lock:
            if self._input_configuration_pending or self.heating_interlock.measurement_input_session_active():
                return False
            self._input_configuration_pending = True
            self._input_temperature_cutoff = float("inf")
            self.telemetry_cache.clear()
        self.input_configuration_pending_changed.emit(True)
        try:
            self._current_reversal_requested.emit({"channel": channel, "enabled": enabled})
        except Exception:
            with self._input_pending_lock:
                self._input_configuration_pending = False
                self._input_temperature_cutoff = time.monotonic()
            self.input_configuration_pending_changed.emit(False)
            raise
        return True

    def begin_measurement_input_session(self, owner):
        if not self.is_connected:
            raise RuntimeError("Lake Shore 335 is not connected")
        config = cfg.lakeshore335
        if (not config.verified_channel_mapping or config.sample_channel != "B"
                or config.reservoir_channel != "A"):
            raise RuntimeError("Verified sample B and reservoir A mapping is required")
        with self._input_pending_lock:
            if self._input_configuration_pending:
                raise RuntimeError("LS335 input configuration is pending")
            inputs = self.input_configurations
            stamp = self._input_configurations_monotonic_s
            if (not isinstance(stamp, (int, float)) or not math.isfinite(stamp)
                    or not 0 <= time.monotonic() - stamp <= max(10., 3 * cfg.lakeshore335.control_polling_interval_s)
                    or not isinstance(inputs, dict)):
                raise RuntimeError("Fresh LS335 input configuration is required")
            a, b = inputs.get("A", {}), inputs.get("B", {})
            if not a.get("supported") or a.get("current_reversal") is not True or not b.get("supported"):
                raise RuntimeError("Reservoir A reversal must be ON and B must be an RTD")
            self.heating_interlock.begin_measurement_input_session(owner)
            self._measurement_original_reversal = b.get("current_reversal")
            self._measurement_confirmed_reversal = b.get("current_reversal")
            return inputs

    def set_measurement_sample_reversal(self, owner, enabled, request_id):
        if not isinstance(enabled, bool) or not isinstance(request_id, str) or not request_id:
            raise ValueError("Measurement reversal requires boolean and request ID")
        with self._input_pending_lock:
            if self._input_configuration_pending or not self.heating_interlock.measurement_input_session_owned(owner):
                return False
            self._input_configuration_pending = True
        # Measurement owns the APS reservation and handles its own mode fence.
        # Do not emit the manual-input signal, whose listeners clear both
        # channels' thermal-protection cache.
        try:
            self._measurement_current_reversal_requested.emit({"owner": str(owner),
                "enabled": enabled, "request_id": request_id})
        except Exception:
            with self._input_pending_lock:
                self._input_configuration_pending = False
            raise
        return True

    def end_measurement_input_session(self, owner):
        with self._input_pending_lock:
            if self._input_configuration_pending:
                raise RuntimeError("LS335 input change is still pending")
            if (not isinstance(self._measurement_original_reversal, bool)
                    or self._measurement_confirmed_reversal is not self._measurement_original_reversal):
                raise RuntimeError("Original sample B current reversal is not confirmed restored")
            self.heating_interlock.end_measurement_input_session(owner)
            self._measurement_original_reversal = None
            self._measurement_confirmed_reversal = None
        return True

    @property
    def adapter(self):
        return self._worker.adapter

    def _deliver_latest_snapshot(self, _queued_snapshot):
        snapshot = self.telemetry_cache.get()
        if snapshot is not None and snapshot is not self._last_delivered_snapshot:
            self._last_delivered_snapshot = snapshot
            self.snapshot_updated.emit(snapshot)

    @property
    def is_connected(self):
        return self.adapter is not None and bool(getattr(self.adapter, "connected", False))

    def connect_instrument(self, resource=None, use_mock=False):
        selected = str(resource or cfg.lakeshore335.visa_resource).strip()
        cfg.lakeshore335.visa_resource = selected
        self._connect_requested.emit(selected, bool(use_mock))

    connect_async = connect_instrument

    def disconnect_instrument(self):
        self._disconnect_requested.emit()

    disconnect_async = disconnect_instrument

    def refresh_snapshot(self):
        self._refresh_requested.emit()

    read_snapshot_async = refresh_snapshot

    def set_polling_enabled(self, enabled):
        self._polling_requested.emit(bool(enabled))

    def set_sample_setpoint(self, target_temperature_k: float):
        self._setpoint_requested.emit(float(target_temperature_k))

    def set_sample_control(self, target_temperature_k, *, heater_range=None,
                           ramp_enabled=None, ramp_rate_k_per_min=None):
        self._sample_control_requested.emit({"temperature_k": float(target_temperature_k),
            "heater_range": heater_range, "ramp_enabled": ramp_enabled,
            "ramp_rate_k_per_min": ramp_rate_k_per_min})

    def refresh_control_configuration(self):
        self._control_refresh_requested.emit()

    def heater_off(self):
        self._heater_off_requested.emit()

    def shutdown(self):
        if self._thread.isRunning():
            QMetaObject.invokeMethod(
                self._worker, "shutdown", Qt.ConnectionType.BlockingQueuedConnection
            )
            self._thread.quit()
            self._thread.wait(5000)


LakeShoreController = LakeShore335Controller
