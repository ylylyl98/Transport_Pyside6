from __future__ import annotations

import json
from PySide6 import QtCore, QtWidgets

from app.constants import GATE_BIAS_RAMP_STEP_T, GATE_BIAS_RAMP_STEP_V, SAFE_RAMP_STEP_T, SAFE_RAMP_STEP_V
from app.device_manager import DeviceManager
from app.cosweep_output import map_filename_parts
from app.gate_transform import (
    RATIO_TARGET_VBG,
    RATIO_TARGET_VTG,
    gates_to_derived,
    doping_axis_label,
    efield_axis_label,
    normalize_ratio_target,
    ratio_formula_text,
)
from app.models import CoParams, Connections, SaveRoot
from app.plot_ranges import coordinate_ranges
from app.settings import get_app_settings
from app.plot_x_axis import (
    FOLLOW_SWEEP,
    PLOT_X_AXES,
    normalize_plot_x_selection,
    plot_x_axis_label,
    record_x_value,
    resolve_map_x_axis,
)
from app.result_channels import compare_channel_options, plot_channel_options, plot_channel_value
from app.run_output import build_planned_output, planned_output_warning
from app.signal_chain import SignalChainSnapshot, signal_chain_metadata
from app.ui.helpers import apply_tooltip, configure_volt_spinbox, flash_button_success, set_standard_input_height, style_form_layout
from app.ui.tabs.base_tab import BaseMeasurementTab, run_filename_snapshot
from app.ui.widgets.plot_widget import PlotWidget, preserve_plot_view
from app.ui.widgets.collapsible_section import CollapsibleSection
from app.ui.widgets.expandable_line_edit import ExpandableLineEdit
from app.ui.widgets.safe_combo import SafeComboBox
from app.ui.widgets.safe_spinbox import SafeDoubleSpinBox, SafeSpinBox, TrimmedDoubleSpinBox
from app.ui.widgets.status_panel import SectionHeader, StatusPanel
from app.utils import _frange_inc, safe_ramp
from app.workers.cosweep import CoSweepWorker, build_cosweep_points, validate_cosweep_params, sequence_point_count
from app.voltage_resolution import source_resolution, validate_voltage_points
from app.workers.cosweep_timing import (
    estimate_cosweep_seconds, format_cosweep_duration, historical_cosweep_match,
    estimate_cosweep_cleanup_seconds, LiveCoSweepTiming, cosweep_calibration_description,
)

SET_BUTTON_WIDTH = 48
COSWEEP_PANEL_MIN_WIDTH = 380
COSWEEP_PANEL_MAX_WIDTH = 560


