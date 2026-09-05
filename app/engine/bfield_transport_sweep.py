"""Planning and output helpers for the continuous B-field transport sweep.

The planner is deliberately independent of Qt and hardware.  This keeps the
field/rate safety rules testable and gives the UI a single source of truth for
its preview.  Hardware workers should call :func:`validate_setup` immediately
before reserving APS100 control.
"""

from __future__ import annotations

import csv
import json
import math
import os
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Iterable, Mapping

from app.gate_transform import derived_to_gates
from app.models import BFieldTransportCondition, BFieldTransportParams
from app.run_output import PlannedOutput, sanitize_segment
from app.signal_chain import signal_chain_filename_parts
from utils.config import cfg


COIL_CONSTANT_T_PER_A = 0.20328
MAX_DRIVEN_FIELD_T = 6.0
MAX_RATE_A_PER_S = 0.03430
MAX_RATE_T_PER_MIN = MAX_RATE_A_PER_S * COIL_CONSTANT_T_PER_A * 60.0
DEFAULT_VOLTAGE_MARGIN_V = 1.0
# APS100 field readback/command resolution used by transport endpoint guards.
# A trajectory shorter than this cannot be distinguished reliably from one
# field value to the next.
APS100_FIELD_RESOLUTION_T = 1e-4
# Backward/short-name aliases for downstream scripts.
MAX_BFIELD_T = MAX_DRIVEN_FIELD_T
MAX_RATE_T_MIN = MAX_RATE_T_PER_MIN
COOLDOWN_POLICIES = ("adaptive", "stay_driven", "persistent_each_row")
COOLDOWN_POLICY_LABELS = {
    "adaptive": "Adaptive (recommended)",
    "stay_driven": "Stay driven for batch",
    "persistent_each_row": "Persistent after every row",
}
FINAL_MODES = ("persistent", "driven")
FINAL_MODE_LABELS = {
    "persistent": "Persistent (cool and zero leads)",
    "driven": "Driven (leave heater ON)",
}


class BFieldTransportSafetyError(ValueError):
    """Raised when a requested transport sweep is outside its envelope."""


def parse_condition_series(text: str, label: str, *, maximum: int = 100) -> tuple[float, ...]:
    """Parse scalars, bracketed arrays, and exclusive ``start:stop:step`` ranges."""
    source = str(text or "").strip()
    if source.startswith("[") or source.endswith("]"):
        if not (source.startswith("[") and source.endswith("]")):
            raise ValueError(f"{label}: array brackets must be paired")
        source = source[1:-1]
    values: list[float] = []
    for raw in source.replace(";", ",").replace("\n", ",").split(","):
        token = raw.strip()
        if not token:
            continue
        if ":" not in token:
            try:
                value = float(token)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"{label}: {token!r} is not a number") from exc
            if not math.isfinite(value):
                raise ValueError(f"{label}: values must be finite")
            values.append(value)
        else:
            parts = [part.strip() for part in token.split(":")]
            if len(parts) != 3 or any(not part for part in parts):
                raise ValueError(f"{label}: use start:stop:step for ranges")
            try:
                start, stop, step = (float(part) for part in parts)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"{label}: range values must be numbers") from exc
            if not all(math.isfinite(value) for value in (start, stop, step)):
                raise ValueError(f"{label}: range values must be finite")
            if step == 0:
                raise ValueError(f"{label}: range step cannot be zero")
            distance = (stop - start) / step
            if not math.isfinite(distance):
                raise ValueError(f"{label}: range is too large")
            if distance < 0:
                raise ValueError(f"{label}: range step direction does not reach its stop")
            count = max(0, math.ceil(distance))
            if len(values) + count > maximum:
                raise ValueError(f"{label}: series cannot exceed {maximum} values")
            for index in range(count):
                value = start + index * step
                if (step > 0 and value >= stop) or (step < 0 and value <= stop):
                    break
                values.append(0.0 if abs(value) < 0.5e-12 else value)
        if len(values) > maximum:
            raise ValueError(f"{label}: series cannot exceed {maximum} values")
    if not values:
        raise ValueError(f"Enter at least one {label} value")
    return tuple(0.0 if abs(value) < 0.5e-12 else value for value in values)


