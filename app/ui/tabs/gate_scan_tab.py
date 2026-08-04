from __future__ import annotations

from PyQt6 import QtCore, QtWidgets

from app.constants import V_LIMIT
from app.device_manager import DeviceManager
from app.gate_transform import (
    RATIO_TARGET_VBG,
    RATIO_TARGET_VTG,
    derived_to_gates,
    normalize_ratio_target,
    ratio_formula_text,
)
from app.models import Connections, LineSweepParams, SaveRoot
from app.plot_x_axis import (
    FOLLOW_SWEEP,
    PLOT_X_AXES,
    normalize_plot_x_selection,
    plot_x_axis_label,
    record_x_value,
    resolve_gate_scan_x_axis,
)
from app.result_channels import compare_channel_options, plot_channel_options, plot_channel_value
from app.run_output import build_planned_output, planned_output_warning
from app.settings import get_app_settings
from app.signal_chain import SignalChainSnapshot, signal_chain_filename_parts, signal_chain_metadata
from app.ui.helpers import apply_tooltip, configure_volt_spinbox, set_standard_input_height, style_form_layout
from app.ui.tabs.base_tab import BaseMeasurementTab
from app.ui.widgets.collapsible_section import CollapsibleSection
from app.ui.widgets.safe_combo import SafeComboBox
from app.ui.widgets.safe_spinbox import SafeDoubleSpinBox, SafeSpinBox
from app.ui.widgets.status_panel import SectionHeader, StatusPanel
from app.workers.line_sweep import LineSweepWorker


