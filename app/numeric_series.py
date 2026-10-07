"""Bounded numeric input syntax shared by field and condition editors."""
from __future__ import annotations

from decimal import Decimal, ROUND_CEILING, localcontext
import math
import re

import numpy as np


_POINTS = re.compile(r"(?:(?:np\.)?linspace)?\(\s*([^,()]+),\s*([^,()]+),\s*([^,()]+)\s*\)")


def _finite_number(text: str) -> float:
    try:
        value = float(text.strip())
    except (TypeError, ValueError) as exc:
        raise ValueError("values must be finite numbers") from exc
    if not math.isfinite(value):
        raise ValueError("values must be finite numbers")
    return value


def _tokens(source: str) -> list[str]:
    """Split only outside parentheses, without interpreting Python code."""
    result = []
    start = 0
    depth = 0
    for index, char in enumerate(source):
        if char == "(":
            depth += 1
            if depth > 1:
                raise ValueError("nested parentheses are not supported")
        elif char == ")":
            depth -= 1
            if depth < 0:
                raise ValueError("parentheses must be paired")
        elif char in ",;\n" and depth == 0:
            token = source[start:index].strip()
            if token:
                result.append(token)
            start = index + 1
    if depth:
        raise ValueError("parentheses must be paired")
    token = source[start:].strip()
    if token:
        result.append(token)
    return result


def _step_values(start: float, stop: float, step: float, maximum: int) -> list[float]:
    if step == 0:
        raise ValueError("range step cannot be zero")
    if (step > 0 and stop < start) or (step < 0 and stop > start):
        raise ValueError("range step direction does not reach its stop")
    # Decimal arithmetic avoids extra endpoints for decimal steps such as
    # 0:0.3:0.1. Check the complete count before allocating; append the exact
    # requested stop. A non-divisible span has a shorter final interval.
    with localcontext() as context:
        context.prec = 50
        first, last, increment = (Decimal(str(value)) for value in (start, stop, step))
        distance = (last - first) / increment
        interior = int(distance.to_integral_value(rounding=ROUND_CEILING))
        if interior + 1 > maximum:
            raise ValueError(f"range cannot exceed the remaining {maximum:,} values")
        values = [float(first + index * increment) for index in range(interior)]
    values.append(stop)
    return values


def _point_values(start: float, stop: float, count: float, maximum: int) -> list[float]:
    if not count.is_integer() or count < 1:
        raise ValueError("point count must be a positive integer")
    if count > maximum:
        raise ValueError(f"point count cannot exceed the remaining {maximum:,} values")
    if count == 1 and start != stop:
        raise ValueError("at least 2 points are needed to include both endpoints")
    try:
        with np.errstate(over="raise", invalid="raise", divide="raise"):
            values = [float(value) for value in np.linspace(start, stop, int(count), endpoint=True)]
    except FloatingPointError as exc:
        raise ValueError("generated values must be finite") from exc
    if not all(math.isfinite(value) for value in values):
        raise ValueError("generated values must be finite")
    return values


def parse_numeric_series(text: str, label: str, *, maximum: int) -> tuple[float, ...]:
    """Accept scalars/lists, inclusive step ranges and (start, stop, count).

    linspace(...) and np.linspace(...) are aliases of the parenthesized
    three-number form. Only this numeric grammar is accepted, never eval.
    """
    source = str(text or "").strip()
    if source.startswith("[") or source.endswith("]"):
        if not (source.startswith("[") and source.endswith("]")):
            raise ValueError(f"{label}: array brackets must be paired")
        source = source[1:-1]
    try:
        tokens = _tokens(source)
    except ValueError as exc:
        raise ValueError(f"{label}: {exc}") from exc
    values = []
    for index, token in enumerate(tokens, start=1):
        remaining = maximum - len(values)
        try:
            points = _POINTS.fullmatch(token)
            if points is not None:
                first, last, count = (_finite_number(part) for part in points.groups())
                added = _point_values(first, last, count, remaining)
            elif "(" in token or ")" in token:
                raise ValueError("use (start, stop, count) or linspace(start, stop, count)")
            elif ":" in token:
                parts = token.split(":")
                if len(parts) != 3 or any(not part.strip() for part in parts):
                    raise ValueError("use start:stop:step for ranges")
                first, last, step = (_finite_number(part) for part in parts)
                added = _step_values(first, last, step, remaining)
            else:
                added = [_finite_number(token)]
            if len(added) > remaining:
                raise ValueError(f"series cannot exceed {maximum:,} values")
            values.extend(added)
        except ValueError as exc:
            raise ValueError(f"{label}, item {index}: {exc}") from exc
    if not values:
        raise ValueError(f"Enter at least one {label} value")
    return tuple(values)