def broadcast_condition_series(
    first: tuple[float, ...],
    second: tuple[float, ...],
    first_label: str,
    second_label: str,
) -> tuple[tuple[float, float], ...]:
    """Pair equal arrays or broadcast either scalar across the other array."""
    if len(first) == len(second):
        return tuple(zip(first, second))
    if len(first) == 1:
        return tuple((first[0], value) for value in second)
    if len(second) == 1:
        return tuple((value, second[0]) for value in first)
    raise ValueError(
        f"{first_label} and {second_label} must have equal lengths, or one must be a single value"
    )


def transport_output_summary_parts(
    params: BFieldTransportParams,
    signal_chain=None,
) -> list[str]:
    """Return the filename tags shared by the transport preview and runner."""
    direction = "round_trip" if params.round_trip else "one_way"
    enabled_count = sum(1 for condition in params.conditions if condition.enabled)
    parts = [
        f"B_{params.start_field_t:g}to{params.stop_field_t:g}T",
        direction,
        f"rate_{params.rate_t_per_min:g}Tpermin",
        f"{enabled_count}conditions",
    ]
    if signal_chain is not None:
        parts.extend(signal_chain_filename_parts(signal_chain))
    return parts


@dataclass(frozen=True)
class BFieldTransportOutputPaths:
    """Every file produced by one multi-condition transport series."""

    planned: PlannedOutput
    condition_csv_paths: tuple[str, ...]
    manifest_path: str
    checkpoint_path: str
    log_path: str

    @property
    def all_paths(self) -> tuple[str, ...]:
        return (
            *self.condition_csv_paths,
            self.manifest_path,
            self.checkpoint_path,
            self.log_path,
        )


def build_transport_output_paths(
    planned: PlannedOutput,
    conditions: Iterable[BFieldTransportCondition],
) -> BFieldTransportOutputPaths:
    """Expand a series stem into readable, collision-checkable output paths."""
    condition_paths = []
    for index, condition in enumerate(conditions, start=1):
        fallback = f"condition_{index}"
        condition_name = sanitize_segment(condition.name, fallback)
        condition_paths.append(
            os.path.join(
                planned.output_dir,
                f"{planned.stem}_C{index:02d}_{condition_name}.csv",
            )
        )
    return BFieldTransportOutputPaths(
        planned=planned,
        condition_csv_paths=tuple(condition_paths),
        manifest_path=os.path.join(planned.output_dir, planned.stem + "_series_manifest.json"),
        checkpoint_path=os.path.join(planned.output_dir, planned.stem + "_series_checkpoint.json"),
        log_path=os.path.join(planned.output_dir, planned.stem + "_series_log.txt"),
    )


def normalize_cooldown_policy(policy: str | None) -> str:
    value = str(policy or "adaptive").strip().lower()
    aliases = {"persistent": "persistent_each_row", "stay-driven": "stay_driven", "stay driven": "stay_driven"}
    value = aliases.get(value, value)
    if value not in COOLDOWN_POLICIES:
        raise BFieldTransportSafetyError(f"Unknown cooldown policy: {policy}")
    return value


def normalize_final_mode(mode: str | None) -> str:
    value = str(mode or "persistent").strip().lower().replace(" ", "_")
    aliases = {"safe": "persistent", "heater_on": "driven"}
    value = aliases.get(value, value)
    if value not in FINAL_MODES:
        raise BFieldTransportSafetyError("Final APS100 mode must be Persistent or Driven")
    return value


