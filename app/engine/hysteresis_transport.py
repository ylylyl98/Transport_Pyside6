"""Acceptance and coverage policy for APS100 stepped hysteresis.

Readbacks are current-derived fields, not independently calibrated sample fields.
Readback reversals and coverage gaps are flagged; qualification timeouts restart
the stability wait. Other quality failures retain their stop rules.
"""
import json
import math
import time

from utils.config import cfg
from app.thermal_safety import _sensor_status_is_clear
from app.engine.aps100_rate_table import field_move_seconds, reduced_rate_table


RESOLUTION_T = 0.0001


class HysteresisTransportMixin:
    def _hysteresis_enabled(self):
        return self._is_stepped() and self.plan.params.magnet_backend == "1000"

    def _field_approach(self):
        from app.engine.field_approach import normalize_approach
        return normalize_approach(getattr(self.plan.params, "field_approach", "auto"),
                                  "no_overshoot" if self._is_stepped() else "linear",
                                  allow_hysteresis=self._hysteresis_enabled())

    def _strict_field_history(self):
        return self._hysteresis_enabled() and self._field_approach() in {"no_overshoot", "linear_no_backward"}

    def _point_is_endpoint(self):
        return self._step_index in {0, self._step_forward_count - 1, len(self._step_targets) - 1}

    def _point_acceptance(self):
        p = self.plan.params
        return p.field_endpoint_tolerance_t if self._point_is_endpoint() else p.field_acceptance_t

    def _hysteresis_bounds(self):
        from app.engine.bfield_transport_sweep import aps100_transport_field_limit
        hard = aps100_transport_field_limit()
        return (-hard, hard)

    def _hysteresis_move_options(self, *, initial=False):
        if not self._strict_field_history():
            return None
        p = self.plan.params
        low, high = self._hysteresis_bounds()
        if initial:
            # Preparation may start outside the measurement interval. The device
            # worker must extend this with its fresh starting read, not a UI cache.
            direction = getattr(self, "_initial_step_direction", 0)
            acceptance = p.field_endpoint_tolerance_t
        else:
            direction = self._step_direction
            acceptance = self._point_acceptance()
        options = dict(acceptance_t=acceptance, reversal_t=p.field_reversal_t,
                       lower_t=low, upper_t=high, no_progress_s=p.field_no_progress_s,
                       preparation=bool(initial))
        options["approach"] = self._field_approach()
        options["polling_profile"] = getattr(p, "aps100_polling_profile", "standard")
        options["no_overshoot_profile"] = getattr(p, "aps100_no_overshoot_profile", "standard")
        if initial:
            options.update(stability_s=p.field_stability_s, span_t=p.field_span_t,
                           drift_t_per_s=p.field_drift_t_per_s, stability_timeout_s=p.field_timeout_s)
        self._pending_hysteresis_options = options
        return options

    def _hysteresis_move_timeout(self, current, target, *, initial=False):
        expected = self._field_move_seconds(current, target)
        if not self._strict_field_history():
            return max(3600., expected * 2 + 180.)
        # The reduced-rate approach also performs serial rate-table writes,
        # readbacks and confirmed pauses. The old 30 s allowance stopped a
        # progressing 20 mT move after ~80 s. Reserve a full operation budget
        # for that overhead; worker no-progress/history guards remain active.
        # Keep a finite total bound even when progress messages keep arriving.
        overhead = max(180., float(cfg.magnet.operation_timeout_s))
        table = self.plan.validation["aps100_rates"]
        slow_rates = reduced_rate_table(table["normal"], table["coil_constant_t_per_a"], .2)
        slow_expected = field_move_seconds(current, target, slow_rates, table["coil_constant_t_per_a"])
        return max(expected * 5., slow_expected) + overhead + (90. if initial else 0.)

    def _quality_event(self, kind, payload):
        event = dict(kind=kind, condition_index=self._condition_index + 1,
                     point_index=getattr(self, "_step_index", 0) + 1,
                     monotonic_s=time.monotonic(), **payload)
        def clean(value):
            if isinstance(value, float) and not math.isfinite(value):
                return None
            if isinstance(value, dict):
                return {k: clean(v) for k, v in value.items()}
            if isinstance(value, (list, tuple)):
                return [clean(v) for v in value]
            return value
        detail = json.dumps(clean(event), ensure_ascii=False, default=str, allow_nan=False)
        self._log("HYSTERESIS " + detail)
        if not self._cleanup_in_progress:
            self._checkpoint_runtime(kind, detail)

    def _quality_failure(self, reason):
        self._quality_event("quality_failed", dict(reason=reason,
                            field_history=getattr(self, "_point_history", {}),
                            unaccepted_row=getattr(self, "_step_pending_row", None)))
        self._fail("Hysteresis quality failure: " + reason)
        return False

    def _quality_warning(self, reason, payload):
        history = self._point_history
        reasons = history.setdefault("quality_warning_reasons", [])
        if reason in reasons:
            return
        reasons.append(reason)
        summary = self.plan.validation.setdefault("field_quality_warnings", {"count": 0, "events": []})
        summary["count"] += 1
        if len(summary["events"]) < 100:
            summary["events"].append(dict(reason=reason, condition_index=self._condition_index + 1,
                                          point_index=self._step_index + 1, **payload))
        self._quality_event("field_quality_warning", dict(reason=reason, **payload))

    def _reversal_warning(self, reason, *, readback=None, peak=None, excursion=None, stamp=None):
        history = self._point_history
        history["reversal_exceeded"] = True
        event = dict(reason=reason, monotonic_s=time.monotonic() if stamp is None else stamp,
                     readback_t=readback, peak_t=peak, excursion_t=excursion,
                     limit_t=self.plan.params.field_reversal_t)
        # Each reason is recorded once per point; per-read repeats do not flood the log.
        reasons = history.setdefault("controller_reversal_reasons", [])
        if reason not in reasons:
            first_warning = not reasons
            reasons.append(reason)
            history.setdefault("controller_reversal_events", []).append(event)
            if first_warning:
                summary = self.plan.validation.setdefault("readback_reversal_warnings", {"point_count": 0, "events": []})
                summary["point_count"] += 1
                if len(summary["events"]) < 100:
                    summary["events"].append(dict(condition_index=self._condition_index + 1,
                                                  point_index=self._step_index + 1, **event))
                self._quality_event("readback_reversal_warning", {"observation": event})

    def _accept_move_history(self, result, *, initial=False):
        if not self._strict_field_history():
            return True
        history = result.get("field_history")
        if history:
            self._quality_event("move_history", dict(history=history, success=bool(result.get("success"))))
        if not result.get("success") and not result.get("cancelled") and not result.get("quality_overshoot"):
            return True  # worker errors use normal stop/cleanup, never accept data
        if initial and result.get("cancelled"):
            self._initial_field_history = {}
            return True  # a later preparation must independently qualify stability
        keys = ("min_t", "max_t", "start_t", "end_t", "max_reverse_t",
                "started_monotonic_s", "finished_monotonic_s", "read_count", "direction")
        invalid = (not isinstance(history, dict) or any(not isinstance(history.get(k), (int, float))
                or not math.isfinite(history[k]) for k in keys)
                or history["read_count"] < 2 or history["direction"] not in (-1, 1)
                or history.get("complete") is not True
                or (not initial and history["direction"] != self._step_direction))
        if invalid:
            held_cancellation = (result.get("cancelled") and
                ((result.get("cancellation_reason") == "monitor" and self._monitor_hold is not None) or
                 (result.get("cancellation_reason") == "thermal" and self._thermal_hold)))
            if not result.get("success") and not result.get("quality_overshoot") and not held_cancellation:
                return self._quality_failure("Interrupted move history is missing or invalid")
            # Do not derive field values from damaged diagnostics. A fresh,
            # acknowledged stationary observation establishes the baseline.
            if initial:
                self._initial_field_history = {}
                self._initial_history_uncertain = True
            else:
                prior = getattr(self, "_point_history", {})
                self._point_history = {"quality_warning_reasons": list(prior.get("quality_warning_reasons", [])),
                                       "reversal_exceeded": bool(prior.get("reversal_exceeded"))}
                self._history_stamp = -math.inf
                if hasattr(self, "_history_extreme"):
                    del self._history_extreme
                self._quality_warning("history_uncertain", {"detail": "Move history missing or invalid"})
            return True
        if initial:
            # Preparation is recorded above, but its motion is not part of the
            # strict acquisition branch that begins at the settled endpoint.
            if history.get("preparation_stable") is not True:
                return self._quality_failure("Preparation stability was not confirmed")
            end = history["end_t"]
            stamp = history["finished_monotonic_s"]
            self._initial_field_history = dict(direction=history["direction"], start_t=end, end_t=end,
                min_t=end, max_t=end, max_reverse_t=0., read_count=1,
                started_monotonic_s=stamp, finished_monotonic_s=stamp, complete=True)
            return True
        move_reversed = history["max_reverse_t"] > self.plan.params.field_reversal_t + 1e-12 or history.get("reversal_exceeded", False)
        between_reverse = 0.
        if not initial:
            previous = getattr(self, "_last_point_field", None)
            if previous is not None and getattr(self, "_last_point_direction", None) == history["direction"]:
                between_reverse = max(0., -self._step_direction * (history["start_t"] - previous))
        old = getattr(self, "_initial_field_history" if initial else "_point_history", {})
        combined = dict(history)
        if old and old.get("direction") in (-1, 1) and old["direction"] != history["direction"]:
            previous = old
            self._point_history = {"quality_warning_reasons": list(old.get("quality_warning_reasons", [])),
                                   "reversal_exceeded": bool(old.get("reversal_exceeded"))}
            self._quality_warning("history_uncertain", {"detail": "Prior diagnostic direction disagrees with move", "prior_history": previous})
            old = self._point_history
        if old and all(key in old for key in ("direction", "min_t", "max_t", "max_reverse_t", "started_monotonic_s", "start_t", "read_count")):
            direction = history["direction"]
            peak = old["max_t"] if direction > 0 else old["min_t"]
            adverse = history["min_t"] if direction > 0 else history["max_t"]
            combined["max_reverse_t"] = max(old["max_reverse_t"], history["max_reverse_t"], direction*(peak-adverse))
            combined["min_t"] = min(old["min_t"], history["min_t"])
            combined["max_t"] = max(old["max_t"], history["max_t"])
            combined["started_monotonic_s"] = old["started_monotonic_s"]
            combined["start_t"] = old["start_t"]
            combined["read_count"] += old["read_count"]
            combined["reversal_exceeded"] = bool(old.get("reversal_exceeded") or history.get("reversal_exceeded"))
            combined["reversal_events"] = (old.get("reversal_events", []) + history.get("reversal_events", []))[:100]
            combined["reversal_event_count"] = old.get("reversal_event_count", 0) + history.get("reversal_event_count", 0)
            combined["controller_reversal_reasons"] = list(old.get("controller_reversal_reasons", []))
            combined["controller_reversal_events"] = list(old.get("controller_reversal_events", []))
            combined["quality_warning_reasons"] = list(old.get("quality_warning_reasons", []))
        elif old:
            combined["quality_warning_reasons"] = list(old.get("quality_warning_reasons", []))
            combined["reversal_exceeded"] = bool(old.get("reversal_exceeded") or history.get("reversal_exceeded"))
        combined["max_reverse_t"] = max(combined["max_reverse_t"], between_reverse)
        if not initial and getattr(self, "_last_point_direction", None) == history["direction"]:
            last = getattr(self, "_last_point_history", {})
            direction = history["direction"]
            if last:
                combined["max_reverse_t"] = max(combined["max_reverse_t"], direction * (
                    last["max_t" if direction > 0 else "min_t"] - history["min_t" if direction > 0 else "max_t"]))
        if initial:
            self._initial_field_history = combined
        else:
            self._point_history = combined
            self._history_extreme = combined["max_t" if self._step_direction > 0 else "min_t"]
            p = self.plan.params
            low = min(p.start_field_t, p.stop_field_t) - p.field_endpoint_tolerance_t
            high = max(p.start_field_t, p.stop_field_t) + p.field_endpoint_tolerance_t
            if combined["min_t"] < low - 1e-12 or combined["max_t"] > high + 1e-12:
                self._quality_warning("scan_envelope_offset", {"min_t": combined["min_t"], "max_t": combined["max_t"],
                                      "start_t": p.start_field_t, "stop_t": p.stop_field_t})
        if move_reversed or combined["max_reverse_t"] > self.plan.params.field_reversal_t + 1e-12:
            self._reversal_warning("move_or_cumulative", readback=history["end_t"],
                                   excursion=combined["max_reverse_t"], stamp=history["finished_monotonic_s"])
        return True

    def _hysteresis_tick(self, snapshot=None):
        now = time.monotonic()
        phase = self._measurement_phase
        p = self.plan.params
        if snapshot is not None:
            self._step_snapshot = snapshot
        magnet, lake = self._step_snapshot, self._latest_lakeshore()
        permitted, detail = self._thermal_permission(lake)
        if not permitted:
            self._step_invalidate()
            if isinstance(detail, str):
                self._fail(detail)
            else:
                self._request_thermal_pause()
            return
        # Separate budgets: stable field must not consume the temperature's
        # longer wait. A total bound prevents alternating readiness forever.
        if phase == "step_settling":
            for attr, budget, reason in (("_field_unready_since", p.field_timeout_s, "Field stability timeout"),
                                        ("_temperature_unready_since", p.temperature_timeout_s, "Temperature stability timeout")):
                since = getattr(self, attr, self._step_wait_started)
                if since is not None and now - since >= budget:
                    self._step_timeout(reason=reason)
                    return
            if now - self._step_wait_started >= max(p.field_timeout_s, p.temperature_timeout_s) + p.field_settle_s:
                self._step_timeout(reason="Point qualification exceeded total time budget")
                return
        stamp, value = getattr(magnet, "monotonic_s", None), getattr(magnet, "field_t", None)
        fresh = (all(isinstance(v, (int, float)) and math.isfinite(v) for v in (stamp, value))
                 and self._step_paused_at < stamp <= now and now - stamp <= 3.)
        if fresh and self._strict_field_history() and not self._observe_point_history(magnet):
            return
        field_valid = self._step_field_valid(magnet, now)
        field_ready = False
        if field_valid:
            values = self._step_field_values
            if not values or stamp > values[-1][0]:
                if values and stamp - values[-1][0] > 3.:
                    values.clear()
                    self._step_qualified_since = None
                values.append((stamp, value))
                while len(values) > 3 and values[1][0] <= stamp - p.field_stability_s:
                    values.pop(0)
            self._step_field_stamps = [t for t, _ in values]
            span = max(v for _, v in values) - min(v for _, v in values)
            xmean = sum(t - values[0][0] for t, _ in values) / len(values)
            ymean = sum(v for _, v in values) / len(values)
            denominator = sum((t-values[0][0]-xmean)**2 for t, _ in values)
            drift = sum((t-values[0][0]-xmean)*(v-ymean) for t, v in values) / denominator if denominator else math.inf
            self._step_field_metrics = span, drift
            field_ready = (len(values) >= 3 and values[-1][0]-values[0][0] >= p.field_stability_s
                           and span <= p.field_span_t + 1e-12 and abs(drift) <= p.field_drift_t_per_s + 1e-12)
            self._step_outside_stamps = []
        else:
            self._step_field_values = []
            self._step_field_stamps = []
            if phase == "step_settling" and self._step_correct_field(magnet, now):
                return
        lake_stamp = getattr(lake, "monotonic_s", None)
        if (not isinstance(lake_stamp, (int, float)) or not math.isfinite(lake_stamp)
                or lake_stamp <= self._step_paused_at or not getattr(lake, "connected", False)
                or not getattr(lake, "communication_valid", False)
                or not _sensor_status_is_clear(getattr(lake, "sample_sensor_status", None))):
            self._step_window.reset()
            temperature_ready = False
        else:
            temperature_ready = self._step_window.observe(lake_stamp, getattr(lake, "sample_temperature_k", None), now)
        for ready, attr in ((field_ready, "_field_unready_since"), (temperature_ready, "_temperature_unready_since")):
            if ready:
                setattr(self, attr, None)
            elif getattr(self, attr, None) is None:
                setattr(self, attr, now)
        qualified = field_ready and temperature_ready
        if not qualified:
            self._step_qualified_since = None
            if phase in {"step_acquiring", "step_verifying"}:
                if not self._step_acquisition_invalid:
                    self._step_invalid_detail = self._step_diagnostics(now)
                self._step_acquisition_invalid = True
        elif self._step_qualified_since is None:
            self._step_qualified_since = now
        if qualified and phase == "step_settling" and now-self._step_qualified_since >= p.field_settle_s:
            if getattr(self, "_step_overshoot_pending", False) and not self._resolve_qualified_overshoot(value):
                return
            if not self._check_actual_point(value):
                return
            if self._reversal_before_acquire():
                return
            self._step_acquisition_invalid = False
            self._step_invalid_detail = None
            self._step_sample_started = now
            self._measurement_phase = "step_acquiring"
            self._last_acquisition = 0.
            self._record_snapshot(magnet)
            return
        if phase == "step_verifying":
            self._step_verify_sample(now)
        if self._measurement_phase == "step_settling" and now-self._step_diagnostic_at >= 30.:
            self._step_diagnostic_at = now
            self._log("Waiting for hysteresis stability: " + self._step_diagnostics(now))
        if self._measurement_phase in {"step_acquiring", "step_verifying"} and now-self._step_sample_started > self._acquisition_timeout_s():
            self._fail("Stepped acquisition or post-acquisition telemetry timed out")

    def _observe_point_history(self, snapshot):
        stamp, value = snapshot.monotonic_s, snapshot.field_t
        if stamp <= getattr(self, "_history_stamp", -math.inf):
            return True
        self._history_stamp = stamp
        low, high = self._hysteresis_bounds()
        if not low - 1e-12 <= value <= high + 1e-12:
            return self._quality_failure("Field exceeded the commissioned field limit")
        p = self.plan.params
        if value < min(p.start_field_t, p.stop_field_t) - p.field_endpoint_tolerance_t - 1e-12 or value > max(p.start_field_t, p.stop_field_t) + p.field_endpoint_tolerance_t + 1e-12:
            self._quality_warning("scan_envelope_offset", {"readback_t": value,
                                  "start_t": p.start_field_t, "stop_t": p.stop_field_t})
        history = self._point_history
        direction = self._step_direction
        if "start_t" not in history:
            history.update(direction=direction, start_t=value, end_t=value, min_t=value, max_t=value,
                           max_reverse_t=0., read_count=0, started_monotonic_s=stamp, complete=True)
        extreme = getattr(self, "_history_extreme", value)
        reverse = max(0., direction * (extreme - value))
        history["min_t"] = min(history.get("min_t", value), value)
        history["max_t"] = max(history.get("max_t", value), value)
        history["max_reverse_t"] = max(history.get("max_reverse_t", 0.), reverse)
        history["end_t"] = value
        history["finished_monotonic_s"] = stamp
        history["read_count"] = history.get("read_count", 0) + 1
        self._history_extreme = max(extreme, value) if direction > 0 else min(extreme, value)
        if reverse > self.plan.params.field_reversal_t + 1e-12:
            self._reversal_warning("post_move", readback=value, peak=extreme, excursion=reverse, stamp=stamp)
        return True

    def _maximum_actual_gap(self):
        return self.plan.params.field_max_gap_t or 2 * max(abs(b-a) for a, b in zip(self._step_targets, self._step_targets[1:]))

    def _check_actual_point(self, value):
        if abs(value - self._target) > self._point_acceptance() + 1e-12 and not (getattr(self, "_step_overshoot_pending", False) and self._point_is_endpoint()):
            return self._quality_failure("Stationary readback exceeds this point's acceptance limit")
        previous = self._last_point_field
        if previous is not None:
            progress = self._step_direction * (value - previous)
            if abs(progress) < RESOLUTION_T - 1e-12:
                self._skip_duplicate_point(value)
                return False
            if progress < 0:
                self._point_history["max_reverse_t"] = max(self._point_history.get("max_reverse_t", 0.), -progress)
                self._reversal_warning("backward_point", readback=value, peak=previous, excursion=-progress)
            if progress > self._maximum_actual_gap() + 1e-12:
                self._quality_warning("coverage_gap", dict(previous_t=previous, readback_t=value,
                                      gap_t=progress, limit_t=self._maximum_actual_gap()))
        return True

    def _resolve_qualified_overshoot(self, value):
        # Move only toward a later nominal target. Endpoint readings retain the
        # requested target and are acquired at their actual stable value.
        skipped = []
        skipped_count = 0
        while not self._point_is_endpoint() and self._step_direction * (self._step_targets[self._step_index] - value) < RESOLUTION_T - 1e-12:
            event = dict(condition_index=self._condition_index + 1, point_index=self._step_index + 1,
                         target_t=self._step_targets[self._step_index], readback_t=value,
                         reason="overshot", endpoint=False)
            skipped_count += 1
            if len(skipped) < 100:
                skipped.append(event)
            self._step_index += 1
        if skipped_count:
            summary = self.plan.validation.setdefault("skipped_quality_points", {"count": 0, "events": []})
            summary["count"] += skipped_count
            summary["events"].extend(skipped[:max(0, 100 - len(summary["events"]))])
            self._quality_event("points_skipped_overshot", {"skipped": skipped, "count": skipped_count})
        self._target = self._step_targets[self._step_index]
        if self._point_is_endpoint() and self._step_direction * (value - self._target) > 0:
            self._quality_warning("endpoint_offset", {"target_t": self._target, "readback_t": value})
            return True
        self._step_overshoot_pending = False
        self._carry_point_history = dict(self._point_history)
        self._move_step()
        return False

    def _advance_hysteresis_target(self, value):
        # Keep the original grid. Only skip passed interior nominal targets;
        # turning points and the final endpoint may never be silently skipped.
        skipped = []
        skipped_count = 0
        while self._step_index < len(self._step_targets):
            if self._point_is_endpoint():
                break
            delta = self._step_targets[self._step_index] - self._step_targets[self._step_index - 1]
            direction = 1 if delta > 0 else -1
            if direction * (self._step_targets[self._step_index] - value) >= RESOLUTION_T - 1e-12:
                break
            skipped_count += 1
            if len(skipped) < 100:
                skipped.append(dict(index=self._step_index + 1, target_t=self._step_targets[self._step_index]))
            self._step_index += 1
        self._skipped_nominal = skipped
        if skipped_count:
            summary = self.plan.validation.setdefault("skipped_quality_points", {"count": 0, "events": []})
            summary["count"] += skipped_count
            events = [dict(condition_index=self._condition_index + 1, point_index=item["index"],
                           target_t=item["target_t"], readback_t=value, reason="passed", endpoint=False)
                      for item in skipped]
            summary["events"].extend(events[:max(0, 100 - len(summary["events"]))])
            self._quality_event("nominal_targets_crossed", dict(readback_t=value, skipped=skipped, count=skipped_count))
