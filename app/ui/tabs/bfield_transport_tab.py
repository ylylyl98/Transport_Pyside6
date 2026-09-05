"""Continuous B-field transport sweep editor.

This tab intentionally has its own recipe and rate controls.  Gate Scan's
trajectory editor remains available for its original workflow; transport rows
are fixed Doping/E-field operating points held while APS100 traverses B.
"""

from __future__ import annotations

from copy import deepcopy
import json
import math
import os
import re
import time

from PyQt6 import QtCore, QtWidgets

from app.engine.bfield_transport_sweep import (
    COIL_CONSTANT_T_PER_A,
    MAX_DRIVEN_FIELD_T,
    MAX_RATE_A_PER_S,
    MAX_RATE_T_PER_MIN,
    COOLDOWN_POLICIES,
    COOLDOWN_POLICY_LABELS,
    FINAL_MODES,
    FINAL_MODE_LABELS,
    estimate_transport_times,
    build_transport_output_paths,
    normalize_cooldown_policy,
    normalize_final_mode,
    parse_condition_series,
    transport_output_summary_parts,
    BFieldTransportSafetyError,
    rate_t_per_min_to_a_per_s,
    validate_setup,
)
from app.gate_transform import derived_to_gates, gates_to_derived
from app.gate_transform import RATIO_TARGET_VBG, RATIO_TARGET_VTG, ratio_formula_text, normalize_ratio_target
from app.models import BFieldTransportCondition, BFieldTransportParams, Connections, SaveRoot
from app.run_output import build_planned_output
from app.settings import get_app_settings
from app.ui.helpers import set_standard_input_height, style_form_layout
from app.ui.tabs.base_tab import BaseMeasurementTab
from app.ui.widgets.safe_combo import SafeComboBox
from app.ui.widgets.safe_spinbox import SafeDoubleSpinBox, SafeSpinBox
from app.ui.widgets.status_panel import SectionHeader, StatusPanel
from utils.config import cfg


