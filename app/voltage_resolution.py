"""Pure source-programming resolution checks; never query an instrument."""
import math
import re
from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP

# Tektronix Models 2400/2401 specifications, source resolution (not accuracy).
# https://www.tek.com/en/documents/specification/models-2400-2401-2400-lv-and-2400-c-sourcemeter-specifications
RANGES = ((.2, .000005), (2., .00005), (20., .0005), (200., .005))
MODEL_RANGES = {"2400": RANGES, "2401": RANGES[:-1]}


@dataclass(frozen=True)
class SourceResolution:
    quantum: float | None
    message: str
    refreshable: bool = False


def source_resolution_status(session):
    """Explain resolution using cached state only, including why it is unknown."""
    if session is None:
        return SourceResolution(None, "source resolution unconfirmed: source is not connected.")
    identity = str(getattr(session, "identity", "")).strip()
    match = re.search(r"(?:^|,)\s*MODEL\s+([^,]+?)\s*(?=,|$)", identity, re.I)
    model = match.group(1).upper() if match else None
    value = getattr(session, "cached_source_voltage_range_v", None)
    range_text = "unavailable" if value is None else f"{value} V"
    if model not in MODEL_RANGES:
        return SourceResolution(None,
            f"source resolution unconfirmed: unsupported identity {identity!r}; "
            f"cached range {range_text}. Supported models: Keithley 2400 and 2401.")
    if value is None:
        error = getattr(session, "cached_source_voltage_range_error", "")
        detail = f" Last range read failed: {error}." if error else ""
        return SourceResolution(None,
            f"MODEL {model}; cached range unavailable; source resolution unconfirmed.{detail} "
            "Refresh the source readback before starting.", refreshable=True)
    try:
        voltage_range = float(value)
    except (TypeError, ValueError, OverflowError):
        voltage_range = math.nan
    for nominal, resolution in MODEL_RANGES[model]:
        if any(math.isclose(voltage_range, scale * nominal, rel_tol=1e-6) for scale in (1., 1.05)):
            return SourceResolution(resolution,
                f"MODEL {model}; cached range {voltage_range:g} V; {resolution:g} V source resolution.")
    return SourceResolution(None,
        f"MODEL {model}; source resolution unconfirmed: unsupported cached range {range_text}. "
        "Check the source range in Devices.")


def source_resolution(session):
    return source_resolution_status(session).quantum


def round_voltage(value, quantum):
    """Nearest source tick, with exact decimal half-ticks away from zero."""
    value, quantum = float(value), float(quantum)
    if not math.isfinite(value) or not math.isfinite(quantum) or quantum <= 0:
        raise ValueError("Voltage and source resolution must be finite; resolution must be positive.")
    tick = (Decimal(str(value)) / Decimal(str(quantum))).to_integral_value(rounding=ROUND_HALF_UP)
    result = float(tick * Decimal(str(quantum)))
    return result if result else 0.0


def rounded_source_target(session, target, label="Keithley", *, limit=None):
    """Validate both the requested and rounded voltage before any output."""
    quantum = validate_source_targets(session, (target,), label)
    rounded = round_voltage(target, quantum)
    if limit is not None:
        limit = float(limit)
        if not math.isfinite(limit) or limit <= 0 or max(abs(float(target)), abs(rounded)) > limit + 1e-12:
            raise ValueError(f"{label} requested or rounded target exceeds verified {limit:g} V protection.")
    return rounded


def validate_voltage_points(points, resolutions):
    """Resolve physical outputs in place; preserve requested trajectory coordinates."""
    for point in points:
        for axis, quantum in resolutions.items():
            point[axis] = round_voltage(point[axis], quantum)


def validate_source_targets(session, targets, label, ramp_step=None, refresh=False):
    """Validate the physical setpoints of one Keithley against its active range.

    When requested, perform one read-only range query before validating the
    complete trajectory. Off-grid targets use the nearest programming increment.
    This check does not mutate the caller's requested values.
    """
    if session is None:
        raise ValueError(f"{label}: source is not connected.")
    if refresh:
        reader = getattr(session, "refresh_source_voltage_range", None)
        if not callable(reader):
            raise ValueError(f"{label}: source range refresh is unavailable.")
        try:
            reader()
        except Exception as exc:
            raise ValueError(f"{label}: source range refresh failed: {exc}") from exc
    status = source_resolution_status(session)
    quantum = status.quantum
    if quantum is None:
        raise ValueError(f"{label}: {status.message}")
    source_range = float(session.cached_source_voltage_range_v)
    if ramp_step is not None:
        step = float(ramp_step)
        if not math.isfinite(step) or step <= 0 or step + quantum * 1e-9 < quantum:
            raise ValueError(f"{label}: ramp step must be finite and at least {quantum:g} V source resolution.")
    for target in targets:
        value = float(target)
        ticks = value / quantum
        if not math.isfinite(ticks):
            raise ValueError(f"{label} target must be finite.")
        if abs(value) > source_range + quantum * 1e-7:
            raise ValueError(f"{label} target {value:.9g} V exceeds the {source_range:g} V source range.")
        rounded = round_voltage(value, quantum)
        if abs(rounded) > source_range + quantum * 1e-7:
            raise ValueError(f"{label} rounded target {rounded:.9g} V exceeds the {source_range:g} V source range.")
        limit = getattr(session, "_max_source_voltage", None)
        if limit is not None and max(abs(value), abs(rounded)) > float(limit) + 1e-12:
            raise ValueError(f"{label} requested or rounded target exceeds configured {float(limit):g} V protection.")
    return quantum
