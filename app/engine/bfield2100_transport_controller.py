"""Stepped transport with active attoDRY2100 sample-temperature control.

SDK calls stay on the SDK owner thread. Client completion and owner drain are
both required before progression or release. No APS100 heater/coil semantics
are used here; only electrical acquisition helpers are shared with transport.
"""
from concurrent.futures import TimeoutError as FutureTimeout
from copy import deepcopy
import math
import os
import threading
import time

from PySide6 import QtCore

from app.engine.bfield_transport_controller import BFieldTransportController
from app.engine.bfield_transport_sweep import (TransportSweepPlan, TransportCsvWriter,
    build_stepped_targets, build_transport_output_paths, transport_output_summary_parts,
    write_series_manifest)
from app.engine.transport_tasks import TaskLane
from app.engine.transport_stability import TemperatureWindow
from app.engine.stepped_transport import STEP_COLUMNS
from app.run_output import build_planned_output
from app.drag_drive import DUAL_COLUMNS, dual_enabled
from app.signal_chain import signal_chain_metadata
from utils.config import cfg


class BField2100TransportController(QtCore.QObject):
    continuation_changed = QtCore.Signal(bool, bool, str)
    state_changed = QtCore.Signal(str, str)
    progress_changed = QtCore.Signal(int, int, str)
    error = QtCore.Signal(str)
    restoration_warning = QtCore.Signal(str)
    finished = QtCore.Signal()
    stopped = QtCore.Signal(str)
    shutdown_ready = QtCore.Signal()

    # These shared methods only operate electrical sources, DAQ and plot data.
    _validate_biases = BFieldTransportController._validate_biases
    _apply_biases = BFieldTransportController._apply_biases
    _check_bias_progress = BFieldTransportController._check_bias_progress
    _check_io_cancel = BFieldTransportController._check_io_cancel
    _read_sample = BFieldTransportController._read_sample
    _acquire_daq = staticmethod(BFieldTransportController._acquire_daq)
    _zero_biases = BFieldTransportController._zero_biases
    refresh_plot = BFieldTransportController.refresh_plot

    def __init__(self, magnet, tab, device_manager, parent=None, review_valid_callable=None):
        super().__init__(parent)
        self.magnet, self.tab, self.device_manager = magnet, tab, device_manager
        self.thermal_safety = None
        self._review = review_valid_callable
        self._owner = "bfield-transport-2100"
        self._active = self._cleanup_in_progress = False
        self._exclusive_acquired = False
        self._claimed = []
        self.plan = self._transport_outputs = self._manifest = self._checkpoint = None
        self._sdk = self._stop_handle = None
        self._io_lane = self._storage_lane = None
        self._io_cancel = threading.Event()
        self._active_ao_channels = set()
        self._results, self._writers, self._cleanup_failures = [], {}, []
        self._calibration, self._signal_chain = (1e7, 100.), None
        self._last_plot = 0.
        self._shutdown_waiting = False
        self._measurement_phase = "idle"
        self._timer = QtCore.QTimer(self)
        self._timer.setInterval(100)
        self._timer.timeout.connect(self._tick)
        magnet.external_stop_requested.connect(self._external_stop)
        magnet.fault.connect(self._on_fault)
        magnet.disconnected.connect(lambda: self._on_fault("2100 disconnected"))

    @property
    def active(self):
        return self._active

    def start(self, *, verified_calibration=None):
        if self._active or self._cleanup_in_progress:
            return False
        try:
            review = self._review or getattr(self.tab, "transport_review_valid", None)
            if not callable(review) or not review():
                raise ValueError("Review the commissioned 2100 connection and limits before starting")
            params = deepcopy(self.tab.collect_params())
            params.magnet_backend = "2100"
            self.plan = TransportSweepPlan.from_params(params)
            if getattr(self.magnet.state, "name", "") not in {"IDLE", "ARMED", "ACTIVE"}:
                raise ValueError("Connect the 2100 and wait for pending operations")
            if not self.magnet.acquire_exclusive(self._owner):
                raise ValueError("2100 is busy or reserved")
            self._exclusive_acquired = True
            calibration = verified_calibration
            if calibration is None:
                calibration = (*self.tab.get_global_rates(), self.tab.get_signal_chain())
            amp, lockin, chain = calibration
            if any(not math.isfinite(float(v)) or float(v) <= 0 for v in (amp, lockin)):
                raise ValueError("Invalid electrical signal-chain calibration")
            self._signal_chain = signal_chain_metadata(chain) if chain is not None else None
            self._calibration = (amp, 10. / self._signal_chain['drag_drive']['drag_sensitivity_v'] if dual_enabled(self._signal_chain) else lockin)
            required = ["daq", "g1", "g2"]
            if any(c.vds_source == "Keithley 2400" for c in self.plan.conditions):
                required.append("g3")
            self._validate_biases(required)
            if self.device_manager.get_session("lockin") is not None:
                required.append("lockin")
            if dual_enabled(self._signal_chain):
                required.append("lockin_drive")
            success, blocked = self.device_manager.mark_in_use(required)
            if not success:
                raise ValueError("Transport devices are busy: " + ", ".join(blocked))
            self._claimed = required
            if hasattr(self.tab, "freeze_output_plan"):
                outputs = self.tab.freeze_output_plan(params)
                self.tab.validate_transport_output_ready(outputs)
            else:
                planned = build_planned_output(self.tab.save, "bfield_transport", params.base_name,
                                               transport_output_summary_parts(params, chain))
                outputs = build_transport_output_paths(planned, self.plan.conditions)
                if any(os.path.exists(path) for path in outputs.all_paths):
                    raise ValueError("Output already exists")
            self._transport_outputs, self._manifest, self._checkpoint = outputs, outputs.manifest_path, outputs.checkpoint_path
            self._results, self._writers, self._cleanup_failures = [], {}, []
            self._active_ao_channels.clear()
            self._condition_index = self._point_index = 0
            forward = build_stepped_targets(params)
            self._forward_count = len(forward)
            self._targets = forward + (forward[-2::-1] if params.round_trip else ())
            self._window = TemperatureWindow(params, 3.)
            self._sample_pending = False
            self._pending_row = None
            self._sample_invalid = False
            self._mutation_started = False
            self._hold = self._hold_stopping = False
            self._retry_requested = False
            self._active, self._cleanup_in_progress = True, False
            self._cleanup_status, self._cleanup_detail = "", ""
            self._stop_handle = self._sdk = None
            self._io_zeroed = self._files_finalized = self._files_finalizing = False
            self._stop_error_reported = False
            self._io_cancel.clear()
            self._started_monotonic = time.monotonic()
            self._last_poll = 0.
            self._io_lane = TaskLane("transport-2100-io", self)
            self._storage_lane = TaskLane("transport-2100-files", self)
            self._io_lane.failed.connect(self._on_fault)
            self._storage_lane.failed.connect(self._on_fault)
            self.tab.set_sweep_locked(True)
            self.magnet.set_polling_enabled(False)
            self._timer.start()
            self._state("configuring", "Checking 2100 safety and sample-temperature capabilities")
            self._submit(lambda: self.magnet.read_snapshot_async(owner=self._owner), self._preflight_ready)
            return True
        except Exception as exc:
            if self._active:
                self._fail(str(exc))
            else:
                if self._claimed:
                    self.device_manager.release(self._claimed)
                    self._claimed = []
                if self._exclusive_acquired:
                    self._exclusive_acquired = not self.magnet.release_exclusive(self._owner)
                self.error.emit(str(exc))
            return False

    def _state(self, phase, detail):
        self._measurement_phase = phase
        self.state_changed.emit(phase, detail)

    def _submit(self, factory, callback):
        if self._cleanup_in_progress:
            return
        if self._sdk is not None:
            raise RuntimeError("Overlapping 2100 workflow requests")
        self._sdk = (factory(), callback)

    @staticmethod
    def _drained(handle):
        if not handle.future.done():
            return False
        try:
            handle.wait_drained(0)
        except FutureTimeout:
            return False
        except Exception:
            pass  # Drain exceptions are propagated from the client result below.
        return True

    def _tick(self):
        try:
            if self._sdk is not None and self._drained(self._sdk[0]):
                handle, callback = self._sdk
                self._sdk = None
                if not self._cleanup_in_progress:
                    callback(handle.future.result())
            if self._cleanup_in_progress:
                self._cleanup_tick()
                return
            if not self._active or self._sdk is not None:
                return
            if self._retry_requested and self._hold and not self._hold_stopping:
                if min(self._field.monotonic_s, self._temperature.monotonic_s) <= self._retry_requested_at:
                    self._submit(lambda: self.magnet.read_snapshot_async(owner=self._owner), self._field_read)
                    return
                self._validate_magnet(self._field)
                self._validate_temperature(self._temperature)
                self._retry_requested = self._hold = False
                self.continuation_changed.emit(False, False, "")
                self._move_point()
                return
            if self._measurement_phase in {"positioning", "step_settling", "step_acquiring", "step_verifying", "thermal_hold"}:
                if time.monotonic() - self._last_poll >= max(.1, float(cfg.attodry2100.poll_interval_s)):
                    self._last_poll = time.monotonic()
                    self._submit(lambda: self.magnet.read_snapshot_async(owner=self._owner), self._field_read)
            if self._measurement_phase == "positioning" and time.monotonic() - self._point_started > float(cfg.mcd.sweep_leg_timeout_s):
                self._fail("2100 field positioning timed out")
        except Exception as exc:
            self._fail(str(exc))

    @staticmethod
    def _finite(value):
        return isinstance(value, (float, int)) and not isinstance(value, bool) and math.isfinite(value)

    def _validate_magnet(self, snap):
        settings = self.magnet.config
        if (not self._finite(snap.monotonic_s) or not 0 <= time.monotonic() - snap.monotonic_s <= 3.
                or not self._finite(snap.field_t) or abs(snap.field_t) > min(float(settings.maximum_field_t), 6.)
                or not self._finite(snap.temperature_k) or snap.temperature_k <= 0
                or snap.temperature_k > min(float(settings.maximum_temperature_k), 7.)
                or snap.status.quench is not False):
            raise ValueError("2100 magnetic safety telemetry is stale, unknown or unsafe")
        if snap.status.driven_mode is not True or snap.status.persistent_mode is not False:
            raise ValueError("2100 must remain in commissioned Driven mode")

    def _preflight_ready(self, snap):
        self._validate_magnet(snap)
        if str(snap.status.field_control_state).strip().upper() != "IDLE":
            raise ValueError("Wait for 2100 field motion to finish before starting transport")
        caps = snap.capabilities
        if not caps.sample_temperature_control or not caps.sample_temperature_readback:
            raise ValueError("2100 SDK sample temperature control and readback are required")
        self._mutation_started = True
        self._submit(lambda: self.magnet.configure_sample_temperature_async(
            self.plan.params.temperature_target_k, 1.0, owner=self._owner), self._temperature_configured)

    def _validate_temperature(self, snap):
        if (not self._finite(snap.monotonic_s) or not 0 <= time.monotonic() - snap.monotonic_s <= 3.
                or not self._finite(snap.sample_temperature_k) or snap.sample_temperature_k <= 0
                or not self._finite(snap.sample_setpoint_k)
                or abs(snap.sample_setpoint_k - self.plan.params.temperature_target_k) > .01
                or snap.sample_control_active is not True):
            raise ValueError("2100 sample temperature control/readback is invalid or target changed")

    def _temperature_configured(self, snap):
        self._validate_temperature(snap)
        self.plan.validation["calibration"] = {"amp_rate": self._calibration[0], "lockin_rate": self._calibration[1],
                                                "signal_chain": self._signal_chain}
        for i, (condition, path) in enumerate(zip(self.plan.conditions, self._transport_outputs.condition_csv_paths), 1):
            self._writers[i] = TransportCsvWriter(path, condition,
                extra_columns=STEP_COLUMNS + ("Magnet_temperature_K", "VTI_temperature_K", "Magnet_backend")
                + (DUAL_COLUMNS if dual_enabled(self._signal_chain) else ()))
        self._save_checkpoint("running")
        self._begin_condition()

    def _begin_condition(self):
        self._point_index = 0
        self._state("biasing", f"Applying condition {self._condition_index + 1}")
        condition = self.plan.conditions[self._condition_index]
        self._io_lane.submit(lambda: self._apply_biases(condition), self._bias_ready)

    def _bias_ready(self, value, error):
        if self._cleanup_in_progress:
            return
        if error:
            self._fail(str(error))
        else:
            self._move_point()

    def _move_point(self):
        if self._cleanup_in_progress or self._hold or self._sample_pending:
            return
        self._target = self._targets[self._point_index]
        self._window.reset()
        self._field_reads = 0
        self._field_stamp = None
        self._wait_started = None
        self._pending_row = None
        self._point_started = time.monotonic()
        self._state("positioning", f"2100 point {self._point_index + 1}/{len(self._targets)}: {self._target:+g} T")
        self._submit(lambda: self.magnet.set_h_setpoint_async(self._target, owner=self._owner),
                     lambda value: self._submit(lambda: self.magnet.start_field_control_async(owner=self._owner), self._field_started))

    def _field_started(self, value):
        self._last_poll = 0.

    def _field_read(self, snap):
        self._validate_magnet(snap)
        self._field = snap
        self._submit(lambda: self.magnet.read_temperature_snapshot_async(owner=self._owner), self._observe)

    def _observe(self, temp):
        self._validate_temperature(temp)
        self._temperature = temp
        field = self._field
        self._validate_magnet(field)
        stationary = (str(field.status.field_control_state).strip().upper() == "IDLE"
                      and self._finite(field.setpoint_t) and abs(field.setpoint_t - self._target) <= 1e-6
                      and abs(field.field_t - self._target) <= .010
                      and field.status.backend_details.get("field_control") is True)
        phase = self._measurement_phase
        if not stationary:
            self._window.reset()
            self._field_reads = 0
            if phase in {"step_acquiring", "step_verifying"}:
                self._sample_invalid = True
        elif self._field_stamp is None or field.monotonic_s > self._field_stamp:
            self._field_reads += 1
            self._field_stamp = field.monotonic_s
            if self._field_reads >= 3:
                if self._wait_started is None:
                    self._wait_started = time.monotonic()
                if phase == "positioning":
                    self._state("step_settling", f"Waiting at {self._target:+g} T for {self.plan.params.temperature_target_k:g} K")
                    phase = "step_settling"
                ready = self._window.observe(temp.monotonic_s, temp.sample_temperature_k, time.monotonic())
                if phase in {"step_acquiring", "step_verifying"} and not ready:
                    self._sample_invalid = True
                if ready and phase == "step_settling":
                    self._acquire()
                    return
        if phase == "step_verifying":
            self._verify_sample()
        if (self._measurement_phase == "step_settling" and self._wait_started is not None
                and time.monotonic() - self._wait_started >= self.plan.params.temperature_timeout_s):
            self._pause_for_temperature()

    def _acquire(self):
        c = self.plan.conditions[self._condition_index]
        self._sample_invalid = False
        self._sample_pending = True
        row = dict(Index=len(self._results), Timestamp=time.time(), Elapsed_s=time.monotonic() - self._started_monotonic,
            Condition_index=self._condition_index + 1, Condition_name=c.name,
            Direction="forward" if self._point_index < self._forward_count else "backward",
            B_target_T=self._target, B_measured_T=self._field.field_t, Doping=c.doping,
            Vtg=c.vtg, Vbg=c.vbg, Vds=c.vds, Sample_temperature_K=self._temperature.sample_temperature_k,
            Magnet_temperature_K=self._field.temperature_k, VTI_temperature_K=self._temperature.vti_temperature_k,
            Magnet_backend="2100", Field_point_index=self._point_index + 1,
            Temperature_target_K=self.plan.params.temperature_target_k)
        row["E-field"] = c.efield
        self._state("step_acquiring", "Acquiring at qualified field and sample temperature")
        self._io_lane.submit(lambda: self._read_sample(row, c), self._sample_ready)

    def _sample_ready(self, row, error):
        self._sample_pending = False
        if self._cleanup_in_progress:
            return
        if error:
            self._fail(str(error))
            return
        self._pending_row = row
        self._sample_finished = time.monotonic()
        self._state("step_verifying", "Checking fresh field and temperature after acquisition")

    def _verify_sample(self):
        if min(self._field.monotonic_s, self._temperature.monotonic_s) <= self._sample_finished:
            return
        row, self._pending_row = self._pending_row, None
        if self._sample_invalid or not self._window.ready():
            self._window.reset()
            self._state("step_settling", "Acquisition discarded: field/temperature changed; requalifying")
            return
        elapsed, span, drift = self._window.metrics()
        row.update(B_after_T=self._field.field_t, Sample_temperature_after_K=self._temperature.sample_temperature_k,
                   Temperature_window_s=elapsed, Temperature_span_K=span, Temperature_drift_K_per_min=drift)
        self._results.append(row)
        writer = self._writers[self._condition_index + 1]
        self._storage_lane.submit(lambda: writer.write(row), self._storage_done)
        receiver = getattr(self.tab, "accept_transport_record", None)
        if callable(receiver):
            receiver(dict(row))
        self.tab.set_progress((self._point_index + 1) / len(self._targets))
        if hasattr(self.tab, "plot"):
            self.refresh_plot()
        self._save_checkpoint("running")
        self._point_index += 1
        if self._point_index < len(self._targets):
            self._move_point()
        else:
            self._condition_index += 1
            if self._condition_index < len(self.plan.conditions):
                self._begin_condition()
            else:
                self._cleanup("finished", "2100 stepped sweep complete; sample temperature control retained")

    def _pause_for_temperature(self):
        self._hold = self._hold_stopping = True
        self._state("thermal_hold", "Temperature stability timed out; stopping field control at this point")
        self.continuation_changed.emit(True, False, "Waiting for SDK Stop acknowledgement")
        self._submit(lambda: self.magnet.request_stop(owner=self._owner), self._hold_stopped)

    def _hold_stopped(self, value):
        self._hold_stopping = False
        self.continuation_changed.emit(True, True, "Same-temperature timeout; Continue retries this point. Sample temperature control remains on.")
        self._save_checkpoint("paused")

    def continue_after_temperature_check(self):
        if self._active and self._hold and not self._hold_stopping and not self._cleanup_in_progress:
            self._retry_requested = True
            self._retry_requested_at = time.monotonic()

    def stop(self):
        if self._cleanup_in_progress:
            if self._stop_error_reported:
                self._stop_error_reported = False
                self._stop_handle = self.magnet.retry_stop(owner=self._owner)
            return
        if self._active:
            self._cleanup("stopped", "2100 sweep stopped; sample temperature control retained")

    def _external_stop(self):
        if self._active:
            self.stop()

    def _on_fault(self, message):
        if self._active and not self._cleanup_in_progress:
            self._fail(str(message))

    def _fail(self, message):
        self.error.emit(str(message))
        if self._active and not self._cleanup_in_progress:
            self._cleanup("failed", str(message))

    def _cleanup(self, status, detail):
        self._cleanup_in_progress = True
        self._cleanup_status, self._cleanup_detail = status, detail
        self._io_cancel.set()
        self._pending_row = None
        self.continuation_changed.emit(False, False, "")
        self._state("cleanup", detail + "; waiting for SDK and electrical cleanup")
        if self._mutation_started:
            self._stop_handle = self.magnet.request_stop(owner=self._owner)
        self._io_lane.submit(self._zero_biases, self._zeroed)

    def _zeroed(self, value, error):
        self._io_zeroed = True
        if error:
            self._cleanup_failures.append(str(error))

    def _cleanup_tick(self):
        if self._sdk is not None or not self._io_zeroed:
            return
        if self._stop_handle is not None:
            if not self._drained(self._stop_handle):
                return
            try:
                self._stop_handle.future.result()
            except Exception as exc:
                if not self._stop_error_reported:
                    self._stop_error_reported = True
                    self.error.emit(f"2100 Stop unconfirmed; reservation retained: {exc}")
                    self._state("cleanup_overdue", "SDK Stop failed; retry Stop before closing")
                return
            self._stop_handle = None
        if not self._files_finalizing:
            self._files_finalizing = True
            self._storage_lane.submit(self._finalize_files, self._files_done)
            return
        if not self._files_finalized or self.magnet.has_pending_work:
            return
        if not self.magnet.release_exclusive(self._owner):
            return
        self._exclusive_acquired = False
        self.device_manager.release(self._claimed)
        self._claimed = []
        self._io_lane.close()
        self._storage_lane.close()
        self._io_lane = self._storage_lane = None
        self._active = self._cleanup_in_progress = False
        self._timer.stop()
        self.magnet.set_polling_enabled(True)
        self.tab.set_sweep_locked(False)
        if hasattr(self.tab, "reset_output_preview"):
            self.tab.reset_output_preview()
        if self._cleanup_failures:
            self._cleanup_status = "failed"
            self.error.emit("; ".join(self._cleanup_failures))
        if self._cleanup_status == "finished":
            self.finished.emit()
        else:
            self.stopped.emit(self._cleanup_detail)
        if self._shutdown_waiting:
            self._shutdown_waiting = False
            self.shutdown_ready.emit()

    def _storage_done(self, value, error):
        if error:
            if self._cleanup_in_progress:
                self._cleanup_failures.append(str(error))
            else:
                self._fail(f"Output save failed: {error}")

    def _save_checkpoint(self, status):
        params, validation, results = deepcopy(self.plan.params), dict(self.plan.validation), list(self._results)
        runtime = {"backend": "2100", "phase": self._measurement_phase,
                   "field_point_index": self._point_index + 1, "condition_index": self._condition_index + 1}
        def save():
            for path in (self._manifest, self._checkpoint):
                write_series_manifest(path, params=params, validation=validation, status=status, results=results, runtime=runtime)
        self._storage_lane.submit(save, self._storage_done)

    def _finalize_files(self):
        for writer in self._writers.values():
            writer.close()
        status = "failed" if self._cleanup_failures else self._cleanup_status
        for path in (self._manifest, self._checkpoint):
            write_series_manifest(path, params=self.plan.params, validation=self.plan.validation,
                status=status, results=self._results, cleanup_failures=self._cleanup_failures,
                runtime={"backend": "2100", "detail": self._cleanup_detail,
                         "sample_temperature_control": "retained", "field_stop": "acknowledged" if self._mutation_started else "not requested"})

    def _files_done(self, value, error):
        self._files_finalized = True
        if error:
            self._cleanup_failures.append(str(error))

    def prepare_shutdown(self):
        if not self._active and not self._cleanup_in_progress:
            return True
        self._shutdown_waiting = True
        self.stop()
        return False

    def cancel_shutdown_preparation(self):
        self._shutdown_waiting = False
