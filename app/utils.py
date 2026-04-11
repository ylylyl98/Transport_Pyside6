from __future__ import annotations

import math
import re
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

