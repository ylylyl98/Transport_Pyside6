"""GUI-thread orchestration for Gate Scan measurements at persistent B fields."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import json
import math
import os
import time
import uuid

from PyQt6 import QtCore, QtWidgets

from app.models import LineSweepParams
from app.run_output import PlannedOutput, field_output_tag, output_blocking_reason, to_jsonable
from utils.config import cfg


def _system_local_iso(*, timespec="seconds"):
    """Return an aware timestamp in the PC's configured local timezone."""
    return datetime.now().astimezone().isoformat(timespec=timespec)


@dataclass(frozen=True)
class GateScanBatchRequest:
    fields_t: tuple[float, ...]
    params: LineSweepParams
    required_devices: tuple[str, ...]
    batch_id: str
    captured_at: str
    calibration: tuple | None = None
    condition_index: int = 0
    condition_name: str = ""


@dataclass
class _BatchState:
    request: GateScanBatchRequest | None = None
    index: int = 0
    phase: str = "idle"
    stop_requested: bool = False
    move_request_id: str | None = None
    audit: object = None
    results: list[dict] = field(default_factory=list)
    jobs: tuple[tuple[GateScanBatchRequest, float], ...] = ()


class GateScanFieldBatch(QtCore.QObject):
    """Serialize APS moves and Gate Scan workers for one or more conditions.

    Requests are flattened field-major (all saved conditions at field 1, then
    all saved conditions at field 2, and so on), while each request remains
    frozen for metadata, collision checks, and checkpoint records.
    """

    state_changed = QtCore.pyqtSignal(str, str)
    progress_changed = QtCore.pyqtSignal(int, int, str)
    # Structured events are consumed by the B-field tab and also persisted in
    # the series log.  The existing signals remain for compatibility.
    activity = QtCore.pyqtSignal(object)
    error = QtCore.pyqtSignal(str)
    finished = QtCore.pyqtSignal()
    stopped = QtCore.pyqtSignal(str)

    # Keep the expansion bounded even when a typo such as ``0:1000000:0.1``
    # is entered.  This is a limit on the complete ordered series, including
    # scalar entries and ranges mixed in the same input.
    MAX_EXPANDED_FIELDS = 10_000

    def __init__(self, magnet1000, gate_scan_tab, parent=None, selected_backend_callable=None,
                 thermal_safety=None, lakeshore_controller=None, temperature_interlock=None):
        super().__init__(parent)
        self.magnet = magnet1000
        self.tab = gate_scan_tab
        self._selected_backend = selected_backend_callable or (lambda: "1000")
        self.thermal_safety = thermal_safety or temperature_interlock
        self.lakeshore_controller = lakeshore_controller
        self._state = _BatchState()
        self._owner = "gate-scan-bfield-batch"
        self._snapshot = None
        self._snapshot_received_at = None
        self._checkpoint_path = None
        self._batch_log_path = None
        self._base_output_dir = None
        self._base_display_stem = None
        self._review_valid = lambda: True
        self._series_id = None
        self._persistent_confirmation_granted = False
        self._last_transition_log_at = None
        self._last_transition_log_key = None
        self._last_transition_label = None
        self._last_measurement_progress_at = None
        self._last_snapshot_log_at = None
        self._last_snapshot_log_key = None
        self._last_snapshot_phase_key = None
        self._cooldown_started_at = None
        self._thermal_wait_context = None
        self._last_persistent_field_t = None
        self._move_started_from_persistent = False
        self._temperature_telemetry_path = None
        self._thermal_observed = {
            "sample_min_k": None, "sample_max_k": None,
            "reservoir_min_k": None, "reservoir_max_k": None,
            "maximum_positive_sample_slope_k_per_min": None,
            "maximum_positive_reservoir_slope_k_per_min": None,
        }
        self._cooldown_timer = QtCore.QTimer(self)
        self._cooldown_timer.setInterval(250)
        self._cooldown_timer.timeout.connect(self._poll_cooldown)
        self._snapshot_max_age_s = max(2.0, float(getattr(cfg.magnet, "poll_interval_s", 0.5)) * 3.0)
        self.magnet.snapshot_updated.connect(self._on_snapshot)
        if hasattr(self.magnet, "connected"):
            self.magnet.connected.connect(self._on_connected)
        if hasattr(self.magnet, "disconnected"):
            self.magnet.disconnected.connect(self._on_disconnected)
        if hasattr(self.magnet, "safe_move_result"):
            self.magnet.safe_move_result.connect(self._on_move_result)
        if hasattr(self.magnet, "transition_progress"):
            self.magnet.transition_progress.connect(self._on_transition_progress)
        if hasattr(self.magnet, "operation_finished"):
            self.magnet.operation_finished.connect(self._on_magnet_operation)
        if hasattr(self.magnet, "error"):
            self.magnet.error.connect(self._on_magnet_error)
        if hasattr(self.magnet, "fault"):
            self.magnet.fault.connect(self._on_magnet_fault)
        self.tab.batch_run_terminal.connect(self._on_measurement_terminal)

    @property
    def active(self) -> bool:
        return self._state.request is not None and self._state.phase not in {"idle", "complete", "stopped", "failed"}

    @property
    def event_context(self) -> dict:
        """Return the current series/job context for UI event rendering."""
        request = self._state.request
        target = None
        if self._state.jobs and 0 <= self._state.index < len(self._state.jobs):
            target = self._state.jobs[self._state.index][1]
        return {
            "series_id": self._series_id,
            "job_index": min(self._state.index + 1, len(self._state.jobs)) if self._state.jobs else None,
            "job_count": len(self._state.jobs),
            "condition_index": int(request.condition_index) + 1 if request else None,
            "condition_name": request.condition_name if request else "",
            "target_t": float(target) if target is not None else None,
            "phase": self._state.phase,
        }

    def _append_series_log(self, event: dict):
        path = self._batch_log_path
        if not path:
            return
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            context = event.get("context") or {}
            context_text = []
            if context.get("condition_index") is not None:
                condition = f"condition {context['condition_index']}"
                if context.get("condition_name"):
                    condition += f" ({context['condition_name']})"
                context_text.append(condition)
            if context.get("target_t") is not None:
                context_text.append(f"target {float(context['target_t']):+.6f} T")
            if context.get("job_count"):
                context_text.append(f"job {context['job_index']}/{context['job_count']}")
            suffix = f" [{', '.join(context_text)}]" if context_text else ""
            with open(path, "a", encoding="utf-8") as handle:
                handle.write(
                    f"{event.get('timestamp', _system_local_iso())} "
                    f"[{event.get('category', 'INFO')}] {event.get('message', '')}{suffix}\n"
                )
        except Exception:
            # Logging must never interrupt magnet safety or measurement flow.
            pass

    def _emit_activity(self, category: str, message: str, **extra):
        event = {
            "timestamp": _system_local_iso(),
            "category": str(category),
            "message": str(message),
            "context": self.event_context,
        }
        event.update(extra)
        self.activity.emit(event)
        self._append_series_log(event)

    def _job_context(self, request, target, index: int) -> dict:
        context = self.event_context
        context.update({
            "job_index": int(index) + 1,
            "condition_index": int(request.condition_index) + 1,
            "condition_name": request.condition_name,
            "target_t": float(target),
        })
        return context

    def _reset_activity_throttles(self):
        self._last_transition_log_at = None
        self._last_transition_log_key = None
        self._last_transition_label = None
        self._last_measurement_progress_at = None
        self._last_snapshot_log_at = None
        self._last_snapshot_log_key = None
        self._last_snapshot_phase_key = None

    def _on_transition_progress(self, label: str, value: float):
        if self.active:
            now = time.monotonic()
            key = (str(label), round(float(value), 3))
            if (
                self._last_transition_log_at is None
                or str(label) != self._last_transition_label
                or now - self._last_transition_log_at >= 1.0
            ):
                self._emit_activity("MAGNET", f"{label}: {float(value):.3f}")
                self._last_transition_log_key = key
                self._last_transition_label = str(label)
                self._last_transition_log_at = now

    def _on_magnet_operation(self, name: str):
        if self.active:
            self._emit_activity("MAGNET", f"APS100 operation complete: {name}")

    def _on_magnet_error(self, message: str):
        if self.active:
            self._emit_activity("ERROR", str(message))

    def _on_magnet_fault(self, message: str):
        # Faults are always recorded, including a fault raised immediately
        # before the batch transitions to its terminal state.
        if self.active or self._batch_log_path:
            self._emit_activity("FAULT", str(message))

    def _on_measurement_log(self, message: str):
        if self.active:
            self._emit_activity("MEASUREMENT", str(message))

    def _on_measurement_progress(self, value: float):
        if not self.active:
            return
        now = time.monotonic()
        if (
            self._last_measurement_progress_at is not None
            and now - self._last_measurement_progress_at < 1.0
            and float(value) < 1.0
        ):
            return
        self._last_measurement_progress_at = now
        self._emit_activity("MEASUREMENT", f"Gate Scan progress: {float(value) * 100:.1f}%")

    @staticmethod
    def parse_fields(text: str) -> tuple[float, ...]:
        """Parse scalar targets and Python-style ``start:stop:step`` ranges.

        Ranges use an exclusive stop, matching Python's range progression;
        commas and newlines may be mixed between entries.
        """
        values = []
        for line_number, raw in enumerate(str(text or "").replace(",", "\n").splitlines(), start=1):
            token = raw.strip()
            if not token:
                continue
            if ":" not in token:
                try:
                    value = float(token)
                except (TypeError, ValueError) as exc:
                    raise ValueError(f"Line {line_number}: enter a finite field in tesla") from exc
                if not math.isfinite(value):
                    raise ValueError(f"Line {line_number}: field must be finite")
                if len(values) >= GateScanFieldBatch.MAX_EXPANDED_FIELDS:
                    raise ValueError(
                        f"The B-field series cannot exceed "
                        f"{GateScanFieldBatch.MAX_EXPANDED_FIELDS:,} fields"
                    )
                values.append(value)
                continue

            parts = [part.strip() for part in token.split(":")]
            if len(parts) != 3 or any(not part for part in parts):
                raise ValueError(
                    f"Line {line_number}: use Python-style start:stop:step ranges"
                )
            try:
                start, stop, step = (float(part) for part in parts)
            except (TypeError, ValueError) as exc:
                raise ValueError(
                    f"Line {line_number}: range values must be finite numbers"
                ) from exc
            if not all(math.isfinite(value) for value in (start, stop, step)):
                raise ValueError(f"Line {line_number}: range values must be finite")
            if step == 0.0:
                raise ValueError(f"Line {line_number}: range step cannot be zero")
            if (step > 0.0 and stop < start) or (step < 0.0 and stop > start):
                raise ValueError(
                    f"Line {line_number}: range step direction does not reach its stop"
                )

            # Python range semantics: stop is exclusive.  Computing each
            # value from its index avoids cumulative floating-point drift and
            # therefore avoids accidentally including a nominally exclusive
            # endpoint.  The strict comparison below intentionally has no
            # tolerance; a stop that is genuinely just beyond a point should
            # include that point, just as a Python numeric progression does.
            distance = (stop - start) / step
            if distance > GateScanFieldBatch.MAX_EXPANDED_FIELDS:
                raise ValueError(
                    f"Line {line_number}: range expands to more than "
                    f"{GateScanFieldBatch.MAX_EXPANDED_FIELDS:,} fields"
                )
            count = max(0, math.ceil(distance))
            if len(values) + count > GateScanFieldBatch.MAX_EXPANDED_FIELDS:
                raise ValueError(
                    f"The B-field series cannot exceed "
                    f"{GateScanFieldBatch.MAX_EXPANDED_FIELDS:,} fields"
                )
            for index in range(count):
                value = start + index * step
                if (step > 0.0 and value >= stop) or (step < 0.0 and value <= stop):
                    break
                values.append(value)
        if not values:
            raise ValueError("Enter at least one B-field target")
        if len(values) > GateScanFieldBatch.MAX_EXPANDED_FIELDS:
            raise ValueError(
                f"The B-field series cannot exceed "
                f"{GateScanFieldBatch.MAX_EXPANDED_FIELDS:,} fields"
            )
        normalized = [0.0 if abs(value) < 0.5e-12 else value for value in values]
        if len({round(value, 12) for value in normalized}) != len(normalized):
            raise ValueError("Duplicate normalized B-field targets are not allowed")
        limit = float(cfg.magnet.safe_control_max_field_t)
        if any(abs(value) > limit + 1e-12 for value in normalized):
            raise ValueError(f"Every B-field target must be within ±{limit:g} T")
        return tuple(normalized)

    @staticmethod
    def format_field_tag(field_t: float) -> str:
        return field_output_tag(float(field_t))

    def set_review_validator(self, callback):
        self._review_valid = callback

    def validate_text(self, text: str) -> tuple[float, ...]:
        return self.parse_fields(text)

    def start(self, text: str):
        if self.active:
            self.error.emit("A Gate Scan B-field batch is already running")
            return False
        if str(self._selected_backend()) != "1000":
            self.error.emit(
                "B-field batch requires attoDRY1000 (APS100); "
                "switch the magnet panel to APS100."
            )
            return False
        try:
            fields = self.parse_fields(text)
            series_id = uuid.uuid4().hex
            requests = self._capture_requests(fields, series_id)
            if not requests:
                raise ValueError("Add at least one enabled scan condition")
            self._validate_requests(requests)
            self._base_output_dir = self.tab._planned_output.output_dir
            self._base_display_stem = (
                f"{self.tab._planned_output.display_stem}_"
                f"{datetime.now():%Y%m%d_%H%M%S}_{series_id[:12]}"
            )
            preflight_thermal_hold = self._check_preflight(
                fields, check_outputs=True, requests=requests
            )
            if not self._confirm_initial_persistent_field():
                return False
        except Exception as exc:
            self.error.emit(str(exc))
            return False
        if not self.magnet.acquire_exclusive(self._owner):
            self.error.emit("APS100 is already reserved by another operation")
            return False
        try:
            required = tuple(dict.fromkeys(device for request in requests for device in request.required_devices))
            reserved = self.tab.begin_field_batch(
                requests[0].params, required, requests[0].calibration
            )
        except TypeError:
            reserved = self.tab.begin_field_batch(requests[0].params, required)
        if not reserved:
            self.magnet.release_exclusive(self._owner)
            self.error.emit("Gate Scan could not reserve its required sessions")
            return False
        # Field-major ordering minimizes persistent-switch cycles while
        # preserving the declared condition order within each field.
        jobs = tuple((request, field_t) for field_t in fields for request in requests)
        self._series_id = series_id
        self._persistent_confirmation_granted = True
        initial_phase = "thermal_wait" if preflight_thermal_hold is not None else "moving"
        self._state = _BatchState(request=requests[0], phase=initial_phase, jobs=jobs)
        self._checkpoint_path = os.path.join(
            self._base_output_dir,
            f"{self._base_display_stem}_bfield_batch_checkpoint.json",
        )
        self._batch_log_path = os.path.join(
            self._base_output_dir,
            f"{self._base_display_stem}_bfield_batch_log.txt",
        )
        self._temperature_telemetry_path = os.path.join(
            self._base_output_dir, f"{self._base_display_stem}_bfield_temperature_telemetry.jsonl"
        )
        self._thermal_observed = {
            "sample_min_k": None, "sample_max_k": None,
            "reservoir_min_k": None, "reservoir_max_k": None,
            "maximum_positive_sample_slope_k_per_min": None,
            "maximum_positive_reservoir_slope_k_per_min": None,
        }
        try:
            os.makedirs(os.path.dirname(self._batch_log_path), exist_ok=True)
            with open(self._batch_log_path, "w", encoding="utf-8") as handle:
                handle.write(
                    f"B-field Gate Scan series: {series_id}\n"
                    f"Started: {_system_local_iso()}\n"
                    f"Fields (T): {', '.join(format(float(value), '.12g') for value in fields)}\n"
                    f"Conditions: {len(requests)}\n\n"
                )
        except Exception:
            pass
        self._write_checkpoint("running")
        self.tab.set_batch_locked(True)
        self._emit_activity("RESERVATION", "APS100 reservation acquired")
        self._emit_activity(
            "SERIES",
            f"B-field series started with {len(jobs)} jobs",
            fields_t=list(fields),
        )
        if preflight_thermal_hold is not None:
            self._thermal_wait_context = "preflight"
            self._cooldown_started_at = time.monotonic()
            self._emit_state("thermal_wait", 0)
            self._emit_activity(
                "COOLDOWN",
                "Waiting for Lake Shore time-based permission before the first APS field move",
                reason=preflight_thermal_hold.reason,
            )
            self._cooldown_timer.start()
            self._request_temperature_refresh()
        else:
            self._emit_state("moving", 0)
            self._request_next_move()
        return True

    def stop(self):
        if not self.active:
            return
        if self._state.stop_requested:
            return
        self._state.stop_requested = True
        # Keep an in-flight APS move in the ``moving`` phase until its
        # terminal result arrives; this lets the result handler consume the
        # request exactly once.  Measurement stops retain ``stopping`` so the
        # existing worker-terminal path remains unchanged.
        if self._state.phase != "moving":
            self._state.phase = "stopping"
        self._emit_activity("STOP", "Stop requested by user")
        self._emit_state("stopping", self._state.index)
        # The APS controller sets its stop event immediately; its queued pause
        # is safe even when the worker is in a mandatory heater dwell.
        self.magnet.pause()
        if self.tab.worker is not None:
            self.tab.stop_run()

    def _capture_requests(self, fields, series_id=None):
        capture_many = getattr(self.tab, "capture_field_batch_requests", None)
        if callable(capture_many):
            captured = capture_many()
            requests = []
            for index, item in enumerate(captured):
                if len(item) == 3:
                    params, required, calibration = item
                    name = ""
                else:
                    params, required = item[:2]
                    calibration = item[2] if len(item) > 2 else None
                    name = str(item[3]) if len(item) > 3 else ""
                requests.append(GateScanBatchRequest(
                    fields_t=tuple(fields), params=deepcopy(params),
                    required_devices=tuple(required), batch_id=series_id or uuid.uuid4().hex,
                    captured_at=_system_local_iso(timespec="microseconds"),
                    calibration=calibration, condition_index=index,
                    condition_name=name,
                ))
            return tuple(requests)
        return (self._capture_request(fields, series_id),)

    def _capture_request(self, fields, series_id=None):
        captured = self.tab.capture_field_batch_params()
        if len(captured) == 3:
            params, required, calibration = captured
        else:
            params, required = captured
            calibration = None
        return GateScanBatchRequest(
            fields_t=tuple(fields),
            params=deepcopy(params),
            required_devices=tuple(required),
            batch_id=series_id or uuid.uuid4().hex,
            captured_at=_system_local_iso(timespec="microseconds"),
            calibration=calibration,
        )

    def _validate_requests(self, requests):
        validator = getattr(self.tab, "validate_field_batch_request", None)
        if not callable(validator):
            return
        for index, request in enumerate(requests, start=1):
            try:
                validator(request.params)
            except Exception as exc:
                raise ValueError(f"Condition {index} is invalid: {exc}") from exc

    def _check_preflight(self, fields, *, check_outputs, request=None, requests=None):
        if not bool(self._review_valid()):
            raise ValueError("Commissioning review is required and must remain valid")
        connected = getattr(self.magnet, "is_connected", False)
        if callable(connected):
            connected = connected()
        if not bool(connected):
            raise ValueError("APS100 must be connected before starting a B-field batch")
        snapshot = self._snapshot
        if snapshot is None:
            self._request_snapshot_refresh()
            raise ValueError("Fresh APS100 telemetry is required; refresh telemetry and try again")
        if self._snapshot_received_at is None or (datetime.now(timezone.utc) - self._snapshot_received_at).total_seconds() > self._snapshot_max_age_s:
            self._request_snapshot_refresh()
            raise ValueError("APS100 telemetry is stale; refresh telemetry and try again")
        self._verify_initial_snapshot(snapshot)
        preflight_thermal_hold = None
        if self.thermal_safety is not None and not self._thermal_armed():
            raise ValueError(
                "Lake Shore thermal preflight rejected the batch: "
                "interlock is disarmed or thresholds/channel mapping are not commissioned"
            )
        thermal = self._evaluate_thermal()
        if self._thermal_armed():
            if thermal is None:
                raise ValueError(
                    "Lake Shore thermal preflight rejected the batch: thermal evaluator unavailable"
                )
            if not thermal.magnet_permission:
                self._request_temperature_refresh()
                if self._is_preflight_time_hold(thermal):
                    preflight_thermal_hold = thermal
                else:
                    raise ValueError(f"Lake Shore thermal preflight rejected the batch: {thermal.reason}")
        requests = tuple(requests or ((request,) if request is not None else ()))
        if check_outputs and requests:
            for condition_index, request_item in enumerate(requests):
                for index, field_t in enumerate(fields, start=1):
                    planned = self._field_output(request_item, field_t, multi_condition=len(requests) > 1)
                    # The output check intentionally covers every condition x
                    # field before reserving the magnet or changing hardware.
                    if output_blocking_reason(planned, self.tab.save):
                        reason = output_blocking_reason(planned, self.tab.save)
                        raise ValueError(f"Condition {condition_index + 1}, field {index} output is not available: {reason}")
        return preflight_thermal_hold

    def _field_output(self, request, field_t, multi_condition=None):
        base = self._base_display_stem or self.tab._planned_output.display_stem
        if multi_condition is None:
            multi_condition = bool(getattr(request, "condition_index", 0))
        condition_tag = ""
        if multi_condition:
            condition_tag = f"_C{int(request.condition_index) + 1}"
        stem = f"{base}{condition_tag}_{self.format_field_tag(field_t)}"
        directory = self._base_output_dir or self.tab._planned_output.output_dir
        return PlannedOutput(
            run_id="batch",
            output_dir=directory,
            stem=stem,
            csv_path=os.path.join(directory, stem + ".csv"),
            metadata_path=os.path.join(directory, stem + "_metadata.json"),
            log_path=os.path.join(directory, stem + "_run_log.txt"),
        )

    def _request_next_move(self):
        if self._state.stop_requested:
            self._finish_stopped("Stopped before the next B-field move")
            return
        if not bool(self._review_valid()):
            self._fail("Commissioning review was revoked before the next B-field move")
            return
        if self._state.index >= len(self._state.jobs):
            self._finish_success()
            return
        request, target = self._state.jobs[self._state.index]
        self._state.request = request
        # Conditions at the same field run without another APS move.  This is
        # the field-major path and avoids needless heater cycles.
        if (self._last_persistent_field_t is not None and
                abs(float(self._last_persistent_field_t) - float(target)) <= float(cfg.magnet.field_tolerance_t) and
                self._snapshot is not None and getattr(self._snapshot, "heater_on", None) is False):
            self._start_measurement_after_move(self._snapshot, target)
            return
        # Fail closed before every operation that may require a new
        # persistent-switch heater cycle.  The separate post-move gate below
        # handles heat released by the cycle that has just completed.
        if self._thermal_armed():
            decision = self._evaluate_thermal()
            if decision is None or not decision.magnet_permission:
                self._state.phase = "thermal_wait"
                self._thermal_wait_context = "pre_move"
                self._cooldown_started_at = time.monotonic()
                self._emit_state("thermal_wait", self._state.index)
                self._emit_activity(
                    "COOLDOWN",
                    "Waiting for Lake Shore permission before the next APS field move",
                    reason=(decision.reason if decision else "thermal evaluator unavailable"),
                )
                self._cooldown_timer.start()
                self._request_temperature_refresh()
                return
        self._state.phase = "moving"
        self._reset_activity_throttles()
        self._move_started_from_persistent = bool(
            self._snapshot is not None
            and getattr(self._snapshot, "heater_on", None) is False
            and abs(float(getattr(self._snapshot, "field_t", 0.0)) - float(target))
            > float(cfg.magnet.field_tolerance_t)
        )
        self._emit_state(f"Moving to {self.format_field_tag(target)} persistent", self._state.index)
        self._state.move_request_id = self._call_safe_move(target)

    def _thermal_armed(self):
        return self.thermal_safety is not None and bool(getattr(self.thermal_safety, "is_armed", getattr(self.thermal_safety, "armed", False)))

    @staticmethod
    def _is_preflight_time_hold(decision):
        return getattr(decision, "block_code", None) in {
            "stable_recovery_dwell", "minimum_heater_interval",
        }

    def _evaluate_thermal(self):
        if self.thermal_safety is None:
            return None
        snapshot = getattr(self.thermal_safety, "latest_snapshot", None)
        try:
            return self.thermal_safety.evaluate(snapshot)
        except TypeError:
            return self.thermal_safety.evaluate()

    def _request_temperature_refresh(self):
        refresh = getattr(self.lakeshore_controller, "refresh_snapshot", None)
        if callable(refresh):
            refresh()

    def _poll_cooldown(self):
        if not self.active or self._state.phase != "thermal_wait":
            self._cooldown_timer.stop()
            return
        timeout = float(getattr(getattr(self.thermal_safety, "config", None), "maximum_cooldown_wait_timeout_s", 3600.0))
        if self._cooldown_started_at is not None and time.monotonic() - self._cooldown_started_at >= timeout:
            self._cooldown_timer.stop()
            self._fail("Lake Shore cooldown timeout expired; operator inspection and restart are required")
            return
        decision = self._evaluate_thermal()
        if decision is not None and decision.magnet_permission:
            self._cooldown_timer.stop()
            self._thermal_wait_context = None
            self._emit_activity("RECOVERY", "Lake Shore thermal permission recovered; continuing B-field batch")
            self._request_next_move()
        elif self._thermal_wait_context == "preflight" and not self._is_preflight_time_hold(decision):
            self._cooldown_timer.stop()
            reason = decision.reason if decision is not None else "thermal evaluator unavailable"
            self._fail(f"Lake Shore thermal preflight rejected the batch: {reason}")
        else:
            # The Lake Shore controller's timer owns routine reads.  Do not
            # request a read from this evaluation path: snapshots are already
            # being delivered asynchronously and doing so creates a callback
            # feedback loop during communication faults.
            pass

    def _confirm_initial_persistent_field(self):
        snapshot = self._snapshot
        if snapshot is None:
            return False
        if snapshot.heater_on is not False:
            self._persistent_confirmation_granted = True
            return True
        answer = QtWidgets.QMessageBox.question(
            self.tab,
            "Confirm stored APS100 field",
            f"The APS100 heater is OFF and the stored magnet field is "
            f"{float(snapshot.field_t):+.6f} T.\n\n"
            "Confirm that this field and polarity are correct. The supply "
            "will be matched before the heater is turned on, then the target "
            "will be made persistent and the supply leads swept with ZERO.",
            QtWidgets.QMessageBox.StandardButton.Yes
            | QtWidgets.QMessageBox.StandardButton.No,
            QtWidgets.QMessageBox.StandardButton.No,
        )
        confirmed = answer == QtWidgets.QMessageBox.StandardButton.Yes
        self._persistent_confirmation_granted = confirmed
        return confirmed

    def _request_snapshot_refresh(self):
        refresh = getattr(self.magnet, "refresh_snapshot", None)
        if callable(refresh):
            refresh()

    def _call_safe_move(self, target):
        result = self.magnet.safe_move_to_field(
            target,
            final_mode="persistent",
            zero_leads=True,
            persistent_field_confirmed=bool(self._persistent_confirmation_granted),
        )
        return result if isinstance(result, str) else None

    def _on_snapshot(self, snapshot):
        self._snapshot = snapshot
        self._snapshot_received_at = datetime.now(timezone.utc)
        if not self.active:
            return
        status = getattr(snapshot, "status", None)
        if status is not None and (
            getattr(status, "quench", False) or getattr(status, "power_module_failure", False)
        ):
            self._emit_activity("FAULT", "APS100 fault reported in telemetry")
        key = (
            round(float(getattr(snapshot, "field_t", 0.0)), 5),
            round(float(getattr(snapshot, "output_field_t", 0.0)), 5),
            round(float(getattr(snapshot, "output_current_a", 0.0)), 4),
            str(getattr(snapshot, "sweep_state", "—")),
            bool(getattr(snapshot, "heater_on", False)),
        )
        now = time.monotonic()
        phase_key = (key[3], key[4])
        if (
            self._last_snapshot_log_at is None
            or phase_key != self._last_snapshot_phase_key
            or now - self._last_snapshot_log_at >= 1.0
        ):
            self._emit_activity(
                "TELEMETRY",
                f"APS100 field={float(snapshot.field_t):+.6f} T, "
                f"output={float(snapshot.output_field_t):+.6f} T, "
                f"current={float(snapshot.output_current_a):+.4f} A, "
                f"heater={'on' if snapshot.heater_on else 'off'}, "
                f"sweep={getattr(snapshot, 'sweep_state', '—')}",
            )
            self._last_snapshot_log_key = key
            self._last_snapshot_phase_key = phase_key
            self._last_snapshot_log_at = now

    def on_lakeshore_snapshot(self, snapshot):
        """Receive external LS335 telemetry without touching APS move state."""
        if self.thermal_safety is not None:
            self.thermal_safety.evaluate(snapshot)
            if self.active and self._state.phase == "thermal_wait":
                self._poll_cooldown()
        self._record_temperature_telemetry(snapshot)

    def on_lakeshore_disconnected(self):
        if self.thermal_safety is not None:
            self.thermal_safety.latest_snapshot = None
            self.thermal_safety.evaluate(None)

    def _record_temperature_telemetry(self, snapshot):
        path = self._temperature_telemetry_path
        if not path or snapshot is None:
            return
        try:
            record = {
                "timestamp": getattr(snapshot, "timestamp", None),
                "monotonic_s": getattr(snapshot, "monotonic_s", None),
                "reading_age_s": getattr(snapshot, "reading_age_s", None),
                "sample_temperature_k": getattr(snapshot, "sample_temperature_k", None),
                "reservoir_temperature_k": getattr(snapshot, "reservoir_temperature_k", None),
                "sample_sensor_status": getattr(snapshot, "sample_sensor_status", None),
                "reservoir_sensor_status": getattr(snapshot, "reservoir_sensor_status", None),
                "sample_slope_k_per_min": getattr(snapshot, "sample_slope_k_per_min", None),
                "reservoir_slope_k_per_min": getattr(snapshot, "reservoir_slope_k_per_min", None),
                "connected": getattr(snapshot, "connected", False),
                "communication_valid": getattr(snapshot, "communication_valid", False),
                "identity": getattr(snapshot, "identity", None),
            }
            for key, value in (("sample_min_k", getattr(snapshot, "sample_temperature_k", None)),
                               ("sample_max_k", getattr(snapshot, "sample_temperature_k", None)),
                               ("reservoir_min_k", getattr(snapshot, "reservoir_temperature_k", None)),
                               ("reservoir_max_k", getattr(snapshot, "reservoir_temperature_k", None))):
                if value is not None:
                    try:
                        value = float(value)
                        if value == value and abs(value) != float("inf"):
                            if "min" in key:
                                self._thermal_observed[key] = value if self._thermal_observed[key] is None else min(self._thermal_observed[key], value)
                            else:
                                self._thermal_observed[key] = value if self._thermal_observed[key] is None else max(self._thermal_observed[key], value)
                    except (TypeError, ValueError):
                        pass
            for key, value in (("maximum_positive_sample_slope_k_per_min", getattr(snapshot, "sample_slope_k_per_min", None)),
                               ("maximum_positive_reservoir_slope_k_per_min", getattr(snapshot, "reservoir_slope_k_per_min", None))):
                try:
                    value = float(value)
                    if value > 0 and value == value and abs(value) != float("inf"):
                        self._thermal_observed[key] = value if self._thermal_observed[key] is None else max(self._thermal_observed[key], value)
                except (TypeError, ValueError):
                    pass
            if self.thermal_safety is not None:
                decision = self._evaluate_thermal()
                record.update({"thermal_state": decision.state.value, "magnet_permission": bool(decision.magnet_permission), "thermal_reason": decision.reason})
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, default=str) + "\n")
        except Exception:
            pass

    def _on_connected(self, *_args):
        # A snapshot from a previous physical connection must never authorize
        # persistent movement after reconnecting.
        self._snapshot = None
        self._snapshot_received_at = None
        self._persistent_confirmation_granted = False

    def _on_disconnected(self, *_args):
        self._snapshot = None
        self._snapshot_received_at = None
        self._persistent_confirmation_granted = False

    def _on_move_result(self, result):
        if not self.active:
            return
        if self._state.phase != "moving":
            return
        request_id = result.get("request_id") if isinstance(result, dict) else None
        if not self._state.move_request_id or request_id != self._state.move_request_id:
            return
        # Consume the request before any verification or measurement start so
        # a duplicate/late signal cannot restart a completed job.
        self._state.move_request_id = None
        if self._state.stop_requested:
            self._finish_stopped("Stopped during APS100 persistent move")
            return
        if not isinstance(result, dict) or not result.get("success"):
            self._fail(f"APS100 persistent move failed: {result.get('error', 'unknown error') if isinstance(result, dict) else result}")
            return
        self._state.audit = result.get("audit")
        snapshot = result.get("snapshot") or self._snapshot
        try:
            request, target = self._state.jobs[self._state.index]
            self._state.request = request
            self._verify_persistent_snapshot(snapshot, target)
        except Exception as exc:
            self._fail(f"Persistent field verification failed: {exc}")
            return
        self._snapshot = snapshot
        self._last_persistent_field_t = float(target)
        if self.thermal_safety is not None:
            commands = (self._state.audit or {}).get("commands", []) if isinstance(self._state.audit, dict) else []
            if self._move_started_from_persistent or any("PSHTR ON" in str(item.get("command", item)).upper() for item in commands):
                self.thermal_safety.note_heater_activation()
        self._emit_activity(
            "COMPLETE",
            "Persistent field verified; APS heater is off and leads are zero",
            verified_field_t=float(snapshot.field_t),
        )
        if self._thermal_armed():
            decision = self._evaluate_thermal()
            if decision is None or not decision.magnet_permission:
                self._state.phase = "thermal_wait"
                self._thermal_wait_context = "post_move"
                self._cooldown_started_at = time.monotonic()
                self._emit_state("thermal_wait", self._state.index)
                self._emit_activity(
                    "COOLDOWN",
                    "Persistent move complete; waiting for Lake Shore recovery before Gate Scan",
                    reason=(decision.reason if decision else "thermal evaluator unavailable"),
                )
                self._cooldown_timer.start()
                self._request_temperature_refresh()
                return
        self._emit_state("Thermal recovery verified; starting Gate Scan", self._state.index)
        self._start_measurement_after_move(snapshot, target)

    def _start_measurement_after_move(self, snapshot, target):
        """Start one frozen Gate Scan after APS persistent verification."""
        request = self._state.request
        self._state.phase = "measuring"
        self._last_measurement_progress_at = None
        field_t = target
        planned = self._field_output(request, field_t, multi_condition=len({job[0].condition_index for job in self._state.jobs}) > 1)
        setter = getattr(self.tab, "set_field_batch_condition", None)
        if callable(setter):
            setter(request.params)
        metadata = self._metadata(field_t, snapshot)
        if not self.tab.start_field_batch_measurement(planned, metadata):
            self._fail("Gate Scan could not start after persistent field verification")
            return
        worker = getattr(self.tab, "worker", None)
        if worker is not None:
            log_signal = getattr(worker, "log", None)
            if log_signal is not None:
                log_signal.connect(self._on_measurement_log)
            progress_signal = getattr(worker, "progress", None)
            if progress_signal is not None:
                progress_signal.connect(self._on_measurement_progress)
        self._emit_activity(
            "MEASUREMENT",
            f"Gate Scan started; output {planned.csv_path}",
        )

    def _verify_persistent_snapshot(self, snapshot, target, require_target=True):
        if snapshot is None:
            raise ValueError("no final APS100 snapshot was returned")
        if require_target and abs(float(snapshot.field_t) - float(target)) > float(cfg.magnet.field_tolerance_t):
            raise ValueError(f"stored field {snapshot.field_t:g} T does not match requested {target:g} T")
        if snapshot.heater_on is not False:
            raise ValueError("heater is not OFF")
        tolerance = max(0.002, float(cfg.magnet.field_tolerance_t))
        if abs(float(snapshot.output_field_t)) > tolerance or abs(float(snapshot.output_current_a)) > float(cfg.magnet.current_match_tolerance_a):
            raise ValueError("supply leads are not zero")
        status = snapshot.status
        if getattr(status, "sweep_active", False):
            raise ValueError("sweep is still active")
        if not getattr(status, "standby", False):
            raise ValueError("APS100 is not in standby/persistent state")
        if getattr(status, "quench", False) or getattr(status, "power_module_failure", False):
            raise ValueError("APS100 reports a quench or power-module fault")

    @staticmethod
    def _verify_initial_snapshot(snapshot):
        if snapshot is None:
            raise ValueError("no fresh APS100 snapshot is available")
        if not isinstance(getattr(snapshot, "heater_on", None), bool):
            raise ValueError("APS100 heater state is unknown; refresh telemetry")
        status = snapshot.status
        if getattr(status, "quench", False) or getattr(status, "power_module_failure", False):
            raise ValueError("APS100 reports a quench or power-module fault")

    def _metadata(self, requested, snapshot):
        request = self._state.request
        metadata = {
            "gate_scan_bfield_batch": {
                "batch_id": request.batch_id,
                "captured_at": request.captured_at,
                "field_index": sum(1 for job in self._state.jobs[:self._state.index + 1] if job[0] is request),
                "field_count": len(request.fields_t),
                "condition_index": int(request.condition_index) + 1,
                "condition_count": len({job[0].condition_index for job in self._state.jobs}),
                "condition_name": request.condition_name,
                "requested_field_t": float(requested),
                "verified_field_t": float(snapshot.field_t),
                "output_field_t": float(snapshot.output_field_t),
                "output_current_a": float(snapshot.output_current_a),
                "heater_on": bool(snapshot.heater_on),
                "sweep_state": str(snapshot.sweep_state),
                "standby": bool(snapshot.status.standby),
                "status": getattr(snapshot.status, "raw", None),
                "quench": bool(snapshot.status.quench),
                "power_module_failure": bool(snapshot.status.power_module_failure),
                "safe_move_request_id": self._state.move_request_id,
                "safe_move_audit": self._state.audit,
                "frozen_gate_scan_params": deepcopy(request.params),
            }
        }
        if self.thermal_safety is not None:
            try:
                metadata["lake_shore_335"] = self.thermal_safety.snapshot_dict()
                try:
                    configured = asdict(self.thermal_safety.config)
                except TypeError:
                    configured = dict(vars(self.thermal_safety.config))
                metadata["lake_shore_335"]["configured"] = configured
                metadata["lake_shore_335"]["observed"] = dict(self._thermal_observed)
                metadata["lake_shore_335"]["events"] = dict(self.thermal_safety.events)
                metadata["lake_shore_335"]["heater_activation_timestamps"] = list(self.thermal_safety.heater_activation_timestamps)
                metadata["lake_shore_335"]["heater_activation_events"] = list(self.thermal_safety.heater_activation_events)
            except Exception:
                pass
        return metadata

    def _on_measurement_terminal(self, status, detail):
        if not self.active or self._state.phase not in {"measuring", "stopping"}:
            return
        if self._state.stop_requested:
            self._finish_stopped(f"Stopped during Gate Scan: {detail}")
            return
        if status != "finished":
            if status == "stopped" or self._state.stop_requested:
                self._finish_stopped(detail)
            else:
                self._fail(f"Gate Scan failed: {detail}")
            return
        completed_index = self._state.index
        completed_request, completed_target = self._state.jobs[completed_index]
        self._state.results.append({
            "field_t": completed_target,
            "condition_index": int(completed_request.condition_index) + 1,
            "path": detail,
        })
        self._state.index += 1
        # A completed job does not mean the whole series is complete.  Keep
        # the checkpoint resumable/nonterminal until the final job succeeds.
        self._checkpoint("job_complete")
        self._emit_activity(
            "COMPLETE",
            f"Gate Scan complete: {detail}",
            completed_field_t=float(completed_target),
            completed_condition_index=int(completed_request.condition_index) + 1,
            context=self._job_context(completed_request, completed_target, completed_index),
        )
        self.progress_changed.emit(self._state.index, len(self._state.jobs), "field complete")
        self._request_next_move()

    def _checkpoint(self, status):
        if self._state.results:
            self._state.results[-1]["status"] = status
        self._write_checkpoint(status)

    def _write_checkpoint(self, status, error=""):
        if self._checkpoint_path is None or self._state.request is None:
            return
        payload = {
            "schema": "gate_scan_bfield_batch_checkpoint_v1",
            "batch_id": self._series_id or self._state.request.batch_id,
            "status": str(status),
            "updated_at": _system_local_iso(timespec="microseconds"),
            "fields_t": list(self._state.request.fields_t),
            # next_job_index is the authoritative resume cursor.  Keep the
            # field-local value for compatibility with v1 single-condition
            # checkpoints.
            "execution_order": "field-major",
            "next_field_index": self._state.index // max(1, len({job[0].condition_index for job in self._state.jobs})),
            "next_condition_index": self._state.index % max(1, len({job[0].condition_index for job in self._state.jobs})),
            "next_job_index": self._state.index,
            "condition_count": len({job[0].condition_index for job in self._state.jobs}),
            "conditions": [
                {
                    "index": int(request.condition_index) + 1,
                    "name": request.condition_name,
                    "frozen_gate_scan_params": to_jsonable(request.params),
                }
                for request in dict((job[0].condition_index, job[0]) for job in self._state.jobs).values()
            ],
            "results": list(self._state.results),
            "frozen_gate_scan_params": to_jsonable(self._state.request.params),
        }
        if self.thermal_safety is not None:
            try:
                payload["lake_shore_335"] = self.thermal_safety.snapshot_dict()
                payload["lake_shore_335"]["events"] = self.thermal_safety.events
                payload["lake_shore_335"]["observed"] = dict(self._thermal_observed)
                payload["lake_shore_335"]["heater_activation_timestamps"] = list(self.thermal_safety.heater_activation_timestamps)
                payload["lake_shore_335"]["heater_activation_events"] = list(self.thermal_safety.heater_activation_events)
            except Exception:
                pass
        if error:
            payload["error"] = str(error)
        temporary = self._checkpoint_path + ".tmp"
        try:
            os.makedirs(os.path.dirname(self._checkpoint_path), exist_ok=True)
            with open(temporary, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, default=str, indent=2)
                handle.write("\n")
            os.replace(temporary, self._checkpoint_path)
        except Exception:
            try:
                if os.path.exists(temporary):
                    os.remove(temporary)
            except OSError:
                pass

    def _emit_state(self, phase, index):
        total = len(self._state.jobs)
        detail = f"Step {min(index + 1, total)} of {total}"
        self.state_changed.emit(str(phase), detail)
        # The tab renders this phase through its state_changed handler; retain
        # the event in the persistent series log without duplicating it in the
        # on-screen activity view.
        self._emit_activity("PHASE", f"{phase}: {detail}", ui_duplicate=True)
        if self._state.request:
            self.progress_changed.emit(index, total, phase)

    def _fail(self, message):
        if not self.active:
            return
        self._state.phase = "failed"
        self._emit_activity("ERROR", str(message))
        self._write_checkpoint("failed", message)
        self.error.emit(str(message))
        self._cleanup()

    def _finish_stopped(self, message):
        if self._state.phase in {"stopped", "complete"}:
            return
        self._state.phase = "stopped"
        self._emit_activity("STOP", str(message))
        self._write_checkpoint("stopped", message)
        self.stopped.emit(str(message))
        self._cleanup()

    def _finish_success(self):
        self._state.phase = "complete"
        self._emit_activity("COMPLETE", "B-field Gate Scan series complete")
        self._write_checkpoint("complete")
        self.finished.emit()
        self._cleanup()

    def _cleanup(self):
        self._cooldown_timer.stop()
        self.tab.finish_field_batch()
        self.tab.set_batch_locked(False)
        self._emit_activity("RESERVATION", "APS100 reservation released")
        self.magnet.release_exclusive(self._owner)
        # Do not let a late fault signal append to a completed series log.
        # The file remains on disk for audit/review.
        self._batch_log_path = None
        self._state.request = None
        self._state.jobs = ()
        self._series_id = None
        self._persistent_confirmation_granted = False
        self._base_output_dir = None
        self._base_display_stem = None
        self._temperature_telemetry_path = None
        self._cooldown_started_at = None
        self._thermal_wait_context = None
        self._last_persistent_field_t = None
        self._move_started_from_persistent = False
