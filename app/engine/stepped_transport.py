"""Stepped acquisition states for the shared transport controller.

All magnet commands retain the controller's reservation, interlocks and cleanup.
Optional B excitation sequencing is delegated to the owned reversal session.
"""
import math
import time

from app.engine.transport_stability import TemperatureWindow
from app.engine.bfield_transport_sweep import build_stepped_targets, normalize_cooldown_policy
from app.thermal_safety import _sensor_status_is_clear
from utils.config import cfg
from app.engine.hysteresis_transport import HysteresisTransportMixin
from app.engine.sample_reversal import SampleReversalMixin, REVERSAL_COLUMNS


STEPPED_FIELD_TOLERANCE_T = 0.010  # 2100 retains its existing tolerance


STEP_COLUMNS = ("Field_point_index", "Temperature_target_K", "Temperature_window_s",
                "Temperature_span_K", "Temperature_drift_K_per_min",
                "B_after_T", "Sample_temperature_after_K", "B_before_T",
                "B_readback_min_T", "B_readback_max_T", "B_max_reverse_T",
                "Field_history_started_monotonic_s", "Field_history_finished_monotonic_s",
                "Field_readback_count", "Field_gap_T", "Field_quality", "Skipped_nominal_points",
                "Field_window_span_T", "Field_window_drift_T_per_s") + REVERSAL_COLUMNS