class BFieldTransportTab(BaseMeasurementTab):
    SETTINGS_PREFIX = "tabs/bfield_transport"

    def __init__(self, save: SaveRoot, conns: Connections, device_manager, **_kwargs):
        self.save = save
        self.conns = conns
        self.device_manager = device_manager
        self.get_global_rates = _kwargs.get("get_global_rates_callable") or (lambda: (1e7, 100.0))
        self.get_signal_chain = _kwargs.get("get_signal_chain_callable") or (lambda: None)
        self.params = BFieldTransportParams()
        self.conditions = [BFieldTransportCondition()]
        self._output_run_id = None
        self._planned_output = None
        self._transport_output_paths = None
        self._locked = False
        self._execution_controller = None
        super().__init__("START B-FIELD SWEEP", "B-field (T)", "Ids (A)", ["g1", "g2", "g3", "daq"])
        self.device_manager.status_changed.connect(self._on_device_status_changed)
        self.device_manager.resources_changed.connect(self.refresh_hardware_readiness)
        self._sync_measurement_statuses()
        self._load_settings()
        self._refresh_conditions()
        self.btn_start.clicked.connect(self.start_run)
        self.btn_stop.clicked.connect(self.stop_run)

    @staticmethod
    def _spin(value=0.0, minimum=-1000.0, maximum=1000.0, decimals=6):
        spin = SafeDoubleSpinBox()
        spin.setRange(minimum, maximum)
        spin.setDecimals(decimals)
        spin.setValue(value)
        set_standard_input_height(spin)
        return spin

    def _build_control_panel(self, ctl_layout):
        ctl_layout.addWidget(SectionHeader("B-field trajectory"))
        group = QtWidgets.QGroupBox("Driven-mode sweep")
        form = QtWidgets.QFormLayout(group)
        style_form_layout(form)
        self.sp_start = self._spin(-0.5, -MAX_DRIVEN_FIELD_T, MAX_DRIVEN_FIELD_T)
        self.sp_stop = self._spin(0.5, -MAX_DRIVEN_FIELD_T, MAX_DRIVEN_FIELD_T)
        self.sp_rate = self._spin(0.1, 1e-6, MAX_RATE_T_PER_MIN, 6)
        self.sp_rate_a = QtWidgets.QLabel()
        self.sp_rate_t_s = QtWidgets.QLabel()
        self.chk_round_trip = QtWidgets.QCheckBox("Round trip: start → stop → start")
        self.chk_round_trip.setChecked(True)
        form.addRow("Start (T):", self.sp_start)
        form.addRow("Stop (T):", self.sp_stop)
        form.addRow("Sweep rate (T/min):", self.sp_rate)
        form.addRow("APS100 rate:", self.sp_rate_a)
        form.addRow("Field rate:", self.sp_rate_t_s)
        form.addRow("", self.chk_round_trip)
        self.lbl_rate_limit = QtWidgets.QLabel(
            f"Hard limit: {MAX_RATE_T_PER_MIN:.8f} T/min ({MAX_RATE_A_PER_S:.5f} A/s); "
            f"driven field envelope ±{MAX_DRIVEN_FIELD_T:g} T"
        )
        self.lbl_rate_limit.setWordWrap(True)
        self.lbl_rate_limit.setProperty("role", "hint")
        form.addRow("", self.lbl_rate_limit)
        self.cbo_cooldown_policy = SafeComboBox()
        for policy in COOLDOWN_POLICIES:
            self.cbo_cooldown_policy.addItem(COOLDOWN_POLICY_LABELS[policy], policy)
        self.cbo_cooldown_policy.setToolTip(
            "Adaptive pauses only when Lake Shore permission requires recovery; "
            "Stay driven holds the heater on across rows; Persistent after every row "
            "performs an acknowledged persistent cooldown between rows."
        )
        form.addRow("Between-row thermal policy:", self.cbo_cooldown_policy)
        self.cbo_final_mode = SafeComboBox()
        for mode in FINAL_MODES:
            self.cbo_final_mode.addItem(FINAL_MODE_LABELS[mode], mode)
        self.cbo_final_mode.setToolTip(
            "On successful completion, Persistent cools and zeros the leads; "
            "Driven leaves the heater ON at the final field. Stops, errors, "
            "and interlocks always use conservative persistent cleanup."
        )
        form.addRow("Successful final APS100 mode:", self.cbo_final_mode)
        self.sp_ratio = SafeDoubleSpinBox()
        self.sp_ratio.setRange(-1e4, 1e4)
        self.sp_ratio.setDecimals(4)
        self.sp_ratio.setValue(self.params.ratio)
        self.cbo_ratio_target = SafeComboBox()
        self.cbo_ratio_target.addItem("Top gate: r × Vtg", RATIO_TARGET_VTG)
        self.cbo_ratio_target.addItem("Bottom gate: r × Vbg", RATIO_TARGET_VBG)
        self.lbl_ratio_formula = QtWidgets.QLabel()
        self.lbl_ratio_formula.setWordWrap(True)
        self.lbl_ratio_formula.setProperty("role", "hint")
        form.addRow("Gate weighting ratio, r:", self.sp_ratio)
        form.addRow("Multiply by r:", self.cbo_ratio_target)
        form.addRow("Active equations:", self.lbl_ratio_formula)
        ctl_layout.addWidget(group)

        ctl_layout.addWidget(SectionHeader("Gate conditions for each B-field sweep"))
        intro = QtWidgets.QLabel(
            "Sweep the magnetic field while measuring current at fixed doping and E-field conditions. "
            "Each row fixes both doping and E-field for one complete trajectory; "
            "Vtg/Vbg are calculated from the global gate weighting ratio."
        )
        intro.setWordWrap(True)
        intro.setProperty("role", "hint")
        ctl_layout.addWidget(intro)
        self.condition_table = QtWidgets.QTableWidget(0, 9)
        self.condition_table.setHorizontalHeaderLabels(
            ["", "Name", "Dop\ning", "E-\nfield", "Vds", "Sour\nce", "AO", "Vtg", "Vbg"]
        )
        self.condition_table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectionBehavior.SelectRows)
        self.condition_table.setSelectionMode(QtWidgets.QAbstractItemView.SelectionMode.SingleSelection)
        self.condition_table.setMinimumHeight(150)
        self.condition_table.setMaximumHeight(330)
        self.condition_table.verticalHeader().setVisible(False)
        header = self.condition_table.horizontalHeader()
        header.setStretchLastSection(False)
        header.setMinimumSectionSize(20)
        header.setFixedHeight(max(38, header.fontMetrics().lineSpacing() * 2 + 8))
        header.setDefaultAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        header.setSectionResizeMode(QtWidgets.QHeaderView.ResizeMode.Fixed)
        for column in (2, 3, 4, 5, 7, 8):
            header.setSectionResizeMode(column, QtWidgets.QHeaderView.ResizeMode.Stretch)
        for column, width in {0: 22, 1: 48, 6: 24}.items():
            self.condition_table.setColumnWidth(column, width)
        for column, tooltip in enumerate((
            "Enable condition", "Condition name", "Fixed doping", "Fixed E-field",
            "Drain bias", "Vds source", "DAQ analog-output channel",
            "Calculated top-gate voltage", "Calculated bottom-gate voltage",
        )):
            self.condition_table.horizontalHeaderItem(column).setToolTip(tooltip)
        self.condition_table.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.condition_table.setToolTip(
            "All condition columns fit without horizontal scrolling. Select a row to see its "
            "complete, unabridged values below the table."
        )
        self.condition_table.itemChanged.connect(self._condition_edited)
        self.condition_table.itemSelectionChanged.connect(self._refresh_condition_details)
        ctl_layout.addWidget(self.condition_table)
        self.lbl_condition_details = QtWidgets.QLabel()
        self.lbl_condition_details.setWordWrap(True)
        self.lbl_condition_details.setProperty("role", "hint")
        self.lbl_condition_details.setMinimumWidth(0)
        self.lbl_condition_details.setSizePolicy(
            QtWidgets.QSizePolicy.Policy.Ignored,
            QtWidgets.QSizePolicy.Policy.Preferred,
        )
        ctl_layout.addWidget(self.lbl_condition_details)
        builder = QtWidgets.QGroupBox("Quick add conditions")
        builder_layout = QtWidgets.QVBoxLayout(builder)
        builder_layout.setContentsMargins(8, 6, 8, 8)
        builder_layout.setSpacing(5)
        mode_row = QtWidgets.QHBoxLayout()
        mode_row.addWidget(QtWidgets.QLabel("Coordinates:"))
        self.cbo_condition_add_mode = SafeComboBox()
        self.cbo_condition_add_mode.addItem("Doping / E-field", "derived")
        self.cbo_condition_add_mode.addItem("Vtg / Vbg", "gates")
        mode_row.addWidget(self.cbo_condition_add_mode, 1)
        builder_layout.addLayout(mode_row)
        values_row = QtWidgets.QHBoxLayout()
        self.lbl_condition_add_first = QtWidgets.QLabel("Doping:")
        self.ed_condition_add_first = QtWidgets.QLineEdit("0")
        self.lbl_condition_add_second = QtWidgets.QLabel("E-field:")
        self.ed_condition_add_second = QtWidgets.QLineEdit("0")
        set_standard_input_height(self.ed_condition_add_first)
        set_standard_input_height(self.ed_condition_add_second)
        self.ed_condition_add_first.setPlaceholderText("0, 1, 2 or 0:3:1")
        self.ed_condition_add_second.setPlaceholderText("single value or matching array")
        values_row.addWidget(self.lbl_condition_add_first)
        values_row.addWidget(self.ed_condition_add_first, 1)
        values_row.addWidget(self.lbl_condition_add_second)
        values_row.addWidget(self.ed_condition_add_second, 1)
        builder_layout.addLayout(values_row)
        vds_row = QtWidgets.QHBoxLayout()
        vds_row.addWidget(QtWidgets.QLabel("Vds (V):"))
        self.ed_condition_add_vds = QtWidgets.QLineEdit("0.1")
        set_standard_input_height(self.ed_condition_add_vds)
        vds_row.addWidget(self.ed_condition_add_vds, 1)
        builder_layout.addLayout(vds_row)
        syntax_hint = QtWidgets.QLabel(
            "Single: 0 | List: -1,1,5 | Range: -1:1:0.5 (stop excluded). "
            "Series pair row by row; single values repeat."
        )
        syntax_hint.setWordWrap(True)
        syntax_hint.setProperty("role", "hint")
        builder_layout.addWidget(syntax_hint)
        for editor in (self.ed_condition_add_first, self.ed_condition_add_second, self.ed_condition_add_vds):
            editor.setToolTip(syntax_hint.text() + " Maximum 100 conditions per addition.")
        self.lbl_condition_add_preview = QtWidgets.QLabel()
        self.lbl_condition_add_preview.setWordWrap(True)
        self.lbl_condition_add_preview.setProperty("role", "hint")
        self.lbl_condition_add_preview.setMinimumWidth(0)
        builder_layout.addWidget(self.lbl_condition_add_preview)
        self.condition_add_preview_table = QtWidgets.QTableWidget(0, 6)
        self.condition_add_preview_table.setHorizontalHeaderLabels(
            ["#", "Doping", "E-field", "Vds (V)", "Vtg (V)", "Vbg (V)"]
        )
        self.condition_add_preview_table.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
        self.condition_add_preview_table.verticalHeader().hide()
        self.condition_add_preview_table.horizontalHeader().setSectionResizeMode(QtWidgets.QHeaderView.ResizeMode.ResizeToContents)
        self.condition_add_preview_table.setMinimumWidth(0)
        self.condition_add_preview_table.setFixedHeight(155)
        builder_layout.addWidget(self.condition_add_preview_table)
        self.btn_condition_add_preview = QtWidgets.QPushButton("Add previewed conditions to table")
        builder_layout.addWidget(self.btn_condition_add_preview)
        ctl_layout.addWidget(builder)
        self.btn_condition_update = QtWidgets.QPushButton("Update selected")
        self.btn_condition_duplicate = QtWidgets.QPushButton("Duplicate")
        self.btn_condition_remove = QtWidgets.QPushButton("Remove")
        self.btn_condition_up = QtWidgets.QPushButton("↑")
        self.btn_condition_down = QtWidgets.QPushButton("↓")
        edit_row = QtWidgets.QHBoxLayout()
        for button in (self.btn_condition_update, self.btn_condition_duplicate, self.btn_condition_remove, self.btn_condition_up, self.btn_condition_down):
            edit_row.addWidget(button)
        ctl_layout.addLayout(edit_row)
        self.cbo_condition_add_mode.currentIndexChanged.connect(self._condition_add_mode_changed)
        self.ed_condition_add_first.textChanged.connect(self._refresh_condition_add_preview)
        self.ed_condition_add_second.textChanged.connect(self._refresh_condition_add_preview)
        self.ed_condition_add_vds.textChanged.connect(self._refresh_condition_add_preview)
        self.btn_condition_add_preview.clicked.connect(self._add_previewed_conditions)
        self.btn_condition_update.clicked.connect(self._update_selected)
        self.btn_condition_duplicate.clicked.connect(self._duplicate_condition)
        self.btn_condition_remove.clicked.connect(self._remove_condition)
        self.btn_condition_up.clicked.connect(lambda: self._move_condition(-1))
        self.btn_condition_down.clicked.connect(lambda: self._move_condition(1))

        ctl_layout.addWidget(SectionHeader("Acquisition / output"))
        acq = QtWidgets.QGroupBox("Acquisition")
        acq_form = QtWidgets.QFormLayout(acq)
        style_form_layout(acq_form)
        self.sp_delay = self._spin(0.1, 0.0, 60.0, 3)
        self.sp_averages = SafeSpinBox()
        self.sp_averages.setRange(1, 10000)
        self.sp_averages.setValue(1)
        self.ed_base = QtWidgets.QLineEdit(self.params.base_name)
        acq_form.addRow("Delay (s):", self.sp_delay)
        acq_form.addRow("Averages:", self.sp_averages)
        acq_form.addRow("Filename stem:", self.ed_base)
        ctl_layout.addWidget(acq)
        output_wrap = QtWidgets.QWidget()
        output_layout = QtWidgets.QVBoxLayout(output_wrap)
        output_layout.setContentsMargins(0, 0, 0, 0)
        self._add_output_preview_section(output_layout)
        ctl_layout.addWidget(output_wrap)
        self.lbl_preview = QtWidgets.QLabel()
        self.lbl_preview.setWordWrap(True)
        self.lbl_preview.setProperty("role", "hint")
        ctl_layout.addWidget(self.lbl_preview)
        self.lbl_time_estimate = QtWidgets.QLabel()
        self.lbl_time_estimate.setWordWrap(True)
        self.lbl_time_estimate.setProperty("role", "hint")
        ctl_layout.addWidget(self.lbl_time_estimate)

        self.lbl_connection_hint = QtWidgets.QLabel("APS100 and measurement sessions must be connected before starting.")
        self.lbl_connection_hint.setWordWrap(True)
        self.lbl_connection_hint.setProperty("role", "hint")
        ctl_layout.addWidget(self.lbl_connection_hint)
        self.status_panel = StatusPanel(["aps100", "g1", "g2", "g3", "daq"])
        ctl_layout.addWidget(self.status_panel)
        self.sp_start.valueChanged.connect(self._refresh_rate_preview)
        self.sp_stop.valueChanged.connect(self._refresh_rate_preview)
        self.sp_rate.valueChanged.connect(self._refresh_rate_preview)
        self.chk_round_trip.toggled.connect(self._refresh_rate_preview)
        self.sp_ratio.valueChanged.connect(self._refresh_ratio_preview)
        self.cbo_ratio_target.currentIndexChanged.connect(self._refresh_ratio_preview)
        self.cbo_cooldown_policy.currentIndexChanged.connect(self._refresh_rate_preview)
        self.cbo_final_mode.currentIndexChanged.connect(self._refresh_rate_preview)
        self.ed_base.textChanged.connect(self.refresh_output_preview)
        self._condition_add_mode_changed()
        self._refresh_rate_preview()
        self._refresh_ratio_preview()

    def _load_settings(self):
        settings = get_app_settings()
        settings.beginGroup(self.SETTINGS_PREFIX)
        self.sp_start.setValue(float(settings.value("start_field_t", self.params.start_field_t)))
        self.sp_stop.setValue(float(settings.value("stop_field_t", self.params.stop_field_t)))
        self.sp_rate.setValue(float(settings.value("rate_t_per_min", self.params.rate_t_per_min)))
        has_global_ratio = settings.contains("ratio")
        if has_global_ratio:
            self.sp_ratio.setValue(float(settings.value("ratio", self.params.ratio)))
        try:
            global_target = normalize_ratio_target(str(settings.value("ratio_target", self.params.ratio_target)))
        except ValueError:
            global_target = self.params.ratio_target
        self.cbo_ratio_target.setCurrentIndex(max(0, self.cbo_ratio_target.findData(global_target)))
        self.chk_round_trip.setChecked(settings.value("round_trip", True, type=bool))
        try:
            policy = normalize_cooldown_policy(str(settings.value("cooldown_policy", "adaptive")))
        except ValueError:
            policy = "adaptive"
        self.cbo_cooldown_policy.setCurrentIndex(max(0, self.cbo_cooldown_policy.findData(policy)))
        # Migrate the old Persistent default once for quick successive sweeps.
        # Subsequent explicit selections (including Persistent) are preserved.
        if not settings.value("driven_default_applied", False, type=bool):
            settings.setValue("final_mode", "driven")
            settings.setValue("driven_default_applied", True)
        try:
            final_mode = normalize_final_mode(str(settings.value("final_mode", "driven")))
        except ValueError:
            final_mode = "driven"
        self.cbo_final_mode.setCurrentIndex(max(0, self.cbo_final_mode.findData(final_mode)))
        self.sp_delay.setValue(float(settings.value("delay_s", self.params.acquisition_delay_s)))
        self.sp_averages.setValue(int(settings.value("averages", self.params.averages)))
        self.ed_base.setText(str(settings.value("base_name", self.params.base_name)))
        raw_conditions = settings.value("conditions", "")
        if raw_conditions:
            try:
                values = json.loads(str(raw_conditions))
                loaded = [BFieldTransportCondition(**dict(item)) for item in values]
                if loaded:
                    for condition in loaded:
                        legacy_name = re.fullmatch(r"Condition\s+(\d+)(.*)", condition.name)
                        if legacy_name:
                            condition.name = f"Con{legacy_name.group(1)}{legacy_name.group(2)}"
                    self.conditions = loaded
                    # Migrate legacy per-row ratio fields once.  A persisted
                    # global value wins; otherwise the first row is the only
                    # permitted source and all rows are normalized to it.
                    if not has_global_ratio:
                        self.sp_ratio.setValue(float(loaded[0].ratio))
                        try:
                            target = normalize_ratio_target(loaded[0].ratio_target)
                            self.cbo_ratio_target.setCurrentIndex(max(0, self.cbo_ratio_target.findData(target)))
                        except ValueError:
                            pass
            except (TypeError, ValueError, json.JSONDecodeError):
                pass
        settings.endGroup()

    def _save_settings(self):
        settings = get_app_settings()
        settings.beginGroup(self.SETTINGS_PREFIX)
        settings.setValue("start_field_t", self.sp_start.value())
        settings.setValue("stop_field_t", self.sp_stop.value())
        settings.setValue("rate_t_per_min", self.sp_rate.value())
        settings.setValue("ratio", self.sp_ratio.value())
        settings.setValue("ratio_target", self.cbo_ratio_target.currentData() or RATIO_TARGET_VBG)
        settings.setValue("round_trip", self.chk_round_trip.isChecked())
        settings.setValue("cooldown_policy", self.cbo_cooldown_policy.currentData() or "adaptive")
        settings.setValue("final_mode", self.cbo_final_mode.currentData() or "driven")
        settings.setValue("driven_default_applied", True)
        settings.setValue("delay_s", self.sp_delay.value())
        settings.setValue("averages", self.sp_averages.value())
        settings.setValue("base_name", self.ed_base.text())
        settings.setValue("conditions", json.dumps([condition.__dict__ for condition in self.conditions]))
        settings.endGroup()

    @staticmethod
    def _condition_values(condition):
        condition.refresh_gates()
        source = "K2400" if condition.vds_source == "Keithley 2400" else (
            "DAQ" if condition.vds_source == "NI DAQ AO" else condition.vds_source
        )
        return ["✓" if condition.enabled else "", condition.name, f"{condition.doping:g}", f"{condition.efield:g}", f"{condition.vds:g}", source, str(condition.ao_channel), f"{condition.vtg:g}", f"{condition.vbg:g}"]

    @staticmethod
    def _canonical_vds_source(value):
        source = str(value or "").strip()
        aliases = {
            "k2400": "Keithley 2400",
            "keithley": "Keithley 2400",
            "keithley 2400": "Keithley 2400",
            "daq": "NI DAQ AO",
            "ni daq": "NI DAQ AO",
            "ni daq ao": "NI DAQ AO",
        }
        return aliases.get(source.lower(), source or "Keithley 2400")

    def _apply_global_ratio(self):
        ratio = float(self.sp_ratio.value())
        target = normalize_ratio_target(self.cbo_ratio_target.currentData() or RATIO_TARGET_VBG)
        for condition in self.conditions:
            condition.ratio = ratio
            condition.ratio_target = target
            condition.refresh_gates()

    def _refresh_ratio_preview(self, *_args):
        try:
            target = normalize_ratio_target(self.cbo_ratio_target.currentData() or RATIO_TARGET_VBG)
            ratio = float(self.sp_ratio.value())
            formula = ratio_formula_text(target).replace("r", f"{ratio:g}")
            self.lbl_ratio_formula.setText(formula)
            self._apply_global_ratio()
            if hasattr(self, "condition_table"):
                self._refresh_conditions()
            if hasattr(self, "lbl_condition_add_preview"):
                self._refresh_condition_add_preview()
        except Exception as exc:
            self.lbl_ratio_formula.setText(f"Invalid ratio: {exc}")

    def _refresh_conditions(self):
        self._apply_global_ratio()
        self.condition_table.blockSignals(True)
        try:
            self.condition_table.setRowCount(len(self.conditions))
            for row, condition in enumerate(self.conditions):
                for column, value in enumerate(self._condition_values(condition)):
                    item = QtWidgets.QTableWidgetItem(value)
                    if column == 0:
                        item.setFlags(item.flags() | QtCore.Qt.ItemFlag.ItemIsUserCheckable)
                        item.setCheckState(QtCore.Qt.CheckState.Checked if condition.enabled else QtCore.Qt.CheckState.Unchecked)
                    if column in (7, 8):
                        item.setFlags(item.flags() & ~QtCore.Qt.ItemFlag.ItemIsEditable)
                    if column == 0:
                        item.setTextAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
                    else:
                        item.setToolTip(condition.vds_source if column == 5 else value)
                    self.condition_table.setItem(row, column, item)
        finally:
            self.condition_table.blockSignals(False)
        self._update_condition_buttons()
        self._refresh_condition_details()
        self.refresh_output_preview()
        self.refresh_hardware_readiness()

    def _condition_from_row(self, row):
        def text(col):
            item = self.condition_table.item(row, col)
            return item.text().strip() if item else ""
        try:
            condition = BFieldTransportCondition(
                name=text(1) or f"Con{row + 1}", doping=float(text(2)), efield=float(text(3)),
                ratio=self.sp_ratio.value(), ratio_target=self.cbo_ratio_target.currentData() or RATIO_TARGET_VBG,
                vds=float(text(4)), vds_source=self._canonical_vds_source(text(5)), ao_channel=int(text(6) or 0),
                enabled=(self.condition_table.item(row, 0).checkState() == QtCore.Qt.CheckState.Checked),
            )
            condition.refresh_gates()
            return condition
        except (TypeError, ValueError) as exc:
            raise BFieldTransportSafetyError(f"Condition row {row + 1}: {exc}") from exc

    def _collect_conditions(self):
        return [self._condition_from_row(row) for row in range(self.condition_table.rowCount())]

    def _condition_edited(self, item):
        if item.column() in (2, 3, 4, 5):
            try:
                condition = self._condition_from_row(item.row())
                self.condition_table.blockSignals(True)
                self.condition_table.item(item.row(), 7).setText(f"{condition.vtg:g}")
                self.condition_table.item(item.row(), 8).setText(f"{condition.vbg:g}")
                self.condition_table.blockSignals(False)
            except Exception:
                pass
        self._update_condition_buttons()
        self._refresh_condition_details()
        self.refresh_output_preview()

    def _refresh_condition_details(self, *_args):
        if not hasattr(self, "lbl_condition_details"):
            return
        row = self._selected_row()
        if row < 0 and self.condition_table.rowCount():
            row = 0
        if row < 0:
            self.lbl_condition_details.setText("No gate conditions configured.")
            return
        try:
            condition = self._condition_from_row(row)
        except Exception:
            condition = self.conditions[row] if row < len(self.conditions) else None
        if condition is None:
            self.lbl_condition_details.setText("Selected condition contains invalid values.")
            return
        condition.refresh_gates()
        state = "Enabled" if condition.enabled else "Disabled"
        self.lbl_condition_details.setText(
            f"{condition.name} | {state} | Doping {condition.doping:g} | "
            f"E-field {condition.efield:g} | Vds {condition.vds:g} V from {condition.vds_source} | "
            f"AO{condition.ao_channel} | Vtg {condition.vtg:g} V | Vbg {condition.vbg:g} V"
        )

    def _selected_row(self):
        selected = self.condition_table.selectionModel().selectedRows()
        return selected[0].row() if selected else -1

    def _update_condition_buttons(self):
        row = self._selected_row()
        enabled = row >= 0
        for button in (self.btn_condition_update, self.btn_condition_duplicate, self.btn_condition_remove, self.btn_condition_up, self.btn_condition_down):
            button.setEnabled(enabled)
        self.btn_condition_remove.setEnabled(len(self.conditions) > 1 and enabled)

    def _condition_add_mode_changed(self, *_args):
        gates = self.cbo_condition_add_mode.currentData() == "gates"
        self.lbl_condition_add_first.setText("Vtg:" if gates else "Doping:")
        self.lbl_condition_add_second.setText("Vbg:" if gates else "E-field:")
        self._refresh_condition_add_preview()

    def _next_condition_name(self):
        used = {condition.name for condition in self.conditions}
        index = 1
        while f"Con{index}" in used:
            index += 1
        return f"Con{index}"

    def _previewed_add_conditions(self):
        gates = self.cbo_condition_add_mode.currentData() == "gates"
        first_label, second_label = (("Vtg", "Vbg") if gates else ("Doping", "E-field"))
        first = parse_condition_series(self.ed_condition_add_first.text(), first_label)
        second = parse_condition_series(self.ed_condition_add_second.text(), second_label)
        vds = parse_condition_series(self.ed_condition_add_vds.text(), "Vds")
        count = max(len(first), len(second), len(vds))
        if any(len(values) not in (1, count) for values in (first, second, vds)):
            raise ValueError(f"{first_label}, {second_label}, and Vds must have equal lengths, or be single values")
        columns = [values * count if len(values) == 1 else values for values in (first, second, vds)]
        ratio = float(self.sp_ratio.value())
        target = self.cbo_ratio_target.currentData() or RATIO_TARGET_VBG
        conditions = []
        for first_value, second_value, vds_value in zip(*columns):
            if gates:
                doping, efield = gates_to_derived(first_value, second_value, ratio, target)
            else:
                doping, efield = first_value, second_value
            condition = BFieldTransportCondition(
                name="",
                doping=doping,
                efield=efield,
                vds=vds_value,
                ratio=ratio,
                ratio_target=target,
            )
            condition.refresh_gates()
            for device, label, value in (("g1", "Vtg", condition.vtg), ("g2", "Vbg", condition.vbg), ("g3", "Vds", condition.vds)):
                if not math.isfinite(value):
                    raise ValueError(f"{label} must be finite")
                if self.device_manager.is_connected(device):
                    limit = self.device_manager.applied_gate_voltage_limit(device)
                    if abs(value) > limit + 1e-9:
                        raise ValueError(f"{label} {value:g} V exceeds verified {device.upper()} limit {limit:g} V")
            conditions.append(condition)
        return conditions

    def _refresh_condition_add_preview(self, *_args):
        try:
            conditions = self._previewed_add_conditions()
            self.condition_add_preview_table.setRowCount(len(conditions))
            for index, condition in enumerate(conditions):
                for column, value in enumerate((index + 1, condition.doping, condition.efield, condition.vds, condition.vtg, condition.vbg)):
                    item = QtWidgets.QTableWidgetItem(f"{value:g}")
                    item.setToolTip(f"{value:.12g}")
                    self.condition_add_preview_table.setItem(index, column, item)
            self.lbl_condition_add_preview.setText(
                f"Preview ({len(conditions)} condition{'s' if len(conditions) != 1 else ''}): "
                "One full B-field trajectory per row. Vds source: Keithley 2400."
            )
            self.lbl_condition_add_preview.setToolTip(
                "\n".join(
                    f"{index}. Doping {condition.doping:g}, E-field {condition.efield:g}, "
                    f"Vtg {condition.vtg:g} V, Vbg {condition.vbg:g} V, Vds {condition.vds:g} V"
                    for index, condition in enumerate(conditions, start=1)
                )
            )
            self.lbl_condition_add_preview.setProperty("role", "hint")
            self.btn_condition_add_preview.setEnabled(not getattr(self, "_locked", False))
        except Exception as exc:
            self.lbl_condition_add_preview.setText(f"Cannot preview: {exc}")
            self.condition_add_preview_table.setRowCount(0)
            self.lbl_condition_add_preview.setToolTip("")
            self.lbl_condition_add_preview.setProperty("role", "warning-hint")
            self.btn_condition_add_preview.setEnabled(False)
        self.lbl_condition_add_preview.style().unpolish(self.lbl_condition_add_preview)
        self.lbl_condition_add_preview.style().polish(self.lbl_condition_add_preview)

    def _add_previewed_conditions(self):
        try:
            pending = self._previewed_add_conditions()
        except Exception:
            self._refresh_condition_add_preview()
            return
        first_new_row = len(self.conditions)
        for condition in pending:
            condition.name = self._next_condition_name()
            self.conditions.append(condition)
        self._refresh_conditions()
        self.condition_table.selectRow(first_new_row)

    def _update_selected(self):
        row = self._selected_row()
        if row >= 0:
            try:
                self.conditions[row] = self._condition_from_row(row)
                self._refresh_conditions()
                self.condition_table.selectRow(row)
            except ValueError as exc:
                self.lbl_preview.setText(str(exc))

    def _duplicate_condition(self):
        row = self._selected_row()
        if row < 0:
            return
        self.conditions.insert(row + 1, deepcopy(self.conditions[row]))
        self.conditions[row + 1].name += " copy"
        self._refresh_conditions()
        self.condition_table.selectRow(row + 1)

    def _remove_condition(self):
        row = self._selected_row()
        if len(self.conditions) > 1 and row >= 0:
            self.conditions.pop(row)
            self._refresh_conditions()
            self.condition_table.selectRow(min(row, len(self.conditions) - 1))

    def _move_condition(self, delta):
        row = self._selected_row()
        target = row + int(delta)
        if not (0 <= row < len(self.conditions) and 0 <= target < len(self.conditions)):
            return
        self.conditions[row], self.conditions[target] = self.conditions[target], self.conditions[row]
        self._refresh_conditions()
        self.condition_table.selectRow(target)

    def _refresh_rate_preview(self, *_args):
        try:
            value = rate_t_per_min_to_a_per_s(self.sp_rate.value())
            self.sp_rate_a.setText(f"{value:.7f} A/s")
            self.sp_rate_t_s.setText(f"{self.sp_rate.value() / 60.0:.8f} T/s")
            validate_setup(self.sp_start.value(), self.sp_stop.value(), self.sp_rate.value())
            planned = build_planned_output(
                self.save, "bfield_transport", self.ed_base.text().strip() or "bfield_transport",
                run_id="preview", create_dir=False,
            )
            enabled = sum(1 for condition in self.conditions if condition.enabled)
            self.lbl_preview.setText(
                "Ready: " + ("round trip" if self.chk_round_trip.isChecked() else "one-way")
                + f" {self.sp_start.value():+.4g} → {self.sp_stop.value():+.4g} T; "
                + f"{enabled} enabled condition(s); final mode: "
                + f"{FINAL_MODE_LABELS.get(self.cbo_final_mode.currentData(), 'Persistent')}; "
                + f"output preview: {planned.output_dir}"
            )
            self.lbl_preview.setProperty("role", "hint")
            enabled_conditions = [condition for condition in self.conditions if condition.enabled]
            estimate = estimate_transport_times(
                self.sp_start.value(), self.sp_stop.value(), self.sp_rate.value(), len(enabled_conditions),
                round_trip=self.chk_round_trip.isChecked(),
                bias_settle_s=max((condition.settle_s for condition in enabled_conditions), default=0.5),
                cooldown_policy=self.cbo_cooldown_policy.currentData() or "adaptive",
                heater_cool_s=getattr(cfg.magnet, "heater_cool_s", None),
                recovery_dwell_s=getattr(cfg.lakeshore335, "required_stable_recovery_dwell_s", None),
                heater_warm_s=getattr(cfg.magnet, "heater_warm_s", None),
            )
            thermal = (
                f"; fixed thermal overhead {estimate['fixed_thermal_extra_s']:.1f} s"
                + (f"; field return/positioning {estimate['field_positioning_extra_s']:.1f} s" if estimate["field_positioning_extra_s"] else "")
                + (" + variable/unknown recovery" if estimate["thermal_recovery_variable"] else "")
            ) if (self.cbo_cooldown_policy.currentData() == "persistent_each_row" and len(enabled_conditions) > 1) else (
                "; adaptive thermal recovery is variable/extra" if estimate["thermal_recovery_variable"] else ""
            )
            self.lbl_time_estimate.setText(
                f"Estimate: {estimate['sweep_s_per_row']:.1f} s sweep/row; "
                f"{estimate['batch_s']:.1f} s deterministic batch{thermal}."
            )
        except Exception as exc:
            self.lbl_preview.setText(f"Invalid sweep: {exc}")
            self.lbl_preview.setProperty("role", "warning-hint")
            if hasattr(self, "lbl_time_estimate"):
                self.lbl_time_estimate.setText("")
        if hasattr(self, "lbl_filename_preview"):
            self.refresh_output_preview()

    def collect_params(self):
        self.conditions = self._collect_conditions()
        self._apply_global_ratio()
        self.params = BFieldTransportParams(
            base_name=self.ed_base.text().strip() or "bfield_transport",
            start_field_t=self.sp_start.value(), stop_field_t=self.sp_stop.value(),
            rate_t_per_min=self.sp_rate.value(), round_trip=self.chk_round_trip.isChecked(),
            ratio=float(self.sp_ratio.value()),
            ratio_target=normalize_ratio_target(self.cbo_ratio_target.currentData() or RATIO_TARGET_VBG),
            cooldown_policy=normalize_cooldown_policy(self.cbo_cooldown_policy.currentData() or "adaptive"),
            final_mode=normalize_final_mode(self.cbo_final_mode.currentData() or "driven"),
            acquisition_delay_s=self.sp_delay.value(), averages=self.sp_averages.value(),
            conditions=deepcopy(self.conditions),
        )
        self._save_settings()
        return deepcopy(self.params)

    def validate_setup(self):
        params = self.collect_params()
        result = validate_setup(params.start_field_t, params.stop_field_t, params.rate_t_per_min)
        if not any(condition.enabled for condition in params.conditions):
            raise BFieldTransportSafetyError("Enable at least one fixed transport condition")
        return result

    def _preview_params(self):
        try:
            conditions = self._collect_conditions()
        except Exception:
            conditions = deepcopy(self.conditions)
        return BFieldTransportParams(
            base_name=self.ed_base.text().strip() or "bfield_transport",
            start_field_t=self.sp_start.value(),
            stop_field_t=self.sp_stop.value(),
            rate_t_per_min=self.sp_rate.value(),
            round_trip=self.chk_round_trip.isChecked(),
            conditions=conditions,
            final_mode=normalize_final_mode(self.cbo_final_mode.currentData() or "persistent"),
        )

    def freeze_output_plan(self, params=None):
        """Build and retain the exact paths that the controller must use."""
        params = deepcopy(params) if params is not None else self._preview_params()
        signal_chain = self.get_signal_chain()
        planned = build_planned_output(
            self.save,
            "bfield_transport",
            params.base_name,
            transport_output_summary_parts(params, signal_chain),
            run_id=self._output_run_id,
        )
        self._output_run_id = planned.run_id
        self._planned_output = planned
        enabled = [condition for condition in params.conditions if condition.enabled]
        self._transport_output_paths = build_transport_output_paths(planned, enabled)
        return self._transport_output_paths

    def _output_warning(self, paths):
        warnings = []
        if not str(self.save.user or "").strip():
            warnings.append("Operator is blank")
        if not str(self.save.device_id or "").strip():
            warnings.append("Device ID is blank")
        if not str(self.save.base or "").strip():
            warnings.append("Data root is blank")
        existing = [os.path.basename(path) for path in paths.all_paths if os.path.exists(path)]
        if existing:
            warnings.append("output already exists: " + ", ".join(existing[:3]))
        return "; ".join(warnings)

    def validate_transport_output_ready(self, paths=None):
        paths = paths or self._transport_output_paths
        if paths is None:
            raise BFieldTransportSafetyError("Output plan is unavailable")
        missing = []
        if not str(self.save.user or "").strip():
            missing.append("Operator")
        if not str(self.save.device_id or "").strip():
            missing.append("Device ID")
        if not str(self.save.base or "").strip():
            missing.append("Data Root")
        if missing:
            raise BFieldTransportSafetyError(
                "Fill in required save settings before starting: " + ", ".join(missing)
            )
        existing = [path for path in paths.all_paths if os.path.exists(path)]
        if existing:
            raise BFieldTransportSafetyError(
                "Output file already exists. Change the filename stem or reset the preview: "
                + ", ".join(os.path.basename(path) for path in existing)
            )
        os.makedirs(paths.planned.output_dir, exist_ok=True)

    def refresh_output_preview(self, *_args):
        """Refresh the full series preview used by MainWindow save updates."""
        if not hasattr(self, "lbl_filename_preview"):
            return
        try:
            paths = self.freeze_output_plan()
            csv_names = [os.path.basename(path) for path in paths.condition_csv_paths]
            preview = csv_names[0] if len(csv_names) == 1 else f"{len(csv_names)} CSV files:\n" + "\n".join(csv_names[:3])
            if len(csv_names) > 3:
                preview += f"\n... {len(csv_names) - 3} more"
            self.lbl_filename_preview.setPlainText(preview or "No enabled condition CSV files")
            self.lbl_filename_preview.setToolTip("\n".join(paths.condition_csv_paths))
            self.lbl_path_preview.setPlainText(paths.planned.output_dir)
            self.lbl_path_preview.setToolTip(paths.planned.output_dir)
            self.lbl_metadata_preview.setPlainText(
                os.path.basename(paths.manifest_path) + "\n" + os.path.basename(paths.checkpoint_path)
            )
            self.lbl_metadata_preview.setToolTip(paths.manifest_path + "\n" + paths.checkpoint_path)
            self.lbl_log_preview.setPlainText(os.path.basename(paths.log_path))
            self.lbl_log_preview.setToolTip(paths.log_path)
            warning = self._output_warning(paths)
            self.lbl_output_warning.setText(warning)
            self._output_warning_row.setVisible(bool(warning))
        except Exception as exc:
            self.lbl_filename_preview.setPlainText(f"Invalid output preview: {exc}")

    def reset_output_preview(self):
        self._output_run_id = None
        self.refresh_output_preview()

    def set_sweep_locked(self, locked: bool):
        self._locked = bool(locked)
        for widget in (self.sp_start, self.sp_stop, self.sp_rate, self.chk_round_trip, self.condition_table,
                       self.cbo_condition_add_mode, self.ed_condition_add_first,
                       self.ed_condition_add_second, self.ed_condition_add_vds, self.btn_condition_add_preview,
                       self.btn_condition_update, self.btn_condition_duplicate,
                       self.btn_condition_remove, self.btn_condition_up, self.btn_condition_down):
            widget.setEnabled(not self._locked)
        self.btn_stop.setEnabled(self._locked)
        self.run_panel.set_running(self._locked)
        self.refresh_hardware_readiness()

    def _required_devices(self):
        required = ["daq", "g1", "g2"]
        if any(
            condition.enabled and condition.vds_source == "Keithley 2400"
            for condition in self.conditions
        ):
            required.append("g3")
        return required

    def _sync_measurement_statuses(self):
        for name in ("g1", "g2", "g3", "daq"):
            state = self.device_manager.state(name)
            detail = self.device_manager.detail(name) if state in {"err", "warn"} else None
            self.set_device_status(name, state, detail)
        self.refresh_hardware_readiness()

    def _on_device_status_changed(self, name, _state, _detail):
        if name in {"g1", "g2", "g3", "daq"}:
            self._sync_measurement_statuses()

    def _sync_aps100_status(self, *_args):
        controller = self._execution_controller
        magnet = getattr(controller, "magnet", None) if controller is not None else None
        if magnet is None or not bool(getattr(magnet, "is_connected", False)):
            self.set_device_status("aps100", "idle")
            self.refresh_hardware_readiness()
            return
        snapshot = getattr(magnet, "latest_snapshot", None)
        status = getattr(snapshot, "status", None)
        if bool(getattr(status, "quench", False)) or bool(
            getattr(status, "power_module_failure", False)
        ):
            self.set_device_status("aps100", "err", "APS100 reports a magnet or power-module fault")
        elif snapshot is None:
            self.set_device_status("aps100", "warn", "Connected, but no telemetry has been received")
        else:
            age = getattr(snapshot, "reading_age_s", None)
            if age is None and getattr(snapshot, "monotonic_s", None) is not None:
                age = time.monotonic() - float(snapshot.monotonic_s)
            if age is not None and (not math.isfinite(float(age)) or float(age) > 3.0):
                self.set_device_status("aps100", "warn", "APS100 telemetry is stale")
            else:
                self.set_device_status("aps100", "ok")
        self.refresh_hardware_readiness()

    def refresh_hardware_readiness(self, *_args):
        if not hasattr(self, "run_panel"):
            return
        blockers = []
        missing = [name.upper() for name in self._required_devices() if not self.device_manager.is_connected(name)]
        if missing:
            blockers.append("connect " + ", ".join(missing))
        controller = self._execution_controller
        magnet = getattr(controller, "magnet", None) if controller is not None else None
        if magnet is None or not bool(getattr(magnet, "is_connected", False)):
            blockers.append("connect APS100")
        else:
            snapshot = getattr(magnet, "latest_snapshot", None)
            age = getattr(snapshot, "reading_age_s", None) if snapshot is not None else None
            if age is None and snapshot is not None and getattr(snapshot, "monotonic_s", None) is not None:
                age = time.monotonic() - float(snapshot.monotonic_s)
            if snapshot is None or (age is not None and (not math.isfinite(float(age)) or float(age) > 3.0)):
                blockers.append("refresh APS100 telemetry")
        thermal = getattr(controller, "thermal_safety", None) if controller is not None else None
        if thermal is None or not bool(getattr(thermal, "is_armed", getattr(thermal, "armed", False))):
            blockers.append("commission Lake Shore safety")
        elif getattr(thermal, "latest_snapshot", None) is None:
            blockers.append("refresh Lake Shore telemetry")
        else:
            try:
                decision = thermal.evaluate(thermal.latest_snapshot)
                if decision is None or not bool(getattr(decision, "magnet_permission", False)):
                    blockers.append("Lake Shore safety hold")
            except Exception:
                blockers.append("Lake Shore safety evaluation failed")
        self.run_panel.set_start_available(not blockers)
        self.lbl_connection_hint.setText(
            "Hardware ready for B-field transport."
            if not blockers
            else "Not ready: " + "; ".join(blockers) + "."
        )

    def start_run(self):
        """Validate and freeze the recipe before a transport controller runs it."""
        if self._locked:
            return False
        try:
            validation = self.validate_setup()
        except Exception as exc:
            self.lbl_preview.setText(f"Cannot start: {exc}")
            self.lbl_preview.setProperty("role", "warning-hint")
            return False
        if self._execution_controller is not None:
            return bool(self._execution_controller.start())
        self.set_sweep_locked(True)
        self.lbl_preview.setText(
            f"Validated: {validation['rate_a_per_s']:.7f} A/s; "
            "transport controller is ready to execute the frozen recipe."
        )
        return True

    def stop_run(self):
        if self._execution_controller is not None and self._execution_controller.active:
            self._execution_controller.stop()
            return
        self.set_sweep_locked(False)
        self.lbl_preview.setText("Transport sweep stopped; recipe remains available for restart.")

    def set_execution_controller(self, controller):
        self._execution_controller = controller
        magnet = getattr(controller, "magnet", None)
        if magnet is not None:
            if hasattr(magnet, "connected"):
                magnet.connected.connect(self._sync_aps100_status)
            if hasattr(magnet, "disconnected"):
                magnet.disconnected.connect(self._sync_aps100_status)
            if hasattr(magnet, "snapshot_updated"):
                magnet.snapshot_updated.connect(self._sync_aps100_status)
            if hasattr(magnet, "fault"):
                magnet.fault.connect(
                    lambda message: self.set_device_status("aps100", "err", str(message))
                )
        self._sync_aps100_status()
        controller.error.connect(lambda message: self.lbl_preview.setText(f"Transport error: {message}"))
        controller.state_changed.connect(self._on_transport_state_changed)
        controller.finished.connect(self._on_transport_finished)
        controller.stopped.connect(self._on_transport_stopped)

    def _on_transport_state_changed(self, phase, detail):
        """Render controller phases in the run panel as well as the preview."""
        phase = str(phase)
        detail = str(detail or "")
        self.lbl_preview.setText(f"{phase}: {detail}" if detail else phase)
        state = phase if phase in {"configuring", "positioning", "biasing", "starting_sweep", "sweeping", "endpoint_hold", "transitioning", "cooldown", "thermal_hold", "thermal_warning", "resuming", "cleanup", "cleanup_overdue"} else "idle"
        self.set_status(detail or phase, state, detail)

    def _on_transport_finished(self):
        self.lbl_preview.setText("B-field Sweep complete")
        self.set_status("B-field Sweep complete", "finished")

    def _on_transport_stopped(self, message):
        text = str(message)
        self.lbl_preview.setText(text)
        self.set_status(text, "stopped")
