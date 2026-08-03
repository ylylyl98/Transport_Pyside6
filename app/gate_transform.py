from __future__ import annotations


RATIO_TARGET_VBG = "Vbg"
RATIO_TARGET_VTG = "Vtg"
RATIO_TARGETS = (RATIO_TARGET_VBG, RATIO_TARGET_VTG)


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
