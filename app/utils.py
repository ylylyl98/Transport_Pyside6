from __future__ import annotations

import math
import re
import time
from typing import List


def _sanitize_base(s: str) -> str:
    return re.sub(r"[^-_.A-Za-z0-9]+", "_", s).strip("_")


def clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def _safe(obj, method, *args, **kw):
    try:
        getattr(obj, method)(*args, **kw)
    except Exception:
        pass


def _frange_inc(start: float, stop: float, step: float) -> List[float]:
    if step == 0:
        return [start]
    n = int(math.floor((stop - start) / step + 0.5))
    if n < 0:
        n = 0
    vals = []
    cur = start
    for _ in range(n + 1):
        vals.append(round(cur, 12))
        cur += step
    if (step > 0 and vals[-1] < stop) or (step < 0 and vals[-1] > stop):
        vals.append(stop)
    return vals


def ramp_quantized(set_fn, current_v, target_v, step_v, step_t, quantum, check_fn=None, *, allow_unaligned_start=False):
    """Write grid-aligned intermediate commands with bounded increments."""
    current_v, target_v, step_v, step_t, quantum = map(float, (current_v, target_v, step_v, step_t, quantum))
    if not all(math.isfinite(value) for value in (current_v, target_v, step_v, step_t, quantum)) or quantum <= 0 or step_v <= 0 or step_t < 0:
        raise ValueError("Invalid quantized ramp parameters.")
    max_ticks = math.floor((step_v + quantum * 1e-9) / quantum)
    if max_ticks < 1:
        raise ValueError("Ramp step is smaller than the source resolution.")
    target_tick = round(target_v / quantum)
    if abs(target_v / quantum - target_tick) > 1e-7:
        raise ValueError("Ramp target is not representable at the source resolution.")
    current_tick = round(current_v / quantum)
    if abs(current_v / quantum - current_tick) > 1e-7:
        if not allow_unaligned_start:
            raise ValueError("Current source setpoint is not representable at the source resolution.")
        # On takeover, the previous level can be unaligned. First approach
        # the target using the next grid point, never increasing the distance.
        current_tick = math.floor(current_v / quantum) if current_v > target_v else math.ceil(current_v / quantum)
        first = target_v if current_tick == target_tick else round(current_tick * quantum, 12)
        if check_fn is not None:
            check_fn()
        set_fn(first)
        if step_t:
            time.sleep(step_t)
    while current_tick != target_tick:
        if check_fn is not None:
            check_fn()
        delta = min(max_ticks, abs(target_tick - current_tick))
        current_tick += delta if target_tick > current_tick else -delta
        value = target_v if current_tick == target_tick else round(current_tick * quantum, 12)
        set_fn(value)
        if step_t:
            time.sleep(step_t)


def safe_ramp(
    set_fn,
    current_v: float,
    target_v: float,
    step_v: float,
    step_t: float,
    check_fn=None,
) -> None:
    """Ramp with bounded progress from the actual source setpoint.

    Keithley intermediates lie on its verified source grid; final targets
    never get rounded. Plain callback users retain the original numeric ramp.
    """
    target_v, step_v, step_t = float(target_v), float(step_v), float(step_t)
    if not math.isfinite(target_v):
        raise ValueError("Ramp target must be finite.")
    if not math.isfinite(step_v) or step_v <= 0:
        raise ValueError("Ramp step must be finite and greater than zero.")
    if not math.isfinite(step_t) or step_t < 0:
        raise ValueError("Ramp delay must be finite and non-negative.")
    owner = getattr(set_fn, "__self__", None)
    if check_fn is not None:
        check_fn()
    read_setpoint = getattr(owner, "get_voltage_setpoint", None)
    if callable(read_setpoint):
        current_v = float(read_setpoint())
    current_v = float(current_v)
    if not math.isfinite(current_v):
        raise ValueError("Cannot ramp from a non-finite voltage setpoint.")
    quantum = None
    if callable(getattr(owner, "refresh_source_voltage_range", None)):
        from app.voltage_resolution import validate_source_targets
        quantum = validate_source_targets(owner, (target_v,), "Keithley", step_v, refresh=True)
    if getattr(set_fn, "__name__", "") == "set_voltage" and hasattr(owner, "set_voltage_fast"):
        set_fn = owner.set_voltage_fast
    if quantum is not None:
        ramp_quantized(set_fn, current_v, target_v, step_v, step_t, quantum, check_fn, allow_unaligned_start=True)
        return
    if abs(target_v - current_v) < 1e-9:
        return
    sign = 1.0 if target_v > current_v else -1.0
    v = current_v
    while abs(target_v - v) > 1e-9:
        if check_fn is not None:
            check_fn()
        delta = sign * min(step_v, abs(target_v - v))
        next_v = round(v + delta, 9)
        if next_v == v:
            raise ValueError("Ramp step is too small to make progress.")
        v = next_v
        set_fn(v)
        if step_t:
            time.sleep(step_t)
