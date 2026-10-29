"""Read-only Qt Quick analysis backend. No widgets or acquisition imports.

One worker lane owns both caches; generation/revision tickets prevent an old
folder or selection from publishing data. Exports use the published arrays.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path

import numpy as np
from PySide6 import QtCore

from app.curve_cache import SnapshotCache
from app.curve_history import (MEASUREMENT_TYPES, SIGNAL_COLUMNS, X_COLUMNS,
                               comparison_traces, discover_history, cached_csv_snapshot)
from app.curve_map import infer_map_axes, map_comparison
from app.curve_normalization import normalize_traces, comparison_y_label
from app.plot_export_labels import comparison_labels, conditions
from app.settings import get_app_settings
from app.ui.png_export_process import PngExportProcess
from app.quick.plot import PlotModel


def _defaults(kind):
    return {"x": {"vds_sweep": "Vds", "gate_scan": "Doping", "map_2d": "Vtg",
                  "bfield_gate_scan": "Doping", "bfield_transport": "B_measured_T",
                  "photocurrent": "Wavelength"}[kind], "signal": "Ids_DC", "direction": "All",
            "normalization": "raw", "map_y": "Vbg", "fixed_axis": "", "fixed_value": None,
            "pass": "All rows", "primary_path": "", "auto_axes": True, "legend": True,
            "cmap": "RdBu_r", "four_channels": False}


def _index_display(folder, header_cache, previous_rows=None):
    """Prepare display metadata on the serial worker, away from key events."""
    records, warnings = discover_history(folder, header_cache)
    previous_rows = previous_rows or {}
    rows = {}
    for record in records:
        path = str(record.path)
        cached = previous_rows.get(path)
        rows[path] = cached if cached is not None and cached[0] is record else (record, HistoryBackend._file_row(record))
    # The discovery cache includes both CSV and sidecar stats. Reuse that key
    # instead of serializing every saved instrument snapshot on every poll.
    signature = tuple((str(r.path), header_cache[r.path][0]) for r in records)
    return records, warnings, rows, signature


def _read_display(records, view, kind, cache):
    """Runs only on the serial worker, reusing the established parsers."""
    if view.get("four_channels") and (kind != "map_2d" or view["map_y"] == "None (1D)"):
        panels, panel_traces = {}, {}
        for channel in view["channel_order"]:
            panel_view = {**view, **view.get("channel_views", {}).get(channel, {}),
                          "signal": channel, "four_channels": False, "auto_axes": False}
            panels[channel], panel_traces[channel], _ = _read_display(records if channel else [], panel_view, kind, cache)
            if not channel:
                panels[channel]["warnings"] = ["No channel assigned to this panel"]
        selected = view["signal"] if view["signal"] in panels else next(iter(panels))
        snapshot = {**panels[selected], "signal": selected, "view": view, "panels": panels}
        snapshot["records"] = list({r["path"]: r for p in panels.values() for r in p["records"]}.values())
        snapshot["warnings"] = [f"{c}: {w}" for c, p in panels.items() for w in p["warnings"]]
        return snapshot, panel_traces[selected], None
    primary = None
    actual = dict(view)
    if kind == "map_2d" and view["auto_axes"] and records:
        record = next((r for r in records if str(r.path) == view["primary_path"]), records[0])
        axes = infer_map_axes(record)
        if axes is None:
            axes = cache.get(("axes", str(record.path), record.fingerprint),
                             lambda: infer_map_axes(record, cached_csv_snapshot(record.path, cache, record.fingerprint)))
        if axes:
            actual.update(x=axes[0], map_y=axes[1])
    view = actual
    if kind == "map_2d" and view["map_y"] != "None (1D)":
        result = map_comparison(records, view["x"], view["map_y"], view["signal"],
                                view["direction"], view["pass"], view["primary_path"],
                                view["fixed_axis"], view["fixed_value"], cache, view["auto_axes"])
        primary, traces, warnings = result
        if primary:
            actual.update(x=primary.x_name, map_y=primary.y_name,
                          primary_path=str(primary.path), fixed_axis=result.fixed_axis,
                          fixed_value=result.fixed_value)
    else:
        traces, warnings = comparison_traces(records, view["x"], view["signal"], view["direction"], cache)
    traces = normalize_traces(traces, view["normalization"])
    paths = {str(t.path) for t in traces}
    if primary:
        paths.add(str(primary.path))
    exported = []
    measured = {str(t.path): t.saved_conditions for t in traces}
    if primary:
        measured[str(primary.path)] = primary.saved_conditions
    for r in records:
        if str(r.path) in paths:
            exported.append({"path": str(r.path), "created_at": r.created_at, "status": r.status,
                             "fingerprint": list(r.fingerprint),
                             "metadata": {**copy.deepcopy(r.metadata), "csv_conditions": measured.get(str(r.path), {})}})
    x_name = (primary.x_name if actual["fixed_axis"] == primary.y_name else primary.y_name) if primary else view["x"]
    snapshot = {"mode": "map" if primary else "curve", "kind": kind, "signal": view["signal"],
                "x_name": x_name, "x_unit": traces[0].x_unit if traces else "",
                "y_unit": traces[0].y_unit if traces else primary.signal_unit if primary else "",
                "records": exported, "traces": [{"path": str(t.path), "x": t.x.copy(), "y": t.y.copy(),
                "direction": t.direction, "condition": t.condition} for t in traces],
                "warnings": list(warnings), "view": actual}
    if primary:
        snapshot["map"] = {"path": str(primary.path), "x_name": primary.x_name, "y_name": primary.y_name,
                           "x_unit": primary.x_unit, "y_unit": primary.y_unit, "signal_unit": primary.signal_unit,
                           "x": primary.x.copy(), "y": primary.y.copy(), "z": primary.z.copy()}
    # Export serialization can run concurrently; nobody may mutate published data.
    for trace in snapshot["traces"]:
        trace["x"].setflags(write=False)
        trace["y"].setflags(write=False)
    if primary:
        for key in ("x", "y", "z"):
            snapshot["map"][key].setflags(write=False)
    return snapshot, traces, primary


class _WorkerSignals(QtCore.QObject):
    done = QtCore.Signal(str, object, object, str)


class _Worker(QtCore.QRunnable):
    def __init__(self, lane, ticket, function, signals):
        super().__init__()
        self.lane, self.ticket, self.function, self.signals = lane, ticket, function, signals

    def run(self):
        try:
            self.signals.done.emit(self.lane, self.ticket, self.function(), "")
        except Exception as exc:
            self.signals.done.emit(self.lane, self.ticket, None, str(exc))


class HistoryBackend(QtCore.QObject):
    changed = QtCore.Signal()
    searchChanged = QtCore.Signal()
    rangeChanged = QtCore.Signal()
    filesChanged = QtCore.Signal()
    busyChanged = QtCore.Signal()
    focusRequested = QtCore.Signal()
    closeRequested = QtCore.Signal()
    reloadRequested = QtCore.Signal()
    event = QtCore.Signal(object)

    def __init__(self, folder, parent=None, settings=None, exporter=None, autostart=True):
        super().__init__(parent)
        self._folder = Path(folder).expanduser().absolute()
        self._current_folder = self._folder
        self._follow = True
        self._settings = settings if settings is not None else get_app_settings()
        self._dark = self._settings.value("quick/theme/dark", False, type=bool)
        self._records, self._files = [], []
        self._file_rows = {}
        self._kind = "vds_sweep"
        self._views = {k: _defaults(k) for k in MEASUREMENT_TYPES}
        self._checked = {k: set() for k in MEASUREMENT_TYPES}
        self._search, self._date, self._scroll = "", "All dates", 0.0
        self._expanded = False
        self._status, self._snapshot = "", None
        self._index_warnings = []
        self._range_errors = {axis: "" for axis in ("x", "y", "color")}
        self._restore_warning = ""
        self._generation, self._revision = 0, 0
        self._index_active = self._plot_active = False
        self._index_visible = False
        self._index_pending = self._plot_pending = False
        self._closed = False
        self._reload_closing = False
        self._header_cache = {}
        self._cache = SnapshotCache(64 * 1024 * 1024)
        self._pool = QtCore.QThreadPool(self)
        self._pool.setMaxThreadCount(1)
        self._signals = _WorkerSignals(self)
        self._signals.done.connect(self._finished, QtCore.Qt.ConnectionType.QueuedConnection)
        self._plot = PlotModel(self)
        self._cut = PlotModel(self)
        self._exporter = exporter or PngExportProcess(self)
        self._exporter.completed.connect(self._export_finished)
        self._export_job = None
        self._restore_limits = None
        self._last_signature = None
        self._layout = {"width": 1380, "height": 900, "x": 80, "y": 60, "fileWidth": 350, "cutHeight": 280}
        try:
            self._layout.update(json.loads(self._settings.value("quick_history/layout", "{}")))
        except (ValueError, TypeError):
            pass
        self._publishing = False
        self.changed.connect(self.rangeChanged.emit)
        self._view_save_timer = QtCore.QTimer(self)
        self._view_save_timer.setSingleShot(True)
        self._view_save_timer.setInterval(150)
        self._view_save_timer.timeout.connect(self.save_state)
        self._search_timer = QtCore.QTimer(self)
        self._search_timer.setSingleShot(True)
        self._search_timer.setInterval(150)
        self._search_timer.timeout.connect(self._apply_search)
        self._load_state()
        self._plot.viewChanged.connect(self._remember_plot_view)
        self._cut.viewChanged.connect(self._remember_plot_view)
        self._plot.cutRequested.connect(self._map_clicked)
        self._timer = QtCore.QTimer(self)
        self._auto_refresh = autostart
        self._timer.setSingleShot(True)
        self._timer.setInterval(4000)
        self._timer.timeout.connect(self._automatic_refresh)
        if autostart:
            QtCore.QTimer.singleShot(0, self.refresh)

    folder = QtCore.Property(str, lambda s: str(s._folder), notify=changed)
    following = QtCore.Property(bool, lambda s: s._follow, notify=changed)
    dark = QtCore.Property(bool, lambda s: s._dark, notify=changed)
    measurementTypes = QtCore.Property("QVariantList", lambda s: [{"key": k, "label": v} for k, v in MEASUREMENT_TYPES.items()], constant=True)
    currentType = QtCore.Property(str, lambda s: s._kind, notify=changed)
    files = QtCore.Property("QVariantList", lambda s: s._files, notify=filesChanged)
    state = QtCore.Property("QVariantMap", lambda s: dict(s._views[s._kind]), notify=changed)
    search = QtCore.Property(str, lambda s: s._search, notify=searchChanged)
    date = QtCore.Property(str, lambda s: s._date, notify=changed)
    dates = QtCore.Property("QVariantList", lambda s: ["All dates"] + sorted({r.date for r in s._records if r.measurement == s._kind}, reverse=True), notify=changed)
    scrollPosition = QtCore.Property(float, lambda s: s._scroll, notify=changed)
    expanded = QtCore.Property(bool, lambda s: s._expanded, notify=changed)
    status = QtCore.Property(str, lambda s: s.statusDetails, notify=changed)
    statusDetails = QtCore.Property(str, lambda s: "\n".join(dict.fromkeys([*s._index_warnings, *s._status.splitlines()])), notify=changed)
    selectedCount = QtCore.Property(int, lambda s: len(s._selected()), notify=changed)
    typeFileCount = QtCore.Property(int, lambda s: sum(r.measurement == s._kind for r in s._records), notify=changed)
    traceCount = QtCore.Property(int, lambda s: sum(len(p["traces"]) for p in s._snapshot["panels"].values())
                                if s._snapshot and s._snapshot.get("panels") else
                                len(s._snapshot.get("traces", [])) if s._snapshot else 0, notify=changed)
    emptyState = QtCore.Property("QVariantMap", lambda s: s._empty_state(), notify=changed)
    selectionSummary = QtCore.Property(str, lambda s: s._selection_summary(), notify=changed)
    busy = QtCore.Property(bool, lambda s: s._index_visible or s._plot_active or s._plot_pending, notify=busyChanged)
    exportReady = QtCore.Property(bool, lambda s: bool(s._snapshot and s._snapshot["records"]) and not s.busy and not s._export_job, notify=changed)
    plotModel = QtCore.Property(QtCore.QObject, lambda s: s._plot, constant=True)
    cutPlotModel = QtCore.Property(QtCore.QObject, lambda s: s._cut, constant=True)
    xChoices = QtCore.Property("QVariantList", lambda s: list(X_COLUMNS), constant=True)
    signalChoices = QtCore.Property("QVariantList", lambda s: list(SIGNAL_COLUMNS), constant=True)
    mapSources = QtCore.Property("QVariantList", lambda s: [{"path": str(r.path), "label": r.path.name} for r in s._selected()], notify=changed)
    hasMap = QtCore.Property(bool, lambda s: bool(s._snapshot and s._snapshot.get("map")), notify=changed)
    cutReady = QtCore.Property(bool, lambda s: s.exportReady and bool(s._snapshot.get("traces")), notify=changed)
    cutEmptyText = QtCore.Property(str, lambda s: s._cut_empty_text(), notify=changed)
    measuredValues = QtCore.Property("QVariantList", lambda s: s._coordinates(), notify=changed)
    passes = QtCore.Property("QVariantList", lambda s: ["All rows"] + getattr(s, "_passes", []), notify=changed)
    layout = QtCore.Property("QVariantMap", lambda s: dict(s._layout), notify=changed)
    rangeState = QtCore.Property("QVariantMap", lambda s: s._range_state(), notify=rangeChanged)

    @QtCore.Slot(bool)
    def setDark(self, dark):
        if self._dark == bool(dark):
            return
        self._dark = bool(dark)
        self._settings.setValue("quick/theme/dark", self._dark)
        self.changed.emit()

    def _empty_state(self):
        if not self._folder.is_dir():
            return {"title": "Folder not found", "detail": "Choose an existing measurement folder or return to the current device.", "action": "folder"}
        if not self.typeFileCount:
            return {"title": "No saved files for this measurement", "detail": "Choose another measurement type or browse to a folder containing saved CSV files.", "action": "folder"}
        if not self._files:
            return {"title": "No files match these filters", "detail": "Clear the search and date filters to see available files. Checked files remain selected.", "action": "filters"}
        if not self._selected():
            return {"title": "Select files to compare", "detail": "Check one or more files in the list to display measured data.", "action": ""}
        if not self._snapshot or (not self.traceCount and not self._snapshot.get("map")):
            return {"title": "No plottable data for this selection", "detail": "Try another signal, direction or source file. Open details below for parser warnings.", "action": ""}
        return {"title": "", "detail": "", "action": ""}

    def _selection_summary(self):
        selected = self._selected()
        prefix = f"{len(selected)} selected files · {self.traceCount} displayed traces"
        hints = {"vds_sweep": "Fixed gate biases", "gate_scan": "Saved raw-voltage / derived trajectory",
                 "bfield_gate_scan": "Gate trajectories at saved B", "bfield_transport": "Measured B (T)",
                 "photocurrent": "Each file may contain multiple gate-condition traces", "map_2d": "Exact measured cuts; no interpolation"}
        if self._kind == "map_2d" and self.state["map_y"] == "None (1D)":
            hints["map_2d"] = "1D curves — Map Y: None"
        if not selected:
            return hints[self._kind]
        values = []
        for record in selected:
            saved = conditions({"path": str(record.path), "metadata": record.metadata})
            keys = {"vds_sweep": ("Vtg", "Vbg"), "gate_scan": ("Vds",),
                    "bfield_gate_scan": ("B", "B (start)", "Vds"), "bfield_transport": ("Vds", "Doping", "E-field"),
                    "photocurrent": ("Vtg", "Vbg", "Vds"), "map_2d": ()}[self._kind]
            text = " · ".join(f"{k}={saved[k]}" for k in keys if k in saved)
            if self._kind == "gate_scan":
                params = record.metadata.get("params", {})
                mode = params.get("coordinate_mode", params.get("mode", ""))
                if mode:
                    text = str(mode) + (" · " + text if text else "")
            if text and text not in values:
                values.append(text)
        context = " / ".join(values[:3]) + (f" / +{len(values) - 3} more conditions" if len(values) > 3 else "")
        return prefix + " · " + (context or hints[self._kind])

    def _cut_empty_text(self):
        values = self._coordinates()
        available = ", ".join(f"{v:.8g}" for v in values[:12])
        if len(values) > 12:
            available += f", … ({len(values)} values)"
        return ("No exact measured cut at this coordinate.\nChoose a measured value above or click a map cell."
                + ("\nAvailable " + self.state["fixed_axis"] + ": " + available if available else ""))

    @QtCore.Slot(str, result=str)
    def axisLabel(self, name):
        return "Measured B (T)" if name == "B_measured_T" else name

    @QtCore.Slot()
    def clearFilters(self):
        self._search_timer.stop()
        self._set_search("")
        self._date = "All dates"
        self._filter()
        self.save_state()
        self.changed.emit()

    @QtCore.Slot()
    def clearSelection(self):
        self._search_timer.stop()
        self._checked[self._kind].clear()
        self._filter()
        self._schedule_plot()
        self.save_state()

    @QtCore.Slot()
    def copyStatus(self):
        from PySide6 import QtGui
        QtGui.QGuiApplication.clipboard().setText(self.statusDetails)

    @QtCore.Slot(str, result=str)
    def suggestedExportUrl(self, scope):
        recent = Path(self._settings.value("quick_history/export_folder", str(self._folder)))
        folder = recent if recent.is_dir() else self._folder if self._folder.is_dir() else Path.home()
        source = self._selected()
        stem = source[0].path.stem if len(source) == 1 else self._kind + "_comparison"
        suffix = "_" + scope if self.hasMap else ""
        return QtCore.QUrl.fromLocalFile(str(folder / (stem + suffix + ".png"))).toString()

    @QtCore.Slot(result=str)
    def dialogFolderUrl(self):
        recent = Path(self._settings.value("quick_history/export_folder", str(self._folder)))
        selected = self._selected()
        candidates = [self._folder, *(r.path.parent for r in selected), recent, self._current_folder, Path.home()]
        folder = next((path for path in candidates if path.is_dir()), Path.home())
        return QtCore.QUrl.fromLocalFile(str(folder)).toString()

    def _range_state(self):
        view = self._plot.captureView()
        y_index = self._plot.channels().index(self._plot.channel) if self._plot.channel in self._plot.channels() else 0
        result = {"x": {"auto": not view.get("manual_x", False), "bounds": list(map(float, view["xlim"]))},
                "y": {"auto": not view.get("manual_y", [False])[y_index], "bounds": list(map(float, view["ylim"][y_index]))},
                "color": {"auto": view.get("clim_auto", True), "bounds": list(map(float, view["clim"]))}}
        for axis, label in (("x", self._plot.xLabel), ("y", self._plot.yLabel), ("color", self._plot.zLabel)):
            result[axis].update(error=self._range_errors[axis], title=label,
                                unit=label.rsplit(" (", 1)[1][:-1] if " (" in label and label.endswith(")") else "")
        return result

    @QtCore.Slot(str, bool, str, str)
    def setRange(self, axis, automatic, low, high):
        try:
            if axis not in ("x", "y", "color"):
                return
            values = [float(low), float(high)] if not automatic else None
            if values and (not all(np.isfinite(values)) or values[0] >= values[1]):
                raise ValueError("Range bounds must be finite and increasing")
            previous_error = self._range_errors[axis]
            self._range_errors[axis] = ""
            if previous_error and self._status == previous_error:
                self._status = ""
            if axis == "color":
                self._plot.autoColorLimits() if automatic else self._plot.setColorLimits(*values)
            else:
                view = self._plot.captureView()
                if axis == "x":
                    view.update(manual_x=not automatic)
                    if values:
                        view["xlim"] = values
                else:
                    index = self._plot.channels().index(self._plot.channel) if self._plot.channel in self._plot.channels() else 0
                    view["manual_y"][index] = not automatic
                    if values:
                        view["ylim"][index] = values
                    if view.get("four_channels"):
                        view["channel_views"][self._plot.channels()[index]].update(
                            manual_y=[not automatic], ylim=view["ylim"][index])
                self._plot.applyView(view)
            self._remember_plot_view()
        except (ValueError, TypeError) as exc:
            self._range_errors[axis] = str(exc)
            self._status = str(exc)
        self.changed.emit()

    @QtCore.Slot(str, float)
    def setLayout(self, key, value):
        if key in self._layout and np.isfinite(value):
            self._layout[key] = float(value)
            self._settings.setValue("quick_history/layout", json.dumps(self._layout))

    def _remember_plot_view(self):
        if self._snapshot and not self._publishing:
            self._views[self._kind].update(self.capture_view())
            # Mouse moves update only range controls. Rebuilding every history
            # property and serializing all settings per move delays dragging.
            self._view_save_timer.start()
            self.rangeChanged.emit()

    @QtCore.Slot(float, float)
    def _map_clicked(self, x, y):
        if self.hasMap:
            value = y if self.state["fixed_axis"] == self.state["map_y"] else x
            self.setOption("fixed_value", value)

    def _key(self):
        return "quick_history/folders/" + hashlib.sha256(os.path.normcase(str(self._folder)).encode()).hexdigest()

    def _load_state(self):
        try:
            legacy_key = self._key().replace("quick_history/", "curve_compare/")
            state = json.loads(self._settings.value(self._key(), self._settings.value(legacy_key, "{}")))
            self._kind = state.get("kind", "vds_sweep")
            if self._kind not in MEASUREMENT_TYPES:
                self._kind = "vds_sweep"
            for kind in MEASUREMENT_TYPES:
                self._views[kind] = {**_defaults(kind), **state.get("views", {}).get(kind, {})}
                self._checked[kind] = set(state.get("checks", {}).get(kind, state.get("checked", [])))
            self._set_search(state.get("search", ""))
            self._date = state.get("date", "All dates")
            self._scroll = float(state.get("scroll", 0))
        except (TypeError, ValueError):
            pass

    def save_state(self):
        state = {"kind": self._kind, "views": self._views, "checks": {k: sorted(v) for k, v in self._checked.items()},
                 "search": self._search, "date": self._date, "scroll": self._scroll}
        encoded = json.dumps(state, ensure_ascii=False)
        if self._settings.value(self._key()) != encoded:
            self._settings.setValue(self._key(), encoded)

    def _selected(self):
        return [r for r in self._records if r.measurement == self._kind and str(r.path) in self._checked[self._kind]]

    def _filter(self):
        needle = self._search.casefold().strip()
        files = []
        for r in self._records:
            if r.measurement != self._kind or (self._date != "All dates" and self._date != r.date):
                continue
            path = str(r.path)
            cached = self._file_rows.get(path)
            if cached is None or cached[0] is not r:
                cached = (r, self._file_row(r))
                self._file_rows[path] = cached
            row, searchable = cached[1]
            if not needle or needle in searchable:
                files.append({**row, "checked": path in self._checked[self._kind]})
        if files != self._files:
            self._files = files
            self.filesChanged.emit()

    @staticmethod
    def _file_row(record):
        return ({"path": str(record.path), "name": record.path.name, "time": record.created_at,
                 "conditions": HistoryBackend._file_summary(record), "status": record.status},
                (record.path.name + " " + record.conditions + " " + record.status).casefold())

    @staticmethod
    def _file_summary(record):
        saved = conditions({"path": str(record.path), "metadata": record.metadata})
        params = record.metadata.get("params", {})
        pieces = [record.conditions]
        if record.measurement in ("gate_scan", "bfield_gate_scan"):
            mode = params.get("coordinate_mode", params.get("mode")) if isinstance(params, dict) else None
            if mode:
                pieces.insert(0, str(mode) + " trajectory")
        for key in ("B", "B (start)"):
            if saved.get(key):
                pieces.append(f"{key}={saved[key]}")
        if record.measurement == "bfield_transport":
            pieces.append("X: Measured B (T)")
        return " · ".join(pieces)

    @QtCore.Slot(str)
    def selectType(self, kind):
        if kind not in MEASUREMENT_TYPES or kind == self._kind:
            return
        self._kind, self._scroll = kind, 0.0
        self._search_timer.stop()
        self._filter()
        self._schedule_plot()
        self.save_state()
        self.changed.emit()

    @QtCore.Slot(str, bool)
    def setChecked(self, path, checked):
        if not any(str(r.path) == path and r.measurement == self._kind for r in self._records):
            return
        self._search_timer.stop()
        if checked:
            self._checked[self._kind].add(path)
        else:
            self._checked[self._kind].discard(path)
        self._filter()
        self._schedule_plot()
        self.save_state()
        self.changed.emit()

    @QtCore.Slot(str, "QVariant")
    def setOption(self, key, value):
        if key in ("search", "date"):
            self._search_timer.stop()
            if key == "search":
                self._set_search(str(value))
            else:
                self._date = str(value)
            self._filter()
        elif key == "scroll":
            self._scroll = max(0., float(value))
        elif key == "expanded":
            self._expanded = bool(value)
        elif key in self._views[self._kind]:
            if key == "normalization" and value not in ("raw", "max_abs", "min_max"):
                return
            if key == "direction" and value not in ("All", "forward", "backward", "unknown"):
                return
            if key == "fixed_value":
                try:
                    value = float(value) if value is not None and str(value) != "" else None
                    if value is not None and not np.isfinite(value):
                        raise ValueError("Enter a finite measured coordinate")
                except (TypeError, ValueError) as exc:
                    self._status = str(exc)
                    self.changed.emit()
                    return
            if self._views[self._kind][key] == value and not (key == "signal" and self._views[self._kind].get("four_channels")):
                return
            self._views[self._kind][key] = value
            if key == "signal":
                # The signal selector returns a restored overview to the
                # requested single channel; the saved overview is explicit.
                self._views[self._kind]["four_channels"] = False
                self._plot.fourChannels = False
                self._views[self._kind]["clim_auto"] = True
                self._plot.autoColorLimits()
            if key in ("x", "map_y"):
                self._views[self._kind]["auto_axes"] = False
            self._schedule_plot()
        else:
            return
        self.save_state()
        self.changed.emit()

    def _set_search(self, text):
        if text != self._search:
            self._search = text
            self.searchChanged.emit()

    @QtCore.Slot(str)
    def queueSearch(self, text):
        if self._closed or text == self._search:
            return
        # Keep the editor's bound value current without rebuilding the entire
        # QVariantList after each keystroke.
        self._set_search(text)
        self._search_timer.start()

    def _apply_search(self):
        self._search_timer.stop()
        self._filter()
        self.save_state()
        self.changed.emit()

    @QtCore.Slot(str)
    def setFolder(self, folder):
        if folder.startswith("file:"):
            folder = QtCore.QUrl(folder).toLocalFile()
        self._follow = False
        self._set_folder(Path(folder).expanduser().absolute())

    def _set_folder(self, folder):
        if folder == self._folder:
            if self._search_timer.isActive():
                self._apply_search()
            self.refresh()
            return
        self._search_timer.stop()
        self.save_state()
        self._folder = folder
        self._generation += 1
        self._revision += 1
        self._records, self._files, self._snapshot = [], [], None
        self._file_rows = {}
        self._index_warnings = []
        self._range_errors = {axis: "" for axis in ("x", "y", "color")}
        self._last_signature = None
        self._load_state()
        self._plot.setTraces([])
        self._cut.setTraces([])
        self.filesChanged.emit()
        self.changed.emit()
        self.refresh()

    @QtCore.Slot()
    def followCurrent(self):
        self._follow = True
        self._set_folder(self._current_folder)
        self.changed.emit()

    @QtCore.Slot()
    def refresh(self):
        self._refresh(automatic=False)

    def _automatic_refresh(self):
        self._refresh(automatic=True)

    def _refresh(self, *, automatic):
        if self._closed:
            return
        if self._index_active:
            if not automatic:
                self._index_pending = True
                if not self._index_visible:
                    self._index_visible = True
                    self.busyChanged.emit()
                    self.changed.emit()
            return
        self._timer.stop()
        self._index_active = True
        self._index_visible = not automatic
        folder, ticket = self._folder, self._generation
        previous_rows = self._file_rows
        self._pool.start(_Worker("index", ticket, lambda: _index_display(folder, self._header_cache, previous_rows), self._signals))
        if self._index_visible:
            self.busyChanged.emit()
            self.changed.emit()

    def _schedule_plot(self):
        self._revision += 1
        self._plot_pending = True
        if not self._plot_active:
            self._start_plot()
        self.busyChanged.emit()
        self.changed.emit()

    def _start_plot(self):
        self._plot_pending = False
        self._plot_active = True
        ticket = (self._generation, self._revision)
        records, view, kind = self._selected(), dict(self._views[self._kind]), self._kind
        self._pool.start(_Worker("plot", ticket, lambda: _read_display(records, view, kind, self._cache), self._signals))

    @QtCore.Slot(str, object, object, str)
    def _finished(self, lane, ticket, result, error):
        if lane == "index":
            was_visible = self._index_visible
            self._index_active = False
            self._index_visible = False
            changed = False
            if ticket == self._generation and not self._closed:
                if error:
                    warnings = [error]
                else:
                    records, warnings, rows, signature = result
                    if signature != self._last_signature:
                        selected_paths = {str(r.path) for r in self._selected()}
                        previous_selection = tuple(key for key in self._last_signature or () if key[0] in selected_paths)
                        initial = self._last_signature is None
                        self._records = records
                        self._file_rows = rows
                        self._filter()
                        selected_paths = {str(r.path) for r in self._selected()}
                        selection = tuple(key for key in signature if key[0] in selected_paths)
                        self._last_signature = signature
                        changed = True
                        if initial or selection != previous_selection:
                            self._schedule_plot()
                if list(warnings) != self._index_warnings:
                    self._index_warnings = list(warnings)
                    changed = True
            if self._index_pending and not self._closed:
                self._index_pending = False
                self.refresh()
            elif self._auto_refresh and not self._closed:
                # A slow scan must finish before the next polling interval
                # begins; automatic refresh never builds a pending backlog.
                self._timer.start()
            if was_visible:
                self.busyChanged.emit()
            if was_visible or changed:
                self.changed.emit()
            return
        else:
            self._plot_active = False
            if ticket == (self._generation, self._revision) and not self._closed:
                if error:
                    self._snapshot = None
                    self._plot.setTraces([])
                    self._cut.setTraces([])
                    self._status = error
                else:
                    self._publish(*result)
            if self._plot_pending and not self._closed:
                self._start_plot()
        self.busyChanged.emit()
        self.changed.emit()

    def _publish(self, snapshot, traces, primary):
        self._publishing = True
        self._snapshot = snapshot
        # A pan can arrive while the worker is reading. Keep the latest GUI
        # viewport, while accepting axes/cut coordinates inferred by the worker.
        if primary or snapshot["view"].get("auto_axes") and snapshot["kind"] == "map_2d":
            self._views[self._kind].update({k: snapshot["view"][k] for k in
                                           ("x", "map_y", "primary_path", "fixed_axis", "fixed_value")})
        # Use the same semantic legends as analysis PNGs.
        labels = comparison_labels(snapshot["records"], snapshot["traces"])
        rows = [{"x": t.x, "y": t.y, "label": " · ".join(dict.fromkeys(label.split(" · "))), "direction": t.direction} for t, label in zip(traces, labels)]
        if snapshot.get("panels"):
            rows = []
            for channel, panel in snapshot["panels"].items():
                labels = comparison_labels(panel["records"], panel["traces"])
                rows.extend({**t, "label": label, "channel": channel}
                            for t, label in zip(panel["traces"], labels))
        x_label = self.axisLabel(snapshot["x_name"]) if snapshot["x_name"] == "B_measured_T" else snapshot["x_name"] + (f" ({snapshot['x_unit']})" if snapshot["x_unit"] else "")
        y_label = comparison_y_label(snapshot["signal"], snapshot["y_unit"], snapshot["view"]["normalization"])
        if primary:
            self._plot.setHeatmap(snapshot["map"], primary.x_name + (f" ({primary.x_unit})" if primary.x_unit else ""),
                                  primary.y_name + (f" ({primary.y_unit})" if primary.y_unit else ""),
                                  snapshot["signal"] + (f" ({primary.signal_unit})" if primary.signal_unit else ""))
            self._cut.setTraces(rows, x_label, y_label)
            self._passes = [f"{p:g}" for p in primary.passes]
        else:
            panel_labels = {c: comparison_y_label(c, p["y_unit"], snapshot["view"]["normalization"])
                            for c, p in snapshot.get("panels", {}).items()}
            self._plot.setTraces(rows, x_label, y_label, panel_labels)
            self._cut.setTraces([])
            self._passes = []
        if hasattr(self._plot, "setColorMap"):
            self._plot.setColorMap(snapshot["view"].get("cmap", "RdBu_r"))
        view = self._restore_limits or self._views[self._kind]
        self._restore_limits = None
        # setTraces deliberately retains zoom during data refresh. Clear it
        # here before applying this measurement type's own saved viewport.
        self._plot.home()
        self._cut.home()
        if primary:
            map_view = {k: v for k, v in view.items() if k in ("map_xlim", "map_ylim", "clim", "clim_auto", "cmap")}
            map_view.update(manual_x=view.get("manual_map_x", False), manual_y=[view.get("manual_map_y", False)],
                            four_channels=False, signal=view["signal"])
            self._plot.applyView(map_view)
            self._cut.applyView({**{k: v for k, v in view.items() if k in ("xlim", "ylim", "manual_x", "manual_y")},
                                 "four_channels": False, "signal": view["signal"]})
        else:
            self._plot.applyView(view)
        self._publishing = False
        self._status = "\n".join([*snapshot["warnings"], *([self._restore_warning] if self._restore_warning else [])]) or f"{len(snapshot['records'])} source files · {self.traceCount} traces"

    def _coordinates(self):
        if not self._snapshot or not self._snapshot.get("map"):
            return []
        data = self._snapshot["map"]
        return [float(v) for v in data["y"] if np.isfinite(v)] if self.state["fixed_axis"] == data["y_name"] else [float(v) for v in data["x"] if np.isfinite(v)]

    def capture_view(self):
        view = dict(self._views[self._kind])
        if self.hasMap:
            view.update(self._cut.captureView())
            mapping = self._plot.captureView()
            for key in ("map_xlim", "map_ylim", "clim", "clim_auto", "cmap"):
                if key in mapping:
                    view[key] = mapping[key]
            view["manual_map_x"] = mapping.get("manual_x", False)
            view["manual_map_y"] = mapping.get("manual_y", [False])[0]
        else:
            view.update(self._plot.captureView())
        # Matplotlib exports a single comparison axis; Quick keeps a list for
        # the optional four-panel live display.
        if not view.get("four_channels") and view.get("ylim") and isinstance(view["ylim"][0], (list, tuple)):
            view["ylim"] = view["ylim"][0]
        return view

    def export_snapshot(self):
        if not self.exportReady:
            raise ValueError("Wait for the selected files and display settings to finish loading")
        view = self.capture_view()
        result = {**self._snapshot, "view": view}
        if result.get("panels"):
            result["panels"] = {c: {**panel, "view": {**view, **view["channel_views"][c]}}
                                for c, panel in result["panels"].items()}
        return result

    @QtCore.Slot(str, str)
    def exportView(self, mode="both", output=""):
        try:
            snapshot = self.export_snapshot()
            if mode == "heatmap" and not snapshot.get("map"):
                raise ValueError("No heatmap is displayed")
            if mode == "cut" and not snapshot["traces"]:
                raise ValueError("No measured cut is displayed")
            target = QtCore.QUrl(output).toLocalFile() if output.startswith("file:") else output
            if target:
                target = str(Path(target).with_suffix(".png"))
                self._settings.setValue("quick_history/export_folder", str(Path(target).parent))
            self._export_job = self._exporter.submit_snapshot(snapshot, Path(target) if target else None,
                                                             cut_only=mode == "cut", heatmap_only=mode == "heatmap")
            self._status = "Exporting displayed PNG + CSV snapshot in background…"
        except (ValueError, OSError, RuntimeError) as exc:
            self._status = str(exc)
        self.changed.emit()

    @QtCore.Slot(object)
    def _export_finished(self, result):
        if result.get("id") != self._export_job:
            return
        self._export_job = None
        self._status = ("Export failed: " + result["error"]) if result.get("error") else "Saved " + ", ".join(result.get("outputs", []))
        self.changed.emit()

    @QtCore.Slot(str)
    def restoreView(self, path):
        try:
            if path.startswith("file:"):
                path = QtCore.QUrl(path).toLocalFile()
            payload = json.loads(Path(path).read_text(encoding="utf-8-sig"))
            if payload.get("schema") != "transport_plot_view_v1":
                raise ValueError("Unsupported plot-view format")
            view, sources = payload["view"], payload["sources"]
            if view.get("four_channels") or view.get("signal") == "4-Channel Compare":
                order = view.get("channel_order")
                if (not isinstance(order, list) or len(order) != 4 or len(set(order)) != 4
                        or any(c not in SIGNAL_COLUMNS and c != "" for c in order)):
                    raise ValueError("Four-channel view requires four supported channel names")
                view["four_channels"] = True
                view["auto_axes"] = False
                if view.get("signal") == "4-Channel Compare":
                    view["signal"] = next(c for c in order if c)
            if not sources or view.get("kind") not in MEASUREMENT_TYPES:
                raise ValueError("No supported source files in this view")
            paths = [Path(r["path"]).absolute() for r in sources]
            if any(not p.is_file() for p in paths):
                raise ValueError("A source CSV is missing; restore its original location first")
            self.setFolder(os.path.commonpath([str(p.parent) for p in paths]))
            self._kind = view["kind"]
            self._checked[self._kind] = {str(p) for p in paths}
            self._views[self._kind] = {**_defaults(self._kind), **view}
            self._set_search("")
            self._date = "All dates"
            self._restore_limits = view
            changed = [p.name for p, source in zip(paths, sources) if source.get("fingerprint") and tuple(source["fingerprint"]) != (p.stat().st_mtime_ns, p.stat().st_size)]
            self._restore_warning = "Source files changed since export: " + ", ".join(changed) if changed else ""
            self._status = "View restored" + ("; " + self._restore_warning if changed else "")
            self._last_signature = None
            self.refresh()
            self.changed.emit()
        except (OSError, ValueError, KeyError, TypeError) as exc:
            self._status = "Cannot restore view: " + str(exc)
            self.changed.emit()

    def capture_session(self):
        if self._search_timer.isActive():
            self._apply_search()
        self.save_state()
        return {"schema": "transport_analysis_session_v1", "folder": str(self._folder),
                "custom_folder": None if self._follow else str(self._folder), "kind": self._kind,
                "checked": sorted(set().union(*self._checked.values())), "checks": {k: sorted(v) for k, v in self._checked.items()},
                "date": self._date, "search": self._search, "views": copy.deepcopy(self._views),
                "scroll": self._scroll, "expanded": self._expanded,
                "layout": dict(self._layout),
                "limits": self.capture_view() if self._snapshot else None}

    def restore_session(self, state):
        if state.get("schema") != "transport_analysis_session_v1" or state.get("kind") not in MEASUREMENT_TYPES:
            raise ValueError("Unsupported analysis session")
        self._follow = state.get("custom_folder") is None
        folder = self._current_folder if self._follow else Path(state["folder"])
        self._set_folder(folder)
        if self._follow and str(folder) != state["folder"]:
            return
        self._kind = state["kind"]
        for kind in MEASUREMENT_TYPES:
            self._views[kind] = {**_defaults(kind), **state.get("views", {}).get(kind, {})}
            self._checked[kind] = set(state.get("checks", {}).get(kind, state.get("checked", [])))
        self._date = state.get("date", "All dates")
        self._set_search(state.get("search", ""))
        self._scroll, self._expanded = float(state.get("scroll", 0)), bool(state.get("expanded", False))
        self._layout.update(state.get("layout", {}))
        self._restore_limits = state.get("limits")
        self._last_signature = None
        self._filter()
        self._schedule_plot()
        self.refresh()
        self.changed.emit()

    @QtCore.Slot()
    def requestReload(self):
        self.reloadRequested.emit()

    @QtCore.Slot(object)
    def handleCommand(self, message):
        command = message.get("command")
        if command == "folder" and isinstance(message.get("folder"), str):
            self._current_folder = Path(message["folder"]).expanduser().absolute()
            if self._follow:
                self._set_folder(self._current_folder)
            self.event.emit({"event": "folder", "folder": str(self._current_folder)})
        elif command == "refresh":
            self.refresh()
        elif command == "focus":
            self.focusRequested.emit()
        elif command == "close":
            self.closeRequested.emit()
        elif command == "reload":
            self._reload_closing = True
            self.event.emit({"event": "reload_session", "session": self.capture_session()})
            self.closeRequested.emit()
        elif command == "restore_session":
            try:
                self.restore_session(message["session"])
                self.event.emit({"event": "session_loaded", "session": self.capture_session()})
            except (ValueError, TypeError, KeyError) as exc:
                self.event.emit({"event": "session_error", "error": str(exc)})

    def can_close(self):
        return not self._exporter.pool.activeThreadCount() and (not self._reload_closing or not self._exporter.busy)

    def shutdown(self):
        if self._closed:
            return
        self._closed = True
        self._timer.stop()
        self._search_timer.stop()
        self._view_save_timer.stop()
        self.save_state()
        self._generation += 1
        self._pool.clear()
        self._pool.waitForDone()
        self._exporter.shutdown()


def create_history_backend(folder, **kwargs):
    return HistoryBackend(folder, **kwargs)
