"""Scalar map snapshots and exact measured-coordinate line cuts."""
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from app.curve_history import CurveTrace, cached_csv_snapshot, numeric, resolve_column, normalize_direction
from app.analysis_signals import numeric_column, signal_values


@dataclass
class MapData:
    path: Path
    x_name: str
    y_name: str
    signal: str
    x: np.ndarray
    y: np.ndarray
    z: np.ndarray
    x_unit: str
    y_unit: str
    signal_unit: str
    passes: list[float]
    pass_index: float | None
    warnings: list[str]
    saved_conditions: dict = field(default_factory=dict)


@dataclass
class MapComparison:
    primary: MapData | None
    traces: list
    warnings: list
    fixed_axis: str = ""
    fixed_value: float | None = None
    requested_value: float | None = None

    def __iter__(self):
        # Keep the existing three-value unpacking API for export/read callers.
        return iter((self.primary, self.traces, self.warnings))


def _map_grid(headers, rows, x_name, y_name, direction, pass_index):
    xi, yi = (headers.index(name) for name in (x_name, y_name))
    pi = headers.index("PassIndex") if "PassIndex" in headers else None
    di = next((headers.index(name) for name in ("FastDirection", "Direction") if name in headers), None)
    passes = sorted({numeric(row[pi]) for row in rows if pi is not None and np.isfinite(numeric(row[pi]))})
    chosen_pass = None if pass_index == "All rows" else (passes[0] if passes else None) if pass_index == "First pass" else numeric(pass_index)
    x, y = numeric_column(rows, xi), numeric_column(rows, yi)
    valid = np.isfinite(x) & np.isfinite(y)
    if pi is not None and chosen_pass is not None:
        valid &= np.asarray([numeric(row[pi]) == chosen_pass for row in rows])
    if direction != "All":
        valid &= np.asarray([di is not None and normalize_direction(row[di]) == direction for row in rows])
    indices = np.flatnonzero(valid)
    if not len(indices):
        raise ValueError("No finite map samples for this pass/direction")
    xs, x_indices = np.unique(x[indices], return_inverse=True)
    ys, y_indices = np.unique(y[indices], return_inverse=True)
    if len(xs) * len(ys) > 2_000_000:
        raise ValueError("Selected coordinates exceed 2 million map cells; choose the scan axes")
    if len(np.unique(y_indices * len(xs) + x_indices)) != len(indices):
        raise ValueError("Duplicate coordinates in selected pass; choose a direction or different axes")
    return xs, ys, indices, x_indices, y_indices, passes, chosen_pass


def load_map(path, x_name, y_name, signal, direction="All", pass_index="All rows", cache=None, fingerprint=None):
    if cache is not None and fingerprint is None:
        stat = Path(path).stat()
        fingerprint = (stat.st_mtime_ns, stat.st_size)
    headers, units, rows, warnings, saved_conditions = cached_csv_snapshot(path, cache, fingerprint)
    x_name, y_name = resolve_column(headers, x_name), resolve_column(headers, y_name)
    signal = resolve_column(headers, signal)
    if len({x_name, y_name, signal}) != 3 or any(name not in headers for name in (x_name, y_name)):
        raise ValueError("Choose different X, Y and signal columns present in this CSV")
    signal_data, signal_unit, signal_warnings = signal_values(headers, units, rows, signal)
    grid = lambda: _map_grid(headers, rows, x_name, y_name, direction, pass_index)
    xs, ys, indices, xi, yi, passes, chosen_pass = (
        cache.get(("grid", str(path), fingerprint, x_name, y_name, direction, pass_index), grid)
        if cache is not None else grid())
    if not np.any(np.isfinite(signal_data[indices])):
        raise ValueError("No finite map samples for this pass/direction")
    z = np.full((len(ys), len(xs)), np.nan)
    z[yi, xi] = signal_data[indices]
    return MapData(Path(path), x_name, y_name, signal, xs, ys, z, units.get(x_name, ""), units.get(y_name, ""), signal_unit, passes, chosen_pass, list(warnings) + signal_warnings, saved_conditions)


def infer_map_axes(record, snapshot=None):
    """Prefer acquisition coordinates; infer a regular fast/slow grid for legacy CSVs."""
    canonical = lambda name: "E-field" if name == "Efield" else name
    columns = {canonical(name) for name in record.columns}
    params = record.metadata.get("params", {})
    params = params if isinstance(params, dict) else {}
    fast, slow = canonical(params.get("axis_fast")), canonical(params.get("axis_slow"))
    if fast in columns:
        if slow in ("None", "None (1D)") or (slow is None and record.metadata.get("measurement") == "sweep_1d"):
            return fast, "None (1D)"
        if slow in columns and slow != fast:
            return fast, slow
    if snapshot is None:
        return None
    headers, _units, rows, _warnings, _conditions = snapshot
    candidates = {}
    for name in ("Vtg", "Vbg", "Vds", "Doping", "E-field"):
        column = resolve_column(headers, name)
        if column in headers:
            i = headers.index(column)
            values = numeric_column(rows, i)
            if np.all(np.isfinite(values)) and len(np.unique(values)) > 1:
                candidates[name] = values
    choices = []
    for x_name, x in candidates.items():
        x_changes = np.count_nonzero(np.diff(x))
        for y_name, y in candidates.items():
            if x_name == y_name or x_changes <= np.count_nonzero(np.diff(y)):
                continue
            cells = len(np.unique(x)) * len(np.unique(y))
            if cells > 2_000_000:
                continue
            points = len(np.unique(np.column_stack((x, y)), axis=0))
            if points >= cells * .5 and points > max(len(np.unique(x)), len(np.unique(y))):
                choices.append((cells, x_name, y_name))
    if choices:
        _cells, x_name, y_name = min(choices)
        return x_name, y_name
    return None