def estimate_transport_times(
    start_field_t: float,
    stop_field_t: float,
    rate_t_per_min: float,
    condition_count: int,
    *,
    round_trip: bool = True,
    bias_settle_s: float = 0.5,
    cooldown_policy: str = "adaptive",
    heater_cool_s: float | None = None,
    recovery_dwell_s: float | None = None,
    heater_warm_s: float | None = None,
    lead_zero_rematch_s: float | None = None,
) -> dict[str, float | str | bool]:
    """Estimate run time without inventing a thermal recovery duration.

    The sweep and bias portions are deterministic.  Thermal recovery is
    reported separately and marked variable when commissioning did not supply
    all transition timings.
    """
    policy = normalize_cooldown_policy(cooldown_policy)
    distance_s = abs(float(stop_field_t) - float(start_field_t)) / float(rate_t_per_min) * 60.0
    sweep_s = distance_s * (2.0 if round_trip else 1.0)
    row_s = sweep_s + max(0.0, float(bias_settle_s))
    rows = max(0, int(condition_count))
    deterministic = row_s * rows
    # Persistent row boundaries require output zero/rematch at the field where
    # the trajectory ends, plus a return to start for one-way trajectories.
    # These are deterministic field-motion times; they are not thermal
    # recovery.  ``lead_zero_rematch_s`` is retained for API compatibility but
    # intentionally ignored so callers cannot double-count this movement.
    end_field = float(start_field_t) if round_trip else float(stop_field_t)
    per_transition_zero_rematch_s = 2.0 * abs(end_field) / float(rate_t_per_min) * 60.0
    positioning_s = 0.0
    if policy == "persistent_each_row" and rows > 1:
        positioning_s = (per_transition_zero_rematch_s + (0.0 if round_trip else distance_s)) * (rows - 1)
    thermal_extra = 0.0
    thermal_known = True
    fixed_thermal_extra = 0.0
    if policy == "persistent_each_row" and rows > 1:
        parts = (heater_cool_s, recovery_dwell_s, heater_warm_s, lead_zero_rematch_s)
        thermal_known = all(value is not None and math.isfinite(float(value)) and float(value) >= 0 for value in parts[:3])
        fixed_thermal_extra = sum(float(value) for value in parts[:3] if value is not None and math.isfinite(float(value)) and float(value) >= 0) * (rows - 1)
        thermal_extra = fixed_thermal_extra
        # Temperature recovery beyond the commissioned fixed dwell is always
        # potentially variable, even when all fixed timing terms are known.
        thermal_known = False
    elif policy == "adaptive":
        thermal_known = False
    return {
        "row_s": row_s,
        "sweep_s_per_row": sweep_s,
        "batch_s": deterministic + thermal_extra + positioning_s,
        "field_positioning_extra_s": positioning_s,
        "thermal_extra_s": thermal_extra,
        "fixed_thermal_extra_s": fixed_thermal_extra,
        "thermal_recovery_variable": not thermal_known,
        "cooldown_policy": policy,
        "round_trip": bool(round_trip),
    }


def rate_t_per_min_to_a_per_s(rate_t_per_min: float, coil_constant: float | None = None) -> float:
    rate = float(rate_t_per_min)
    coil = float(getattr(cfg.magnet, "coil_constant_t_per_a", COIL_CONSTANT_T_PER_A) if coil_constant is None else coil_constant)
    if not math.isfinite(rate) or rate <= 0:
        raise BFieldTransportSafetyError("Sweep rate must be positive and finite")
    if not math.isfinite(coil) or coil <= 0:
        raise BFieldTransportSafetyError("Coil constant must be positive and finite")
    return rate / (coil * 60.0)


def rate_a_per_s_to_t_per_min(rate_a_per_s: float, coil_constant: float | None = None) -> float:
    value = float(rate_a_per_s)
    coil = float(getattr(cfg.magnet, "coil_constant_t_per_a", COIL_CONSTANT_T_PER_A) if coil_constant is None else coil_constant)
    if not math.isfinite(value) or value < 0:
        raise BFieldTransportSafetyError("APS100 rate must be finite and non-negative")
    if not math.isfinite(coil) or coil <= 0:
        raise BFieldTransportSafetyError("Coil constant must be positive and finite")
    return value * coil * 60.0


def validate_voltage_margin(
    rate_t_per_min: float,
    *,
    inductance_h: float | None,
    voltage_limit_v: float | None,
    margin_v: float = DEFAULT_VOLTAGE_MARGIN_V,
    coil_constant: float | None = None,
) -> float | None:
    """Return predicted magnet voltage, or ``None`` when not commissioned.

    An unknown inductance or voltage limit is not silently replaced with a
    guessed value.  The absolute commissioned app rate cap still applies.
    """
    if inductance_h is None or voltage_limit_v is None:
        return None
    inductance = float(inductance_h)
    limit = float(voltage_limit_v)
    margin = float(margin_v)
    if not all(math.isfinite(v) for v in (inductance, limit, margin)) or inductance <= 0 or limit <= margin:
        raise BFieldTransportSafetyError("Commissioned inductance/voltage margin is invalid")
    predicted = inductance * rate_t_per_min_to_a_per_s(rate_t_per_min, coil_constant)
    if predicted > limit - margin + 1e-12:
        raise BFieldTransportSafetyError(
            f"Sweep rate would require {predicted:.6g} V; voltage limit with {margin:g} V margin is {limit - margin:.6g} V"
        )
    return predicted