class SteppedTransportMixin(SampleReversalMixin, HysteresisTransportMixin):
    def _is_stepped(self):
        return getattr(getattr(getattr(self, "plan", None), "params", None), "measurement_mode", "continuous") == "stepped"

    def _reset_step_condition(self):
        targets = build_stepped_targets(self.plan.params)
        self._step_targets = targets + (targets[-2::-1] if self.plan.params.round_trip else ())
        self._step_forward_count = len(targets)
        self._step_index = 0
        self._step_window = TemperatureWindow(self.plan.params, float(cfg.lakeshore335.maximum_reading_age_s))
        self._step_snapshot = None
        self._step_pending_row = None
        self._step_move_inflight = False
        self._step_qualified_since = None
        self._last_point_field = None
        self._last_point_history = {}
        self._last_point_direction = None
        self._history_point_identity = None
        self._point_history = {}
        self._skipped_nominal = []

    def _move_step(self, *, reposition=False):
        if not self._active or self._cleanup_in_progress or self._thermal_hold or self._monitor_hold is not None:
            return
        if self._reversal_before_move():
            return
        if self._hysteresis_enabled() and getattr(self, "_hysteresis_history_gap", False):
            self._quality_failure("Magnet telemetry gap left field history unverified; restart a prepared loop")
            return
        # A manual continuation cannot queue a second acquisition behind a
        # read that was interrupted by a protection or monitoring hold.
        if self._sample_pending:
            self._measurement_phase = "step_retry"
            return
        aps = self.plan.params.magnet_backend == "1000"
        if aps:
            if self._step_index == 0:
                direction = getattr(self, "_initial_step_direction", 0)
            else:
                delta = self._step_targets[self._step_index] - self._step_targets[self._step_index - 1]
                direction = 1 if delta > 0 else -1
            self._step_direction = direction
            identity = (self._condition_index, self._step_index)
            if self._history_point_identity != identity:
                self._history_point_identity = identity
                self._point_history = (dict(getattr(self, "_initial_field_history", {}))
                                       if self._step_index == 0 else {})
                self._history_stamp = -math.inf
                if hasattr(self, "_history_extreme"):
                    del self._history_extreme
        self._step_repositioning = reposition
        if not reposition:
            self._step_corrections = 0
        self._step_pending_row = None
        self._step_window.reset()
        self._step_snapshot = None
        self._target = self._step_targets[self._step_index]
        self._leg_index = int(self._step_index >= self._step_forward_count)
        self._measurement_phase = "step_moving"
        self._phase_started = time.monotonic()
        latest = self._latest_magnet()
        current = float(getattr(latest, "field_t", self._target))
        expected = self._field_move_seconds(current, self._target)
        self._step_move_timeout = self._hysteresis_move_timeout(current, self._target)
        if reposition:
            budget = self.plan.params.field_timeout_s if aps else self.plan.params.temperature_timeout_s
            remaining = budget - (self._phase_started - self._step_wait_started)
            if remaining <= 0:
                self._step_timeout()
                return
            self._step_move_timeout = min(self._step_move_timeout, remaining)
        detail = f"Point {self._step_index + 1}/{len(self._step_targets)}: moving to {self._target:+.6f} T"
        self.state_changed.emit("step_moving", detail)
        self._checkpoint_runtime("step_moving", detail)
        try:
            self._step_move_inflight = True
            direction_kwargs = {"monotonic_direction": self._step_direction,
                                "hysteresis_options": self._hysteresis_move_options()} if aps else {}
            tolerance = self._step_position_tolerance()
            if aps:
                tolerance = min(self.plan.params.field_tolerance_t, self._point_acceptance())
                if self._last_point_field is not None:
                    # A small grid interval must not be swallowed by the normal
                    # stopping tolerance and return the preceding sample again.
                    tolerance = min(tolerance, abs(self._target-self._last_point_field) / 2.)
            self.magnet.safe_move_to_field(
                self._target, final_mode="driven", zero_leads=False,
                persistent_field_confirmed=self._persistent_confirmation_granted,
                tolerance_t=tolerance,
                timeout_s=self._step_move_timeout, **direction_kwargs)
        except Exception as exc:
            self._step_move_inflight = False
            self._fail(f"Stepped field move failed: {exc}")

    def _step_position_tolerance(self):
        return float(self._point_acceptance()) if self.plan.params.magnet_backend == "1000" else STEPPED_FIELD_TOLERANCE_T

    def _step_move_ready(self):
        self._measurement_phase = "step_pausing"
        self._phase_started = time.monotonic()
        try:
            self.magnet.pause()
        except Exception as exc:
            self._fail(f"Stepped field pause failed: {exc}")
        # Production always supplies operation_finished. Do not infer a
        # confirmed pause from a successful move or an absent signal.

    def _step_pause_ready(self):
        now = time.monotonic()
        self._measurement_phase = "step_settling"
        self._phase_started = self._step_paused_at = now
        if not self._step_repositioning:
            self._step_wait_started = now
            self._field_unready_since = self._temperature_unready_since = now
        self._step_window.reset()
        self._step_field_stamps = []
        self._step_field_values = []
        self._step_qualified_since = None
        self._step_outside_stamps = []
        self._step_diagnostic_at = now
        self._step_invalid_detail = None
        self._step_snapshot = None
        self._step_pending_row = None
        self._step_field_metrics = (0., 0.)
        self._set_transport_polling(True)
        self._refresh_snapshot()
        detail = (f"Point {self._step_index + 1}/{len(self._step_targets)}: waiting for stationary field "
                  f"within {self._step_position_tolerance()*1000:g} mT and {self.plan.params.temperature_target_k:g} K stability")
        self.state_changed.emit("step_settling", detail)
        self._checkpoint_runtime("step_settling", detail)

    def _step_field_valid(self, snapshot, now, *, check_target=True):
        stamp = getattr(snapshot, "monotonic_s", None)
        value = getattr(snapshot, "field_t", None)
        status = getattr(snapshot, "status", None)
        return (isinstance(stamp, (float, int)) and math.isfinite(stamp)
                and self._step_paused_at < stamp <= now and now - stamp <= 3.0
                and isinstance(value, (float, int)) and math.isfinite(value)
                and (not check_target or abs(value - self._target) <= self._step_position_tolerance() + 1e-12)
                and getattr(status, "sweep_active", None) is False
                and (self.plan.params.magnet_backend != "1000" or any(word in str(getattr(snapshot, "sweep_state", "")).lower() for word in ("pause", "standby")))
                and getattr(snapshot, "heater_on", None) is True
                and not any(getattr(status, key, False) for key in ("quench", "power_module_failure", "faulted")))

    def _step_diagnostics(self, now):
        magnet, lake = self._step_snapshot, self._latest_lakeshore()
        def number(value, unit):
            return f"{value:.6g} {unit}" if isinstance(value, (int, float)) and math.isfinite(value) else "unavailable"
        def age(snapshot):
            stamp = getattr(snapshot, "monotonic_s", None)
            return number(now - stamp, "s") if isinstance(stamp, (int, float)) else "unavailable"
        field = getattr(magnet, "field_t", None)
        error = abs(field - self._target) * 1000 if isinstance(field, (int, float)) else None
        elapsed, span, drift = self._step_window.metrics()
        p = self.plan.params
        return (f"B={number(field, 'T')}, error={number(error, 'mT')} (limit {self._step_position_tolerance()*1000:g} mT), "
                f"age={age(magnet)}, sweep_active={getattr(getattr(magnet, 'status', None), 'sweep_active', None)}, "
                f"switch_heater_on={getattr(magnet, 'heater_on', None)}; "
                f"sample={number(getattr(lake, 'sample_temperature_k', None), 'K')}, "
                f"target={p.temperature_target_k:g} +/- {p.temperature_tolerance_k:g} K, age={age(lake)}, "
                f"sensor={getattr(lake, 'sample_sensor_status', None)}, "
                f"communication_valid={getattr(lake, 'communication_valid', None)}; "
                f"window={elapsed:.3g}/{p.temperature_window_s:g} s, "
                f"span={span:.4g}/{p.temperature_span_k:g} K, "
                f"drift={drift:.4g} K/min (absolute limit {p.temperature_drift_k_per_min:g})")

    def _step_invalidate(self):
        if self._measurement_phase in {"step_acquiring", "step_verifying"}:
            if not self._step_acquisition_invalid:
                self._step_invalid_detail = self._step_diagnostics(time.monotonic())
            self._step_acquisition_invalid = True
        self._step_window.reset()
        self._step_field_stamps = []
        self._step_field_values = []
        self._step_qualified_since = None

    def _step_overshoot_hold(self):
        self._target = self._step_targets[self._step_index]
        if self._hysteresis_enabled():
            self._quality_failure("Approach exceeded permitted acceptance; no backwards correction")
            return
        self._step_timeout(overshoot=True)

    def _step_correct_field(self, magnet, now):
        if self._reversal_mode() == "per_point" and getattr(self, "_reversal_state", None) == "postcheck":
            self._fail("Field changed during post-acquisition ON verification; point not accepted")
            return True
        # Only confirmed, fresh, stationary driven readings may trigger another
        # normal protected move. Cached reads and safety faults never do so.
        if (self._measurement_phase != "step_settling" or self._sample_pending
                or self._step_pending_row is not None
                or not self._step_field_valid(magnet, now, check_target=False)):
            self._step_outside_stamps = []
            return False
        if self.plan.params.magnet_backend == "1000" and self._step_direction * (magnet.field_t - self._target) > self._step_position_tolerance():
            self._step_overshoot_hold()
            return True
        stamps = self._step_outside_stamps
        if not stamps or magnet.monotonic_s > stamps[-1]:
            stamps.append(magnet.monotonic_s)
        if len(stamps) < 3:
            return False
        budget = self.plan.params.field_timeout_s if self._hysteresis_enabled() else self.plan.params.temperature_timeout_s
        if now - self._step_wait_started >= budget:
            self._step_timeout()
        else:
            self._step_corrections += 1
            self._log(f"Repositioning same stepped point (attempt {self._step_corrections}): "
                      + self._step_diagnostics(now))
            self._move_step(reposition=True)
        return True

    def _step_tick(self, snapshot=None):
        if (not self._is_stepped() or not self._active or self._cleanup_in_progress
                or self._thermal_hold or self._monitor_hold is not None):
            return
        if self._reversal_tick(snapshot):
            return
        phase = self._measurement_phase
        if phase == "step_retry":
            self._move_step()
            return
        if phase not in {"step_settling", "step_acquiring", "step_verifying"}:
            return
        if self._hysteresis_enabled():
            self._hysteresis_tick(snapshot)
            return
        now = time.monotonic()
        if snapshot is not None:
            self._step_snapshot = snapshot
        magnet = self._step_snapshot
        lake = self._latest_lakeshore()
        permitted, detail = self._thermal_permission(lake)
        if not permitted:
            self._step_invalidate()
            if isinstance(detail, str):
                self._fail(detail)
            else:
                self._request_thermal_pause()
            return
        if phase == "step_settling" and now - self._step_wait_started >= self.plan.params.temperature_timeout_s:
            self._step_timeout()
            return
        if not self._step_field_valid(magnet, now):
            self._step_invalidate()
            if self._step_correct_field(magnet, now):
                return
        else:
            self._step_outside_stamps = []
            stamp = magnet.monotonic_s
            if not self._step_field_stamps or stamp > self._step_field_stamps[-1]:
                if self._step_field_stamps and stamp - self._step_field_stamps[-1] > 3.0:
                    self._step_field_stamps = []
                    self._step_field_values = []
                    self._step_qualified_since = None
                self._step_field_stamps.append(stamp)
                self._step_field_stamps = self._step_field_stamps[-3:]
            lake_stamp = getattr(lake, "monotonic_s", None)
            if (not isinstance(lake_stamp, (int, float)) or not math.isfinite(lake_stamp)
                    or lake_stamp <= self._step_paused_at
                    or not getattr(lake, "connected", False)
                    or not getattr(lake, "communication_valid", False)
                    or not _sensor_status_is_clear(getattr(lake, "sample_sensor_status", None))):
                self._step_invalidate()
            elif len(self._step_field_stamps) >= 3:
                ready = self._step_window.observe(lake_stamp, getattr(lake, "sample_temperature_k", None), now)
                if phase in {"step_acquiring", "step_verifying"} and not ready:
                    if not self._step_acquisition_invalid:
                        self._step_invalid_detail = self._step_diagnostics(now)
                    self._step_acquisition_invalid = True
                if ready and phase == "step_settling":
                    self._step_acquisition_invalid = False
                    self._step_invalid_detail = None
                    self._step_sample_started = now
                    self._measurement_phase = "step_acquiring"
                    self._last_acquisition = 0.
                    self._record_snapshot(magnet)
                    return
        if phase == "step_verifying":
            self._step_verify_sample(now)
        if self._measurement_phase == "step_settling" and now - self._step_diagnostic_at >= 30.:
            self._step_diagnostic_at = now
            self._log("Waiting for stepped stability: " + self._step_diagnostics(now))
        if self._measurement_phase == "step_settling" and now - self._step_wait_started >= self.plan.params.temperature_timeout_s:
            self._step_timeout()
        elif self._measurement_phase in {"step_acquiring", "step_verifying"}:
            if now - self._step_sample_started > max(180., float(cfg.magnet.operation_timeout_s)):
                self._fail("Stepped acquisition or post-acquisition telemetry timed out")

    def _step_timeout(self, *, overshoot=False):
        if self._hysteresis_enabled():
            self._quality_failure("Field correction or qualification timed out")
            return
        diagnostics = self._step_diagnostics(time.monotonic())
        self._step_invalidate()
        self._thermal_hold = True
        self._thermal_manual_required = True
        self._thermal_resume_action = "step"
        self._measurement_phase = "thermal_hold"
        self._monitor_generation += 1
        detail = (("Directional overshoot; no reverse move will be commanded. " if overshoot else "Stability timeout. ") + f"At {self._target:+.6f} T / {self.plan.params.temperature_target_k:g} K. "
                  f"{diagnostics}. "
                  "No point accepted. Field remains paused; Continue retries this point, Stop ends the run.")
        self._log(detail)
        self.state_changed.emit("thermal_hold", detail)
        self._checkpoint_runtime("thermal_hold", detail)
        self._resume_after_thermal_recovery()

    def _step_sample_ready(self, row):
        if (not self._active or self._cleanup_in_progress or self._thermal_hold
                or self._monitor_hold is not None
                or row.get("_monitor_generation") != self._monitor_generation):
            if self._hysteresis_enabled():
                self._quality_event("interrupted_acquisition", dict(row=row, field_history=getattr(self, "_point_history", {})))
            self._log("Discarded stepped acquisition interrupted by stop or protection hold")
            return
        self._step_pending_row = row
        self._step_sample_finished = time.monotonic()
        self._measurement_phase = "step_verifying"
        self._refresh_snapshot()

    def _step_verify_sample(self, now):
        lake, magnet = self._latest_lakeshore(), self._step_snapshot
        # New observations AFTER acquisition must bracket the measurement.
        if any(getattr(s, "monotonic_s", -1) <= self._step_sample_finished for s in (lake, magnet)):
            return
        row = self._step_pending_row
        if row is None:
            return
        if self._step_acquisition_invalid or not self._step_field_valid(magnet, now) or not self._step_window.ready():
            if self._hysteresis_enabled():
                self._quality_event("unaccepted_acquisition", dict(row=row, field_history=self._point_history))
            self._step_pending_row = None
            self._log("Discarded stepped point: field/temperature changed during acquisition; requalifying. "
                      + (self._step_invalid_detail or self._step_diagnostics(now)))
            self._measurement_phase = "step_settling"
            self._step_window.reset()
            return
        if self._hysteresis_enabled():
            if not self._check_actual_point(magnet.field_t):
                return
            history = self._point_history
            row.update(B_before_T=row.get("B_measured_T"),
                       B_readback_min_T=history.get("min_t"), B_readback_max_T=history.get("max_t"),
                       B_max_reverse_T=history.get("max_reverse_t"),
                       Field_history_started_monotonic_s=history.get("started_monotonic_s"),
                       Field_history_finished_monotonic_s=history.get("finished_monotonic_s"),
                       Field_readback_count=history.get("read_count"),
                       Field_gap_T=abs(row["B_measured_T"]-self._last_point_field) if self._last_point_field is not None else None,
                       Field_quality="accepted_offset" if abs(row["B_measured_T"]-self._target) > self.plan.params.field_tolerance_t + 1e-12 else "accepted",
                       Skipped_nominal_points=",".join(str(item["index"]) for item in self._skipped_nominal),
                       Field_window_span_T=self._step_field_metrics[0], Field_window_drift_T_per_s=self._step_field_metrics[1])
        self._step_pending_row = None
        elapsed, span, drift = self._step_window.metrics()
        row.update(Field_point_index=self._step_index + 1,
                   Temperature_target_K=self.plan.params.temperature_target_k,
                   Temperature_window_s=elapsed, Temperature_span_K=span,
                   Temperature_drift_K_per_min=drift, B_after_T=magnet.field_t,
                   Sample_temperature_after_K=lake.sample_temperature_k)
        self._reversal_row_metadata(row)
        self._commit_sample(row)
        if not self._active or self._cleanup_in_progress:
            return
        self._step_index += 1
        if self._hysteresis_enabled():
            self._last_point_field = row["B_measured_T"]
            self._last_point_history = dict(self._point_history)
            self._last_point_direction = self._step_direction
            self._advance_hysteresis_target(magnet.field_t)
        if self._step_index < len(self._step_targets):
            self._move_step()
        elif (self._condition_index < len(self.plan.conditions) - 1
              and normalize_cooldown_policy(self.plan.params.cooldown_policy) == "persistent_each_row"):
            self._begin_row_persistent_transition()
        else:
            self._next_condition()

    def _step_watchdog(self):
        phase = self._measurement_phase
        if not phase.startswith("step_"):
            return False
        now = time.monotonic()
        if (getattr(self, "_step_repositioning", False) and phase in {"step_moving", "step_pausing"}
                and now - self._step_wait_started >= (self.plan.params.field_timeout_s if self._hysteresis_enabled() else self.plan.params.temperature_timeout_s)):
            # Normal failure cleanup requests Stop/pause and drains the worker;
            # never label a still-unacknowledged correction as safely paused.
            self._fail("Stepped field correction exceeded the point wait deadline")
            return True
        if phase == "step_moving":
            # safe_move owns the APS worker and polls status/thermal interlocks
            # internally, just like initial positioning. Normal snapshot polling
            # resumes after its result; old snapshots never qualify a point.
            if now - self._phase_started > self._step_move_timeout:
                self._fail("No acknowledgement in step_moving")
            return True
        last = self._last_telemetry_monotonic
        if last is not None and now - last > self._communication_timeout_s():
            self._begin_monitor_hold("APS100 telemetry timeout during stepped sweep")
            return True
        if phase == "step_pausing":
            timeout = self._communication_timeout_s()
            if now - self._phase_started > timeout:
                self._fail(f"No acknowledgement in {phase}")
            return True
        self._step_tick()
        return True
