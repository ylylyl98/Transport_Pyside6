"""Qt orchestration for the APS100 continuous transport workflow.

Magnet I/O remains on ``MagnetController``'s dedicated thread.  This object
owns only the series state, device reservation, and incremental data capture.
"""

from __future__ import annotations

import os
import time
import math
from copy import deepcopy

from PyQt6 import QtCore
from PyQt6 import QtWidgets

from app.engine.bfield_transport_sweep import (
    BFieldTransportSafetyError,
    TransportCsvWriter,
    TransportSweepPlan,
    build_transport_output_paths,
    transport_output_summary_parts,
    validate_voltage_margin,
    write_series_manifest,
    normalize_cooldown_policy,
)
from app.run_output import build_planned_output
from app.utils import safe_ramp
from utils.config import cfg


class BFieldTransportController(QtCore.QObject):
    state_changed = QtCore.pyqtSignal(str, str)
    progress_changed = QtCore.pyqtSignal(int, int, str)
    error = QtCore.pyqtSignal(str)
    finished = QtCore.pyqtSignal()
    stopped = QtCore.pyqtSignal(str)

    def __init__(self, magnet, tab, device_manager, thermal_safety=None, parent=None):
        super().__init__(parent)
        self.magnet = magnet
        self.tab = tab
        self.device_manager = device_manager
        self.thermal_safety = thermal_safety
        self.plan = None
        self._active = False
        self._stop_requested = False
        self._condition_index = 0
        self._leg_index = 0
        self._target = None
        self._owner = "bfield-transport"
        self._claimed = []
        self._exclusive_acquired = False
        self._stored_rates = self._stored_limits = None
        self._persistent_confirmation_granted = False
        self._writers = {}
        self._transport_outputs = None
        self._manifest = self._checkpoint = None
        self._log_path = None
        self._results = []
        self._last_acquisition = 0.0
        self._started_monotonic = time.monotonic()
        self._cleanup_in_progress = False
        self._cleanup_status = ""
        self._cleanup_detail = ""
        self._cleanup_failures = []
        self._cleanup_waiting = ""
        self._cleanup_overdue_phases = set()
        self._transition_pending = False
        self._transition_next = None
        self._thermal_hold = False
        self._thermal_pause_pending = False
        self._persistent_row_transition = False
        self._persistent_row_entered = False
        self._row_persistent_ready_at = None
        self._thermal_resume_action = None
        self._cleanup_timer = QtCore.QTimer(self)
        self._cleanup_timer.setSingleShot(True)
        self._cleanup_timer.timeout.connect(self._on_cleanup_timeout)
        self._active_ao_channels = set()
        self._calibration = (1e7, 100.0)
        self._signal_chain = None
        self._heater_active = False
        magnet.transport_config_result.connect(self._on_configured)
        magnet.safe_move_result.connect(self._on_safe_move)
        magnet.transport_sweep_result.connect(self._on_sweep_started)
        magnet.snapshot_updated.connect(self._on_snapshot)
        magnet.fault.connect(self._on_fault)
        if hasattr(magnet, "operation_finished"):
            magnet.operation_finished.connect(self._on_magnet_operation)
        if hasattr(magnet, "transport_restore_result"):
            magnet.transport_restore_result.connect(self._on_restore_result)
        if hasattr(magnet, "error"):
            magnet.error.connect(self._on_magnet_error)
        if hasattr(magnet, "disconnected"):
            magnet.disconnected.connect(self._on_magnet_disconnected)

    @property
    def active(self):
        return self._active

    def start(self):
        if self._active:
            self.error.emit("A B-field Transport sweep is already running")
            return False
        try:
            params = self.tab.collect_params()
            self.plan = TransportSweepPlan.from_params(params)
            if hasattr(self.tab, "freeze_output_plan"):
                self._transport_outputs = self.tab.freeze_output_plan(params)
                self.tab.validate_transport_output_ready(self._transport_outputs)
            else:
                signal_getter = getattr(self.tab, "get_signal_chain", None)
                signal_chain = signal_getter() if callable(signal_getter) else None
                planned = build_planned_output(
                    self.tab.save,
                    "bfield_transport",
                    params.base_name,
                    transport_output_summary_parts(params, signal_chain),
                )
                self._transport_outputs = build_transport_output_paths(planned, self.plan.conditions)
                existing = [path for path in self._transport_outputs.all_paths if os.path.exists(path)]
                if existing:
                    raise BFieldTransportSafetyError(
                        "Output file already exists: " + ", ".join(os.path.basename(path) for path in existing)
                    )
                os.makedirs(planned.output_dir, exist_ok=True)
            policy = normalize_cooldown_policy(self.plan.params.cooldown_policy)
            window = self.tab.window()
            panel = getattr(window, "magnet_panel", None)
            if panel is None or not bool(panel._review_valid()):
                raise BFieldTransportSafetyError("APS100 commissioning review is required before transport")
            backend = getattr(panel, "_backend", None)
            if backend is not None and str(backend) != "1000":
                raise BFieldTransportSafetyError("B-field Transport requires the APS100 backend (1000)")
            if not bool(getattr(self.magnet, "is_connected", False)):
                raise BFieldTransportSafetyError("APS100 must be connected before transport")
            snapshot = getattr(self.magnet, "latest_snapshot", None)
            if snapshot is None:
                raise BFieldTransportSafetyError("Fresh APS100 telemetry is required before transport")
            snapshot_age = getattr(snapshot, "reading_age_s", None)
            if snapshot_age is None and getattr(snapshot, "monotonic_s", None) is not None:
                snapshot_age = time.monotonic() - float(snapshot.monotonic_s)
            if snapshot_age is not None and (not math.isfinite(float(snapshot_age)) or float(snapshot_age) > 3.0):
                raise BFieldTransportSafetyError("Fresh APS100 telemetry is required before transport")
            rates_getter = getattr(self.tab, "get_global_rates", None)
            signal_getter = getattr(self.tab, "get_signal_chain", None)
            self._calibration = tuple(rates_getter()) if callable(rates_getter) else (1e7, 100.0)
            if len(self._calibration) != 2 or any(not math.isfinite(float(v)) or float(v) <= 0 for v in self._calibration):
                raise BFieldTransportSafetyError("Signal-chain rates are invalid; verify calibration before transport")
            self._signal_chain = signal_getter() if callable(signal_getter) else None
            if self._signal_chain is not None:
                for key in ("frequency_hz", "lockin_sensitivity_v", "preamp_sensitivity_a"):
                    value = getattr(self._signal_chain, key, None)
                    if value is None and isinstance(self._signal_chain, dict):
                        value = self._signal_chain.get(key)
                    if value is not None and (not math.isfinite(float(value)) or float(value) <= 0):
                        raise BFieldTransportSafetyError("Signal-chain calibration is invalid; verify calibration before transport")
            calibration_meta = {"amp_rate": float(self._calibration[0]), "lockin_rate": float(self._calibration[1])}
            if self._signal_chain is not None:
                calibration_meta["signal_chain"] = (
                    self._signal_chain.to_dict() if hasattr(self._signal_chain, "to_dict")
                    else dict(self._signal_chain) if isinstance(self._signal_chain, dict)
                    else str(self._signal_chain)
                )
            self.plan.validation["calibration"] = calibration_meta
            self._persistent_confirmation_granted = self._confirm_initial_persistent_field(snapshot)
            if self.thermal_safety is None or not bool(
                getattr(self.thermal_safety, "is_armed", getattr(self.thermal_safety, "armed", False))
            ):
                raise BFieldTransportSafetyError(
                    "A commissioned, armed Lake Shore thermal evaluator is required before transport"
                )
            decision = self.thermal_safety.evaluate(getattr(self.thermal_safety, "latest_snapshot", None))
            if decision is None or not decision.magnet_permission:
                raise BFieldTransportSafetyError(
                    "Lake Shore thermal preflight rejected transport sweep: "
                    + str(getattr(decision, "reason", "thermal evaluator unavailable"))
                )
            self._heater_active = getattr(snapshot, "heater_on", None) is True
            if not self.magnet.acquire_exclusive(self._owner):
                raise BFieldTransportSafetyError("APS100 is already reserved by another operation")
            self._exclusive_acquired = True
            required = ["daq", "g1", "g2"]
            if any(c.vds_source == "Keithley 2400" for c in self.plan.conditions):
                required.append("g3")
            self._validate_biases(required)
            claimed, blocked = self.device_manager.mark_in_use(required)
            if not claimed:
                self.magnet.release_exclusive(self._owner)
                self._exclusive_acquired = False
                raise BFieldTransportSafetyError("Transport devices already in use: " + ", ".join(blocked))
            self._claimed = required
            self._manifest = self._transport_outputs.manifest_path
            self._checkpoint = self._transport_outputs.checkpoint_path
            self._log_path = self._transport_outputs.log_path
            self._log("B-field Transport series started")
            write_series_manifest(self._manifest, params=params, validation=self.plan.validation)
            write_series_manifest(self._checkpoint, params=params, validation=self.plan.validation)
            self._writers = {}
            for index, (condition, csv_path) in enumerate(
                zip(self.plan.conditions, self._transport_outputs.condition_csv_paths), start=1
            ):
                self._writers[index] = TransportCsvWriter(csv_path, condition)
            self._active = True
            self._stop_requested = False
            self._started_monotonic = time.monotonic()
            self._last_acquisition = 0.0
            self._results = []
            self._condition_index = 0
            self._leg_index = 0
            self.tab.set_sweep_locked(True)
            self.state_changed.emit("configuring", "Programming APS100 rate and ±6 T limits")
            self.magnet.configure_transport(params.rate_t_per_min, 6.0)
            return True
        except Exception as exc:
            self.error.emit(str(exc))
            self._cleanup("failed")
            return False

    def stop(self):
        if not self._active:
            return
        self._stop_requested = True
        self._log("Stop requested by user")
        self._cleanup("stopped", "B-field Transport stopped; partial data and checkpoint preserved")

    def _on_magnet_operation(self, name):
        name = str(name)
        if not self._cleanup_in_progress:
            if self._thermal_pause_pending and name == "pause":
                self._thermal_pause_pending = False
                self.state_changed.emit("thermal_hold", "APS100 paused; waiting for Lake Shore recovery dwell")
                return
            if self._transition_pending and name == "pause":
                self._transition_pending = False
                next_step = self._transition_next
                self._transition_next = None
                if self._persistent_row_transition:
                    self._begin_row_persistent_transition()
                elif self._thermal_hold:
                    self._resume_after_thermal_recovery()
                elif next_step == "return":
                    self._leg_index = 1
                    self._target = self.plan.params.start_field_t
                    self.magnet.start_transport_sweep(self._target)
                elif next_step == "next":
                    permitted, detail = self._thermal_permission()
                    if not permitted:
                        if not isinstance(detail, str) or detail.startswith("Lake Shore thermal interlock"):
                            self._thermal_hold = True
                            self._thermal_resume_action = "next"
                            self.state_changed.emit("thermal_hold", "Waiting for Lake Shore recovery before changing condition")
                        else:
                            self._fail(detail if isinstance(detail, str) else "Lake Shore recovery gate rejected next condition")
                    else:
                        self._next_condition()
            elif self._persistent_row_transition and name == "enter_persistent_mode":
                self._persistent_row_entered = True
                self._resume_after_thermal_recovery()
            elif self._active and name.startswith("failed:"):
                self._fail(f"APS100 operation failed: {name[7:]}")
            return
        if name == "pause" and self._cleanup_waiting == "pause":
            self._begin_persistent_cleanup()
        elif name == "failed:pause" and self._cleanup_waiting == "pause":
            self._cleanup_failures.append("pause cleanup failed: APS100 reported operation failure")
            self._begin_persistent_cleanup()
        elif name == "enter_persistent_mode" and self._cleanup_waiting == "persistent":
            self._request_restore()
        elif name == "failed:enter_persistent_mode" and self._cleanup_waiting == "persistent":
            self._cleanup_failures.append("persistent cleanup failed: APS100 reported operation failure")
            self._request_restore()

    def _begin_persistent_cleanup(self):
        self._cleanup_waiting = "persistent"
        self._cleanup_timer.start(self._cleanup_watchdog_ms())
        try:
            self.magnet.enter_persistent_mode(zero_leads=True)
        except Exception as exc:
            self._cleanup_failures.append(f"persistent cleanup failed: {exc}")
            self._request_restore()

    def _on_magnet_error(self, message):
        if self._cleanup_in_progress:
            self._cleanup_failures.append(f"APS100 cleanup error: {message}")
            if self._cleanup_waiting in {"pause", "persistent"}:
                self._request_restore()
            else:
                self._finish_cleanup()
        elif self._active:
            self._fail(str(message))

    def _on_magnet_disconnected(self):
        if self._cleanup_in_progress:
            self._cleanup_failures.append("APS100 disconnected during cleanup; restoration could not be verified")
            self._finish_cleanup()
        elif self._active:
            self._fail("APS100 disconnected during transport sweep")

    def _on_cleanup_timeout(self):
        if self._cleanup_in_progress:
            phase = self._cleanup_waiting or "APS100"
            if phase not in self._cleanup_overdue_phases:
                self._cleanup_overdue_phases.add(phase)
                message = f"cleanup overdue while waiting for {phase}; ownership retained until acknowledgement"
                self._cleanup_failures.append(message)
                self._log(message)
                self.state_changed.emit("cleanup_overdue", message)
            # This watchdog is deliberately non-destructive.  A late APS
            # acknowledgement (or confirmed disconnect) is the only path that
            # may advance/finalize cleanup.
            self._cleanup_timer.start(self._cleanup_watchdog_ms())

    def _cleanup_watchdog_ms(self):
        operation = max(1.0, float(getattr(cfg.magnet, "operation_timeout_s", 180.0)))
        persistent = max(
            operation,
            float(getattr(cfg.magnet, "heater_cool_s", 120.0)),
            float(getattr(cfg.magnet, "persistent_operation_timeout_s", 900.0)),
        )
        seconds = persistent if self._cleanup_waiting == "persistent" else operation
        if self._cleanup_waiting == "restore":
            seconds = operation
        return max(1000, int(seconds * 1000))

    def _request_restore(self):
        if self._cleanup_waiting == "restore":
            return
        self._cleanup_waiting = "restore"
        self._cleanup_timer.start(self._cleanup_watchdog_ms())
        if self._stored_rates is None and self._stored_limits is None:
            self._finish_cleanup()
            return
        try:
            self.magnet.restore_transport(self._stored_rates, self._stored_limits)
        except Exception as exc:
            self._cleanup_failures.append(f"APS100 restoration request failed: {exc}")
            self._finish_cleanup()
            return
        if not hasattr(self.magnet, "transport_restore_result"):
            self._finish_cleanup()

    def _begin_row_persistent_transition(self):
        self._persistent_row_transition = True
        self._persistent_row_entered = False
        self._thermal_hold = True
        self._row_persistent_ready_at = time.monotonic() + max(0.0, float(getattr(cfg.magnet, "heater_cool_s", 120.0)))
        self.state_changed.emit("cooldown", "Condition complete; entering persistent mode for thermal recovery")
        try:
            self.magnet.enter_persistent_mode(zero_leads=True)
        except Exception as exc:
            self._fail(f"Persistent row transition failed: {exc}")

    def _thermal_permission(self, snapshot=None, *, for_reentry=False):
        if self.thermal_safety is None or not bool(getattr(self.thermal_safety, "is_armed", getattr(self.thermal_safety, "armed", False))):
            return False, "Lake Shore thermal evaluator is unavailable or no longer commissioned/armed"
        try:
            decision = self.thermal_safety.evaluate(snapshot if snapshot is not None else getattr(self.thermal_safety, "latest_snapshot", None))
        except Exception as exc:
            return False, f"Lake Shore thermal evaluation failed closed: {exc}"
        if decision is None:
            return False, "Lake Shore thermal evaluator returned no decision"
        if decision.magnet_permission:
            return True, decision
        if (
            not for_reentry and self._heater_active and
            str(getattr(decision, "block_code", "")) == "minimum_heater_interval"
        ):
            # The minimum interval protects the next heater activation; it
            # must not pause a sweep whose heater is already on.
            return True, decision
        state = str(getattr(getattr(decision, "state", None), "value", getattr(decision, "state", "")))
        if state in {"WARNING", "COOLDOWN_HOLD"}:
            return False, decision
        return False, f"Lake Shore thermal interlock: {decision.reason}"

    def _resume_after_thermal_recovery(self):
        if not self._active or not self._thermal_hold:
            return
        if self._thermal_pause_pending:
            return
        if self._persistent_row_transition and not self._persistent_row_entered:
            return
        if self._row_persistent_ready_at is not None and time.monotonic() < self._row_persistent_ready_at:
            return
        permitted, detail = self._thermal_permission(for_reentry=self._persistent_row_transition)
        if not permitted:
            if isinstance(detail, str) and not detail.startswith("Lake Shore thermal interlock"):
                self._fail(detail)
            return
        self._thermal_hold = False
        if self._persistent_row_transition:
            self._persistent_row_transition = False
            self._persistent_row_entered = False
            self._condition_index += 1
            if self._condition_index >= len(self.plan.conditions):
                self._cleanup("finished")
                return
            self.magnet.safe_move_to_field(
                self.plan.params.start_field_t, final_mode="driven", zero_leads=False,
                persistent_field_confirmed=True,
            )
        elif self._thermal_resume_action == "next":
            self._thermal_resume_action = None
            self._next_condition()
        else:
            self.state_changed.emit("resuming", "Lake Shore recovery dwell complete; resuming APS100 sweep")
            self.magnet.start_transport_sweep(self._target)

    def _on_restore_result(self, result):
        if self._cleanup_in_progress and self._cleanup_waiting == "restore":
            if not result.get("success", False):
                self._cleanup_failures.extend(result.get("failures", [result.get("error", "APS100 restore failed")]))
            self._finish_cleanup()

    def _finish_cleanup(self):
        if not self._cleanup_in_progress:
            return
        self._cleanup_timer.stop()
        for writer in self._writers.values():
            try: writer.close()
            except Exception as exc: self._cleanup_failures.append(f"CSV close failed: {exc}")
        self._writers.clear()
        if self._manifest:
            write_series_manifest(self._manifest, params=self.plan.params, validation=self.plan.validation, status=self._cleanup_status, results=self._results, cleanup_failures=self._cleanup_failures)
        if self._checkpoint:
            write_series_manifest(self._checkpoint, params=self.plan.params, validation=self.plan.validation, status=self._cleanup_status, results=self._results, cleanup_failures=self._cleanup_failures)
        if self._claimed: self.device_manager.release(self._claimed)
        self._claimed = []
        if self._exclusive_acquired:
            self.magnet.release_exclusive(self._owner)
            self._exclusive_acquired = False
        self._active = False
        self._cleanup_in_progress = False
        self.tab.set_sweep_locked(False)
        if hasattr(self.tab, "reset_output_preview"):
            self.tab.reset_output_preview()
        self.state_changed.emit(self._cleanup_status, self._cleanup_detail or self._cleanup_status)
        if self._cleanup_status == "finished": self.finished.emit()
        elif self._cleanup_status == "stopped": self.stopped.emit(self._cleanup_detail or "B-field Transport stopped")

    def _on_configured(self, result):
        if not self._active:
            return
        if not result.get("success"):
            self._fail(result.get("error", "APS100 transport configuration failed")); return
        self._stored_rates, self._stored_limits = result.get("stored_rates"), result.get("stored_limits")
        try:
            validate_voltage_margin(
                self.plan.params.rate_t_per_min,
                inductance_h=getattr(cfg.magnet, "inductance_h", None),
                voltage_limit_v=result.get("voltage_limit_v"),
            )
        except Exception as exc:
            self._fail(str(exc)); return
        self.magnet.safe_move_to_field(
            self.plan.params.start_field_t, final_mode="driven", zero_leads=False,
            persistent_field_confirmed=self._persistent_confirmation_granted,
        )

    def _confirm_initial_persistent_field(self, snapshot):
        if getattr(snapshot, "heater_on", None) is True:
            return True
        if not hasattr(snapshot, "field_t"):
            raise BFieldTransportSafetyError("Stored APS100 field/polarity could not be reviewed")
        answer = QtWidgets.QMessageBox.question(
            self.tab, "Confirm stored APS100 field",
            f"APS100 heater is OFF and stored field is {float(snapshot.field_t):+.6f} T.\n\n"
            "Confirm this field and polarity before matching supply leads.",
            QtWidgets.QMessageBox.StandardButton.Yes | QtWidgets.QMessageBox.StandardButton.No,
            QtWidgets.QMessageBox.StandardButton.No,
        )
        if answer != QtWidgets.QMessageBox.StandardButton.Yes:
            raise BFieldTransportSafetyError("Stored persistent field confirmation was declined")
        return True

    def _start_condition(self):
        condition = self.plan.conditions[self._condition_index]
        try:
            self._apply_biases(condition)
        except Exception as exc:
            self._fail(f"Condition bias transition failed: {exc}"); return
        self._leg_index = 0
        self._target = self.plan.legs[1][0]
        self.state_changed.emit("sweeping", f"Condition {self._condition_index + 1}/{len(self.plan.conditions)}")

    def _on_safe_move(self, result):
        if not self._active or not result.get("success"):
            if self._active: self._fail(result.get("error", "APS100 driven-mode positioning failed"))
            return
        self._note_heater_activation(result)
        self._start_condition()
        if not self._active:
            return
        self.magnet.start_transport_sweep(self._target)

    def _on_sweep_started(self, result):
        if self._active and not result.get("success"):
            self._fail(result.get("error", "APS100 sweep command failed"))

    def _on_snapshot(self, snapshot):
        if not self._active or snapshot is None:
            return
        status = getattr(snapshot, "status", None)
        heater = getattr(snapshot, "heater_on", None)
        if heater is not None:
            self._heater_active = bool(heater)
        age = getattr(snapshot, "reading_age_s", None)
        if age is None:
            stamp = getattr(snapshot, "monotonic_s", None)
            if stamp is not None:
                try:
                    age = max(0.0, time.monotonic() - float(stamp))
                except (TypeError, ValueError):
                    age = float("inf")
        if age is not None and (not isinstance(age, (int, float)) or float(age) > 3.0):
            self._fail("APS100 telemetry is stale during transport sweep"); return
        if status is not None and (getattr(status, "quench", False) or getattr(status, "power_module_failure", False) or getattr(status, "faulted", False)):
            self._fail("APS100 quench or power-module fault reported"); return
        voltage = getattr(snapshot, "magnet_voltage_v", None)
        limit = getattr(snapshot, "voltage_limit_v", None)
        if voltage is not None and limit is not None and abs(float(voltage)) > float(limit) + 1e-9:
            self._fail(f"APS100 magnet voltage {float(voltage):.6g} V exceeds {float(limit):.6g} V limit"); return
        permitted, thermal_detail = self._thermal_permission()
        if not permitted:
            if self._thermal_hold:
                return
            if not isinstance(thermal_detail, str) or thermal_detail.startswith("Lake Shore thermal interlock"):
                self._request_thermal_pause()
                return
            self._fail(thermal_detail)
            return
        target = self._target
        measured = float(getattr(snapshot, "field_t", 0.0))
        self._record_snapshot(snapshot)
        if self._transition_pending:
            return
        if target is None or abs(measured - target) > 0.002 or getattr(status, "sweep_active", False):
            return
        if self._leg_index == 0:
            self._leg_index = 1
            self._target = self.plan.params.start_field_t
            if self.plan.params.round_trip:
                self._transition_pending = True
                self._transition_next = "return"
            else:
                self._transition_pending = True
                self._transition_next = "next"
                if self._condition_index < len(self.plan.conditions) - 1 and normalize_cooldown_policy(self.plan.params.cooldown_policy) == "persistent_each_row":
                    self._persistent_row_transition = True
        else:
            self._transition_pending = True
            self._transition_next = "next"
            if self._condition_index < len(self.plan.conditions) - 1 and normalize_cooldown_policy(self.plan.params.cooldown_policy) == "persistent_each_row":
                self._persistent_row_transition = True
        self.magnet.pause()
        # Simple test doubles may not expose an operation signal.  Production
        # MagnetController always does, and waits for its acknowledgement.
        if not hasattr(self.magnet, "operation_finished"):
            self._on_magnet_operation("pause")

    def _record_snapshot(self, snapshot):
        now = time.monotonic()
        # APS telemetry is the clock for a driven sweep; do not imply a faster
        # acquisition cadence than the configured poll interval.
        interval = max(float(getattr(cfg.magnet, "poll_interval_s", 0.1)),
                       float(self.plan.params.acquisition_delay_s), 0.01)
        if now - self._last_acquisition < interval:
            return
        self._last_acquisition = now
        condition = self.plan.conditions[self._condition_index]
        direction = "forward" if self._leg_index == 0 else "backward"
        row = {"Index": len(self._results), "Timestamp": time.time(), "Elapsed_s": time.monotonic() - self._started_monotonic, "Condition_index": self._condition_index + 1,
               "Condition_name": condition.name, "Direction": direction, "B_measured_T": float(getattr(snapshot, "field_t", 0.0)),
               "B_target_T": float(self._target), "Sweep_rate_T_per_min": self.plan.params.rate_t_per_min,
               "Sweep_rate_A_per_s": self.plan.validation["rate_a_per_s"], "Doping": condition.doping, "E-field": condition.efield,
               "Vtg": condition.vtg, "Vbg": condition.vbg, "Vds": condition.vds}
        daq = self.device_manager.get_session("daq")
        if daq is not None and hasattr(daq, "acquire"):
            try:
                raw = self._acquire_daq(daq, max(1, int(self.plan.params.averages)))
            except Exception as exc:
                self._fail(f"DAQ acquisition failed during transport sweep: {exc}")
                return
            row.update({"raw_X": raw[0], "raw_Y": raw[1], "raw_DC": raw[2]})
            try:
                amp_rate, lockin_rate = self._calibration
                row.update({"Ids_X": raw[0] / (float(amp_rate) * float(lockin_rate)), "Ids_Y": raw[1] / (float(amp_rate) * float(lockin_rate)), "Ids_DC": raw[2] / float(amp_rate)})
            except Exception:
                pass
            if condition.vds_source != "Keithley 2400" and hasattr(daq, "get_ao_vs_gnd_value"):
                row["Vds_measured"] = daq.get_ao_vs_gnd_value(int(condition.ao_channel))
        if condition.vds_source == "Keithley 2400":
            smu = self.device_manager.get_session("g3")
            if smu is not None:
                try:
                    values = smu.acquire() if hasattr(smu, "acquire") else None
                    if isinstance(values, dict) and "current" in values:
                        row["Keithley_current"] = float(values["current"])
                    elif hasattr(smu, "get_current"):
                        row["Keithley_current"] = float(smu.get_current())
                    else:
                        row["Keithley_current"] = float(smu.current)
                except Exception: pass
        thermal = getattr(self.thermal_safety, "latest_snapshot", None) if self.thermal_safety is not None else None
        if thermal is not None:
            row["Sample_temperature_K"] = getattr(thermal, "sample_temperature_k", None)
            row["Reservoir_temperature_K"] = getattr(thermal, "reservoir_temperature_k", None)
        self._results.append(row)
        writer = self._writers.get(self._condition_index + 1)
        if writer: writer.write(row)
        self._emit_measurement_update(row)
        if self._checkpoint:
            write_series_manifest(self._checkpoint, params=self.plan.params, validation=self.plan.validation, results=self._results)

    @staticmethod
    def _acquire_daq(daq, averages):
        raw = [0.0, 0.0, 0.0]
        for _ in range(averages):
            values = daq.acquire()
            for channel in range(3):
                if hasattr(daq, "get_ai_value"):
                    value = daq.get_ai_value(channel)
                elif isinstance(values, dict):
                    value = values.get(f"ai{channel}", values.get(("raw_X", "raw_Y", "raw_DC")[channel]))
                else:
                    value = values[channel]
                raw[channel] += float(value)
        return [value / averages for value in raw]

    def _emit_measurement_update(self, row):
        """Update the visible progress/plot/log from each acquired row."""
        target = float(row.get("B_target_T", 0.0))
        start = float(self.plan.params.start_field_t)
        span = abs(target - start) or 1.0
        fraction = min(1.0, max(0.0, abs(float(row.get("B_measured_T", start)) - start) / span))
        if hasattr(self.tab, "set_progress"):
            self.tab.set_progress(fraction)
        if hasattr(self.tab, "plot"):
            try:
                self.tab.plot.ax.plot([row["B_measured_T"]], [row.get("Ids_DC", float("nan"))], marker=".", color="tab:blue")
                self.tab.plot.canvas.draw_idle()
            except Exception:
                pass
        if hasattr(self.tab, "log"):
            try:
                self.tab.log.appendPlainText(
                    f"{row['Condition_name']} {row['Direction']}: B={row['B_measured_T']:.6g} T"
                )
            except Exception:
                pass

    def _next_condition(self):
        self._condition_index += 1
        if self._condition_index >= len(self.plan.conditions):
            self._cleanup("finished"); return
        self._start_condition()
        self._target = self.plan.params.stop_field_t
        self.magnet.start_transport_sweep(self._target)

    def _note_heater_activation(self, result):
        """Record only a real OFF→ON transition for thermal interval gating."""
        audit = result.get("audit") or {}
        request = audit.get("request") or {}
        if str(request.get("final_mode", "")).lower() != "driven":
            return
        starting = audit.get("starting_snapshot") or {}
        final = audit.get("final_snapshot") or {}
        start_heater = starting.get("heater_on") if isinstance(starting, dict) else getattr(starting, "heater_on", None)
        final_heater = final.get("heater_on") if isinstance(final, dict) else getattr(final, "heater_on", None)
        if final_heater is None:
            moved_snapshot = result.get("snapshot")
            final_heater = getattr(moved_snapshot, "heater_on", None)
        if start_heater is False and final_heater is True:
            note = getattr(self.thermal_safety, "note_heater_activation", None)
            if callable(note):
                note()
            self._heater_active = True

    def _request_thermal_pause(self):
        if self._thermal_pause_pending or self._thermal_hold or not self._active:
            return
        self._thermal_pause_pending = True
        self._thermal_hold = True
        self._row_persistent_ready_at = None
        self.state_changed.emit("thermal_warning", "Lake Shore warning; pausing APS100 until recovery dwell completes")
        try:
            self.magnet.pause()
        except Exception as exc:
            self._fail(f"Unable to pause APS100 for thermal recovery: {exc}")

    def _apply_biases(self, condition):
        condition.refresh_gates()
        pairs = (("g1", condition.vtg), ("g2", condition.vbg))
        if condition.vds_source == "Keithley 2400": pairs += (("g3", condition.vds),)
        for name, target in pairs:
            session = self.device_manager.get_session(name)
            if session is None: raise BFieldTransportSafetyError(f"Missing session {name.upper()}")
            current = float(getattr(session, "voltage", 0.0) or 0.0)
            safe_ramp(session.set_voltage, current, float(target), 0.1, 0.02)
        if condition.vds_source != "Keithley 2400":
            daq = self.device_manager.get_session("daq")
            if daq is None or not hasattr(daq, "ramp_voltage"):
                raise BFieldTransportSafetyError("NI DAQ session is required for AO Vds")
            daq.ramp_voltage(int(condition.ao_channel), float(condition.vds), 0.1, 0.02)
            self._active_ao_channels.add(int(condition.ao_channel))

    def _validate_biases(self, required):
        daq = self.device_manager.get_session("daq")
        # The production tab always supplies the calibration callback; a
        # minimal controller test double may intentionally omit all DAQ I/O.
        if (daq is None or not hasattr(daq, "acquire")) and callable(getattr(self.tab, "get_global_rates", None)):
            raise BFieldTransportSafetyError("NI DAQ acquisition session is required for transport measurements")
        for condition in self.plan.conditions:
            condition.refresh_gates()
            if condition.vds_source not in {"Keithley 2400", "NI DAQ AO"}:
                raise BFieldTransportSafetyError(f"Unsupported Vds source: {condition.vds_source}")
            for name, value in (("g1", condition.vtg), ("g2", condition.vbg)):
                session = self.device_manager.get_session(name)
                if session is None or not self.device_manager.is_voltage_source_mode(name):
                    raise BFieldTransportSafetyError(f"{name.upper()} must be connected in voltage-source mode")
                limit = self.device_manager.applied_gate_voltage_limit(name)
                if abs(float(value)) > limit + 1e-9:
                    raise BFieldTransportSafetyError(f"Condition {condition.name}: {name.upper()} target exceeds verified {limit:g} V protection")
            if condition.vds_source == "Keithley 2400":
                if self.device_manager.get_session("g3") is None or not self.device_manager.is_voltage_source_mode("g3"):
                    raise BFieldTransportSafetyError("G3 must be connected in voltage-source mode for Keithley Vds")
                limit = self.device_manager.applied_gate_voltage_limit("g3")
                if abs(float(condition.vds)) > limit + 1e-9:
                    raise BFieldTransportSafetyError(f"Condition {condition.name}: Vds exceeds verified G3 protection")
            else:
                channel = int(condition.ao_channel)
                if channel < 0:
                    raise BFieldTransportSafetyError("NI DAQ AO channel must be non-negative")
                if hasattr(daq, "get_max_output"):
                    limit = float(daq.get_max_output(channel))
                    if not math.isfinite(limit) or abs(float(condition.vds)) > abs(limit) + 1e-9:
                        raise BFieldTransportSafetyError(
                            f"Condition {condition.name}: Vds exceeds verified DAQ AO{channel} protection"
                        )

    def _on_fault(self, message):
        if self._active: self._fail(str(message))

    def on_lakeshore_snapshot(self, snapshot):
        """Feed this run's thermal evaluator explicitly from Lake Shore."""
        if self.thermal_safety is None:
            return
        try:
            self.thermal_safety.latest_snapshot = snapshot
        except Exception:
            pass
        if self._active:
            permitted, detail = self._thermal_permission(snapshot)
            if permitted:
                self._resume_after_thermal_recovery()
            elif isinstance(detail, str) and detail.startswith("Lake Shore thermal interlock"):
                self._fail(detail)
            elif not self._thermal_hold:
                self._request_thermal_pause()

    def on_lakeshore_disconnected(self):
        if self.thermal_safety is not None:
            try:
                self.thermal_safety.latest_snapshot = None
            except Exception:
                pass
        if self._active:
            self._fail("Lake Shore disconnected during transport sweep")

    def _fail(self, message):
        if not self._active: return
        self._log(f"ERROR: {message}")
        self.error.emit(str(message)); self._cleanup("failed", str(message))

    def _cleanup(self, status, detail=""):
        if not self._active and not self._claimed and not self._exclusive_acquired: return
        if self._cleanup_in_progress:
            return
        self._cleanup_in_progress = True
        self._cleanup_status, self._cleanup_detail = status, detail
        self._cleanup_failures = []
        self._cleanup_waiting = "pause"
        self._cleanup_overdue_phases = set()
        self._transition_pending = False
        self._transition_next = None
        self._cleanup_timer.start(self._cleanup_watchdog_ms())
        try:
            self.magnet.pause()
        except Exception as exc:
            self._cleanup_failures.append(f"pause cleanup failed: {exc}")
            self._begin_persistent_cleanup()
        for name in ("g1", "g2", "g3"):
            if name not in self._claimed: continue
            try:
                session = self.device_manager.get_session(name)
                if session is not None: safe_ramp(session.set_voltage, getattr(session, "voltage", 0.0) or 0.0, 0.0, 0.1, 0.02)
            except Exception as exc: self._cleanup_failures.append(f"{name} zero failed: {exc}")
        daq = self.device_manager.get_session("daq")
        if daq is not None and hasattr(daq, "ramp_voltage"):
            for channel in self._active_ao_channels:
                try: daq.ramp_voltage(channel, 0.0, 0.1, 0.02)
                except Exception as exc: self._cleanup_failures.append(f"DAQ ao{channel} zero failed: {exc}")
        self._log(f"Series {status}: {detail}")
        # Test doubles and synchronous adapters have no operation_finished
        # signal; complete their cleanup immediately while real controllers
        # retain reservations until both operations acknowledge completion.
        if not hasattr(self.magnet, "operation_finished"):
            try: self.magnet.enter_persistent_mode(zero_leads=True)
            except Exception as exc: self._cleanup_failures.append(f"persistent cleanup failed: {exc}")
            self._request_restore()

    def _log(self, message):
        if not self._log_path:
            return
        try:
            with open(self._log_path, "a", encoding="utf-8") as handle:
                handle.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {message}\n")
        except OSError:
            pass
