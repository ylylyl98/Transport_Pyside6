"""Acknowledged, transport-owned B excitation changes; A is never written.

OFF measurements are not certified thermometry. Per-point mode brackets each
acquisition with separate ON temperature windows, without mixing OFF samples.
"""
import math
import time
import uuid

from app.lakeshore_input_state import input_state_fresh, input_change_magnet_block
from utils.config import cfg


REVERSAL_COLUMNS = ("Sample_reversal_mode", "Sample_reversal_during_acquisition",
                    "Sample_temperature_quality", "Sample_ON_before_K", "Sample_ON_after_K",
                    "Sample_reversal_settle_s", "Sample_OFF_confirmed_monotonic_s",
                    "Sample_ON_confirmed_monotonic_s")


class SampleReversalMixin:
    def _reversal_mode(self):
        return getattr(getattr(getattr(self, "plan", None), "params", None), "sample_reversal_mode", "unchanged")

    def _reversal_preflight(self):
        if self._reversal_mode() == "unchanged":
            return
        ls = self.lakeshore_controller
        if ls is None or not callable(getattr(ls, "begin_measurement_input_session", None)):
            raise ValueError("LS335 measurement current-reversal support is required")
        if cfg.lakeshore335.sample_channel != "B" or cfg.lakeshore335.reservoir_channel != "A":
            raise ValueError("Automatic reversal requires sample B and magnet A")
        inputs = ls.input_configurations or {}
        if not input_state_fresh(ls.input_configurations_monotonic_s):
            raise ValueError("Refresh LS335 A/B inputs before automatic reversal")
        if inputs.get("A", {}).get("current_reversal") is not True:
            raise ValueError("Keep magnet channel A current reversal ON for automatic B modes")
        if not isinstance(inputs.get("B", {}).get("current_reversal"), bool):
            raise ValueError("Sample B must be a supported RTD input with verified reversal")

    def _reversal_start(self):
        if self._reversal_mode() == "unchanged":
            return False
        ls = self.lakeshore_controller
        self._reversal_preflight()
        original = ls.begin_measurement_input_session(self._owner)
        self._reversal_original = original["B"]["current_reversal"]
        self._reversal_session = True
        self._reversal_pending = None
        self._reversal_state = "starting"
        self._reversal_confirmed = None
        self._reversal_restore_done = False
        self._reversal_restore_error = None
        ls.control_result.connect(self._reversal_result)
        self.plan.validation["sample_reversal"] = dict(
            mode=self._reversal_mode(), original_b=self._reversal_original,
            a_required_on=True, settle_s=self.plan.params.sample_reversal_settle_s,
            temperature_accuracy="OFF thermometry unverified", restoration="pending")
        self._reversal_schedule(self._reversal_mode() == "per_point", "start")
        return True

    def _reversal_schedule(self, enabled, purpose):
        self._reversal_action = (bool(enabled), purpose)
        self._reversal_action_started = time.monotonic()
        self._reversal_waiting_fresh = False
        if not self._cleanup_in_progress:
            self._measurement_phase = "step_input_switch"
        self._refresh_snapshot()
        self.state_changed.emit("step_input_switch" if not self._cleanup_in_progress else "cleanup",
                                f"LS335 B {'ON' if enabled else 'OFF (DC)'}: {purpose}; waiting for confirmed readback")

    def _reversal_result(self, result):
        if (getattr(self, "_reversal_session", False) and not self._cleanup_in_progress
                and result.get("operation") == "read_input_configurations"):
            if not result.get("ok"):
                self._fail("LS335 input configuration monitoring failed")
            else:
                inputs = result.get("inputs", {})
                if inputs.get("A", {}).get("current_reversal") is not True:
                    self._fail("Magnet A current reversal no longer ON")
                elif (getattr(self, "_reversal_pending", None) is None
                      and self._reversal_confirmed is not None
                      and inputs.get("B", {}).get("current_reversal") is not self._reversal_confirmed):
                    self._fail("Sample B excitation changed outside the measurement sequence")
            return
        if (result.get("operation") != "measurement_current_reversal"
                or result.get("request_id") != getattr(self, "_reversal_pending", None)
                or not getattr(self, "_reversal_session", False)):
            return
        self._reversal_pending = None
        if not result.get("ok"):
            error = "LS335 B reversal failed: " + str(result.get("error", "readback failed"))
            self._reversal_action = None
            if self._cleanup_in_progress:
                self._reversal_restore_failed(error)
            else:
                self._fail(error)
            return
        enabled, purpose = self._reversal_action
        inputs = result.get("inputs", {})
        if (inputs.get("A", {}).get("current_reversal") is not True
                or inputs.get("B", {}).get("current_reversal") is not enabled):
            self._reversal_action = None
            if self._cleanup_in_progress:
                self._reversal_restore_failed("LS335 A/B readback mismatch")
            else:
                self._fail("LS335 A/B readback mismatch")
            return
        stamp = result.get("monotonic_s")
        if not isinstance(stamp, (float, int)) or not math.isfinite(stamp):
            self._reversal_action = None
            if self._cleanup_in_progress:
                self._reversal_restore_failed("LS335 reversal acknowledgement lacks timing")
            else:
                self._fail("LS335 reversal acknowledgement lacks timing")
            return
        self._reversal_confirmed = enabled
        self._reversal_cutoff = stamp
        self._reversal_waiting_fresh = True
        self._log(f"LS335 B reversal {'ON' if enabled else 'OFF'} verified ({purpose})")
        if not enabled:
            self._reversal_off_stamp = stamp
        else:
            self._reversal_on_stamp = stamp
        self.lakeshore_controller.refresh_snapshot()

    def _reversal_restore_failed(self, detail):
        self._reversal_restore_error = str(detail)
        self._cleanup_failures.append(str(detail))
        self.plan.validation["sample_reversal"]["restoration"] = "failed: " + str(detail)
        self.restoration_warning.emit(str(detail) + "; ownership retained; Stop retries restoration")
        self.state_changed.emit("cleanup_overdue", str(detail))
        self._cleanup_waiting = "sample_reversal"
        self._cleanup_timer.start(self._cleanup_watchdog_ms())

    def _reversal_tick(self, snapshot=None):
        if not getattr(self, "_reversal_session", False):
            return False
        if snapshot is not None:
            self._step_snapshot = snapshot
        now = time.monotonic()
        action = getattr(self, "_reversal_action", None)
        if action:
            if self._cleanup_in_progress and action[1] != "restore" and self._reversal_pending is None:
                # Cleanup may take longer than a switching deadline. Once the
                # old request is drained, restore directly; never execute a
                # still-unsent OFF or wait for obsolete acquisition conditions.
                self._reversal_schedule(self._reversal_original, "restore")
                return True
            if action[1] in {"acquire", "postcheck"} and not self._cleanup_in_progress:
                magnet = self._latest_magnet()
                if not self._reversal_field_ok(magnet, now):
                    self._fail("Field changed while switching B excitation")
                    return True
            if now - self._reversal_action_started > max(30., self.plan.params.sample_reversal_settle_s + 15.):
                if self._cleanup_in_progress:
                    if not getattr(self, "_reversal_restore_error", None):
                        self._reversal_restore_failed("LS335 B restoration timed out")
                else:
                    self._fail("LS335 B switching/fresh temperature timed out")
                return True
            if self._reversal_pending is not None:
                return True
            if not self._reversal_waiting_fresh:
                magnet = self._latest_magnet()
                if input_change_magnet_block(magnet, True):
                    self._refresh_snapshot()
                    return True
                request_id = uuid.uuid4().hex
                self._reversal_pending = request_id
                try:
                    if not self.lakeshore_controller.set_measurement_sample_reversal(self._owner, action[0], request_id):
                        raise RuntimeError("LS335 reversal transaction busy")
                except Exception as exc:
                    self._reversal_pending = None
                    self._reversal_action = None
                    if self._cleanup_in_progress:
                        self._reversal_restore_failed(str(exc))
                    else:
                        self._fail(str(exc))
                return True
            lake = self._latest_lakeshore()
            stamp = getattr(lake, "monotonic_s", -math.inf)
            if not (self._reversal_cutoff < stamp <= now and now-stamp <= cfg.lakeshore335.maximum_reading_age_s):
                return True
            permitted, detail = self._thermal_permission(lake)
            # Restoration must still complete during an existing thermal fault;
            # it restores sensor compensation, never authorizes field motion.
            if not permitted and not self._cleanup_in_progress:
                self._fail("Thermal protection during B reversal: " + str(getattr(detail, "reason", detail)))
                return True
            if now-self._reversal_cutoff < self.plan.params.sample_reversal_settle_s:
                return True
            self._reversal_action = None
            purpose = action[1]
            if self._cleanup_in_progress:
                if purpose != "restore":
                    self._reversal_schedule(self._reversal_original, "restore")
                    return True
                self._reversal_restore_done = True
                try:
                    self.lakeshore_controller.end_measurement_input_session(self._owner)
                except Exception as exc:
                    self._reversal_restore_failed(str(exc))
                    return True
                self.plan.validation["sample_reversal"]["restoration"] = "verified"
                self.lakeshore_controller.control_result.disconnect(self._reversal_result)
                self._reversal_session = False
                self._finish_cleanup()
            elif purpose == "start":
                self._reversal_state = "qualifying"
                self._position_at_start()
            elif purpose == "before_point":
                self._reversal_state = "qualifying"
                self._move_step()
            elif purpose == "acquire":
                self._reversal_state = "acquiring"
                self._step_window.reset()
                self._step_sample_started = now
                self._measurement_phase = "step_acquiring"
                self._last_acquisition = 0.
                magnet = self._latest_magnet()
                if not self._reversal_field_ok(magnet, now):
                    self._fail("Field changed during B excitation switching")
                    return True
                self._record_snapshot(magnet)
            elif purpose == "postcheck":
                self._reversal_state = "postcheck"
                # Requalify from ON readings only; retain the acquired row.
                self._measurement_phase = "step_settling"
                self._step_window.reset()
                self._step_paused_at = now
                self._step_field_values = []
                self._step_field_stamps = []
                self._step_qualified_since = None
                self._step_wait_started = now
                self._field_unready_since = self._temperature_unready_since = now
            return True
        if self._cleanup_in_progress:
            return False
        if self._reversal_mode() == "per_point" and self._reversal_state == "acquiring":
            magnet = self._latest_magnet()
            if not self._reversal_field_ok(magnet, now):
                self._step_acquisition_invalid = True
                self._fail("Field invalid during OFF acquisition")
                return True
            permitted, detail = self._thermal_permission()
            if not permitted:
                self._fail("Thermal protection during OFF acquisition: " + str(getattr(detail, "reason", detail)))
                return True
            if self._measurement_phase == "step_verifying":
                self._reversal_schedule(True, "postcheck")
            elif now-self._step_sample_started > max(180., float(cfg.magnet.operation_timeout_s)):
                self._fail("OFF acquisition timed out")
            return True
        return False

    def _reversal_check_inputs(self):
        if (not getattr(self, "_reversal_session", False) or self._cleanup_in_progress
                or getattr(self, "_reversal_pending", None) is not None):
            return True
        ls = self.lakeshore_controller
        if not input_state_fresh(ls.input_configurations_monotonic_s):
            self._fail("LS335 input configuration monitoring is stale")
            return False
        inputs = ls.input_configurations or {}
        if (inputs.get("A", {}).get("current_reversal") is not True
                or (self._reversal_confirmed is not None and
                    inputs.get("B", {}).get("current_reversal") is not self._reversal_confirmed)):
            self._fail("LS335 excitation state no longer matches the selected workflow")
            return False
        return True

    def _reversal_field_ok(self, magnet, now):
        if not self._step_field_valid(magnet, now) or not self._observe_point_history(magnet):
            return False
        values = self._step_field_values
        stamp, value = magnet.monotonic_s, magnet.field_t
        p = self.plan.params
        if not values or stamp > values[-1][0]:
            if values and stamp-values[-1][0] > 3.:
                return False
            values.append((stamp, value))
            while len(values) > 3 and values[1][0] <= stamp-p.field_stability_s:
                values.pop(0)
        if len(values) < 3:
            return False
        xs = [t-values[0][0] for t, _ in values]
        ys = [v for _, v in values]
        xm, ym = sum(xs)/len(xs), sum(ys)/len(ys)
        denom = sum((x-xm)**2 for x in xs)
        drift = sum((x-xm)*(y-ym) for x,y in zip(xs,ys))/denom if denom else math.inf
        span = max(ys)-min(ys)
        self._step_field_metrics = span, drift
        return span <= p.field_span_t+1e-12 and abs(drift) <= p.field_drift_t_per_s+1e-12

    def _reversal_before_acquire(self):
        if self._reversal_mode() != "per_point":
            return False
        if self._reversal_state == "postcheck":
            lake = self._latest_lakeshore()
            if abs(lake.sample_temperature_k-self._reversal_before_k) > self.plan.params.temperature_span_k:
                self._fail("ON temperature before/after OFF acquisition differs beyond maximum span; point not accepted")
                return True
            self._reversal_state = "verified"
            self._step_verify_sample(time.monotonic())
        else:
            self._reversal_before_k = self._latest_lakeshore().sample_temperature_k
            self._step_acquisition_invalid = False
            self._step_invalid_detail = None
            self._reversal_schedule(False, "acquire")
        return True

    def _reversal_before_move(self):
        if self._reversal_mode() == "per_point" and getattr(self, "_reversal_session", False):
            if self._reversal_pending is not None or getattr(self, "_reversal_action", None):
                return True
            if self._reversal_confirmed is not True:
                self._reversal_schedule(True, "before_point")
                return True
            self._reversal_state = "qualifying"
        return False

    def _reversal_row_metadata(self, row):
        mode = self._reversal_mode()
        if mode == "unchanged":
            return
        row.update(Sample_reversal_mode=mode, Sample_reversal_during_acquisition="OFF",
                   Sample_temperature_quality=("ON_bracketed_OFF_accuracy_unverified" if mode == "per_point" else "DC_stable_accuracy_unverified"),
                   Sample_ON_before_K=getattr(self, "_reversal_before_k", None) if mode == "per_point" else None,
                   Sample_ON_after_K=self._latest_lakeshore().sample_temperature_k if mode == "per_point" else None,
                   Sample_reversal_settle_s=self.plan.params.sample_reversal_settle_s,
                   Sample_OFF_confirmed_monotonic_s=getattr(self, "_reversal_off_stamp", None),
                   Sample_ON_confirmed_monotonic_s=getattr(self, "_reversal_on_stamp", None))

    def _reversal_cleanup(self):
        if not getattr(self, "_reversal_session", False):
            return False
        self._cleanup_waiting = "sample_reversal"
        self._cleanup_timer.start(200)
        # Never overwrite an in-flight request: its result must be drained before
        # queuing the final restore, preventing a late OFF after restoration.
        if not getattr(self, "_reversal_action", None) and not getattr(self, "_reversal_restore_error", None):
            self._reversal_schedule(self._reversal_original, "restore")
        self._reversal_tick()
        return True