class CoSweepTab(BaseMeasurementTab):
    SETTINGS_PREFIX = "tabs/map_2d"

    def __init__(self, save: SaveRoot, conns: Connections, device_manager: DeviceManager, get_global_rates_callable=None, get_ao_items_callable=None, get_signal_chain_callable=None):
        self.save = save
        self.conns = conns
        self.device_manager = device_manager
        self.get_global_rates = get_global_rates_callable or (lambda: (1e7, 100.0))
        self.get_signal_chain = get_signal_chain_callable or SignalChainSnapshot
        self.get_ao_items = get_ao_items_callable or (lambda: ["ao0", "ao1"])
        self.p = CoParams()
        self._regions = []
        self.s_g1 = self.s_g2 = self.s_g3 = self.s_daq = None
        self.worker_thread = None
        self.worker = None
        self._updating_combos = False
        self._last_coordinate_mode = "Raw"
        self._axis_memory = {
            "Raw": ("Vtg", "Vbg"),
            "Derived": ("Doping", "E-field"),
        }
        self._preferred_slow_axis = None
        self._plot_records = []
        self._output_run_id = None
        self._planned_output = None
        super().__init__("START SWEEP", "Fast Axis", "Ids (A)", ["g1", "g2", "g3", "daq"])
        self.plot_tabs = QtWidgets.QTabWidget()
        self.preview_plot = PlotWidget()
        self.preview_plot.btn_plot_mode.hide()
        self.preview_plot.btn_x_range.hide()
        self.preview_plot.setMinimumHeight(300)
        self.plot_splitter.replaceWidget(0, self.plot_tabs)
        self.plot_tabs.addTab(self.preview_plot, "Sweep Preview")
        self.plot_tabs.addTab(self.plot, "Measurement")
        self.control_scroll.setMinimumWidth(COSWEEP_PANEL_MIN_WIDTH)
        self.control_scroll.setMaximumWidth(COSWEEP_PANEL_MAX_WIDTH)
        self.main_splitter.setSizes([430, 830])
        self._live_eta = None
        self._eta_outcome = "Running"
        self._eta_detail = ""
        self.lbl_eta = QtWidgets.QLabel("ETA (estimated total): --")
        self.lbl_eta.setWordWrap(True)
        eta_font = self.lbl_eta.font()
        eta_font.setBold(True)
        self.lbl_eta.setFont(eta_font)
        self.run_panel.layout().insertWidget(1, self.lbl_eta)
        self._wire()
        self.btn_start.setToolTip("Connect instruments first")
        self.device_manager.status_changed.connect(self._on_device_status_changed)
        self.device_manager.operation_changed.connect(self._on_operation_changed)
        self.device_manager.protection_changed.connect(lambda *_: self._update_sweep_summary())
        self.device_manager.gate_currents_read.connect(self._on_precision_readback)
        self._sync_sessions_from_manager()
        self._load_tab_settings()
        self._bind_tab_settings()
        self._update_manual_buttons()
        self.on_fast_combo_changed()

    def _build_control_panel(self, ctl_layout: QtWidgets.QVBoxLayout):
        ctl_layout.addWidget(SectionHeader("1. Sweep mode"))
        grp_setup = QtWidgets.QGroupBox("Sweep Setup")
        form_setup = QtWidgets.QFormLayout(grp_setup)
        style_form_layout(form_setup)
        self.cbo_sweep_dim = SafeComboBox()
        self.cbo_sweep_dim.addItems(["1D sweep", "2D map"])
        self.cbo_sweep_dim.setCurrentText("2D map")
        self.cbo_coordinates = SafeComboBox()
        self.cbo_coordinates.addItem("Raw voltages", "Raw")
        self.cbo_coordinates.addItem("Doping / E-field", "Derived")
        self.chk_link = QtWidgets.QCheckBox("Plot as Doping/E-field axes (display only)")
        self.chk_link.setToolTip("Raw mode only: changes preview and plotted axes without changing the hardware trajectory.")
        self.cbo_fast = SafeComboBox()
        self.cbo_fast.addItems(["Vtg", "Vbg", "Vds"])
        self.cbo_slow = SafeComboBox()
        self.cbo_slow.addItems(["None", "Vtg", "Vbg", "Vds"])
        self.cbo_slow.setCurrentText("Vbg")
        self.cbo_source = SafeComboBox()
        self.cbo_source.addItems(["Keithley 2400"])
        self.sp_ratio = SafeDoubleSpinBox()
        self.sp_ratio.setDecimals(4)
        self.sp_ratio.setRange(-1e4, 1e4)
        self.sp_ratio.setValue(1.0)
        self.cbo_ratio_target = SafeComboBox()
        self.cbo_ratio_target.addItem("Vbg (back gate)", RATIO_TARGET_VBG)
        self.cbo_ratio_target.addItem("Vtg (top gate)", RATIO_TARGET_VTG)
        self.lbl_sweep_summary = QtWidgets.QLabel()
        self.lbl_sweep_summary.setWordWrap(True)
        self.lbl_sweep_summary.setProperty("role", "hint")
        self.lbl_sweep_summary.setMinimumWidth(0)
        self.lbl_sweep_summary.setSizePolicy(
            QtWidgets.QSizePolicy.Policy.Ignored,
            QtWidgets.QSizePolicy.Policy.Preferred,
        )
        self.lbl_ratio_formula = QtWidgets.QLabel()
        self.lbl_ratio_formula.setWordWrap(True)
        self.lbl_ratio_formula.setMinimumWidth(0)
        self.lbl_ratio_formula.setProperty("role", "hint")
        self.lbl_ratio_formula.setSizePolicy(QtWidgets.QSizePolicy.Policy.Ignored, QtWidgets.QSizePolicy.Policy.Preferred)
        lbl_mode = QtWidgets.QLabel("Sweep Type:")
        lbl_coordinates = QtWidgets.QLabel("Sweep Coordinates:")
        lbl_fast = QtWidgets.QLabel("Fast Axis:")
        lbl_slow = QtWidgets.QLabel("Slow Axis:")
        lbl_source = QtWidgets.QLabel("Vds Source:")
        lbl_ratio = QtWidgets.QLabel("Ratio r:")
        form_setup.addRow(lbl_mode, self.cbo_sweep_dim)
        form_setup.addRow(lbl_coordinates, self.cbo_coordinates)
        form_setup.addRow(lbl_fast, self.cbo_fast)
        form_setup.addRow(lbl_slow, self.cbo_slow)
        form_setup.addRow(lbl_source, self.cbo_source)
        form_setup.addRow(self.chk_link)
        form_setup.addRow("Ratio multiplies:", self.cbo_ratio_target)
        form_setup.addRow(lbl_ratio, self.sp_ratio)
        form_setup.addRow("", self.lbl_ratio_formula)
        preview_content = QtWidgets.QWidget()
        preview_layout = QtWidgets.QVBoxLayout(preview_content)
        preview_layout.setContentsMargins(0, 0, 0, 0)
        preview_layout.addWidget(self.lbl_sweep_summary)
        self.trajectory_preview_section = CollapsibleSection("Trajectory preview", preview_content, expanded=False)
        form_setup.addRow(self.trajectory_preview_section)
        ctl_layout.addWidget(grp_setup)

        ctl_layout.addWidget(SectionHeader("2. Sweep range and fixed biases"))
        grp_vars = QtWidgets.QGroupBox("Axis Values")
        lay_vars = QtWidgets.QGridLayout(grp_vars)
        lay_vars.setContentsMargins(8, 16, 8, 8)
        lay_vars.setHorizontalSpacing(4)
        lay_vars.setVerticalSpacing(6)
        lay_vars.addWidget(QtWidgets.QLabel("Axis"), 0, 0)
        lay_vars.addWidget(QtWidgets.QLabel("Mode"), 0, 1)
        lay_vars.addWidget(QtWidgets.QLabel("Start / Fixed"), 0, 2)
        lay_vars.addWidget(QtWidgets.QLabel("Stop"), 0, 3)
        lay_vars.addWidget(QtWidgets.QLabel("Step"), 0, 4)
        lay_vars.addWidget(QtWidgets.QLabel("Apply now"), 0, 5)
        lay_vars.setColumnMinimumWidth(0, 34)
        lay_vars.setColumnMinimumWidth(1, 48)
        lay_vars.setColumnStretch(2, 1)
        lay_vars.setColumnStretch(3, 1)
        lay_vars.setColumnStretch(4, 1)

        self.sp_vtg_start = TrimmedDoubleSpinBox()
        self.sp_vtg_stop = TrimmedDoubleSpinBox()
        self.sp_vtg_step = TrimmedDoubleSpinBox()
        configure_volt_spinbox(self.sp_vtg_start, 0.0, decimals=6)
        configure_volt_spinbox(self.sp_vtg_stop, 1.0, decimals=6)
        configure_volt_spinbox(self.sp_vtg_step, 0.1, decimals=6)
        self.btn_set_vtg = QtWidgets.QPushButton("Set")
        self.btn_set_vtg.setFixedWidth(SET_BUTTON_WIDTH)
        self.lbl_vtg_mode = QtWidgets.QLabel()
        lay_vars.addWidget(QtWidgets.QLabel("Vtg"), 1, 0)
        lay_vars.addWidget(self.lbl_vtg_mode, 1, 1)
        lay_vars.addWidget(self.sp_vtg_start, 1, 2)
        lay_vars.addWidget(self.sp_vtg_stop, 1, 3)
        lay_vars.addWidget(self.sp_vtg_step, 1, 4)
        lay_vars.addWidget(self.btn_set_vtg, 1, 5)

        self.sp_vbg_start = TrimmedDoubleSpinBox()
        self.sp_vbg_stop = TrimmedDoubleSpinBox()
        self.sp_vbg_step = TrimmedDoubleSpinBox()
        configure_volt_spinbox(self.sp_vbg_start, 0.0, decimals=6)
        configure_volt_spinbox(self.sp_vbg_stop, 1.0, decimals=6)
        configure_volt_spinbox(self.sp_vbg_step, 0.1, decimals=6)
        self.btn_set_vbg = QtWidgets.QPushButton("Set")
        self.btn_set_vbg.setFixedWidth(SET_BUTTON_WIDTH)
        self.lbl_vbg_mode = QtWidgets.QLabel()
        lay_vars.addWidget(QtWidgets.QLabel("Vbg"), 2, 0)
        lay_vars.addWidget(self.lbl_vbg_mode, 2, 1)
        lay_vars.addWidget(self.sp_vbg_start, 2, 2)
        lay_vars.addWidget(self.sp_vbg_stop, 2, 3)
        lay_vars.addWidget(self.sp_vbg_step, 2, 4)
        lay_vars.addWidget(self.btn_set_vbg, 2, 5)

        self.sp_vds_start = TrimmedDoubleSpinBox()
        self.sp_vds_stop = TrimmedDoubleSpinBox()
        self.sp_vds_step = TrimmedDoubleSpinBox()
        configure_volt_spinbox(self.sp_vds_start, 0.0, decimals=6)
        configure_volt_spinbox(self.sp_vds_stop, 0.0, decimals=6)
        configure_volt_spinbox(self.sp_vds_step, 0.01, decimals=6)
        self.btn_set_vds = QtWidgets.QPushButton("Set")
        self.btn_set_vds.setFixedWidth(SET_BUTTON_WIDTH)
        self.lbl_vds_mode = QtWidgets.QLabel()
        lay_vars.addWidget(QtWidgets.QLabel("Vds"), 3, 0)
        lay_vars.addWidget(self.lbl_vds_mode, 3, 1)
        lay_vars.addWidget(self.sp_vds_start, 3, 2)
        lay_vars.addWidget(self.sp_vds_stop, 3, 3)
        lay_vars.addWidget(self.sp_vds_step, 3, 4)
        lay_vars.addWidget(self.btn_set_vds, 3, 5)
        self.lbl_precision = QtWidgets.QLabel("Source resolution unconfirmed")
        self.lbl_precision.setWordWrap(True)
        self.lbl_precision.setProperty("role", "hint")
        lay_vars.addWidget(self.lbl_precision, 6, 0, 1, 6)
        self.lbl_doping_mode = QtWidgets.QLabel()
        self.lbl_efield_mode = QtWidgets.QLabel()
        self.sp_doping_start = TrimmedDoubleSpinBox()
        self.sp_doping_stop = TrimmedDoubleSpinBox()
        self.sp_doping_step = TrimmedDoubleSpinBox()
        self.sp_efield_start = TrimmedDoubleSpinBox()
        self.sp_efield_stop = TrimmedDoubleSpinBox()
        self.sp_efield_step = TrimmedDoubleSpinBox()
        for spinbox, value in ((self.sp_doping_start, 0.0), (self.sp_doping_stop, 1.0), (self.sp_doping_step, 0.1), (self.sp_efield_start, 0.0), (self.sp_efield_stop, 1.0), (self.sp_efield_step, 0.1)):
            spinbox.setDecimals(6)
            spinbox.setRange(-1e4, 1e4)
            spinbox.setValue(value)
        lay_vars.addWidget(QtWidgets.QLabel("Doping"), 4, 0)
        lay_vars.addWidget(self.lbl_doping_mode, 4, 1)
        lay_vars.addWidget(self.sp_doping_start, 4, 2)
        lay_vars.addWidget(self.sp_doping_stop, 4, 3)
        lay_vars.addWidget(self.sp_doping_step, 4, 4)
        lay_vars.addWidget(QtWidgets.QLabel("E-field"), 5, 0)
        lay_vars.addWidget(self.lbl_efield_mode, 5, 1)
        lay_vars.addWidget(self.sp_efield_start, 5, 2)
        lay_vars.addWidget(self.sp_efield_stop, 5, 3)
        lay_vars.addWidget(self.sp_efield_step, 5, 4)
        ctl_layout.addWidget(grp_vars)

        self.grp_regions = QtWidgets.QGroupBox("Additional regions")
        region_layout = QtWidgets.QVBoxLayout(self.grp_regions)
        hint = QtWidgets.QLabel("Axis Values define region 1. Extensions merge into one serpentine scan. Shared points are measured once; exact stops are included (the last step may be smaller).")
        hint.setWordWrap(True)
        region_layout.addWidget(hint)
        self.lst_regions = QtWidgets.QListWidget()
        self.lst_regions.setMaximumHeight(110)
        region_layout.addWidget(self.lst_regions)
        buttons = QtWidgets.QHBoxLayout()
        self.btn_add_region = QtWidgets.QPushButton("Add")
        self.btn_edit_region = QtWidgets.QPushButton("Edit")
        self.btn_remove_region = QtWidgets.QPushButton("Remove")
        for button in (self.btn_add_region, self.btn_edit_region, self.btn_remove_region):
            buttons.addWidget(button)
        region_layout.addLayout(buttons)
        self.btn_add_region.clicked.connect(lambda: self._edit_region())
        self.btn_edit_region.clicked.connect(lambda: self._edit_region(self.lst_regions.currentRow()))
        self.btn_remove_region.clicked.connect(self._remove_region)
        ctl_layout.addWidget(self.grp_regions)

        row_tools = QtWidgets.QHBoxLayout()
        self.btn_preview = QtWidgets.QPushButton("Preview Sweep")
        row_tools.addWidget(self.btn_preview)
        ctl_layout.addLayout(row_tools)

        ctl_layout.addWidget(SectionHeader("3. Acquisition and waiting"))
        grp_time = QtWidgets.QGroupBox("Timing")
        form_time = QtWidgets.QFormLayout(grp_time)
        style_form_layout(form_time)
        self.sp_delay = SafeDoubleSpinBox()
        self.sp_delay.setDecimals(3)
        self.sp_delay.setRange(0.0, 30.0)
        self.sp_delay.setValue(0.5)
        self.sp_nsamp = SafeSpinBox()
        self.sp_nsamp.setRange(1, 1000)
        self.sp_nsamp.setValue(3)
        lbl_delay = QtWidgets.QLabel("Delay (s):")
        lbl_avg = QtWidgets.QLabel("Averages:")
        form_time.addRow(lbl_delay, self.sp_delay)
        form_time.addRow(lbl_avg, self.sp_nsamp)
        self.exp_timing = CollapsibleSection("Timing", grp_time, expanded=False)
        ctl_layout.addWidget(self.exp_timing)

        ctl_layout.addWidget(SectionHeader("4. Output files"))
        grp_output = QtWidgets.QGroupBox("Output Settings")
        form_output = QtWidgets.QFormLayout(grp_output)
        style_form_layout(form_output)
        self.ed_base = ExpandableLineEdit(self.p.base_name, title="Edit filename stem")
        self.cbo_x = SafeComboBox()
        for axis in PLOT_X_AXES:
            self.cbo_x.addItem(axis, axis)
        self.lbl_x_resolved = QtWidgets.QLabel()
        self.lbl_x_resolved.setWordWrap(True)
        self.lbl_x_resolved.setProperty("role", "hint")
        self.cbo_y = SafeComboBox()
        self.cbo_y.addItems(["Ids_DC", "Ids_X", "Ids_Y"])
        lbl_base = QtWidgets.QLabel("Filename Stem:")
        lbl_x = QtWidgets.QLabel("Plot X Axis:")
        lbl_y = QtWidgets.QLabel("Plot Y Axis:")
        form_output.addRow(lbl_base)
        form_output.addRow(self.ed_base.field_widget())
        form_output.addRow(lbl_x, self.cbo_x)
        form_output.addRow("", self.lbl_x_resolved)
        form_output.addRow(lbl_y, self.cbo_y)
        output_content = QtWidgets.QWidget()
        output_layout = QtWidgets.QVBoxLayout(output_content)
        output_layout.setContentsMargins(0, 0, 0, 0)
        output_layout.setSpacing(4)
        output_layout.addWidget(grp_output)
        self._add_output_preview_section(output_layout)
        self.exp_output = CollapsibleSection("Output, Plot, and Files", output_content, expanded=False)
        ctl_layout.addWidget(self.exp_output)

        self.lbl_connection_hint = QtWidgets.QLabel()
        self.lbl_connection_hint.setWordWrap(True)
        self.lbl_connection_hint.setProperty("role", "hint")
        ctl_layout.addWidget(self.lbl_connection_hint)

        ctl_layout.addWidget(SectionHeader("Status"))
        self.status_panel = StatusPanel(["g1", "g2", "g3", "daq"])
        self.lbl_g1 = self.status_panel.label("g1")
        self.lbl_g2 = self.status_panel.label("g2")
        self.lbl_g3 = self.status_panel.label("g3")
        self.lbl_daq = self.status_panel.label("daq")
        ctl_layout.addWidget(self.status_panel)

        for widget in [
            self.sp_vtg_start, self.sp_vtg_stop, self.sp_vtg_step,
            self.sp_vbg_start, self.sp_vbg_stop, self.sp_vbg_step,
            self.sp_vds_start, self.sp_vds_stop, self.sp_vds_step,
            self.sp_delay, self.sp_nsamp,
            self.ed_base, self.cbo_source, self.cbo_x, self.cbo_y, self.cbo_fast, self.cbo_slow, self.cbo_sweep_dim, self.cbo_coordinates,
            self.sp_ratio, self.cbo_ratio_target,
            self.sp_doping_start, self.sp_doping_stop, self.sp_doping_step,
            self.sp_efield_start, self.sp_efield_stop, self.sp_efield_step,
        ]:
            set_standard_input_height(widget)

        for spinbox in [
            self.sp_vtg_start, self.sp_vtg_stop, self.sp_vtg_step,
            self.sp_vbg_start, self.sp_vbg_stop, self.sp_vbg_step,
            self.sp_vds_start, self.sp_vds_stop, self.sp_vds_step,
        ]:
            spinbox.setMinimumWidth(64)

        apply_tooltip("Choose whether this run is a single sweep or a two-axis map.", lbl_mode, self.cbo_sweep_dim)
        apply_tooltip("Raw voltages preserves the original Vtg/Vbg/Vds grid. Doping/E-field drives both gates together; select Vds as an axis to sweep bias while holding the unused gate coordinate fixed.", lbl_coordinates, self.cbo_coordinates)
        apply_tooltip("Axis that moves for every point in the inner loop.", lbl_fast, self.cbo_fast)
        apply_tooltip("Axis that steps between fast-axis passes. Choose 1D sweep to hold all other axes fixed.", lbl_slow, self.cbo_slow)
        apply_tooltip("Choose Keithley G3 or an NI AO channel as the Vds source.", lbl_source, self.cbo_source)
        apply_tooltip("First value used for a swept axis, or the fixed value when the axis is not swept.", self.sp_vtg_start, self.sp_vbg_start, self.sp_vds_start)
        apply_tooltip("Last value included when this axis is part of the sweep.", self.sp_vtg_stop, self.sp_vbg_stop, self.sp_vds_stop)
        apply_tooltip("Point spacing for the selected sweep axis.", self.sp_vtg_step, self.sp_vbg_step, self.sp_vds_step)
        apply_tooltip(
            (
                "Apply the current Start value to hardware immediately. "
                f"Gate Set ramps at {GATE_BIAS_RAMP_STEP_V:g} V/step; "
                f"Vds Set ramps at {SAFE_RAMP_STEP_V:g} V/step."
            ),
            self.btn_set_vtg,
            self.btn_set_vbg,
            self.btn_set_vds,
        )
        apply_tooltip("Choose whether ratio r multiplies Vbg or Vtg in the Doping/E-field definitions.", self.cbo_ratio_target)
        apply_tooltip("Gate weighting r used when calculating Doping/E-field.", lbl_ratio, self.sp_ratio)
        apply_tooltip("Wait time after each setpoint update before acquiring data.", lbl_delay, self.sp_delay)
        apply_tooltip("Number of DAQ reads averaged at each map point.", lbl_avg, self.sp_nsamp)
        apply_tooltip("Show the planned point order without running hardware acquisition.", self.btn_preview)
        apply_tooltip("Base filename for the output CSV.", lbl_base, self.ed_base)
        apply_tooltip("Follow Sweep uses the fast sweep axis. Choose another recorded coordinate for a display-only override.", lbl_x, self.cbo_x, self.lbl_x_resolved)
        apply_tooltip("Select which current channel is drawn in the live plot.", lbl_y, self.cbo_y)

    def _wire(self):
        self._preview_timer = QtCore.QTimer(self)
        self._preview_timer.setSingleShot(True)
        self._preview_timer.setInterval(300)
        self._preview_timer.timeout.connect(self._refresh_automatic_preview)
        self.btn_start.clicked.connect(self.start_run)
        self.btn_stop.clicked.connect(self.stop_run)
        self.btn_preview.clicked.connect(self._show_preview)
        self.btn_set_vtg.clicked.connect(lambda: self.on_set_generic("Vtg", self.btn_set_vtg))
        self.btn_set_vbg.clicked.connect(lambda: self.on_set_generic("Vbg", self.btn_set_vbg))
        self.btn_set_vds.clicked.connect(lambda: self.on_set_generic("Vds", self.btn_set_vds))
        self.cbo_sweep_dim.currentIndexChanged.connect(self.on_sweep_type_changed)
        self.cbo_coordinates.currentIndexChanged.connect(self.on_sweep_type_changed)
        self.chk_link.toggled.connect(self.update_field_states)
        self.cbo_ratio_target.currentIndexChanged.connect(self._on_ratio_target_changed)
        self.cbo_fast.currentIndexChanged.connect(self.on_fast_combo_changed)
        self.cbo_slow.currentIndexChanged.connect(self.on_slow_combo_changed)
        self.cbo_source.currentIndexChanged.connect(self._update_connection_hint)
        self.cbo_source.currentIndexChanged.connect(self._update_manual_buttons)
        self.cbo_source.currentIndexChanged.connect(self._update_plot_axis_choices)
        self.cbo_x.currentIndexChanged.connect(self._on_plot_x_axis_changed)
        self.cbo_y.currentTextChanged.connect(self.set_plot_axis_source)
        self._update_plot_axis_choices()
        self.plot.y_axis_changed.connect(self.set_plot_axis_source)
        self.plot.plot_mode_changed.connect(lambda _mode: self._redraw_plot())
        self.update_field_states()
        self.ed_base.textChanged.connect(self.refresh_output_preview)
        for widget in (
            self.sp_vtg_start,
            self.sp_vtg_stop,
            self.sp_vtg_step,
            self.sp_vbg_start,
            self.sp_vbg_stop,
            self.sp_vbg_step,
            self.sp_vds_start,
            self.sp_vds_stop,
            self.sp_vds_step,
            self.sp_ratio,
            self.sp_doping_start, self.sp_doping_stop, self.sp_doping_step,
            self.sp_efield_start, self.sp_efield_stop, self.sp_efield_step,
        ):
            widget.valueChanged.connect(self._update_sweep_summary)
            widget.valueChanged.connect(self._schedule_preview)
        for widget in (self.cbo_source, self.cbo_fast, self.cbo_slow, self.cbo_sweep_dim, self.cbo_coordinates):
            widget.currentIndexChanged.connect(self.refresh_output_preview)
        self.sp_delay.valueChanged.connect(self._update_sweep_summary)
        self.sp_nsamp.valueChanged.connect(self._update_sweep_summary)
        self.cbo_source.currentIndexChanged.connect(self._update_sweep_summary)
        self.cbo_ratio_target.currentIndexChanged.connect(self.refresh_output_preview)
        self.chk_link.toggled.connect(self.refresh_output_preview)
        self.refresh_output_preview()

    def _schedule_preview(self, *_args):
        if self.worker_thread is None:
            self._preview_timer.start()

    def _refresh_automatic_preview(self):
        if self.worker_thread is None:
            self.on_preview()

    def _is_2d_map(self) -> bool:
        return self.cbo_sweep_dim.currentText() == "2D map"

    def _is_derived(self) -> bool:
        return (self.cbo_coordinates.currentData() or "Raw") == "Derived"

    def _swept_axes(self) -> list[str]:
        axes = [self.cbo_fast.currentText()]
        if self._is_2d_map() and self.cbo_slow.currentText() != "None":
            axes.append(self.cbo_slow.currentText())
        return axes

    def _axis_controls(self, axis: str):
        if axis == "Doping":
            return self.lbl_doping_mode, self.sp_doping_start, self.sp_doping_stop, self.sp_doping_step
        if axis == "E-field":
            return self.lbl_efield_mode, self.sp_efield_start, self.sp_efield_stop, self.sp_efield_step
        if axis == "Vtg":
            return self.lbl_vtg_mode, self.sp_vtg_start, self.sp_vtg_stop, self.sp_vtg_step
        if axis == "Vbg":
            return self.lbl_vbg_mode, self.sp_vbg_start, self.sp_vbg_stop, self.sp_vbg_step
        if axis == "Vds":
            return self.lbl_vds_mode, self.sp_vds_start, self.sp_vds_stop, self.sp_vds_step
        raise ValueError(f"Unknown sweep axis: {axis}")

    def _axis_values(self, axis: str) -> tuple[float, float, float]:
        _label, start, stop, step = self._axis_controls(axis)
        return start.value(), stop.value(), step.value()

    def _axis_sequence(self, axis: str) -> list[float]:
        start, stop, step = self._axis_values(axis)
        step = abs(step) * (1 if stop >= start else -1)
        return [start] if abs(step) < 1e-9 else _frange_inc(start, stop, step)

    def _point_count(self) -> int:
        if self._active_regions():
            return len(build_cosweep_points(self._region_params()))
        fast_count = sequence_point_count(*self._axis_values(self.cbo_fast.currentText()))
        if not self._is_2d_map():
            return fast_count
        slow_count = sequence_point_count(*self._axis_values(self.cbo_slow.currentText()))
        return fast_count * slow_count

    def _output_summary_parts(self) -> list[str]:
        parts = map_filename_parts(self._params_for_summary(), self.filename_signal_chain())
        if self._active_regions():
            parts.append(f"merged_{1 + len(self._regions)}regions")
        return parts

    def refresh_output_preview(self, *_args):
        measurement = "map_2d" if self._is_2d_map() else "sweep_1d"
        planned = build_planned_output(
            self.save,
            measurement,
            self.ed_base.text(),
            self._output_summary_parts(),
            run_id=self._output_run_id,
        )
        self._output_run_id = planned.run_id
        self._planned_output = planned
        self.set_output_preview_text(planned, planned_output_warning(planned, self.save))

    def _settings_widgets(self):
        return [
            ("base_name", self.ed_base),
            ("source", self.cbo_source),
            ("plot_x", self.cbo_x),
            ("plot_y", self.cbo_y),
            ("sweep_dim", self.cbo_sweep_dim),
            ("coordinates", self.cbo_coordinates),
            ("fast_axis", self.cbo_fast),
            ("slow_axis", self.cbo_slow),
            ("link_doping_efield", self.chk_link),
            ("ratio", self.sp_ratio),
            ("ratio_target", self.cbo_ratio_target),
            ("vtg_start", self.sp_vtg_start),
            ("vtg_stop", self.sp_vtg_stop),
            ("vtg_step", self.sp_vtg_step),
            ("vbg_start", self.sp_vbg_start),
            ("vbg_stop", self.sp_vbg_stop),
            ("vbg_step", self.sp_vbg_step),
            ("vds_start", self.sp_vds_start),
            ("vds_stop", self.sp_vds_stop),
            ("vds_step", self.sp_vds_step),
            ("doping_start", self.sp_doping_start),
            ("doping_stop", self.sp_doping_stop),
            ("doping_step", self.sp_doping_step),
            ("efield_start", self.sp_efield_start),
            ("efield_stop", self.sp_efield_stop),
            ("efield_step", self.sp_efield_step),
            ("delay", self.sp_delay),
            ("averages", self.sp_nsamp),
        ]

    def _load_tab_settings(self):
        # Axis settings are stored as text, but the combo contents depend on
        # the coordinate mode.  Capture both orientations before restoring the
        # widgets so a derived E-field-fast map is not lost to raw defaults.
        settings = get_app_settings()
        try:
            saved_regions = json.loads(str(settings.value(f"{self.SETTINGS_PREFIX}/regions", "[]")))
            self._regions = [{f"{axis}_{part}": float(region[f"{axis}_{part}"])
                              for axis in ("vtg", "vbg") for part in ("start", "stop", "step")}
                             for region in saved_regions]
        except (ValueError, TypeError, KeyError):
            self._regions = []
        self._refresh_regions()
        for mode, fallback in self._axis_memory.items():
            if mode == "Raw":
                fast_choices, slow_choices = {"Vtg", "Vbg", "Vds"}, {"Vtg", "Vbg", "Vds"}
                fast_key, slow_key = "raw_fast_axis", "raw_slow_axis"
            else:
                fast_choices, slow_choices = {"Doping", "E-field", "Vds"}, {"Doping", "E-field", "Vds"}
                fast_key, slow_key = "derived_fast_axis", "derived_slow_axis"
            if mode == "Raw":
                legacy_fast = settings.value(f"{self.SETTINGS_PREFIX}/fast_axis", fallback[0])
                legacy_slow = settings.value(f"{self.SETTINGS_PREFIX}/slow_axis", fallback[1])
            else:
                legacy_fast = settings.value(f"{self.SETTINGS_PREFIX}/fast_axis", fallback[0])
                legacy_slow = settings.value(f"{self.SETTINGS_PREFIX}/slow_axis", fallback[1])
            fast_value = str(settings.value(f"{self.SETTINGS_PREFIX}/{fast_key}", legacy_fast))
            slow_value = str(settings.value(f"{self.SETTINGS_PREFIX}/{slow_key}", legacy_slow))
            if fast_value in fast_choices and slow_value in slow_choices and fast_value != slow_value:
                self._axis_memory[mode] = (fast_value, slow_value)
        self._load_tab_widget_settings(self.SETTINGS_PREFIX, self._settings_widgets())
        self.on_sweep_type_changed()
        self._update_plot_axis_choices()
        self.set_plot_axis_source(self.cbo_y.currentText())
        self._update_sweep_summary()
        self.refresh_output_preview()

    def _bind_tab_settings(self):
        self._bind_tab_widget_settings(self.SETTINGS_PREFIX, self._settings_widgets())

    def save_tab_settings(self):
        # Keep both coordinate-mode orientations so toggling modes does not
        # overwrite the user's preferred fast/slow pair.
        current_fast, current_slow = self.cbo_fast.currentText(), self.cbo_slow.currentText()
        choices = {"Doping", "E-field", "Vds"} if self._is_derived() else {"Vtg", "Vbg", "Vds"}
        if current_fast in choices and current_slow in choices and current_fast != current_slow:
            self._axis_memory["Derived" if self._is_derived() else "Raw"] = (current_fast, current_slow)
        self._save_tab_widget_settings(self.SETTINGS_PREFIX, self._settings_widgets())
        settings = get_app_settings()
        settings.setValue(f"{self.SETTINGS_PREFIX}/regions", json.dumps(self._regions))
        for mode, (fast, slow) in self._axis_memory.items():
            settings.setValue(f"{self.SETTINGS_PREFIX}/{'derived' if mode == 'Derived' else 'raw'}_fast_axis", fast)
            settings.setValue(f"{self.SETTINGS_PREFIX}/{'derived' if mode == 'Derived' else 'raw'}_slow_axis", slow)
        settings.sync()

    def _update_manual_buttons(self):
        self._sync_sessions_from_manager()
        manual_available = not self.device_manager.is_busy() and not self.device_manager.current_in_use()
        self.btn_set_vtg.setEnabled(manual_available and self.s_g1 is not None and self.device_manager.is_voltage_source_mode("g1"))
        self.btn_set_vbg.setEnabled(manual_available and self.s_g2 is not None and self.device_manager.is_voltage_source_mode("g2"))
        self.btn_set_vds.setEnabled(
            manual_available
            and (
                ("NI DAQ" in self.cbo_source.currentText() and self.s_daq is not None)
                or (self.s_g3 is not None and self.device_manager.is_voltage_source_mode("g3"))
            )
        )
        source_ready = (
            self.cbo_source.currentText() != "Keithley 2400"
            or (self.s_g3 is not None and self.device_manager.is_voltage_source_mode("g3"))
        )
        self.run_panel.set_start_available(
            self.s_daq is not None and source_ready and self.worker_thread is None
        )
        self._update_connection_hint()

    def _sync_sessions_from_manager(self):
        self.s_g1 = self.device_manager.get_session("g1")
        self.s_g2 = self.device_manager.get_session("g2")
        self.s_g3 = self.device_manager.get_session("g3")
        self.s_daq = self.device_manager.get_session("daq")
        for name in ("g1", "g2", "g3", "daq"):
            self.set_device_status(name, self.device_manager.state(name), self.device_manager.detail(name) if self.device_manager.state(name) == "err" else None)
        self._update_source_items()

    def _update_source_items(self):
        items = ["Keithley 2400"] + [f"NI DAQ {a}" for a in self.get_ao_items()]
        cur = self.cbo_source.currentText()
        self.cbo_source.blockSignals(True)
        self.cbo_source.clear()
        self.cbo_source.addItems(items)
        if cur in items:
            self.cbo_source.setCurrentText(cur)
        self.cbo_source.blockSignals(False)
        self._update_plot_axis_choices()

    def _update_plot_axis_choices(self):
        options = plot_channel_options(self.cbo_source.currentText(), getattr(self.device_manager.connections, "drag_drive_enabled", False))
        current = self.cbo_y.currentText()
        if current not in options:
            current = "Ids_DC"
        self.cbo_y.blockSignals(True)
        self.cbo_y.clear()
        self.cbo_y.addItems(options)
        self.cbo_y.setCurrentText(current)
        self.cbo_y.blockSignals(False)
        self.plot.set_y_axis_options(options, current)
        self.plot.set_compare_channels(compare_channel_options(self.cbo_source.currentText(), getattr(self.device_manager.connections, "drag_drive_enabled", False)), grid=getattr(self.device_manager.connections, "drag_drive_enabled", False))
        self.set_plot_axis_source(current)

    def _on_device_status_changed(self, name: str, _state: str, _detail: str):
        if name in {"g1", "g2", "g3", "daq"}:
            self._sync_sessions_from_manager()
            self._update_manual_buttons()
            self._update_sweep_summary()

    def _on_operation_changed(self, busy: bool, message: str):
        if busy:
            self.set_status(message, "idle")
        self._update_manual_buttons()

    def _required_devices(self) -> list[str]:
        required = ["daq"]
        if self._is_derived():
            required.extend(["g1", "g2"])
        swept_axes = self._swept_axes()
        if "Vtg" in swept_axes or abs(self.sp_vtg_start.value()) > 1e-12:
            required.append("g1")
        if "Vbg" in swept_axes or abs(self.sp_vbg_start.value()) > 1e-12:
            required.append("g2")
        if self.cbo_source.currentText() == "Keithley 2400":
            required.append("g3")
        return list(dict.fromkeys(required))

    def _validate_required_sessions(self) -> bool:
        self._sync_sessions_from_manager()
        missing = [name for name in self._required_devices() if not self.device_manager.is_connected(name)]
        if missing:
            QtWidgets.QMessageBox.warning(self, "Missing Device", f"Connect required devices first: {', '.join(missing).upper()}")
            return False
        for gate in ("g1", "g2"):
            if gate in self._required_devices() and not self.device_manager.is_voltage_source_mode(gate):
                label = "G1 / Vtg" if gate == "g1" else "G2 / Vbg"
                QtWidgets.QMessageBox.warning(self, "Gate Mode", f"{label} must be in 2-wire voltage source mode for this sweep.")
                return False
        if self.cbo_source.currentText() == "Keithley 2400" and not self.device_manager.is_voltage_source_mode("g3"):
            QtWidgets.QMessageBox.warning(self, "Keithley Mode", "G3 must be in 2-wire voltage source mode for a Keithley-driven sweep.")
            return False
        return True

    def _validate_sweep_setup(self) -> bool:
        if self._is_derived() and not self._is_2d_map():
            QtWidgets.QMessageBox.warning(self, "Sweep Coordinates", "Doping/E-field coordinates are supported for 2D maps only.")
            return False
        if self._is_derived() and abs(self.sp_ratio.value()) < 1e-12:
            QtWidgets.QMessageBox.warning(self, "Invalid Ratio", "Doping/E-field sweeps require a non-zero ratio.")
            return False
        fast = self.cbo_fast.currentText()
        slow = self.cbo_slow.currentText()
        if self._is_2d_map() and (slow == "None" or slow == fast):
            QtWidgets.QMessageBox.warning(
                self,
                "Sweep Setup",
                "Choose two different axes before starting a 2D map.",
            )
            return False
        for axis in self._swept_axes():
            _start, _stop, step = self._axis_values(axis)
            if abs(step) < 1e-9:
                QtWidgets.QMessageBox.warning(
                    self,
                    "Invalid Step",
                    f"{axis} is selected as a swept axis, so its Step must be greater than zero.",
                )
                return False
        if self.sp_nsamp.value() < 1:
            QtWidgets.QMessageBox.warning(self, "Invalid Averages", "Averages must be at least 1.")
            return False
        if not self._is_derived() and self.chk_link.isChecked() and not self._link_plot_available():
            QtWidgets.QMessageBox.warning(
                self,
                "Plot Axis",
                "Doping/E-field plotting needs Vtg or Vbg in the selected sweep axes. Choose a gate axis or turn off Doping/E-field plotting.",
            )
            return False
        points = self._point_count()
        if points > 250000:
            QtWidgets.QMessageBox.warning(
                self,
                "Sweep Too Large",
                f"This setup would run {points:,} points. Reduce the range or increase the step before starting.",
            )
            return False
        return True

    def _update_connection_hint(self):
        required = self._required_devices()
        optional = [name for name in ("g1", "g2") if name not in required]
        missing_required = [name.upper() for name in required if not self.device_manager.is_connected(name)]
        missing_optional = [name.upper() for name in optional if not self.device_manager.is_connected(name)]
        if self.device_manager.is_connected("g1") and not self.device_manager.is_voltage_source_mode("g1"):
            (missing_required if "g1" in required else missing_optional).append("G1 mode")
        if self.device_manager.is_connected("g2") and not self.device_manager.is_voltage_source_mode("g2"):
            (missing_required if "g2" in required else missing_optional).append("G2 mode")
        if self.cbo_source.currentText() == "Keithley 2400" and self.device_manager.is_connected("g3") and not self.device_manager.is_voltage_source_mode("g3"):
            missing_required.append("G3 mode")
        if self.device_manager.is_busy():
            text = "Hardware is busy with another connection or disconnect operation from Devices."
            self.lbl_connection_hint.setProperty("role", "warning-hint")
            self.btn_start.setToolTip("Wait for the dock connection operation to finish")
        elif missing_required:
            text = f"Required before start: {', '.join(missing_required)}. Connect from Devices."
            if missing_optional:
                text += f" Optional gate controls unavailable: {', '.join(missing_optional)}."
            self.lbl_connection_hint.setProperty("role", "warning-hint")
            self.btn_start.setToolTip(f"Connect required devices from Devices: {', '.join(missing_required)}")
        else:
            text = "Ready to run with dock-managed sessions."
            if missing_optional:
                text += f" Manual gate controls unavailable until {', '.join(missing_optional)} connects."
            self.lbl_connection_hint.setProperty("role", "hint")
            self.btn_start.setToolTip("Start co-sweep")
        self.lbl_connection_hint.setText(text)
        self.lbl_connection_hint.style().unpolish(self.lbl_connection_hint)
        self.lbl_connection_hint.style().polish(self.lbl_connection_hint)

    def on_fast_combo_changed(self):
        self._update_slow_combo_items_grid()
        self.update_field_states()

    def on_slow_combo_changed(self):
        if not self._updating_combos:
            self.update_field_states()

    def on_sweep_type_changed(self):
        mode = "Derived" if self._is_derived() else "Raw"
        desired = ["Doping", "E-field", "Vds"] if mode == "Derived" else ["Vtg", "Vbg", "Vds"]
        previous_mode = self._last_coordinate_mode
        if mode != previous_mode:
            old_fast, old_slow = self.cbo_fast.currentText(), self.cbo_slow.currentText()
            old_choices = {"Doping", "E-field", "Vds"} if previous_mode == "Derived" else {"Vtg", "Vbg", "Vds"}
            if old_fast in old_choices and old_slow in old_choices and old_fast != old_slow:
                self._axis_memory[previous_mode] = (old_fast, old_slow)
            target_fast, target_slow = self._axis_memory[mode]
        else:
            target_fast, target_slow = self.cbo_fast.currentText(), self.cbo_slow.currentText()
        current = target_fast
        self.cbo_fast.blockSignals(True)
        self.cbo_fast.clear()
        self.cbo_fast.addItems(desired)
        self.cbo_fast.setCurrentText(current if current in desired else desired[0])
        self.cbo_fast.blockSignals(False)
        self._preferred_slow_axis = target_slow
        self._update_slow_combo_items_grid()
        self._preferred_slow_axis = None
        self._last_coordinate_mode = mode
        self.update_field_states()

    def _update_slow_combo_items_grid(self):
        if self._updating_combos:
            return
        self._updating_combos = True
        fast = self.cbo_fast.currentText()
        current_slow = self._preferred_slow_axis or self.cbo_slow.currentText()
        self.cbo_slow.blockSignals(True)
        self.cbo_slow.clear()
        if self._is_2d_map():
            choices = ["Doping", "E-field", "Vds"] if self._is_derived() else ["Vtg", "Vbg", "Vds"]
            for axis in choices:
                if axis != fast:
                    self.cbo_slow.addItem(axis)
        else:
            self.cbo_slow.addItem("None")
        idx = self.cbo_slow.findText(current_slow)
        self.cbo_slow.setCurrentIndex(idx if idx >= 0 else 0)
        self.cbo_slow.blockSignals(False)
        self.cbo_slow.setEnabled(self._is_2d_map())
        self._updating_combos = False

    def update_field_states(self):
        if self._updating_combos:
            return
        self._updating_combos = True
        self.sp_ratio.setEnabled(True)
        self.cbo_ratio_target.setEnabled(True)
        self.chk_link.setEnabled(not self._is_derived())
        if self._is_derived():
            self.chk_link.setChecked(False)
        self.cbo_slow.setEnabled(self._is_2d_map())
        active_sweep = self._swept_axes()
        for axis in ("Vtg", "Vbg", "Vds"):
            label, _start, stop, step = self._axis_controls(axis)
            is_swept = axis in active_sweep
            label.setText("Swept" if is_swept else "Fixed")
            label.setProperty("role", "hint")
            stop.setEnabled(is_swept)
            step.setEnabled(is_swept)
            stop.setVisible(is_swept)
            step.setVisible(is_swept)
        for axis in ("Doping", "E-field"):
            label, _start, stop, step = self._axis_controls(axis)
            is_swept = self._is_derived() and axis in active_sweep
            label.setText("Swept" if is_swept else "Fixed")
            stop.setEnabled(is_swept)
            step.setEnabled(is_swept)
            stop.setVisible(self._is_derived())
            step.setVisible(self._is_derived())
        for axis in ("Vtg", "Vbg"):
            _label, start, stop, step = self._axis_controls(axis)
            for widget in (start, stop, step):
                widget.setVisible(not self._is_derived())
        for axis in ("Doping", "E-field"):
            _label, start, stop, step = self._axis_controls(axis)
            for widget in (start, stop, step):
                widget.setVisible(self._is_derived())
        self._updating_combos = False
        self._update_ratio_formula()
        self._update_sweep_summary()
        self.on_axis_change_label()

    def _ratio_target(self) -> str:
        return normalize_ratio_target(self.cbo_ratio_target.currentData() or RATIO_TARGET_VBG)

    def _update_ratio_formula(self) -> None:
        if self._is_derived():
            prefix = "Hardware coordinates and CSV columns:\n"
        else:
            prefix = "CSV columns and plot axes:\n" if self.chk_link.isChecked() else "Doping/E-field CSV columns:\n"
        self.lbl_ratio_formula.setText(prefix + ratio_formula_text(self._ratio_target()))

    def _on_ratio_target_changed(self, *_args) -> None:
        self._update_ratio_formula()
        self._update_sweep_summary()
        self.on_axis_change_label()

    def _selected_plot_x_axis(self) -> str:
        return normalize_plot_x_selection(self.cbo_x.currentData() or self.cbo_x.currentText())

    def _resolved_plot_x_axis(self) -> str:
        return resolve_map_x_axis(self._selected_plot_x_axis(), self.cbo_fast.currentText())

    def _update_plot_x_resolution_hint(self, resolved_axis: str | None = None) -> None:
        resolved_axis = resolved_axis or self._resolved_plot_x_axis()
        prefix = "Following fast sweep" if self._selected_plot_x_axis() == FOLLOW_SWEEP else "Manual override"
        self.lbl_x_resolved.setText(f"{prefix}: {resolved_axis}")

    def _record_plot_x(self, record: dict) -> float:
        return record_x_value(record, self._resolved_plot_x_axis())

    def _plot_ratio_context(self) -> tuple[float, str]:
        if self._plot_records:
            record = self._plot_records[0]
            return float(record.get("plot_ratio", self.sp_ratio.value())), str(record.get("plot_ratio_target", self._ratio_target()))
        return self.sp_ratio.value(), self._ratio_target()

    def _on_plot_x_axis_changed(self, *_args) -> None:
        self.on_axis_change_label()

    def _format_axis_summary(self, axis: str) -> str:
        start, stop, step = self._axis_values(axis)
        if axis in self._swept_axes():
            return f"{axis}: {start:g} to {stop:g} V, step {abs(step):g} V"
        return f"{axis}: fixed {start:g} V"

    def _update_sweep_summary(self):
        self._schedule_preview()
        self.grp_regions.setEnabled(self._regions_available() and self.worker_thread is None)
        mode = "2D map" if self._is_2d_map() else "1D sweep"
        fast = self.cbo_fast.currentText()
        slow = self.cbo_slow.currentText() if self._is_2d_map() else "None"
        basis = "Model estimate; no matching historical calibration."
        try:
            preview = self._params_for_summary()
            trajectory = build_cosweep_points(preview)
            timing_context = getattr(self, "get_timing_context", None)
            connections, save = timing_context() if timing_context else (self.conns, self.save)
            calibrated = historical_cosweep_match(preview, connections, save.device_id)
            seconds = estimate_cosweep_seconds(preview, points=trajectory,
                                               connections=connections, device_id=save.device_id)
            duration = format_cosweep_duration(seconds)
            basis = cosweep_calibration_description(preview, connections, save.device_id)
            source = "model estimate"
            if calibrated:
                if preview.n_sample in (1, 3):
                    source = f"historical calibration; completed Ave={preview.n_sample} reference"
                else:
                    source = "Ave interpolated" if preview.n_sample == 2 else "Ave extrapolated"
            timing = f"Estimated total: ~{duration} ({source}; approximate)"
        except ValueError:
            trajectory = []
            timing = "Estimated total: unavailable until ranges and steps are valid"
        self.lbl_sweep_summary.setToolTip(
            basis + "\nTime estimate assumes outputs start at 0 V and includes final return to 0 V. "
            "Delay occurs once per point; Ave readings follow consecutively. "
            "Without matching history, additional averages assume 10 ms/read. "
            "Planning allowances: DAQ read 10 ms, Keithley I/O 20 ms, current read 100 ms, "
            "file/plot update 10 ms per point. Actual device and disk timing may vary."
        )
        if self._is_derived():
            axes = "; ".join(self._format_axis_summary(axis) for axis in ("Doping", "E-field", "Vds"))
            try:
                vtg_values = [point["vtg"] for point in trajectory]
                vbg_values = [point["vbg"] for point in trajectory]
                axes += f"\nComputed Vtg: {min(vtg_values):g} to {max(vtg_values):g} V; Vbg: {min(vbg_values):g} to {max(vbg_values):g} V"
            except Exception:
                axes += "\nComputed Vtg/Vbg: unavailable until ratio and ranges are valid"
            coordinate_label = "Derived Doping/E-field hardware trajectory"
        else:
            axes = "; ".join(self._format_axis_summary(axis) for axis in ("Vtg", "Vbg", "Vds"))
            coordinate_label = "Raw-voltage hardware trajectory"
        try:
            points = self._point_count()
        except Exception:
            points = 0
        order = f"Fast: {fast}; Slow: {slow}" if self._is_2d_map() else f"Sweep: {fast}; fixed axes use Start / Fixed"
        if self._active_regions():
            order += f"; {1 + len(self._regions)} regions merged into one scan"
        if self._is_2d_map():
            order += "; alternate slow passes run the fast axis in reverse."
        if self._live_eta is None:
            self.lbl_eta.setText(timing.replace("Estimated total:", "ETA (estimated total):", 1))
            self.lbl_eta.setToolTip(self.lbl_sweep_summary.toolTip())
        self._precision_error = ""
        try:
            precision = self._precision_check(trajectory)
        except ValueError as ex:
            precision = str(ex)
            self._precision_error = precision
        self._last_precision_signature = self._precision_signature()
        self.lbl_precision.setText(precision)
        self.lbl_sweep_summary.setText(f"{mode} · {coordinate_label}. {order}\n{axes}\nEstimated points: {points}\n{timing}\n{precision}")
        self.refresh_output_preview()

    def _params_for_summary(self) -> CoParams:
        """Build a non-destructive parameter snapshot for the setup summary."""
        p = CoParams(
            coordinate_mode="Derived" if self._is_derived() else "Raw",
            mode="Linked" if self.chk_link.isChecked() and self._link_plot_available() else "Grid",
            axis_fast=self.cbo_fast.currentText(),
            axis_slow=self.cbo_slow.currentText() if self._is_2d_map() else "None",
            vtg_start=self.sp_vtg_start.value(),
            vtg_stop=self.sp_vtg_stop.value() if "Vtg" in self._swept_axes() else self.sp_vtg_start.value(),
            vtg_step=abs(self.sp_vtg_step.value()),
            vbg_start=self.sp_vbg_start.value(),
            vbg_stop=self.sp_vbg_stop.value() if "Vbg" in self._swept_axes() else self.sp_vbg_start.value(),
            vbg_step=abs(self.sp_vbg_step.value()),
            vds_source=self.cbo_source.currentText(),
            vg_ramp=GATE_BIAS_RAMP_STEP_V,
            vds_ramp=self.p.vds_ramp,
            delay=self.sp_delay.value(),
            n_sample=self.sp_nsamp.value(),
            ratio=self.sp_ratio.value(),
            ratio_target=self._ratio_target(),
            vds_start=self.sp_vds_start.value(),
            vds_stop=self.sp_vds_stop.value() if "Vds" in self._swept_axes() else self.sp_vds_start.value(),
            vds_step=abs(self.sp_vds_step.value()),
            doping_start=self.sp_doping_start.value(),
            doping_stop=self.sp_doping_stop.value(),
            doping_step=abs(self.sp_doping_step.value()),
            efield_start=self.sp_efield_start.value(),
            efield_stop=self.sp_efield_stop.value(),
            efield_step=abs(self.sp_efield_step.value()),
        )
        return p

    def _regions_available(self):
        return not self._is_derived() and self._is_2d_map() and set(self._swept_axes()) == {"Vtg", "Vbg"}

    def _active_regions(self):
        return [dict(region) for region in self._regions] if self._regions_available() else []

    def _region_params(self):
        p = CoParams(axis_fast=self.cbo_fast.currentText(), axis_slow=self.cbo_slow.currentText(),
                     vds_start=self.sp_vds_start.value(), vds_stop=self.sp_vds_start.value(),
                     ratio=self.sp_ratio.value(), ratio_target=self._ratio_target(), regions=self._active_regions())
        for axis in ("vtg", "vbg"):
            for part in ("start", "stop", "step"):
                setattr(p, f"{axis}_{part}", getattr(self, f"sp_{axis}_{part}").value())
        return p

    def _refresh_regions(self):
        self.lst_regions.clear()
        for index, region in enumerate(self._regions, 2):
            self.lst_regions.addItem(f"{index}: " + "; ".join(
                f"{axis}: {region[f'{axis.lower()}_start']:g} → {region[f'{axis.lower()}_stop']:g}, step {region[f'{axis.lower()}_step']:g}"
                for axis in ("Vtg", "Vbg")))

    def _edit_region(self, index=None):
        if self.worker_thread is not None or not self._regions_available():
            return
        if index is not None and index < 0:
            return
        original = self._regions[index] if index is not None else {
            f"{axis}_{part}": getattr(self, f"sp_{axis}_{part}").value()
            for axis in ("vtg", "vbg") for part in ("start", "stop", "step")}
        dialog = QtWidgets.QDialog(self)
        dialog.setWindowTitle("Edit region" if index is not None else "Add region")
        form = QtWidgets.QFormLayout(dialog)
        inputs = {}
        for key, value in original.items():
            spin = SafeDoubleSpinBox()
            axis, part = key.split("_", 1)
            source = getattr(self, f"sp_{axis}_{part}")
            configure_volt_spinbox(spin, value, decimals=source.decimals())
            if key.endswith("step"):
                spin.setMinimum(10.0 ** -source.decimals())
            inputs[key] = spin
            form.addRow(key.replace("_", " ") + " (V)", spin)
        buttons = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.StandardButton.Ok | QtWidgets.QDialogButtonBox.StandardButton.Cancel)
        form.addRow(buttons)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        if dialog.exec() != QtWidgets.QDialog.DialogCode.Accepted:
            return
        region = {key: spin.value() for key, spin in inputs.items()}
        p = self._region_params()
        if index is None:
            p.regions.append(region)
        else:
            p.regions[index] = region
        try:
            validate_cosweep_params(p)
        except Exception as ex:
            QtWidgets.QMessageBox.warning(self, "Invalid region", str(ex))
            return
        self._regions = p.regions
        self._refresh_regions()
        self.save_tab_settings()
        self._update_sweep_summary()

    def _remove_region(self):
        index = self.lst_regions.currentRow()
        if index >= 0 and self.worker_thread is None:
            self._regions.pop(index)
            self._refresh_regions()
            self.save_tab_settings()
            self._update_sweep_summary()

    def on_axis_change_label(self):
        self._redraw_plot()

    def _set_plot_x_label(self, axis) -> None:
        resolved_axis = self._resolved_plot_x_axis()
        self.plot.set_x_axis_key(resolved_axis)
        self._update_plot_x_resolution_hint(resolved_axis)
        ratio, ratio_target = self._plot_ratio_context()
        axis.set_xlabel(plot_x_axis_label(resolved_axis, ratio, ratio_target))

    def _link_plot_available(self) -> bool:
        return bool({"Vtg", "Vbg"} & set(self._swept_axes()))

    def _show_preview(self):
        self.plot_tabs.setCurrentWidget(self.preview_plot)
        self.on_preview()

    def on_preview(self):
        if self.worker_thread is not None:
            return
        self._preview_timer.stop()
        self.preview_plot.ax.clear()
        if self._active_regions():
            try:
                points = build_cosweep_points(self._region_params())
                linked = self.chk_link.isChecked()
                path = [(points[0]["fast_value"], points[0]["slow_value"])]
                for point in points[1:]:
                    for axis, value in point["moves"]:
                        f, s = path[-1]
                        path.append((value, s) if axis == self.cbo_fast.currentText() else (f, value))
                if linked:
                    path = [gates_to_derived(f, s, self.sp_ratio.value(), self._ratio_target())
                            if self.cbo_fast.currentText() == "Vtg" else
                            gates_to_derived(s, f, self.sp_ratio.value(), self._ratio_target()) for f, s in path]
                self.preview_plot.ax.plot([p["doping" if linked else "fast_value"] for p in points],
                                  [p["efield" if linked else "slow_value"] for p in points], "o", markersize=3)
                self.preview_plot.ax.plot([p[0] for p in path], [p[1] for p in path], "-", linewidth=0.7,
                                  color=self.preview_plot.ax.lines[0].get_color())
                self.preview_plot.ax.set_xlabel(doping_axis_label(self.sp_ratio.value(), self._ratio_target()) if linked else self.cbo_fast.currentText() + " (V)")
                self.preview_plot.ax.set_ylabel(efield_axis_label(self.sp_ratio.value(), self._ratio_target()) if linked else self.cbo_slow.currentText() + " (V)")
                self.preview_plot.ax.set_title(f"Merged 2D map: {len(points):,} points, one serpentine scan")
            except Exception as ex:
                self.preview_plot.ax.set_title(f"Preview unavailable: {ex}")
            self.preview_plot.ax.grid(True)
            self.preview_plot.canvas.draw_idle()
            return
        if self._is_derived():
            try:
                points = build_cosweep_points(self._params_for_preview())
            except Exception as ex:
                self.preview_plot.ax.set_title(f"Preview unavailable: {ex}")
                self.preview_plot.canvas.draw_idle()
                return
            xs = [point["fast_value"] for point in points]
            ys = [point["slow_value"] for point in points]
            self.preview_plot.ax.plot(xs, ys, "o-", markersize=4, linewidth=1.0, color="blue", alpha=0.8)
            self.preview_plot.ax.set_xlabel(plot_x_axis_label(self.cbo_fast.currentText(), self.sp_ratio.value(), self._ratio_target()))
            self.preview_plot.ax.set_ylabel(plot_x_axis_label(self.cbo_slow.currentText(), self.sp_ratio.value(), self._ratio_target()))
            self.preview_plot.ax.set_title(f"Derived 2D Map Preview: {len(points)} pts (coordinated Vtg/Vbg)")
            self.preview_plot.ax.grid(True)
            self.preview_plot.canvas.draw_idle()
            return
        try:
            if self._point_count() > 250000:
                raise ValueError("The limit is 250,000 points.")
        except ValueError as ex:
            self.preview_plot.ax.set_title(f"Preview unavailable: {ex}")
            self.preview_plot.canvas.draw_idle()
            return
        use_ratio = self.chk_link.isChecked() and self._link_plot_available()
        fast_axis = self.cbo_fast.currentText()
        slow_axis = self.cbo_slow.currentText() if self._is_2d_map() else "None"
        f_seq = self._axis_sequence(fast_axis)

        s_seq = [0.0]
        if slow_axis != "None":
            s_seq = self._axis_sequence(slow_axis)

        xs, ys = [], []
        for pass_idx, s_val in enumerate(s_seq):
            if slow_axis != "None" and pass_idx % 2 == 1:
                row_f_seq = list(reversed(f_seq))
            else:
                row_f_seq = f_seq
            for f_val in row_f_seq:
                curr_vtg = f_val if fast_axis == "Vtg" else (s_val if slow_axis == "Vtg" else self.sp_vtg_start.value())
                curr_vbg = f_val if fast_axis == "Vbg" else (s_val if slow_axis == "Vbg" else self.sp_vbg_start.value())
                if use_ratio:
                    doping, efield = gates_to_derived(
                        curr_vtg,
                        curr_vbg,
                        self.sp_ratio.value(),
                        self._ratio_target(),
                    )
                    xs.append(doping)
                    ys.append(efield)
                else:
                    xs.append(f_val)
                    ys.append(s_val if slow_axis != "None" else 0.0)

        self.preview_plot.ax.plot(xs, ys, "o-", markersize=4, linewidth=1.0, color="blue", alpha=0.6 if use_ratio else 1.0)
        if use_ratio:
            formula_lines = ratio_formula_text(self._ratio_target()).splitlines()
            self.preview_plot.ax.set_xlabel(formula_lines[0])
            self.preview_plot.ax.set_ylabel(formula_lines[1])
            self.preview_plot.ax.set_title(f"{self.cbo_sweep_dim.currentText()} Preview: {len(xs)} pts")
        else:
            self.preview_plot.ax.set_xlabel(f"{fast_axis} (V)")
            self.preview_plot.ax.set_ylabel(f"{slow_axis if slow_axis != 'None' else 'Point order'} (V)")
            self.preview_plot.ax.set_title(f"{self.cbo_sweep_dim.currentText()} Preview: {len(xs)} pts")
        self.preview_plot.ax.grid(True)
        self.preview_plot.canvas.draw_idle()

    def _params_for_preview(self) -> CoParams:
        return self._params_for_summary()

    def on_set_generic(self, name, button):
        if name == "Vtg":
            val, gate_name = self.sp_vtg_start.value(), "g1"
        elif name == "Vbg":
            val, gate_name = self.sp_vbg_start.value(), "g2"
        else:
            val = self.sp_vds_start.value()
            gate_name = "g3" if "NI DAQ" not in self.cbo_source.currentText() else None
        if gate_name is not None:
            if self.device_manager.ramp_gate(gate_name, val):
                self.log.appendPlainText(f"Ramping {name} -> {val} V.")
            return

        sess = self.s_daq
        if sess:
            try:
                self.log.appendPlainText(f"Ramping {name} -> {val} ({SAFE_RAMP_STEP_V:g} V/step)")
                idx = int(self.cbo_source.currentText().split()[-1].replace("ao", ""))
                sess.ramp_voltage(idx, val, SAFE_RAMP_STEP_V, SAFE_RAMP_STEP_T)
                self.log.appendPlainText(f"Set {name} -> {val} ({SAFE_RAMP_STEP_V:g} V/step)")
                flash_button_success(button)
            except Exception as ex:
                self.log.appendPlainText(str(ex))
        else:
            self.log.appendPlainText(f"{name} not connected")

    def collect_params(self):
        self.p.regions = self._active_regions()
        self.refresh_output_preview()
        self.p.base_name = self.ed_base.text()
        self.p.output_csv_path = self._planned_output.csv_path if self._planned_output else ""
        self.p.output_metadata_path = self._planned_output.metadata_path if self._planned_output else ""
        self.p.output_log_path = self._planned_output.log_path if self._planned_output else ""
        src = self.cbo_source.currentText()
        if "NI DAQ" in src:
            self.p.vds_source = "NI DAQ AO"
            self.p.ao_channel = int(src.split()[-1].replace("ao", ""))
        else:
            self.p.vds_source = "Keithley 2400"
        swept_axes = self._swept_axes()
        self.p.vtg_start = self.sp_vtg_start.value()
        self.p.vtg_stop = self.sp_vtg_stop.value() if "Vtg" in swept_axes else self.sp_vtg_start.value()
        self.p.vtg_step = abs(self.sp_vtg_step.value())
        self.p.vbg_start = self.sp_vbg_start.value()
        self.p.vbg_stop = self.sp_vbg_stop.value() if "Vbg" in swept_axes else self.sp_vbg_start.value()
        self.p.vbg_step = abs(self.sp_vbg_step.value())
        self.p.vds_start = self.sp_vds_start.value()
        self.p.vds_stop = self.sp_vds_stop.value() if "Vds" in swept_axes else self.sp_vds_start.value()
        self.p.vds_step = abs(self.sp_vds_step.value())
        self.p.mode = "Linked" if self.chk_link.isChecked() and self._link_plot_available() else "Grid"
        self.p.coordinate_mode = self.cbo_coordinates.currentData() or "Raw"
        self.p.axis_fast = self.cbo_fast.currentText()
        self.p.axis_slow = self.cbo_slow.currentText() if self._is_2d_map() else "None"
        self.p.ratio = self.sp_ratio.value()
        self.p.ratio_target = self._ratio_target()
        self.p.plot_x_axis = self._selected_plot_x_axis()
        self.p.plot_x_resolved = self._resolved_plot_x_axis()
        self.p.delay = self.sp_delay.value()
        self.p.n_sample = self.sp_nsamp.value()
        self.p.plot_choice = self.cbo_y.currentText()
        self.p.vg_ramp = GATE_BIAS_RAMP_STEP_V
        self.p.doping_start = self.sp_doping_start.value()
        self.p.doping_stop = self.sp_doping_stop.value()
        self.p.doping_step = abs(self.sp_doping_step.value())
        self.p.efield_start = self.sp_efield_start.value()
        self.p.efield_stop = self.sp_efield_stop.value()
        self.p.efield_step = abs(self.sp_efield_step.value())
        self.p.derived_fast_axis = self.p.axis_fast if self._is_derived() else "Doping"
        self.p.derived_slow_axis = self.p.axis_slow if self._is_derived() else "E-field"

    def _precision_signature(self):
        return tuple(source_resolution(self.device_manager.sessions.get(name)) for name in ("g1", "g2", "g3"))

    def _on_precision_readback(self, *_args):
        # Most quiet reads change only current; avoid rebuilding the trajectory.
        if self._precision_signature() != getattr(self, "_last_precision_signature", None):
            self._update_sweep_summary()

    def _precision_check(self, points, require_known=False):
        resolutions, messages = {}, []
        for device, axis in (("g1", "vtg"), ("g2", "vbg"), ("g3", "vds")):
            if device not in self._required_devices():
                continue
            session = self.device_manager.sessions.get(device)
            quantum = source_resolution(session)
            if quantum is None:
                message = f"{axis.upper()}: source resolution unconfirmed; connect/read back a supported Keithley 2400 range."
                if require_known:
                    raise ValueError(message)
                messages.append(message)
            else:
                resolutions[axis] = quantum
                messages.append(f"{axis.upper()}: {quantum:g} V source resolution (cached range).")
        validate_voltage_points(points, resolutions)
        return " ".join(messages) or "Keithley source resolution: not applicable."

    @run_filename_snapshot
    def start_run(self):
        if self.worker_thread:
            return
        try:
            self._precision_check(build_cosweep_points(self._params_for_summary()), require_known=True)
        except ValueError as ex:
            QtWidgets.QMessageBox.warning(self, "Voltage Resolution", str(ex))
            return
        mw = self.window()
        if hasattr(mw, "refresh_models_from_ui"):
            mw.refresh_models_from_ui()
        calibration = self.verified_run_calibration()
        if calibration is None:
            return
        amp, lkn, signal_chain = calibration
        self.refresh_output_preview()
        if not self.validate_output_ready(self.save):
            return
        if not self._validate_sweep_setup():
            return
        if not self._validate_required_sessions():
            return
        try:
            self.collect_params()
            validate_cosweep_params(self.p)
        except Exception as ex:
            QtWidgets.QMessageBox.warning(self, "Invalid Parameters", str(ex))
            return
        claimed, blocked = self.claim_run_devices(self._required_devices())
        if not claimed:
            QtWidgets.QMessageBox.warning(self, "Busy", f"Devices already in use: {', '.join(blocked).upper()}")
            return
        self.on_preview()
        self._plot_records = []
        self._preview_timer.stop()
        self.plot_tabs.setCurrentWidget(self.plot)
        self.plot.clear(reset_plan=True)
        self.set_plot_axis_source(self.p.plot_choice)
        try:
            self.begin_run_logging(self._planned_output, "2D Map" if self._is_2d_map() else "1D Sweep")
            self.worker = CoSweepWorker(self.p, self.save, self.conns, g1=self.s_g1, g2=self.s_g2, g3=self.s_g3, daq=self.s_daq, plot_choice=self.p.plot_choice, amp_rate=amp, lkn_rate=lkn, signal_chain=signal_chain_metadata(signal_chain))
            trajectory = build_cosweep_points(self.p)
            self.plot.set_planned_ranges(coordinate_ranges(trajectory),
                                         axis=self._resolved_plot_x_axis())
            initial_seconds = estimate_cosweep_seconds(self.p, points=trajectory,
                                                       connections=self.conns, device_id=self.save.device_id)
            self._live_eta = LiveCoSweepTiming(len(trajectory), initial_seconds,
                estimate_cosweep_cleanup_seconds(self.p, trajectory[-1]))
            self._eta_outcome = "Running"
            self._eta_detail = ""
            self.worker.timing_updated.connect(self._on_live_timing)
            self.lbl_eta.setToolTip("Live estimate uses the last 20 point intervals, excluding pauses. "
                                   "The first point is excluded from the rate; the first 5 intervals "
                                   "blend with the initial estimate. Final zero-return time is reserved. "
                                   "Remaining time updates at each point; it is approximate.")
            self._on_live_timing({"completed": 0, "elapsed": 0, "phase": "sampling"})
            self.worker_thread = QtCore.QThread()
            self.worker.moveToThread(self.worker_thread)
            self.worker_thread.started.connect(self.worker.run)
            self.worker.point_data.connect(self.on_point_data)
            self.worker.clear_plot.connect(self._clear_plot)
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
            self.set_status("Starting measurement...", "preparing")
            self.worker_thread.start()
        except Exception as ex:
            self._live_eta = None
            self._update_sweep_summary()
            self.append_log(str(ex))
            self.end_run_logging("error", str(ex))
            self.release_run_devices()

    def stop_run(self):
        if self.worker:
            self.set_status("Stopping safely...", "stopping", "Stop requested. Waiting for the worker to reach a safe checkpoint and ramp outputs to 0 V.")
            self._eta_outcome = "Stopped"
            self.lbl_eta.setText("ETA: stopping safely; remaining time unavailable")
            self.worker.request_stop()

    def _on_live_timing(self, sample):
        eta = self._live_eta
        if eta is None:
            return
        phase = sample["phase"]
        eta.update(int(sample["completed"]), float(sample["elapsed"]))
        if phase in ("done", "cleanup_failed"):
            eta.finish(float(sample["elapsed"]))
            outcome = "Zero return not confirmed" if phase == "cleanup_failed" else self._eta_outcome
            self.lbl_eta.setText(f"{outcome} | Active elapsed: {format_cosweep_duration(eta.elapsed)}")
            self._finalize_run_outcome(cleanup_failed=phase == "cleanup_failed")
            # Retain the frozen result until thread cleanup releases the run.
        elif phase == "cleanup":
            self.lbl_eta.setText("ETA: returning outputs to zero; waiting for cleanup")
        elif self._eta_outcome != "Running":
            self.lbl_eta.setText("ETA: stopping safely; remaining time unavailable")
        else:
            state = "Paused" if phase == "paused" else "Live ETA"
            self.lbl_eta.setText(
                f"{state} | Remaining: ~{format_cosweep_duration(eta.remaining_seconds)}\n"
                f"Estimated total: ~{format_cosweep_duration(eta.total_seconds)} "
                f"({eta.completed}/{eta.total_points} points)")

    def _clear_plot(self):
        self.plot.clear()
        self._plot_records = []
        self.on_axis_change_label()

    def on_point_data(self, record):
        self._plot_records.append(record)
        self._redraw_plot()

    def set_plot_axis_source(self, source: str):
        if source not in plot_channel_options(self.cbo_source.currentText(), getattr(self.device_manager.connections, "drag_drive_enabled", False)):
            source = "Ids_DC"
        if self.cbo_y.currentText() != source:
            self.cbo_y.blockSignals(True)
            self.cbo_y.setCurrentText(source)
            self.cbo_y.blockSignals(False)
        self.plot.set_selected_y_axis(source)
        self.p.plot_choice = source
        self._redraw_plot()

    @preserve_plot_view
    def _redraw_plot(self):
        xs = [self._record_plot_x(record) for record in self._plot_records]
        if self.plot.current_plot_mode() == "4-Channel Compare":
            axes = self.plot.get_axes()
            channels = self.plot.compare_channels()
            for axis, channel in zip(axes, channels):
                axis.clear()
                ys = [plot_channel_value(record, channel) for record in self._plot_records]
                if xs:
                    axis.plot(xs, ys, "o-")
                axis.set_ylabel(f"{channel} (A)")
                axis.grid(True)
            for axis in self.plot.bottom_axes():
                self._set_plot_x_label(axis)
            self.plot.format_compare_axes()
            self.plot.canvas.draw_idle()
        else:
            source = self.cbo_y.currentText()
            ax = self.plot.ax
            ax.clear()
            ys = [plot_channel_value(record, source) for record in self._plot_records]
            if xs:
                ax.plot(xs, ys, "o-")
            ax.grid(True)
            self._set_plot_x_label(ax)
            ax.set_ylabel(f"{self.cbo_y.currentText()} (A)")
            self.plot.canvas.draw_idle()

    def _cleanup_thread(self):
        self._live_eta = None
        if self.worker:
            self.worker.deleteLater()
            self.worker = None
        self.worker_thread = None
        self.release_run_devices()
        self.run_panel.set_running(False)
        self._update_manual_buttons()

    def _record_run_outcome(self, outcome, detail):
        self._eta_outcome = outcome
        self._eta_detail = detail
        if self._live_eta is not None:
            self.set_status("Returning outputs to zero...", "cleanup", detail)
            return
        self._finalize_run_outcome()

    def _finalize_run_outcome(self, *, cleanup_failed=False):
        outcome, detail = self._eta_outcome, self._eta_detail
        if cleanup_failed:
            outcome = "Error"
            detail = "Zero return not confirmed. " + detail
        status = {"Finished": "finished", "Stopped": "stopped"}.get(outcome, "error")
        label = {"finished": "Finished", "stopped": "Stopped by user", "error": "Run error"}[status]
        self.set_status(label, "error" if status == "error" else "done", detail)
        self.append_log(("Saved: " if status == "finished" else "ERROR: " if status == "error" else "") + detail)
        self.end_run_logging(status, detail)
        self._output_run_id = None
        self.refresh_output_preview()

    def on_error(self, msg):
        self._record_run_outcome("Error", msg)

    def on_finished(self, path):
        self._record_run_outcome("Finished", path)

    def on_stopped(self, message: str):
        self._record_run_outcome("Stopped", message)
