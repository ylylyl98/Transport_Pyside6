"""Pure source-programming resolution checks; never query an instrument."""
import math
import re

# Tektronix Models 2400/2401 specifications, source resolution (not accuracy).
# https://www.tek.com/en/documents/specification/models-2400-2401-2400-lv-and-2400-c-sourcemeter-specifications
RANGES = ((.2, .000005), (2., .00005), (20., .0005), (200., .005))

def source_resolution(session):
    identity = str(getattr(session, "identity", ""))
    if not re.search(r"\bMODEL\s+2400(?=,|$)", identity, re.I):
        return None
    value = getattr(session, "cached_source_voltage_range_v", None)
    if value is None:
        return None
    for nominal, resolution in RANGES:
        if any(math.isclose(abs(float(value)), scale * nominal, rel_tol=1e-6) for scale in (1., 1.05)):
            return resolution
    return None

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
