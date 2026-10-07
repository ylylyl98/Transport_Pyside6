"""Comparison-only implementation of attoDRY1000 Table 3.

No method in this module commands hardware or grants live permission. Numeric
warning/recovery settings are deliberately unset until commissioning. Table 3
is an operating envelope, not a critical-temperature curve.
"""
from dataclasses import asdict, dataclass
import math


def finite(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def temperature_ceiling(field_t):
    if not finite(field_t) or abs(field_t) > 9:
        return None
    for field, ceiling in ((6, 5.5), (7, 5.0), (8, 4.5), (9, 4.2)):
        if abs(field_t) <= field:
            return ceiling


@dataclass(frozen=True)
class FieldThermalDecision:
    state: str
    reason: str
    actual_field_t: float | None
    target_t: float | None
    magnet_temperature_k: float | None
    ceiling_k: float | None
    trajectory_ceiling_k: float | None
    heater_on: bool | None
    mode: str = "comparison_only"
    source: str = "attoDRY1000 manual p25 Table 3"
    warning_margin_k: float | None = None
    recovery_hysteresis_k: float | None = None
    recovery_dwell_s: float | None = None
    hold_timeout_s: float | None = None
    maximum_recoveries: int | None = None

    def to_dict(self):
        return asdict(self)

    def display(self):
        field = "unknown" if self.actual_field_t is None else f"{self.actual_field_t:+.3f} T"
        temp = "unknown" if self.magnet_temperature_k is None else f"{self.magnet_temperature_k:.3f} K"
        ceiling = "unknown" if self.ceiling_k is None else f"<{self.ceiling_k:g} K"
        heater = {True: "ON", False: "OFF", None: "unknown"}[self.heater_on]
        return (f"Comparison only · {self.state} · magnet {field}, {temp}, "
                f"envelope {ceiling}, heater {heater}. {self.reason}")


class FieldThermalComparison:
    def __init__(self, config):
        self.config = config
        self.reset()

    def reset(self):
        self.hold_since = None
        self.recovery_since = None
        self.recoveries = 0
        self.latched_fault = None
        self.phase = None

    def evaluate(self, temperature, magnet, *, now, phase="operating", target_t=None,
                 first_stage_k=None, first_stage_monotonic_s=None):
        """Project hold/recovery decisions from observed data; never actuate.

        magnet must contain timestamped *magnet* field, not lead current. A
        projection of recovery is not evidence that the physical ramp paused.
        Faults latch for this comparison session until reset/new job.
        """
        c = self.config
        margin = getattr(c, "field_warning_margin_k", None)
        hysteresis = getattr(c, "field_recovery_hysteresis_k", None)
        dwell = getattr(c, "field_recovery_dwell_s", None)
        timeout = getattr(c, "field_hold_timeout_s", None)
        max_recoveries = getattr(c, "field_maximum_recoveries", None)
        field = getattr(magnet, "field_t", None)
        temp = getattr(temperature, "reservoir_temperature_k", None)
        heater = getattr(magnet, "heater_on", None)
        heater = heater if isinstance(heater, bool) else None
        ceiling = temperature_ceiling(field)
        trajectory = temperature_ceiling(max(abs(field), abs(target_t))) if finite(field) and finite(target_t) else ceiling

        def result(state, reason, latch=False):
            if state == "MONITOR_FAULT":
                self.recovery_since = None
            if latch:
                self.latched_fault = reason
            return FieldThermalDecision(state, reason, field if finite(field) else None,
                target_t if finite(target_t) else None, temp if finite(temp) else None,
                ceiling, trajectory, heater, warning_margin_k=margin,
                recovery_hysteresis_k=hysteresis, recovery_dwell_s=dwell,
                hold_timeout_s=timeout, maximum_recoveries=max_recoveries)

        if self.latched_fault:
            return result("FAULT", self.latched_fault)
        if self.phase != phase:
            self.recovery_since = None
            self.phase = phase
        age_limit = getattr(c, "maximum_reading_age_s", 3.0)
        if not finite(now) or not finite(age_limit) or age_limit <= 0:
            return result("MONITOR_FAULT", "Invalid clock or freshness configuration")
        if not getattr(c, "enabled", False) or not getattr(c, "verified_channel_mapping", False):
            return result("MONITOR_FAULT", "Temperature protection or channel mapping is not enabled")
        for snapshot, name in ((temperature, "temperature"), (magnet, "magnet")):
            stamp = getattr(snapshot, "monotonic_s", None)
            if not finite(stamp) or not 0 <= now - stamp <= age_limit:
                self.recovery_since = None
                return result("MONITOR_FAULT", f"Missing or stale {name} telemetry")
        # Share the status parser, but not the live evaluator's recovery state.
        from app.thermal_safety import _sensor_status_is_clear
        if (not getattr(temperature, "connected", False) or
            not getattr(temperature, "communication_valid", False) or
            any(not _sensor_status_is_clear(getattr(temperature, key, None))
                for key in ("sample_sensor_status", "reservoir_sensor_status"))):
            self.recovery_since = None
            return result("MONITOR_FAULT", "Temperature connection or sensor fault")
        sample = getattr(temperature, "sample_temperature_k", None)
        if not finite(temp) or not finite(sample) or ceiling is None or trajectory is None:
            return result("MONITOR_FAULT", "Invalid temperature/field or field outside the documented 9 T range")
        if target_t is not None and not finite(target_t):
            return result("MONITOR_FAULT", "Invalid target field")
        if heater is None:
            return result("MONITOR_FAULT", "Heater state is unverified")
        status = getattr(magnet, "status", None)
        if status is None or any(not isinstance(getattr(status, flag, None), bool)
                                 for flag in ("quench", "power_module_failure")):
            return result("MONITOR_FAULT", "APS hardware status unavailable")
        if getattr(status, "quench", False) or getattr(status, "power_module_failure", False):
            return result("FAULT", "APS hardware fault", True)
        if temp >= ceiling:
            return result("FAULT", f"Magnet temperature {temp:g} K reaches the {ceiling:g} K envelope boundary", True)
        if sample >= c.sample_trip_temperature_k:
            return result("FAULT", "Sample temperature exceeds its independent trip setting", True)
        if phase == "precharge":
            if temp > 4.2:
                return result("PRECHARGE_BLOCKED", "Magnet must be at or below 4.2 K before charging")
            if (not finite(first_stage_k) or not finite(first_stage_monotonic_s) or
                not 0 <= now - first_stage_monotonic_s <= age_limit):
                return result("PRECHARGE_READY", "Magnet temperature check passed; first-stage temperature is not monitored on this installation")
            if first_stage_k >= 65:
                return result("PRECHARGE_BLOCKED", "First stage must be below 65 K")
            return result("PRECHARGE_READY", "Documented pre-charge temperature checks passed")
        if phase == "measurement":
            if (temp > c.reservoir_recovery_temperature_k or sample > c.sample_recovery_temperature_k):
                self.recovery_since = None
                return result("MEASUREMENT_WAIT", "Waiting for measurement temperature recovery")
            if self.recovery_since is None:
                self.recovery_since = now
            if now - self.recovery_since < c.required_stable_recovery_dwell_s:
                return result("MEASUREMENT_WAIT", "Verifying stable measurement temperature")
            return result("MEASUREMENT_READY", "Measurement temperature recovery verified")
        configured = (all(finite(v) and v >= 0 for v in (margin, hysteresis, dwell)) and
                      finite(timeout) and timeout > 0 and isinstance(max_recoveries, int) and
                      not isinstance(max_recoveries, bool) and max_recoveries >= 0)
        if not configured:
            state = "TRAJECTORY_BLOCKED" if temp >= trajectory else "WITHIN_ENVELOPE"
            return result(state, "Warning/recovery margins and stop procedure are not commissioned; live protection unchanged")
        effective = min(ceiling, trajectory)
        warning = temp >= effective - margin or sample >= c.sample_warning_temperature_k
        if warning:
            if self.hold_since is None:
                self.hold_since = now
            self.recovery_since = None
        if self.hold_since is not None:
            if now - self.hold_since >= timeout:
                return result("FAULT", "Projected thermal hold timed out; inspect heater and temperature", True)
            recovered = (temp < effective - margin - hysteresis and
                         sample <= c.sample_recovery_temperature_k)
            if not recovered:
                self.recovery_since = None
                return result("THERMAL_HOLD", "Would pause ramp; heater may remain ON")
            if self.recovery_since is None:
                self.recovery_since = now
            if now - self.recovery_since < dwell:
                return result("RECOVERY_VERIFY", "Would wait for stable recovery before continuing")
            if self.recoveries >= max_recoveries:
                return result("FAULT", "Projected recovery limit reached", True)
            self.recoveries += 1
            self.hold_since = self.recovery_since = None
            return result("CONTINUE", "Would continue the unfinished target; no hardware action in comparison mode")
        return result("WITHIN_ENVELOPE", "Actual field and remaining trajectory are inside the proposed envelope")
