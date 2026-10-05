"""Asynchronous, hardware-independent history and comparison workspace."""
from __future__ import annotations

import hashlib
import json
import math
import os
import textwrap
from pathlib import Path

from PySide6 import QtCore, QtWidgets
from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg, NavigationToolbar2QT
from matplotlib.figure import Figure
from matplotlib import colormaps
from matplotlib.font_manager import fontManager

from app.curve_history import MEASUREMENT_TYPES, X_COLUMNS, SIGNAL_COLUMNS, discover_history, comparison_traces
from app.settings import get_app_settings
from app.ui.curve_history_model import HistoryModel, HistoryFilter
from app.ui.history_item_delegate import HistoryItemDelegate
from app.curve_map import MapComparison, infer_map_axes, map_comparison
from app.curve_cache import SnapshotCache
from app.curve_normalization import NORMALIZATION_MODES, comparison_y_label, read_normalized_comparison
from app.analysis_signals import RATIO_SIGNALS
from app.plot_export_labels import comparison_heading, comparison_labels, comparison_setting_signature
from app.plot_style import style_heatmap_axes
from app.ui.analysis_png_export import AnalysisPngExport
from app.ui.analysis_session import capture_session, restore_session
from app.ui.map_view_ranges import MapViewRanges


class _TaskSignals(QtCore.QObject):
    done = QtCore.Signal(int, object, str)


class _ReadTask(QtCore.QRunnable):
    def __init__(self, number, function, args):
        super().__init__()
        self.number, self.function, self.args = number, function, args
        self.signals = _TaskSignals()

    def run(self):
        try:
            result, error = self.function(*self.args), ""
        except Exception as exc:
            result, error = None, str(exc)
        self.signals.done.emit(self.number, result, error)


class _AnalysisToolbar(NavigationToolbar2QT):
    def __init__(self, canvas, page):
        self.page = page
        super().__init__(canvas, page)

    def save_figure(self, *args):
        self.page.png_actions._choose_export(False)


