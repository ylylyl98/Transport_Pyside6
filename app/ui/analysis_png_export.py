"""Analysis export actions and immutable view snapshots, independent of acquisition."""
from __future__ import annotations

import json
import os
from pathlib import Path

from PySide6 import QtCore, QtWidgets

from app.curve_history import MEASUREMENT_TYPES
from app.ui.png_export_process import PngExportProcess
from app.ui.analysis_session import capture_view


class AnalysisPngExport(QtCore.QObject):
    def __init__(self, page):
        super().__init__(page)
        self.page = page
        self.exporter = PngExportProcess(self)
        self.exporter.completed.connect(self._completed, QtCore.Qt.ConnectionType.QueuedConnection)
        self.job_id = None
        self.restore_view = None
        self.restore_sources = []
        self.button = QtWidgets.QPushButton("Export PNG + CSV")
        self.button.setAccessibleName("Export PNG and CSV with source information")
        self.menu = QtWidgets.QMenu(self.button)
        self.heatmap_action = self.menu.addAction("Heatmap only")
        self.cut_action = self.menu.addAction("Cut only")
        self.both_action = self.menu.addAction("Heatmap + cut")
        self.heatmap_action.triggered.connect(lambda: self._choose_export(False, heatmap_only=True))
        self.cut_action.triggered.connect(lambda: self._choose_export(True))
        self.both_action.triggered.connect(lambda: self._choose_export(False))
        self.menu.addSeparator()
        self.save_as_action = self.menu.addAction("Save as…")
        self.save_as_action.triggered.connect(self._save_as)
        self.button.addAction(self.save_as_action)
        self.button.setContextMenuPolicy(QtCore.Qt.ContextMenuPolicy.ActionsContextMenu)
        self.restore_button = QtWidgets.QPushButton("Restore view…")
        self.button.clicked.connect(self._export_clicked)
        self.restore_button.clicked.connect(self._choose_restore)
        self.update_actions()

    def _is_ready(self):
        page = self.page
        return bool(page._displayed_traces or page._displayed_primary) and not page._cut_edit_pending and not page._plot_active and not page.plot_timer.isActive() and page._last_plot_signature == page._desired_plot_signature

    def update_actions(self):
        ready = self._is_ready() if hasattr(self.page, "plot_timer") else False
        has_map = self.page._displayed_primary is not None
        self.button.setMenu(self.menu if has_map else None)
        self.button.setToolTip("Save PNG + CSV beside the source CSV, in its source-named folder with compact condition labels. "
                               "Unchanged exports are reused. Right-click for Save as. "
                               "CSV rows 1–3: Long Name, Units, Comments; values start at row 4.")
        self.button.setEnabled(ready and self.job_id is None)
        self.heatmap_action.setEnabled(ready and has_map and self.job_id is None)
        self.cut_action.setEnabled(ready and bool(self.page._displayed_traces) and self.job_id is None)
        self.both_action.setEnabled(self.heatmap_action.isEnabled() and self.cut_action.isEnabled())
        self.save_as_action.setEnabled(ready and self.job_id is None)
        self.restore_button.setEnabled(self.job_id is None)

    def _export_clicked(self):
        if self.button.menu() is None:
            self._choose_export(False)

    def _snapshot(self):
        page = self.page
        if not self._is_ready() or self.job_id is not None:
            raise ValueError("Wait for the selected curves to finish loading before exporting")
        traces = page._displayed_traces
        primary = page._displayed_primary
        paths = {str(trace.path) for trace in traces}
        if primary is not None:
            paths.add(str(primary.path))
        records = [{"path": str(record.path), "created_at": record.created_at, "status": record.status,
                    "fingerprint": list(record.fingerprint), "metadata": record.metadata}
                   for record in page._displayed_records if str(record.path) in paths]
        measured_conditions = {str(trace.path): trace.saved_conditions for trace in traces}
        if primary:
            measured_conditions[str(primary.path)] = primary.saved_conditions
        for record in records:
            record["metadata"] = {**record["metadata"], "csv_conditions": measured_conditions.get(record["path"], {})}
        if not records:
            raise ValueError("No displayed source files")
        x_name = (primary.x_name if page._fixed_axis() == primary.y_name else primary.y_name) if primary else page.x_combo.currentText()
        snapshot = {"mode": "map" if primary else "curve", "kind": page._current_kind,
                    "signal": page.signal_combo.currentText(), "x_name": x_name,
                    "x_unit": traces[0].x_unit if traces else "", "y_unit": traces[0].y_unit if traces else primary.signal_unit,
                    "records": records, "traces": [{"path": str(trace.path), "x": trace.x, "y": trace.y,
                                                      "direction": trace.direction, "condition": trace.condition} for trace in traces],
                    "warnings": list(page._plot_warnings),
                    "view": capture_view(page)}
        if primary:
            snapshot["map"] = {"path": str(primary.path), "x_name": primary.x_name, "y_name": primary.y_name,
                               "x_unit": primary.x_unit, "y_unit": primary.y_unit,
                               "signal_unit": primary.signal_unit,
                               "x": primary.x, "y": primary.y, "z": primary.z}
        return snapshot

    def export_to(self, path=None, cut_only=False, *, heatmap_only=False):
        snapshot = self._snapshot()
        if cut_only and not snapshot.get("traces"):
            raise ValueError("No displayed cuts")
        if heatmap_only and (cut_only or not snapshot.get("map")):
            raise ValueError("Choose a displayed heatmap for heatmap-only export")
        self.page.last_export_result = None
        self.job_id = self.exporter.submit_snapshot(snapshot, Path(path) if path is not None else None, cut_only, heatmap_only=heatmap_only)
        self.page.export_status_label.setText("Exporting PNG + CSV in background…")
        self.update_actions()

    def _choose_export(self, cut_only, *, heatmap_only=False):
        try:
            self.export_to(cut_only=cut_only, heatmap_only=heatmap_only)
        except (ValueError, OSError) as exc:
            self.page.export_status_label.setText(str(exc))

    def _save_as(self):
        snapshot = self._snapshot()
        source = Path(snapshot.get("map", {}).get("path") or snapshot["records"][0]["path"])
        from app.analysis_export import _safe_name, export_folder
        target = export_folder(source) / (_safe_name(snapshot["signal"]) + ".png")
        path, _filter = QtWidgets.QFileDialog.getSaveFileName(self.page, "Export PNG + CSV as", str(target), "PNG image (*.png)")
        if path:
            try:
                self.export_to(path)
            except (ValueError, OSError) as exc:
                self.page.export_status_label.setText(str(exc))

    def _completed(self, result):
        if result.get("id") != self.job_id:
            return
        self.job_id = None
        self.page.last_export_result = result
        if result.get("error"):
            self.page.export_status_label.setText("Export failed: " + result["error"])
        else:
            pngs, csvs = result["outputs"], result.get("csv_outputs", [])
            reused = len(result.get("reused_outputs", []))
            self.page.export_status_label.setText(f"Saved {len(pngs)} PNG + {len(csvs)} CSV" + (f" · {reused} unchanged files reused" if reused else ""))
        self.page.export_status_label.setToolTip("\n".join([*result.get("outputs", []), *result.get("csv_outputs", []), result.get("view_path", "")]))
        self.update_actions()

    def _choose_restore(self):
        recent = self.page.last_export_result or {}
        folder = Path(recent["view_path"]).parent if recent.get("view_path") else self.page.folder
        path, _filter = QtWidgets.QFileDialog.getOpenFileName(self.page, "Restore analysis view", str(folder), "Plot view (*_view.json)")
        if path:
            try:
                self.restore_export_view(path)
            except (ValueError, OSError) as exc:
                self.page.export_status_label.setText("Cannot restore view: " + str(exc))

    def restore_export_view(self, path):
        payload = json.loads(Path(path).read_text(encoding="utf-8-sig"))
        if payload.get("schema") != "transport_plot_view_v1":
            raise ValueError("Unsupported plot-view format")
        sources, view = payload["sources"], payload["view"]
        if not sources or view.get("kind") not in MEASUREMENT_TYPES:
            raise ValueError("No supported source files in this view")
        files = [Path(source["path"]).absolute() for source in sources]
        if any(not file.exists() for file in files):
            raise ValueError("A source CSV is missing; restore its original location first")
        folder = Path(os.path.commonpath([str(file.parent) for file in files]))
        page = self.page
        page._custom_folder = folder
        page.follow_button.setEnabled(True)
        page.set_folder(folder)
        page.model.checked = {str(file) for file in files}
        page._current_kind = view["kind"]
        with QtCore.QSignalBlocker(page.measurement_tabs):
            page.measurement_tabs.setCurrentIndex(list(MEASUREMENT_TYPES).index(view["kind"]))
        page._views[view["kind"]] = view
        page._restore_display()
        with QtCore.QSignalBlocker(page.map_file_combo):
            page.map_file_combo.clear()
            if view.get("primary_path"):
                page.map_file_combo.addItem(Path(view["primary_path"]).stem, view["primary_path"])
        page.date_combo.setCurrentText("All dates")
        page.search_edit.clear()
        self.restore_view, self.restore_sources = view, sources
        page._last_plot_signature = None
        page._desired_plot_signature = None
        page._filters_changed()
        page.refresh()

    def apply_restored_limits(self):
        if self.restore_view is None or not self.page._displayed_records:
            return
        page, view = self.page, self.restore_view
        for name, setter in (("xlim", page.ax.set_xlim), ("ylim", page.ax.set_ylim), ("xscale", page.ax.set_xscale), ("yscale", page.ax.set_yscale)):
            if name in view:
                setter(view[name])
        if page.map_ax is not None:
            page.map_ranges.restore(view)
            page.map_ranges.apply(page.map_ranges.state())
            if view.get("cmap") and page._colorbar is not None:
                page._colorbar.mappable.set_cmap(view["cmap"])
                with QtCore.QSignalBlocker(page.colormap_combo):
                    page.colormap_combo.setCurrentText(page._colorbar.mappable.get_cmap().name)
                page.save_state()
        changed = [Path(source["path"]).name for source in self.restore_sources
                   if source.get("fingerprint") and tuple(source["fingerprint"]) != (Path(source["path"]).stat().st_mtime_ns, Path(source["path"]).stat().st_size)]
        page.export_status_label.setText("View restored." + (" Source files changed since export: " + ", ".join(changed) if changed else ""))
        self.restore_view = None
        page.canvas.draw_idle()

    def shutdown(self):
        self.exporter.shutdown()