def effective_rate_limit_t_per_min(
    *,
    inductance_h: float | None = None,
    voltage_limit_v: float | None = None,
    margin_v: float = DEFAULT_VOLTAGE_MARGIN_V,
    coil_constant: float | None = None,
    max_rate_a_per_s: float | None = None,
) -> float:
    """Return the lower of the absolute cap and commissioned voltage cap."""
    configured_max = float(getattr(cfg.magnet, "maximum_rate_a_per_s", MAX_RATE_A_PER_S) if max_rate_a_per_s is None else max_rate_a_per_s)
    hard = rate_a_per_s_to_t_per_min(configured_max, coil_constant)
    if inductance_h is None or voltage_limit_v is None:
        return hard
    inductance, limit, margin = float(inductance_h), float(voltage_limit_v), float(margin_v)
    if not all(math.isfinite(v) for v in (inductance, limit, margin)) or inductance <= 0 or limit <= margin:
        raise BFieldTransportSafetyError("Commissioned inductance/voltage margin is invalid")
    voltage_cap_a = (limit - margin) / inductance
    return min(hard, rate_a_per_s_to_t_per_min(voltage_cap_a, coil_constant))


def validate_field_bounds(start_field_t: float, stop_field_t: float, *, max_field_t: float = MAX_DRIVEN_FIELD_T) -> tuple[float, float]:
    start, stop, limit = float(start_field_t), float(stop_field_t), float(max_field_t)
    if not all(math.isfinite(v) for v in (start, stop, limit)):
        raise BFieldTransportSafetyError("B-field bounds must be finite")
    if limit <= 0 or abs(start) > limit + 1e-12 or abs(stop) > limit + 1e-12:
        raise BFieldTransportSafetyError(f"Start and stop must be within ±{limit:g} T driven-mode envelope")
    if math.isclose(start, stop, abs_tol=1e-12):
        raise BFieldTransportSafetyError("Start and stop fields must be different")
    if abs(stop - start) < 2.0 * APS100_FIELD_RESOLUTION_T:
        raise BFieldTransportSafetyError(
            "B-field span is below the APS100 transport resolution "
            f"({2.0 * APS100_FIELD_RESOLUTION_T:g} T minimum)"
        )
    return start, stop


def validate_setup(
    start_field_t: float,
    stop_field_t: float,
    rate_t_per_min: float,
    *,
    max_field_t: float = MAX_DRIVEN_FIELD_T,
    max_rate_a_per_s: float | None = None,
    coil_constant: float | None = None,
    inductance_h: float | None = None,
    voltage_limit_v: float | None = None,
    voltage_margin_v: float = DEFAULT_VOLTAGE_MARGIN_V,
) -> dict[str, float | None]:
    start, stop = validate_field_bounds(start_field_t, stop_field_t, max_field_t=max_field_t)
    configured_max = float(getattr(cfg.magnet, "maximum_rate_a_per_s", MAX_RATE_A_PER_S) if max_rate_a_per_s is None else max_rate_a_per_s)
    rate_a = rate_t_per_min_to_a_per_s(rate_t_per_min, coil_constant)
    if rate_a > configured_max + 1e-12:
        raise BFieldTransportSafetyError(
            f"Sweep rate {rate_a:.7g} A/s exceeds the absolute {configured_max:.7g} A/s cap"
        )
    voltage = validate_voltage_margin(
        rate_t_per_min, inductance_h=inductance_h, voltage_limit_v=voltage_limit_v,
        margin_v=voltage_margin_v, coil_constant=coil_constant,
    )
    return {
        "start_field_t": start,
        "stop_field_t": stop,
        "rate_t_per_min": float(rate_t_per_min),
        "rate_a_per_s": rate_a,
        "predicted_voltage_v": voltage,
        "max_field_t": float(max_field_t),
        "max_rate_a_per_s": configured_max,
    }


