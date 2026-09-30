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


def safe_ramp(
    set_fn,
    current_v: float,
    target_v: float,
    step_v: float,
    step_t: float,
    check_fn=None,
) -> None:
    """Ramp from the source setpoint, or current_v for plain callbacks.

    Query-capable voltage sources must never use a cached start, even when
    that cache already equals the target. Read failures propagate before any
    write so a front-panel change cannot cause an uncontrolled first step.
    """
    owner = getattr(set_fn, "__self__", None)
    if check_fn is not None:
        check_fn()
    read_setpoint = getattr(owner, "get_voltage_setpoint", None)
    if callable(read_setpoint):
        current_v = float(read_setpoint())
    if not math.isfinite(current_v):
        raise ValueError("Cannot ramp from a non-finite voltage setpoint.")
    if getattr(set_fn, "__name__", "") == "set_voltage" and hasattr(owner, "set_voltage_fast"):
        set_fn = owner.set_voltage_fast
    if abs(target_v - current_v) < 1e-9:
        return
    sign = 1.0 if target_v > current_v else -1.0
    v = current_v
    while abs(target_v - v) > 1e-9:
        if check_fn is not None:
            check_fn()
        delta = sign * min(step_v, abs(target_v - v))
        v = round(v + delta, 9)
        set_fn(v)
        time.sleep(step_t)

