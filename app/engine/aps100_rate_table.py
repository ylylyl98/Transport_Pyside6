"""Validation and integration of APS100 normal-mode rate readbacks."""
import math


def validate_rate_table(rates, coil_constant, max_current_a, max_rate_a_per_s, extent_t):
    coil, current_cap, rate_cap, extent = map(float, (coil_constant, max_current_a, max_rate_a_per_s, extent_t))
    if not all(math.isfinite(x) and x > 0 for x in (coil, current_cap, rate_cap, extent)) or extent / coil > current_cap + 1e-9:
        raise ValueError("Invalid APS100 commissioned limits or field extent")
    if set(rates) != set(range(5)):
        raise ValueError("APS100 rate table must contain RANGE/RATE 0..4")
    previous = 0.0
    normalized = {}
    for i in range(5):
        boundary, rate = map(float, rates[i])
        if not math.isfinite(boundary) or boundary <= previous:
            raise ValueError(f"APS100 RANGE {i} boundaries must increase strictly")
        if not math.isfinite(rate) or rate <= 0:
            raise ValueError(f"APS100 RATE {i} must be positive and finite")
        if previous < extent / coil and rate / (coil * 60) > rate_cap + 1e-12:
            raise ValueError(f"APS100 RATE {i} exceeds commissioned current rate")
        normalized[i] = (boundary, rate)
        previous = boundary
    if previous + 1e-9 < extent / coil:
        raise ValueError("APS100 rate table does not cover the requested field")
    return normalized


def field_move_seconds(start_t, end_t, rates, coil_constant):
    """Integrate field distance over absolute-current bands, including zero crossings."""
    start, end, coil = float(start_t), float(end_t), float(coil_constant)
    if not all(math.isfinite(x) for x in (start, end, coil)) or coil <= 0:
        raise ValueError("Invalid field or coil constant")
    lo, hi = sorted((start, end))
    seconds, lower = 0.0, 0.0
    for i in range(5):
        boundary, rate = map(float, rates[i])
        upper = boundary * coil
        if not math.isfinite(upper) or upper <= lower or not math.isfinite(rate) or rate <= 0:
            raise ValueError("Invalid APS100 rate table")
        for a, b in ((lower, upper), (-upper, -lower)):
            seconds += max(0.0, min(hi, b) - max(lo, a)) / rate * 60.0
        lower = upper
    if max(abs(lo), abs(hi)) > lower + 1e-9:
        raise ValueError("APS100 rate table does not cover field move")
    return seconds
