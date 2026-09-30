from __future__ import annotations

from app.models import CoParams
from app.signal_chain import SignalChainSnapshot, signal_chain_filename_parts


def map_filename_parts(params: CoParams, signal_chain: SignalChainSnapshot | dict) -> list[str]:
    """Compact map conditions, with swept coordinates in fast-to-slow order."""
    derived = params.coordinate_mode == "Derived"
    available = ("Doping", "E-field", "Vds") if derived else ("Vtg", "Vbg", "Vds")
    swept = [axis for axis in (params.axis_fast, params.axis_slow) if axis != "None"]
    fields = {"Doping": "doping", "E-field": "efield", "Vtg": "vtg", "Vbg": "vbg", "Vds": "vds"}
    parts = []
    for axis in swept + [axis for axis in available if axis not in swept]:
        prefix = fields[axis]
        label = "E" if axis == "E-field" else axis
        unit = "" if axis in ("Doping", "E-field") else "V"
        start = getattr(params, f"{prefix}_start")
        if axis in swept:
            stop = getattr(params, f"{prefix}_stop")
            parts.append(f"{label}{start:g}to{stop:g}{unit}")
        else:
            parts.append(f"{label}{start:g}{unit}")
    if derived or params.mode == "Linked":
        parts.append(f"r{params.ratio_target}{params.ratio:g}")
    parts.extend(signal_chain_filename_parts(signal_chain))
    return parts
