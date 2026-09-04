"""Centralized, fail-closed Lake Shore permission evaluator."""

from __future__ import annotations

from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from enum import Enum
import math
import time


class ThermalState(str, Enum):
    SAFE = "SAFE"
    WARNING = "WARNING"
    COOLDOWN_HOLD = "COOLDOWN_HOLD"
    TRIPPED = "TRIPPED"
    MONITOR_FAULT = "MONITOR_FAULT"
    DISARMED = "DISARMED"


@dataclass(frozen=True)
class ThermalDecision:
    state: ThermalState
    magnet_permission: bool
    reason: str
    reading_age_s: float | None = None
    block_code: str | None = None

    @property
    def message(self):
        return self.reason


def _finite(value):
    try:
        value = float(value)
    except (TypeError, ValueError):
        return False
    return math.isfinite(value)


def _sensor_status_is_clear(status):
    """Return whether an RDGST? decimal bitmask is valid and has no flags set."""
    try:
        value = Decimal(str(status).strip())
    except (InvalidOperation, ValueError):
        return False
    return value.is_finite() and value == 0


class ThermalSafetyEvaluator:
    """Single authority for whether a *new* persistent move may begin."""

    def __init__(self, config, *, clock=time.monotonic):
        self.config = config
        self.clock = clock
        self.state = ThermalState.DISARMED
        self.magnet_permission = False
        self.reason = "Lake Shore interlock is disarmed or unconfigured"
        self.latest_snapshot = None
        self._stable_since = None
        self._last_heater_activation = None
        self.heater_activation_timestamps = []
        self.heater_activation_events = []
        self.events = {"warnings": [], "cooldown_holds": [], "trips": [], "recoveries": [], "communication_faults": []}
        self._communication_fault_active = False
        self._communication_fault_count = 0
        self._last_communication_fault_key = None

    @property
    def is_armed(self):
        required = (
            "sample_warning_temperature_k", "sample_trip_temperature_k", "sample_recovery_temperature_k",
            "reservoir_warning_temperature_k", "reservoir_trip_temperature_k", "reservoir_recovery_temperature_k",
        )
        if not (getattr(self.config, "enabled", False) and
                getattr(self.config, "verified_channel_mapping", False) and
                all(_finite(getattr(self.config, key, None)) for key in required)):
            return False
        maximum_age = getattr(self.config, "maximum_reading_age_s", None)
        if not _finite(maximum_age) or float(maximum_age) <= 0:
            return False
        sample_channel = getattr(self.config, "sample_channel", None)
        reservoir_channel = getattr(self.config, "reservoir_channel", None)
        if sample_channel not in {"A", "B"} or reservoir_channel not in {"A", "B"} or sample_channel == reservoir_channel:
            return False
        for prefix in ("sample", "reservoir"):
            warning = float(getattr(self.config, f"{prefix}_warning_temperature_k"))
            trip = float(getattr(self.config, f"{prefix}_trip_temperature_k"))
            recovery = float(getattr(self.config, f"{prefix}_recovery_temperature_k"))
            if not recovery <= warning < trip:
                return False
        return True

    @property
    def armed(self):
        return self.is_armed

    def note_heater_activation(self, timestamp=None):
        self._last_heater_activation = self.clock() if timestamp is None else float(timestamp)
        self.heater_activation_timestamps.append(self._last_heater_activation)
        self.heater_activation_events.append({
            "monotonic_s": self._last_heater_activation,
            "utc_timestamp": datetime.now(timezone.utc).isoformat(),
        })

    notify_heater_activation = note_heater_activation

    @property
    def last_heater_activation(self):
        return self._last_heater_activation

    def _set(self, state, permission, reason, age=None, block_code=None):
        old = self.state
        self.state, self.magnet_permission, self.reason = state, bool(permission), str(reason)
        if state == ThermalState.WARNING and old != state:
            self.events["warnings"].append(self.clock())
        elif state == ThermalState.COOLDOWN_HOLD and old != state:
            self.events["cooldown_holds"].append(self.clock())
        elif state == ThermalState.TRIPPED and old != state:
            self.events["trips"].append(self.clock())
        elif state == ThermalState.SAFE and old in {ThermalState.TRIPPED, ThermalState.COOLDOWN_HOLD}:
            self.events["recoveries"].append(self.clock())
        return ThermalDecision(state, bool(permission), str(reason), age, block_code)

    def evaluate(self, snapshot=None, now=None) -> ThermalDecision:
        now = self.clock() if now is None else float(now)
        if snapshot is not None:
            self.latest_snapshot = snapshot
        if not self.is_armed:
            return self._set(ThermalState.DISARMED, False, "Lake Shore interlock is disarmed or thresholds/mapping are not commissioned")
        snapshot = self.latest_snapshot
        if snapshot is None:
            self._stable_since = None
            return self._set(ThermalState.MONITOR_FAULT, False, "Lake Shore snapshot is unavailable")
        if not hasattr(snapshot, "monotonic_s"):
            self._stable_since = None
            return self._set(ThermalState.MONITOR_FAULT, False, "Lake Shore timing metadata is missing", None)
        try:
            monotonic_s = float(getattr(snapshot, "monotonic_s"))
            if not math.isfinite(monotonic_s) or monotonic_s < 0 or not math.isfinite(now):
                raise ValueError
            age = now - monotonic_s
        except (TypeError, ValueError):
            self._stable_since = None
            return self._set(ThermalState.MONITOR_FAULT, False, "Lake Shore timing metadata is invalid", None)
        max_age = float(getattr(self.config, "maximum_reading_age_s", 3.0))
        if (not getattr(snapshot, "connected", False) or not getattr(snapshot, "communication_valid", False)):
            fault_key = (
                getattr(snapshot, "monotonic_s", None),
                getattr(snapshot, "diagnostic_error", None),
            )
            if not self._communication_fault_active:
                self.events["communication_faults"].append({"timestamp": now, "reason": getattr(snapshot, "diagnostic_error", None)})
                del self.events["communication_faults"][:-1000]
                self._communication_fault_active = True
            if fault_key != self._last_communication_fault_key:
                self._communication_fault_count += 1
                self._last_communication_fault_key = fault_key
            self._stable_since = None
            return self._set(ThermalState.MONITOR_FAULT, False, "Lake Shore is disconnected or communication is invalid", age)
        self._communication_fault_active = False
        if age < 0 or not math.isfinite(age) or age > max_age:
            self._stable_since = None
            return self._set(ThermalState.MONITOR_FAULT, False, f"Lake Shore reading is stale ({max(0.0, age):.1f} s old)", age)
        sample = getattr(snapshot, "sample_temperature_k", None)
        reservoir = getattr(snapshot, "reservoir_temperature_k", None)
        if not _finite(sample) or not _finite(reservoir):
            self._stable_since = None
            return self._set(ThermalState.MONITOR_FAULT, False, "Lake Shore temperature is unavailable or non-finite", age)
        statuses = (getattr(snapshot, "sample_sensor_status", None), getattr(snapshot, "reservoir_sensor_status", None))
        if any(not _sensor_status_is_clear(status) for status in statuses):
            self._stable_since = None
            return self._set(ThermalState.MONITOR_FAULT, False, f"Lake Shore sensor status is invalid: {statuses[0]!r}, {statuses[1]!r}", age)
        sample, reservoir = float(sample), float(reservoir)
        trip_s = float(self.config.sample_trip_temperature_k)
        trip_r = float(self.config.reservoir_trip_temperature_k)
        if sample >= trip_s or reservoir >= trip_r:
            self._stable_since = None
            return self._set(ThermalState.TRIPPED, False, "Lake Shore temperature is at or above the configured trip threshold", age)
        warn_s = float(self.config.sample_warning_temperature_k)
        warn_r = float(self.config.reservoir_warning_temperature_k)
        if sample >= warn_s or reservoir >= warn_r:
            self._stable_since = None
            return self._set(ThermalState.WARNING, False, "Lake Shore temperature is at or above the configured warning threshold", age)
        # Recovery thresholds provide hysteresis after warning/trip.  A fresh
        # process starts in hold until the configured stable dwell is met.
        recovered = (sample <= float(self.config.sample_recovery_temperature_k) and
                     reservoir <= float(self.config.reservoir_recovery_temperature_k))
        if not recovered:
            self._stable_since = None
            return self._set(ThermalState.COOLDOWN_HOLD, False, "Temperatures are below warning but have not reached recovery thresholds", age)
        max_slope = getattr(self.config, "maximum_positive_slope_k_per_min", None)
        slopes = (getattr(snapshot, "sample_slope_k_per_min", None), getattr(snapshot, "reservoir_slope_k_per_min", None))
        if max_slope is not None and (any(_finite(s) and float(s) > float(max_slope) for s in slopes) or any(not _finite(s) for s in slopes)):
            self._stable_since = None
            return self._set(ThermalState.COOLDOWN_HOLD, False, "Temperature warming rate exceeds the configured limit", age)
        if self._stable_since is None:
            self._stable_since = now
        dwell = max(0.0, float(getattr(self.config, "required_stable_recovery_dwell_s", 0.0)))
        if now - self._stable_since < dwell:
            return self._set(
                ThermalState.COOLDOWN_HOLD, False,
                f"Stable recovery dwell in progress ({now - self._stable_since:.1f}/{dwell:.1f} s)",
                age, "stable_recovery_dwell",
            )
        interval = max(0.0, float(getattr(self.config, "minimum_interval_between_heater_activations_s", 0.0)))
        if self._last_heater_activation is not None and now - self._last_heater_activation < interval:
            return self._set(
                ThermalState.COOLDOWN_HOLD, False,
                f"Minimum heater interval in progress ({now - self._last_heater_activation:.1f}/{interval:.1f} s)",
                age, "minimum_heater_interval",
            )
        return self._set(ThermalState.SAFE, True, "Lake Shore temperatures are valid, fresh, recovered, and stable", age)

    evaluate_snapshot = evaluate

    def check(self, snapshot=None, now=None):
        return self.evaluate(snapshot, now)

    def snapshot_dict(self):
        decision = self.evaluate()
        snap = self.latest_snapshot
        return {
            "state": decision.state.value,
            "magnet_permission": decision.magnet_permission,
            "reason": decision.reason,
            "block_code": decision.block_code,
            "reading_age_s": decision.reading_age_s,
            "identity": getattr(snap, "identity", None),
            "sample_temperature_k": getattr(snap, "sample_temperature_k", None),
            "reservoir_temperature_k": getattr(snap, "reservoir_temperature_k", None),
            "sample_sensor_status": getattr(snap, "sample_sensor_status", None),
            "reservoir_sensor_status": getattr(snap, "reservoir_sensor_status", None),
            "sample_slope_k_per_min": getattr(snap, "sample_slope_k_per_min", None),
            "reservoir_slope_k_per_min": getattr(snap, "reservoir_slope_k_per_min", None),
            "connected": getattr(snap, "connected", False),
            "communication_valid": getattr(snap, "communication_valid", False),
            "diagnostic_error": getattr(snap, "diagnostic_error", None),
            "communication_fault_count": self._communication_fault_count,
            "heater_activation_timestamps": list(self.heater_activation_timestamps),
            "heater_activation_events": list(self.heater_activation_events),
        }

    to_dict = snapshot_dict
