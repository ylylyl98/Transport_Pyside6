from __future__ import annotations

from PyQt6 import QtCore, QtWidgets

from app.constants import GATE_BIAS_RAMP_STEP_T, GATE_BIAS_RAMP_STEP_V, SAFE_RAMP_STEP_T, SAFE_RAMP_STEP_V
from app.device_manager import DeviceManager
from app.models import CoParams, Connections, SaveRoot
from app.result_channels import compare_channel_options, plot_channel_options, plot_channel_value
from app.run_output import build_planned_output, planned_output_warning
from app.ui.helpers import apply_tooltip, configure_volt_spinbox, flash_button_success, set_standard_input_height, style_form_layout
from app.ui.tabs.base_tab import BaseMeasurementTab
from app.ui.widgets.collapsible_section import CollapsibleSection
from app.ui.widgets.status_panel import SectionHeader, StatusPanel
from app.utils import _frange_inc, safe_ramp
from app.workers.cosweep import CoSweepWorker

SET_BUTTON_WIDTH = 48
COSWEEP_PANEL_MIN_WIDTH = 380
COSWEEP_PANEL_MAX_WIDTH = 560


class CoSweepTab(BaseMeasurementTab):
    SETTINGS_PREFIX = "tabs/map_2d"

    def __init__(self, save: SaveRoot, conns: Connections, device_manager: DeviceManager, get_global_rates_callable=None, get_ao_items_callable=None):
        self.save = save
        self.conns = conns
        self.device_manager = device_manager
        self.get_global_rates = get_global_rates_callable or (lambda: (1e7, 100.0))
        self.get_ao_items = get_ao_items_callable or (lambda: ["ao0", "ao1"])
        self.p = CoParams()
        self.s_g1 = self.s_g2 = self.s_g3 = self.s_daq = None
        self.worker_thread = None
        self.worker = None
        self._updating_combos = False
        self._plot_records = []
        self._output_run_id = None
        self._planned_output = None
        super().__init__("START SWEEP", "Fast Axis", "Ids (A)", ["g1", "g2", "g3", "daq"])
        self.control_scroll.setMinimumWidth(COSWEEP_PANEL_MIN_WIDTH)
        self.control_scroll.setMaximumWidth(COSWEEP_PANEL_MAX_WIDTH)
        self.main_splitter.setSizes([430, 830])
        self._wire()
        self.btn_start.setToolTip("Connect instruments first")
        self.device_manager.status_changed.connect(self._on_device_status_changed)
        self.device_manager.operation_changed.connect(self._on_operation_changed)
        self._sync_sessions_from_manager()
        self._load_tab_settings()
        self._bind_tab_settings()
        self._update_manual_buttons()
        self.on_fast_combo_changed()

    def _build_control_panel(self, ctl_layout: QtWidgets.QVBoxLayout):
        ctl_layout.addWidget(SectionHeader("Sweep Setup"))
        grp_setup = QtWidgets.QGroupBox("Sweep Setup")
        form_setup = QtWidgets.QFormLayout(grp_setup)
        style_form_layout(form_setup)
        self.cbo_sweep_dim = QtWidgets.QComboBox()
        self.cbo_sweep_dim.addItems(["1D sweep", "2D map"])
        self.cbo_sweep_dim.setCurrentText("2D map")
        self.chk_link = QtWidgets.QCheckBox("Plot as Doping/E-field axes")
        self.chk_link.setToolTip("Changes only the preview and plotted x-axis labels. Hardware control still uses the raw Vtg/Vbg/Vds grid.")
        self.cbo_fast = QtWidgets.QComboBox()
        self.cbo_fast.addItems(["Vtg", "Vbg", "Vds"])
        self.cbo_slow = QtWidgets.QComboBox()
        self.cbo_slow.addItems(["None", "Vtg", "Vbg", "Vds"])
        self.cbo_slow.setCurrentText("Vbg")
        self.cbo_source = QtWidgets.QComboBox()
        self.cbo_source.addItems(["Keithley 2400"])
        self.sp_ratio = QtWidgets.QDoubleSpinBox()
        self.sp_ratio.setDecimals(4)
        self.sp_ratio.setRange(-1e4, 1e4)
        self.sp_ratio.setValue(1.0)
        self.lbl_sweep_summary = QtWidgets.QLabel()
        self.lbl_sweep_summary.setWordWrap(True)
        self.lbl_sweep_summary.setProperty("role", "hint")
        self.lbl_sweep_summary.setMinimumWidth(0)
        self.lbl_sweep_summary.setSizePolicy(
            QtWidgets.QSizePolicy.Policy.Ignored,
            QtWidgets.QSizePolicy.Policy.Preferred,
        )
        plot_note = QtWidgets.QLabel("(Plot-only: Doping = Vtg + r*Vbg; E-field = Vtg - r*Vbg.)")
        plot_note.setWordWrap(True)
        plot_note.setMinimumWidth(0)
        plot_note.setSizePolicy(QtWidgets.QSizePolicy.Policy.Ignored, QtWidgets.QSizePolicy.Policy.Preferred)
        lbl_mode = QtWidgets.QLabel("Sweep Type:")
        lbl_fast = QtWidgets.QLabel("Fast Axis:")
        lbl_slow = QtWidgets.QLabel("Slow Axis:")
        lbl_source = QtWidgets.QLabel("Vds Source:")
        lbl_ratio = QtWidgets.QLabel("Back-gate ratio r:")
        form_setup.addRow(lbl_mode, self.cbo_sweep_dim)
        form_setup.addRow(lbl_fast, self.cbo_fast)
        form_setup.addRow(lbl_slow, self.cbo_slow)
        form_setup.addRow(lbl_source, self.cbo_source)
        form_setup.addRow(self.chk_link)
        form_setup.addRow(lbl_ratio, self.sp_ratio)
        form_setup.addRow("", plot_note)
        form_setup.addRow(QtWidgets.QLabel("Summary:"), self.lbl_sweep_summary)
        ctl_layout.addWidget(grp_setup)

        ctl_layout.addWidget(SectionHeader("Axis Values"))
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
        lay_vars.addWidget(QtWidgets.QLabel("Set"), 0, 5)
        lay_vars.setColumnMinimumWidth(0, 34)
        lay_vars.setColumnMinimumWidth(1, 48)
        lay_vars.setColumnStretch(2, 1)
        lay_vars.setColumnStretch(3, 1)
        lay_vars.setColumnStretch(4, 1)

        self.sp_vtg_start = QtWidgets.QDoubleSpinBox()
        self.sp_vtg_stop = QtWidgets.QDoubleSpinBox()
        self.sp_vtg_step = QtWidgets.QDoubleSpinBox()
        configure_volt_spinbox(self.sp_vtg_start, 0.0)
        configure_volt_spinbox(self.sp_vtg_stop, 1.0)
        configure_volt_spinbox(self.sp_vtg_step, 0.1)
        self.btn_set_vtg = QtWidgets.QPushButton("Set")
        self.btn_set_vtg.setFixedWidth(SET_BUTTON_WIDTH)
        self.lbl_vtg_mode = QtWidgets.QLabel()
        lay_vars.addWidget(QtWidgets.QLabel("Vtg"), 1, 0)
        lay_vars.addWidget(self.lbl_vtg_mode, 1, 1)
        lay_vars.addWidget(self.sp_vtg_start, 1, 2)
        lay_vars.addWidget(self.sp_vtg_stop, 1, 3)
        lay_vars.addWidget(self.sp_vtg_step, 1, 4)
        lay_vars.addWidget(self.btn_set_vtg, 1, 5)

        self.sp_vbg_start = QtWidgets.QDoubleSpinBox()
        self.sp_vbg_stop = QtWidgets.QDoubleSpinBox()
        self.sp_vbg_step = QtWidgets.QDoubleSpinBox()
        configure_volt_spinbox(self.sp_vbg_start, 0.0)
        configure_volt_spinbox(self.sp_vbg_stop, 1.0)
        configure_volt_spinbox(self.sp_vbg_step, 0.1)
        self.btn_set_vbg = QtWidgets.QPushButton("Set")
        self.btn_set_vbg.setFixedWidth(SET_BUTTON_WIDTH)
        self.lbl_vbg_mode = QtWidgets.QLabel()
        lay_vars.addWidget(QtWidgets.QLabel("Vbg"), 2, 0)
        lay_vars.addWidget(self.lbl_vbg_mode, 2, 1)
        lay_vars.addWidget(self.sp_vbg_start, 2, 2)
        lay_vars.addWidget(self.sp_vbg_stop, 2, 3)
        lay_vars.addWidget(self.sp_vbg_step, 2, 4)
        lay_vars.addWidget(self.btn_set_vbg, 2, 5)

        self.sp_vds_start = QtWidgets.QDoubleSpinBox()
        self.sp_vds_stop = QtWidgets.QDoubleSpinBox()
        self.sp_vds_step = QtWidgets.QDoubleSpinBox()
        configure_volt_spinbox(self.sp_vds_start, 0.0)
        configure_volt_spinbox(self.sp_vds_stop, 0.0)
        configure_volt_spinbox(self.sp_vds_step, 0.01)
        self.btn_set_vds = QtWidgets.QPushButton("Set")
        self.btn_set_vds.setFixedWidth(SET_BUTTON_WIDTH)
        self.lbl_vds_mode = QtWidgets.QLabel()
        lay_vars.addWidget(QtWidgets.QLabel("Vds"), 3, 0)
        lay_vars.addWidget(self.lbl_vds_mode, 3, 1)
        lay_vars.addWidget(self.sp_vds_start, 3, 2)
        lay_vars.addWidget(self.sp_vds_stop, 3, 3)
        lay_vars.addWidget(self.sp_vds_step, 3, 4)
        lay_vars.addWidget(self.btn_set_vds, 3, 5)
        ctl_layout.addWidget(grp_vars)

        row_tools = QtWidgets.QHBoxLayout()
        self.btn_preview = QtWidgets.QPushButton("Preview Sweep")
        row_tools.addWidget(self.btn_preview)
        ctl_layout.addLayout(row_tools)

        ctl_layout.addWidget(SectionHeader("Acquisition"))
        grp_time = QtWidgets.QGroupBox("Timing")
        form_time = QtWidgets.QFormLayout(grp_time)
        style_form_layout(form_time)
        self.sp_delay = QtWidgets.QDoubleSpinBox()
        self.sp_delay.setDecimals(3)
        self.sp_delay.setRange(0.0, 30.0)
        self.sp_delay.setValue(0.5)
        self.sp_nsamp = QtWidgets.QSpinBox()
        self.sp_nsamp.setRange(1, 1000)
        self.sp_nsamp.setValue(3)
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
        self.cbo_y = QtWidgets.QComboBox()
        self.cbo_y.addItems(["Ids_DC", "Ids_X", "Ids_Y"])
        lbl_base = QtWidgets.QLabel("Filename Stem:")
        lbl_y = QtWidgets.QLabel("Plot Axis:")
        form_output.addRow(lbl_base, self.ed_base)
        form_output.addRow(lbl_y, self.cbo_y)
        self.exp_output = CollapsibleSection("Output and Plot Options", grp_output, expanded=True)
        ctl_layout.addWidget(self.exp_output)
        self._add_output_preview_section(ctl_layout)

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
            self.ed_base, self.cbo_source, self.cbo_y, self.cbo_fast, self.cbo_slow, self.cbo_sweep_dim, self.sp_ratio,
        ]:
            set_standard_input_height(widget)

        for spinbox in [
            self.sp_vtg_start, self.sp_vtg_stop, self.sp_vtg_step,
            self.sp_vbg_start, self.sp_vbg_stop, self.sp_vbg_step,
            self.sp_vds_start, self.sp_vds_stop, self.sp_vds_step,
        ]:
            spinbox.setMinimumWidth(64)

        apply_tooltip("Choose whether this run is a single sweep or a two-axis map.", lbl_mode, self.cbo_sweep_dim)
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
        apply_tooltip("Back-gate weighting r used only when plotting the map as Doping/E-field.", lbl_ratio, self.sp_ratio)
        apply_tooltip("Wait time after each setpoint update before acquiring data.", lbl_delay, self.sp_delay)
        apply_tooltip("Number of DAQ reads averaged at each map point.", lbl_avg, self.sp_nsamp)
        apply_tooltip("Show the planned point order without running hardware acquisition.", self.btn_preview)
        apply_tooltip("Base filename for the output CSV.", lbl_base, self.ed_base)
        apply_tooltip("Select which current channel is drawn in the live plot.", lbl_y, self.cbo_y)

    def _wire(self):
        self.btn_start.clicked.connect(self.start_run)
        self.btn_stop.clicked.connect(self.stop_run)
        self.btn_preview.clicked.connect(self.on_preview)
        self.btn_set_vtg.clicked.connect(lambda: self.on_set_generic("Vtg", self.btn_set_vtg))
        self.btn_set_vbg.clicked.connect(lambda: self.on_set_generic("Vbg", self.btn_set_vbg))
        self.btn_set_vds.clicked.connect(lambda: self.on_set_generic("Vds", self.btn_set_vds))
        self.cbo_sweep_dim.currentIndexChanged.connect(self.on_sweep_type_changed)
        self.chk_link.toggled.connect(self.update_field_states)
        self.cbo_fast.currentIndexChanged.connect(self.on_fast_combo_changed)
        self.cbo_slow.currentIndexChanged.connect(self.on_slow_combo_changed)
        self.cbo_source.currentIndexChanged.connect(self._update_connection_hint)
        self.cbo_source.currentIndexChanged.connect(self._update_manual_buttons)
        self.cbo_source.currentIndexChanged.connect(self._update_plot_axis_choices)
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
        ):
            widget.valueChanged.connect(self.refresh_output_preview)
        for widget in (self.cbo_source, self.cbo_fast, self.cbo_slow, self.cbo_sweep_dim):
            widget.currentIndexChanged.connect(self.refresh_output_preview)
        self.chk_link.toggled.connect(self.refresh_output_preview)
        self.refresh_output_preview()

    def _is_2d_map(self) -> bool:
        return self.cbo_sweep_dim.currentText() == "2D map"

    def _swept_axes(self) -> list[str]:
        axes = [self.cbo_fast.currentText()]
        if self._is_2d_map() and self.cbo_slow.currentText() != "None":
            axes.append(self.cbo_slow.currentText())
        return axes

    def _axis_controls(self, axis: str):
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
        fast_count = len(self._axis_sequence(self.cbo_fast.currentText()))
        if not self._is_2d_map():
            return fast_count
        slow_count = len(self._axis_sequence(self.cbo_slow.currentText()))
        return fast_count * slow_count

    def _output_summary_parts(self) -> list[str]:
        swept_axes = self._swept_axes()
        source = "keithley_g3" if self.cbo_source.currentText() == "Keithley 2400" else self.cbo_source.currentText()
        parts = [
            f"fast_{self.cbo_fast.currentText()}",
            f"slow_{self.cbo_slow.currentText() if self._is_2d_map() else 'None'}",
            source,
        ]
        for axis in ("Vtg", "Vbg", "Vds"):
            start, stop, _step = self._axis_values(axis)
            if axis in swept_axes:
                parts.append(f"{axis}_{start:g}to{stop:g}V")
            else:
                parts.append(f"fixed_{axis}_{start:g}V")
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
            ("plot_y", self.cbo_y),
            ("sweep_dim", self.cbo_sweep_dim),
            ("fast_axis", self.cbo_fast),
            ("slow_axis", self.cbo_slow),
            ("link_doping_efield", self.chk_link),
            ("ratio", self.sp_ratio),
            ("vtg_start", self.sp_vtg_start),
            ("vtg_stop", self.sp_vtg_stop),
            ("vtg_step", self.sp_vtg_step),
            ("vbg_start", self.sp_vbg_start),
            ("vbg_stop", self.sp_vbg_stop),
            ("vbg_step", self.sp_vbg_step),
            ("vds_start", self.sp_vds_start),
            ("vds_stop", self.sp_vds_stop),
            ("vds_step", self.sp_vds_step),
            ("delay", self.sp_delay),
            ("averages", self.sp_nsamp),
        ]

    def _load_tab_settings(self):
        self._load_tab_widget_settings(self.SETTINGS_PREFIX, self._settings_widgets())
        self.on_sweep_type_changed()
        self._update_plot_axis_choices()
        self.set_plot_axis_source(self.cbo_y.currentText())
        self._update_sweep_summary()
        self.refresh_output_preview()

    def _bind_tab_settings(self):
        self._bind_tab_widget_settings(self.SETTINGS_PREFIX, self._settings_widgets())

    def save_tab_settings(self):
        self._save_tab_widget_settings(self.SETTINGS_PREFIX, self._settings_widgets())

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
        self.btn_start.setEnabled(self.s_daq is not None and source_ready and self.worker_thread is None)
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
        self._update_manual_buttons()

    def _required_devices(self) -> list[str]:
        required = ["daq"]
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
        if self.chk_link.isChecked() and not self._link_plot_available():
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
            text = "Hardware is busy with another connection or disconnect operation from Instrument Setup."
            self.lbl_connection_hint.setProperty("role", "warning-hint")
            self.btn_start.setToolTip("Wait for the dock connection operation to finish")
        elif missing_required:
            text = f"Required before start: {', '.join(missing_required)}. Connect from Instrument Setup."
            if missing_optional:
                text += f" Optional gate controls unavailable: {', '.join(missing_optional)}."
            self.lbl_connection_hint.setProperty("role", "warning-hint")
            self.btn_start.setToolTip(f"Connect required devices from Instrument Setup: {', '.join(missing_required)}")
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
        self._update_slow_combo_items_grid()
        self.update_field_states()

    def _update_slow_combo_items_grid(self):
        if self._updating_combos:
            return
        self._updating_combos = True
        fast = self.cbo_fast.currentText()
        current_slow = self.cbo_slow.currentText()
        self.cbo_slow.blockSignals(True)
        self.cbo_slow.clear()
        if self._is_2d_map():
            for axis in ["Vtg", "Vbg", "Vds"]:
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
        self.sp_ratio.setEnabled(self.chk_link.isChecked())
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
        self._updating_combos = False
        self._update_sweep_summary()
        self.on_axis_change_label()

    def _format_axis_summary(self, axis: str) -> str:
        start, stop, step = self._axis_values(axis)
        if axis in self._swept_axes():
            return f"{axis}: {start:g} to {stop:g} V, step {abs(step):g} V"
        return f"{axis}: fixed {start:g} V"

    def _update_sweep_summary(self):
        mode = "2D map" if self._is_2d_map() else "1D sweep"
        fast = self.cbo_fast.currentText()
        slow = self.cbo_slow.currentText() if self._is_2d_map() else "None"
        axes = "; ".join(self._format_axis_summary(axis) for axis in ("Vtg", "Vbg", "Vds"))
        try:
            points = self._point_count()
        except Exception:
            points = 0
        order = f"Fast: {fast}; Slow: {slow}" if self._is_2d_map() else f"Sweep: {fast}; fixed axes use Start / Fixed"
        if self._is_2d_map():
            order += "; alternate slow passes run the fast axis in reverse."
        self.lbl_sweep_summary.setText(f"{mode}. {order}\n{axes}\nEstimated points: {points}")
        self.refresh_output_preview()

    def on_axis_change_label(self):
        if self.plot.current_plot_mode() == "4-Channel Compare" and self._plot_records:
            self._redraw_plot()
            return
        fast = self.cbo_fast.currentText()
        if self.chk_link.isChecked() and self._link_plot_available():
            r = self.sp_ratio.value()
            self.plot.ax.set_xlabel(f"Doping (Vtg + {r:.2f}*Vbg)")
        else:
            self.plot.ax.set_xlabel(f"{fast} (V)")
        self.plot.ax.set_ylabel(f"{self.cbo_y.currentText()} (A)")
        self.plot.canvas.draw_idle()

    def _link_plot_available(self) -> bool:
        return bool({"Vtg", "Vbg"} & set(self._swept_axes()))

    def on_preview(self):
        self.plot.ax.clear()
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
                    ratio = self.sp_ratio.value()
                    xs.append(curr_vtg + ratio * curr_vbg)
                    ys.append(curr_vtg - ratio * curr_vbg)
                else:
                    xs.append(f_val)
                    ys.append(s_val if slow_axis != "None" else 0.0)

        self.plot.ax.plot(xs, ys, "o-", markersize=4, linewidth=1.0, color="blue", alpha=0.6 if use_ratio else 1.0)
        if use_ratio:
            self.plot.ax.set_xlabel("Doping (Vtg + r*Vbg)")
            self.plot.ax.set_ylabel("E-field (Vtg - r*Vbg)")
            self.plot.ax.set_title(f"{self.cbo_sweep_dim.currentText()} Preview: {len(xs)} pts")
        else:
            self.plot.ax.set_xlabel(f"{fast_axis} (V)")
            self.plot.ax.set_ylabel(f"{slow_axis if slow_axis != 'None' else 'Point order'} (V)")
            self.plot.ax.set_title(f"{self.cbo_sweep_dim.currentText()} Preview: {len(xs)} pts")
        self.plot.ax.grid(True)
        self.plot.canvas.draw_idle()

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
                safe_ramp(
                    lambda v: sess.set_voltage(idx, v),
                    sess.get_ao_value(idx),
                    val,
                    SAFE_RAMP_STEP_V,
                    SAFE_RAMP_STEP_T,
                )
                self.log.appendPlainText(f"Set {name} -> {val} ({SAFE_RAMP_STEP_V:g} V/step)")
                flash_button_success(button)
            except Exception as ex:
                self.log.appendPlainText(str(ex))
        else:
            self.log.appendPlainText(f"{name} not connected")

    def collect_params(self):
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
        self.p.axis_fast = self.cbo_fast.currentText()
        self.p.axis_slow = self.cbo_slow.currentText() if self._is_2d_map() else "None"
        self.p.ratio = self.sp_ratio.value()
        self.p.delay = self.sp_delay.value()
        self.p.n_sample = self.sp_nsamp.value()
        self.p.plot_choice = self.cbo_y.currentText()
        self.p.vg_ramp = GATE_BIAS_RAMP_STEP_V

    def start_run(self):
        if self.worker_thread:
            return
        mw = self.window()
        if hasattr(mw, "refresh_models_from_ui"):
            mw.refresh_models_from_ui()
        self.refresh_output_preview()
        if not self.validate_output_ready(self.save):
            return
        if not self._validate_sweep_setup():
            return
        if not self._validate_required_sessions():
            return
        try:
            self.collect_params()
        except Exception as ex:
            QtWidgets.QMessageBox.warning(self, "Invalid Parameters", str(ex))
            return
        claimed, blocked = self.device_manager.mark_in_use(self._required_devices())
        if not claimed:
            QtWidgets.QMessageBox.warning(self, "Busy", f"Devices already in use: {', '.join(blocked).upper()}")
            return
        self._plot_records = []
        self.plot.clear()
        self.set_plot_axis_source(self.p.plot_choice)
        try:
            self.begin_run_logging(self._planned_output, "2D Map" if self._is_2d_map() else "1D Sweep")
            amp, lkn = self.get_global_rates()
            self.worker = CoSweepWorker(self.p, self.save, self.conns, g1=self.s_g1, g2=self.s_g2, g3=self.s_g3, daq=self.s_daq, plot_choice=self.p.plot_choice, amp_rate=amp, lkn_rate=lkn)
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

    def _clear_plot(self):
        self.plot.clear()
        self._plot_records = []
        self.on_axis_change_label()

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
        xs = [record["x"] for record in self._plot_records]
        if self.plot.current_plot_mode() == "4-Channel Compare":
            axes = self.plot.get_axes()
            channels = self.plot.compare_channels()
            for axis, channel in zip(axes, channels):
                axis.clear()
                ys = [plot_channel_value(record, channel) for record in self._plot_records]
                if xs:
                    axis.plot(xs, ys, "o-")
                    axis.relim()
                    axis.autoscale_view()
                axis.set_ylabel(f"{channel} (A)")
                axis.grid(True)
            if axes:
                fast = self.cbo_fast.currentText()
                if self.chk_link.isChecked() and self._link_plot_available():
                    r = self.sp_ratio.value()
                    axes[-1].set_xlabel(f"Doping (Vtg + {r:.2f}*Vbg)")
                else:
                    axes[-1].set_xlabel(f"{fast} (V)")
                self.plot.canvas.draw_idle()
        else:
            source = self.cbo_y.currentText()
            ax = self.plot.ax
            ax.clear()
            ys = [plot_channel_value(record, source) for record in self._plot_records]
            if xs:
                ax.plot(xs, ys, "o-")
                ax.relim()
                ax.autoscale_view()
            ax.grid(True)
            self.on_axis_change_label()

    def _cleanup_thread(self):
        if self.worker:
            self.worker.deleteLater()
            self.worker = None
        self.worker_thread = None
        self.device_manager.release(self._required_devices())
        self.run_panel.set_running(False)
        self._update_manual_buttons()

    def on_error(self, msg):
        self.set_status("Run error", "error", msg)
        self.append_log("ERROR: " + msg)
        self.end_run_logging("error", msg)
        self._output_run_id = None
        self.refresh_output_preview()

    def on_finished(self, path):
        self.set_status("Finished", "done", path)
        self.append_log(f"Saved: {path}")
        self.end_run_logging("finished", path)
        self._output_run_id = None
        self.refresh_output_preview()

    def on_stopped(self, message: str):
        self.set_status("Stopped by user", "done", message)
        self.append_log(message)
        self.end_run_logging("stopped", message)
        self._output_run_id = None
        self.refresh_output_preview()
