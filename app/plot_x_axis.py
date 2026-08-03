from __future__ import annotations

from collections.abc import Mapping

from app.gate_transform import doping_axis_label, efield_axis_label


FOLLOW_SWEEP = "Follow Sweep"
STEP_INDEX = "Step Index"
PLOT_X_AXES = (FOLLOW_SWEEP, STEP_INDEX, "Vtg", "Vbg", "Vds", "Doping", "E-field")


def normalize_plot_x_selection(selection: str) -> str:
    normalized = str(selection).strip()
    if normalized == "Auto":
        return FOLLOW_SWEEP
    if normalized == "Efield":
        return "E-field"
    if normalized not in PLOT_X_AXES:
        raise ValueError(f"Plot X axis must be one of: {', '.join(PLOT_X_AXES)}")
    return normalized


def resolve_gate_scan_x_axis(
    selection: str,
    mode: str,
    derived_axis: str,
    raw_vtg_active: bool,
    raw_vbg_active: bool,
    raw_vds_active: bool,
) -> str:
    selection = normalize_plot_x_selection(selection)
    if selection != FOLLOW_SWEEP:
        return selection
    if str(mode).strip().lower() == "derived":
        return "E-field" if str(derived_axis).strip() in {"Efield", "E-field"} else "Doping"
    active_axes = [
        axis
        for axis, active in (
            ("Vtg", raw_vtg_active),
            ("Vbg", raw_vbg_active),
            ("Vds", raw_vds_active),
        )
        if active
    ]
    return active_axes[0] if len(active_axes) == 1 else STEP_INDEX


def resolve_map_x_axis(selection: str, fast_axis: str) -> str:
    selection = normalize_plot_x_selection(selection)
    return str(fast_axis) if selection == FOLLOW_SWEEP else selection


def record_x_value(record: Mapping[str, object], resolved_axis: str) -> float:
    key = {
        STEP_INDEX: "index",
        "Vtg": "vtg",
        "Vbg": "vbg",
        "Vds": "vds",
        "Doping": "doping",
        "E-field": "efield",
        "Efield": "efield",
    }.get(resolved_axis)
    if key is None:
        raise ValueError(f"Unsupported resolved plot X axis: {resolved_axis}")
    return float(record[key])


def plot_x_axis_label(resolved_axis: str, ratio: float, ratio_target: str) -> str:
    if resolved_axis == STEP_INDEX:
        return STEP_INDEX
    if resolved_axis == "Doping":
        return doping_axis_label(ratio, ratio_target) + " (V)"
    if resolved_axis in {"Efield", "E-field"}:
        return efield_axis_label(ratio, ratio_target) + " (V)"
    return f"{resolved_axis} (V)"

