from __future__ import annotations

from PyQt6 import QtCore, QtWidgets

from app.constants import V_LIMIT
from app.device_manager import DeviceManager
from app.models import Connections, LineSweepParams, SaveRoot
from app.result_channels import compare_channel_options, plot_channel_options, plot_channel_value
from app.ui.helpers import apply_tooltip, configure_volt_spinbox, set_standard_input_height, style_form_layout
from app.ui.tabs.base_tab import BaseMeasurementTab
from app.ui.widgets.collapsible_section import CollapsibleSection
from app.ui.widgets.status_panel import SectionHeader, StatusPanel
from app.workers.line_sweep import LineSweepWorker


class GateScanTab(BaseMeasurementTab):
    def __init__(self, save: SaveRoot, conns: Connections, device_manager: DeviceManager, get_global_rates_callable=None, get_ao_items_callable=None):
        self.save = save
        self.conns = conns
        self.device_manager = device_manager
        self.get_global_rates = get_global_rates_callable or (lambda: (1e7, 100.0))
        self.get_ao_items = get_ao_items_callable or (lambda: ["ao0", "ao1"])
        self.p = LineSweepParams()
        self.s_g1 = self.s_g2 = self.s_g3 = self.s_daq = None
        self.worker_thread = None
        self.worker = None
        self._plot_records = []
        super().__init__("START SWEEP", "Sweep Axis", "Ids (A)", ["g1", "g2", "g3", "daq"])
        self.control_scroll.setMinimumWidth(430)
        self.control_scroll.setMaximumWidth(620)
        self.main_splitter.setSizes([470, 820])
        self._wire()
        self.btn_start.setToolTip("Connect instruments first")
        self.device_manager.status_changed.connect(self._on_device_status_changed)
        self.device_manager.operation_changed.connect(self._on_operation_changed)
        self._sync_sessions_from_manager()
        self._update_manual_buttons()
        self._update_mode_ui()

    def _build_control_panel(self, ctl_layout: QtWidgets.QVBoxLayout):
        ctl_layout.addWidget(SectionHeader("Mode"))
        grp_mode = QtWidgets.QGroupBox("Gate Scan Mode")
        lay_mode = QtWidgets.QHBoxLayout(grp_mode)
        lay_mode.setContentsMargins(10, 18, 10, 10)
        lay_mode.setSpacing(16)
        self.rad_mode_raw = QtWidgets.QRadioButton("Raw Trajectory")
        self.rad_mode_derived = QtWidgets.QRadioButton("Derived (Doping/Efield)")
        self.rad_mode_raw.setChecked(True)
        lay_mode.addWidget(self.rad_mode_raw)
        lay_mode.addWidget(self.rad_mode_derived)
        lay_mode.addStretch(1)
        ctl_layout.addWidget(grp_mode)

        ctl_layout.addWidget(SectionHeader("Trajectory"))
        self.mode_stack = QtWidgets.QStackedWidget()
        self.mode_stack.addWidget(self._build_raw_mode_widget())
        self.mode_stack.addWidget(self._build_derived_mode_widget())
        ctl_layout.addWidget(self.mode_stack)

        ctl_layout.addWidget(SectionHeader("Acquisition"))
        grp_acq = QtWidgets.QGroupBox("Timing")
        form_acq = QtWidgets.QFormLayout(grp_acq)
        style_form_layout(form_acq)
        self.sp_n_points = QtWidgets.QSpinBox()
        self.sp_n_points.setRange(1, 100000)
        self.sp_n_points.setValue(self.p.n_points)
        self.lbl_eta = QtWidgets.QLabel()
        self.lbl_eta.setProperty("role", "hint")
        self.sp_vg_ramp = QtWidgets.QDoubleSpinBox()
        self.sp_vg_ramp.setDecimals(3)
        self.sp_vg_ramp.setRange(1e-3, 5.0)
        self.sp_vg_ramp.setValue(self.p.vg_ramp)
        self.sp_vds_ramp = QtWidgets.QDoubleSpinBox()
        self.sp_vds_ramp.setDecimals(3)
        self.sp_vds_ramp.setRange(1e-3, 5.0)
        self.sp_vds_ramp.setValue(self.p.vds_ramp)
        self.sp_delay = QtWidgets.QDoubleSpinBox()
        self.sp_delay.setDecimals(3)
        self.sp_delay.setRange(0.0, 30.0)
        self.sp_delay.setValue(self.p.delay)
        self.sp_nsamp = QtWidgets.QSpinBox()
        self.sp_nsamp.setRange(1, 1000)
        self.sp_nsamp.setValue(self.p.n_sample)
        lbl_points = QtWidgets.QLabel("N points:")
        lbl_eta = QtWidgets.QLabel("Estimated time:")
        lbl_vg_ramp = QtWidgets.QLabel("Gate Ramp (V/s):")
        lbl_vds_ramp = QtWidgets.QLabel("Vds Ramp (V/s):")
        lbl_delay = QtWidgets.QLabel("Delay (s):")
        lbl_avg = QtWidgets.QLabel("Averages:")
        form_acq.addRow(lbl_points, self.sp_n_points)
        form_acq.addRow(lbl_eta, self.lbl_eta)
        form_acq.addRow(lbl_vg_ramp, self.sp_vg_ramp)
        form_acq.addRow(lbl_vds_ramp, self.sp_vds_ramp)
        form_acq.addRow(lbl_delay, self.sp_delay)
        form_acq.addRow(lbl_avg, self.sp_nsamp)
        ctl_layout.addWidget(grp_acq)

        ctl_layout.addWidget(SectionHeader("Advanced"))
        grp_output = QtWidgets.QGroupBox("Output and Plot")
        form_output = QtWidgets.QFormLayout(grp_output)
        style_form_layout(form_output)
        self.cbo_source = QtWidgets.QComboBox()
        self.cbo_source.addItems(["Keithley 2400"])
        self.cbo_x = QtWidgets.QComboBox()
        self.cbo_x.addItems(["Auto", "Step Index", "Vtg", "Vbg", "Vds", "Doping", "Efield"])
        self.cbo_y = QtWidgets.QComboBox()
        self.cbo_y.addItems(["Ids_DC", "Ids_X", "Ids_Y"])
        self.ed_base = QtWidgets.QLineEdit(self.p.base_name)
        lbl_source = QtWidgets.QLabel("Vds Source:")
        lbl_x = QtWidgets.QLabel("Plot X Axis:")
        lbl_y = QtWidgets.QLabel("Plot Y Axis:")
        lbl_base = QtWidgets.QLabel("Filename:")
        form_output.addRow(lbl_source, self.cbo_source)
        form_output.addRow(lbl_x, self.cbo_x)
        form_output.addRow(lbl_y, self.cbo_y)
        form_output.addRow(lbl_base, self.ed_base)
        self.exp_output = CollapsibleSection("Output and Plot Options", grp_output, expanded=False)
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

        widgets = [
            self.sp_n_points, self.sp_vg_ramp, self.sp_vds_ramp, self.sp_delay, self.sp_nsamp,
            self.cbo_source, self.cbo_x, self.cbo_y, self.ed_base,
            self.sp_raw_vtg_start, self.sp_raw_vtg_stop, self.sp_raw_vbg_start, self.sp_raw_vbg_stop,
            self.sp_raw_vds_start, self.sp_raw_vds_stop, self.sp_ratio, self.sp_derived_start,
            self.sp_derived_stop, self.sp_derived_fixed, self.cbo_derived_vds_mode,
            self.sp_derived_vds_fixed, self.sp_derived_vds_start, self.sp_derived_vds_stop,
        ]
        for widget in widgets:
            set_standard_input_height(widget)

        for spinbox in (
            self.sp_raw_vtg_start, self.sp_raw_vtg_stop, self.sp_raw_vbg_start, self.sp_raw_vbg_stop,
            self.sp_raw_vds_start, self.sp_raw_vds_stop, self.sp_derived_start, self.sp_derived_stop,
            self.sp_derived_fixed, self.sp_derived_vds_fixed, self.sp_derived_vds_start, self.sp_derived_vds_stop,
        ):
            spinbox.setMinimumWidth(88)

        apply_tooltip("Switch between direct voltage trajectories and derived Doping/Efield trajectories.", self.rad_mode_raw, self.rad_mode_derived)
        apply_tooltip("Number of measurement points in the full line scan.", lbl_points, self.sp_n_points)
        apply_tooltip("Rough estimate based on ramp distances and per-point delay.", lbl_eta, self.lbl_eta)
        apply_tooltip("Ramp speed used for gate motion between line-scan points.", lbl_vg_ramp, self.sp_vg_ramp)
        apply_tooltip("Ramp speed used for Vds updates when Vds changes along the line.", lbl_vds_ramp, self.sp_vds_ramp)
        apply_tooltip("Wait time after each point is set before data acquisition.", lbl_delay, self.sp_delay)
        apply_tooltip("Number of DAQ reads averaged at each line-scan point.", lbl_avg, self.sp_nsamp)
        apply_tooltip("Choose Keithley G3 or an NI AO channel as the Vds source.", lbl_source, self.cbo_source)
        apply_tooltip("Auto uses the active sweep quantity for the live plot x-axis.", lbl_x, self.cbo_x)
        apply_tooltip("Select which current channel is shown live.", lbl_y, self.cbo_y)
        apply_tooltip("Base filename for the saved line-scan CSV.", lbl_base, self.ed_base)

    def _build_raw_mode_widget(self) -> QtWidgets.QWidget:
        grp = QtWidgets.QGroupBox("Raw Voltage Trajectory")
        lay = QtWidgets.QGridLayout(grp)
        lay.setContentsMargins(8, 16, 8, 8)
        lay.setHorizontalSpacing(4)
        lay.setVerticalSpacing(8)
        lay.addWidget(QtWidgets.QLabel("Start"), 0, 1)
        lay.addWidget(QtWidgets.QLabel("Stop"), 0, 2)
        lay.addWidget(QtWidgets.QLabel("Active"), 0, 3)

        self.sp_raw_vtg_start, self.sp_raw_vtg_stop, self.chk_raw_vtg_active, self.lbl_raw_vtg_mode = self._add_raw_row(lay, 1, "Vtg (V):", 0.0, 1.0, True)
        self.sp_raw_vbg_start, self.sp_raw_vbg_stop, self.chk_raw_vbg_active, self.lbl_raw_vbg_mode = self._add_raw_row(lay, 2, "Vbg (V):", 0.0, 0.0, False)
        self.sp_raw_vds_start, self.sp_raw_vds_stop, self.chk_raw_vds_active, self.lbl_raw_vds_mode = self._add_raw_row(lay, 3, "Vds (V):", 0.0, 0.0, False)
        lay.setColumnStretch(1, 1)
        lay.setColumnStretch(2, 1)
        lay.setColumnMinimumWidth(0, 64)
        lay.setColumnMinimumWidth(3, 92)
        return grp

    def _build_derived_mode_widget(self) -> QtWidgets.QWidget:
        grp = QtWidgets.QGroupBox("Derived Trajectory")
        form = QtWidgets.QFormLayout(grp)
        style_form_layout(form)
        self.sp_ratio = QtWidgets.QDoubleSpinBox()
        self.sp_ratio.setDecimals(4)
        self.sp_ratio.setRange(-1e4, 1e4)
        self.sp_ratio.setValue(self.p.derived_ratio)

        sweep_wrap = QtWidgets.QWidget()
        sweep_row = QtWidgets.QHBoxLayout(sweep_wrap)
        sweep_row.setContentsMargins(0, 0, 0, 0)
        sweep_row.setSpacing(12)
        self.rad_sweep_doping = QtWidgets.QRadioButton("Doping")
        self.rad_sweep_efield = QtWidgets.QRadioButton("Efield")
        self.rad_sweep_doping.setChecked(True)
        sweep_row.addWidget(self.rad_sweep_doping)
        sweep_row.addWidget(self.rad_sweep_efield)
        sweep_row.addStretch(1)

        self.sp_derived_start = QtWidgets.QDoubleSpinBox()
        self.sp_derived_stop = QtWidgets.QDoubleSpinBox()
        self.sp_derived_fixed = QtWidgets.QDoubleSpinBox()
        for spinbox, value in (
            (self.sp_derived_start, 0.0),
            (self.sp_derived_stop, 1.0),
            (self.sp_derived_fixed, 0.0),
        ):
            configure_volt_spinbox(spinbox, value)

        self.lbl_fixed_name = QtWidgets.QLabel("Efield (fixed):")
        self.lbl_derived_range = QtWidgets.QLabel()
        self.lbl_derived_range.setWordWrap(True)
        self.lbl_derived_range.setProperty("role", "hint")

        self.cbo_derived_vds_mode = QtWidgets.QComboBox()
        self.cbo_derived_vds_mode.addItems(["Fixed", "Swept"])
        self.derived_vds_stack = QtWidgets.QStackedWidget()
        self.derived_vds_stack.addWidget(self._build_derived_vds_fixed_widget())
        self.derived_vds_stack.addWidget(self._build_derived_vds_swept_widget())

        form.addRow("Ratio (r):", self.sp_ratio)
        form.addRow("Sweep:", sweep_wrap)
        form.addRow("Start:", self.sp_derived_start)
        form.addRow("Stop:", self.sp_derived_stop)
        form.addRow(self.lbl_fixed_name, self.sp_derived_fixed)
        form.addRow("Vds:", self.cbo_derived_vds_mode)
        form.addRow("Vds Values:", self.derived_vds_stack)
        form.addRow("Computed Range:", self.lbl_derived_range)
        apply_tooltip("Back-gate weighting used in the Doping and Efield definitions.", self.sp_ratio)
        apply_tooltip("Choose which derived quantity is swept along the line.", self.rad_sweep_doping, self.rad_sweep_efield)
        apply_tooltip("Start of the selected Doping/Efield sweep.", self.sp_derived_start)
        apply_tooltip("Stop of the selected Doping/Efield sweep.", self.sp_derived_stop)
        apply_tooltip("Fixed value of the non-swept derived quantity.", self.sp_derived_fixed)
        apply_tooltip("Keep Vds fixed or sweep it together with the gate trajectory.", self.cbo_derived_vds_mode)
        apply_tooltip("Single Vds bias used for the whole derived scan.", self.sp_derived_vds_fixed)
        apply_tooltip("Start Vds for a simultaneous Vds sweep in derived mode.", self.sp_derived_vds_start)
        apply_tooltip("Stop Vds for a simultaneous Vds sweep in derived mode.", self.sp_derived_vds_stop)
        return grp

    def _build_derived_vds_fixed_widget(self) -> QtWidgets.QWidget:
        wrap = QtWidgets.QWidget()
        row = QtWidgets.QHBoxLayout(wrap)
        row.setContentsMargins(0, 0, 0, 0)
        self.sp_derived_vds_fixed = QtWidgets.QDoubleSpinBox()
        configure_volt_spinbox(self.sp_derived_vds_fixed, 0.0)
        row.addWidget(self.sp_derived_vds_fixed)
        return wrap

    def _build_derived_vds_swept_widget(self) -> QtWidgets.QWidget:
        wrap = QtWidgets.QWidget()
        row = QtWidgets.QHBoxLayout(wrap)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(6)
        self.sp_derived_vds_start = QtWidgets.QDoubleSpinBox()
        self.sp_derived_vds_stop = QtWidgets.QDoubleSpinBox()
        configure_volt_spinbox(self.sp_derived_vds_start, 0.0)
        configure_volt_spinbox(self.sp_derived_vds_stop, 0.5)
        row.addWidget(QtWidgets.QLabel("Start"))
        row.addWidget(self.sp_derived_vds_start, 1)
        row.addWidget(QtWidgets.QLabel("Stop"))
        row.addWidget(self.sp_derived_vds_stop, 1)
        return wrap

    def _add_raw_row(self, layout: QtWidgets.QGridLayout, row: int, label: str, start: float, stop: float, active: bool):
        start_box = QtWidgets.QDoubleSpinBox()
        stop_box = QtWidgets.QDoubleSpinBox()
        configure_volt_spinbox(start_box, start)
        configure_volt_spinbox(stop_box, stop)
        check = QtWidgets.QCheckBox("Sweep")
        check.setChecked(active)
        mode_label = QtWidgets.QLabel()
        mode_label.setProperty("role", "hint")
        state_wrap = QtWidgets.QWidget()
        state_row = QtWidgets.QHBoxLayout(state_wrap)
        state_row.setContentsMargins(0, 0, 0, 0)
        state_row.setSpacing(4)
        state_row.addWidget(check, 0, QtCore.Qt.AlignmentFlag.AlignCenter)
        state_row.addStretch(1)
        layout.addWidget(QtWidgets.QLabel(label), row, 0)
        layout.addWidget(start_box, row, 1)
        layout.addWidget(stop_box, row, 2)
        layout.addWidget(state_wrap, row, 3)
        layout.addWidget(mode_label, row + 1, 1, 1, 3)
        apply_tooltip("Value used when this variable is fixed, or the start of its trajectory when Active is checked.", start_box)
        apply_tooltip("End value used when this variable is active in the trajectory.", stop_box)
        apply_tooltip("Enable this variable to move with the shared line-scan index.", check)
        return start_box, stop_box, check, mode_label

    def _wire(self):
        self.btn_start.clicked.connect(self.start_run)
        self.btn_stop.clicked.connect(self.stop_run)
        self.rad_mode_raw.toggled.connect(self._update_mode_ui)
        self.rad_sweep_doping.toggled.connect(self._update_derived_labels)
        self.cbo_derived_vds_mode.currentIndexChanged.connect(self._update_derived_vds_mode)
        self.cbo_source.currentIndexChanged.connect(self._update_connection_hint)
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
            self.sp_vg_ramp, self.sp_vds_ramp, self.sp_delay, self.sp_nsamp,
        ):
            if hasattr(widget, "valueChanged"):
                widget.valueChanged.connect(self._update_eta)
            if hasattr(widget, "valueChanged"):
                widget.valueChanged.connect(self._update_derived_range_summary)

        self.rad_mode_raw.toggled.connect(self._update_connection_hint)
        self.rad_mode_derived.toggled.connect(self._update_connection_hint)
        self.rad_sweep_doping.toggled.connect(self._update_derived_range_summary)
        self.rad_sweep_efield.toggled.connect(self._update_derived_range_summary)
        self.cbo_derived_vds_mode.currentIndexChanged.connect(self._update_eta)
        self.cbo_derived_vds_mode.currentIndexChanged.connect(self._update_connection_hint)
        self.cbo_source.currentIndexChanged.connect(self._update_plot_axis_choices)
        self.cbo_y.currentTextChanged.connect(self.set_plot_axis_source)
        self._update_plot_axis_choices()
        self.plot.y_axis_changed.connect(self.set_plot_axis_source)
        self.plot.plot_mode_changed.connect(lambda _mode: self._redraw_plot())

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
        self.btn_start.setEnabled(all(self.device_manager.is_connected(name) for name in self._required_devices()) and self.worker_thread is None)
        self._update_connection_hint()

    def _required_devices(self) -> list[str]:
        required = ["daq", "g1", "g2"]
        if self.cbo_source.currentText() == "Keithley 2400":
            required.append("g3")
        return required

    def _validate_required_sessions(self) -> bool:
        self._sync_sessions_from_manager()
        missing = [name for name in self._required_devices() if not self.device_manager.is_connected(name)]
        if missing:
            QtWidgets.QMessageBox.warning(self, "Missing Device", f"Connect required devices first: {', '.join(missing).upper()}")
            return False
        return True

    def _update_mode_ui(self):
        raw_mode = self.rad_mode_raw.isChecked()
        self.mode_stack.setCurrentIndex(0 if raw_mode else 1)
        self._update_raw_row_states()
        self._update_derived_vds_mode()
        self._update_derived_labels()
        self._update_plot_axis_label()
        self._update_eta()
        self._update_connection_hint()

    def _update_raw_row_states(self):
        for start_box, stop_box, check, label in (
            (self.sp_raw_vtg_start, self.sp_raw_vtg_stop, self.chk_raw_vtg_active, self.lbl_raw_vtg_mode),
            (self.sp_raw_vbg_start, self.sp_raw_vbg_stop, self.chk_raw_vbg_active, self.lbl_raw_vbg_mode),
            (self.sp_raw_vds_start, self.sp_raw_vds_stop, self.chk_raw_vds_active, self.lbl_raw_vds_mode),
        ):
            active = check.isChecked()
            stop_box.setEnabled(active and self.rad_mode_raw.isChecked())
            check.setText("Sweep" if active else "Fixed")
            label.setText("Active trajectory range" if active else "Row is fixed at the Start value")

    def _update_derived_labels(self):
        if self.rad_sweep_doping.isChecked():
            self.lbl_fixed_name.setText("Efield (fixed):")
        else:
            self.lbl_fixed_name.setText("Doping (fixed):")
        self._update_derived_range_summary()
        self._update_plot_axis_label()

    def _update_derived_vds_mode(self):
        self.derived_vds_stack.setCurrentIndex(self.cbo_derived_vds_mode.currentIndex())
        self._update_eta()
        self._update_derived_range_summary()

    def _update_derived_range_summary(self):
        if self.rad_mode_raw.isChecked():
            return
        try:
            ratio = self.sp_ratio.value()
            if abs(ratio) < 1e-12:
                raise ValueError("Ratio cannot be zero.")
            start = self.sp_derived_start.value()
            stop = self.sp_derived_stop.value()
            fixed = self.sp_derived_fixed.value()
            if self.rad_sweep_doping.isChecked():
                doping_values = [start, stop]
                efield_values = [fixed, fixed]
            else:
                doping_values = [fixed, fixed]
                efield_values = [start, stop]
            vtg_values = [(d + e) / 2.0 for d, e in zip(doping_values, efield_values)]
            vbg_values = [(d - e) / (2.0 * ratio) for d, e in zip(doping_values, efield_values)]
            vtg_min, vtg_max = min(vtg_values), max(vtg_values)
            vbg_min, vbg_max = min(vbg_values), max(vbg_values)
            text = f"Vtg: {vtg_min:.3f} to {vtg_max:.3f} V   |   Vbg: {vbg_min:.3f} to {vbg_max:.3f} V"
            warn = any(abs(v) > V_LIMIT for v in (*vtg_values, *vbg_values))
            self.lbl_derived_range.setProperty("role", "warning-hint" if warn else "hint")
            if warn:
                text += f"   exceeds {V_LIMIT:.1f} V limit"
            self.lbl_derived_range.setText(text)
        except Exception as ex:
            self.lbl_derived_range.setProperty("role", "warning-hint")
            self.lbl_derived_range.setText(str(ex))
        self.lbl_derived_range.style().unpolish(self.lbl_derived_range)
        self.lbl_derived_range.style().polish(self.lbl_derived_range)

    def _update_plot_axis_label(self):
        if self.plot.current_plot_mode() == "4-Channel Compare" and self._plot_records:
            self._redraw_plot()
            return
        label = self.cbo_x.currentText()
        if label == "Auto":
            if self.rad_mode_derived.isChecked():
                label = "Doping" if self.rad_sweep_doping.isChecked() else "Efield"
            elif self.chk_raw_vds_active.isChecked():
                label = "Vds"
            elif self.chk_raw_vtg_active.isChecked():
                label = "Vtg"
            elif self.chk_raw_vbg_active.isChecked():
                label = "Vbg"
            else:
                label = "Step Index"
        self.plot.ax.set_xlabel(label)
        self.plot.ax.set_ylabel(f"{self.cbo_y.currentText()} (A)")
        self.plot.canvas.draw_idle()

    def _update_connection_hint(self):
        required = self._required_devices()
        missing_required = [name.upper() for name in required if not self.device_manager.is_connected(name)]
        mode_text = "raw trajectory" if self.rad_mode_raw.isChecked() else "derived trajectory"
        if self.device_manager.is_busy():
            text = "Hardware is busy with another connection or disconnect operation from Instrument Setup."
            self.lbl_connection_hint.setProperty("role", "warning-hint")
            self.btn_start.setToolTip("Wait for the dock connection operation to finish")
        elif missing_required:
            text = f"Required before start for {mode_text}: {', '.join(missing_required)}. Connect from Instrument Setup."
            self.lbl_connection_hint.setProperty("role", "warning-hint")
            self.btn_start.setToolTip(f"Connect required devices from Instrument Setup: {', '.join(missing_required)}")
        else:
            text = f"Ready to run {mode_text} with dock-managed sessions."
            self.lbl_connection_hint.setProperty("role", "hint")
            self.btn_start.setToolTip("Start gate scan")
        self.lbl_connection_hint.setText(text)
        self.lbl_connection_hint.style().unpolish(self.lbl_connection_hint)
        self.lbl_connection_hint.style().polish(self.lbl_connection_hint)

    def _estimate_seconds(self) -> float:
        count = max(1, self.sp_n_points.value())
        if self.rad_mode_raw.isChecked():
            vg_distance = 0.0
            if self.chk_raw_vtg_active.isChecked():
                vg_distance += abs(self.sp_raw_vtg_stop.value() - self.sp_raw_vtg_start.value())
            if self.chk_raw_vbg_active.isChecked():
                vg_distance += abs(self.sp_raw_vbg_stop.value() - self.sp_raw_vbg_start.value())
            if self.chk_raw_vds_active.isChecked():
                vds_distance = abs(self.sp_raw_vds_stop.value() - self.sp_raw_vds_start.value())
            else:
                vds_distance = 0.0
        else:
            try:
                ratio = self.sp_ratio.value()
                start = self.sp_derived_start.value()
                stop = self.sp_derived_stop.value()
                fixed = self.sp_derived_fixed.value()
                if self.rad_sweep_doping.isChecked():
                    endpoints = [(start, fixed), (stop, fixed)]
                else:
                    endpoints = [(fixed, start), (fixed, stop)]
                vtg_values = [(d + e) / 2.0 for d, e in endpoints]
                vbg_values = [(d - e) / (2.0 * ratio) for d, e in endpoints]
                vg_distance = abs(vtg_values[1] - vtg_values[0]) + abs(vbg_values[1] - vbg_values[0])
            except Exception:
                vg_distance = 0.0
            if self.cbo_derived_vds_mode.currentText() == "Swept":
                vds_distance = abs(self.sp_derived_vds_stop.value() - self.sp_derived_vds_start.value())
            else:
                vds_distance = 0.0
        gate_time = vg_distance / max(self.sp_vg_ramp.value(), 1e-6)
        vds_time = vds_distance / max(self.sp_vds_ramp.value(), 1e-6)
        sample_time = count * max(0.0, self.sp_delay.value())
        return gate_time + vds_time + sample_time

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
        self.p.base_name = self.ed_base.text()
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
        self.p.derived_axis = "Doping" if self.rad_sweep_doping.isChecked() else "Efield"
        self.p.derived_start = self.sp_derived_start.value()
        self.p.derived_stop = self.sp_derived_stop.value()
        self.p.derived_fixed = self.sp_derived_fixed.value()
        self.p.derived_vds_mode = self.cbo_derived_vds_mode.currentText()
        self.p.derived_vds_fixed = self.sp_derived_vds_fixed.value()
        self.p.derived_vds_start = self.sp_derived_vds_start.value()
        self.p.derived_vds_stop = self.sp_derived_vds_stop.value()
        self.p.n_points = self.sp_n_points.value()
        self.p.vg_ramp = self.sp_vg_ramp.value()
        self.p.vds_ramp = self.sp_vds_ramp.value()
        self.p.delay = self.sp_delay.value()
        self.p.n_sample = self.sp_nsamp.value()
        self.p.plot_choice = self.cbo_y.currentText()
        self.p.plot_x_axis = self.cbo_x.currentText()

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
        if not self._validate_required_sessions() or not self._validate_params():
            return
        claimed, blocked = self.device_manager.mark_in_use(self._required_devices())
        if not claimed:
            QtWidgets.QMessageBox.warning(self, "Busy", f"Devices already in use: {', '.join(blocked).upper()}")
            return
        self.collect_params()
        self._plot_records = []
        self.plot.clear()
        self.set_plot_axis_source(self.p.plot_choice)
        try:
            amp, lkn = self.get_global_rates()
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
            )
            self.worker_thread = QtCore.QThread()
            self.worker.moveToThread(self.worker_thread)
            self.worker_thread.started.connect(self.worker.run)
            self.worker.point_data.connect(self.on_point_data)
            self.worker.progress.connect(self.set_progress)
            self.worker.status.connect(lambda m: self.set_status(m, "running"))
            self.worker.log.connect(self.log.appendPlainText)
            self.worker.finished.connect(self.on_finished)
            self.worker.finished.connect(self.worker_thread.quit)
            self.worker.error.connect(self.on_error)
            self.worker.error.connect(self.worker_thread.quit)
            self.worker_thread.finished.connect(self._cleanup_thread)
            self.run_panel.set_running(True)
            self.set_status("Running...", "running")
            self.progress.setValue(0)
            self.worker_thread.start()
        except Exception as ex:
            self.log.appendPlainText(str(ex))
            self.device_manager.release(self._required_devices())

    def stop_run(self):
        if self.worker:
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
                label = self.cbo_x.currentText()
                if label == "Auto":
                    if self.rad_mode_derived.isChecked():
                        label = "Doping" if self.rad_sweep_doping.isChecked() else "Efield"
                    elif self.chk_raw_vds_active.isChecked():
                        label = "Vds"
                    elif self.chk_raw_vtg_active.isChecked():
                        label = "Vtg"
                    elif self.chk_raw_vbg_active.isChecked():
                        label = "Vbg"
                    else:
                        label = "Step Index"
                axes[-1].set_xlabel(label)
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
            self._update_plot_axis_label()
            ax.grid(True)
            self.plot.canvas.draw_idle()

    def on_finished(self, path: str):
        self.set_status("Finished", "done")
        self.log.appendPlainText(f"Saved: {path}")

    def on_error(self, msg: str):
        self.set_status("Run error", "error", msg)
        self.log.appendPlainText(msg)