class CurveComparePage(QtWidgets.QWidget):
    reload_requested = QtCore.Signal()

    def __init__(self, folder_callable, parent=None, settings=None):
        super().__init__(parent)
        self.setObjectName("curveComparePage")
        self.folder_callable = folder_callable
        self.settings = settings if settings is not None else get_app_settings()
        self.folder = None
        self._custom_folder = None
        self._closed = False
        self._folder_generation = 0
        self._plot_revision = 0
        self._jobs = {}
        self._serial = 0
        self._scan_active = False
        self._scan_pending = False
        self._plot_active = False
        self._plot_pending = False
        self._last_plot_signature = None
        self._desired_plot_signature = None
        self._scan_warnings = []
        self._filter_state = None
        self._plot_warnings = []
        self._views = {}
        self._displayed_traces = []
        self._displayed_primary = None
        self._displayed_records = []
        self.last_export_result = None
        self._current_kind = "vds_sweep"
        self.map_ax = None
        self._colorbar = None
        self._plot_heading = ("", "")
        available_fonts = {font.name for font in fontManager.ttflist}
        self._heading_fonts = [name for name in ("DejaVu Sans", "Microsoft YaHei", "Segoe UI Emoji") if name in available_fonts]
        self._cut_edit_pending = False
        self._cut_coordinates = None
        self._index_cache = {}
        self._snapshot_cache = SnapshotCache()
        self.pool = QtCore.QThreadPool(self)
        self.pool.setMaxThreadCount(2)
        self.model = HistoryModel(self)
        self.proxy = HistoryFilter(self)
        self.proxy.setSourceModel(self.model)
        self.proxy.sort(1, QtCore.Qt.SortOrder.DescendingOrder)
        self._build_ui()
        self.plot_timer = QtCore.QTimer(self)
        self.plot_timer.setSingleShot(True)
        self.plot_timer.setInterval(120)
        self.plot_timer.timeout.connect(self._start_plot)
        self.refresh_timer = QtCore.QTimer(self)
        self.refresh_timer.setInterval(5000)
        self.refresh_timer.timeout.connect(self.refresh)
        self.png_actions.update_actions()
        self.model.selection_changed.connect(self._selection_changed)
        self.follow_current_folder()

    @property
    def busy(self):
        return bool(self._jobs or self.plot_timer.isActive())

    def _build_ui(self):
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        folder_row = QtWidgets.QHBoxLayout()
        self.folder_label = QtWidgets.QLabel()
        self.folder_label.setWordWrap(True)
        folder_row.addWidget(self.folder_label, 1)
        self.follow_button = QtWidgets.QPushButton("Current device")
        self.follow_button.setToolTip("Follow the operator/device folder in Sample / Files")
        self.follow_button.clicked.connect(self.use_current_folder)
        self.follow_button.setEnabled(False)
        folder_row.addWidget(self.follow_button)
        browse = QtWidgets.QPushButton("Browse folder…")
        browse.clicked.connect(self._browse)
        folder_row.addWidget(browse)
        self.refresh_button = QtWidgets.QPushButton("Refresh")
        self.refresh_button.clicked.connect(self.refresh)
        folder_row.addWidget(self.refresh_button)
        self.reload_button = QtWidgets.QPushButton("Reload analysis")
        self.reload_button.setAccessibleName("Reload analysis modules without restarting Transport")
        self.reload_button.setToolTip("Finish accepted exports, reload analysis code, and restore this view. Measurements keep running.")
        self.reload_button.clicked.connect(self.reload_requested)
        self.reload_button.hide()
        folder_row.addWidget(self.reload_button)
        layout.addLayout(folder_row)
        self.measurement_tabs = QtWidgets.QTabBar()
        self.measurement_tabs.setAccessibleName("Measurement type history")
        self.measurement_tabs.setExpanding(False)
        for kind, title in MEASUREMENT_TYPES.items():
            index = self.measurement_tabs.addTab(title)
            self.measurement_tabs.setTabData(index, kind)
        self.measurement_tabs.currentChanged.connect(self._type_changed)
        layout.addWidget(self.measurement_tabs)
        self.splitter = QtWidgets.QSplitter(QtCore.Qt.Orientation.Horizontal)
        self.splitter.setChildrenCollapsible(False)
        self.history_panel = QtWidgets.QWidget()
        self.history_panel.setMinimumWidth(300)
        history_layout = QtWidgets.QVBoxLayout(self.history_panel)
        history_layout.setContentsMargins(0, 0, 8, 0)
        heading = QtWidgets.QHBoxLayout()
        heading.addWidget(QtWidgets.QLabel("Measurements · newest first"), 1)
        self.expand_files_button = QtWidgets.QPushButton("Expand file list")
        self.expand_files_button.setCheckable(True)
        self.expand_files_button.setAccessibleName("Expand file list or return to comparison")
        self.expand_files_button.toggled.connect(self._expand_files)
        heading.addWidget(self.expand_files_button)
        history_layout.addLayout(heading)
        filters = QtWidgets.QHBoxLayout()
        self.date_combo = QtWidgets.QComboBox()
        self.date_combo.addItems(["All dates", "Today"])
        self.search_edit = QtWidgets.QLineEdit()
        self.search_edit.setPlaceholderText("Filename, condition or status")
        self.search_edit.setClearButtonEnabled(True)
        for title, widget in (("Date", self.date_combo),):
            label = QtWidgets.QLabel(title)
            label.setBuddy(widget)
            widget.setAccessibleName(title + " filter")
            filters.addWidget(label)
            filters.addWidget(widget, 1 if widget is self.search_edit else 0)
        self.clear_button = QtWidgets.QPushButton("Clear selection")
        self.clear_button.clicked.connect(self._clear_selection)
        filters.addWidget(self.clear_button)
        history_layout.addLayout(filters)
        self.search_edit.setAccessibleName("Search measurement filename, condition or status")
        history_layout.addWidget(self.search_edit)
        self.count_label = QtWidgets.QLabel("No measurements")
        self.count_label.setWordWrap(True)
        history_layout.addWidget(self.count_label)
        self.table = QtWidgets.QListView()
        self.table.setObjectName("curveHistoryTable")
        self.table.setAccessibleName("Measurement history; check files to overlay curves")
        self.table.setModel(self.proxy)
        self.table.setModelColumn(0)
        self.table.setItemDelegate(HistoryItemDelegate(self.table))
        self.table.setTextElideMode(QtCore.Qt.TextElideMode.ElideNone)
        self.table.setResizeMode(QtWidgets.QListView.ResizeMode.Adjust)
        self.table.setLayoutMode(QtWidgets.QListView.LayoutMode.Batched)
        self.table.setBatchSize(100)
        self.table.setSpacing(6)
        self.table.setVerticalScrollMode(QtWidgets.QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.table.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.table.setSelectionMode(QtWidgets.QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setAlternatingRowColors(True)
        self.table.setWordWrap(True)
        history_layout.addWidget(self.table, 1)
        self.splitter.addWidget(self.history_panel)
        self.chart_panel = QtWidgets.QWidget()
        chart = self.chart_panel
        chart_layout = QtWidgets.QVBoxLayout(chart)
        chart_layout.setContentsMargins(0, 0, 0, 0)
        self.comparison_controls = QtWidgets.QWidget()
        controls = QtWidgets.QHBoxLayout(self.comparison_controls)
        controls.setContentsMargins(0, 0, 0, 0)
        self._comparison_fields = {}
        self._control_placement = None
        self.x_combo = QtWidgets.QComboBox()
        self.x_combo.addItems(X_COLUMNS)
        self.signal_combo = QtWidgets.QComboBox()
        self.signal_combo.addItems(SIGNAL_COLUMNS)
        for name, (numerator, denominator) in RATIO_SIGNALS.items():
            self.signal_combo.setItemData(self.signal_combo.findText(name),
                f"{numerator} / {denominator}; dimensionless. Zero Drive or missing values stay blank.",
                QtCore.Qt.ItemDataRole.ToolTipRole)
        self.direction_combo = QtWidgets.QComboBox()
        self.direction_combo.addItems(["All", "forward", "backward", "unknown"])
        for title, widget in (("X", self.x_combo), ("Signal", self.signal_combo), ("Direction", self.direction_combo)):
            field = QtWidgets.QWidget()
            field_layout = QtWidgets.QHBoxLayout(field)
            field_layout.setContentsMargins(0, 0, 0, 0)
            label = QtWidgets.QLabel(title)
            label.setBuddy(widget)
            widget.setAccessibleName("Comparison " + title)
            field_layout.addWidget(label)
            field_layout.addWidget(widget, 1)
            self._comparison_fields[title] = field
            controls.addWidget(field)
        chart_layout.addWidget(self.comparison_controls)
        self.display_controls = QtWidgets.QWidget()
        display_controls = QtWidgets.QHBoxLayout(self.display_controls)
        display_controls.setContentsMargins(0, 0, 0, 0)
        self.normalization_combo = QtWidgets.QComboBox()
        self.normalization_combo.setAccessibleName("Comparison normalization")
        for mode, title in NORMALIZATION_MODES.items():
            self.normalization_combo.addItem(title, mode)
        self.normalization_combo.setToolTip(
            "Each displayed direction/condition trace is normalized independently.\n"
            "Max |Y|: divide by its largest absolute value; signs are preserved.\n"
            "0–1: (Y - min) / (max - min); constant traces are shown at zero.\n"
            "Uses all finite points in each displayed trace, regardless of zoom.\n"
            "For 2D maps, only comparison cuts are normalized; heatmaps keep physical units."
        )
        self.normalization_label = QtWidgets.QLabel("Normalize")
        self.normalization_label.setBuddy(self.normalization_combo)
        display_controls.addWidget(self.normalization_label)
        display_controls.addWidget(self.normalization_combo)
        self.legend_check = QtWidgets.QCheckBox("Legend")
        self.legend_check.setChecked(True)
        display_controls.addWidget(self.legend_check)
        controls.addSpacing(8)
        controls.addWidget(self.display_controls)
        controls.addStretch()
        self.auto_axes_check = QtWidgets.QCheckBox("Auto axes")
        self.auto_axes_check.setChecked(True)
        self.auto_axes_check.setToolTip("Use the selected map's saved fast/slow axes; infer a grid for older CSVs. Choosing X or Map Y manually turns this off.")
        self.auto_axes_check.toggled.connect(self._selection_changed)
        self.colormap_label = QtWidgets.QLabel("Color")
        self.colormap_combo = QtWidgets.QComboBox()
        preferred = ["RdBu_r", "RdBu", "viridis", "plasma", "inferno", "magma", "cividis", "coolwarm", "seismic", "gray"]
        self.colormap_combo.addItems(preferred + sorted(set(colormaps) - set(preferred)))
        self.colormap_combo.setAccessibleName("Map colormap")
        self.colormap_combo.setSizeAdjustPolicy(QtWidgets.QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.colormap_combo.setMinimumContentsLength(8)
        self.colormap_label.setBuddy(self.colormap_combo)
        self.colormap_combo.currentTextChanged.connect(self._colormap_changed)
        settings_row = QtWidgets.QHBoxLayout()
        self.map_controls = QtWidgets.QGroupBox("Heatmap")
        map_layout = QtWidgets.QVBoxLayout(self.map_controls)
        map_source = QtWidgets.QHBoxLayout()
        self.map_axes_row = QtWidgets.QGridLayout()
        self.map_axes_row.setColumnStretch(0, 1)
        self.map_axes_row.setColumnStretch(1, 2)
        map_form = QtWidgets.QHBoxLayout()
        self.map_options_row = map_form
        self.map_file_combo = QtWidgets.QComboBox()
        self.map_file_combo.setSizeAdjustPolicy(QtWidgets.QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.map_file_combo.setMinimumContentsLength(12)
        self.map_y_combo = QtWidgets.QComboBox()
        self.map_y_combo.addItems(["Vbg", "Vtg", "Vds", "Doping", "E-field", "None (1D)"])
        self.pass_combo = QtWidgets.QComboBox()
        self.pass_combo.addItem("All rows")
        self.map_ranges = MapViewRanges(self)
        for row, key in enumerate(('map_xlim', 'map_ylim', 'clim')):
            self.map_axes_row.addWidget(self.map_ranges.rows[key], row, 1)
        for title, short_title, widget in (("Source CSV", "Source CSV", self.map_file_combo), ("Map Y", "Y", self.map_y_combo), ("Row / pass", "Pass", self.pass_combo)):
            label = QtWidgets.QLabel(short_title)
            label.setBuddy(widget)
            widget.setAccessibleName(title)
            if widget is self.map_y_combo:
                field = QtWidgets.QWidget()
                row_layout = QtWidgets.QHBoxLayout(field)
                row_layout.setContentsMargins(0, 0, 0, 0)
                self.map_axes_row.addWidget(field, 1, 0)
            else:
                row_layout = map_source if widget is self.map_file_combo else map_form
            row_layout.addWidget(label)
            row_layout.addWidget(widget, 1)
            handler = self._axes_changed if widget is self.map_y_combo else self._selection_changed
            widget.currentIndexChanged.connect(handler)
        map_source.addWidget(self.auto_axes_check)
        map_form.addWidget(self.colormap_label)
        map_form.addWidget(self.colormap_combo, 1)
        map_layout.addLayout(map_source)
        map_layout.addLayout(self.map_axes_row)
        map_layout.addLayout(map_form)
        map_layout.addWidget(self.map_ranges.error_label)
        self.map_controls.hide()
        settings_row.addWidget(self.map_controls, 3)
        self.cut_controls = QtWidgets.QGroupBox("Line cut")
        cut_layout = QtWidgets.QVBoxLayout(self.cut_controls)
        self.cut_axis_row = QtWidgets.QWidget()
        cut_row = QtWidgets.QHBoxLayout(self.cut_axis_row)
        cut_row.setContentsMargins(0, 0, 0, 0)
        self.fixed_axis_combo = QtWidgets.QComboBox()
        self.fixed_axis_combo.setSizeAdjustPolicy(QtWidgets.QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.fixed_axis_combo.setMinimumContentsLength(11)
        self.fixed_axis_combo.setAccessibleName("Line cut direction: fix Y or fix X")
        self.fixed_axis_combo.currentIndexChanged.connect(self._map_fixed_axis_changed)
        self.fixed_value_combo = QtWidgets.QComboBox()
        self.fixed_value_combo.setSizeAdjustPolicy(QtWidgets.QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.fixed_value_combo.setMinimumContentsLength(8)
        self.fixed_value_combo.setEditable(True)
        self.fixed_value_combo.setInsertPolicy(QtWidgets.QComboBox.InsertPolicy.NoInsert)
        self.fixed_value_combo.setAccessibleName("Requested cut coordinate")
        self.fixed_value_combo.setToolTip("Type a coordinate and press Enter or Apply. The nearest measured coordinate is used; the actual value is shown below. You can also choose a measured value from the list.")
        self.fixed_value_combo.currentIndexChanged.connect(self._cut_value_selected)
        self.fixed_value_combo.lineEdit().textEdited.connect(self._cut_value_edited)
        self.fixed_value_combo.lineEdit().editingFinished.connect(self._commit_cut_value)
        cut_row.addWidget(self.fixed_axis_combo, 1)
        value_label = QtWidgets.QLabel("Value")
        value_label.setBuddy(self.fixed_value_combo)
        cut_row.addWidget(value_label)
        cut_row.addWidget(self.fixed_value_combo, 1)
        self.cut_unit_label = QtWidgets.QLabel()
        cut_row.addWidget(self.cut_unit_label)
        self.apply_cut_button = QtWidgets.QPushButton("Apply")
        self.apply_cut_button.clicked.connect(self._commit_cut_value)
        cut_row.addWidget(self.apply_cut_button)
        cut_layout.addWidget(self.cut_axis_row)
        self.cut_display_row = QtWidgets.QHBoxLayout()
        cut_layout.addLayout(self.cut_display_row)
        self.cut_value_label = QtWidgets.QLabel("Enter a value or choose a measured coordinate.")
        self.cut_value_label.setWordWrap(True)
        cut_layout.addWidget(self.cut_value_label)
        cut_layout.addStretch()
        settings_row.addWidget(self.cut_controls, 2)
        chart_layout.addLayout(settings_row)
        self.figure = Figure(figsize=(8, 4), layout="constrained")
        self.ax = self.figure.add_subplot(111)
        self.canvas = FigureCanvasQTAgg(self.figure)
        self.canvas.mpl_connect("resize_event", self._update_plot_heading)
        self.toolbar = _AnalysisToolbar(self.canvas, self)
        self.png_actions = AnalysisPngExport(self)
        self.export_button = self.png_actions.button
        export_row = QtWidgets.QHBoxLayout()
        export_row.addWidget(self.toolbar, 1)
        for button in (self.export_button, self.png_actions.restore_button):
            export_row.addWidget(button)
        chart_layout.addLayout(export_row)
        chart_layout.addWidget(self.canvas, 1)
        self.splitter.addWidget(chart)
        self.splitter.setStretchFactor(0, 0)
        self.splitter.setStretchFactor(1, 1)
        self.splitter.setSizes([420, 820])
        layout.addWidget(self.splitter, 1)
        self.export_status_label = QtWidgets.QLabel()
        self.export_status_label.setWordWrap(True)
        self.export_status_label.setTextInteractionFlags(QtCore.Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.export_status_label)
        self.status_label = QtWidgets.QLabel("Check measurements to compare their curves")
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)
        self.warning_label = QtWidgets.QLabel()
        self.warning_label.setWordWrap(True)
        self.warning_label.setTextInteractionFlags(QtCore.Qt.TextInteractionFlag.TextSelectableByMouse)
        self.warning_label.hide()
        layout.addWidget(self.warning_label)
        for widget in (self.date_combo,):
            widget.currentTextChanged.connect(self._filters_changed)
        self.search_edit.textChanged.connect(self._filters_changed)
        self.x_combo.currentTextChanged.connect(self._axes_changed)
        for widget in (self.signal_combo, self.direction_combo):
            widget.currentTextChanged.connect(self._selection_changed)
        self.normalization_combo.currentIndexChanged.connect(self._selection_changed)
        self.legend_check.toggled.connect(self._legend_changed)
        self._empty_plot()

    def _place_comparison_controls(self, map_page, map_mode):
        placement = (map_page, map_mode)
        if self._control_placement == placement:
            return
        self._control_placement = placement
        fields = self._comparison_fields
        if map_page:
            self.map_axes_row.addWidget(fields["X"], 0, 0)
            self.map_axes_row.addWidget(fields["Signal"], 2, 0)
            self.map_options_row.insertWidget(0, fields["Direction"])
        else:
            for index, name in enumerate(("X", "Signal", "Direction")):
                self.comparison_controls.layout().insertWidget(index, fields[name])
        display_row = self.cut_display_row if map_mode else self.comparison_controls.layout()
        display_row.insertWidget(0 if map_mode else 3, self.display_controls)
        # Reparenting hides widgets; keep the shared controls available in their
        # new group while retaining their existing signal connections and state.
        for widget in (*fields.values(), self.display_controls):
            widget.show()
        self.comparison_controls.setVisible(not map_mode)
        range_order = lambda key: [self.map_ranges.mode_controls[key], *self.map_ranges.bound_edits[key]]
        order = ([self.map_file_combo, self.auto_axes_check, self.x_combo, *range_order('map_xlim'),
                  self.map_y_combo, *range_order('map_ylim'), self.signal_combo, *range_order('clim'),
                  self.direction_combo, self.pass_combo, self.colormap_combo, self.fixed_axis_combo, self.fixed_value_combo,
                  self.apply_cut_button, self.normalization_combo, self.legend_check] if map_mode else
                 [self.x_combo, self.signal_combo, self.direction_combo,
                  self.normalization_combo, self.legend_check])
        for first, second in zip(order, order[1:]):
            QtWidgets.QWidget.setTabOrder(first, second)

    def _update_plot_heading(self, *_):
        heading = self.figure.suptitle("", x=.02, ha="left", fontsize=10, fontfamily=self._heading_fonts)
        heading.set_visible(any(self._plot_heading))
        if not any(self._plot_heading):
            return
        renderer = self.canvas.get_renderer()
        font = heading.get_fontproperties()
        available = max(100, self.figure.bbox.width * .96)
        measure = lambda text: renderer.get_text_width_height_descent(text, font, ismath=False)[0]
        lines = []
        for part in self._plot_heading:
            for line in part.splitlines():
                columns = max(12, int(len(line) * available / max(1, measure(line))))
                wrapped = textwrap.fill(line, width=columns, break_on_hyphens=False)
                while columns > 12 and any(measure(text) > available for text in wrapped.splitlines()):
                    columns = max(12, int(columns * .9))
                    wrapped = textwrap.fill(line, width=columns, break_on_hyphens=False)
                lines.append(wrapped)
        heading.set_text("\n".join(lines))

    def _set_plot_heading(self, primary=None):
        saved = {str(trace.path): trace.saved_conditions for trace in self._displayed_traces}
        if primary is not None:
            saved[str(primary.path)] = primary.saved_conditions
        records = [{"path": str(record.path), "metadata": {**record.metadata, "csv_conditions": saved[str(record.path)]}}
                   for record in self._displayed_records if str(record.path) in saved]
        excluded = (primary.x_name, primary.y_name) if primary is not None else (self.x_combo.currentText(),)
        self._plot_heading = comparison_heading(records, self.signal_combo.currentText(), excluded,
                                                str(primary.path) if primary is not None else "")
        self._update_plot_heading()

    def _axes_changed(self, *_):
        if self._current_kind == "map_2d":
            with QtCore.QSignalBlocker(self.auto_axes_check):
                self.auto_axes_check.setChecked(False)
        self._selection_changed()

    def _set_map_axes(self, axes):
        for combo, value in zip((self.x_combo, self.map_y_combo), axes):
            value = "E-field" if value == "Efield" else value
            with QtCore.QSignalBlocker(combo):
                if combo.findText(value) < 0:
                    combo.addItem(value)
                combo.setCurrentText(value)

    def _colormap_changed(self, *_):
        if self._colorbar is not None:
            self._colorbar.mappable.set_cmap(self.colormap_combo.currentText())
            self.canvas.draw_idle()
        if hasattr(self, "plot_timer"):
            self.save_state()

    def _fixed_axis(self):
        return self.fixed_axis_combo.currentData() or self.fixed_axis_combo.currentText()

    def _cut_value_selected(self, *_):
        self._cut_edit_pending = False
        self._selection_changed()

    def _cut_value_edited(self, *_):
        self._cut_edit_pending = True
        self.png_actions.update_actions()

    def _commit_cut_value(self, *_):
        text = self.fixed_value_combo.currentText().strip().replace("−", "-")
        try:
            value = float(text)
            if not math.isfinite(value):
                raise ValueError
        except ValueError:
            self._cut_edit_pending = True
            self.cut_value_label.setText("Enter a finite numeric coordinate, then press Enter or Apply.")
            self.png_actions.update_actions()
            return
        changed = value != self.fixed_value_combo.currentData()
        with QtCore.QSignalBlocker(self.fixed_value_combo):
            index = self.fixed_value_combo.findData(value)
            if index < 0:
                self.fixed_value_combo.addItem(f"{value:.12g}", value)
                index = self.fixed_value_combo.count() - 1
            self.fixed_value_combo.setCurrentIndex(index)
            self.fixed_value_combo.setEditText(f"{value:.12g}")
        self._cut_edit_pending = False
        if changed:
            self.cut_value_label.setText(f"Requested {value:g}; finding the nearest measured coordinate…")
        self._selection_changed()

    def _expand_files(self, expanded):
        if expanded:
            self._splitter_sizes = self.splitter.sizes()
            self.chart_panel.hide()
            self.expand_files_button.setText("Return to comparison")
        else:
            self.chart_panel.show()
            self.splitter.setSizes(getattr(self, "_splitter_sizes", [420, 820]))
            self.expand_files_button.setText("Expand file list")
            self.canvas.draw_idle()

    def _state_key(self):
        canonical = os.path.normcase(str(self.folder))
        digest = hashlib.sha256(canonical.encode()).hexdigest()
        return "curve_compare/folders/" + digest

    def save_state(self):
        if self.folder is None:
            return
        self._views[self._current_kind] = self._display_state()
        state = {"checked": sorted(self.model.checked), "date": self.date_combo.currentText(),
                 "kind": self._current_kind, "search": self.search_edit.text(), "views": self._views}
        key, serialized = self._state_key(), json.dumps(state)
        if self.settings.value(key, "") != serialized:
            self.settings.setValue(key, serialized)
            self.settings.sync()

    def _restore_state(self):
        try:
            state = json.loads(str(self.settings.value(self._state_key(), "{}")))
            if not isinstance(state, dict):
                state = {}
        except (ValueError, TypeError):
            state = {}
        checked = state.get("checked", [])
        self.model.checked = set(str(v) for v in checked) if isinstance(checked, list) else set()
        self._views = state.get("views", {}) if isinstance(state.get("views", {}), dict) else {}
        kind = state.get("kind", "vds_sweep")
        self._current_kind = kind if kind in MEASUREMENT_TYPES else "vds_sweep"
        with QtCore.QSignalBlocker(self.measurement_tabs):
            self.measurement_tabs.setCurrentIndex(list(MEASUREMENT_TYPES).index(self._current_kind))
        values = ((self.date_combo, state.get("date", "All dates")),)
        for combo, value in values:
            with QtCore.QSignalBlocker(combo):
                if combo is self.date_combo and value not in ("Today", "All dates") and combo.findText(str(value)) < 0:
                    combo.addItem(str(value))
                combo.setCurrentText(str(value))
        with QtCore.QSignalBlocker(self.search_edit):
            self.search_edit.setText(str(state.get("search", "")))
        self._restore_display()
        self._filters_changed()

    def _display_state(self):
        return {**self.map_ranges.state(), "x": self.x_combo.currentText(), "signal": self.signal_combo.currentText(),
                "direction": self.direction_combo.currentText(), "legend": self.legend_check.isChecked(),
                "normalization": self.normalization_combo.currentData(),
                "map_y": self.map_y_combo.currentText(), "fixed_axis": self._fixed_axis(),
                "fixed_value": self.fixed_value_combo.currentData(), "pass": self.pass_combo.currentText(),
                "primary_path": self.map_file_combo.currentData(), "auto_axes": self.auto_axes_check.isChecked(),
                "cmap": self.colormap_combo.currentText()}

    def _restore_display(self):
        self._cut_edit_pending = False
        view = self._views.get(self._current_kind, {})
        if not isinstance(view, dict):
            view = {}
        self.map_ranges.restore(view)
        default_x = {"vds_sweep": "Vds", "gate_scan": "Doping", "bfield_gate_scan": "Doping", "map_2d": "Vtg", "bfield_transport": "B_measured_T", "photocurrent": "Wavelength"}[self._current_kind]
        for combo, value in ((self.x_combo, view.get("x", default_x)), (self.signal_combo, view.get("signal", "Ids_DC")), (self.direction_combo, view.get("direction", "All"))):
            with QtCore.QSignalBlocker(combo):
                combo.setCurrentText(str(value))
        with QtCore.QSignalBlocker(self.legend_check):
            self.legend_check.setChecked(bool(view.get("legend", True)))
        with QtCore.QSignalBlocker(self.auto_axes_check):
            self.auto_axes_check.setChecked(bool(view.get("auto_axes", not bool(view.get("primary_path")))))
        with QtCore.QSignalBlocker(self.colormap_combo):
            self.colormap_combo.setCurrentText(view.get("cmap") if view.get("cmap") in colormaps else "RdBu_r")
        with QtCore.QSignalBlocker(self.normalization_combo):
            self.normalization_combo.setCurrentIndex(max(0, self.normalization_combo.findData(view.get("normalization", "raw"))))
        with QtCore.QSignalBlocker(self.map_file_combo):
            self.map_file_combo.clear()
            if view.get("primary_path"):
                self.map_file_combo.addItem(Path(view["primary_path"]).stem, view["primary_path"])
        with QtCore.QSignalBlocker(self.map_y_combo), QtCore.QSignalBlocker(self.fixed_axis_combo), QtCore.QSignalBlocker(self.fixed_value_combo), QtCore.QSignalBlocker(self.pass_combo):
            self.map_y_combo.setCurrentText(str(view.get("map_y", "Vbg")))
            self.fixed_axis_combo.clear()
            if view.get("fixed_axis"):
                self.fixed_axis_combo.addItem(str(view["fixed_axis"]), str(view["fixed_axis"]))
            self.fixed_value_combo.clear()
            if view.get("fixed_value") is not None:
                value = float(view["fixed_value"])
                self.fixed_value_combo.addItem(f"{value:g}", value)
            self.pass_combo.clear()
            self.pass_combo.addItem("All rows")
            if view.get("pass", "All rows") != "All rows":
                self.pass_combo.addItem(str(view["pass"]))
                self.pass_combo.setCurrentText(str(view["pass"]))
        self._configure_axes()

    def _map_fixed_axis_changed(self, *_):
        self._cut_edit_pending = False
        with QtCore.QSignalBlocker(self.fixed_value_combo):
            self.fixed_value_combo.clear()
        self._selection_changed()

    def _type_changed(self, index):
        if not hasattr(self, "plot_timer"):
            return
        self._views[self._current_kind] = self._display_state()
        self._current_kind = self.measurement_tabs.tabData(index)
        self._restore_display()
        self._filters_changed()

    def _configure_axes(self):
        map_mode = self._current_kind == "map_2d" and self.map_y_combo.currentText() != "None (1D)"
        self._place_comparison_controls(self._current_kind == "map_2d", map_mode)
        self.map_controls.setVisible(self._current_kind == "map_2d")
        self.auto_axes_check.setVisible(self._current_kind == "map_2d")
        self.colormap_label.setVisible(map_mode)
        self.colormap_combo.setVisible(map_mode)
        self.map_ranges.set_visible(map_mode)
        self.cut_controls.setVisible(map_mode)
        self.cut_axis_row.setVisible(map_mode)
        self.cut_value_label.setVisible(map_mode)
        self.normalization_label.setText("Normalize")
        if map_mode == (self.map_ax is not None):
            return
        self._colorbar = None
        self.figure.clear()
        if map_mode:
            self.map_ax, self.ax = self.figure.subplots(1, 2)
        else:
            self.map_ax = None
            self.ax = self.figure.add_subplot(111)
        self._last_plot_signature = None
        self.toolbar.update()
        self._empty_plot()

    def set_folder(self, folder):
        folder = Path(folder).expanduser().absolute()
        if folder == self.folder:
            return
        self.save_state()
        self.folder = folder
        self._index_cache = {}
        self._folder_generation += 1
        self._last_plot_signature = None
        self.model.replace([])
        self.folder_label.setText(str(folder))
        self.folder_label.setToolTip(str(folder))
        self._empty_plot()
        self._restore_state()
        self.refresh()

    def follow_current_folder(self, *_):
        if self._custom_folder is None:
            self.set_folder(self.folder_callable())

    def use_current_folder(self):
        self._custom_folder = None
        self.follow_button.setEnabled(False)
        self.follow_current_folder()

    def _browse(self):
        folder = QtWidgets.QFileDialog.getExistingDirectory(self, "Measurement history folder", str(self.folder))
        if folder:
            self._custom_folder = Path(folder)
            self.follow_button.setEnabled(True)
            self.set_folder(folder)

    def showEvent(self, event):
        super().showEvent(event)
        self.follow_current_folder()
        self.refresh()
        self.refresh_timer.start()

    def hideEvent(self, event):
        self.refresh_timer.stop()
        super().hideEvent(event)

    def refresh(self, *_):
        if self._closed:
            return
        if self._scan_active:
            self._scan_pending = True
            return
        self._scan_active = True
        self.refresh_button.setEnabled(False)
        self.refresh_button.setText("Reading…")
        self._submit("scan", self._folder_generation, discover_history, (self.folder, self._index_cache))

    def _submit(self, kind, token, function, args, signature=None):
        self._serial += 1
        number = self._serial
        task = _ReadTask(number, function, args)
        task.signals.done.connect(self._task_done)
        self._jobs[number] = (kind, token, task, signature)
        self.pool.start(task)

    @QtCore.Slot(int, object, str)
    def _task_done(self, number, result, error):
        job = self._jobs.pop(number, None)
        if job is None or self._closed:
            return
        kind, token, _task, signature = job
        if kind == "scan":
            self._scan_active = False
            self.refresh_button.setEnabled(True)
            self.refresh_button.setText("Refresh")
            if token == self._folder_generation:
                records, self._scan_warnings = result if not error else ([], [error])
                if records != self.model.records:
                    self.model.replace(records)
                self._update_dates(records)
            if self._scan_pending:
                self._scan_pending = False
                self.refresh()
        else:
            self._plot_active = False
            if token == (self._folder_generation, self._plot_revision):
                if signature[0] == "map":
                    primary, traces, warnings = result if not error else (None, [], [error])
                    self._draw_map(primary, traces, warnings, result if isinstance(result, MapComparison) else None)
                else:
                    traces, warnings = result if not error else ([], [error])
                    self._draw_traces(traces, warnings)
                actual_signature = self._plot_signature(self._selected_records())
                replaced_coordinate = signature[0] == "map" and not isinstance(result, MapComparison) and (
                    (signature[-1] is not None and signature[-1] != actual_signature[-1])
                    or (signature[-2] and signature[-2] != actual_signature[-2])
                )
                if replaced_coordinate:
                    # A new primary map lacks the old coordinate. Its control
                    # now explicitly selects a new measured value; recalculate
                    # rather than caching cuts from the obsolete coordinate.
                    self._last_plot_signature = None
                    self._desired_plot_signature = None
                    self._selection_changed()
                else:
                    self._last_plot_signature = actual_signature
                    self._desired_plot_signature = actual_signature
                    self.png_actions.apply_restored_limits()
                self._plot_warnings = warnings
            else:
                self._plot_pending = True
            if self._plot_pending:
                self._plot_pending = False
                self.plot_timer.start()
        self.png_actions.update_actions()

    def _update_dates(self, records):
        current = self.date_combo.currentText()
        dates = sorted({r.date for r in records}, reverse=True)
        choices = ["All dates", "Today"] + dates
        if current not in choices:
            choices.append(current)
        if choices != [self.date_combo.itemText(i) for i in range(self.date_combo.count())]:
            with QtCore.QSignalBlocker(self.date_combo):
                self.date_combo.clear()
                self.date_combo.addItems(choices)
                self.date_combo.setCurrentText(current)
        self._filters_changed(persist=False)

    def _filters_changed(self, *_, persist=True):
        self.proxy.date = self.date_combo.currentText()
        self.proxy.kind = self._current_kind
        self.proxy.search = self.search_edit.text().strip()
        state = (self.proxy.date, self.proxy.kind, self.proxy.search,
                 QtCore.QDate.currentDate().toString("yyyy-MM-dd") if self.proxy.date == "Today" else None)
        if state != self._filter_state:
            self._filter_state = state
            self.proxy.invalidateFilter()
        if hasattr(self, "plot_timer"):
            self._selection_changed(persist=persist)

    def _selected_records(self):
        return [self.model.records[self.proxy.mapToSource(self.proxy.index(row, 0)).row()]
                for row in range(self.proxy.rowCount())
                if str(self.model.records[self.proxy.mapToSource(self.proxy.index(row, 0)).row()].path) in self.model.checked]

    def _selection_changed(self, *_, persist=True):
        if self._closed:
            return
        selected = self._selected_records()
        if self._current_kind == "map_2d" and self.auto_axes_check.isChecked() and selected:
            primary = next((r for r in selected if str(r.path) == self.map_file_combo.currentData()), selected[0])
            axes = infer_map_axes(primary)
            if axes is not None:
                self._set_map_axes(axes)
        self._configure_axes()
        if self.map_ax is not None:
            canonical = lambda value: "E-field" if value == "Efield" else value
            valid_axes = {canonical(self.x_combo.currentText()), canonical(self.map_y_combo.currentText())}
            if self._fixed_axis() and canonical(self._fixed_axis()) not in valid_axes:
                with QtCore.QSignalBlocker(self.fixed_axis_combo), QtCore.QSignalBlocker(self.fixed_value_combo):
                    self.fixed_axis_combo.clear()
                    self.fixed_value_combo.clear()
        self.count_label.setText(f"{self.proxy.rowCount()} visible / {len(self.model.records)} measurements · {len(selected)} selected")
        signature = self._plot_signature(selected)
        if signature != self._desired_plot_signature:
            self._desired_plot_signature = signature
            self._plot_revision += 1
            self.plot_timer.start()
        if persist:
            self.save_state()
        self.png_actions.update_actions()

    def _clear_selection(self):
        self.model.checked.clear()
        if self.model.rowCount():
            self.model.dataChanged.emit(self.model.index(0, 0), self.model.index(self.model.rowCount() - 1, 0), [QtCore.Qt.ItemDataRole.CheckStateRole])
        self._selection_changed()

    def _start_plot(self):
        if self._closed:
            return
        if self._plot_active:
            self._plot_pending = True
            return
        records = self._selected_records()
        x, signal, direction = self.x_combo.currentText(), self.signal_combo.currentText(), self.direction_combo.currentText()
        signature = self._plot_signature(records)
        if signature == self._last_plot_signature:
            self._show_warnings(self._scan_warnings + self._plot_warnings)
            return
        if not records:
            self._empty_plot()
            self._last_plot_signature = signature
            self._plot_warnings = []
            self.status_label.setText("No matching history. Try All dates or Browse folder." if not self.proxy.rowCount() else "Check measurements in the file list to overlay curves")
            self._show_warnings(self._scan_warnings)
            return
        self.status_label.setText(f"Loading {len(records)} selected measurements…")
        self._plot_active = True
        normalization = self.normalization_combo.currentData()
        if signature[0] == "map":
            args = (records, x, self.map_y_combo.currentText(), signal, direction, self.pass_combo.currentText(),
                    self.map_file_combo.currentData() or "", self._fixed_axis(), self.fixed_value_combo.currentData(), self._snapshot_cache, self.auto_axes_check.isChecked(), True)
            self._submit("plot", (self._folder_generation, self._plot_revision), read_normalized_comparison,
                         (map_comparison, args, normalization, True), signature)
        else:
            args = (records, x, signal, direction, self._snapshot_cache)
            self._submit("plot", (self._folder_generation, self._plot_revision), read_normalized_comparison,
                         (comparison_traces, args, normalization), signature)

    def _plot_signature(self, records):
        base = (tuple((str(r.path), r.fingerprint, r.conditions, comparison_setting_signature(r.metadata)) for r in records), self.x_combo.currentText(), self.signal_combo.currentText(), self.direction_combo.currentText(), self.normalization_combo.currentData())
        if self.map_ax is not None:
            return ("map",) + base + (self.auto_axes_check.isChecked(), self.map_y_combo.currentText(), self.pass_combo.currentText(), self.map_file_combo.currentData(), self._fixed_axis(), self.fixed_value_combo.currentData())
        return ("curve",) + base

    def _empty_plot(self, draw=True):
        self.map_ranges.detach()
        self._displayed_traces = []
        self._displayed_primary = None
        self._displayed_records = []
        self._plot_heading = ("", "")
        self._update_plot_heading()
        if self._colorbar is not None:
            self._colorbar.remove()
            self._colorbar = None
        if self.map_ax is not None:
            self.map_ax.clear()
            self.map_ax.set_title("Select a historical 2D map", loc="left", fontsize=11)
        self.ax.clear()
        self.ax.grid(True)
        self.ax.set_xlabel(self.x_combo.currentText())
        self.ax.set_ylabel(comparison_y_label(self.signal_combo.currentText(), "", self.normalization_combo.currentData()))
        if draw:
            self.canvas.draw_idle()

    def _draw_map(self, primary, traces, warnings, cut=None):
        draft = self.fixed_value_combo.currentText() if self._cut_edit_pending else None
        preserve_map = primary is not None and primary is self._displayed_primary and self._colorbar is not None
        self._draw_traces(traces, warnings, preserve_map=preserve_map)
        if primary is None or self.map_ax is None:
            self.canvas.draw_idle()
            return
        if self.auto_axes_check.isChecked():
            self._set_map_axes((primary.x_name, primary.y_name))
        pass_label = "all rows" if primary.pass_index is None else f"row/pass {primary.pass_index:g}"
        self.map_ax.set_title(f"{primary.signal} map", loc="left", fontsize=11, y=1., pad=22)
        if not preserve_map:
            image = self.map_ax.pcolormesh(primary.x, primary.y, primary.z, shading="auto", cmap=self.colormap_combo.currentText())
            self.map_ax.set_xlabel(primary.x_name + (f" ({primary.x_unit})" if primary.x_unit else ""))
            self.map_ax.set_ylabel(primary.y_name + (f" ({primary.y_unit})" if primary.y_unit else ""))
            style_heatmap_axes(self.map_ax)
            self._colorbar = self.figure.colorbar(image, ax=self.map_ax)
            self._colorbar.set_label(primary.signal + (f" ({primary.signal_unit})" if primary.signal_unit else ""))
        records = self._selected_records()
        with QtCore.QSignalBlocker(self.map_file_combo):
            if [self.map_file_combo.itemData(i) for i in range(self.map_file_combo.count())] != [str(record.path) for record in records]:
                self.map_file_combo.clear()
                for record in records:
                    self.map_file_combo.addItem(record.path.stem, str(record.path))
                    self.map_file_combo.setItemData(self.map_file_combo.count() - 1, str(record.path), QtCore.Qt.ItemDataRole.ToolTipRole)
            self.map_file_combo.setCurrentIndex(max(0, self.map_file_combo.findData(str(primary.path))))
        self.map_file_combo.setToolTip(str(primary.path))
        with QtCore.QSignalBlocker(self.pass_combo):
            current = self.pass_combo.currentText()
            choices = ["All rows"] + [f"{p:g}" for p in primary.passes]
            if [self.pass_combo.itemText(i) for i in range(self.pass_combo.count())] != choices:
                self.pass_combo.clear()
                self.pass_combo.addItems(choices)
            if self.pass_combo.findText(current) >= 0:
                self.pass_combo.setCurrentText(current)
        with QtCore.QSignalBlocker(self.fixed_axis_combo):
            current = cut.fixed_axis if cut is not None else self._fixed_axis()
            choices = [(f"Fix Y ({primary.y_name})", primary.y_name),
                       (f"Fix X ({primary.x_name})", primary.x_name)]
            if [(self.fixed_axis_combo.itemText(i), self.fixed_axis_combo.itemData(i)) for i in range(self.fixed_axis_combo.count())] != choices:
                self.fixed_axis_combo.clear()
                for label, axis in choices:
                    self.fixed_axis_combo.addItem(label, axis)
            self.fixed_axis_combo.setCurrentIndex(max(0, self.fixed_axis_combo.findData(current)))
        fixed_y = self._fixed_axis() == primary.y_name
        coords = primary.y if fixed_y else primary.x
        with QtCore.QSignalBlocker(self.fixed_value_combo):
            value = cut.fixed_value if cut is not None else self.fixed_value_combo.currentData()
            if self._cut_coordinates is not coords or self.fixed_value_combo.count() != len(coords):
                self.fixed_value_combo.clear()
                for coord in coords:
                    self.fixed_value_combo.addItem(f"{coord:.12g}", float(coord))
                self._cut_coordinates = coords
            if value is not None and self.fixed_value_combo.findData(value) >= 0:
                self.fixed_value_combo.setCurrentIndex(self.fixed_value_combo.findData(value))
            if draft is not None:
                self.fixed_value_combo.setEditText(draft)
        value = self.fixed_value_combo.currentData()
        self._cut_edit_pending = draft is not None
        varying_name = primary.x_name if fixed_y else primary.y_name
        unit = primary.x_unit if fixed_y else primary.y_unit
        self.ax.set_xlabel(varying_name + (f" ({unit})" if unit else ""))
        fixed_unit = primary.y_unit if fixed_y else primary.x_unit
        self.cut_unit_label.setText(fixed_unit)
        actual = f"{self._fixed_axis()} = {value:g}" + (f" {fixed_unit}" if fixed_unit else "") if value is not None else ""
        if cut is not None and cut.requested_value != value:
            self.cut_value_label.setText(f"Input {cut.requested_value:g} → measured {actual}")
        else:
            self.cut_value_label.setText(f"Measured {actual}")
        title = f"Line cut · {actual}" if actual else "Line cuts"
        self.ax.set_title(title, loc="left", fontsize=11, y=1., pad=22)
        self._displayed_primary = primary
        if not preserve_map:
            self.map_ranges.attach(primary)
        self._set_plot_heading(primary)
        self.status_label.setText(f"Map: {primary.path.name} · {pass_label} · {len(traces)} measured cuts. Missing coordinates remain blank.")
        self.canvas.draw_idle()
        if not preserve_map:
            self.toolbar.update()
        self.save_state()

    def _draw_traces(self, traces, warnings, preserve_map=False):
        if preserve_map:
            self.ax.clear()
            self.ax.grid(True)
        else:
            self._empty_plot(draw=False)
        self._displayed_traces = traces
        self._displayed_records = self._selected_records()
        colors = {}
        saved = {str(trace.path): trace.saved_conditions for trace in traces}
        records = [{"path": str(r.path), "metadata": {**r.metadata, "csv_conditions": saved[str(r.path)]}}
                   for r in self._displayed_records if str(r.path) in saved]
        trace_info = [{"path": str(trace.path), "direction": trace.direction, "condition": trace.condition} for trace in traces]
        excluded = (self.x_combo.currentText(), self.map_y_combo.currentText()) if self.map_ax is not None else (self.x_combo.currentText(),)
        labels = comparison_labels(records, trace_info, excluded)
        for trace, label in zip(traces, labels):
            color = colors.setdefault(trace.path, f"C{len(colors) % 10}")
            label = textwrap.fill(label, 32 if self.map_ax is not None else 65)
            self.ax.plot(trace.x, trace.y, label=label, color=color, linestyle="--" if trace.direction == "backward" else "-", marker=".", markersize=4)
        if traces:
            self.ax.set_xlabel(self.x_combo.currentText() + (f" ({traces[0].x_unit})" if traces[0].x_unit else ""))
            self.ax.set_ylabel(comparison_y_label(self.signal_combo.currentText(), traces[0].y_unit, self.normalization_combo.currentData()))
        if self.map_ax is None:
            self._set_plot_heading()
        self._update_legend()
        if self.map_ax is None:
            self.toolbar.update()
            self.canvas.draw_idle()
            self.save_state()
        self.status_label.setText(f"{len({t.path for t in traces})} measurements · {len(traces)} direction traces. Solid: forward / unknown; dashed: backward.")
        self._show_warnings(self._scan_warnings + warnings)

    def _show_warnings(self, warnings):
        self.warning_label.setVisible(bool(warnings))
        self.warning_label.setText("\n".join(warnings[:3]) + (f"\n… {len(warnings) - 3} more (hover for details)" if len(warnings) > 3 else ""))
        self.warning_label.setToolTip("\n".join(warnings))

    def _update_legend(self):
        legend = self.ax.get_legend()
        if legend:
            legend.remove()
        if self.legend_check.isChecked() and self.ax.lines:
            handles, labels = self.ax.get_legend_handles_labels()
            unique = dict(zip(labels, handles))
            self.ax.legend(unique.values(), unique.keys(), fontsize=8, loc="best")

    def _legend_changed(self, *_):
        self._update_legend()
        self.canvas.draw_idle()
        self.save_state()

    def shutdown(self):
        self.png_actions.shutdown()
        self.save_state()
        self._closed = True
        self.refresh_timer.stop()
        self.plot_timer.stop()
        self.pool.clear()

    def export_to(self, path=None, cut_only=False, *, heatmap_only=False):
        self.png_actions.export_to(path, cut_only, heatmap_only=heatmap_only)

    def capture_session(self):
        return capture_session(self)

    def restore_session(self, state):
        restore_session(self, state)

    def restore_export_view(self, path):
        self.png_actions.restore_export_view(path)
