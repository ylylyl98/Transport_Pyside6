"""Display-only normalization of independent comparison traces."""
from dataclasses import replace

import numpy as np

NORMALIZATION_MODES = {"raw": "Raw", "max_abs": "Max |Y|", "min_max": "0–1"}


def normalize_traces(traces, mode="raw"):
    if mode not in NORMALIZATION_MODES:
        raise ValueError(f"Unknown normalization mode: {mode}")
    if mode == "raw":
        return traces
    result = []
    for trace in traces:
        source = np.asarray(trace.y, dtype=float)
        valid = np.isfinite(trace.x) & np.isfinite(source)
        values = np.full(source.shape, np.nan, dtype=float)
        if np.any(valid):
            finite = source[valid]
            if mode == "max_abs":
                scale = np.max(np.abs(finite))
                values[valid] = finite / scale if scale else 0.
            else:
                low, high = np.min(finite), np.max(finite)
                if high == low:
                    values[valid] = 0.
                else:
                    span = float(high) - float(low)
                    if np.isfinite(span):
                        # Subtract first to retain precision for narrow ranges.
                        values[valid] = (finite - low) / span
                    else:
                        # Only scale first when the finite endpoints span more
                        # than float64 can hold, such as [-1e308, 1e308].
                        scale = max(abs(low), abs(high))
                        scaled = finite / scale
                        values[valid] = (scaled - low / scale) / (high / scale - low / scale)
        result.append(replace(trace, y=values, y_unit=""))
    return result


def read_normalized_comparison(reader, args, mode, map_mode=False):
    """Run in the existing read worker; never normalize large arrays in Qt slots."""
    result = reader(*args)
    if map_mode:
        from app.curve_map import MapComparison
        if isinstance(result, MapComparison):
            return replace(result, traces=normalize_traces(result.traces, mode))
        primary, traces, warnings = result
        return primary, normalize_traces(traces, mode), warnings
    traces, warnings = result
    return normalize_traces(traces, mode), warnings


def comparison_y_label(signal, unit, mode="raw"):
    if mode != "raw":
        return f"Normalized {signal} ({NORMALIZATION_MODES[mode]})"
    return signal + (f" ({unit})" if unit else "")