class GateScanTab(BaseMeasurementTab):
    SETTINGS_PREFIX = "tabs/gate_scan"
    PANEL_WIDTH_KEY = f"{SETTINGS_PREFIX}/control_panel_width"
    PANEL_MIN_WIDTH = 390
    PANEL_MAX_WIDTH = 500
    PANEL_DEFAULT_WIDTH = 420

    def __init__(self, save: SaveRoot, conns: Connections, device_manager: DeviceManager, get_global_rates_callable=None, get_ao_items_callable=None, get_signal_chain_callable=None):
        self.save = save
        self.conns = conns
        self.device_manager = device_manager
        self.get_global_rates = get_global_rates_callable or (lambda: (1e7, 100.0))
        self.get_signal_chain = get_signal_chain_callable or SignalChainSnapshot
        self.get_ao_items = get_ao_items_callable or (lambda: ["ao0", "ao1"])
        self.p = LineSweepParams()
        self.s_g1 = self.s_g2 = self.s_g3 = self.s_daq = None
        self.worker_thread = None
        self.worker = None
        self._plot_records = []
        self._output_run_id = None
        self._planned_output = None
        super().__init__("START SWEEP", "Sweep Axis", "Ids (A)", ["g1", "g2", "g3", "daq"])
        self.control_scroll.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.control_scroll.setMinimumWidth(self.PANEL_MIN_WIDTH)
        self.control_scroll.setMaximumWidth(self.PANEL_MAX_WIDTH)
        self.control_widget.setMinimumWidth(0)
        self.main_splitter.setSizes([self.PANEL_DEFAULT_WIDTH, 930])
        self._panel_width_save_timer = QtCore.QTimer(self)
        self._panel_width_save_timer.setSingleShot(True)
        self._panel_width_save_timer.setInterval(250)
        self._panel_width_save_timer.timeout.connect(self._save_control_panel_width)
        self.main_splitter.splitterMoved.connect(self._schedule_control_panel_width_save)
        self._restore_control_panel_width()
        self._wire()
        self.btn_start.setToolTip("Connect instruments first")
        self.device_manager.status_changed.connect(self._on_device_status_changed)
        self.device_manager.operation_changed.connect(self._on_operation_changed)
        self._sync_sessions_from_manager()
        self._load_tab_settings()
        self._bind_tab_settings()
        self._update_mode_ui()
        self._update_manual_buttons()

    def _build_control_panel(self, ctl_layout: QtWidgets.QVBoxLayout):
        ctl_layout.addWidget(SectionHeader("Mode"))
        grp_mode = QtWidgets.QGroupBox("Gate Scan Mode")
        lay_mode = QtWidgets.QVBoxLayout(grp_mode)
        lay_mode.setContentsMargins(10, 18, 10, 10)
        lay_mode.setSpacing(8)
        mode_wrap = QtWidgets.QWidget()
        mode_row = QtWidgets.QHBoxLayout(mode_wrap)
        mode_row.setContentsMargins(0, 0, 0, 0)
        mode_row.setSpacing(0)
        self.rad_mode_raw = QtWidgets.QToolButton()
        self.rad_mode_derived = QtWidgets.QToolButton()
        for button, text in (
            (self.rad_mode_raw, "Raw Voltages"),
            (self.rad_mode_derived, "Doping / E-field"),
        ):
            button.setText(text)
            button.setCheckable(True)
            button.setAutoRaise(False)
            button.setProperty("role", "segmented")
            button.setMinimumHeight(34)
            button.setSizePolicy(QtWidgets.QSizePolicy.Policy.Expanding, QtWidgets.QSizePolicy.Policy.Fixed)
            mode_row.addWidget(button, 1)
        self._trajectory_mode_group = QtWidgets.QButtonGroup(mode_wrap)
        self._trajectory_mode_group.setExclusive(True)
        self._trajectory_mode_group.addButton(self.rad_mode_raw)
        self._trajectory_mode_group.addButton(self.rad_mode_derived)
        self.rad_mode_raw.setChecked(True)
        lay_mode.addWidget(mode_wrap)
        self.lbl_mode_description = QtWidgets.QLabel()
        self.lbl_mode_description.setWordWrap(True)
        self.lbl_mode_description.setProperty("role", "hint")
        lay_mode.addWidget(self.lbl_mode_description)
        self.chk_sweep_bidirectional = QtWidgets.QCheckBox("Forward + backward sweep")
        self.chk_sweep_bidirectional.setChecked(False)
        lay_mode.addWidget(self.chk_sweep_bidirectional)
        apply_tooltip(
            "When checked, sweeps from start to stop then back to start. Forward trace shown in blue, backward in red.",
            self.chk_sweep_bidirectional,
        )
        set_standard_input_height(self.chk_sweep_bidirectional)
        ctl_layout.addWidget(grp_mode)

        ctl_layout.addWidget(SectionHeader("Trajectory"))
        self.raw_trajectory_widget = self._build_raw_mode_widget()
        self.derived_trajectory_widget = self._build_derived_mode_widget()
        ctl_layout.addWidget(self.raw_trajectory_widget)
        ctl_layout.addWidget(self.derived_trajectory_widget)

        ctl_layout.addWidget(SectionHeader("Acquisition"))
        grp_acq = QtWidgets.QGroupBox("Timing")
        form_acq = QtWidgets.QFormLayout(grp_acq)
        style_form_layout(form_acq)
        self.sp_n_points = SafeSpinBox()
        self.sp_n_points.setRange(1, 100000)
        self.sp_n_points.setValue(self.p.n_points)
        self.lbl_eta = QtWidgets.QLabel()
        self.lbl_eta.setProperty("role", "hint")
        self.sp_delay = SafeDoubleSpinBox()
        self.sp_delay.setDecimals(3)
        self.sp_delay.setRange(0.0, 30.0)
        self.sp_delay.setValue(self.p.delay)
        self.sp_nsamp = SafeSpinBox()
        self.sp_nsamp.setRange(1, 1000)
        self.sp_nsamp.setValue(self.p.n_sample)
        lbl_points = QtWidgets.QLabel("N points:")
        lbl_eta = QtWidgets.QLabel("Estimated time:")
        lbl_delay = QtWidgets.QLabel("Delay (s):")
        lbl_avg = QtWidgets.QLabel("Averages:")
        form_acq.addRow(lbl_points, self.sp_n_points)
        form_acq.addRow(lbl_eta, self.lbl_eta)
        form_acq.addRow(lbl_delay, self.sp_delay)
        form_acq.addRow(lbl_avg, self.sp_nsamp)
        self.exp_timing = CollapsibleSection("Timing", grp_acq, expanded=False)
        ctl_layout.addWidget(self.exp_timing)

        ctl_layout.addWidget(SectionHeader("Output"))
        grp_output = QtWidgets.QGroupBox("Output and Plot")
        form_output = QtWidgets.QFormLayout(grp_output)
        style_form_layout(form_output)
        self.cbo_source = SafeComboBox()
        self.cbo_source.addItems(["Keithley 2400"])
        self.cbo_x = SafeComboBox()
        for axis in PLOT_X_AXES:
            self.cbo_x.addItem(axis, axis)
        self.lbl_x_resolved = QtWidgets.QLabel()
        self.lbl_x_resolved.setWordWrap(True)
        self.lbl_x_resolved.setProperty("role", "hint")
        self.cbo_y = SafeComboBox()
        self.cbo_y.addItems(["Ids_DC", "Ids_X", "Ids_Y"])
        self.ed_base = QtWidgets.QLineEdit(self.p.base_name)
        lbl_source = QtWidgets.QLabel("Vds Source:")
        lbl_x = QtWidgets.QLabel("Plot X Axis:")
        lbl_y = QtWidgets.QLabel("Plot Y Axis:")
        lbl_base = QtWidgets.QLabel("Filename Stem:")
        form_output.addRow(lbl_source, self.cbo_source)
        form_output.addRow(lbl_x, self.cbo_x)
        form_output.addRow("", self.lbl_x_resolved)
        form_output.addRow(lbl_y, self.cbo_y)
        form_output.addRow(lbl_base, self.ed_base)
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
        self.lbl_connection_hint.setSizePolicy(
            QtWidgets.QSizePolicy.Policy.Ignored,
            QtWidgets.QSizePolicy.Policy.Preferred,
        )
        self.lbl_connection_hint.setProperty("role", "hint")
        ctl_layout.addWidget(self.lbl_connection_hint)

        ctl_layout.addWidget(SectionHeader("Status"))
        self.status_panel = StatusPanel(["g1", "g2", "g3", "daq"], columns=1)
        self.lbl_g1 = self.status_panel.label("g1")
        self.lbl_g2 = self.status_panel.label("g2")
        self.lbl_g3 = self.status_panel.label("g3")
        self.lbl_daq = self.status_panel.label("daq")
        ctl_layout.addWidget(self.status_panel)

        widgets = [
            self.sp_n_points, self.sp_delay, self.sp_nsamp,
            self.cbo_source, self.cbo_x, self.cbo_y, self.ed_base,
            self.sp_raw_vtg_start, self.sp_raw_vtg_stop, self.sp_raw_vbg_start, self.sp_raw_vbg_stop,
            self.sp_raw_vds_start, self.sp_raw_vds_stop, self.sp_ratio, self.sp_derived_start,
            self.sp_derived_stop, self.sp_derived_fixed,
            self.sp_derived_vds_fixed, self.sp_derived_vds_start, self.sp_derived_vds_stop,
            self.btn_derived_vbias_fixed, self.btn_derived_vbias_swept, self.cbo_ratio_target,
        ]
        for widget in widgets:
            set_standard_input_height(widget)

        for spinbox in (
            self.sp_ratio,
            self.sp_derived_start, self.sp_derived_stop,
            self.sp_derived_fixed, self.sp_derived_vds_fixed, self.sp_derived_vds_start, self.sp_derived_vds_stop,
        ):
            spinbox.setMinimumWidth(72)
            spinbox.setSizePolicy(
                QtWidgets.QSizePolicy.Policy.Ignored,
                QtWidgets.QSizePolicy.Policy.Fixed,
            )
        self.derived_vds_stack.setMinimumWidth(0)
        self.derived_vds_stack.setSizePolicy(
            QtWidgets.QSizePolicy.Policy.Ignored,
            QtWidgets.QSizePolicy.Policy.Fixed,
        )

        apply_tooltip("Switch between direct voltage trajectories and derived Doping/E-field trajectories.", self.rad_mode_raw, self.rad_mode_derived)
        apply_tooltip("Number of measurement points in the full line scan.", lbl_points, self.sp_n_points)
        apply_tooltip("Rough estimate based on ramp distances and per-point delay.", lbl_eta, self.lbl_eta)
        apply_tooltip("Wait time after each point is set before data acquisition.", lbl_delay, self.sp_delay)
        apply_tooltip("Number of DAQ reads averaged at each line-scan point.", lbl_avg, self.sp_nsamp)
        apply_tooltip("Choose Keithley G3 or an NI AO channel as the Vds source.", lbl_source, self.cbo_source)
        apply_tooltip("Follow Sweep tracks the configured sweep axis. Choose another axis to override it without changing hardware motion.", lbl_x, self.cbo_x, self.lbl_x_resolved)
        apply_tooltip("Select which current channel is shown live.", lbl_y, self.cbo_y)
        apply_tooltip("Base filename for the saved line-scan CSV.", lbl_base, self.ed_base)
        apply_tooltip("Exact CSV filename and folder that will be used for the next run.", self.lbl_filename_preview, self.lbl_path_preview)

    def _build_raw_mode_widget(self) -> QtWidgets.QWidget:
        grp = QtWidgets.QGroupBox("Raw Voltage Trajectory")
        lay = QtWidgets.QGridLayout(grp)
        lay.setContentsMargins(8, 16, 8, 8)
        lay.setHorizontalSpacing(3)
        lay.setVerticalSpacing(4)
        for col, text in ((1, "Start"), (2, "Stop"), (3, "Mode"), (4, "Step")):
            header = QtWidgets.QLabel(text)
            header.setFixedHeight(24)
            header.setAlignment(QtCore.Qt.AlignmentFlag.AlignLeft | QtCore.Qt.AlignmentFlag.AlignVCenter)
            lay.addWidget(header, 0, col)

        self.sp_raw_vtg_start, self.sp_raw_vtg_stop, self.chk_raw_vtg_active, self.lbl_raw_vtg_mode = self._add_raw_row(lay, 1, "Vtg:", 0.0, 1.0, True)
        self.sp_raw_vbg_start, self.sp_raw_vbg_stop, self.chk_raw_vbg_active, self.lbl_raw_vbg_mode = self._add_raw_row(lay, 2, "Vbg:", 0.0, 0.0, False)
        self.sp_raw_vds_start, self.sp_raw_vds_stop, self.chk_raw_vds_active, self.lbl_raw_vds_mode = self._add_raw_row(lay, 3, "Vds:", 0.0, 0.0, False)
        lay.setColumnStretch(1, 1)
        lay.setColumnStretch(2, 1)
        lay.setColumnMinimumWidth(0, 34)
        lay.setColumnMinimumWidth(3, 120)
        lay.setColumnMinimumWidth(4, 48)
        return grp

    def _build_derived_mode_widget(self) -> QtWidgets.QWidget:
        grp = QtWidgets.QGroupBox("Derived Trajectory")
        outer = QtWidgets.QVBoxLayout(grp)
        outer.setContentsMargins(8, 16, 8, 8)
        outer.setSpacing(8)

        grp_ratio = QtWidgets.QGroupBox("Coupling Ratio")
        form_ratio = QtWidgets.QFormLayout(grp_ratio)
        style_form_layout(form_ratio)
        self.sp_ratio = SafeDoubleSpinBox()
        self.sp_ratio.setDecimals(4)
        self.sp_ratio.setRange(-1e4, 1e4)
        self.sp_ratio.setValue(self.p.derived_ratio)
        self.cbo_ratio_target = SafeComboBox()
        self.cbo_ratio_target.addItem("Vbg (back gate)", RATIO_TARGET_VBG)
        self.cbo_ratio_target.addItem("Vtg (top gate)", RATIO_TARGET_VTG)
        self.cbo_ratio_target.setMinimumWidth(100)
        self.cbo_ratio_target.setSizePolicy(
            QtWidgets.QSizePolicy.Policy.Ignored,
            QtWidgets.QSizePolicy.Policy.Fixed,
        )
        self.lbl_ratio_formula = QtWidgets.QLabel()
        self.lbl_ratio_formula.setWordWrap(True)
        self.lbl_ratio_formula.setProperty("role", "hint")
        self.lbl_ratio_formula.setMinimumWidth(0)
        self.lbl_ratio_formula.setSizePolicy(
            QtWidgets.QSizePolicy.Policy.Ignored,
            QtWidgets.QSizePolicy.Policy.Preferred,
        )
        form_ratio.addRow("Ratio multiplies:", self.cbo_ratio_target)
        form_ratio.addRow("Ratio r:", self.sp_ratio)
        form_ratio.addRow("", self.lbl_ratio_formula)
        outer.addWidget(grp_ratio)

        grp_sweep = QtWidgets.QGroupBox("Sweep Definition")
        form_sweep = QtWidgets.QFormLayout(grp_sweep)
        style_form_layout(form_sweep)
        sweep_wrap = QtWidgets.QWidget()
        sweep_row = QtWidgets.QHBoxLayout(sweep_wrap)
        sweep_row.setContentsMargins(0, 0, 0, 0)
        sweep_row.setSpacing(0)
        self.rad_sweep_doping = QtWidgets.QToolButton()
        self.rad_sweep_efield = QtWidgets.QToolButton()
        for btn, text in ((self.rad_sweep_doping, "Doping"), (self.rad_sweep_efield, "E-field")):
            btn.setText(text)
            btn.setProperty("role", "segmented-compact")
            btn.setCheckable(True)
            btn.setAutoRaise(False)
            btn.setFixedWidth(68)
            btn.setFixedHeight(26)
            sweep_row.addWidget(btn)
        self._derived_axis_group = QtWidgets.QButtonGroup(sweep_wrap)
        self._derived_axis_group.setExclusive(True)
        self._derived_axis_group.addButton(self.rad_sweep_doping)
        self._derived_axis_group.addButton(self.rad_sweep_efield)
        self.rad_sweep_doping.setChecked(True)
        sweep_row.addStretch(1)

        self.sp_derived_start = SafeDoubleSpinBox()
        self.sp_derived_stop = SafeDoubleSpinBox()
        self.sp_derived_fixed = SafeDoubleSpinBox()
        for spinbox, value in (
            (self.sp_derived_start, 0.0),
            (self.sp_derived_stop, 1.0),
            (self.sp_derived_fixed, 0.0),
        ):
            configure_volt_spinbox(spinbox, value)

        self.lbl_derived_start = QtWidgets.QLabel("Doping start:")
        self.lbl_derived_stop = QtWidgets.QLabel("Doping stop:")
        self.lbl_fixed_name = QtWidgets.QLabel("E-field held:")
        self.lbl_derived_range = QtWidgets.QLabel()
        self.lbl_derived_range.setWordWrap(True)
        self.lbl_derived_range.setProperty("role", "hint")
        self.lbl_derived_range.setMinimumWidth(0)
        self.lbl_derived_range.setSizePolicy(
            QtWidgets.QSizePolicy.Policy.Ignored,
            QtWidgets.QSizePolicy.Policy.Preferred,
        )
        form_sweep.addRow("Sweep axis:", sweep_wrap)
        form_sweep.addRow(self.lbl_derived_start, self.sp_derived_start)
        form_sweep.addRow(self.lbl_derived_stop, self.sp_derived_stop)
        form_sweep.addRow(self.lbl_fixed_name, self.sp_derived_fixed)
        outer.addWidget(grp_sweep)

        grp_vbias = QtWidgets.QGroupBox("Vds")
        form_vbias = QtWidgets.QFormLayout(grp_vbias)
        style_form_layout(form_vbias)
        vbias_mode_wrap = QtWidgets.QWidget()
        vbias_mode_row = QtWidgets.QHBoxLayout(vbias_mode_wrap)
        vbias_mode_row.setContentsMargins(0, 0, 0, 0)
        vbias_mode_row.setSpacing(0)
        self.btn_derived_vbias_fixed = QtWidgets.QToolButton()
        self.btn_derived_vbias_swept = QtWidgets.QToolButton()
        for btn, text in ((self.btn_derived_vbias_fixed, "Fixed"), (self.btn_derived_vbias_swept, "Sweep")):
            btn.setText(text)
            btn.setProperty("role", "segmented-compact")
            btn.setCheckable(True)
            btn.setAutoRaise(False)
            btn.setMinimumWidth(68)
            btn.setFixedHeight(26)
            vbias_mode_row.addWidget(btn)
        self._derived_vbias_group = QtWidgets.QButtonGroup(vbias_mode_wrap)
        self._derived_vbias_group.setExclusive(True)
        self._derived_vbias_group.addButton(self.btn_derived_vbias_fixed)
        self._derived_vbias_group.addButton(self.btn_derived_vbias_swept)
        self.btn_derived_vbias_fixed.setChecked(True)
        self.derived_vds_stack = QtWidgets.QStackedWidget()
        self.derived_vds_stack.addWidget(self._build_derived_vds_fixed_widget())
        self.derived_vds_stack.addWidget(self._build_derived_vds_swept_widget())
        form_vbias.addRow("Mode:", vbias_mode_wrap)
        form_vbias.addRow("Value:", self.derived_vds_stack)
        outer.addWidget(grp_vbias)

        grp_preview = QtWidgets.QGroupBox("Computed Physical Voltages")
        preview_layout = QtWidgets.QVBoxLayout(grp_preview)
        preview_layout.setContentsMargins(10, 16, 10, 10)
        preview_layout.addWidget(self.lbl_derived_range)
        outer.addWidget(grp_preview)

        for section in (grp_ratio, grp_sweep, grp_vbias, grp_preview):
            section.setMinimumWidth(0)
            section.setSizePolicy(
                QtWidgets.QSizePolicy.Policy.Ignored,
                QtWidgets.QSizePolicy.Policy.Preferred,
            )

        apply_tooltip("Choose whether ratio r multiplies Vbg or Vtg in both derived definitions.", self.cbo_ratio_target)
        apply_tooltip("Gate weighting used in the Doping and E-field definitions.", self.sp_ratio)
        apply_tooltip("Choose which derived quantity is swept along the line.", self.rad_sweep_doping, self.rad_sweep_efield)
        apply_tooltip("Start of the selected Doping/E-field sweep.", self.sp_derived_start)
        apply_tooltip("Stop of the selected Doping/E-field sweep.", self.sp_derived_stop)
        apply_tooltip("Fixed value of the non-swept derived quantity.", self.sp_derived_fixed)
        apply_tooltip("Keep Vds fixed or sweep it together with the gate trajectory.", self.btn_derived_vbias_fixed, self.btn_derived_vbias_swept)
        apply_tooltip("Single Vds used for the whole derived scan.", self.sp_derived_vds_fixed)
        apply_tooltip("Start Vds for a simultaneous Vds sweep in derived mode.", self.sp_derived_vds_start)
        apply_tooltip("Stop Vds for a simultaneous Vds sweep in derived mode.", self.sp_derived_vds_stop)
        return grp

    def _build_derived_vds_fixed_widget(self) -> QtWidgets.QWidget:
        wrap = QtWidgets.QWidget()
        row = QtWidgets.QHBoxLayout(wrap)
        row.setContentsMargins(0, 0, 0, 0)
        self.sp_derived_vds_fixed = SafeDoubleSpinBox()
        configure_volt_spinbox(self.sp_derived_vds_fixed, 0.0)
        row.addWidget(self.sp_derived_vds_fixed)
        return wrap

    def _build_derived_vds_swept_widget(self) -> QtWidgets.QWidget:
        wrap = QtWidgets.QWidget()
        row = QtWidgets.QHBoxLayout(wrap)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(6)
        self.sp_derived_vds_start = SafeDoubleSpinBox()
        self.sp_derived_vds_stop = SafeDoubleSpinBox()
        configure_volt_spinbox(self.sp_derived_vds_start, 0.0)
        configure_volt_spinbox(self.sp_derived_vds_stop, 0.5)
        row.addWidget(QtWidgets.QLabel("Start"))
        self.sp_derived_vds_start.setFixedWidth(68)
        self.sp_derived_vds_stop.setFixedWidth(68)
        row.addWidget(self.sp_derived_vds_start, 1)
        row.addWidget(QtWidgets.QLabel("Stop"))
        row.addWidget(self.sp_derived_vds_stop, 1)
        return wrap

    def _add_raw_row(self, layout: QtWidgets.QGridLayout, row: int, label: str, start: float, stop: float, active: bool):
        start_box = SafeDoubleSpinBox()
        stop_box = SafeDoubleSpinBox()
        configure_volt_spinbox(start_box, start)
        configure_volt_spinbox(stop_box, stop)
        for box in (start_box, stop_box):
            box.setMinimumWidth(58)
            box.setMaximumWidth(72)
            box.setSizePolicy(
                QtWidgets.QSizePolicy.Policy.Expanding,
                QtWidgets.QSizePolicy.Policy.Fixed,
            )
        mode_wrap = QtWidgets.QWidget()
        mode_row = QtWidgets.QHBoxLayout(mode_wrap)
        mode_row.setContentsMargins(0, 0, 0, 0)
        mode_row.setSpacing(0)
        fixed_btn = QtWidgets.QToolButton()
        sweep_btn = QtWidgets.QToolButton()
        for btn, text in ((fixed_btn, "Fix"), (sweep_btn, "Sweep")):
            btn.setText(text)
            btn.setProperty("role", "segmented-compact")
            btn.setCheckable(True)
            btn.setAutoRaise(False)
            btn.setFixedWidth(60)
            btn.setFixedHeight(24)
            mode_row.addWidget(btn)
        mode_wrap.setFixedWidth(120)
        group = QtWidgets.QButtonGroup(mode_wrap)
        group.setExclusive(True)
        group.addButton(fixed_btn)
        group.addButton(sweep_btn)
        mode_wrap._button_group = group
        fixed_btn.setChecked(not active)
        sweep_btn.setChecked(active)
        mode_label = QtWidgets.QLabel()
        mode_label.setAlignment(QtCore.Qt.AlignmentFlag.AlignLeft | QtCore.Qt.AlignmentFlag.AlignVCenter)
        mode_label.setFixedHeight(24)
        mode_label.setMinimumWidth(44)
        mode_label.setMaximumWidth(60)
        mode_label.setProperty("role", "hint")
        name_label = QtWidgets.QLabel(label)
        name_label.setFixedHeight(24)
        layout.addWidget(name_label, row, 0)
        layout.addWidget(start_box, row, 1)
        layout.addWidget(stop_box, row, 2)
        layout.addWidget(mode_wrap, row, 3)
        layout.addWidget(mode_label, row, 4)
        apply_tooltip("Value used when this variable is fixed, or the start of its trajectory when Active is checked.", start_box)
        apply_tooltip("End value used when this variable is active in the trajectory.", stop_box)
        apply_tooltip(
            "Fixed holds this voltage at Start. Sweep moves from Start to Stop across the selected N points.",
            fixed_btn,
            sweep_btn,
            mode_wrap,
        )
        return start_box, stop_box, sweep_btn, mode_label

    def _wire(self):
        self.btn_start.clicked.connect(self.start_run)
        self.btn_stop.clicked.connect(self.stop_run)
        self.rad_mode_raw.toggled.connect(self._update_mode_ui)
        self.cbo_ratio_target.currentIndexChanged.connect(self._on_ratio_target_changed)
        self.rad_sweep_doping.toggled.connect(self._update_derived_labels)
        self.btn_derived_vbias_fixed.toggled.connect(self._update_derived_vds_mode)
        self.btn_derived_vbias_swept.toggled.connect(self._update_derived_vds_mode)
        self.cbo_source.currentIndexChanged.connect(self._update_connection_hint)
        self.cbo_source.currentIndexChanged.connect(self._update_manual_buttons)
        self.cbo_source.currentIndexChanged.connect(self.refresh_output_preview)
        self.cbo_x.currentIndexChanged.connect(self._update_plot_axis_label)

        for chk in (self.chk_raw_vtg_active, self.chk_raw_vbg_active, self.chk_raw_vds_active):
            chk.toggled.connect(self._update_raw_row_states)
            chk.toggled.connect(self._update_plot_axis_label)
            chk.toggled.connect(self._update_eta)
            chk.toggled.connect(self._update_connection_hint)

        for widget in (
            self.sp_raw_vtg_start, self.sp_raw_vtg_stop, self.sp_raw_vbg_start, self.sp_raw_vbg_stop,
            self.sp_raw_vds_start, self.sp_raw_vds_stop, self.sp_ratio, self.sp_derived_start,
            self.sp_derived_stop, self.sp_derived_fixed, self.sp_derived_vds_fixed,
            self.sp_derived_vds_start, self.sp_derived_vds_stop, self.sp_n_points,
            self.sp_delay, self.sp_nsamp,
        ):
            if hasattr(widget, "valueChanged"):
                widget.valueChanged.connect(self._update_eta)
            if hasattr(widget, "valueChanged"):
                widget.valueChanged.connect(self._update_derived_range_summary)

        for widget in (
            self.sp_raw_vtg_start, self.sp_raw_vtg_stop,
            self.sp_raw_vbg_start, self.sp_raw_vbg_stop,
            self.sp_raw_vds_start, self.sp_raw_vds_stop,
            self.sp_n_points,
        ):
            widget.valueChanged.connect(self._update_raw_row_states)

        self.rad_mode_raw.toggled.connect(self._update_connection_hint)
        self.rad_mode_derived.toggled.connect(self._update_connection_hint)
        self.rad_sweep_doping.toggled.connect(self._update_derived_range_summary)
        self.rad_sweep_efield.toggled.connect(self._update_derived_range_summary)
        self.sp_ratio.valueChanged.connect(self._update_plot_axis_label)
        self.btn_derived_vbias_fixed.toggled.connect(self._update_eta)
        self.btn_derived_vbias_swept.toggled.connect(self._update_eta)
        self.btn_derived_vbias_fixed.toggled.connect(self._update_connection_hint)
        self.btn_derived_vbias_swept.toggled.connect(self._update_connection_hint)
        self.cbo_source.currentIndexChanged.connect(self._update_plot_axis_choices)
        self.cbo_y.currentTextChanged.connect(self.set_plot_axis_source)
        self._update_plot_axis_choices()
        self.plot.y_axis_changed.connect(self.set_plot_axis_source)
        self.plot.plot_mode_changed.connect(lambda _mode: self._redraw_plot())
        self.chk_sweep_bidirectional.toggled.connect(self._update_eta)
        self.chk_sweep_bidirectional.toggled.connect(self.refresh_output_preview)
        self.ed_base.textChanged.connect(self.refresh_output_preview)

        preview_widgets = [
            self.sp_raw_vtg_start, self.sp_raw_vtg_stop,
            self.sp_raw_vbg_start, self.sp_raw_vbg_stop,
            self.sp_raw_vds_start, self.sp_raw_vds_stop,
            self.sp_ratio, self.sp_derived_start, self.sp_derived_stop, self.sp_derived_fixed,
            self.sp_derived_vds_fixed, self.sp_derived_vds_start, self.sp_derived_vds_stop,
        ]
        for widget in preview_widgets:
            widget.valueChanged.connect(self.refresh_output_preview)
        for chk in (self.chk_raw_vtg_active, self.chk_raw_vbg_active, self.chk_raw_vds_active):
            chk.toggled.connect(self.refresh_output_preview)
        self.rad_mode_raw.toggled.connect(self.refresh_output_preview)
        self.cbo_ratio_target.currentIndexChanged.connect(self.refresh_output_preview)
        self.rad_sweep_doping.toggled.connect(self.refresh_output_preview)
        self.rad_sweep_efield.toggled.connect(self.refresh_output_preview)
        self.btn_derived_vbias_fixed.toggled.connect(self.refresh_output_preview)
        self.btn_derived_vbias_swept.toggled.connect(self.refresh_output_preview)

    def _output_summary_parts(self) -> list[str]:
        source = "keithley_g3" if self.cbo_source.currentText() == "Keithley 2400" else self.cbo_source.currentText()
        mode = "raw" if self.rad_mode_raw.isChecked() else "derived"
        direction = "forward_backward" if self.chk_sweep_bidirectional.isChecked() else "forward"
        parts = [mode, source, direction]
        if self.rad_mode_raw.isChecked():
            for axis, start, stop, active in (
                ("Vtg", self.sp_raw_vtg_start.value(), self.sp_raw_vtg_stop.value(), self.chk_raw_vtg_active.isChecked()),
                ("Vbg", self.sp_raw_vbg_start.value(), self.sp_raw_vbg_stop.value(), self.chk_raw_vbg_active.isChecked()),
                ("Vds", self.sp_raw_vds_start.value(), self.sp_raw_vds_stop.value(), self.chk_raw_vds_active.isChecked()),
            ):
                parts.append(f"{axis}_{start:g}to{stop:g}V" if active else f"fixed_{axis}_{start:g}V")
            parts.extend(signal_chain_filename_parts(self.get_signal_chain()))
            return parts

        axis = "Doping" if self.rad_sweep_doping.isChecked() else "E-field"
        fixed_axis = "E-field" if self.rad_sweep_doping.isChecked() else "Doping"
        parts.extend(
            [
                f"ratio_on_{self._ratio_target()}_r_{self.sp_ratio.value():g}",
                f"{axis}_{self.sp_derived_start.value():g}to{self.sp_derived_stop.value():g}V",
                f"fixed_{fixed_axis}_{self.sp_derived_fixed.value():g}V",
            ]
        )
        if self._derived_vbias_is_swept():
            parts.append(f"Vds_{self.sp_derived_vds_start.value():g}to{self.sp_derived_vds_stop.value():g}V")
        else:
            parts.append(f"fixed_Vds_{self.sp_derived_vds_fixed.value():g}V")
        parts.extend(signal_chain_filename_parts(self.get_signal_chain()))
        return parts

    def refresh_output_preview(self, *_args) -> None:
        planned = build_planned_output(
            self.save,
            "gate_scan",
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
            ("mode_raw", self.rad_mode_raw),
            ("mode_derived", self.rad_mode_derived),
            ("sweep_bidirectional", self.chk_sweep_bidirectional),
            ("raw_vtg_active", self.chk_raw_vtg_active),
            ("raw_vtg_start", self.sp_raw_vtg_start),
            ("raw_vtg_stop", self.sp_raw_vtg_stop),
            ("raw_vbg_active", self.chk_raw_vbg_active),
            ("raw_vbg_start", self.sp_raw_vbg_start),
            ("raw_vbg_stop", self.sp_raw_vbg_stop),
            ("raw_vds_active", self.chk_raw_vds_active),
            ("raw_vds_start", self.sp_raw_vds_start),
            ("raw_vds_stop", self.sp_raw_vds_stop),
            ("derived_ratio", self.sp_ratio),
            ("derived_ratio_target", self.cbo_ratio_target),
            ("derived_axis_doping", self.rad_sweep_doping),
            ("derived_axis_efield", self.rad_sweep_efield),
            ("derived_start", self.sp_derived_start),
            ("derived_stop", self.sp_derived_stop),
            ("derived_fixed", self.sp_derived_fixed),
            ("derived_vds_fixed_mode", self.btn_derived_vbias_fixed),
            ("derived_vds_swept_mode", self.btn_derived_vbias_swept),
            ("derived_vds_fixed", self.sp_derived_vds_fixed),
            ("derived_vds_start", self.sp_derived_vds_start),
            ("derived_vds_stop", self.sp_derived_vds_stop),
            ("n_points", self.sp_n_points),
            ("delay", self.sp_delay),
            ("averages", self.sp_nsamp),
        ]

    def _load_tab_settings(self):
        self._load_tab_widget_settings(self.SETTINGS_PREFIX, self._settings_widgets())
        self._update_mode_ui()
        self._update_raw_row_states()
        self._update_derived_labels()
        self._update_derived_vds_mode()
        self._update_eta()
        self._update_plot_axis_choices()
        self.set_plot_axis_source(self.cbo_y.currentText())
        self.refresh_output_preview()

    def _bind_tab_settings(self):
        self._bind_tab_widget_settings(self.SETTINGS_PREFIX, self._settings_widgets())

    def save_tab_settings(self):
        self._save_tab_widget_settings(self.SETTINGS_PREFIX, self._settings_widgets())
        self._save_control_panel_width()

    def _restore_control_panel_width(self) -> None:
        value = get_app_settings().value(self.PANEL_WIDTH_KEY, self.PANEL_DEFAULT_WIDTH)
        try:
            width = int(value)
        except (TypeError, ValueError):
            width = self.PANEL_DEFAULT_WIDTH
        width = max(self.PANEL_MIN_WIDTH, min(width, self.PANEL_MAX_WIDTH))
        total = max(sum(self.main_splitter.sizes()), width + 1)
        self.main_splitter.setSizes([width, total - width])

    def _schedule_control_panel_width_save(self, *_args) -> None:
        self._panel_width_save_timer.start()

    def _save_control_panel_width(self) -> None:
        self._panel_width_save_timer.stop()
        sizes = self.main_splitter.sizes()
        if not sizes:
            return
        width = max(self.PANEL_MIN_WIDTH, min(int(sizes[0]), self.PANEL_MAX_WIDTH))
        settings = get_app_settings()
        settings.setValue(self.PANEL_WIDTH_KEY, width)
        settings.sync()

    def closeEvent(self, event) -> None:
        self._save_control_panel_width()
        super().closeEvent(event)

    def _sync_sessions_from_manager(self):
        self.s_g1 = self.device_manager.get_session("g1")
        self.s_g2 = self.device_manager.get_session("g2")
        self.s_g3 = self.device_manager.get_session("g3")
        self.s_daq = self.device_manager.get_session("daq")
        for name in ("g1", "g2", "g3", "daq"):
            detail = self.device_manager.detail(name) if self.device_manager.state(name) == "err" else None
            self.set_device_status(name, self.device_manager.state(name), detail)
        self._update_source_items()

    def _update_source_items(self):
        items = ["Keithley 2400"] + [f"NI DAQ {a}" for a in self.get_ao_items()]
        current = self.cbo_source.currentText()
        self.cbo_source.blockSignals(True)
        self.cbo_source.clear()
        self.cbo_source.addItems(items)
        if current in items:
            self.cbo_source.setCurrentText(current)
        self.cbo_source.blockSignals(False)
        self._update_plot_axis_choices()

    def _update_plot_axis_choices(self):
        options = plot_channel_options(self.cbo_source.currentText())
        current = self.cbo_y.currentText()
        if current not in options:
            current = "Ids_DC"
        self.cbo_y.blockSignals(True)
        self.cbo_y.clear()
        self.cbo_y.addItems(options)
        self.cbo_y.setCurrentText(current)
        self.cbo_y.blockSignals(False)
        self.plot.set_y_axis_options(options, current)
        self.plot.set_compare_channels(compare_channel_options(self.cbo_source.currentText()))
        self.set_plot_axis_source(current)

    def _on_device_status_changed(self, name: str, _state: str, _detail: str):
        if name in {"g1", "g2", "g3", "daq"}:
            self._sync_sessions_from_manager()
            self._update_manual_buttons()

    def _on_operation_changed(self, busy: bool, message: str):
        if busy:
            self.set_status(message, "idle")
        self._update_connection_hint()

    def _update_manual_buttons(self):
        self._sync_sessions_from_manager()
        self.btn_start.setEnabled(
            all(self.device_manager.is_connected(name) for name in self._required_devices())
            and not self._missing_mode_requirements()
            and self.worker_thread is None
        )
        self._update_connection_hint()

    def _required_devices(self) -> list[str]:
        required = ["daq", "g1", "g2"]
        if self.cbo_source.currentText() == "Keithley 2400":
            required.append("g3")
        return required

    def _missing_mode_requirements(self) -> list[str]:
        required_modes: list[str] = []
        if self.device_manager.is_connected("g1") and not self.device_manager.is_voltage_source_mode("g1"):
            required_modes.append("G1 mode")
        if self.device_manager.is_connected("g2") and not self.device_manager.is_voltage_source_mode("g2"):
            required_modes.append("G2 mode")
        if (
            self.cbo_source.currentText() == "Keithley 2400"
            and self.device_manager.is_connected("g3")
            and not self.device_manager.is_voltage_source_mode("g3")
        ):
            required_modes.append("G3 mode")
        return required_modes

    def _validate_required_sessions(self) -> bool:
        self._sync_sessions_from_manager()
        missing = [name for name in self._required_devices() if not self.device_manager.is_connected(name)]
        if missing:
            QtWidgets.QMessageBox.warning(self, "Missing Device", f"Connect required devices first: {', '.join(missing).upper()}")
            return False
        missing_modes = self._missing_mode_requirements()
        if missing_modes:
            QtWidgets.QMessageBox.warning(
                self,
                "Keithley Mode",
                "Gate Scan requires 2-wire voltage source mode for: " + ", ".join(missing_modes) + ".",
            )
            return False
        return True

    def _update_mode_ui(self):
        raw_mode = self.rad_mode_raw.isChecked()
        self.raw_trajectory_widget.setVisible(raw_mode)
        self.derived_trajectory_widget.setVisible(not raw_mode)
        self.lbl_mode_description.setText(
            "Sweep Vtg, Vbg, and/or Vds directly."
            if raw_mode
            else "Sweep Doping or E-field; the app calculates coordinated Vtg and Vbg setpoints."
        )
        self._update_ratio_formula()
        self._update_raw_row_states()
        self._update_derived_vds_mode()
        self._update_derived_labels()
        self._update_plot_axis_label()
        self._update_eta()
        self._update_connection_hint()

    def _set_mode_selector_enabled(self, enabled: bool) -> None:
        self.rad_mode_raw.setEnabled(enabled)
        self.rad_mode_derived.setEnabled(enabled)

    def _ratio_target(self) -> str:
        return normalize_ratio_target(self.cbo_ratio_target.currentData() or RATIO_TARGET_VBG)

    def _update_ratio_formula(self) -> None:
        self.lbl_ratio_formula.setText(ratio_formula_text(self._ratio_target()))

    def _on_ratio_target_changed(self, *_args) -> None:
        self._update_ratio_formula()
        self._update_derived_range_summary()
        self._update_eta()
        self._update_plot_axis_label()

    def _derived_gate_endpoints(self) -> list[tuple[float, float]]:
        start = self.sp_derived_start.value()
        stop = self.sp_derived_stop.value()
        fixed = self.sp_derived_fixed.value()
        if self.rad_sweep_doping.isChecked():
            derived_endpoints = [(start, fixed), (stop, fixed)]
        else:
            derived_endpoints = [(fixed, start), (fixed, stop)]
        return [
            derived_to_gates(doping, efield, self.sp_ratio.value(), self._ratio_target())
            for doping, efield in derived_endpoints
        ]

    def _update_raw_row_states(self):
        for start_box, stop_box, check, label in (
            (self.sp_raw_vtg_start, self.sp_raw_vtg_stop, self.chk_raw_vtg_active, self.lbl_raw_vtg_mode),
            (self.sp_raw_vbg_start, self.sp_raw_vbg_stop, self.chk_raw_vbg_active, self.lbl_raw_vbg_mode),
            (self.sp_raw_vds_start, self.sp_raw_vds_stop, self.chk_raw_vds_active, self.lbl_raw_vds_mode),
        ):
            active = check.isChecked()
            stop_box.setEnabled(active and self.rad_mode_raw.isChecked())
            label.setText(self._raw_step_summary(start_box, stop_box, active))

    def _points_step(self, start: float, stop: float, active: bool) -> float:
        if not active:
            return 0.0
        count = max(1, self.sp_n_points.value())
        if count <= 1:
            return 0.0
        return abs(stop - start) / float(count - 1)

    def _raw_step_summary(self, start_box: QtWidgets.QDoubleSpinBox, stop_box: QtWidgets.QDoubleSpinBox, active: bool) -> str:
        if not active:
            return "Fixed"
        step = self._points_step(start_box.value(), stop_box.value(), True)
        return f"{step:g} V/pt"

    def _derived_gate_steps(self) -> tuple[float, float, float]:
        count = max(1, self.sp_n_points.value())
        if count <= 1:
            return 0.0, 0.0, 0.0
        try:
            gate_endpoints = self._derived_gate_endpoints()
            vtg_values = [vtg for vtg, _vbg in gate_endpoints]
            vbg_values = [vbg for _vtg, vbg in gate_endpoints]
            vds_step = 0.0
            if self._derived_vbias_is_swept():
                vds_step = abs(self.sp_derived_vds_stop.value() - self.sp_derived_vds_start.value()) / float(count - 1)
            return (
                abs(vtg_values[1] - vtg_values[0]) / float(count - 1),
                abs(vbg_values[1] - vbg_values[0]) / float(count - 1),
                vds_step,
            )
        except Exception:
            return 0.0, 0.0, 0.0

    def _update_derived_labels(self):
        if self.rad_sweep_doping.isChecked():
            self.lbl_derived_start.setText("Doping start:")
            self.lbl_derived_stop.setText("Doping stop:")
            self.lbl_fixed_name.setText("E-field held:")
        else:
            self.lbl_derived_start.setText("E-field start:")
            self.lbl_derived_stop.setText("E-field stop:")
            self.lbl_fixed_name.setText("Doping held:")
        self._update_derived_range_summary()
        self._update_plot_axis_label()

    def _update_derived_vds_mode(self):
        self.derived_vds_stack.setCurrentIndex(1 if self._derived_vbias_is_swept() else 0)
        self._update_eta()
        self._update_derived_range_summary()

    def _derived_vbias_is_swept(self) -> bool:
        return self.btn_derived_vbias_swept.isChecked()

    def _derived_vbias_mode_text(self) -> str:
        return "Swept" if self._derived_vbias_is_swept() else "Fixed"

    def _update_derived_range_summary(self):
        if self.rad_mode_raw.isChecked():
            return
        try:
            gate_endpoints = self._derived_gate_endpoints()
            vtg_values = [vtg for vtg, _vbg in gate_endpoints]
            vbg_values = [vbg for _vtg, vbg in gate_endpoints]
            vtg_min, vtg_max = min(vtg_values), max(vtg_values)
            vbg_min, vbg_max = min(vbg_values), max(vbg_values)
            text = f"Vtg: {vtg_min:.3f} to {vtg_max:.3f} V\nVbg: {vbg_min:.3f} to {vbg_max:.3f} V"
            warn = any(abs(v) > V_LIMIT for v in (*vtg_values, *vbg_values))
            self.lbl_derived_range.setProperty("role", "warning-hint" if warn else "hint")
            if warn:
                text += f"   exceeds {V_LIMIT:.1f} V limit"
            vtg_step, vbg_step, vds_step = self._derived_gate_steps()
            text += f"\nSteps: Vtg {vtg_step:g} V/point, Vbg {vbg_step:g} V/point"
            if self._derived_vbias_is_swept():
                text += f", Vds {vds_step:g} V/point"
                text += f"\nVds: {self.sp_derived_vds_start.value():.3f} to {self.sp_derived_vds_stop.value():.3f} V"
            else:
                text += f"\nVds: fixed {self.sp_derived_vds_fixed.value():.3f} V"
            self.lbl_derived_range.setText(text)
        except Exception as ex:
            self.lbl_derived_range.setProperty("role", "warning-hint")
            self.lbl_derived_range.setText(str(ex))
        self.lbl_derived_range.style().unpolish(self.lbl_derived_range)
        self.lbl_derived_range.style().polish(self.lbl_derived_range)

    def _update_plot_axis_label(self, *_args):
        if self._plot_records:
            self._redraw_plot()
            return
        self._set_plot_x_label(self.plot.ax)
        self.plot.ax.set_ylabel(f"{self.cbo_y.currentText()} (A)")
        self.plot.canvas.draw_idle()

    def _set_plot_x_label(self, axis) -> None:
        resolved_axis = self._resolved_plot_x_axis()
        self._update_plot_x_resolution_hint(resolved_axis)
        ratio, ratio_target = self._plot_ratio_context()
        axis.set_xlabel(plot_x_axis_label(resolved_axis, ratio, ratio_target))

    def _selected_plot_x_axis(self) -> str:
        return normalize_plot_x_selection(self.cbo_x.currentData() or self.cbo_x.currentText())

    def _resolved_plot_x_axis(self) -> str:
        return resolve_gate_scan_x_axis(
            self._selected_plot_x_axis(),
            "Raw" if self.rad_mode_raw.isChecked() else "Derived",
            "Doping" if self.rad_sweep_doping.isChecked() else "E-field",
            self.chk_raw_vtg_active.isChecked(),
            self.chk_raw_vbg_active.isChecked(),
            self.chk_raw_vds_active.isChecked(),
        )

    def _update_plot_x_resolution_hint(self, resolved_axis: str | None = None) -> None:
        resolved_axis = resolved_axis or self._resolved_plot_x_axis()
        prefix = "Following sweep" if self._selected_plot_x_axis() == FOLLOW_SWEEP else "Manual override"
        self.lbl_x_resolved.setText(f"{prefix}: {resolved_axis}")

    def _record_plot_x(self, record: dict) -> float:
        return record_x_value(record, self._resolved_plot_x_axis())

    def _plot_ratio_context(self) -> tuple[float, str]:
        if self._plot_records:
            record = self._plot_records[0]
            return float(record.get("plot_ratio", self.sp_ratio.value())), str(record.get("plot_ratio_target", self._ratio_target()))
        return self.sp_ratio.value(), self._ratio_target()

    def _update_connection_hint(self):
        required = self._required_devices()
        missing_required = [name.upper() for name in required if not self.device_manager.is_connected(name)]
        missing_modes = self._missing_mode_requirements()
        mode_text = "raw trajectory" if self.rad_mode_raw.isChecked() else "derived trajectory"
        if self.device_manager.is_busy():
            text = "Hardware is busy with another connection or disconnect operation from Instrument Setup."
            self.lbl_connection_hint.setProperty("role", "warning-hint")
            self.btn_start.setToolTip("Wait for the dock connection operation to finish")
        elif missing_required:
            text = f"Required before start for {mode_text}: {', '.join(missing_required)}. Connect from Instrument Setup."
            self.lbl_connection_hint.setProperty("role", "warning-hint")
            self.btn_start.setToolTip(f"Connect required devices from Instrument Setup: {', '.join(missing_required)}")
        elif missing_modes:
            text = (
                f"{mode_text.capitalize()} requires 2-wire voltage source mode for: "
                + ", ".join(missing_modes)
                + ". Update the Keithley mode in Instrument Setup and reconnect."
            )
            self.lbl_connection_hint.setProperty("role", "warning-hint")
            self.btn_start.setToolTip("Set the required Keithley mode in Instrument Setup, then reconnect")
        else:
            text = f"Ready to run {mode_text} with dock-managed sessions."
            self.lbl_connection_hint.setProperty("role", "hint")
            self.btn_start.setToolTip("Start gate scan")
        self.lbl_connection_hint.setText(text)
        self.lbl_connection_hint.style().unpolish(self.lbl_connection_hint)
        self.lbl_connection_hint.style().polish(self.lbl_connection_hint)

    def _estimate_seconds(self) -> float:
        import math
        from app.constants import GATE_BIAS_RAMP_STEP_T, GATE_BIAS_RAMP_STEP_V, SAFE_RAMP_STEP_T, SAFE_RAMP_STEP_V

        def ramp_time(v: float, step_v: float, step_t: float) -> float:
            if abs(v) < 1e-9:
                return 0.0
            return math.ceil(abs(v) / step_v) * step_t

        count = max(1, self.sp_n_points.value())
        if self.rad_mode_raw.isChecked():
            start_vtg = self.sp_raw_vtg_start.value() if self.chk_raw_vtg_active.isChecked() else 0.0
            stop_vtg = self.sp_raw_vtg_stop.value() if self.chk_raw_vtg_active.isChecked() else 0.0
            start_vbg = self.sp_raw_vbg_start.value() if self.chk_raw_vbg_active.isChecked() else 0.0
            stop_vbg = self.sp_raw_vbg_stop.value() if self.chk_raw_vbg_active.isChecked() else 0.0
            start_vds = self.sp_raw_vds_start.value() if self.chk_raw_vds_active.isChecked() else 0.0
            stop_vds = self.sp_raw_vds_stop.value() if self.chk_raw_vds_active.isChecked() else 0.0
            vg_distance = 0.0
            if self.chk_raw_vtg_active.isChecked():
                vg_distance += abs(stop_vtg - start_vtg)
            if self.chk_raw_vbg_active.isChecked():
                vg_distance += abs(stop_vbg - start_vbg)
            vds_distance = abs(stop_vds - start_vds) if self.chk_raw_vds_active.isChecked() else 0.0
        else:
            try:
                gate_endpoints = self._derived_gate_endpoints()
                vtg_values = [vtg for vtg, _vbg in gate_endpoints]
                vbg_values = [vbg for _vtg, vbg in gate_endpoints]
                start_vtg, stop_vtg = vtg_values[0], vtg_values[1]
                start_vbg, stop_vbg = vbg_values[0], vbg_values[1]
                vg_distance = abs(vtg_values[1] - vtg_values[0]) + abs(vbg_values[1] - vbg_values[0])
            except Exception:
                start_vtg = stop_vtg = start_vbg = stop_vbg = 0.0
                vg_distance = 0.0
            if self._derived_vbias_is_swept():
                start_vds = self.sp_derived_vds_start.value()
                stop_vds = self.sp_derived_vds_stop.value()
                vds_distance = abs(stop_vds - start_vds)
            else:
                start_vds = stop_vds = 0.0
                vds_distance = 0.0
        ramp_to_start = (
            ramp_time(start_vtg, GATE_BIAS_RAMP_STEP_V, GATE_BIAS_RAMP_STEP_T)
            + ramp_time(start_vbg, GATE_BIAS_RAMP_STEP_V, GATE_BIAS_RAMP_STEP_T)
            + ramp_time(start_vds, SAFE_RAMP_STEP_V, SAFE_RAMP_STEP_T)
        )
        ramp_to_zero = (
            ramp_time(stop_vtg, SAFE_RAMP_STEP_V, SAFE_RAMP_STEP_T)
            + ramp_time(stop_vbg, SAFE_RAMP_STEP_V, SAFE_RAMP_STEP_T)
            + ramp_time(stop_vds, SAFE_RAMP_STEP_V, SAFE_RAMP_STEP_T)
        )
        sample_time = count * max(0.0, self.sp_delay.value())
        sweep_time = sample_time
        if self.chk_sweep_bidirectional.isChecked():
            sweep_time *= 2.0
        return ramp_to_start + sweep_time + ramp_to_zero

    def _update_eta(self):
        seconds = self._estimate_seconds()
        if seconds < 60:
            text = f"~{seconds:.1f} s"
        elif seconds < 3600:
            text = f"~{seconds / 60.0:.1f} min"
        else:
            text = f"~{seconds / 3600.0:.2f} h"
        self.lbl_eta.setText(text)

    def collect_params(self):
        self.refresh_output_preview()
        self.p.base_name = self.ed_base.text()
        self.p.output_csv_path = self._planned_output.csv_path if self._planned_output else ""
        self.p.output_metadata_path = self._planned_output.metadata_path if self._planned_output else ""
        self.p.output_log_path = self._planned_output.log_path if self._planned_output else ""
        self.p.mode = "Raw" if self.rad_mode_raw.isChecked() else "Derived"
        src = self.cbo_source.currentText()
        if src.startswith("NI DAQ "):
            self.p.vds_source = "NI DAQ AO"
            self.p.ao_channel = int(src.split()[-1].replace("ao", ""))
        else:
            self.p.vds_source = "Keithley 2400"
        self.p.raw_vtg_active = self.chk_raw_vtg_active.isChecked()
        self.p.raw_vtg_start = self.sp_raw_vtg_start.value()
        self.p.raw_vtg_stop = self.sp_raw_vtg_stop.value()
        self.p.raw_vbg_active = self.chk_raw_vbg_active.isChecked()
        self.p.raw_vbg_start = self.sp_raw_vbg_start.value()
        self.p.raw_vbg_stop = self.sp_raw_vbg_stop.value()
        self.p.raw_vds_active = self.chk_raw_vds_active.isChecked()
        self.p.raw_vds_start = self.sp_raw_vds_start.value()
        self.p.raw_vds_stop = self.sp_raw_vds_stop.value()
        self.p.derived_ratio = self.sp_ratio.value()
        self.p.derived_ratio_target = self._ratio_target()
        self.p.derived_axis = "Doping" if self.rad_sweep_doping.isChecked() else "E-field"
        self.p.derived_start = self.sp_derived_start.value()
        self.p.derived_stop = self.sp_derived_stop.value()
        self.p.derived_fixed = self.sp_derived_fixed.value()
        self.p.derived_vds_mode = self._derived_vbias_mode_text()
        self.p.derived_vds_fixed = self.sp_derived_vds_fixed.value()
        self.p.derived_vds_start = self.sp_derived_vds_start.value()
        self.p.derived_vds_stop = self.sp_derived_vds_stop.value()
        self.p.n_points = self.sp_n_points.value()
        if self.p.mode == "Raw":
            vtg_step = self._points_step(self.p.raw_vtg_start, self.p.raw_vtg_stop, self.p.raw_vtg_active)
            vbg_step = self._points_step(self.p.raw_vbg_start, self.p.raw_vbg_stop, self.p.raw_vbg_active)
            vds_step = self._points_step(self.p.raw_vds_start, self.p.raw_vds_stop, self.p.raw_vds_active)
        else:
            vtg_step, vbg_step, vds_step = self._derived_gate_steps()
        self.p.vg_ramp = max(vtg_step, vbg_step, 1e-6)
        self.p.vds_ramp = max(vds_step, 1e-6)
        self.p.delay = self.sp_delay.value()
        self.p.n_sample = self.sp_nsamp.value()
        self.p.plot_choice = self.cbo_y.currentText()
        self.p.plot_x_axis = self._selected_plot_x_axis()
        self.p.plot_x_resolved = self._resolved_plot_x_axis()
        self.p.sweep_both_ways = self.chk_sweep_bidirectional.isChecked()

    def _validate_params(self) -> bool:
        if self.rad_mode_raw.isChecked() and not any((self.chk_raw_vtg_active.isChecked(), self.chk_raw_vbg_active.isChecked(), self.chk_raw_vds_active.isChecked())):
            QtWidgets.QMessageBox.warning(self, "Invalid Sweep", "Raw trajectory needs at least one active variable.")
            return False
        if self.rad_mode_derived.isChecked() and abs(self.sp_ratio.value()) < 1e-12:
            QtWidgets.QMessageBox.warning(self, "Invalid Sweep", "Derived trajectory requires a non-zero ratio.")
            return False
        return True

    def start_run(self):
        if self.worker_thread:
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
        if not self._validate_required_sessions() or not self._validate_params():
            return
        try:
            self.collect_params()
        except Exception as ex:
            QtWidgets.QMessageBox.warning(self, "Invalid Parameters", str(ex))
            return
        claimed, blocked = self.claim_run_devices(self._required_devices())
        if not claimed:
            QtWidgets.QMessageBox.warning(self, "Busy", f"Devices already in use: {', '.join(blocked).upper()}")
            return
        self._plot_records = []
        self.plot.clear()
        self.set_plot_axis_source(self.p.plot_choice)
        try:
            self.begin_run_logging(self._planned_output, "Gate Scan")
            self.worker = LineSweepWorker(
                self.p,
                self.save,
                self.conns,
                g1=self.s_g1,
                g2=self.s_g2,
                g3=self.s_g3,
                daq=self.s_daq,
                plot_choice=self.p.plot_choice,
                amp_rate=amp,
                lkn_rate=lkn,
                signal_chain=signal_chain_metadata(signal_chain),
            )
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
            self._set_mode_selector_enabled(False)
            self.run_panel.set_running(True)
            self.set_status("Running...", "running")
            self.progress.setValue(0)
            self.worker_thread.start()
        except Exception as ex:
            self._set_mode_selector_enabled(True)
            self.append_log(str(ex))
            self.end_run_logging("error", str(ex))
            self.release_run_devices()

    def stop_run(self):
        if self.worker:
            self.set_status("Stopping safely...", "running", "Stop requested. Waiting for the worker to reach a safe checkpoint and ramp outputs to 0 V.")
            self.worker.request_stop()

    def _cleanup_thread(self):
        if self.worker:
            self.worker.deleteLater()
            self.worker = None
        self.worker_thread = None
        self.release_run_devices()
        self.run_panel.set_running(False)
        self._set_mode_selector_enabled(True)
        self._update_manual_buttons()

    def on_point_data(self, record):
        self._plot_records.append(record)
        self._redraw_plot()

    def set_plot_axis_source(self, source: str):
        if source not in plot_channel_options(self.cbo_source.currentText()):
            source = "Ids_DC"
        if self.cbo_y.currentText() != source:
            self.cbo_y.blockSignals(True)
            self.cbo_y.setCurrentText(source)
            self.cbo_y.blockSignals(False)
        self.plot.set_selected_y_axis(source)
        self.p.plot_choice = source
        self._redraw_plot()

    def _redraw_plot(self):
        fwd = [r for r in self._plot_records if r.get("direction", "forward") == "forward"]
        bwd = [r for r in self._plot_records if r.get("direction") == "backward"]
        if self.plot.current_plot_mode() == "4-Channel Compare":
            axes = self.plot.get_axes()
            channels = self.plot.compare_channels()
            for axis, channel in zip(axes, channels):
                axis.clear()
                if fwd:
                    axis.plot([self._record_plot_x(r) for r in fwd], [plot_channel_value(r, channel) for r in fwd], "b-o", markersize=3, label="Forward")
                if bwd:
                    axis.plot([self._record_plot_x(r) for r in bwd], [plot_channel_value(r, channel) for r in bwd], "r-o", markersize=3, label="Backward")
                    axis.legend(loc="best", fontsize=7)
                axis.relim()
                axis.autoscale_view()
                axis.set_ylabel(f"{channel} (A)")
                axis.grid(True)
            if axes:
                self._set_plot_x_label(axes[-1])
                self.plot.canvas.draw_idle()
        else:
            source = self.cbo_y.currentText()
            ax = self.plot.ax
            ax.clear()
            if fwd:
                ax.plot([self._record_plot_x(r) for r in fwd], [plot_channel_value(r, source) for r in fwd], "b-o", markersize=3, label="Forward")
            if bwd:
                ax.plot([self._record_plot_x(r) for r in bwd], [plot_channel_value(r, source) for r in bwd], "r-o", markersize=3, label="Backward")
                ax.legend(loc="best", fontsize=7)
            ax.relim()
            ax.autoscale_view()
            self._set_plot_x_label(ax)
            ax.set_ylabel(f"{self.cbo_y.currentText()} (A)")
            ax.grid(True)
            self.plot.canvas.draw_idle()

    def on_finished(self, path: str):
        self.set_status("Finished", "done", path)
        self.append_log(f"Saved: {path}")
        self.end_run_logging("finished", path)
        self._output_run_id = None
        self.refresh_output_preview()

    def on_error(self, msg: str):
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
