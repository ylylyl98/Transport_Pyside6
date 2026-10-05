"""Read-only measurement history and curve snapshots; no Qt or hardware access."""
from __future__ import annotations

import csv
import datetime as dt
import json
import re
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
from app.saved_conditions import summarize_conditions
from app.analysis_signals import RATIO_SIGNALS, numeric, signal_values
from app.curve_cache import CsvColumns

MEASUREMENT_TYPES = {"vds_sweep": "Vds Sweep", "gate_scan": "Gate Scan", "map_2d": "2D Map",
                     "bfield_gate_scan": "B-field Gate Scan", "bfield_transport": "B-field Sweep", "photocurrent": "Photocurrent"}
X_COLUMNS = ("Vds", "Vtg", "Vbg", "Doping", "E-field", "B_measured_T", "Wavelength", "Step Index")
from app.drag_drive import DUAL_COLUMNS, DUAL_SIGNALS

SIGNAL_COLUMNS = ("Ids_DC", "Ids_X", "Ids_Y", "Ids_Keithley", "raw_DC", "raw_X", "raw_Y", *DUAL_COLUMNS, *RATIO_SIGNALS)
_UNITS = {"", "v", "a", "mv", "ma", "ua", "µa", "μa", "na", "pa", "t", "k", "s", "hz", "ohm", "ω", "#", "deg", "rad", "a.u.", "nm"}


@dataclass(frozen=True)
class HistoryRecord:
    path: Path
    measurement: str
    created_at: str
    status: str
    conditions: str
    columns: tuple[str, ...]
    units: dict[str, str]
    metadata: dict = field(default_factory=dict)
    fingerprint: tuple[int, int] = (0, 0)

    @property
    def date(self):
        return self.created_at[:10]


@dataclass
class CurveData:
    x: np.ndarray
    y: np.ndarray
    directions: tuple[str, ...]
    x_unit: str
    y_unit: str
    warnings: list[str]
    groups: tuple[str, ...] = ()
    saved_conditions: dict = field(default_factory=dict)


@dataclass
class CurveTrace:
    path: Path
    label: str
    direction: str
    x: np.ndarray
    y: np.ndarray
    x_unit: str
    y_unit: str
    condition: str = ""
    saved_conditions: dict = field(default_factory=dict)


def device_history_folder(save) -> Path:
    """Resolve the device root without SaveRoot.path()'s directory creation."""
    def safe(value, fallback):
        return re.sub(r"[^-_.A-Za-z0-9]+", "_", str(value)).strip("_") or fallback
    return Path(save.base).expanduser().absolute() / safe(save.user, "User") / safe(save.device_id, "device")


def _header(reader):
    headers = tuple(value.strip() for value in next(reader, []))
    if not headers or not all(headers) or len(set(headers)) != len(headers):
        raise ValueError("CSV headers must be nonempty and unique")
    second = next(reader, [])
    has_units = len(second) == len(headers) and all(v.strip().lower() in _UNITS for v in second)
    units = dict(zip(headers, (v.strip() for v in second))) if has_units else {}
    # The native B-field writer has no units row. Its named schema carries
    # field in tesla, converted/Keithley currents in amperes and biases in volts.
    if not has_units and "B_measured_T" in headers:
        units = {name: "T" if name in {"B_measured_T", "B_target_T"} else "A" if name.startswith("Ids_") or name in DUAL_SIGNALS or name == "Keithley_current" else "V" if name.startswith('raw_') or name in {"Vtg", "Vbg", "Vds", "Doping", "E-field", "Efield"} else "" for name in headers}
    return headers, units, [] if has_units else second


def _measurement(path, root, metadata, headers):
    kind = str(metadata.get("measurement", "")).lower()
    if kind == "sweep_1d":
        kind = "map_2d"
    if kind == "gate_scan" and metadata.get("gate_scan_bfield_batch"):
        kind = "bfield_gate_scan"
    if kind:
        return kind if kind in MEASUREMENT_TYPES else ""
    relative = path.relative_to(root)
    if any(p.lower() in ("exports", "results", "spectral_slices", "line_cuts") for p in relative.parts[:-1]):
        return ""
    for name in ("bfield_gate_scan", "bfield_transport", "gate_scan", "vds_sweep", "map_2d", "photocurrent"):
        if name in str(relative).lower():
            return name
    if "FastDirection" in headers or "PassIndex" in headers:
        return "map_2d"
    if "Wavelength" in headers:
        return "photocurrent"
    # Old flat files have the acquisition's direction column, unlike most cuts.
    if "Direction" in headers and any(c.startswith("Ids_") for c in headers):
        return "gate_scan" if "Doping" in headers else "vds_sweep"
    return ""


