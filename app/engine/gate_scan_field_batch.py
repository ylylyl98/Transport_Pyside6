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
from types import SimpleNamespace

from PySide6 import QtCore, QtWidgets

from app.models import LineSweepParams
from app.run_output import (PlannedOutput, field_output_tag, output_blocking_reason, to_jsonable,
                            new_run_id, compose_output_stem, planned_output_at,
                            unique_planned_output, gate_scan_filename_parts)
from utils.config import cfg


def _system_local_iso(*, timespec="seconds"):
    """Return an aware timestamp in the PC's configured local timezone."""
    return datetime.now().astimezone().isoformat(timespec=timespec)


class _SnapshotUnavailable(ValueError):
    """A readback that may recover after a bounded refresh."""


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

    state_changed = QtCore.Signal(str, str)
    continuation_changed = QtCore.Signal(bool, bool, str)
    progress_changed = QtCore.Signal(int, int, str)
    # Structured events are consumed by the B-field tab and also persisted in
    # the series log.  The existing signals remain for compatibility.
    activity = QtCore.Signal(object)
    error = QtCore.Signal(str)
    finished = QtCore.Signal()
    stopped = QtCore.Signal(str)

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
        self._field_outputs = {}
        self._review_valid = lambda: True
        self._series_id = None
        self._resumed_from = None
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
        self._thermal_manual_required = False
        self.measurement_temperature_provider = None
        self._last_persistent_field_t = None
        self._move_started_from_persistent = False
        self._temperature_telemetry_path = None
        self._field_comparison = (self.thermal_safety.new_field_comparison()
                                  if hasattr(self.thermal_safety, "new_field_comparison") else None)
        self._comparison_job = None
        self._comparison_readbacks = {}
        self._comparison_last_key = None
        self._comparison_record = None
        self._thermal_observed = {
            "sample_min_k": None, "sample_max_k": None,
            "reservoir_min_k": None, "reservoir_max_k": None,
            "maximum_positive_sample_slope_k_per_min": None,
            "maximum_positive_reservoir_slope_k_per_min": None,
        }
        self._verification_deadline = None
        self._verification_reason = ""
        self._verification_timer = QtCore.QTimer(self)
        self._verification_timer.setInterval(1000)
        self._verification_timer.timeout.connect(self._poll_verification)
        self._measurement_monitor_timer = QtCore.QTimer(self)
        self._measurement_monitor_timer.setInterval(250)
        self._measurement_monitor_timer.timeout.connect(self._poll_measurement_thermal)
        self._dispatching_move = False
        self._early_move_results = []
        self._cooldown_timer = QtCore.QTimer(self)
        self._cooldown_timer.setInterval(250)
        self._cooldown_timer.timeout.connect(self._poll_cooldown)
        self._snapshot_max_age_s = float(cfg.lakeshore335.maximum_field_reading_age_s)
        self.magnet.snapshot_updated.connect(self._on_snapshot)
        if hasattr(self.magnet, "diagnostic_readback"):
            self.magnet.diagnostic_readback.connect(self._on_diagnostic_readback)
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
            if self._state.phase == "stopping" and self.tab.worker is None:
                if name == "pause":
                    self._finish_stopped("Stopped; APS100 pause acknowledged")
                elif name == "failed:pause":
                    self._fail("APS100 pause failed while stopping")

    def _on_magnet_error(self, message: str):
        if self.active:
            self._emit_activity("ERROR", str(message))

    def _on_magnet_fault(self, message: str):
        # Faults are always recorded, including a fault raised immediately
        # before the batch transitions to its terminal state.
        if self.active or self._batch_log_path:
            self._emit_activity("FAULT", str(message))
        if self.active:
            self._abort_batch(f"APS100 fault: {message}")

    def _abort_batch(self, message):
        if not self.active:
            return
        if self.tab.worker is not None:
            self._measurement_monitor_timer.stop()
            self._pending_failure = str(message)
            self._state.phase = "stopping"
            self.tab.stop_run()
        elif self._state.phase == "moving":
            self._pending_failure = str(message)
            self.magnet.pause()
        else:
            self._fail(str(message))

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

    def start(self, text: str, *, resume_checkpoint=None):
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
            resume = self._validate_resume(resume_checkpoint, requests) if resume_checkpoint else None
            start_index = resume["next_job_index"] if resume else 0
            self._base_output_dir = self.tab._planned_output.output_dir
            run_id = new_run_id()
            series_stem = compose_output_stem(self.tab.save.device_id, "gate_scan",
                                             requests[0].params.base_name, ("series",), run_id)
            series_output = unique_planned_output(planned_output_at(self._base_output_dir, series_stem, run_id))
            self._base_display_stem = series_output.stem
            self._field_outputs = {}
            reserved = []
            for request in requests:
                for field_t in fields:
                    signal_chain = request.calibration[2] if request.calibration is not None else None
                    parts = gate_scan_filename_parts(request.params, signal_chain, field_t)
                    stem = compose_output_stem(self.tab.save.device_id, "gate_scan", request.params.base_name, parts, run_id)
                    output = unique_planned_output(planned_output_at(self._base_output_dir, stem, run_id), reserved)
                    self._field_outputs[(request.condition_index, field_t)] = output
                    reserved.extend((output.csv_path, output.metadata_path, output.log_path))
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
            # Exclusive ownership prevents competing commands, but temperature
            # protection still needs fresh field readings throughout cooldown
            # and measurement. The APS worker serializes polls with moves.
            polling = getattr(self.magnet, "set_polling_enabled", None)
            if callable(polling):
                polling(True)
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
        self._thermal_manual_required = False
        self._resumed_from = os.path.abspath(resume_checkpoint) if resume else None
        self._state = _BatchState(request=jobs[start_index][0], index=start_index, phase=initial_phase, jobs=jobs,
                                  results=deepcopy(resume["results"]) if resume else [])
        self._pending_failure = None
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
        if getattr(self, "_checkpoint_write_error", None):
            self._fail(self._checkpoint_write_error)
            return False
        self.tab.set_batch_locked(True)
        self._emit_activity("RESERVATION", "APS100 reservation acquired")
        if resume:
            self._emit_activity("RESUME", f"Verified {start_index} completed jobs; resuming at job {start_index + 1}. Previous files preserved.")
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
        return self._state.phase != "failed"

    @staticmethod
    def _recipe_values(params):
        return {key: value for key, value in to_jsonable(params).items()
                if not key.startswith("output_")}

    def _validate_resume(self, path, requests):
        """Validate an external checkpoint as data before any instrument mutation."""
        with open(path, encoding="utf-8") as handle:
            saved = json.load(handle)
        if saved.get("schema") != "gate_scan_bfield_batch_checkpoint_v1":
            raise ValueError("Unsupported Gate Scan checkpoint")
        if saved.get("status") == "complete":
            raise ValueError("This series is already complete")
        fields = list(requests[0].fields_t)
        if saved.get("fields_t") != fields:
            raise ValueError("Resume field list differs from the saved series")
        conditions = saved.get("conditions", [])
        if len(conditions) != len(requests):
            raise ValueError("Resume condition count differs from the saved series")
        results = saved.get("results", [])
        cursor = saved.get("next_job_index")
        if type(cursor) is not int or not 0 <= cursor < len(fields) * len(requests) or len(results) != cursor:
            raise ValueError("Checkpoint completion cursor is inconsistent or has no unfinished jobs")
        directory = os.path.realpath(self.tab._planned_output.output_dir)
        if os.path.realpath(os.path.dirname(path)) != directory:
            raise ValueError("Select the same data output folder as the checkpoint before resuming")
        for index, (request, condition) in enumerate(zip(requests, conditions), start=1):
            if self._recipe_values(condition["frozen_gate_scan_params"]) != self._recipe_values(request.params):
                raise ValueError(f"Resume settings differ for condition {index}; restore the saved settings first")
            if "calibration" in condition:
                if condition["calibration"] != to_jsonable(request.calibration):
                    raise ValueError(f"Resume calibration differs for condition {index}")
            elif request.calibration is not None:
                # Older checkpoints retain calibration in completed run metadata.
                prior = next((item for item in results if item.get("condition_index") == index), None)
                if prior is None:
                    raise ValueError(f"Older checkpoint has no calibration evidence for condition {index}")
                csv = os.path.realpath(prior["path"])
                if os.path.dirname(csv) != directory:
                    raise ValueError("Completed file is outside the selected data folder")
                with open(os.path.splitext(csv)[0] + "_metadata.json", encoding="utf-8") as handle:
                    metadata = json.load(handle)
                from app.signal_chain import signal_chain_metadata
                current = signal_chain_metadata(request.calibration[2])
                previous = metadata.get("signal_chain", {})
                for key in ("frequency_hz", "lockin_sensitivity_v", "preamp_sensitivity_a", "lockin_scale", "preamp_gain_v_per_a"):
                    if previous.get(key) != current.get(key):
                        raise ValueError(f"Resume {key} differs for condition {index}")
        for index, result in enumerate(results):
            expected_field = fields[index // len(requests)]
            expected_condition = index % len(requests) + 1
            if result.get("field_t") != expected_field or result.get("condition_index") != expected_condition or result.get("status") != "job_complete":
                raise ValueError("Checkpoint completed jobs are not a verified contiguous sequence")
            csv = os.path.realpath(result["path"])
            if os.path.dirname(csv) != directory or not os.path.isfile(csv) or os.path.getsize(csv) == 0:
                raise ValueError(f"Completed output is missing, empty, or outside the data folder: job {index + 1}")
            with open(os.path.splitext(csv)[0] + "_metadata.json", encoding="utf-8") as handle:
                metadata = json.load(handle)
            if metadata.get("status") not in {"finished", "complete", "completed"}:
                raise ValueError(f"Completed output metadata does not confirm success: job {index + 1}")
            if metadata.get("safe_state", {}).get("ok") is False:
                raise ValueError(f"Completed output has failed output cleanup: job {index + 1}")
            if self._recipe_values(metadata["params"]) != self._recipe_values(requests[expected_condition - 1].params):
                raise ValueError(f"Completed output recipe differs: job {index + 1}")
        return saved

    def resume(self, checkpoint_path):
        try:
            with open(checkpoint_path, encoding="utf-8") as handle:
                saved = json.load(handle)
            text = ", ".join(str(float(value)) for value in saved["fields_t"])
        except Exception as exc:
            self.error.emit(f"Cannot read resume checkpoint: {exc}")
            return False
        return self.start(text, resume_checkpoint=checkpoint_path)

    def stop(self):
        if not self.active:
            return
        if self._state.stop_requested:
            return
        self._state.stop_requested = True
        if self._state.phase == "verifying":
            self._finish_stopped("Stopped while waiting for valid APS100 readings")
            return
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
        frozen = self._field_outputs.get((request.condition_index, field_t))
        if frozen is not None:
            return frozen
        run_id = self.tab._planned_output.run_id
        signal_chain = request.calibration[2] if request.calibration is not None else None
        parts = gate_scan_filename_parts(request.params, signal_chain, field_t)
        stem = compose_output_stem(self.tab.save.device_id, "gate_scan", request.params.base_name, parts, run_id)
        directory = self._base_output_dir or self.tab._planned_output.output_dir
        return planned_output_at(directory, stem, run_id)

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
            self._state.audit = None
            self._move_started_from_persistent = False
            self._complete_move_verification(self._snapshot)
            return
        # Fail closed before every operation that may require a new
        # persistent-switch heater cycle.  The separate post-move gate below
        # handles heat released by the cycle that has just completed.
        if self._thermal_armed():
            decision = self._evaluate_thermal(measurement=False)
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
        # Local validation can emit a terminal result before the call returns
        # its request ID. Buffer only during dispatch, then apply the usual ID
        # check so stale/duplicate results still cannot advance this job.
        self._dispatching_move = True
        self._early_move_results = []
        try:
            self._state.move_request_id = self._call_safe_move(target)
        except Exception as exc:
            self._fail(f"APS100 move request failed: {exc}")
        finally:
            self._dispatching_move = False
        results, self._early_move_results = self._early_move_results, []
        for result in results:
            self._on_move_result(result)

    def _thermal_armed(self):
        return self.thermal_safety is not None and bool(getattr(self.thermal_safety, "is_armed", getattr(self.thermal_safety, "armed", False)))

    @staticmethod
    def _is_preflight_time_hold(decision):
        return getattr(decision, "block_code", None) in {
            "stable_recovery_dwell", "minimum_heater_interval",
        }

    def _evaluate_thermal(self, *, measurement=None):
        if self.thermal_safety is None:
            return None
        snapshot = getattr(self.thermal_safety, "latest_snapshot", None)
        if measurement is None:
            measurement = self._thermal_wait_context == "post_move" or self._state.phase == "measuring"
        if measurement and hasattr(self.thermal_safety, "evaluate_measurement"):
            target = None
            if callable(self.measurement_temperature_provider):
                waiting, target = self.measurement_temperature_provider()
                if waiting and target is None:
                    from app.thermal_safety import ThermalDecision, ThermalState
                    return ThermalDecision(ThermalState.COOLDOWN_HOLD, False,
                        "Wait until stable is selected; set a sample temperature target first")
                if not waiting:
                    target = None
            return self.thermal_safety.evaluate_measurement(snapshot, sample_target_k=target)
        try:
            return self.thermal_safety.evaluate(snapshot)
        except TypeError:
            return self.thermal_safety.evaluate()

    def _request_temperature_refresh(self):
        refresh = getattr(self.lakeshore_controller, "refresh_snapshot", None)
        if callable(refresh):
            refresh()

    def continue_after_temperature_check(self):
        self._poll_cooldown(manual=True)

    def _poll_cooldown(self, *, manual=False):
        if not self.active or self._state.phase != "thermal_wait":
            self._cooldown_timer.stop()
            return
        timeout = float(getattr(getattr(self.thermal_safety, "config", None), "maximum_cooldown_wait_timeout_s", 3600.0))
        if self._cooldown_started_at is not None and time.monotonic() - self._cooldown_started_at >= timeout:
            self._cooldown_timer.stop()
            self._fail("Lake Shore cooldown timeout expired; operator inspection and restart are required")
            return
        decision = self._evaluate_thermal()
        state = str(getattr(getattr(decision, "state", None), "value", ""))
        if state in {"WARNING", "TRIPPED", "MONITOR_FAULT", "DISARMED"}:
            self._thermal_manual_required = True
        ready = decision is not None and decision.magnet_permission
        reason = getattr(decision, "reason", "Temperature monitoring unavailable")
        self.continuation_changed.emit(self._thermal_manual_required, bool(ready), str(reason))
        if decision is not None and decision.magnet_permission:
            if self._thermal_manual_required and not manual:
                return
            self._thermal_manual_required = False
            self.continuation_changed.emit(False, False, "")
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
        self._comparison_readbacks.clear()
        self._update_field_comparison(
            getattr(self.thermal_safety, "latest_snapshot", None),
            phase_override="precharge", target_override=target,
        )
        result = self.magnet.safe_move_to_field(
            target,
            final_mode="persistent",
            zero_leads=True,
            persistent_field_confirmed=bool(self._persistent_confirmation_granted),
            use_gate_scan_rate=True,
        )
        return result if isinstance(result, str) else None

    def _on_snapshot(self, snapshot):
        self._snapshot = snapshot
        self._snapshot_received_at = datetime.now(timezone.utc)
        note_field = getattr(self.thermal_safety, "note_magnet_snapshot", None)
        if callable(note_field):
            note_field(snapshot)
        if not self.active:
            return
        if self._state.phase == "verifying":
            self._complete_move_verification(snapshot)
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
        self._update_field_comparison(snapshot)
        self._check_live_thermal(snapshot)
        if self.active and self._state.phase == "thermal_wait":
            self._poll_cooldown()
        self._record_temperature_telemetry(snapshot)

    def _poll_measurement_thermal(self):
        if not self.active or self._state.phase != "measuring":
            self._measurement_monitor_timer.stop()
            return
        # A stalled/disconnected monitor may deliver no new signal at all.
        # Re-evaluate the cached timestamps without issuing hardware reads.
        self._check_live_thermal()

    def _check_live_thermal(self, snapshot=None):
        if self.thermal_safety is not None:
            evaluate = (self.thermal_safety.evaluate_continuation
                        if self._state.phase in {"moving", "measuring"} and hasattr(self.thermal_safety, "evaluate_continuation")
                        else self.thermal_safety.evaluate)
            try:
                decision = evaluate(snapshot)
            except Exception as exc:
                if self.active:
                    self._abort_batch(f"Lake Shore thermal evaluation failed: {exc}; manual restart required")
                return
            state = str(getattr(getattr(decision, "state", None), "value", ""))
            if self.active and (state == "TRIPPED" or
                                (self._state.phase in {"moving", "measuring"} and state in {"WARNING", "MONITOR_FAULT", "DISARMED"})):
                self._abort_batch(f"Lake Shore thermal stop: {decision.reason}; manual restart required")

    def _on_diagnostic_readback(self, event):
        # Copies of existing APS queries, not extra instrument I/O.
        if not self.active or event.get("request_id") != self._state.move_request_id:
            return
        self._comparison_readbacks[event["command"]] = dict(event)

    def _update_field_comparison(self, temperature, **kwargs):
        # Diagnostic code must never prevent the independent live interlock
        # from evaluating a temperature sample.
        try:
            self._evaluate_field_comparison(temperature, **kwargs)
        except Exception as exc:
            self._comparison_record = {
                "mode": "comparison_only", "state": "MONITOR_FAULT",
                "reason": f"Comparison failed: {type(exc).__name__}: {exc}",
                "timestamp": _system_local_iso(),
            }
            if self.thermal_safety is not None:
                self.thermal_safety.latest_field_comparison = None

    def _evaluate_field_comparison(self, temperature, *, phase_override=None, target_override=None):
        if not self.active or self._field_comparison is None:
            return
        if not getattr(self.thermal_safety.config, "field_envelope_comparison_enabled", True):
            self.thermal_safety.latest_field_comparison = None
            self._comparison_record = None
            return
        from app.devices.aps100_attodry1000_adapter import (
            field_response_to_tesla, parse_heater_state, decode_status_byte,
        )
        now = time.monotonic()
        key = (self._series_id, self._state.index)
        if key != self._comparison_job:
            self._field_comparison.reset()
            self._comparison_job = key
            self._comparison_last_key = None
        magnet = self._snapshot
        raw = self._comparison_readbacks
        if self._state.phase == "moving" and phase_override != "precharge":
            # Never pass a pre-move snapshot off as current telemetry.
            magnet = None
            try:
                field = raw["IMAG?"]
                status = raw["*STB?"]
                heater = parse_heater_state(raw["PSHTR?"]["response"])
                magnet = SimpleNamespace(
                    field_t=field_response_to_tesla(field["response"], cfg.magnet.coil_constant_t_per_a),
                    monotonic_s=min(field["monotonic_s"], status["monotonic_s"]),
                    heater_on=None if heater == 2 else bool(heater),
                    status=decode_status_byte(status["response"]),
                )
            except (KeyError, TypeError, ValueError, RuntimeError):
                pass
        target = self.event_context.get("target_t")
        phase = "operating" if self._state.phase == "moving" else "measurement"
        if self._state.phase == "thermal_wait" and self._thermal_wait_context == "preflight":
            phase = "precharge"
        if phase_override is not None:
            phase = phase_override
            target = target_override
        report = self._field_comparison.evaluate(temperature, magnet, now=now, phase=phase, target_t=target)
        record = report.to_dict()
        record.update({"timestamp": _system_local_iso(), "monotonic_s": now,
                       "context": self.event_context, "readbacks": deepcopy(raw)})
        self._comparison_record = record
        self.thermal_safety.latest_field_comparison = report
        setter = getattr(self.tab, "set_field_thermal_comparison", None)
        if callable(setter):
            setter(report)
        state_key = (report.state, report.ceiling_k, report.trajectory_ceiling_k)
        if state_key != self._comparison_last_key:
            self._comparison_last_key = state_key
            self._emit_activity("THERMAL_COMPARISON", report.display())

    def on_lakeshore_disconnected(self):
        if self.thermal_safety is not None:
            self.thermal_safety.latest_snapshot = None
            self.thermal_safety.evaluate(None)
        self._abort_batch("Lake Shore disconnected; temperature readings unavailable")

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
            record["field_envelope_comparison"] = self._comparison_record
            if self.thermal_safety is not None and hasattr(self.thermal_safety, "evaluate_continuation"):
                operating = self.thermal_safety.evaluate_continuation(snapshot)
                record["operating_permission"] = operating.magnet_permission
                record["operating_reason"] = operating.reason
                field_observation = getattr(self.thermal_safety, "latest_magnet_snapshot", None)
                record["live_field_observation"] = {
                    "field_t": getattr(field_observation, "field_t", None),
                    "monotonic_s": getattr(field_observation, "monotonic_s", None),
                }
            record["magnet_snapshot"] = to_jsonable(self._snapshot) if self._snapshot is not None else None
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
        self._comparison_readbacks.clear()
        self._snapshot_received_at = None
        self._persistent_confirmation_granted = False

    def _on_disconnected(self, *_args):
        self._snapshot = None
        self._comparison_readbacks.clear()
        self._snapshot_received_at = None
        self._persistent_confirmation_granted = False
        if self.active:
            if self._state.phase == "moving":
                self._fail("APS100 disconnected during persistent move")
            else:
                self._abort_batch("APS100 disconnected during Gate Scan")

    def _on_move_result(self, result):
        if not self.active:
            return
        if self._state.phase != "moving":
            return
        if self._dispatching_move:
            self._early_move_results.append(result)
            return
        request_id = result.get("request_id") if isinstance(result, dict) else None
        if not self._state.move_request_id or request_id != self._state.move_request_id:
            return
        # Consume the request before any verification or measurement start so
        # a duplicate/late signal cannot restart a completed job.
        self._state.move_request_id = None
        self._state.audit = result.get("audit")
        if getattr(self, "_pending_failure", None):
            self._fail(self._pending_failure)
            return
        if self._state.stop_requested:
            self._finish_stopped("Stopped during APS100 persistent move")
            return
        if not isinstance(result, dict) or not result.get("success"):
            self._fail(f"APS100 persistent move failed: {result.get('error', 'unknown error') if isinstance(result, dict) else result}")
            return
        self._state.audit = result.get("audit")
        self._complete_move_verification(result.get("snapshot"))

    def _complete_move_verification(self, snapshot):
        try:
            request, target = self._state.jobs[self._state.index]
            self._state.request = request
            self._verify_persistent_snapshot(snapshot, target)
        except _SnapshotUnavailable as exc:
            self._verification_reason = str(exc)
            if self._verification_deadline is None:
                self._verification_deadline = time.monotonic() + 5.0
                self._state.phase = "verifying"
                self._emit_state("Waiting for valid APS100 readings (up to 5 s)", self._state.index)
                self._emit_activity("WAIT", self._verification_reason)
                self._verification_timer.start()
            return
        except Exception as exc:
            self._fail(f"Persistent field verification failed: {exc}")
            return
        self._verification_timer.stop()
        self._verification_deadline = None
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
            decision = self._evaluate_thermal(measurement=True)
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

    def _poll_verification(self):
        if not self.active or self._state.phase != "verifying":
            self._verification_timer.stop()
            return
        if time.monotonic() >= self._verification_deadline:
            self._fail("APS100 readings did not recover within 5 s: " + self._verification_reason)
            return
        try:
            self._request_snapshot_refresh()
        except Exception as exc:
            self._verification_reason = f"Status refresh failed: {exc}"

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
        if self.thermal_safety is not None:
            self._measurement_monitor_timer.start()
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
            raise _SnapshotUnavailable("No final APS100 snapshot was returned")
        status = getattr(snapshot, "status", None)
        if status is not None and (getattr(status, "quench", False) or getattr(status, "power_module_failure", False)):
            raise ValueError("APS100 reports a quench or power-module fault")
        timestamp = getattr(snapshot, "monotonic_s", None)
        if timestamp is not None and (not math.isfinite(float(timestamp)) or
                                     not 0 <= time.monotonic() - float(timestamp) <= self._snapshot_max_age_s):
            raise _SnapshotUnavailable("APS100 snapshot is stale")
        for name in ("field_t", "output_field_t", "output_current_a"):
            try:
                valid = math.isfinite(float(getattr(snapshot, name, None)))
            except (TypeError, ValueError):
                valid = False
            if not valid:
                raise _SnapshotUnavailable(f"APS100 {name} readback is unavailable")
        if status is None or not isinstance(getattr(status, "sweep_active", None), bool):
            raise _SnapshotUnavailable("APS100 sweep status is unavailable")
        if not isinstance(getattr(snapshot, "heater_on", None), bool):
            raise _SnapshotUnavailable("APS100 heater status is unavailable")
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
        # APS100 firmware can report Pause / STB=0 with the persistent
        # switch off. Standby is a supply state, not the switch state.
        sweep_state = " ".join(str(getattr(snapshot, "sweep_state", "")).lower().split())
        paused = sweep_state in {"pause", "paused", "sweep paused"}
        if not getattr(status, "standby", False) and not paused:
            raise _SnapshotUnavailable(
                f"APS100 idle state is not confirmed: sweep={sweep_state!r}, "
                f"standby={getattr(status, 'standby', None)}"
            )
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
        self._measurement_monitor_timer.stop()
        if getattr(self, "_pending_failure", None):
            self._fail(self._pending_failure)
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
        if getattr(self, "_checkpoint_write_error", None):
            self._fail(self._checkpoint_write_error)
            return
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
        self._checkpoint_write_error = None
        if self._checkpoint_path is None or self._state.request is None:
            return
        payload = {
            "schema": "gate_scan_bfield_batch_checkpoint_v1",
            "batch_id": self._series_id or self._state.request.batch_id,
            "resumed_from": self._resumed_from,
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
                    "calibration": to_jsonable(request.calibration),
                }
                for request in dict((job[0].condition_index, job[0]) for job in self._state.jobs).values()
            ],
            "safe_move_audit": to_jsonable(self._state.audit),
            "field_envelope_comparison": self._comparison_record,
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
        except Exception as exc:
            self._checkpoint_write_error = f"Checkpoint save failed: {exc}"
            self._emit_activity("ERROR", self._checkpoint_write_error)
            try:
                if os.path.exists(temporary):
                    os.remove(temporary)
            except OSError:
                pass

    def _emit_state(self, phase, index):
        if phase != "thermal_wait":
            self.continuation_changed.emit(False, False, "")
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
        if self._checkpoint_write_error:
            message = self._checkpoint_write_error
            self._state.phase = "failed"
            self.error.emit(message)
            self._cleanup()
            return
        self.finished.emit()
        self._cleanup()

    def _cleanup(self):
        self._measurement_monitor_timer.stop()
        self._early_move_results = []
        self._verification_timer.stop()
        self._verification_deadline = None
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
        self._field_outputs = {}
        # Keep recording the final temperature trajectory after a stop. The
        # next explicitly started series selects a new telemetry file.
        self._cooldown_started_at = None
        self._thermal_wait_context = None
        self._last_persistent_field_t = None
        self._move_started_from_persistent = False
