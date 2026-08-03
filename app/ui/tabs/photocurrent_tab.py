from __future__ import annotations

import json
import os

from PyQt6 import QtCore, QtWidgets

from app.constants import GATE_BIAS_RAMP_STEP_T, GATE_BIAS_RAMP_STEP_V
from app.device_manager import DeviceManager
from app.models import Connections, PhotocurrentBiasCondition, PhotocurrentParams, SaveRoot
from app.result_channels import compare_channel_options, plot_channel_options, plot_channel_value
from app.run_output import build_planned_output, planned_output_warning
from app.ui.helpers import apply_tooltip, configure_volt_spinbox, flash_button_success, set_standard_input_height, style_form_layout
from app.ui.tabs.base_tab import BaseMeasurementTab
from app.ui.widgets.collapsible_section import CollapsibleSection
from app.ui.widgets.safe_spinbox import SafeDoubleSpinBox, SafeSpinBox
from app.ui.widgets.status_panel import SectionHeader, StatusPanel
from app.workers.photocurrent import PhotocurrentWorker

SET_BUTTON_WIDTH = 48


class PhotocurrentTab(BaseMeasurementTab):
    SETTINGS_PREFIX = "tabs/photocurrent"

    def __init__(self, save: SaveRoot, conns: Connections, device_manager: DeviceManager, get_global_rates_callable=None, get_ao_items_callable=None):
        self.save = save
        self.conns = conns
        self.device_manager = device_manager
        self.get_global_rates = get_global_rates_callable or (lambda: (1e7, 100.0))
        self.get_ao_items = get_ao_items_callable or (lambda: ["ao0", "ao1"])
        self.p = PhotocurrentParams()
        self.s_g1 = self.s_g2 = self.s_g3 = self.s_daq = self.s_mono = None
        self.worker_thread = None
        self.worker = None
        self._plot_records = []
        self._output_run_id = None
        self._planned_output = None
        self._loading_tab_settings = False
        super().__init__("START PHOTOCURRENT", "Wavelength (nm)", "Ids (A)", ["g1", "g2", "g3", "daq", "mono"])
        self._wire()
        self.btn_start.setToolTip("Connect instruments first")
        self.device_manager.status_changed.connect(self._on_device_status_changed)
        self.device_manager.operation_changed.connect(self._on_operation_changed)
        self._sync_sessions_from_manager()
        self._load_tab_settings()
        self._bind_tab_settings()
        self._update_manual_buttons()

    def _make_set_row(self, spinbox, button):
        wrap = QtWidgets.QWidget()
        row = QtWidgets.QHBoxLayout(wrap)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(6)
        row.addWidget(spinbox, 1)
        row.addWidget(button)
        return wrap

    def _build_control_panel(self, ctl_layout: QtWidgets.QVBoxLayout):
        ctl_layout.addWidget(SectionHeader("Sweep Parameters"))
        grp_wl = QtWidgets.QGroupBox("Wavelength Scan")
        form_wl = QtWidgets.QFormLayout(grp_wl)
        style_form_layout(form_wl)
        self.sp_wls = SafeDoubleSpinBox()
        self.sp_wls.setRange(200, 2000)
        self.sp_wls.setValue(550)
        self.sp_wle = SafeDoubleSpinBox()
        self.sp_wle.setRange(200, 2000)
        self.sp_wle.setValue(740)
        self.sp_wld = SafeDoubleSpinBox()
        self.sp_wld.setRange(0.001, 100)
        self.sp_wld.setValue(0.5)
        self.btn_go_wl = QtWidgets.QPushButton("Go")
        self.btn_go_wl.setFixedWidth(SET_BUTTON_WIDTH)
        lbl_wls = QtWidgets.QLabel("Start (nm):")
        lbl_wle = QtWidgets.QLabel("Stop (nm):")
        lbl_wld = QtWidgets.QLabel("Step (nm):")
        form_wl.addRow(lbl_wls, self.sp_wls)
        form_wl.addRow(lbl_wle, self.sp_wle)
        form_wl.addRow(lbl_wld, self._make_set_row(self.sp_wld, self.btn_go_wl))
        ctl_layout.addWidget(grp_wl)

        ctl_layout.addWidget(SectionHeader("Bias"))
        grp_vds = QtWidgets.QGroupBox("Vds Bias")
        form_vds = QtWidgets.QFormLayout(grp_vds)
        style_form_layout(form_vds)
        self.chk_use_vds = QtWidgets.QCheckBox("Apply Vds From Recipe")
        self.sp_vds = SafeDoubleSpinBox()
        configure_volt_spinbox(self.sp_vds, 0.0)
        self.btn_set_vds = QtWidgets.QPushButton("Set")
        self.btn_set_vds.setFixedWidth(SET_BUTTON_WIDTH)
        self.sp_vds_ramp = SafeDoubleSpinBox()
        self.sp_vds_ramp.setDecimals(3)
        self.sp_vds_ramp.setRange(1e-3, 5.0)
        self.sp_vds_ramp.setValue(0.01)
        form_vds.addRow(self.chk_use_vds)
        lbl_vds = QtWidgets.QLabel("Manual Set (V):")
        lbl_vds_ramp = QtWidgets.QLabel("Vds Step (V):")
        form_vds.addRow(lbl_vds, self._make_set_row(self.sp_vds, self.btn_set_vds))
        form_vds.addRow(lbl_vds_ramp, self.sp_vds_ramp)
        self.lbl_vds_availability = QtWidgets.QLabel()
        self.lbl_vds_availability.setWordWrap(True)
        self.lbl_vds_availability.setProperty("role", "hint")
        form_vds.addRow("", self.lbl_vds_availability)
        self.exp_vds = CollapsibleSection("Optional Vds Bias", grp_vds, expanded=False)
        ctl_layout.addWidget(self.exp_vds)

        grp_recipe = QtWidgets.QGroupBox("Bias Recipe")
        recipe_layout = QtWidgets.QVBoxLayout(grp_recipe)
        recipe_layout.setContentsMargins(10, 18, 10, 10)
        recipe_layout.setSpacing(6)
        self.tbl_bias_conditions = QtWidgets.QTableWidget(0, 5)
        self.tbl_bias_conditions.setHorizontalHeaderLabels(["", "Vtg (V)", "Vbg (V)", "Vds (V)", "Settle (s)"])
        self.tbl_bias_conditions.verticalHeader().setVisible(False)
        self.tbl_bias_conditions.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectionBehavior.SelectRows)
        self.tbl_bias_conditions.setSelectionMode(QtWidgets.QAbstractItemView.SelectionMode.ExtendedSelection)
        self.tbl_bias_conditions.setMinimumHeight(138)
        self.tbl_bias_conditions.setMaximumHeight(220)
        recipe_header = self.tbl_bias_conditions.horizontalHeader()
        recipe_header.setSectionResizeMode(0, QtWidgets.QHeaderView.ResizeMode.ResizeToContents)
        for column in range(1, self.tbl_bias_conditions.columnCount()):
            recipe_header.setSectionResizeMode(column, QtWidgets.QHeaderView.ResizeMode.Stretch)
        self.tbl_bias_conditions.setColumnWidth(0, 30)
        recipe_layout.addWidget(self.tbl_bias_conditions)
        row_recipe_buttons = QtWidgets.QHBoxLayout()
        row_recipe_buttons.setContentsMargins(0, 0, 0, 0)
        row_recipe_buttons.setSpacing(4)
        self.btn_condition_add = QtWidgets.QPushButton("Add")
        self.btn_condition_duplicate = QtWidgets.QPushButton("Duplicate")
        self.btn_condition_remove = QtWidgets.QPushButton("Remove")
        self.btn_condition_paste = QtWidgets.QPushButton("Paste")
        for button in (self.btn_condition_add, self.btn_condition_duplicate, self.btn_condition_remove, self.btn_condition_paste):
            row_recipe_buttons.addWidget(button)
        recipe_layout.addLayout(row_recipe_buttons)
        self.lbl_condition_summary = QtWidgets.QLabel()
        self.lbl_condition_summary.setWordWrap(True)
        self.lbl_condition_summary.setProperty("role", "hint")
        recipe_layout.addWidget(self.lbl_condition_summary)
        ctl_layout.addWidget(grp_recipe)
        self._append_bias_condition(PhotocurrentBiasCondition())

        ctl_layout.addWidget(SectionHeader("Acquisition"))
        grp_time = QtWidgets.QGroupBox("Timing")
        form_time = QtWidgets.QFormLayout(grp_time)
        style_form_layout(form_time)
        self.sp_delay = SafeDoubleSpinBox()
        self.sp_delay.setDecimals(3)
        self.sp_delay.setRange(0.0, 30.0)
        self.sp_delay.setValue(0.01)
        self.sp_nsamp = SafeSpinBox()
        self.sp_nsamp.setRange(1, 1000)
        self.sp_nsamp.setValue(1)
        lbl_delay = QtWidgets.QLabel("Delay (s):")
        lbl_avg = QtWidgets.QLabel("Averages:")
        form_time.addRow(lbl_delay, self.sp_delay)
        form_time.addRow(lbl_avg, self.sp_nsamp)
        ctl_layout.addWidget(grp_time)

        ctl_layout.addWidget(SectionHeader("Output"))
        grp_output = QtWidgets.QGroupBox("Output Settings")
        form_output = QtWidgets.QFormLayout(grp_output)
        style_form_layout(form_output)
        self.ed_base = QtWidgets.QLineEdit(self.p.base_name)
        self.cbo_source = QtWidgets.QComboBox()
        self.cbo_source.addItems(["None", "Keithley 2400"])
        self.cbo_y = QtWidgets.QComboBox()
        self.cbo_y.addItems(["Ids_DC", "Ids_X", "Ids_Y"])
        lbl_base = QtWidgets.QLabel("Filename Stem:")
        lbl_source = QtWidgets.QLabel("Vds Source:")
        lbl_y = QtWidgets.QLabel("Plot Axis:")
        form_output.addRow(lbl_base, self.ed_base)
        form_output.addRow(lbl_source, self.cbo_source)
        form_output.addRow(lbl_y, self.cbo_y)
        self.exp_output = CollapsibleSection("Output and Plot Options", grp_output, expanded=True)
        ctl_layout.addWidget(self.exp_output)
        self._add_output_preview_section(ctl_layout)

        self.lbl_connection_hint = QtWidgets.QLabel()
        self.lbl_connection_hint.setWordWrap(True)
        self.lbl_connection_hint.setProperty("role", "hint")
        ctl_layout.addWidget(self.lbl_connection_hint)

        ctl_layout.addWidget(SectionHeader("Status"))
        self.status_panel = StatusPanel(["g1", "g2", "g3", "daq", "mono"])
        self.lbl_g1 = self.status_panel.label("g1")
        self.lbl_g2 = self.status_panel.label("g2")
        self.lbl_g3 = self.status_panel.label("g3")
        self.lbl_daq = self.status_panel.label("daq")
        self.lbl_mono = self.status_panel.label("mono")
        ctl_layout.addWidget(self.status_panel)

        for widget in [
            self.ed_base, self.cbo_source, self.cbo_y, self.sp_wls, self.sp_wle, self.sp_wld,
            self.sp_vds, self.sp_vds_ramp,
            self.sp_delay, self.sp_nsamp,
        ]:
            set_standard_input_height(widget)

        apply_tooltip("First wavelength in the scan.", lbl_wls, self.sp_wls)
        apply_tooltip("Last wavelength included in the scan.", lbl_wle, self.sp_wle)
        apply_tooltip("Wavelength spacing between points. The Go button moves the mono immediately.", lbl_wld, self.sp_wld, self.btn_go_wl)
        apply_tooltip("Apply the Vds value from each enabled bias-recipe row. This stays off unless explicitly selected.", self.chk_use_vds)
        apply_tooltip("Move Vds manually without starting a measurement.", lbl_vds, self.sp_vds, self.btn_set_vds)
        apply_tooltip("Voltage step size used when changing the Vds bias.", lbl_vds_ramp, self.sp_vds_ramp)
        apply_tooltip("Each enabled row is one complete photocurrent spectrum. Unchecked rows are kept but skipped.", self.tbl_bias_conditions)
        apply_tooltip("Paste spreadsheet columns in the order Vtg, Vbg, Vds, Settle (s).", self.btn_condition_paste)
        apply_tooltip("Wait time after each wavelength move before measuring.", lbl_delay, self.sp_delay)
        apply_tooltip("Number of DAQ reads averaged per wavelength point.", lbl_avg, self.sp_nsamp)
        apply_tooltip("Base filename for the saved spectrum.", lbl_base, self.ed_base)
        apply_tooltip("Select the instrument used when Vds bias is enabled.", lbl_source, self.cbo_source)
        apply_tooltip("Choose which current channel is shown in the live plot.", lbl_y, self.cbo_y)

    def _wire(self):
        self.btn_start.clicked.connect(self.start_run)
        self.btn_stop.clicked.connect(self.stop_run)
        self.btn_set_vds.clicked.connect(self.on_set_vds)
        self.btn_go_wl.clicked.connect(self.on_go_wl)
        self.btn_condition_add.clicked.connect(lambda: self._append_bias_condition(PhotocurrentBiasCondition()))
        self.btn_condition_duplicate.clicked.connect(self._duplicate_selected_bias_conditions)
        self.btn_condition_remove.clicked.connect(self._remove_selected_bias_conditions)
        self.btn_condition_paste.clicked.connect(self._paste_bias_conditions)
        self.tbl_bias_conditions.itemChanged.connect(self._on_bias_conditions_changed)
        self.chk_use_vds.toggled.connect(self._update_connection_hint)
        self.chk_use_vds.toggled.connect(self._update_manual_buttons)
        self.chk_use_vds.toggled.connect(self._update_plot_axis_choices)
        self.chk_use_vds.toggled.connect(self._update_vds_bias_state)
        self.chk_use_vds.toggled.connect(self._refresh_condition_preview)
        self.cbo_source.currentIndexChanged.connect(self._update_connection_hint)
        self.cbo_source.currentIndexChanged.connect(self._update_manual_buttons)
        self.cbo_source.currentIndexChanged.connect(self._update_plot_axis_choices)
        self.cbo_source.currentIndexChanged.connect(self._refresh_condition_preview)
        self.cbo_y.currentTextChanged.connect(self.set_plot_axis_source)
        self._update_plot_axis_choices()
        self.plot.y_axis_changed.connect(self.set_plot_axis_source)
        self.plot.plot_mode_changed.connect(lambda _mode: self._redraw_plot())
        self._update_vds_bias_state()
        self.ed_base.textChanged.connect(self.refresh_output_preview)
        self.cbo_source.currentIndexChanged.connect(self.refresh_output_preview)
        self.chk_use_vds.toggled.connect(self.refresh_output_preview)
        for widget in (
            self.sp_wls,
            self.sp_wle,
            self.sp_vds,
        ):
            widget.valueChanged.connect(self.refresh_output_preview)
        self.refresh_output_preview()
        self._refresh_condition_preview()

    def _append_bias_condition(self, condition: PhotocurrentBiasCondition):
        table = self.tbl_bias_conditions
        table.blockSignals(True)
        row = table.rowCount()
        table.insertRow(row)
        include = QtWidgets.QTableWidgetItem()
        include.setFlags(
            QtCore.Qt.ItemFlag.ItemIsEnabled
            | QtCore.Qt.ItemFlag.ItemIsSelectable
            | QtCore.Qt.ItemFlag.ItemIsUserCheckable
        )
        include.setCheckState(QtCore.Qt.CheckState.Checked if condition.enabled else QtCore.Qt.CheckState.Unchecked)
        table.setItem(row, 0, include)
        for column, value in enumerate((condition.vtg, condition.vbg, condition.vds, condition.settle_s), start=1):
            item = QtWidgets.QTableWidgetItem(f"{value:g}")
            item.setTextAlignment(QtCore.Qt.AlignmentFlag.AlignRight | QtCore.Qt.AlignmentFlag.AlignVCenter)
            table.setItem(row, column, item)
        table.blockSignals(False)
        self._on_bias_conditions_changed()

    def _selected_condition_rows(self) -> list[int]:
        rows = sorted({index.row() for index in self.tbl_bias_conditions.selectionModel().selectedRows()})
        if not rows and self.tbl_bias_conditions.currentRow() >= 0:
            rows = [self.tbl_bias_conditions.currentRow()]
        return rows

    def _duplicate_selected_bias_conditions(self):
        rows = self._selected_condition_rows()
        if not rows:
            return
        try:
            conditions = self._bias_conditions(rows=rows, strict=True)
        except ValueError as ex:
            QtWidgets.QMessageBox.warning(self, "Duplicate Bias Condition", str(ex))
            return
        for condition in conditions:
            self._append_bias_condition(condition)

    def _remove_selected_bias_conditions(self):
        rows = self._selected_condition_rows()
        if not rows:
            return
        for row in reversed(rows):
            self.tbl_bias_conditions.removeRow(row)
        if self.tbl_bias_conditions.rowCount() == 0:
            self._append_bias_condition(PhotocurrentBiasCondition())
        else:
            self._on_bias_conditions_changed()

    def _paste_bias_conditions(self):
        text = QtWidgets.QApplication.clipboard().text().strip()
        if not text:
            return
        pasted: list[PhotocurrentBiasCondition] = []
        for line_number, line in enumerate(text.splitlines(), start=1):
            values = [part.strip() for part in line.replace(",", "\t").split("\t")]
            if len(values) < 2 or not values[0] or not values[1]:
                continue
            try:
                pasted.append(
                    PhotocurrentBiasCondition(
                        vtg=float(values[0]),
                        vbg=float(values[1]),
                        vds=float(values[2]) if len(values) >= 3 and values[2] else 0.0,
                        settle_s=float(values[3]) if len(values) >= 4 and values[3] else 2.0,
                    )
                )
            except ValueError:
                if line_number == 1:
                    continue
                QtWidgets.QMessageBox.warning(
                    self,
                    "Paste Bias Recipe",
                    "Paste rows as Vtg, Vbg, optional Vds, optional settle time. "
                    f"Could not read row {line_number}.",
                )
                return
        if not pasted:
            QtWidgets.QMessageBox.warning(self, "Paste Bias Recipe", "No valid bias-condition rows were found.")
            return
        for condition in pasted:
            self._append_bias_condition(condition)

    def _bias_conditions(self, rows: list[int] | None = None, strict: bool = False) -> list[PhotocurrentBiasCondition]:
        table = self.tbl_bias_conditions
        rows = list(range(table.rowCount())) if rows is None else rows
        conditions: list[PhotocurrentBiasCondition] = []
        errors: list[str] = []
        for row in rows:
            include = table.item(row, 0)
            enabled = include is not None and include.checkState() == QtCore.Qt.CheckState.Checked
            try:
                values = []
                for column, label in ((1, "Vtg"), (2, "Vbg"), (3, "Vds"), (4, "settle time")):
                    item = table.item(row, column)
                    values.append(float(item.text()) if item is not None else 0.0)
                if values[3] < 0:
                    raise ValueError("settle time must be zero or greater")
                conditions.append(
                    PhotocurrentBiasCondition(
                        enabled=enabled,
                        vtg=values[0],
                        vbg=values[1],
                        vds=values[2],
                        settle_s=values[3],
                    )
                )
            except ValueError as ex:
                if enabled:
                    errors.append(f"row {row + 1}: {ex}")
        if strict and errors:
            raise ValueError("Invalid bias recipe: " + "; ".join(errors))
        return conditions

    def _active_bias_conditions(self, strict: bool = False) -> list[PhotocurrentBiasCondition]:
        return [condition for condition in self._bias_conditions(strict=strict) if condition.enabled]

    def _on_bias_conditions_changed(self, *_args):
        if not hasattr(self, "cbo_source"):
            return
        self._refresh_condition_preview()
        self.refresh_output_preview()
        if not getattr(self, "_loading_tab_settings", False):
            self._save_bias_conditions_settings()

    def _vds_sources(self) -> list[str]:
        sources: list[str] = []
        if self.device_manager.is_connected("g3") and self.device_manager.is_voltage_source_mode("g3"):
            sources.append("Keithley 2400")
        if self.device_manager.is_connected("daq"):
            sources.extend(f"NI DAQ {item}" for item in self.get_ao_items())
        return sources

    def _vds_is_available(self) -> bool:
        return self.cbo_source.currentText() in self._vds_sources()

    def _refresh_condition_preview(self, *_args):
        try:
            conditions = self._active_bias_conditions(strict=True)
            issue = ""
        except ValueError as ex:
            conditions = []
            issue = str(ex)
        use_vds = self.chk_use_vds.isChecked() and self._vds_is_available()
        self.tbl_bias_conditions.setColumnHidden(3, False)
        if issue:
            text = issue
            role = "warning-hint"
        elif not conditions:
            text = "Select at least one condition to run."
            role = "warning-hint"
        elif use_vds:
            text = (
                f"{len(conditions)} condition(s) selected; each saves to a separate CSV. "
                f"Vds source: {self.cbo_source.currentText()}."
            )
            role = "hint"
        else:
            text = f"{len(conditions)} condition(s) selected; each saves to a separate CSV. Vds: Off."
            role = "hint"
        self.lbl_condition_summary.setText(text)
        self.lbl_condition_summary.setProperty("role", role)
        self.lbl_condition_summary.style().unpolish(self.lbl_condition_summary)
        self.lbl_condition_summary.style().polish(self.lbl_condition_summary)

    def _output_summary_parts(self) -> list[str]:
        return [
            f"wl_{self.sp_wls.value():g}to{self.sp_wle.value():g}nm",
        ]

    def refresh_output_preview(self, *_args):
        planned = build_planned_output(
            self.save,
            "photocurrent",
            self.ed_base.text(),
            self._output_summary_parts(),
            run_id=self._output_run_id,
            filename_measurement_type="PC",
        )
        self._output_run_id = planned.run_id
        self._planned_output = planned
        self.set_output_preview_text(planned, planned_output_warning(planned, self.save))
        condition_paths = self._condition_csv_paths()
        if condition_paths:
            condition_names = [os.path.basename(path) for path in condition_paths]
            if len(condition_names) == 1:
                preview_text = condition_names[0]
            else:
                preview_lines = [f"{len(condition_names)} CSV files:"]
                preview_lines.extend(condition_names[:3])
                if len(condition_names) > 3:
                    preview_lines.append(f"... {len(condition_names) - 3} more")
                preview_text = "\n".join(preview_lines)
            self.lbl_filename_preview.setPlainText(preview_text)
            self.lbl_filename_preview.setToolTip("\n".join(condition_paths))

    def _settings_widgets(self):
        return [
            ("base_name", self.ed_base),
            ("source", self.cbo_source),
            ("plot_y", self.cbo_y),
            ("wl_start", self.sp_wls),
            ("wl_stop", self.sp_wle),
            ("wl_step", self.sp_wld),
            ("use_vds", self.chk_use_vds),
            ("manual_vds", self.sp_vds),
            ("vds_ramp", self.sp_vds_ramp),
            ("delay", self.sp_delay),
            ("averages", self.sp_nsamp),
        ]

    def _load_tab_settings(self):
        self._loading_tab_settings = True
        try:
            self._load_tab_widget_settings(self.SETTINGS_PREFIX, self._settings_widgets())
            self._load_bias_conditions_settings()
        finally:
            self._loading_tab_settings = False
        self._update_vds_bias_state()
        self._update_plot_axis_choices()
        self.set_plot_axis_source(self.cbo_y.currentText())
        self._refresh_condition_preview()
        self.refresh_output_preview()

    def _bind_tab_settings(self):
        self._bind_tab_widget_settings(self.SETTINGS_PREFIX, self._settings_widgets())

    def save_tab_settings(self):
        self._save_tab_widget_settings(self.SETTINGS_PREFIX, self._settings_widgets())
        self._save_bias_conditions_settings()

    def _save_bias_conditions_settings(self):
        try:
            conditions = self._bias_conditions(strict=False)
        except Exception:
            return
        from app.settings import get_app_settings

        payload = [
            {
                "enabled": condition.enabled,
                "vtg": condition.vtg,
                "vbg": condition.vbg,
                "vds": condition.vds,
                "settle_s": condition.settle_s,
            }
            for condition in conditions
        ]
        settings = get_app_settings()
        settings.setValue(f"{self.SETTINGS_PREFIX}/bias_conditions", json.dumps(payload))
        settings.sync()

    def _load_bias_conditions_settings(self):
        from app.settings import get_app_settings

        raw = get_app_settings().value(f"{self.SETTINGS_PREFIX}/bias_conditions", "")
        if not raw:
            return
        try:
            rows = json.loads(str(raw))
        except (TypeError, ValueError):
            return
        if not isinstance(rows, list):
            return
        conditions: list[PhotocurrentBiasCondition] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            try:
                conditions.append(
                    PhotocurrentBiasCondition(
                        enabled=bool(row.get("enabled", True)),
                        vtg=float(row.get("vtg", 0.0)),
                        vbg=float(row.get("vbg", 0.0)),
                        vds=float(row.get("vds", 0.0)),
                        settle_s=float(row.get("settle_s", 2.0)),
                    )
                )
            except (TypeError, ValueError):
                continue
        if not conditions:
            return
        self.tbl_bias_conditions.setRowCount(0)
        for condition in conditions:
            self._append_bias_condition(condition)

    def _condition_csv_paths(self) -> list[str]:
        if self._planned_output is None:
            return []
        try:
            conditions = self._active_bias_conditions(strict=True)
        except ValueError:
            return []
        return PhotocurrentWorker.condition_csv_paths(
            self._planned_output.csv_path,
            conditions,
            self.chk_use_vds.isChecked() and self._vds_is_available(),
        )

    def _validate_condition_output_paths(self) -> bool:
        existing = [path for path in self._condition_csv_paths() if os.path.exists(path)]
        if not existing:
            return True
        QtWidgets.QMessageBox.warning(
            self,
            "Output File Exists",
            "Change the filename stem or reset the preview before starting. Existing condition CSV file(s):\n"
            + "\n".join(os.path.basename(path) for path in existing),
        )
        return False

    def _update_manual_buttons(self):
        self._sync_sessions_from_manager()
        manual_available = not self.device_manager.is_busy() and not self.device_manager.current_in_use()
        self.btn_set_vds.setEnabled(
            self.chk_use_vds.isChecked()
            and manual_available
            and (
                ("NI DAQ" in self.cbo_source.currentText() and self.s_daq is not None)
                or (self.s_g3 is not None and self.device_manager.is_voltage_source_mode("g3"))
            )
        )
        self.btn_go_wl.setEnabled(manual_available and self.s_mono is not None)
        source_ready = (
            not self.chk_use_vds.isChecked()
            or self._vds_is_available()
        )
        self.btn_start.setEnabled(self.s_mono is not None and self.s_daq is not None and source_ready and self.worker_thread is None)
        self._update_connection_hint()
        self._update_vds_bias_state()

    def _sync_sessions_from_manager(self):
        self.s_g1 = self.device_manager.get_session("g1")
        self.s_g2 = self.device_manager.get_session("g2")
        self.s_g3 = self.device_manager.get_session("g3")
        self.s_daq = self.device_manager.get_session("daq")
        self.s_mono = self.device_manager.get_session("mono")
        for name in ("g1", "g2", "g3", "daq", "mono"):
            self.set_device_status(name, self.device_manager.state(name), self.device_manager.detail(name) if self.device_manager.state(name) == "err" else None)
        self._update_source_items()

    def _update_source_items(self):
        items = self._vds_sources()
        cur = self.cbo_source.currentText()
        source_lost = bool(cur and cur != "None" and cur not in items)
        self.cbo_source.blockSignals(True)
        self.cbo_source.clear()
        self.cbo_source.addItems(items or ["None"])
        if cur in items:
            self.cbo_source.setCurrentText(cur)
        elif items:
            self.cbo_source.setCurrentIndex(0)
        self.cbo_source.blockSignals(False)
        self.chk_use_vds.blockSignals(True)
        if source_lost or not items:
            self.chk_use_vds.setChecked(False)
        self.chk_use_vds.setEnabled(bool(items))
        self.chk_use_vds.blockSignals(False)
        if not items:
            availability = "Vds unavailable: no compatible Vds source is connected."
        elif len(items) == 1:
            availability = f"Vds available via {items[0]}. Enable recipe Vds to apply it."
        else:
            availability = "Vds sources available: " + ", ".join(items) + "."
        self.lbl_vds_availability.setText(availability)
        self._update_plot_axis_choices()
        self._refresh_condition_preview()

    def _plot_vds_source_for_choices(self) -> str:
        return self.cbo_source.currentText() if self.chk_use_vds.isChecked() else "None"

    def _update_plot_axis_choices(self):
        options = plot_channel_options(self._plot_vds_source_for_choices())
        current = self.cbo_y.currentText()
        if current not in options:
            current = "Ids_DC"
        self.cbo_y.blockSignals(True)
        self.cbo_y.clear()
        self.cbo_y.addItems(options)
        self.cbo_y.setCurrentText(current)
        self.cbo_y.blockSignals(False)
        self.plot.set_y_axis_options(options, current)
        self.plot.set_compare_channels(compare_channel_options(self._plot_vds_source_for_choices()))
        self.set_plot_axis_source(current)

    def _on_device_status_changed(self, name: str, _state: str, _detail: str):
        if name in {"g1", "g2", "g3", "daq", "mono"}:
            self._sync_sessions_from_manager()
            self._update_manual_buttons()

    def _on_operation_changed(self, busy: bool, message: str):
        if busy:
            self.set_status(message, "idle")
        self._update_manual_buttons()

    def _required_devices(self) -> list[str]:
        required = ["daq", "mono"]
        conditions = self._active_bias_conditions()
        if any(abs(condition.vtg) > 1e-12 for condition in conditions):
            required.append("g1")
        if any(abs(condition.vbg) > 1e-12 for condition in conditions):
            required.append("g2")
        if self.chk_use_vds.isChecked() and self.cbo_source.currentText() == "Keithley 2400":
            required.append("g3")
        return list(dict.fromkeys(required))

    def _validate_required_sessions(self) -> bool:
        self._sync_sessions_from_manager()
        try:
            conditions = self._active_bias_conditions(strict=True)
        except ValueError as ex:
            QtWidgets.QMessageBox.warning(self, "Bias Recipe", str(ex))
            return False
        if not conditions:
            QtWidgets.QMessageBox.warning(self, "Bias Recipe", "Select at least one bias condition to run.")
            return False
        for index, condition in enumerate(conditions, start=1):
            values = [("Vtg", condition.vtg), ("Vbg", condition.vbg)]
            if self.chk_use_vds.isChecked():
                values.append(("Vds", condition.vds))
            for label, value in values:
                if abs(value) > 20.0:
                    QtWidgets.QMessageBox.warning(self, "Bias Recipe", f"Condition {index}: {label} is outside the ±20 V limit.")
                    return False
        missing = [name for name in self._required_devices() if not self.device_manager.is_connected(name)]
        if missing:
            QtWidgets.QMessageBox.warning(self, "Missing Device", f"Connect required devices first: {', '.join(missing).upper()}")
            return False
        if self.chk_use_vds.isChecked() and self.cbo_source.currentText() == "None":
            QtWidgets.QMessageBox.warning(self, "Vds Source", "Connect a Vds source or turn off Apply Vds From Recipe before starting.")
            return False
        if self.chk_use_vds.isChecked() and self.cbo_source.currentText() == "Keithley 2400" and not self.device_manager.is_voltage_source_mode("g3"):
            QtWidgets.QMessageBox.warning(self, "Keithley Mode", "G3 must be in 2-wire voltage source mode when photocurrent uses Keithley Vds bias.")
            return False
        for gate, label in (("g1", "G1 / Vtg"), ("g2", "G2 / Vbg")):
            if gate in self._required_devices() and not self.device_manager.is_voltage_source_mode(gate):
                QtWidgets.QMessageBox.warning(self, "Gate Mode", f"{label} must be in 2-wire voltage source mode because a selected recipe condition uses it.")
                return False
        return True

    def _update_connection_hint(self):
        required = self._required_devices()
        optional = [name for name in ("g1", "g2") if name not in required]
        if not self.chk_use_vds.isChecked():
            optional.append("g3")
        missing_required = [name.upper() for name in required if not self.device_manager.is_connected(name)]
        missing_optional = [name.upper() for name in optional if not self.device_manager.is_connected(name)]
        if self.device_manager.is_connected("g1") and not self.device_manager.is_voltage_source_mode("g1"):
            (missing_required if "g1" in required else missing_optional).append("G1 mode")
        if self.device_manager.is_connected("g2") and not self.device_manager.is_voltage_source_mode("g2"):
            (missing_required if "g2" in required else missing_optional).append("G2 mode")
        if (
            self.chk_use_vds.isChecked()
            and self.cbo_source.currentText() == "Keithley 2400"
            and self.device_manager.is_connected("g3")
            and not self.device_manager.is_voltage_source_mode("g3")
        ):
            missing_required.append("G3 mode")
        if self.device_manager.is_busy():
            text = "Hardware is busy with another connection or disconnect operation from Instrument Setup."
            self.lbl_connection_hint.setProperty("role", "warning-hint")
            self.btn_start.setToolTip("Wait for the dock connection operation to finish")
        elif missing_required:
            text = f"Required before start: {', '.join(missing_required)}. Connect from Instrument Setup."
            if missing_optional:
                text += f" Optional manual controls unavailable: {', '.join(missing_optional)}."
            self.lbl_connection_hint.setProperty("role", "warning-hint")
            self.btn_start.setToolTip(f"Connect required devices from Instrument Setup: {', '.join(missing_required)}")
        else:
            text = "Ready to run with dock-managed sessions."
            if missing_optional:
                text += f" Optional controls unavailable until {', '.join(missing_optional)} connects."
            self.lbl_connection_hint.setProperty("role", "hint")
            self.btn_start.setToolTip("Start photocurrent scan")
        self.lbl_connection_hint.setText(text)
        self.lbl_connection_hint.style().unpolish(self.lbl_connection_hint)
        self.lbl_connection_hint.style().polish(self.lbl_connection_hint)

    def _update_vds_bias_state(self):
        available = self._vds_is_available()
        enabled = self.chk_use_vds.isChecked() and available
        manual_available = not self.device_manager.is_busy() and not self.device_manager.current_in_use()
        self.sp_vds.setEnabled(enabled)
        self.sp_vds_ramp.setEnabled(enabled)
        self.cbo_source.setEnabled(available)
        self.btn_set_vds.setEnabled(
            enabled
            and manual_available
            and (
                ("NI DAQ" in self.cbo_source.currentText() and self.s_daq is not None)
                or (self.s_g3 is not None and self.device_manager.is_voltage_source_mode("g3"))
            )
        )
        self._refresh_condition_preview()

    def on_set_vds(self):
        src = self.cbo_source.currentText()
        val = self.sp_vds.value()
        if src == "Keithley 2400" and self.s_g3:
            if self.device_manager.ramp_gate("g3", val):
                self.log.appendPlainText(f"[Manual] Ramping G3 / Vds to {val} V.")
        elif "NI DAQ" in src and self.s_daq:
            self.s_daq.ramp_voltage(int(src.split()[-1].replace("ao", "")), val, self.sp_vds_ramp.value())
            flash_button_success(self.btn_set_vds)

    def on_go_wl(self):
        if self.device_manager.set_monochromator_wavelength(self.sp_wls.value()):
            self.log.appendPlainText(f"[Manual] Moving monochromator to {self.sp_wls.value():g} nm.")

    def collect_params(self):
        self.refresh_output_preview()
        self.p.base_name = self.ed_base.text()
        self.p.output_csv_path = self._planned_output.csv_path if self._planned_output else ""
        self.p.output_metadata_path = self._planned_output.metadata_path if self._planned_output else ""
        self.p.output_log_path = self._planned_output.log_path if self._planned_output else ""
        self.p.use_vds = self.chk_use_vds.isChecked()
        src = self.cbo_source.currentText()
        if "NI DAQ" in src:
            self.p.vds_source = "NI DAQ AO"
            self.p.ao_channel = int(src.split()[-1].replace("ao", ""))
        else:
            self.p.vds_source = src
        self.p.vds_set = self.sp_vds.value()
        self.p.vds_ramp = self.sp_vds_ramp.value()
        self.p.bias_conditions = self._active_bias_conditions(strict=True)
        if not self.p.bias_conditions:
            raise ValueError("Select at least one enabled bias condition to run.")
        first = self.p.bias_conditions[0]
        self.p.vtg_set = first.vtg
        self.p.vbg_set = first.vbg
        if self.p.use_vds:
            self.p.vds_set = first.vds
        self.p.wl_start = self.sp_wls.value()
        self.p.wl_stop = self.sp_wle.value()
        self.p.wl_step = self.sp_wld.value()
        self.p.delay = self.sp_delay.value()
        self.p.n_sample = self.sp_nsamp.value()
        self.p.plot_choice = self.cbo_y.currentText()

    def start_run(self):
        if self.worker_thread:
            return
        mw = self.window()
        if hasattr(mw, "refresh_models_from_ui"):
            mw.refresh_models_from_ui()
        self.refresh_output_preview()
        if not self.validate_output_ready(self.save):
            return
        if not self._validate_required_sessions():
            return
        try:
            self.collect_params()
        except Exception as ex:
            QtWidgets.QMessageBox.warning(self, "Invalid Parameters", str(ex))
            return
        if not self._validate_condition_output_paths():
            return
        claimed, blocked = self.device_manager.mark_in_use(self._required_devices())
        if not claimed:
            QtWidgets.QMessageBox.warning(self, "Busy", f"Devices already in use: {', '.join(blocked).upper()}")
            return
        self._plot_records = []
        self.plot.clear()
        self.plot.ax.set_xlabel("Wavelength (nm)")
        self.set_plot_axis_source(self.p.plot_choice)
        try:
            self.begin_run_logging(self._planned_output, "Photocurrent")
            amp, lkn = self.get_global_rates()
            self.worker = PhotocurrentWorker(self.p, self.save, self.conns, g1=self.s_g1, g2=self.s_g2, g3=self.s_g3, daq=self.s_daq, mono=self.s_mono, plot_choice=self.p.plot_choice, amp_rate=amp, lkn_rate=lkn)
            self.worker_thread = QtCore.QThread()
            self.worker.moveToThread(self.worker_thread)
            self.worker_thread.started.connect(self.worker.run)
            self.worker.point_data.connect(self.on_point_data)
            self.worker.progress.connect(self.set_progress)
            self.worker.status.connect(lambda m: self.set_status(m, "running"))
            self.worker.log.connect(self.append_log)
            self.worker.finished.connect(self.on_finished)
            self.worker.stopped.connect(self.on_stopped)
            self.worker.finished.connect(self.worker_thread.quit)
            self.worker.stopped.connect(self.worker_thread.quit)
            self.worker.error.connect(self.on_error)
            self.worker.error.connect(self.worker_thread.quit)
            self.worker_thread.finished.connect(self._cleanup_thread)
            self.run_panel.set_running(True)
            self.set_status("Running...", "running")
            self.worker_thread.start()
        except Exception as ex:
            self.append_log(str(ex))
            self.end_run_logging("error", str(ex))
            self.device_manager.release(self._required_devices())

    def stop_run(self):
        if self.worker:
            self.set_status("Stopping safely...", "running", "Stop requested. Waiting for the worker to reach a safe checkpoint and ramp outputs to 0 V.")
            self.worker.request_stop()

    def _cleanup_thread(self):
        if self.worker:
            self.worker.deleteLater()
            self.worker = None
        self.worker_thread = None
        self.device_manager.release(self._required_devices())
        self.run_panel.set_running(False)
        self._update_manual_buttons()

    def on_point_data(self, record):
        self._plot_records.append(record)
        self._redraw_plot()

    def set_plot_axis_source(self, source: str):
        if source not in plot_channel_options(self._plot_vds_source_for_choices()):
            source = "Ids_DC"
        if self.cbo_y.currentText() != source:
            self.cbo_y.blockSignals(True)
            self.cbo_y.setCurrentText(source)
            self.cbo_y.blockSignals(False)
        self.plot.set_selected_y_axis(source)
        self.p.plot_choice = source
        self._redraw_plot()

    def _redraw_plot(self):
        groups: dict[int, list[dict]] = {}
        for record in self._plot_records:
            groups.setdefault(int(record.get("condition", 1)), []).append(record)

        def label_for(records: list[dict]) -> str:
            record = records[0]
            label = f"Run {record.get('condition', 1)}: Vtg={record.get('Vtg', 0):g} V, Vbg={record.get('Vbg', 0):g} V"
            if self.chk_use_vds.isChecked():
                label += f", Vds={record.get('Vds', 0):g} V"
            return label

        if self.plot.current_plot_mode() == "4-Channel Compare":
            axes = self.plot.get_axes()
            channels = self.plot.compare_channels()
            for axis, channel in zip(axes, channels):
                axis.clear()
                for records in groups.values():
                    axis.plot(
                        [record["x"] for record in records],
                        [plot_channel_value(record, channel) for record in records],
                        marker="o",
                        label=label_for(records),
                    )
                if groups:
                    axis.relim()
                    axis.autoscale_view()
                    if len(groups) > 1:
                        axis.legend(fontsize="x-small")
                axis.set_ylabel(f"{channel} (A)")
                axis.grid(True)
            if axes:
                axes[-1].set_xlabel("Wavelength (nm)")
        else:
            source = self.cbo_y.currentText()
            ax = self.plot.ax
            ax.clear()
            for records in groups.values():
                ax.plot(
                    [record["x"] for record in records],
                    [plot_channel_value(record, source) for record in records],
                    marker="o",
                    label=label_for(records),
                )
            if groups:
                ax.relim()
                ax.autoscale_view()
                if len(groups) > 1:
                    ax.legend(fontsize="small")
            ax.set_xlabel("Wavelength (nm)")
            ax.set_ylabel(f"{source} (A)")
            ax.grid(True)
        self.plot.canvas.draw_idle()

    def on_finished(self, path):
        self.set_status("Finished", "done", path)
        self.append_log(f"Saved: {path}")
        self.end_run_logging("finished", path)
        self._output_run_id = None
        self.refresh_output_preview()

    def on_error(self, msg):
        self.set_status("Run error", "error", msg)
        self.append_log("ERROR: " + msg)
        self.end_run_logging("error", msg)
        self._output_run_id = None
        self.refresh_output_preview()

    def on_stopped(self, message: str):
        self.set_status("Stopped by user", "done", message)
        self.append_log(message)
        self.end_run_logging("stopped", message)
        self._output_run_id = None
        self.refresh_output_preview()
