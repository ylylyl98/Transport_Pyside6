from __future__ import annotations
import math


RATIO_TARGET_VBG = "Vbg"
RATIO_TARGET_VTG = "Vtg"
RATIO_TARGETS = (RATIO_TARGET_VBG, RATIO_TARGET_VTG)


def derived_axis_limits(ratio, ratio_target, axis, held, vtg_limit, vbg_limit):
    """Intersect physical gate bounds at a fixed value of the other coordinate."""
    values = (ratio, held, vtg_limit, vbg_limit)
    if not all(math.isfinite(float(v)) for v in values):
        raise ValueError("Derived coordinates and gate limits must be finite")
    if vtg_limit <= 0 or vbg_limit <= 0:
        raise ValueError("Gate limits must be positive")
    if axis not in ("Doping", "E-field"):
        raise ValueError("Unknown derived axis")
    # D+E is bounded by twice the weighted top gate; D-E by the bottom.
    normalize_ratio_target(ratio_target)
    if abs(ratio) < 1e-12:
        raise ValueError("Derived trajectory requires a non-zero ratio.")
    top = 2 * vtg_limit * (abs(ratio) if ratio_target == RATIO_TARGET_VTG else 1)
    bottom = 2 * vbg_limit * (abs(ratio) if ratio_target == RATIO_TARGET_VBG else 1)
    lower = max(-top - held, held - bottom)
    upper = min(top - held, held + bottom)
    if lower > upper:
        raise ValueError("Held coordinate cannot be reached within physical gate limits")
    return lower, upper


def normalize_ratio_target(target: str) -> str:
    normalized = str(target).strip()
    if normalized not in RATIO_TARGETS:
        raise ValueError(f"Ratio target must be one of: {', '.join(RATIO_TARGETS)}")
    return normalized


def gates_to_derived(vtg: float, vbg: float, ratio: float, ratio_target: str) -> tuple[float, float]:
    """Convert physical gate voltages to (Doping, E-field)."""
    ratio_target = normalize_ratio_target(ratio_target)
    if ratio_target == RATIO_TARGET_VTG:
        weighted_vtg = float(ratio) * float(vtg)
        return weighted_vtg + float(vbg), weighted_vtg - float(vbg)
    weighted_vbg = float(ratio) * float(vbg)
    return float(vtg) + weighted_vbg, float(vtg) - weighted_vbg


def derived_to_gates(doping: float, efield: float, ratio: float, ratio_target: str) -> tuple[float, float]:
    """Convert (Doping, E-field) to the physical (Vtg, Vbg) setpoints."""
    ratio = float(ratio)
    if abs(ratio) < 1e-12:
        raise ValueError("Derived trajectory requires a non-zero ratio.")
    ratio_target = normalize_ratio_target(ratio_target)
    if ratio_target == RATIO_TARGET_VTG:
        return (float(doping) + float(efield)) / (2.0 * ratio), (float(doping) - float(efield)) / 2.0
    return (float(doping) + float(efield)) / 2.0, (float(doping) - float(efield)) / (2.0 * ratio)


def ratio_formula_text(ratio_target: str) -> str:
    ratio_target = normalize_ratio_target(ratio_target)
    if ratio_target == RATIO_TARGET_VTG:
        return "Doping = r*Vtg + Vbg\nE-field = r*Vtg - Vbg"
    return "Doping = Vtg + r*Vbg\nE-field = Vtg - r*Vbg"


def doping_axis_label(ratio: float, ratio_target: str) -> str:
    ratio_target = normalize_ratio_target(ratio_target)
    if ratio_target == RATIO_TARGET_VTG:
        return f"Doping ({ratio:.2f}*Vtg + Vbg)"
    return f"Doping (Vtg + {ratio:.2f}*Vbg)"


def efield_axis_label(ratio: float, ratio_target: str) -> str:
    ratio_target = normalize_ratio_target(ratio_target)
    if ratio_target == RATIO_TARGET_VTG:
        return f"E-field ({ratio:.2f}*Vtg - Vbg)"
    return f"E-field (Vtg - {ratio:.2f}*Vbg)"
