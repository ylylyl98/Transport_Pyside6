"""Read saved scalar signals, including display-only Drag/Drive ratios."""
import math

import numpy as np
from app.curve_cache import CsvColumns

RATIO_SIGNALS = {f"Drag ratio {axis}": (f"I_drag_{axis}", f"I_drive_{axis}") for axis in ("X", "Y", "R")}
_CURRENT_SCALES = {"": 1., "A": 1., "mA": 1e-3, "uA": 1e-6, "µA": 1e-6, "μA": 1e-6, "nA": 1e-9, "pA": 1e-12}


def numeric(value):
    try:
        number = float(value)
        return number if math.isfinite(number) else math.nan
    except (TypeError, ValueError):
        return math.nan


def numeric_column(rows, index):
    if isinstance(rows, CsvColumns):
        values = rows.columns[index]
        if isinstance(values, np.ndarray):
            return values
    else:
        values = (row[index] for row in rows)
    return np.asarray([numeric(value) for value in values], dtype=float)


def signal_values(headers, units, rows, signal):
    def column(name):
        if name not in headers:
            raise ValueError(f"Missing column: {name} (required for {signal})")
        index = headers.index(name)
        return numeric_column(rows, index)

    if signal not in RATIO_SIGNALS:
        return column(signal), units.get(signal, ""), []

    def current(name):
        if name.endswith("_R") and name not in headers:
            return np.hypot(current(name[:-1] + "X"), current(name[:-1] + "Y"))
        unit = units.get(name, "")
        if unit not in _CURRENT_SCALES:
            raise ValueError(f"{signal} requires current units; {name} has {unit}")
        return column(name) * _CURRENT_SCALES[unit]

    numerator, denominator = (current(name) for name in RATIO_SIGNALS[signal])
    values = np.full(numerator.shape, np.nan)
    valid = np.isfinite(numerator) & np.isfinite(denominator) & (denominator != 0)
    with np.errstate(over="ignore", invalid="ignore", divide="ignore"):
        np.divide(numerator, denominator, out=values, where=valid)
    values[~np.isfinite(values)] = np.nan
    skipped = int(np.count_nonzero(~np.isfinite(values)))
    warnings = [f"{signal}: {skipped} undefined point(s) left blank (zero Drive, missing or nonfinite values)"] if skipped else []
    return values, "", warnings
