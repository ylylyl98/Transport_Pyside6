"""Threaded controller for Lake Shore Model 335 telemetry and sample setpoint."""

from __future__ import annotations

import threading
import traceback
from PySide6.QtCore import QObject, QMetaObject, QThread, QTimer, Qt, Signal, Slot

from app.devices.lakeshore335_adapter import LakeShore335Adapter, MockLakeShore335Adapter, LakeShore335Snapshot
import time
from utils.config import cfg




class _LakeShoreWorker(QObject):
    connected = Signal(object)
    disconnected = Signal()
    snapshot_updated = Signal(object)
    error = Signal(str)
    fault = Signal(str)
    control_result = Signal(object)

    def __init__(self):
        super().__init__()
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
            self.control_result.emit({"operation": "read_control", "ok": True, **state})
        except Exception as exc:
            # Control mapping/configuration failure is distinct from valid
            # sample/reservoir telemetry and must not disarm thermal protection.
            self.control_result.emit({"operation": "read_control", "ok": False, "error": str(exc)})

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
        try:
            config = cfg.lakeshore335
            if not config.verified_channel_mapping:
                raise RuntimeError("Sample/reservoir channel mapping must be verified")
            result = self.adapter.set_sample_control(target, config.sample_channel,
                minimum_temperature_k=config.sample_minimum_temperature_k,
                maximum_temperature_k=config.sample_maximum_temperature_k,
                maximum_heater_range=config.sample_maximum_heater_range)
            self.control_result.emit({"operation": "setpoint", "ok": True, **result})
        except Exception as exc:
            self.control_result.emit({"operation": "setpoint", "ok": False, "error": str(exc)})
            self.error.emit(f"Lake Shore setpoint failed: {exc}")

    @Slot(object)
    def set_sample_control(self, settings):
        try:
            if self.adapter is None:
                raise RuntimeError("Lake Shore 335 is not connected")
            config = cfg.lakeshore335
            if not config.verified_channel_mapping:
                raise RuntimeError("Sample/reservoir channel mapping must be verified")
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
            self.control_result.emit({"operation": "sample_control", "ok": True, **result})
        except Exception as exc:
            self.control_result.emit({"operation": "sample_control", "ok": False, "error": str(exc)})
            self.error.emit(f"Lake Shore sample control failed: {exc}")

    @Slot()
    def heater_off(self):
        if self.adapter is None:
            self.control_result.emit({"operation": "heater_off", "ok": False, "error": "Not connected; OFF unconfirmed"})
            self.error.emit("Lake Shore 335 is not connected; heater-off is unconfirmed")
            return
        try:
            result = self.adapter.heater_off(cfg.lakeshore335.sample_channel)
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

    _connect_requested = Signal(str, bool)
    _disconnect_requested = Signal()
    _refresh_requested = Signal()
    _polling_requested = Signal(bool)
    _setpoint_requested = Signal(float)
    _sample_control_requested = Signal(object)
    _control_refresh_requested = Signal()
    _heater_off_requested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._thread = QThread(self)
        self._worker = _LakeShoreWorker()
        from app.engine.transport_tasks import LatestTelemetry
        self.telemetry_cache = LatestTelemetry()
        self._worker.snapshot_updated.connect(self.telemetry_cache.publish, Qt.ConnectionType.DirectConnection)
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
        self._worker.connected.connect(self.connected)
        self._worker.disconnected.connect(self.disconnected)
        self._last_delivered_snapshot = None
        self._worker.snapshot_updated.connect(self._deliver_latest_snapshot)
        self._worker.error.connect(self.error)
        self._worker.fault.connect(self.fault)
        self._worker.control_result.connect(self.control_result)
        self._thread.start()

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