def _conditions(metadata):
    params = metadata.get("params", {})
    if not isinstance(params, dict):
        return "Conditions unknown"
    pieces = []
    if "axis_fast" in params:
        pieces.append(f"X={params['axis_fast']}" + (f", Y={params['axis_slow']}" if params.get("axis_slow") not in (None, "None") else ""))
    for keys, label in ((("vtg_set",), "Vtg"), (("vbg_set",), "Vbg"),
                        (("derived_vds_fixed",), "Vds"), (("derived_fixed",), "Fixed D/E")):
        for key in keys:
            if key in params and (not key.startswith("derived") or params.get("mode") == "Derived"):
                try:
                    pieces.append(f"{label}={float(params[key]):g} V")
                except (ValueError, TypeError):
                    pass
    for prefix, label in (("vds", "Vds"), ("vtg", "Vtg"), ("vbg", "Vbg"), ("doping", "Doping"), ("efield", "E-field"), ("raw_vtg", "Vtg"), ("raw_vbg", "Vbg"), ("raw_vds", "Vds"), ("derived", str(params.get("derived_axis", "D/E")))):
        if prefix.startswith("raw") and params.get("mode") != "Raw":
            continue
        if prefix == "derived" and params.get("mode") != "Derived":
            continue
        if prefix + "_start" in params:
            try:
                start = float(params[prefix + "_start"])
                stop = float(params.get(prefix + "_stop", start))
                if prefix.startswith("raw") and not params.get(prefix + "_active", False):
                    stop = start
                pieces.append(f"{label}={start:g} V" if start == stop else f"{label}={start:g}→{stop:g} V")
            except (ValueError, TypeError):
                pass
    if params.get("mode") == "Derived" and "derived_ratio" in params:
        pieces.append(f"r={params['derived_ratio']} on {params.get('derived_ratio_target', 'Vbg')}")
    chain = metadata.get("signal_chain", {})
    if isinstance(chain, dict) and chain.get("ac_contact"):
        pieces.append(f"Contact={chain['ac_contact']}")
    return " · ".join(pieces) or "Conditions unknown"


def discover_history(root, cache=None) -> tuple[list[HistoryRecord], list[str]]:
    root = Path(root).expanduser().absolute()
    records, warnings = [], []
    if not root.is_dir():
        return [], [f"Folder does not exist: {root}"]
    try:
        paths = root.rglob("*.csv")
        seen_paths = set()
        for path in paths:
            try:
                seen_paths.add(path)
                metadata = {}
                sidecar = path.with_name(path.stem + "_metadata.json")
                stat = path.stat()
                meta_stat = sidecar.stat() if sidecar.exists() else None
                signature = (stat.st_mtime_ns, stat.st_size, meta_stat.st_mtime_ns if meta_stat else 0, meta_stat.st_size if meta_stat else 0)
                if cache is not None and path in cache and cache[path][0] == signature:
                    _signature, record, cached_warnings = cache[path]
                    if record is not None:
                        records.append(record)
                    warnings.extend(cached_warnings)
                    continue
                warning_start = len(warnings)
                if sidecar.exists():
                    try:
                        metadata = json.loads(sidecar.read_text(encoding="utf-8-sig"))
                        if not isinstance(metadata, dict):
                            raise ValueError("expected a JSON object")
                    except (OSError, ValueError) as exc:
                        warnings.append(f"{path.name}: metadata unavailable ({exc})")
                        metadata = {}
                with path.open(encoding="utf-8-sig", newline="") as stream:
                    reader = csv.reader(stream)
                    first_rows = [next(reader, []) for _ in range(3)]
                if first_rows[2] and first_rows[2][0].startswith('ExportFormat="transport_analysis_csv_v1";'):
                    if cache is not None:
                        cache[path] = (signature, None, warnings[warning_start:])
                    continue
                headers, units, _ = _header(iter(first_rows))
                kind = _measurement(path, root, metadata, headers)
                if not kind or not any(c in SIGNAL_COLUMNS for c in headers):
                    if cache is not None:
                        cache[path] = (signature, None, warnings[warning_start:])
                    continue
                stamp = str(metadata.get("created_at", ""))
                try:
                    dt.datetime.fromisoformat(stamp)
                except ValueError:
                    match = re.search(r"(\d{8})_(\d{6})(?:_\d+)?$", path.stem)
                    stamp = (dt.datetime.strptime("".join(match.groups()), "%Y%m%d%H%M%S") if match else dt.datetime.fromtimestamp(path.stat().st_mtime)).isoformat(timespec="seconds")
                record = HistoryRecord(path, kind, stamp, str(metadata.get("status", "unknown")), _conditions(metadata), headers, units, metadata, (stat.st_mtime_ns, stat.st_size))
                records.append(record)
                if cache is not None:
                    cache[path] = (signature, record, warnings[warning_start:])
            except (OSError, ValueError, csv.Error) as exc:
                warnings.append(f"{path.name}: {exc}")
    except OSError as exc:
        warnings.append(f"Folder scan failed: {exc}")
    if cache is not None:
        for missing in set(cache) - seen_paths:
            cache.pop(missing, None)
    records.sort(key=lambda r: (r.created_at, str(r.path)), reverse=True)
    return records, warnings


