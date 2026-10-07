"""Display bounds from execution trajectories, without placeholder samples."""
from __future__ import annotations

import math

import numpy as np


FULL_SWEEP = "Full sweep"
ACQUIRED_DATA = "Acquired data"
COORDINATE_KEYS = {"Vtg": "vtg", "Vbg": "vbg", "Vds": "vds",
                   "Doping": "doping", "E-field": "efield"}


def finite_bounds(values):
    array = np.asarray(values, dtype=float)
    array = array[np.isfinite(array)]
    return (float(array.min()), float(array.max())) if array.size else None


def padded_limits(bounds):
    low, high = sorted(float(value) for value in bounds)
    if not all(math.isfinite(value) for value in (low, high)):
        raise ValueError("Plot bounds must be finite")
    margin = (high - low) * 0.03 if low != high else max(abs(low) * 0.05, 0.05)
    return low - margin, high + margin


def coordinate_ranges(points, *, total_count=None, first_index=0):
    """Summarize the already built trajectory, retaining only its bounds."""
    ranges = {}
    for axis, key in COORDINATE_KEYS.items():
        bounds = finite_bounds([point[key] for point in points])
        if bounds is not None:
            ranges[axis] = bounds
    count = len(points) if total_count is None else total_count
    if count:
        ranges["Step Index"] = (float(first_index), float(first_index + count - 1))
    return ranges


def stepped_sweep_bounds(start, stop, step):
    """Match _frange_inc endpoints without allocating its complete sequence."""
    start, stop, step = float(start), float(stop), float(step)
    if not all(math.isfinite(value) for value in (start, stop, step)):
        raise ValueError("Sweep bounds must be finite")
    if step == 0:
        return start, start
    count = max(0, math.floor((stop - start) / step + 0.5))
    last = round(start + count * step, 12)
    if (step > 0 and last < stop) or (step < 0 and last > stop):
        last = stop
    return min(start, last), max(start, last)
