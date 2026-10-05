"""Summarize saved numeric conditions without instrument reads or GUI state."""
import math


def quantity(value, unit="", *, precision=6, engineering=False):
    try:
        number = float(value)
        if not math.isfinite(number):
            return ""
    except (ValueError, TypeError):
        return ""
    if unit == "V" and 0 < abs(number) < 1:
        number, unit = number * 1000, "mV"
    if engineering and unit in {"Hz", "Ω"}:
        magnitude = float(f"{abs(number):.{precision}g}")
        for scale, prefix in ((1e9, "G"), (1e6, "M"), (1e3, "k")):
            if magnitude >= scale:
                number, unit = number / scale, prefix + unit
                break
    return f"{number:.{precision}g}".replace("-", "−") + (f" {unit}" if unit else "")


def summarize_conditions(headers, units, rows):
    result = {}
    for name in ("Vtg", "Vbg", "Vds", "Doping", "E-field", "B_measured_T"):
        column = "Efield" if name == "E-field" and name not in headers else name
        if column not in headers:
            continue
        index = headers.index(column)
        values = []
        for row in rows:
            try:
                value = float(row[index])
                if math.isfinite(value):
                    values.append(value)
            except (TypeError, ValueError):
                pass
        if values:
            low, high = min(values), max(values)
            unit = units.get(column, "T" if column == "B_measured_T" else "")
            label = "B" if name == "B_measured_T" else name
            result[label] = quantity(low, unit) if low == high else f"{quantity(low, unit)} → {quantity(high, unit)}"
    return result
