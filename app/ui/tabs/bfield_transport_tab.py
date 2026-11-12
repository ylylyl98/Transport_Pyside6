"""Continuous and stepped B-field transport sweep editor.

This tab intentionally has its own fixed-bias recipe.  Gate Scan's
trajectory editor remains available for its original workflow; transport rows
are fixed Doping/E-field operating points held while APS100 traverses B.
"""

from __future__ import annotations

from copy import deepcopy
from app.condition_names import unique_condition_name
import json
import math
import os
import re
import time

from PySide6 import QtCore, QtWidgets

from app.engine.bfield_transport_sweep import (
    COIL_CONSTANT_T_PER_A,
    MAX_DRIVEN_FIELD_T,
    aps100_transport_field_limit,
    MAX_RATE_A_PER_S,
    MAX_RATE_T_PER_MIN,
    COOLDOWN_POLICIES,
    COOLDOWN_POLICY_LABELS,
    FINAL_MODE_LABELS,
    build_transport_output_paths,
    normalize_cooldown_policy,
    parse_condition_series,
    transport_output_summary_parts,
    BFieldTransportSafetyError,
    validate_stepped_params,
)
from app.gate_transform import derived_to_gates, gates_to_derived
from app.gate_transform import RATIO_TARGET_VBG, RATIO_TARGET_VTG, ratio_formula_text, normalize_ratio_target
from app.models import BFIELD_HYSTERESIS_LABELS, BFIELD_MT_FIELDS
from app.models import BFieldTransportCondition, BFieldTransportParams, Connections, SaveRoot
from app.run_output import build_planned_output
from app.result_channels import plot_channel_options, compare_channel_options
from app.signal_chain import signal_chain_metadata
from app.settings import get_app_settings
from app.ui.helpers import set_standard_input_height, style_form_layout
from app.ui.tabs.base_tab import BaseMeasurementTab
from app.ui.widgets.bias2_editor import Bias2Editor
from app.ui.widgets.safe_combo import SafeComboBox
from app.ui.widgets.expandable_line_edit import ExpandableLineEdit
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
        self._magnet_backend = "1000"
        super().__init__("START B-FIELD SWEEP", "B-field (T)", "Ids (A)", ["g1", "g2", "g3", "daq"])
        self.plot_choice = 'Ids_DC'
        self._update_plot_axis_choices()
        self.plot.y_axis_changed.connect(self.set_plot_axis_source)
        self.plot.plot_mode_changed.connect(lambda _mode: self._redraw_plot())
        self.device_manager.status_changed.connect(self._on_device_status_changed)
        self.device_manager.resources_changed.connect(self.refresh_hardware_readiness)
        self._sync_measurement_statuses()
        self._load_settings()
        self._refresh_conditions()
        self.btn_start.clicked.connect(self.start_run)
        self.btn_stop.clicked.connect(self.stop_run)

    def _update_plot_axis_choices(self):
        chain = self.get_signal_chain()
        chain = signal_chain_metadata(chain) if chain is not None else {}
        options = plot_channel_options('', chain, bias2=self.bias2_is_enabled())
        if self.plot_choice not in options:
            self.plot_choice = options[0] if options else ''
        self.plot.set_y_axis_options(options, self.plot_choice)
        dual = getattr(self.device_manager.connections, 'drag_drive_enabled', False)
        self.plot.set_compare_channels(compare_channel_options('', chain, bias2=self.bias2_is_enabled()), grid=dual)
        self.set_plot_axis_source(self.plot_choice)

    def set_plot_axis_source(self, source):
        self.plot_choice = source
        self.plot.set_selected_y_axis(source)
        self._redraw_plot()

    def _redraw_plot(self):
        if self._execution_controller is not None and hasattr(self._execution_controller, "refresh_plot"):
            self._execution_controller.refresh_plot(force=True)
        else:
            channels = (self.plot.compare_channels() if self.plot.current_plot_mode() == '4-Channel Compare'
                        else [self.plot_choice])
            for axis, channel in zip(self.plot.get_axes(), channels):
                axis.set_ylabel(f'{channel} (A)')
            for axis in self.plot.bottom_axes():
                axis.set_xlabel('B-field (T)')
            self.plot.format_compare_axes()
            self.plot.canvas.draw_idle()

    @staticmethod
    def _spin(value=0.0, minimum=-1000.0, maximum=1000.0, decimals=6):
        spin = SafeDoubleSpinBox()
        spin.setRange(minimum, maximum)
        spin.setDecimals(decimals)
        spin.setValue(value)
        set_standard_input_height(spin)
        return spin

    def _build_control_panel(self, ctl_layout):
        self.restoration_banner = QtWidgets.QLabel()
        self.restoration_banner.setWordWrap(True)
        self.restoration_banner.setStyleSheet("background:#fff0ed;color:#9c2016;padding:8px;font-weight:600;border:1px solid #c94936;")
        self.restoration_banner.hide()
        ctl_layout.addWidget(self.restoration_banner)
        self.ls_current_reversal_label = QtWidgets.QLabel("LS335 current reversal: unknown")
        self.ls_current_reversal_label.setWordWrap(True)
        ctl_layout.addWidget(self.ls_current_reversal_label)
        ctl_layout.addWidget(SectionHeader("1. Sweep range"))
        group = QtWidgets.QGroupBox("Driven-mode sweep")
        form = QtWidgets.QFormLayout(group)
        self._sweep_form = form
        style_form_layout(form)
        self.sp_start = self._spin(-0.5, -aps100_transport_field_limit(), aps100_transport_field_limit())
        self.sp_stop = self._spin(0.5, -aps100_transport_field_limit(), aps100_transport_field_limit())
        self.sp_rate = self._spin(0.1, 1e-6, MAX_RATE_T_PER_MIN, 6)
        self.sp_rate_a = QtWidgets.QLabel()
        self.sp_rate_t_s = QtWidgets.QLabel()
        self.chk_round_trip = QtWidgets.QCheckBox("Round trip: start → stop → start")
        self.chk_round_trip.setChecked(True)
        form.addRow("Start (T):", self.sp_start)
        form.addRow("Stop (T):", self.sp_stop)
        self.lbl_rate_limit = QtWidgets.QLabel()
        self.lbl_rate_limit.setWordWrap(True)
        self.lbl_rate_limit.setProperty("role", "hint")
        form.addRow("", self.lbl_rate_limit)
        form.addRow("Maximum sweep rate (T/min):", self.sp_rate)
        form.addRow("APS100 rate:", self.sp_rate_a)
        form.addRow("Field rate:", self.sp_rate_t_s)
        for widget in (self.sp_rate, self.sp_rate_a, self.sp_rate_t_s):
            form.setRowVisible(widget, True)
        form.addRow("", self.chk_round_trip)
        self.cbo_measurement_mode = SafeComboBox()
        self.cbo_measurement_mode.addItem("Continuous", "continuous")
        self.cbo_measurement_mode.addItem("Stepped", "stepped")
        self.cbo_field_spacing = SafeComboBox()
        self.cbo_field_spacing.addItem("Point count", "points")
        self.cbo_field_spacing.addItem("Step size", "step")
        self.cbo_field_spacing.setCurrentIndex(1)
        self.sp_field_points = SafeSpinBox()
        self.sp_field_points.setRange(2, 10000)
        self.sp_field_points.setValue(11)
        self.sp_field_points.setReadOnly(True)
        self.sp_field_points.setButtonSymbols(QtWidgets.QAbstractSpinBox.ButtonSymbols.NoButtons)
        self.sp_field_step = self._spin(100, 0.001, 12000, 6)
        self.sp_field_tolerance = self._spin(self.params.field_tolerance_t * 1000, 0.1, 1000, 4)
        self.sp_field_stability = self._spin(self.params.field_stability_s, 0.1, 86400, 2)
        self.sp_field_settle = self._spin(self.params.field_settle_s, 0, 86400, 2)
        self._hysteresis_extra = {}
        for key in BFIELD_HYSTERESIS_LABELS:
            if key in {"field_tolerance_t", "field_stability_s", "field_settle_s"}:
                continue
            scale = 1000 if key in BFIELD_MT_FIELDS else 1
            self._hysteresis_extra[key] = self._spin(getattr(self.params, key) * scale,
                0 if key == "field_max_gap_t" else 0.0001, 86400, 4)
        self._hysteresis_inputs = (self.sp_field_tolerance, self.sp_field_stability, self.sp_field_settle, *self._hysteresis_extra.values())
        self.sp_field_tolerance.setToolTip("Fine approach stopping threshold. Stationary points use separate point and endpoint acceptance limits; no backwards correction.")
        self.sp_field_stability.setToolTip("Require fresh stationary readings satisfying the independent span and drift limits throughout this window.")
        self.sp_field_settle.setToolTip("Additional delay after field and temperature qualify. Any loss of stability restarts the delay.")
        self.cbo_sample_reversal = SafeComboBox()
        for label, value in (("Unchanged", "unchanged"), ("Per point", "per_point"), ("Whole sweep", "whole_sweep")):
            self.cbo_sample_reversal.addItem(label, value)
        self.sp_sample_reversal_settle = self._spin(2.0, 0, 86400, 3)
        explanation = ("Per point: B ON to qualify temperature, OFF and wait, acquire, then ON and requalify. "
                       "Whole sweep: B OFF throughout. Wait is configured, not a verified transient. "
                       "Sample target stability remains required. A remains ON; magnet safety remains active.")
        for widget in (self.cbo_sample_reversal, self.sp_sample_reversal_settle):
            widget.setToolTip(explanation)
        self.sp_temperature_target = self._spin(4.0, 0.001, 300, 4)
        self.sp_temperature_tolerance = self._spin(0.05, 0.000001, 300, 6)
        self.sp_temperature_window = self._spin(10, 0.01, 86400, 2)
        self.sp_temperature_span = self._spin(0.02, 0.000001, 300, 6)
        self.sp_temperature_drift = self._spin(0.01, 0.000001, 300, 6)
        self.sp_temperature_timeout = self._spin(600, 0.01, 86400, 2)
        self._step_inputs = (self.cbo_measurement_mode, self.cbo_field_spacing, self.sp_field_points,
            self.sp_field_step, *self._hysteresis_inputs, self.cbo_sample_reversal, self.sp_sample_reversal_settle, self.sp_temperature_target, self.sp_temperature_tolerance,
            self.sp_temperature_window, self.sp_temperature_span, self.sp_temperature_drift, self.sp_temperature_timeout)
        for label, widget in (("Measurement mode:", self.cbo_measurement_mode), ("Spacing:", self.cbo_field_spacing),
            ("Points / pass (calculated):", self.sp_field_points), ("Step size (mT):", self.sp_field_step),
            ("Approach stopping tolerance (±mT):", self.sp_field_tolerance),
            ("Field stability window (s):", self.sp_field_stability),
            ("Delay after stability (s):", self.sp_field_settle),
            *((BFIELD_HYSTERESIS_LABELS[k] + ":", w) for k, w in self._hysteresis_extra.items()),
            ("Sample B current reversal:", self.cbo_sample_reversal),
            ("Reversal settling wait (s):", self.sp_sample_reversal_settle),
            ("Sample target (K):", self.sp_temperature_target), ("Target tolerance (K):", self.sp_temperature_tolerance),
            ("Stability window (s):", self.sp_temperature_window), ("Maximum window span (K):", self.sp_temperature_span),
            ("Maximum absolute drift (K/min):", self.sp_temperature_drift), ("Wait timeout (s):", self.sp_temperature_timeout)):
            form.addRow(label, widget)
        form.setRowVisible(self.cbo_field_spacing, False)
        hint = QtWidgets.QLabel("Stepped: one fixed sample target; passive waiting only. Values require hardware commissioning. Timeout pauses the run.")
        self._step_hint = hint
        hint.setWordWrap(True)
        form.addRow("", hint)
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
        self.cbo_final_mode.addItem(FINAL_MODE_LABELS["driven"], "driven")
        self.cbo_final_mode.setEnabled(False)
        self.cbo_final_mode.setToolTip(
            "Successful B-field sweeps always leave the heater ON at the final field. Stops, errors, "
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

        ctl_layout.addWidget(SectionHeader("2. Fixed biases"))
        intro = QtWidgets.QLabel(
            "Sweep the magnetic field while measuring current at fixed doping and E-field conditions. "
            "Each row fixes both doping and E-field for one complete trajectory; "
            "Vtg/Vbg are calculated from the global gate weighting ratio."
        )
        intro.setWordWrap(True)
        intro.setProperty("role", "hint")
        ctl_layout.addWidget(intro)
        self.condition_table = QtWidgets.QTableWidget(0, 13)
        self.condition_table.setHorizontalHeaderLabels(
            ["", "Name", "Dop\ning", "E-\nfield", "Vds", "Sour\nce", "AO", "Vtg", "Vbg", "Bias2 mode", "Bias2 fixed", "Offset", "Ramp"]
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
        for column in range(9, 13):
            self.condition_table.setColumnHidden(column, True)
        self.condition_table.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.condition_table.setToolTip(
            "All condition columns fit without horizontal scrolling. Select a row to see its "
            "complete, unabridged values below the table."
        )
        self.condition_table.itemChanged.connect(self._condition_edited)
        self.condition_table.itemSelectionChanged.connect(self._refresh_condition_details)
        ctl_layout.addWidget(self.condition_table)
        self.condition_bias2_editor = Bias2Editor()
        self.condition_bias2_editor.setTitle("Selected condition - Bias 2")
        self._updating_condition_bias2 = False
        self.condition_bias2_editor.changed.connect(self._condition_bias2_changed)
        self.condition_table.itemSelectionChanged.connect(self._load_condition_bias2)
        ctl_layout.addWidget(self.condition_bias2_editor)
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
        self.lbl_condition_add_first = QtWidgets.QLabel("Doping:")
        self.ed_condition_add_first = ExpandableLineEdit("0", title="Edit first coordinate values", allow_newlines=True)
        self.lbl_condition_add_second = QtWidgets.QLabel("E-field:")
        self.ed_condition_add_second = ExpandableLineEdit("0", title="Edit second coordinate values", allow_newlines=True)
        set_standard_input_height(self.ed_condition_add_first)
        set_standard_input_height(self.ed_condition_add_second)
        self.ed_condition_add_first.setPlaceholderText("0, 1, 2 | (0, 2, 3) | 0:2:1")
        self.ed_condition_add_second.setPlaceholderText("single value or matching list/range")
        builder_layout.addWidget(self.lbl_condition_add_first)
        builder_layout.addWidget(self.ed_condition_add_first.field_widget())
        builder_layout.addWidget(self.lbl_condition_add_second)
        builder_layout.addWidget(self.ed_condition_add_second.field_widget())
        builder_layout.addWidget(QtWidgets.QLabel("Vds (V):"))
        self.ed_condition_add_vds = ExpandableLineEdit("0.1", title="Edit Vds values (V)", allow_newlines=True)
        set_standard_input_height(self.ed_condition_add_vds)
        builder_layout.addWidget(self.ed_condition_add_vds.field_widget())
        syntax_hint = QtWidgets.QLabel(
            "Single: 0 | List: -1,1,5 | Points: (-1,1,5) | Step: -1:1:0.5. "
            "Ranges include both endpoints; the final step may be shorter. "
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

        ctl_layout.addWidget(SectionHeader("3. Acquisition and waiting"))
        acq = QtWidgets.QGroupBox("Acquisition")
        acq_form = QtWidgets.QFormLayout(acq)
        style_form_layout(acq_form)
        self.sp_delay = self._spin(0.1, 0.0, 60.0, 3)
        self.sp_averages = SafeSpinBox()
        self.sp_averages.setRange(1, 10000)
        self.sp_averages.setValue(1)
        self.ed_base = ExpandableLineEdit(self.params.base_name, title="Edit filename stem")
        acq_form.addRow("Delay (s):", self.sp_delay)
        acq_form.addRow("Averages:", self.sp_averages)
        ctl_layout.addWidget(acq)
        ctl_layout.addWidget(SectionHeader("4. Output files"))
        output_wrap = QtWidgets.QWidget()
        output_layout = QtWidgets.QVBoxLayout(output_wrap)
        output_layout.setContentsMargins(0, 0, 0, 0)
        file_form = QtWidgets.QFormLayout()
        file_form.addRow(QtWidgets.QLabel("Filename stem:"))
        file_form.addRow(self.ed_base.field_widget())
        output_layout.addLayout(file_form)
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
        for widget in self._step_inputs:
            signal = widget.currentIndexChanged if isinstance(widget, QtWidgets.QComboBox) else widget.valueChanged
            signal.connect(self._refresh_rate_preview)
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
        for key, widget in (("measurement_mode", self.cbo_measurement_mode), ("sample_reversal_mode", self.cbo_sample_reversal)):
            widget.setCurrentIndex(max(0, widget.findData(str(settings.value(key, getattr(self.params, key))))))
        for key, widget in self._step_numeric_inputs():
            value = settings.value(key, getattr(self.params, key))
            widget.setValue(int(value) if key == "field_points" else float(value) * (1000 if key in BFIELD_MT_FIELDS else 1))
        self.sp_start.setValue(float(settings.value("start_field_t", self.params.start_field_t)))
        self.sp_stop.setValue(float(settings.value("stop_field_t", self.params.stop_field_t)))
        if str(settings.value("field_spacing", self.params.field_spacing)) == "points":
            count = int(settings.value("field_points", self.params.field_points))
            if count >= 2:
                self.sp_field_step.setValue(abs(self.sp_stop.value() - self.sp_start.value()) * 1000 / (count - 1))
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
        settings.setValue("final_mode", "driven")
        self.cbo_final_mode.setCurrentIndex(0)
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

    def _step_numeric_inputs(self):
        return (("sample_reversal_settle_s", self.sp_sample_reversal_settle), ("field_points", self.sp_field_points), ("field_step_t", self.sp_field_step),
            ("field_tolerance_t", self.sp_field_tolerance), ("field_stability_s", self.sp_field_stability),
            ("field_settle_s", self.sp_field_settle), *self._hysteresis_extra.items(),
            ("temperature_target_k", self.sp_temperature_target), ("temperature_tolerance_k", self.sp_temperature_tolerance),
            ("temperature_window_s", self.sp_temperature_window), ("temperature_span_k", self.sp_temperature_span),
            ("temperature_drift_k_per_min", self.sp_temperature_drift), ("temperature_timeout_s", self.sp_temperature_timeout))

    def _step_values(self):
        return {"measurement_mode": self.cbo_measurement_mode.currentData(),
                "sample_reversal_mode": self.cbo_sample_reversal.currentData(),
                "field_spacing": "step",
                **{key: widget.value() / 1000 if key in BFIELD_MT_FIELDS else widget.value()
                   for key, widget in self._step_numeric_inputs()}}

    def _save_settings(self):
        settings = get_app_settings()
        settings.beginGroup(self.SETTINGS_PREFIX)
        for key, value in self._step_values().items():
            settings.setValue(key, value)
        settings.setValue("start_field_t", self.sp_start.value())
        settings.setValue("stop_field_t", self.sp_stop.value())
        settings.setValue("rate_t_per_min", self.sp_rate.value())
        settings.setValue("ratio", self.sp_ratio.value())
        settings.setValue("ratio_target", self.cbo_ratio_target.currentData() or RATIO_TARGET_VBG)
        settings.setValue("round_trip", self.chk_round_trip.isChecked())
        settings.setValue("cooldown_policy", self.cbo_cooldown_policy.currentData() or "adaptive")
        settings.setValue("final_mode", "driven")
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
        return ["✓" if condition.enabled else "", condition.name, f"{condition.doping:g}", f"{condition.efield:g}", f"{condition.vds:g}", source, str(condition.ao_channel), f"{condition.vtg:g}", f"{condition.vbg:g}", condition.bias2_mode, f"{condition.bias2_voltage:g}", f"{condition.bias2_offset:g}", f"{condition.bias2_ramp:g}"]

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
        self._load_condition_bias2()
        self._update_plot_axis_choices()
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
                bias2_mode=text(9), bias2_voltage=float(text(10)),
                bias2_offset=float(text(11)), bias2_ramp=float(text(12)),
            )
            if condition.bias2_mode not in ("Disabled", "Fixed", "Follow"):
                raise ValueError("Bias2 mode must be Disabled, Fixed or Follow")
            condition.refresh_gates()
            return condition
        except (TypeError, ValueError) as exc:
            raise BFieldTransportSafetyError(f"Condition row {row + 1}: {exc}") from exc

    def _load_condition_bias2(self):
        row = self._selected_row()
        if row < 0 and self.condition_table.rowCount():
            row = 0
        self.condition_bias2_editor.setEnabled(row >= 0 and not self._locked)
        if row >= 0:
            try:
                condition = self._condition_from_row(row)
            except BFieldTransportSafetyError:
                return
            self._updating_condition_bias2 = True
            try:
                self.condition_bias2_editor.set_values(condition)
            finally:
                self._updating_condition_bias2 = False

    def _condition_bias2_changed(self):
        if self._updating_condition_bias2:
            return
        row = self._selected_row()
        if row < 0 and self.condition_table.rowCount():
            row = 0
        if row < 0:
            return
        for column, value in enumerate(self.condition_bias2_editor.values().values(), start=9):
            self.condition_table.item(row, column).setText(str(value))

    def bias2_is_enabled(self):
        try:
            conditions = self._collect_conditions() if hasattr(self, 'condition_table') else self.conditions
        except (ValueError, BFieldTransportSafetyError):
            conditions = self.conditions
        return any(c.enabled and c.bias2_mode != 'Disabled' for c in conditions)

    def _collect_conditions(self):
        return [self._condition_from_row(row) for row in range(self.condition_table.rowCount())]

    def _condition_edited(self, item):
        try:
            self.conditions[item.row()] = self._condition_from_row(item.row())
        except (ValueError, BFieldTransportSafetyError):
            pass
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
        self._update_plot_axis_choices()

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
        self.conditions[row + 1].name = unique_condition_name(
            (condition.name for condition in self.conditions), self.conditions[row].name)
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
        from app.engine.bfield_transport_sweep import effective_rate_limit_t_per_min
        try:
            limit = effective_rate_limit_t_per_min()
            limit_text = f"{limit * 10000 / 60:g} Oe/s ({limit:g} T/min)"
        except (ValueError, TypeError):
            limit_text = "unavailable; check magnet configuration"
        self.lbl_rate_limit.setText(
            f"Configured maximum: {limit_text}. "
            f"Current request: {self.sp_rate.value() * 10000 / 60:g} Oe/s. "
            "Stored range/voltage limits and final approach may be slower.")
        hysteresis = self._magnet_backend == "1000" and self.cbo_measurement_mode.currentData() == "stepped"
        for widget in self._hysteresis_inputs:
            self._sweep_form.setRowVisible(widget, hysteresis)
        self._sweep_form.labelForField(self.sp_temperature_timeout).setText(
            "Temperature qualification timeout (s):" if hysteresis else "Wait timeout (s):")
        self._step_hint.setText(
            "Stepped: directional approach → stable field and temperature → extra delay → measure. "
            "Stable offsets use readback fields; history, endpoint or gap violations end the run. Gap 0 = twice the largest planned interval. Sample heater stays OFF."
            if self._magnet_backend == "1000" else
            "Stepped: SDK sets and enables the sample temperature target; SDK manages the field ramp.")
        try:
            from app.engine.bfield_transport_sweep import TransportSweepPlan
            TransportSweepPlan.from_params(self._preview_params())
            step_detail = ""
            if self.cbo_measurement_mode.currentData() == "stepped":
                points = validate_stepped_params(self._preview_params())
                blocker = QtCore.QSignalBlocker(self.sp_field_points)
                self.sp_field_points.setValue(len(points))
                del blocker
                total = len(points) * (2 if self.chk_round_trip.isChecked() else 1) - (1 if self.chk_round_trip.isChecked() else 0)
                step_detail = f"{len(points)} points / pass; {total} points / condition; "
                if hysteresis:
                    step_detail += (f"field ±{self.sp_field_tolerance.value():g} mT for {self.sp_field_stability.value():g} s; "
                                    f"extra delay {self.sp_field_settle.value():g} s; ")
                last_step = abs(points[-1] - points[-2]) * 1000
                if last_step < self.sp_field_step.value() - 1e-9:
                    step_detail += f"final interval {last_step:g} mT (shorter); "
            planned = build_planned_output(
                self.save, "bfield_transport", self.ed_base.text().strip() or "bfield_transport",
                run_id="preview", create_dir=False,
            )
            enabled = sum(1 for condition in self.conditions if condition.enabled)
            self.lbl_preview.setText(
                "Ready: " + step_detail + ("round trip" if self.chk_round_trip.isChecked() else "one-way")
                + f" {self.sp_start.value():+.4g} → {self.sp_stop.value():+.4g} T; "
                + f"{enabled} enabled condition(s); "
                + ("APS100 maximum rate; stored ranges may be slower; " if self._magnet_backend == "1000" else "SDK-managed ramp; ")
                + f"output preview: {planned.output_dir}"
            )
            self.lbl_preview.setProperty("role", "hint")
            self.lbl_time_estimate.setText(
                "Temperature waiting is variable. " + ("Field timing is calculated from the instrument's rate table at Start."
                if self._magnet_backend == "1000" else "Field ramp timing is managed by the SDK.")
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
            final_mode="driven", magnet_backend=self._magnet_backend,
            acquisition_delay_s=self.sp_delay.value(), averages=self.sp_averages.value(),
            conditions=deepcopy(self.conditions),
            **self._step_values(),
        )
        self._save_settings()
        return deepcopy(self.params)

    def validate_setup(self):
        params = self.collect_params()
        from app.engine.bfield_transport_sweep import TransportSweepPlan
        result = dict(TransportSweepPlan.from_params(params).validation)
        if not any(condition.enabled for condition in params.conditions):
            raise BFieldTransportSafetyError("Enable at least one fixed transport condition")
        from app.bias_control import validate_bias2
        for condition in params.conditions:
            if condition.enabled:
                validate_bias2(condition, [condition.vds], self.device_manager)
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
            **self._step_values(),
            final_mode="driven", magnet_backend=self._magnet_backend,
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
            freeze=self._output_run_id is not None,
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
        for widget in (*self._step_inputs, self.sp_start, self.sp_stop, self.sp_rate, self.chk_round_trip, self.condition_table, self.condition_bias2_editor,
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

    def _aps100_readiness_max_age_s(self):
        """Allow idle polling plus read latency; retain the active-run limit."""
        if self._locked or bool(getattr(self._execution_controller, "active", False)):
            return 3.0
        poll = max(0.1, float(cfg.magnet.poll_interval_s))
        stationary = max(poll, float(cfg.magnet.stationary_poll_interval_s))
        read_margin = max(2.0, float(cfg.magnet.timeout_ms) / 1000.0 + poll)
        return max(3.0, stationary + read_margin)

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
            if age is not None and (not math.isfinite(float(age)) or float(age) < 0 or float(age) > self._aps100_readiness_max_age_s()):
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
        if self._magnet_backend == "2100":
            state = getattr(getattr(magnet, "state", None), "name", "")
            if state not in ("IDLE", "ARMED", "ACTIVE"):
                blockers.append("connect attoDRY2100 SDK")
            if getattr(magnet, "has_pending_work", False) or getattr(magnet, "exclusive_owner", None):
                blockers.append("wait for attoDRY2100 operation")
            if self.cbo_measurement_mode.currentData() != "stepped":
                blockers.append("select stepped measurement")
            self.run_panel.set_start_available(not blockers)
            self.lbl_connection_hint.setText("Not ready: " + "; ".join(blockers) + "." if blockers else "Hardware ready for B-field transport.")
            return
        if magnet is None or not bool(getattr(magnet, "is_connected", False)):
            blockers.append("connect APS100")
        else:
            snapshot = getattr(magnet, "latest_snapshot", None)
            age = getattr(snapshot, "reading_age_s", None) if snapshot is not None else None
            if age is None and snapshot is not None and getattr(snapshot, "monotonic_s", None) is not None:
                age = time.monotonic() - float(snapshot.monotonic_s)
            if snapshot is None or (age is not None and (not math.isfinite(float(age)) or float(age) < 0 or float(age) > self._aps100_readiness_max_age_s())):
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
                    blockers.append("Lake Shore safety hold: " + str(getattr(decision, "reason", "no decision")))
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
        if cfg.setup.magnet_backend == "none":
            self.lbl_preview.setText("Cannot start: no magnet is configured")
            return False
        if self._execution_controller is not None and self._execution_controller.active:
            self._execution_controller.continue_after_temperature_check()
            return False
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
            ("Validated: APS100 maximum rate; stored ranges may be slower; " if self._magnet_backend == "1000" else "Validated: SDK-managed ramp; ") +
            "transport controller is ready to execute the frozen recipe."
        )
        return True

    def stop_run(self):
        if self._execution_controller is not None and self._execution_controller.active:
            self._execution_controller.stop()
            return
        self.set_sweep_locked(False)
        self.lbl_preview.setText("Transport sweep stopped; recipe remains available for restart.")

    def set_ls_current_reversal_summary(self, text):
        self.ls_current_reversal_label.setText(str(text))

    def set_magnet_backend(self, backend):
        self._magnet_backend = str(backend)
        aps = self._magnet_backend == "1000"
        self.ls_current_reversal_label.setVisible(aps)
        if aps:
            limit = aps100_transport_field_limit()
        else:
            from app.devices.attodry2100_adapter import HARD_MAX_FIELD_T
            configured = getattr(cfg.attodry2100, "maximum_field_t", None)
            limit = min(float(configured), HARD_MAX_FIELD_T) if configured is not None else HARD_MAX_FIELD_T
        for spin in (self.sp_start, self.sp_stop):
            spin.setRange(-limit, limit)
        for widget in (self.sp_rate, self.sp_rate_a, self.sp_rate_t_s):
            self._sweep_form.setRowVisible(widget, aps)
        for widget in (self.lbl_rate_limit, self.cbo_cooldown_policy, self.cbo_final_mode):
            self._sweep_form.setRowVisible(widget, aps)
        self._step_hint.setText("Stepped: one fixed sample target; passive waiting only." if aps else
                                "Stepped: SDK sets and enables the sample temperature target; SDK manages the field ramp.")
        self._refresh_rate_preview()
        self.refresh_hardware_readiness()

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
        if hasattr(controller, "restoration_warning"):
            controller.restoration_warning.connect(self._show_restoration_warning)
        controller.error.connect(lambda message: self.lbl_preview.setText(f"Transport error: {message}"))
        controller.state_changed.connect(self._on_transport_state_changed)
        if hasattr(controller, "continuation_changed"):
            controller.continuation_changed.connect(self.run_panel.set_continuation)
        controller.finished.connect(self._on_transport_finished)
        controller.stopped.connect(self._on_transport_stopped)

    def _show_restoration_warning(self, message):
        self.restoration_banner.setText("Instrument settings need attention: " + str(message))
        self.restoration_banner.setVisible(bool(message))
        if message:
            self.set_status("APS100 settings restoration failed - see warning", "error")
            dialog = getattr(self, "_restoration_dialog", None)
            if dialog is None:
                dialog = QtWidgets.QMessageBox(self)
                dialog.setWindowTitle("APS100 settings need attention")
                dialog.setIcon(QtWidgets.QMessageBox.Icon.Warning)
                dialog.setTextFormat(QtCore.Qt.TextFormat.PlainText)
                self._restoration_dialog = dialog
            dialog.setText(str(message))
            dialog.open()  # Cleanup must continue while the notice is visible.

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
