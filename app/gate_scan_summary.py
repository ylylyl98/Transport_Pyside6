"""Human-readable summaries for frozen Gate Scan condition recipes.

This module deliberately works from :class:`LineSweepParams` rather than live
Qt widgets.  The same formatter can therefore be used for the condition list,
the selected-condition details view, and any future run confirmation or
metadata preview without accidentally reflecting unsaved editor values.
"""

from __future__ import annotations

from app.gate_transform import derived_to_gates, gates_to_derived, normalize_ratio_target


def _number(value: float) -> str:
    """Format a recipe value compactly while retaining useful precision."""
    return f"{float(value):.3f}"


def _step(start: float, stop: float, count: int, active: bool = True) -> float:
    if not active or int(count) <= 1:
        return 0.0
    return abs(float(stop) - float(start)) / float(int(count) - 1)


def _conversion_endpoints(params, vtg_start: float, vtg_stop: float, vbg_start: float, vbg_stop: float):
    """Return derived endpoint values or a user-facing conversion error."""
    try:
        ratio = float(params.derived_ratio)
        target = normalize_ratio_target(params.derived_ratio_target)
        return (
            gates_to_derived(vtg_start, vbg_start, ratio, target),
            gates_to_derived(vtg_stop, vbg_stop, ratio, target),
        ), None
    except (TypeError, ValueError) as exc:
        return None, f"Not configured — conversion unavailable: {exc}."


def format_gate_scan_condition(params, *, details: bool = False) -> str:
    """Format one frozen Gate Scan recipe as wrapped, human-readable lines.

    The compact form is intended for a wrapped table cell.  ``details=True``
    adds the complete recipe, including acquisition and output-relevant axes.
    No values are written back to ``params``.
    """
    count = max(1, int(getattr(params, "n_points", 1)))
    bidirectional = bool(getattr(params, "sweep_both_ways", False))
    lines: list[str] = []
    mode = str(getattr(params, "mode", "Raw"))
    if mode == "Raw":
        active_axes = [
            axis for axis, active in (("Vtg", params.raw_vtg_active), ("Vbg", params.raw_vbg_active), ("Vds", params.raw_vds_active))
            if active
        ]
        lines.append("RAW · sweep " + ", ".join(active_axes or ["none"]))
        rows = (
            ("Vtg", params.raw_vtg_start, params.raw_vtg_stop, params.raw_vtg_active),
            ("Vbg", params.raw_vbg_start, params.raw_vbg_stop, params.raw_vbg_active),
            ("Vds", params.raw_vds_start, params.raw_vds_stop, params.raw_vds_active),
        )
        for axis, start, stop, active in rows:
            if active:
                if count <= 1:
                    lines.append(f"{axis}: {_number(start)} V (1 point)")
                else:
                    lines.append(f"{axis}: {_number(start)} → {_number(stop)} V ({_number(_step(start, stop, count))} V/pt)")
            else:
                lines.append(f"{axis}: {_number(start)} V (fixed)")
        endpoints, error = _conversion_endpoints(
            params,
            params.raw_vtg_start,
            params.raw_vtg_stop if params.raw_vtg_active and count > 1 else params.raw_vtg_start,
            params.raw_vbg_start,
            params.raw_vbg_stop if params.raw_vbg_active and count > 1 else params.raw_vbg_start,
        )
        if error:
            lines.append(error)
        else:
            (d0, e0), (d1, e1) = endpoints
            if count <= 1:
                lines.append(f"Doping: {_number(d0)} (1 point)")
                lines.append(f"E-field: {_number(e0)} (1 point)")
            else:
                lines.append(f"Doping: {_number(d0)} → {_number(d1)}")
                lines.append(f"E-field: {_number(e0)} → {_number(e1)}")
    else:
        axis = str(getattr(params, "derived_axis", "Doping"))
        fixed_axis = "E-field" if axis == "Doping" else "Doping"
        start = params.derived_start
        stop = params.derived_stop
        fixed = params.derived_fixed
        lines.append(f"DERIVED · sweep {axis}")
        if count <= 1:
            lines.append(f"{axis}: {_number(start)} (1 point)")
        else:
            lines.append(f"{axis}: {_number(start)} → {_number(stop)} ({_number(_step(start, stop, count))}/pt)")
        lines.append(f"{fixed_axis}: {_number(fixed)} (fixed)")
        if params.derived_vds_mode == "Swept":
            if count <= 1:
                lines.append(f"Vds: {_number(params.derived_vds_start)} V (1 point)")
            else:
                lines.append(
                    f"Vds: {_number(params.derived_vds_start)} → {_number(params.derived_vds_stop)} V "
                    f"({_number(_step(params.derived_vds_start, params.derived_vds_stop, count))} V/pt)"
                )
        else:
            lines.append(f"Vds: {_number(params.derived_vds_fixed)} V (fixed)")
        try:
            ratio = float(params.derived_ratio)
            target = normalize_ratio_target(params.derived_ratio_target)
            if abs(ratio) < 1e-12:
                raise ValueError("ratio r is zero")
            if axis == "Doping":
                derived = ((start, fixed), (stop, fixed))
            else:
                derived = ((fixed, start), (fixed, stop))
            gates = [derived_to_gates(doping, efield, ratio, target) for doping, efield in derived]
            if count <= 1:
                lines.append(f"Vtg: {_number(gates[0][0])} V (1 point)")
                lines.append(f"Vbg: {_number(gates[0][1])} V (1 point)")
            else:
                lines.append(f"Vtg: {_number(gates[0][0])} → {_number(gates[1][0])} V")
                lines.append(f"Vbg: {_number(gates[0][1])} → {_number(gates[1][1])} V")
        except (TypeError, ValueError) as exc:
            lines.append(f"Not configured — conversion unavailable: {exc}.")

    if details:
        if bidirectional:
            lines.insert(1, f"Points: {count * 2} total ({count} each direction)")
        else:
            lines.insert(1, f"Points: {count}")
        if mode == "Derived":
            lines.insert(2, f"Ratio: r={_number(params.derived_ratio)} on {params.derived_ratio_target}")
        lines.append(f"Delay: {_number(params.delay)} s · Samples/point: {int(params.n_sample)}")
        lines.append(f"Plot: {params.plot_choice} · X: {params.plot_x_axis}")
    return "\n".join(lines)
