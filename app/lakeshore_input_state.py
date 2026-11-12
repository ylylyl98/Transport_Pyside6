"""Read-only presentation and idle-state checks for LS335 input configuration."""
import math
import time

from utils.config import cfg


def input_state_fresh(stamp):
    return (isinstance(stamp, (int, float)) and math.isfinite(stamp)
            and 0 <= time.monotonic() - stamp <= max(10., 3 * cfg.lakeshore335.control_polling_interval_s))


def current_reversal_summary(inputs, stamp, connected=True):
    if not connected:
        return "LS335 current reversal: disconnected"
    if not inputs or not input_state_fresh(stamp):
        return "LS335 current reversal: unknown / stale — refresh inputs in Instruments"
    parts = []
    for channel in ("A", "B"):
        state = inputs.get(channel, {})
        enabled = state.get("current_reversal")
        value = ("not applicable" if state.get("supported") is False else
                 "ON (alternating)" if enabled is True else "OFF (DC)" if enabled is False else "unknown")
        role = ("sample" if channel == cfg.lakeshore335.sample_channel else
                "magnet" if channel == cfg.lakeshore335.reservoir_channel else "unassigned")
        parts.append(f"{channel} ({role}): {value}")
    return "LS335 current reversal — " + "; ".join(parts)


def input_change_magnet_block(snapshot, connected):
    """Allow stationary nonzero field; never infer idle from absent telemetry."""
    if not connected:
        return ""
    stamp = getattr(snapshot, "monotonic_s", None)
    if (not isinstance(stamp, (int, float)) or not math.isfinite(stamp)
            or not 0 <= time.monotonic() - stamp <= cfg.lakeshore335.maximum_field_reading_age_s):
        return "Refresh APS100 telemetry before changing LS335 current reversal"
    status = getattr(snapshot, "status", None)
    if (getattr(status, "sweep_active", None) is not False
            or getattr(status, "faulted", None) is not False
            or any(getattr(status, name, False) for name in ("quench", "power_module_failure"))
            or str(getattr(snapshot, "sweep_state", "")).strip().lower() not in {"idle", "standby", "paused", "pause"}):
        return "Wait for a stationary, fault-free APS100 before changing LS335 current reversal"
    return ""
