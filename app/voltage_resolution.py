"""Pure source-programming resolution checks; never query an instrument."""
import math
import re
from dataclasses import dataclass

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

def validate_voltage_points(points, resolutions):
    """Check physical outputs, including fixed values and derived coordinates."""
    labels = {"vtg":"Vtg", "vbg":"Vbg", "vds":"Vds"}
    for point in points:
        for axis, quantum in resolutions.items():
            value = point[axis]
            ticks = value / quantum
            if not math.isfinite(ticks) or abs(ticks - round(ticks)) > 1e-7:
                nearest = round(ticks) * quantum if math.isfinite(ticks) else 0
                raise ValueError(f"{labels[axis]} target {value:.9g} V is not representable on the cached source range: "
                                 f"resolution {quantum:.9g} V. Nearest target {nearest:.9g} V. "
                                 "Adjust Start/Stop/Step (or derived ratio); no rounding was applied.")


def validate_source_targets(session, targets, label, ramp_step=None, refresh=False):
    """Validate the physical setpoints of one Keithley against its active range.

    When requested, perform one read-only range query before validating the
    complete trajectory. No target is silently rounded.
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
        if abs(ticks - round(ticks)) > 1e-7:
            nearest = round(ticks) * quantum
            raise ValueError(f"{label} target {value:.9g} V is not representable at {quantum:g} V source resolution. Nearest target {nearest:.9g} V; no rounding was applied.")
    return quantum