def map_comparison(records, x_name, y_name, signal, direction="All", pass_index="All rows", primary_path="", fixed_axis="", fixed_value=None, cache=None, auto_axes=False, nearest=False):
    maps, warnings = {}, []
    if auto_axes and records:
        record = next((r for r in records if str(r.path) == primary_path), records[0])
        axes = infer_map_axes(record)
        if axes is None:
            def infer():
                return infer_map_axes(record, cached_csv_snapshot(record.path, cache, record.fingerprint))
            try:
                axes = cache.get(("axes", str(record.path), record.fingerprint), infer) if cache is not None else infer()
            except (OSError, ValueError):
                axes = None
        if axes is not None and axes[1] != "None (1D)":
            x_name, y_name = axes
            if ("E-field" if fixed_axis == "Efield" else fixed_axis) not in axes:
                fixed_axis, fixed_value = y_name, None
    for record in records:
        try:
            result = (cache.get(("map", str(record.path), record.fingerprint, x_name, y_name, signal, direction, pass_index), lambda: load_map(record.path, x_name, y_name, signal, direction, pass_index, cache, record.fingerprint))
                      if cache is not None else load_map(record.path, x_name, y_name, signal, direction, pass_index))
            maps[str(record.path)] = result
            warnings.extend(f"{record.path.name}: {w}" for w in result.warnings)
        except (OSError, ValueError) as exc:
            warnings.append(f"{record.path.name}: {exc}")
    primary = maps.get(primary_path)
    if primary is None:
        primary = next(iter(maps.values()), None)
    if primary is None:
        return MapComparison(None, [], warnings)
    fixed_axis = fixed_axis or primary.y_name
    fixed_axis = resolve_column((primary.x_name, primary.y_name), fixed_axis)
    if fixed_axis not in (primary.x_name, primary.y_name):
        fixed_axis, fixed_value = primary.y_name, None
    coordinates = primary.y if fixed_axis == primary.y_name else primary.x
    value = float(coordinates[0]) if fixed_value is None else float(fixed_value)
    requested_value = value
    if nearest:
        if not np.isfinite(value):
            raise ValueError("Enter a finite cut coordinate")
        index = int(np.searchsorted(coordinates, value))
        candidates = coordinates[max(0, index - 1):min(len(coordinates), index + 1)]
        value = float(min(candidates, key=lambda coordinate: abs(coordinate - value)))
    traces = []
    expected_units = (primary.x_unit if fixed_axis == primary.y_name else primary.y_unit, primary.signal_unit)
    primary_fixed_unit = primary.y_unit if fixed_axis == primary.y_name else primary.x_unit
    for record in records:
        result = maps.get(str(record.path))
        if result is None:
            continue
        fixed_y = fixed_axis == result.y_name
        if not fixed_y and fixed_axis != result.x_name:
            warnings.append(f"{record.path.name}: fixed axis unavailable")
            continue
        coords = result.y if fixed_y else result.x
        fixed_unit = result.y_unit if fixed_y else result.x_unit
        if fixed_unit != primary_fixed_unit:
            warnings.append(f"{record.path.name}: incompatible fixed-axis unit {fixed_unit}; expected {primary_fixed_unit}")
            continue
        indices = np.flatnonzero(coords == value)
        if not len(indices):
            warnings.append(f"{record.path.name}: no measured {fixed_axis}={value:g}; cut skipped")
            continue
        x = result.x if fixed_y else result.y
        y = result.z[indices[0], :] if fixed_y else result.z[:, indices[0]]
        units = (result.x_unit if fixed_y else result.y_unit, result.signal_unit)
        if units != expected_units:
            warnings.append(f"{record.path.name}: incompatible cut units {units}")
            continue
        expected_units = units
        pass_label = "all rows" if result.pass_index is None else f"row/pass {result.pass_index:g}"
        label = f"{record.path.stem[:28]} · {fixed_axis}={value:g} · {pass_label}"
        traces.append(CurveTrace(record.path, label, direction, x, y, *units, saved_conditions=result.saved_conditions))
    return MapComparison(primary, traces, warnings, fixed_axis, value, requested_value)
