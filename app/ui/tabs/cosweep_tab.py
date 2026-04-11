from __future__ import annotations

from PyQt6 import QtCore, QtWidgets

from app.device_manager import DeviceManager
from app.models import CoParams, Connections, SaveRoot
from app.result_channels import compare_channel_options, plot_channel_options, plot_channel_value
from app.ui.helpers import apply_tooltip, configure_volt_spinbox, flash_button_success, set_standard_input_height, style_form_layout
from app.ui.tabs.base_tab import BaseMeasurementTab
from app.ui.widgets.collapsible_section import CollapsibleSection
from app.ui.widgets.status_panel import SectionHeader, StatusPanel
from app.utils import _frange_inc
from app.workers.cosweep import CoSweepWorker

SET_BUTTON_WIDTH = 48
COSWEEP_PANEL_MIN_WIDTH = 380
COSWEEP_PANEL_MAX_WIDTH = 560


class CoSweepTab(BaseMeasurementTab):
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
        super().__init__("START SWEEP", "Fast Axis", "Ids (A)", ["g1", "g2", "g3", "daq"])
        self.control_scroll.setMinimumWidth(COSWEEP_PANEL_MIN_WIDTH)
        self.control_scroll.setMaximumWidth(COSWEEP_PANEL_MAX_WIDTH)
        self.main_splitter.setSizes([430, 830])
        self._wire()
        self.btn_start.setToolTip("Connect instruments first")
        self.device_manager.status_changed.connect(self._on_device_status_changed)
        self.device_manager.operation_changed.connect(self._on_operation_changed)
        self._sync_sessions_from_manager()
        self._update_manual_buttons()
        self.on_fast_combo_changed()

    def _build_control_panel(self, ctl_layout: QtWidgets.QVBoxLayout):
        ctl_layout.addWidget(SectionHeader("Sweep Parameters"))
        grp_vars = QtWidgets.QGroupBox("Sweep Parameters")
        lay_vars = QtWidgets.QGridLayout(grp_vars)
        lay_vars.setContentsMargins(8, 16, 8, 8)
        lay_vars.setHorizontalSpacing(4)
        lay_vars.setVerticalSpacing(6)
        lay_vars.addWidget(QtWidgets.QLabel("Start"), 0, 1)
        lay_vars.addWidget(QtWidgets.QLabel("Stop"), 0, 2)
        lay_vars.addWidget(QtWidgets.QLabel("Step"), 0, 3)
        lay_vars.addWidget(QtWidgets.QLabel("Set"), 0, 4)
        lay_vars.setColumnMinimumWidth(0, 56)
        lay_vars.setColumnStretch(1, 1)
        lay_vars.setColumnStretch(2, 1)
        lay_vars.setColumnStretch(3, 1)

        self.sp_vtg_start = QtWidgets.QDoubleSpinBox()
        self.sp_vtg_stop = QtWidgets.QDoubleSpinBox()
        self.sp_vtg_step = QtWidgets.QDoubleSpinBox()
        configure_volt_spinbox(self.sp_vtg_start, 0.0)
        configure_volt_spinbox(self.sp_vtg_stop, 1.0)
        configure_volt_spinbox(self.sp_vtg_step, 0.1)
        self.btn_set_vtg = QtWidgets.QPushButton("Set")
        self.btn_set_vtg.setFixedWidth(SET_BUTTON_WIDTH)
        lay_vars.addWidget(QtWidgets.QLabel("Vtg (V):"), 1, 0)
        lay_vars.addWidget(self.sp_vtg_start, 1, 1)
        lay_vars.addWidget(self.sp_vtg_stop, 1, 2)
        lay_vars.addWidget(self.sp_vtg_step, 1, 3)
        lay_vars.addWidget(self.btn_set_vtg, 1, 4)

        self.sp_vbg_start = QtWidgets.QDoubleSpinBox()
        self.sp_vbg_stop = QtWidgets.QDoubleSpinBox()
        self.sp_vbg_step = QtWidgets.QDoubleSpinBox()
        configure_volt_spinbox(self.sp_vbg_start, 0.0)
        configure_volt_spinbox(self.sp_vbg_stop, 1.0)
        configure_volt_spinbox(self.sp_vbg_step, 0.1)
        self.btn_set_vbg = QtWidgets.QPushButton("Set")
        self.btn_set_vbg.setFixedWidth(SET_BUTTON_WIDTH)
        lay_vars.addWidget(QtWidgets.QLabel("Vbg (V):"), 2, 0)
        lay_vars.addWidget(self.sp_vbg_start, 2, 1)
        lay_vars.addWidget(self.sp_vbg_stop, 2, 2)
        lay_vars.addWidget(self.sp_vbg_step, 2, 3)
        lay_vars.addWidget(self.btn_set_vbg, 2, 4)

        self.sp_vds_start = QtWidgets.QDoubleSpinBox()
        self.sp_vds_stop = QtWidgets.QDoubleSpinBox()
        self.sp_vds_step = QtWidgets.QDoubleSpinBox()
        configure_volt_spinbox(self.sp_vds_start, 0.0)
        configure_volt_spinbox(self.sp_vds_stop, 0.0)
        configure_volt_spinbox(self.sp_vds_step, 0.01)
        self.btn_set_vds = QtWidgets.QPushButton("Set")
        self.btn_set_vds.setFixedWidth(SET_BUTTON_WIDTH)
        lay_vars.addWidget(QtWidgets.QLabel("Vds (V):"), 3, 0)
        lay_vars.addWidget(self.sp_vds_start, 3, 1)
        lay_vars.addWidget(self.sp_vds_stop, 3, 2)
        lay_vars.addWidget(self.sp_vds_step, 3, 3)
        lay_vars.addWidget(self.btn_set_vds, 3, 4)
        ctl_layout.addWidget(grp_vars)

        ctl_layout.addWidget(SectionHeader("Sweep Logic"))
        grp_logic = QtWidgets.QGroupBox("Sweep Logic")
        form_logic = QtWidgets.QFormLayout(grp_logic)
        style_form_layout(form_logic)
        self.chk_link = QtWidgets.QCheckBox("Plot as Doping/Efield axes")
        self.chk_link.setToolTip("Changes only the preview and plotted x-axis labels. Hardware control still uses the raw Vtg/Vbg/Vds grid.")
        self.cbo_fast = QtWidgets.QComboBox()
        self.cbo_fast.addItems(["Vtg", "Vbg", "Vds"])
        self.cbo_slow = QtWidgets.QComboBox()
        self.cbo_slow.addItems(["None", "Vtg", "Vbg", "Vds"])
        self.sp_ratio = QtWidgets.QDoubleSpinBox()
        self.sp_ratio.setDecimals(4)
        self.sp_ratio.setRange(-1e4, 1e4)
        self.sp_ratio.setValue(1.0)
        lbl_fast = QtWidgets.QLabel("Fast Axis:")
        lbl_slow = QtWidgets.QLabel("Slow Axis:")
        lbl_ratio = QtWidgets.QLabel("Ratio:")
        form_logic.addRow(self.chk_link)
        form_logic.addRow(lbl_fast, self.cbo_fast)
        form_logic.addRow(lbl_slow, self.cbo_slow)
        form_logic.addRow(lbl_ratio, self.sp_ratio)
        form_logic.addRow(QtWidgets.QLabel("(Plot-only: Doping/Efield are derived from Ratio * Vtg and Vbg.)"))
        ctl_layout.addWidget(grp_logic)

        ctl_layout.addWidget(SectionHeader("Acquisition"))
        grp_time = QtWidgets.QGroupBox("Timing")
        form_time = QtWidgets.QFormLayout(grp_time)
        style_form_layout(form_time)
        self.sp_delay = QtWidgets.QDoubleSpinBox()
        self.sp_delay.setValue(0.5)
        self.sp_nsamp = QtWidgets.QSpinBox()
        self.sp_nsamp.setValue(3)
        self.sp_vg_ramp = QtWidgets.QDoubleSpinBox()
        self.sp_vg_ramp.setValue(0.2)
        lbl_delay = QtWidgets.QLabel("Delay (s):")
        lbl_avg = QtWidgets.QLabel("Averages:")
        lbl_ramp = QtWidgets.QLabel("Ramp (V/s):")
        form_time.addRow(lbl_delay, self.sp_delay)
        form_time.addRow(lbl_avg, self.sp_nsamp)
        form_time.addRow(lbl_ramp, self.sp_vg_ramp)
        ctl_layout.addWidget(grp_time)

        ctl_layout.addWidget(SectionHeader("Advanced"))
        grp_output = QtWidgets.QGroupBox("Output Settings")
        form_output = QtWidgets.QFormLayout(grp_output)
        style_form_layout(form_output)
        self.ed_base = QtWidgets.QLineEdit(self.p.base_name)
        self.cbo_source = QtWidgets.QComboBox()
        self.cbo_source.addItems(["Keithley 2400"])
        self.cbo_y = QtWidgets.QComboBox()
        self.cbo_y.addItems(["Ids_DC", "Ids_X", "Ids_Y"])
        lbl_base = QtWidgets.QLabel("Filename:")
        lbl_source = QtWidgets.QLabel("Vds Source:")
        lbl_y = QtWidgets.QLabel("Plot Axis:")
        form_output.addRow(lbl_base, self.ed_base)
        form_output.addRow(lbl_source, self.cbo_source)
        form_output.addRow(lbl_y, self.cbo_y)
        self.exp_output = CollapsibleSection("Output and Plot Options", grp_output, expanded=False)
        ctl_layout.addWidget(self.exp_output)

        row_tools = QtWidgets.QHBoxLayout()
        self.btn_preview = QtWidgets.QPushButton("Preview Sweep")
        row_tools.addWidget(self.btn_preview)
        ctl_layout.addLayout(row_tools)

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
            self.sp_delay, self.sp_nsamp, self.sp_vg_ramp,
            self.ed_base, self.cbo_source, self.cbo_y, self.cbo_fast, self.cbo_slow, self.sp_ratio,
        ]:
            set_standard_input_height(widget)

        for spinbox in [
            self.sp_vtg_start, self.sp_vtg_stop, self.sp_vtg_step,
            self.sp_vbg_start, self.sp_vbg_stop, self.sp_vbg_step,
            self.sp_vds_start, self.sp_vds_stop, self.sp_vds_step,
        ]:
            spinbox.setMinimumWidth(90)

        apply_tooltip("First value used for this axis. Also used as the fixed value if the axis is not selected.", self.sp_vtg_start, self.sp_vbg_start, self.sp_vds_start)
        apply_tooltip("Last value included when this axis is part of the sweep.", self.sp_vtg_stop, self.sp_vbg_stop, self.sp_vds_stop)
        apply_tooltip("Point spacing for the selected sweep axis.", self.sp_vtg_step, self.sp_vbg_step, self.sp_vds_step)
        apply_tooltip("Apply the current Start value to hardware immediately.", self.btn_set_vtg, self.btn_set_vbg, self.btn_set_vds)
        apply_tooltip("Axis that moves for every point in the inner loop.", lbl_fast, self.cbo_fast)
        apply_tooltip("Axis that steps between fast-axis passes. Choose None for a 1D sweep.", lbl_slow, self.cbo_slow)
        apply_tooltip("BG weighting used only when plotting the map as Doping/Efield.", lbl_ratio, self.sp_ratio)
        apply_tooltip("Wait time after each setpoint update before acquiring data.", lbl_delay, self.sp_delay)
        apply_tooltip("Number of DAQ reads averaged at each map point.", lbl_avg, self.sp_nsamp)
        apply_tooltip("Ramp speed used for gate moves during the sweep.", lbl_ramp, self.sp_vg_ramp)
        apply_tooltip("Show the planned point order without running hardware acquisition.", self.btn_preview)
        apply_tooltip("Base filename for the output CSV.", lbl_base, self.ed_base)
        apply_tooltip("Choose Keithley G3 or an NI AO channel as the Vds source.", lbl_source, self.cbo_source)
        apply_tooltip("Select which current channel is drawn in the live plot.", lbl_y, self.cbo_y)

    def _wire(self):
        self.btn_start.clicked.connect(self.start_run)
        self.btn_stop.clicked.connect(self.stop_run)
        self.btn_preview.clicked.connect(self.on_preview)
        self.btn_set_vtg.clicked.connect(lambda: self.on_set_generic("Vtg", self.btn_set_vtg))
        self.btn_set_vbg.clicked.connect(lambda: self.on_set_generic("Vbg", self.btn_set_vbg))
        self.btn_set_vds.clicked.connect(lambda: self.on_set_generic("Vds", self.btn_set_vds))
        self.chk_link.clicked.connect(self.update_field_states)
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

    def _update_manual_buttons(self):
        self._sync_sessions_from_manager()
        self.btn_set_vtg.setEnabled(self.s_g1 is not None and self.device_manager.is_voltage_source_mode("g1"))
        self.btn_set_vbg.setEnabled(self.s_g2 is not None and self.device_manager.is_voltage_source_mode("g2"))
        self.btn_set_vds.setEnabled(
            ("NI DAQ" in self.cbo_source.currentText() and self.s_daq is not None)
            or (self.s_g3 is not None and self.device_manager.is_voltage_source_mode("g3"))
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
        self._update_connection_hint()

    def _required_devices(self) -> list[str]:
        required = ["daq"]
        if self.cbo_source.currentText() == "Keithley 2400":
            required.append("g3")
        return required

    def _validate_required_sessions(self) -> bool:
        self._sync_sessions_from_manager()
        missing = [name for name in self._required_devices() if not self.device_manager.is_connected(name)]
        if missing:
            QtWidgets.QMessageBox.warning(self, "Missing Device", f"Connect required devices first: {', '.join(missing).upper()}")
            return False
        if self.cbo_source.currentText() == "Keithley 2400" and not self.device_manager.is_voltage_source_mode("g3"):
            QtWidgets.QMessageBox.warning(self, "Keithley Mode", "G3 must be in 2-wire voltage source mode for a Keithley-driven 2D map.")
            return False
        return True

    def _update_connection_hint(self):
        required = self._required_devices()
        optional = ["g1", "g2"]
        missing_required = [name.upper() for name in required if not self.device_manager.is_connected(name)]
        missing_optional = [name.upper() for name in optional if not self.device_manager.is_connected(name)]
        if self.device_manager.is_connected("g1") and not self.device_manager.is_voltage_source_mode("g1"):
            missing_optional.append("G1 mode")
        if self.device_manager.is_connected("g2") and not self.device_manager.is_voltage_source_mode("g2"):
            missing_optional.append("G2 mode")
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

    def _update_slow_combo_items_grid(self):
        if self._updating_combos:
            return
        self._updating_combos = True
        fast = self.cbo_fast.currentText()
        current_slow = self.cbo_slow.currentText()
        self.cbo_slow.blockSignals(True)
        self.cbo_slow.clear()
        self.cbo_slow.addItem("None")
        for axis in ["Vtg", "Vbg", "Vds"]:
            if axis != fast:
                self.cbo_slow.addItem(axis)
        idx = self.cbo_slow.findText(current_slow)
        self.cbo_slow.setCurrentIndex(idx if idx >= 0 else 0)
        self.cbo_slow.blockSignals(False)
        self._updating_combos = False

    def update_field_states(self):
        if self._updating_combos:
            return
        self._updating_combos = True
        self.sp_ratio.setEnabled(self.chk_link.isChecked())
        active_sweep = [self.cbo_fast.currentText()]
        if self.cbo_slow.currentText() != "None":
            active_sweep.append(self.cbo_slow.currentText())
        self.sp_vtg_stop.setEnabled("Vtg" in active_sweep)
        self.sp_vtg_step.setEnabled("Vtg" in active_sweep)
        self.sp_vbg_stop.setEnabled("Vbg" in active_sweep)
        self.sp_vbg_step.setEnabled("Vbg" in active_sweep)
        self.sp_vds_stop.setEnabled("Vds" in active_sweep)
        self.sp_vds_step.setEnabled("Vds" in active_sweep)
        self._updating_combos = False
        self.on_axis_change_label()

    def on_axis_change_label(self):
        if self.plot.current_plot_mode() == "4-Channel Compare" and self._plot_records:
            self._redraw_plot()
            return
        fast = self.cbo_fast.currentText()
        if self.chk_link.isChecked():
            other = "Vbg" if fast == "Vtg" else "Vtg"
            r = self.sp_ratio.value()
            self.plot.ax.set_xlabel(f"Doping (r={r:.2f}*{fast} + {other})")
        else:
            self.plot.ax.set_xlabel(f"{fast} (V)")
        self.plot.ax.set_ylabel(f"{self.cbo_y.currentText()} (A)")
        self.plot.canvas.draw_idle()

    def on_preview(self):
        self.plot.ax.clear()
        use_ratio = self.chk_link.isChecked()
        fast_axis = self.cbo_fast.currentText()
        slow_axis = self.cbo_slow.currentText()

        def get_p(name):
            if name == "Vtg":
                return self.sp_vtg_start.value(), self.sp_vtg_stop.value(), self.sp_vtg_step.value()
            if name == "Vbg":
                return self.sp_vbg_start.value(), self.sp_vbg_stop.value(), self.sp_vbg_step.value()
            if name == "Vds":
                return self.sp_vds_start.value(), self.sp_vds_stop.value(), self.sp_vds_step.value()
            return 0, 0, 1

        f_start, f_stop, f_step = get_p(fast_axis)
        f_step = abs(f_step) * (1 if f_stop >= f_start else -1)
        f_seq = [f_start] if abs(f_step) < 1e-9 else _frange_inc(f_start, f_stop, f_step)

        s_seq = [0.0]
        if slow_axis != "None":
            s_start, s_stop, s_step = get_p(slow_axis)
            s_step = abs(s_step) * (1 if s_stop >= s_start else -1)
            s_seq = [s_start] if abs(s_step) < 1e-9 else _frange_inc(s_start, s_stop, s_step)

        xs, ys = [], []
        for s_val in s_seq:
            for f_val in f_seq:
                curr_vtg = f_val if fast_axis == "Vtg" else (s_val if slow_axis == "Vtg" else self.sp_vtg_start.value())
                curr_vbg = f_val if fast_axis == "Vbg" else (s_val if slow_axis == "Vbg" else self.sp_vbg_start.value())
                if use_ratio:
                    ratio = self.sp_ratio.value()
                    xs.append(ratio * curr_vtg + curr_vbg)
                    ys.append(ratio * curr_vtg - curr_vbg)
                else:
                    xs.append(f_val)
                    ys.append(s_val)

        self.plot.ax.scatter(xs, ys, s=15, c="blue", alpha=0.6 if use_ratio else 1.0)
        if use_ratio:
            self.plot.ax.set_xlabel("Doping (Ratio*Vtg + Vbg)")
            self.plot.ax.set_ylabel("Efield (Ratio*Vtg - Vbg)")
            self.plot.ax.set_title(f"Megasweep Preview: {len(xs)} pts")
        else:
            self.plot.ax.set_xlabel(f"{fast_axis} (V)")
            self.plot.ax.set_ylabel(f"{slow_axis if slow_axis != 'None' else 'Fixed'} (V)")
            self.plot.ax.set_title(f"Standard Grid: {len(xs)} pts")
        self.plot.ax.grid(True)
        self.plot.canvas.draw_idle()

    def on_set_generic(self, name, button):
        if name == "Vtg":
            val, sess = self.sp_vtg_start.value(), self.s_g1
        elif name == "Vbg":
            val, sess = self.sp_vbg_start.value(), self.s_g2
        else:
            val = self.sp_vds_start.value()
            sess = self.s_daq if "NI DAQ" in self.cbo_source.currentText() else self.s_g3
        if sess:
            try:
                sess.ramp_voltage(val, 0.5)
                self.log.appendPlainText(f"Set {name} -> {val}")
                flash_button_success(button)
            except Exception as ex:
                self.log.appendPlainText(str(ex))
        else:
            self.log.appendPlainText(f"{name} not connected")

    def collect_params(self):
        self.p.base_name = self.ed_base.text()
        src = self.cbo_source.currentText()
        if "NI DAQ" in src:
            self.p.vds_source = "NI DAQ AO"
            self.p.ao_channel = int(src.split()[-1].replace("ao", ""))
        else:
            self.p.vds_source = "Keithley 2400"
        self.p.vtg_start = self.sp_vtg_start.value()
        self.p.vtg_stop = self.sp_vtg_stop.value()
        self.p.vtg_step = self.sp_vtg_step.value()
        self.p.vbg_start = self.sp_vbg_start.value()
        self.p.vbg_stop = self.sp_vbg_stop.value()
        self.p.vbg_step = self.sp_vbg_step.value()
        self.p.vds_start = self.sp_vds_start.value()
        self.p.vds_stop = self.sp_vds_stop.value()
        self.p.vds_step = self.sp_vds_step.value()
        self.p.mode = "Linked" if self.chk_link.isChecked() else "Grid"
        self.p.axis_fast = self.cbo_fast.currentText()
        self.p.axis_slow = self.cbo_slow.currentText()
        self.p.ratio = self.sp_ratio.value()
        self.p.delay = self.sp_delay.value()
        self.p.n_sample = self.sp_nsamp.value()
        self.p.plot_choice = self.cbo_y.currentText()
        self.p.vg_ramp = self.sp_vg_ramp.value()

    def start_run(self):
        if self.worker_thread:
            return
        mw = self.window()
        if hasattr(mw, "refresh_models_from_ui"):
            mw.refresh_models_from_ui()
        if not self._validate_required_sessions():
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
            self.worker = CoSweepWorker(self.p, self.save, self.conns, g1=self.s_g1, g2=self.s_g2, g3=self.s_g3, daq=self.s_daq, plot_choice=self.p.plot_choice, amp_rate=amp, lkn_rate=lkn)
            self.worker_thread = QtCore.QThread()
            self.worker.moveToThread(self.worker_thread)
            self.worker_thread.started.connect(self.worker.run)
            self.worker.point_data.connect(self.on_point_data)
            self.worker.clear_plot.connect(self._clear_plot)
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
            self.worker_thread.start()
        except Exception as ex:
            self.log.appendPlainText(str(ex))
            self.device_manager.release(self._required_devices())

    def stop_run(self):
        if self.worker:
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
                if self.chk_link.isChecked():
                    other = "Vbg" if fast == "Vtg" else "Vtg"
                    r = self.sp_ratio.value()
                    axes[-1].set_xlabel(f"Doping (r={r:.2f}*{fast} + {other})")
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

    def on_finished(self, _path):
        self.set_status("Finished", "done")