def read_csv_snapshot(path):
    """Read selected data once, tolerating only an unfinished final CSV row."""
    with Path(path).open(encoding="utf-8-sig", newline="") as stream:
        reader = csv.reader(stream)
        headers, units, first = _header(reader)
        rows = ([first] if first else []) + list(reader)
    rows = [row for row in rows if any(v.strip() for v in row)]
    warnings = []
    if rows and len(rows[-1]) != len(headers):
        rows.pop()
        warnings.append("Incomplete trailing row ignored; refresh after acquisition writes it")
    for index, row in enumerate(rows):
        if len(row) != len(headers):
            raise ValueError(f"Malformed row {index + 1} before end of CSV")
    return headers, units, rows, warnings


def cached_csv_snapshot(path, cache=None, fingerprint=None):
    """Share parsed data and condition summaries across display channels."""
    def read():
        headers, units, rows, warnings = read_csv_snapshot(path)
        conditions = summarize_conditions(headers, units, rows)
        if cache is not None:
            rows = CsvColumns.from_rows(headers, rows, numeric)
        return headers, units, rows, warnings, conditions
    if cache is None:
        return read()
    if fingerprint is None:
        stat = Path(path).stat()
        fingerprint = (stat.st_mtime_ns, stat.st_size)
    return cache.get(("csv", str(path), fingerprint), read)


def resolve_column(headers, name):
    if name == "E-field" and name not in headers and "Efield" in headers:
        return "Efield"
    if name == "Ids_Keithley" and name not in headers and "Keithley_current" in headers:
        return "Keithley_current"
    return name


def normalize_direction(value):
    value = value.strip().lower()
    return "backward" if value == "reverse" else value


def load_curve(path, x_name, signal, cache=None, fingerprint=None) -> CurveData:
    headers, units, rows, warnings, saved_conditions = cached_csv_snapshot(path, cache, fingerprint)
    warnings = list(warnings)
    x_name = resolve_column(headers, x_name)
    signal = resolve_column(headers, signal)
    if x_name != "Step Index" and x_name not in headers:
        raise ValueError(f"Missing column: {x_name}")
    y, y_unit, signal_warnings = signal_values(headers, units, rows, signal)
    warnings.extend(signal_warnings)
    xs, directions, groups = [], [], []
    xi = headers.index(x_name) if x_name != "Step Index" else None
    di = next((headers.index(name) for name in ("Direction", "FastDirection") if name in headers), None)
    group_names = [name for name in ("Condition", "Condition_index", "PassIndex") if name in headers]
    for index, row in enumerate(rows):
        xs.append(float(index) if xi is None else numeric(row[xi]))
        directions.append(normalize_direction(row[di]) if di is not None else "unknown")
        groups.append(", ".join(f"{name}={row[headers.index(name)]}" for name in group_names))
    x = np.asarray(xs, dtype=float)
    if not np.any(np.isfinite(x) & np.isfinite(y)):
        raise ValueError("No finite measurements for selected columns")
    return CurveData(x, y, tuple(directions), units.get(x_name, ""), y_unit, warnings, tuple(groups), saved_conditions)


def comparison_traces(records, x_name, signal, direction="All", cache=None):
    traces, warnings = [], []
    expected_units = None
    for record in records:
        try:
            curve = (cache.get(("curve", str(record.path), record.fingerprint, x_name, signal), lambda: load_curve(record.path, x_name, signal, cache, record.fingerprint))
                     if cache is not None else load_curve(record.path, x_name, signal))
            units = (curve.x_unit, curve.y_unit)
            if expected_units is not None and units != expected_units:
                raise ValueError(f"Incompatible units {units}; expected {expected_units}")
            starts = [0] + [i for i in range(1, len(curve.x)) if curve.directions[i] != curve.directions[i - 1] or curve.groups[i] != curve.groups[i - 1]] + [len(curve.x)]
            added = False
            for start, end in zip(starts, starts[1:]):
                value = curve.directions[start]
                if direction != "All" and value != direction:
                    continue
                x, y = curve.x[start:end], curve.y[start:end]
                if not np.any(np.isfinite(x) & np.isfinite(y)):
                    continue
                group = curve.groups[start]
                label = f"{record.path.stem} [{value}] {group}"
                traces.append(CurveTrace(record.path, label, value, x, y, *units, group, curve.saved_conditions))
                added = True
            if added:
                expected_units = units
            else:
                warnings.append(f"{record.path.name}: no points for direction {direction}")
            warnings.extend(f"{record.path.name}: {w}" for w in curve.warnings)
        except (OSError, ValueError, csv.Error) as exc:
            warnings.append(f"{record.path.name}: {exc}")
    return traces, warnings
