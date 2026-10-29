"""Small, widget-independent form contracts shared by the Quick presenters."""
from __future__ import annotations

import math


def field(key, label, value, *, kind=None, options=None, group="", **extra):
    kind = kind or ("bool" if isinstance(value, bool) else "integer" if isinstance(value, int)
                    else "number" if isinstance(value, float) else "text")
    result = dict(key=key, label=label, value=value, kind=kind, group=group)
    if options is not None:
        result.update(kind="choice", options=options)
    result.update(extra)
    return result


def checked_value(spec, value):
    """Reject invalid drafts; never silently clamp a physical parameter."""
    if hasattr(value, "toVariant"):
        value = value.toVariant()
    kind = spec["kind"]
    if kind == "bool":
        if type(value) is not bool:
            raise ValueError("Choose on or off")
    elif kind in ("integer", "number"):
        if isinstance(value, bool):
            raise ValueError("Enter a number")
        number = float(value)
        if not math.isfinite(number):
            raise ValueError("Enter a finite number")
        if kind == "integer" and number != int(number):
            raise ValueError("Enter a whole number")
        if number < spec.get("min", -math.inf) or number > spec.get("max", math.inf):
            raise ValueError(f"Allowed range: {spec.get('min', '-∞')} to {spec.get('max', '∞')}")
        value = int(number) if kind == "integer" else number
    elif kind == "choice":
        options = [item.get("value", item.get("label")) if isinstance(item, dict) else item
                   for item in spec.get("options", [])]
        if value not in options:
            raise ValueError("Choose one of the listed values")
    else:
        value = str(value)
        if "\x00" in value:
            raise ValueError("The value contains an invalid character")
    return value