def build_trajectory(start_field_t: float, stop_field_t: float, round_trip: bool = True) -> tuple[tuple[float, str], ...]:
    """Return endpoint legs used by the sweep state machine.

    The magnet is continuously swept by APS100; endpoints are represented as
    explicit legs so acquisition can label outbound/return data correctly.
    """
    start, stop = validate_field_bounds(start_field_t, stop_field_t)
    legs = [(start, "start"), (stop, "forward")]
    if bool(round_trip):
        legs.append((start, "backward"))
    return tuple(legs)


def enabled_conditions(
    conditions: Iterable[BFieldTransportCondition],
    *, ratio: float | None = None,
    ratio_target: str | None = None,
) -> tuple[BFieldTransportCondition, ...]:
    result = tuple(c for c in conditions if bool(getattr(c, "enabled", True)))
    if not result:
        raise BFieldTransportSafetyError("Enable at least one fixed transport condition")
    for condition in result:
        resolved_ratio = float(condition.ratio if ratio is None else ratio)
        resolved_target = condition.ratio_target if ratio_target is None else ratio_target
        if not math.isfinite(resolved_ratio) or abs(resolved_ratio) < 1e-12:
            raise BFieldTransportSafetyError(f"Condition {condition.name or 'unnamed'} requires a non-zero coupling ratio")
        condition.ratio = resolved_ratio
        condition.ratio_target = resolved_target
        condition.vtg, condition.vbg = derived_to_gates(condition.doping, condition.efield, resolved_ratio, resolved_target)
    return result


def build_jobs(
    conditions: Iterable[BFieldTransportCondition],
    *,
    start_field_t: float = -0.5,
    stop_field_t: float = 0.5,
    round_trip: bool = True,
) -> tuple[dict, ...]:
    """Expand enabled rows into ordered condition/leg jobs."""
    rows = enabled_conditions(conditions)
    jobs = []
    for index, condition in enumerate(rows, start=1):
        legs = build_trajectory(start_field_t, stop_field_t, round_trip)
        jobs.append({"condition_index": index, "condition": condition, "legs": legs})
    return tuple(jobs)


@dataclass
class TransportCsvWriter:
    """Crash-tolerant per-condition writer used by a transport worker."""

    path: str
    condition: BFieldTransportCondition
    handle: object = field(init=False, repr=False)
    writer: object = field(init=False, repr=False)

    COLUMNS = (
        "Index", "Timestamp", "Elapsed_s", "Condition_index", "Condition_name", "Direction",
        "B_measured_T", "B_target_T", "Sweep_rate_T_per_min", "Sweep_rate_A_per_s",
        "Doping", "E-field", "Vtg", "Vbg", "Vds", "Vds_measured",
        "raw_X", "raw_Y", "raw_DC", "Ids_X", "Ids_Y", "Ids_DC", "Keithley_current",
        "Sample_temperature_K", "Reservoir_temperature_K",
        "Acquisition_started", "Acquisition_finished", "Keithley_read_error",
    )

    def __post_init__(self):
        os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
        self.handle = open(self.path, "x", newline="", encoding="utf-8", buffering=1)
        self.writer = csv.DictWriter(self.handle, fieldnames=self.COLUMNS, extrasaction="ignore")
        self.writer.writeheader()
        self.handle.flush()

    def write(self, record: Mapping):
        self.writer.writerow(dict(record))
        self.handle.flush()
        try:
            os.fsync(self.handle.fileno())
        except OSError:
            pass

    def close(self):
        if not self.handle.closed:
            self.handle.close()

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()


def write_series_manifest(path: str, *, params: BFieldTransportParams, validation: Mapping, status: str = "running", results: Iterable[Mapping] = (), cleanup_failures: Iterable[str] = (), runtime: Mapping | None = None) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    params_payload = asdict(params)
    for condition_payload in params_payload.get("conditions", []):
        condition_payload.pop("ratio", None)
        condition_payload.pop("ratio_target", None)
    condition_payloads = []
    for condition in params.conditions:
        item = asdict(condition)
        item.pop("ratio", None)
        item.pop("ratio_target", None)
        condition_payloads.append(item)
    payload = {
        "schema": "bfield_transport_manifest_v1",
        "updated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "status": str(status),
        "params": params_payload,
        "validation": dict(validation),
        "conditions": condition_payloads,
        "results": list(results),
        "cleanup_failures": list(cleanup_failures),
    }
    if runtime is not None:
        payload["runtime"] = dict(runtime)
    temporary = path + ".tmp"
    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, default=str)
        handle.write("\n")
    os.replace(temporary, path)


