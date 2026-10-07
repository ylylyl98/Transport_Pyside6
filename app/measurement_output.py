from __future__ import annotations

from app.models import DualGateParams, LineSweepParams
from app.signal_chain import signal_chain_filename_parts


def dual_gate_filename_parts(params: DualGateParams, signal_chain) -> list[str]:
    return [
        f"Vds{params.vds_start:g}to{params.vds_stop:g}V",
        f"Vtg{params.vtg_set:g}V", f"Vbg{params.vbg_set:g}V",
        "RT" if params.sweep_both_ways else "Fwd",
        *signal_chain_filename_parts(signal_chain),
    ]


def gate_scan_filename_parts(params: LineSweepParams, signal_chain) -> list[str]:
    parts = []
    if params.mode == "Raw":
        for axis in ("Vtg", "Vbg", "Vds"):
            prefix = "raw_" + axis.lower()
            start = getattr(params, prefix + "_start")
            if getattr(params, prefix + "_active"):
                stop = getattr(params, prefix + "_stop")
                parts.append(f"{axis}{start:g}to{stop:g}V")
            else:
                parts.append(f"{axis}{start:g}V")
    else:
        axis = "Doping" if params.derived_axis == "Doping" else "E"
        fixed = "E" if axis == "Doping" else "Doping"
        parts.extend([f"{axis}{params.derived_start:g}to{params.derived_stop:g}",
                      f"{fixed}{params.derived_fixed:g}"])
        if params.derived_vds_mode == "Swept":
            parts.append(f"Vds{params.derived_vds_start:g}to{params.derived_vds_stop:g}V")
        else:
            parts.append(f"Vds{params.derived_vds_fixed:g}V")
        parts.append(f"r{params.derived_ratio_target}{params.derived_ratio:g}")
    parts.append("RT" if params.sweep_both_ways else "Fwd")
    if signal_chain is not None:
        parts.extend(signal_chain_filename_parts(signal_chain))
    return parts
