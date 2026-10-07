"""Application temperature/range bands, with no assumed heater calibration."""
from __future__ import annotations

import math


def validate_heater_ranges(bands):
    if not isinstance(bands, (list, tuple)) or not 1 <= len(bands) <= 10:
        raise ValueError("Configure 1–10 automatic heater temperature bands")
    normalized = []
    previous = 0.0
    for band in bands:
        if not isinstance(band, dict):
            raise ValueError("Each automatic heater band must contain a temperature and range")
        try:
            upper = float(band["upper_temperature_k"])
            value = float(band["heater_range"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("Each band needs an upper temperature in K and heater range 0–3") from exc
        if isinstance(band["upper_temperature_k"], bool) or not math.isfinite(upper) or upper <= previous:
            raise ValueError("Band upper temperatures must be positive and strictly increasing")
        if (isinstance(band["heater_range"], bool) or not math.isfinite(value)
                or not value.is_integer() or not 0 <= value <= 3):
            raise ValueError("Band heater ranges must be integers 0–3")
        normalized.append({"upper_temperature_k": upper, "heater_range": int(value)})
        previous = upper
    return normalized


def select_heater_range(target_k, bands):
    bands = validate_heater_ranges(bands)
    try:
        target = float(target_k)
    except (TypeError, ValueError) as exc:
        raise ValueError("Automatic range needs a positive finite temperature") from exc
    if not math.isfinite(target) or target <= 0:
        raise ValueError("Automatic range needs a positive finite temperature")
    for band in bands:
        if target <= band["upper_temperature_k"]:
            return dict(band)
    raise ValueError(f"Automatic heater table does not cover {target:g} K")