@dataclass(frozen=True)
class TransportSweepPlan:
    """Frozen, validated execution plan shared by UI and workers."""

    params: BFieldTransportParams
    validation: Mapping
    conditions: tuple[BFieldTransportCondition, ...]
    legs: tuple[tuple[float, str], ...]

    @classmethod
    def from_params(cls, params: BFieldTransportParams, **validation_kwargs):
        params.cooldown_policy = normalize_cooldown_policy(params.cooldown_policy)
        # Successful transport completion always keeps the heater on, including
        # recipes saved before this policy. Failure cleanup remains Persistent.
        params.final_mode = "driven"
        validation = validate_setup(
            params.start_field_t, params.stop_field_t, params.rate_t_per_min,
            **validation_kwargs,
        )
        conditions = enabled_conditions(params.conditions, ratio=params.ratio, ratio_target=params.ratio_target)
        return cls(
            params=params,
            validation=validation,
            conditions=conditions,
            legs=build_trajectory(params.start_field_t, params.stop_field_t, params.round_trip),
        )

    def jobs(self) -> tuple[dict, ...]:
        return tuple(
            {"condition_index": index, "condition": condition, "legs": self.legs}
            for index, condition in enumerate(self.conditions, start=1)
        )


class BFieldTransportSweep:
    """Small synchronous safety runner for adapters and deterministic tests.

    Production Qt code can execute this object on its worker thread.  Device
    ownership remains with the caller; this class only operates the adapter
    supplied by that owner and always attempts a safe persistent cleanup.
    """

    def __init__(self, adapter, plan: TransportSweepPlan, *, apply_condition=None, acquire=None,
                 stop_requested=None, thermal_check=None, fault_check=None,
                 output_dir=None, output_stem="bfield_transport"):
        self.adapter = adapter
        self.plan = plan
        self.apply_condition = apply_condition or (lambda _condition: None)
        self.acquire = acquire or (lambda _condition, _direction, _snapshot: None)
        self.stop_requested = stop_requested or (lambda: False)
        self.thermal_check = thermal_check or (lambda _snapshot: True)
        self.fault_check = fault_check or (lambda _snapshot: None)
        self.output_dir = output_dir
        self.output_stem = str(output_stem or "bfield_transport")
        self.results: list[dict] = []
        self.cleanup_failures: list[str] = []
        self.restoration: dict = {}
        self._writers: dict[int, TransportCsvWriter] = {}
        self._manifest_path = None
        self._checkpoint_path = None

    def _stop_if_requested(self):
        if self.stop_requested():
            pause = getattr(self.adapter, "pause", None)
            if callable(pause):
                pause(confirm=False)
            raise BFieldTransportSafetyError("Transport sweep stopped by user")

    def run(self, *, persistent_field_confirmed: bool = False) -> list[dict]:
        params = self.plan.params
        final_mode = "driven"
        started = False
        captured_rates = captured_limits = None
        status = "stopped"
        original_error = None
        try:
            self._stop_if_requested()
            # Rate and limit writes happen before any field motion.  Adapter
            # implementations must read back and verify their own commands.
            if hasattr(self.adapter, "get_rates"):
                captured_rates = self.adapter.get_rates()
            if hasattr(self.adapter, "get_limits_t"):
                captured_limits = self.adapter.get_limits_t()
            if hasattr(self.adapter, "set_rate_t_per_min"):
                changed = self.adapter.set_rate_t_per_min(
                    params.rate_t_per_min, max_abs_field_t=MAX_DRIVEN_FIELD_T
                )
                if not changed:
                    raise BFieldTransportSafetyError("APS100 did not report any verified RATE settings")
            if hasattr(self.adapter, "set_limits_t"):
                actual_limits = self.adapter.set_limits_t(-MAX_DRIVEN_FIELD_T, MAX_DRIVEN_FIELD_T)
                if abs(float(actual_limits[0]) + MAX_DRIVEN_FIELD_T) > 2e-4 or abs(float(actual_limits[1]) - MAX_DRIVEN_FIELD_T) > 2e-4:
                    raise BFieldTransportSafetyError("APS100 field-limit readback does not match the ±6 T transport envelope")
            if self.output_dir:
                os.makedirs(self.output_dir, exist_ok=True)
                self._manifest_path = os.path.join(self.output_dir, self.output_stem + "_series_manifest.json")
                self._checkpoint_path = os.path.join(self.output_dir, self.output_stem + "_series_checkpoint.json")
                write_series_manifest(self._manifest_path, params=params, validation=self.plan.validation)
                write_series_manifest(self._checkpoint_path, params=params, validation=self.plan.validation)
                for index, condition in enumerate(self.plan.conditions, start=1):
                    self._writers[index] = TransportCsvWriter(
                        os.path.join(self.output_dir, f"{self.output_stem}_C{index}.csv"), condition
                    )
            move = getattr(self.adapter, "safe_move_to_field", None)
            if callable(move):
                # The safe-move operation may already have matched leads or
                # changed the heater before reporting an error.  Treat its
                # invocation as the start of hardware ownership so every
                # failure path still attempts conservative persistent cleanup.
                started = True
                move(
                    params.start_field_t, final_mode="driven", zero_leads=False,
                    persistent_field_confirmed=bool(persistent_field_confirmed),
                )
            else:
                started = True
            for condition_index, condition in enumerate(self.plan.conditions, start=1):
                self._stop_if_requested()
                self.apply_condition(condition)
                for leg_index, (target, direction) in enumerate(self.plan.legs):
                    self._stop_if_requested()
                    if leg_index == 0:
                        snapshot = self.adapter.read_snapshot() if hasattr(self.adapter, "read_snapshot") else None
                        self._sample_snapshot(condition_index, condition, direction, target, snapshot)
                    else:
                        self.adapter.start_sweep_to(target)
                        def progress(_value):
                            self._stop_if_requested()
                            live = self.adapter.read_snapshot() if hasattr(self.adapter, "read_snapshot") else _value
                            self._check_live_safety(live)
                            self._sample_snapshot(condition_index, condition, direction, target, live)
                        snapshot = self.adapter.wait_for_field(
                            target, stop_event=None, progress=progress,
                        )
                        if hasattr(self.adapter, "read_snapshot"):
                            snapshot = self.adapter.read_snapshot()
                        self._check_live_safety(snapshot)
                    measured = getattr(snapshot, "field_t", snapshot)
                    record = {
                        "condition_index": condition_index,
                        "condition_name": condition.name,
                        "direction": direction,
                        "B_target_T": float(target),
                        "B_measured_T": float(measured) if measured is not None else None,
                        "Doping": float(condition.doping),
                        "E-field": float(condition.efield),
                        "Vtg": float(condition.vtg),
                        "Vbg": float(condition.vbg),
                        "Vds": float(condition.vds),
                    }
                    # Endpoint records are already emitted by progress for
                    # adapters that report the final value; retain one final
                    # authoritative snapshot for adapters that do not.
                    if not self.results or self.results[-1].get("condition_index") != condition_index or self.results[-1].get("direction") != direction or abs(float(self.results[-1].get("B_measured_T") or 1e9) - float(record.get("B_measured_T") or 0)) > 1e-10:
                        self._record(condition_index, condition, direction, target, snapshot)
                if condition_index < len(self.plan.conditions):
                    pause = getattr(self.adapter, "pause", None)
                    if callable(pause):
                        try:
                            pause(confirm=False)
                        except TypeError:
                            pause()
            status = "finished"
            return list(self.results)
        except Exception as exc:
            original_error = exc
            status = "failed"
            raise
        finally:
            pause = getattr(self.adapter, "pause", None)
            if started and callable(pause):
                try:
                    pause(confirm=False)
                except TypeError:
                    pause()
            persistent = getattr(self.adapter, "enter_persistent_mode", None)
            if started and status == "finished" and final_mode == "driven":
                # The endpoint is already paused by the transport leg; leave
                # the confirmed heater ON only for an otherwise successful
                # recipe. Any exception path retains conservative cleanup.
                pass
            elif started and callable(persistent):
                try:
                    persistent(zero_leads=True)
                except Exception as exc:
                    self.cleanup_failures.append(f"persistent cleanup failed: {exc}")
            if captured_rates is not None and hasattr(self.adapter, "restore_rates"):
                try:
                    self.adapter.restore_rates(captured_rates)
                except Exception as exc:
                    self.cleanup_failures.append(f"RATE restoration failed: {exc}")
            if captured_limits is not None and hasattr(self.adapter, "set_limits_t"):
                try:
                    self.adapter.set_limits_t(*captured_limits)
                except Exception as exc:
                    self.cleanup_failures.append(f"limit restoration failed: {exc}")
            self.restoration = {
                "rates": captured_rates,
                "limits": captured_limits,
                "final_mode": final_mode if status == "finished" else "persistent",
                "cleanup_failures": list(self.cleanup_failures),
            }
            for writer in self._writers.values():
                try:
                    writer.close()
                except Exception as exc:
                    self.cleanup_failures.append(f"CSV close failed: {exc}")
            if self.cleanup_failures and original_error is None:
                status = "cleanup_failed"
            if self._manifest_path:
                write_series_manifest(self._manifest_path, params=params, validation=self.plan.validation, status=status, results=self.results, cleanup_failures=self.cleanup_failures)
            if self._checkpoint_path:
                write_series_manifest(self._checkpoint_path, params=params, validation=self.plan.validation, status=status, results=self.results, cleanup_failures=self.cleanup_failures)
            if self.cleanup_failures and original_error is None:
                raise BFieldTransportSafetyError("; ".join(self.cleanup_failures))

    def _check_live_safety(self, snapshot):
        if snapshot is None:
            return
        age = getattr(snapshot, "reading_age_s", None)
        if age is not None:
            try:
                if float(age) > 3.0:
                    raise BFieldTransportSafetyError("APS100 telemetry is stale during transport sweep")
            except (TypeError, ValueError):
                raise BFieldTransportSafetyError("APS100 telemetry age is invalid during transport sweep")
        status = getattr(snapshot, "status", None)
        if status is not None and (getattr(status, "quench", False) or getattr(status, "power_module_failure", False) or getattr(status, "faulted", False)):
            raise BFieldTransportSafetyError("APS100 fault/quench/power-module failure reported during transport sweep")
        fault = self.fault_check(snapshot)
        if fault:
            raise BFieldTransportSafetyError(str(fault))
        if not self.thermal_check(snapshot):
            raise BFieldTransportSafetyError("Lake Shore thermal permission was revoked during transport sweep")
        voltage = getattr(snapshot, "magnet_voltage_v", None)
        if voltage is None and hasattr(self.adapter, "get_magnet_voltage_v"):
            voltage = self.adapter.get_magnet_voltage_v()
        limit = getattr(snapshot, "voltage_limit_v", None)
        if limit is None and hasattr(self.adapter, "get_voltage_limit_v"):
            limit = self.adapter.get_voltage_limit_v()
        if voltage is not None and limit is not None:
            try:
                if abs(float(voltage)) > float(limit) + 1e-9:
                    raise BFieldTransportSafetyError(
                        f"APS100 magnet voltage {float(voltage):.6g} V exceeds {float(limit):.6g} V limit"
                    )
            except (TypeError, ValueError):
                raise BFieldTransportSafetyError("APS100 voltage telemetry is invalid during transport sweep")

    def _record(self, condition_index, condition, direction, target, snapshot):
        measured = getattr(snapshot, "field_t", snapshot)
        record = {
            "condition_index": condition_index, "condition_name": condition.name,
            "direction": direction, "B_target_T": float(target),
            "B_measured_T": float(measured) if measured is not None else None,
            "Doping": float(condition.doping), "E-field": float(condition.efield),
            "Vtg": float(condition.vtg), "Vbg": float(condition.vbg), "Vds": float(condition.vds),
        }
        self.results.append(record)
        self.acquire(condition, direction, snapshot)
        writer = self._writers.get(condition_index)
        if writer:
            writer.write(record)
        if self._checkpoint_path:
            write_series_manifest(self._checkpoint_path, params=self.plan.params, validation=self.plan.validation, status="running", results=self.results)

    def _sample_snapshot(self, condition_index, condition, direction, target, snapshot):
        self._check_live_safety(snapshot)
        # Continuous acquisition is delegated to ``acquire`` on every fresh
        # APS snapshot, not only when wait_for_field returns at an endpoint.
        self._record(condition_index, condition, direction, target, snapshot)
