"""Unambiguous display labels for saved floating-point coordinates."""
import math


def coordinate_options(values):
    """Format a coordinate group without changing values or merging distinct floats.

    Order and duplicates are preserved. Labels start at six significant digits;
    all labels gain precision together until distinct values are distinguishable.
    """
    coordinates = [float(value) for value in values]
    if any(not math.isfinite(value) for value in coordinates):
        raise ValueError("Coordinates must be finite")
    distinct = set(coordinates)

    def label(value, precision):
        text = format(value, f".{precision}g")
        if "e" in text:
            mantissa, exponent = text.split("e")
            return f"{mantissa}e{int(exponent):+d}"
        return text

    for precision in range(6, 18):
        labels = [label(value, precision) for value in coordinates]
        if len({label(value, precision) for value in distinct}) == len(distinct):
            return [{"value": value, "label": label, "exact": repr(value)}
                    for value, label in zip(coordinates, labels)]
    raise ValueError("Could not distinguish coordinates")


def coordinate_label(values, value):
    """Return the group-consistent label for an exactly matching coordinate."""
    target = float(value)
    if not math.isfinite(target):
        raise ValueError("Coordinate must be finite")
    for option in coordinate_options(values):
        if option["value"] == target:
            return option["label"]
    raise ValueError("Coordinate is not present in the group")
