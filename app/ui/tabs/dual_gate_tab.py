from __future__ import annotations

import time
from typing import List

from PyQt6 import QtCore, QtWidgets

from app.constants import GATE_BIAS_RAMP_STEP_T, GATE_BIAS_RAMP_STEP_V
from app.device_manager import DeviceManager
from app.models import Connections, DualGateParams, SaveRoot
from app.result_channels import compare_channel_options, plot_channel_options, plot_channel_value
from app.run_output import build_planned_output, planned_output_warning
from app.ui.helpers import apply_tooltip, configure_volt_spinbox, flash_button_success, set_standard_input_height, style_form_layout
from app.ui.tabs.base_tab import BaseMeasurementTab
from app.ui.widgets.collapsible_section import CollapsibleSection
from app.ui.widgets.status_panel import SectionHeader, StatusPanel
from app.utils import _safe, safe_ramp
from app.workers.dual_gate import DualGateWorker

SET_BUTTON_WIDTH = 48


class DualGateTab(BaseMeasurementTab):
    def __init__(self, save: SaveRoot, conns: Connections, device_manager: DeviceManager, get_global_rates_callable=None):
        self.save = save
        self.conns = conns
        self.device_manager = device_manager
        self.get_global_rates = get_global_rates_callable or (lambda: (1e7, 100.0))
        self.p = DualGateParams()
        self.s_g1 = self.s_g2 = self.s_g3 = self.s_daq = None
        self.worker_thread = None
        self.worker = None
        self._plot_records = []
        self._output_run_id = None
        self._planned_output = None
        super().__init__("START SWEEP", "Vds (V)", "Ids (A)", ["g1", "g2", "g3", "daq"])
        self._wire()
        self.btn_start.setToolTip("Connect instruments first")
        self.device_manager.status_changed.connect(self._on_device_status_changed)
        self.device_manager.operation_changed.connect(self._on_operation_changed)
        self._sync_sessions_from_manager()
        self._update_manual_buttons()

    def get_ao_items_if_available(self) -> List[str]:
        return self.device_manager.get_ao_items()

    def _make_set_row(self, spinbox: QtWidgets.QDoubleSpinBox, button: QtWidgets.QPushButton) -> QtWidgets.QWidget:
        wrap = QtWidgets.QWidget()
        row = QtWidgets.QHBoxLayout(wrap)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(6)
        row.addWidget(spinbox, 1)
        row.addWidget(button)
        return wrap

    def _build_control_panel(self, ctl_layout: QtWidgets.QVBoxLayout):
        ctl_layout.addWidget(SectionHeader("Sweep Parameters"))
        grp_vds = QtWidgets.QGroupBox("Vds Sweep")
        form_vds = QtWidgets.QFormLayout(grp_vds)
        style_form_layout(form_vds)
        self.sp_vds_start = QtWidgets.QDoubleSpinBox()
        self.sp_vds_stop = QtWidgets.QDoubleSpinBox()
        configure_volt_spinbox(self.sp_vds_start, 0.0)
        configure_volt_spinbox(self.sp_vds_stop, 0.5)
        self.sp_vds_step = QtWidgets.QDoubleSpinBox()
        self.sp_vds_step.setDecimals(3)
        self.sp_vds_step.setRange(1e-3, 5.0)
        self.sp_vds_step.setValue(0.01)
        self.sp_vds_ramp = QtWidgets.QDoubleSpinBox()
        self.sp_vds_ramp.setDecimals(3)
        self.sp_vds_ramp.setRange(1e-3, 5.0)
        self.sp_vds_ramp.setValue(0.05)
        lbl_vds_start = QtWidgets.QLabel("Start (V):")
        lbl_vds_stop = QtWidgets.QLabel("Stop (V):")
        lbl_vds_step = QtWidgets.QLabel("Step (V):")
        lbl_vds_ramp = QtWidgets.QLabel("Vds Step (V):")
        form_vds.addRow(lbl_vds_start, self.sp_vds_start)
        form_vds.addRow(lbl_vds_stop, self.sp_vds_stop)
        form_vds.addRow(lbl_vds_step, self.sp_vds_step)
        form_vds.addRow(lbl_vds_ramp, self.sp_vds_ramp)
        self.chk_sweep_bidirectional = QtWidgets.QCheckBox("Sweep forward and backward")
        self.chk_sweep_bidirectional.setChecked(False)
        set_standard_input_height(self.chk_sweep_bidirectional)
        apply_tooltip(
            "When checked, sweeps from start to stop then back to start. Forward trace shown in blue, backward in red.",
            self.chk_sweep_bidirectional,
        )
        form_vds.addRow("", self.chk_sweep_bidirectional)
        ctl_layout.addWidget(grp_vds)

        ctl_layout.addWidget(SectionHeader("Fixed Gate Voltages"))
        grp_gate = QtWidgets.QGroupBox("Fixed Gate Voltages")
        form_gate = QtWidgets.QFormLayout(grp_gate)
        style_form_layout(form_gate)
        self.sp_vtg = QtWidgets.QDoubleSpinBox()
        self.sp_vbg = QtWidgets.QDoubleSpinBox()
        configure_volt_spinbox(self.sp_vtg, 0.0)
        configure_volt_spinbox(self.sp_vbg, 0.0)
        self.btn_set_vtg = QtWidgets.QPushButton("Set")
        self.btn_set_vbg = QtWidgets.QPushButton("Set")
        self.btn_set_vtg.setFixedWidth(SET_BUTTON_WIDTH)
        self.btn_set_vbg.setFixedWidth(SET_BUTTON_WIDTH)
        lbl_vtg = QtWidgets.QLabel("Vtg (V):")
        lbl_vbg = QtWidgets.QLabel("Vbg (V):")
        form_gate.addRow(lbl_vtg, self._make_set_row(self.sp_vtg, self.btn_set_vtg))
        form_gate.addRow(lbl_vbg, self._make_set_row(self.sp_vbg, self.btn_set_vbg))
        ctl_layout.addWidget(grp_gate)

        ctl_layout.addWidget(SectionHeader("Acquisition"))
        grp_time = QtWidgets.QGroupBox("Timing")
        form_time = QtWidgets.QFormLayout(grp_time)
        style_form_layout(form_time)
        self.sp_delay = QtWidgets.QDoubleSpinBox()
        self.sp_delay.setDecimals(3)
        self.sp_delay.setRange(0.0, 10.0)
        self.sp_delay.setValue(0.5)
        self.sp_nsamp = QtWidgets.QSpinBox()
        self.sp_nsamp.setRange(1, 1000)
        self.sp_nsamp.setValue(3)
        lbl_delay = QtWidgets.QLabel("Delay (s):")
        lbl_nsamp = QtWidgets.QLabel("Averages:")
        form_time.addRow(lbl_delay, self.sp_delay)
        form_time.addRow(lbl_nsamp, self.sp_nsamp)
        ctl_layout.addWidget(grp_time)

        ctl_layout.addWidget(SectionHeader("Output"))
        grp_output = QtWidgets.QGroupBox("Output Settings")
        form_output = QtWidgets.QFormLayout(grp_output)
        style_form_layout(form_output)
        self.ed_base = QtWidgets.QLineEdit(self.p.base_name)
        self.cbo_source = QtWidgets.QComboBox()
        self.cbo_source.addItems(["Keithley 2400"])
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

        grp_dev = QtWidgets.QGroupBox("Developer Tools")
        lay_dev = QtWidgets.QVBoxLayout(grp_dev)
        lay_dev.setContentsMargins(8, 16, 8, 8)
        self.btn_ao_test = QtWidgets.QPushButton("AO Test (+0.1V)")
        self.btn_ao_test.setProperty("role", "dev")
        lay_dev.addWidget(self.btn_ao_test)
        self.exp_dev = CollapsibleSection("Developer Tools", grp_dev, expanded=False)
        ctl_layout.addWidget(self.exp_dev)

        ctl_layout.addWidget(SectionHeader("Status"))
        self.status_panel = StatusPanel(["g1", "g2", "g3", "daq"])
        self.lbl_g1 = self.status_panel.label("g1")
        self.lbl_g2 = self.status_panel.label("g2")
        self.lbl_g3 = self.status_panel.label("g3")
        self.lbl_daq = self.status_panel.label("daq")
        ctl_layout.addWidget(self.status_panel)

        for widget in [
            self.ed_base, self.cbo_source, self.cbo_y,
            self.sp_vds_start, self.sp_vds_stop, self.sp_vds_step, self.sp_vds_ramp,
            self.sp_vtg, self.sp_vbg, self.sp_delay, self.sp_nsamp,
            self.chk_sweep_bidirectional,
        ]:
            set_standard_input_height(widget)

        apply_tooltip("First Vds value sent during the sweep.", lbl_vds_start, self.sp_vds_start)
        apply_tooltip("Last Vds value included in the sweep.", lbl_vds_stop, self.sp_vds_stop)
        apply_tooltip("Spacing between Vds points.", lbl_vds_step, self.sp_vds_step)
        apply_tooltip(
            "Voltage step size for Vds moves between measurement points. Transition ramps to start and to 0 V always use the built-in safe rate.",
            lbl_vds_ramp,
            self.sp_vds_ramp,
        )
        apply_tooltip(
            (
                "Top-gate bias held fixed during the run. "
                f"Set ramps at {GATE_BIAS_RAMP_STEP_V:g} V/step, {GATE_BIAS_RAMP_STEP_T:g} s/step."
            ),
            lbl_vtg,
            self.sp_vtg,
            self.btn_set_vtg,
        )
        apply_tooltip(
            (
                "Back-gate bias held fixed during the run. "
                f"Set ramps at {GATE_BIAS_RAMP_STEP_V:g} V/step, {GATE_BIAS_RAMP_STEP_T:g} s/step."
            ),
            lbl_vbg,
            self.sp_vbg,
            self.btn_set_vbg,
        )
        apply_tooltip("Wait time after each Vds step before acquiring data.", lbl_delay, self.sp_delay)
        apply_tooltip("Number of DAQ reads averaged at each Vds point.", lbl_nsamp, self.sp_nsamp)
        apply_tooltip("Base filename for the CSV saved at the end of the run.", lbl_base, self.ed_base)
        apply_tooltip("Select Keithley G3 or an NI AO channel as the Vds source.", lbl_source, self.cbo_source)
        apply_tooltip("Choose which current channel is shown live in the plot.", lbl_y, self.cbo_y)
        apply_tooltip("Developer utility for briefly pulsing an NI AO output.", self.btn_ao_test)

    def _wire(self):
        self.btn_start.clicked.connect(self.start_run)
        self.btn_stop.clicked.connect(self.stop_run)
        self.btn_ao_test.clicked.connect(self.on_ao_test)
        self.btn_set_vtg.clicked.connect(self.on_set_vtg)
        self.btn_set_vbg.clicked.connect(self.on_set_vbg)
        self.cbo_source.currentIndexChanged.connect(self._update_connection_hint)
        self.cbo_source.currentIndexChanged.connect(self._update_manual_buttons)
        self.cbo_source.currentIndexChanged.connect(self._update_plot_axis_choices)
        self.cbo_y.currentTextChanged.connect(self.set_plot_axis_source)
        self._update_plot_axis_choices()
        self.plot.y_axis_changed.connect(self.set_plot_axis_source)
        self.plot.plot_mode_changed.connect(lambda _mode: self._redraw_plot())
        self.ed_base.textChanged.connect(self.refresh_output_preview)
        self.cbo_source.currentIndexChanged.connect(self.refresh_output_preview)
        for widget in (
            self.sp_vds_start,
            self.sp_vds_stop,
            self.sp_vtg,
            self.sp_vbg,
            self.chk_sweep_bidirectional,
        ):
            if hasattr(widget, "valueChanged"):
                widget.valueChanged.connect(self.refresh_output_preview)
            else:
                widget.toggled.connect(self.refresh_output_preview)
        self.refresh_output_preview()

    def _output_summary_parts(self) -> list[str]:
        direction = "forward_backward" if self.chk_sweep_bidirectional.isChecked() else "forward"
        source = "keithley_g3" if self.cbo_source.currentText() == "Keithley 2400" else self.cbo_source.currentText()
        return [
            f"Vds_{self.sp_vds_start.value():g}to{self.sp_vds_stop.value():g}V",
            f"Vtg_{self.sp_vtg.value():g}V",
            f"Vbg_{self.sp_vbg.value():g}V",
            source,
            direction,
        ]

    def refresh_output_preview(self, *_args):
        planned = build_planned_output(
            self.save,
            "vds_sweep",
            self.ed_base.text(),
            self._output_summary_parts(),
            run_id=self._output_run_id,
        )
        self._output_run_id = planned.run_id
        self._planned_output = planned
        self.set_output_preview_text(planned, planned_output_warning(planned, self.save))

    def _update_manual_buttons(self):
        self._sync_sessions_from_manager()
        self.btn_set_vtg.setEnabled(self.s_g1 is not None and self.device_manager.is_voltage_source_mode("g1"))
        self.btn_set_vbg.setEnabled(self.s_g2 is not None and self.device_manager.is_voltage_source_mode("g2"))
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
        items = ["Keithley 2400"] + [f"NI DAQ {a}" for a in self.get_ao_items_if_available()]
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

    def _required_devices(self) -> List[str]:
        required = ["daq"]
        if abs(self.sp_vtg.value()) > 1e-12:
            required.append("g1")
        if abs(self.sp_vbg.value()) > 1e-12:
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
        if self.cbo_source.currentText() == "Keithley 2400" and not self.device_manager.is_voltage_source_mode("g3"):
            QtWidgets.QMessageBox.warning(self, "Keithley Mode", "G3 must be in 2-wire voltage source mode for a Keithley-driven Vds sweep.")
            return False
        for gate, label in (("g1", "G1 / Vtg"), ("g2", "G2 / Vbg")):
            if gate in self._required_devices() and not self.device_manager.is_voltage_source_mode(gate):
                QtWidgets.QMessageBox.warning(self, "Gate Mode", f"{label} must be in 2-wire voltage source mode because its fixed bias is nonzero.")
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
                text += f" Optional manual controls unavailable: {', '.join(missing_optional)}."
            self.lbl_connection_hint.setProperty("role", "warning-hint")
            self.btn_start.setToolTip(f"Connect required devices from Instrument Setup: {', '.join(missing_required)}")
        else:
            text = "Ready to run with dock-managed sessions."
            if missing_optional:
                text += f" Manual gate controls unavailable until {', '.join(missing_optional)} connects."
            self.lbl_connection_hint.setProperty("role", "hint")
            self.btn_start.setToolTip("Start Vds sweep")
        self.lbl_connection_hint.setText(text)
        self.lbl_connection_hint.style().unpolish(self.lbl_connection_hint)
        self.lbl_connection_hint.style().polish(self.lbl_connection_hint)

    def on_set_vtg(self):
        if self.s_g1:
            try:
                val = self.sp_vtg.value()
                self.log.appendPlainText(
                    f"[Manual] Ramping G1 / Vtg to {val} V ({GATE_BIAS_RAMP_STEP_V:g} V/step)"
                )
                safe_ramp(
                    self.s_g1.set_voltage,
                    getattr(self.s_g1, "voltage", None) or 0.0,
                    val,
                    GATE_BIAS_RAMP_STEP_V,
                    GATE_BIAS_RAMP_STEP_T,
                )
                self.log.appendPlainText(f"[Manual] G1 / Vtg set to {val} V ({GATE_BIAS_RAMP_STEP_V:g} V/step)")
                flash_button_success(self.btn_set_vtg)
            except Exception as ex:
                self.log.appendPlainText(f"Error setting G1: {ex}")
        else:
            self.log.appendPlainText("G1 / Vtg not connected.")

    def on_set_vbg(self):
        if self.s_g2:
            try:
                val = self.sp_vbg.value()
                self.log.appendPlainText(
                    f"[Manual] Ramping G2 / Vbg to {val} V ({GATE_BIAS_RAMP_STEP_V:g} V/step)"
                )
                safe_ramp(
                    self.s_g2.set_voltage,
                    getattr(self.s_g2, "voltage", None) or 0.0,
                    val,
                    GATE_BIAS_RAMP_STEP_V,
                    GATE_BIAS_RAMP_STEP_T,
                )
                self.log.appendPlainText(f"[Manual] G2 / Vbg set to {val} V ({GATE_BIAS_RAMP_STEP_V:g} V/step)")
                flash_button_success(self.btn_set_vbg)
            except Exception as ex:
                self.log.appendPlainText(f"Error setting G2: {ex}")
        else:
            self.log.appendPlainText("G2 / Vbg not connected.")

    def collect_params(self):
        self.refresh_output_preview()
        self.p.base_name = self.ed_base.text()
        self.p.output_csv_path = self._planned_output.csv_path if self._planned_output else ""
        self.p.output_metadata_path = self._planned_output.metadata_path if self._planned_output else ""
        self.p.output_log_path = self._planned_output.log_path if self._planned_output else ""
        src_text = self.cbo_source.currentText()
        if src_text.startswith("NI DAQ "):
            self.p.vds_source = "NI DAQ AO"
            try:
                self.p.ao_channel = int(src_text.split()[-1].replace("ao", ""))
            except Exception:
                self.p.ao_channel = 0
        else:
            self.p.vds_source = "Keithley 2400"
        self.p.vds_start = float(self.sp_vds_start.value())
        self.p.vds_stop = float(self.sp_vds_stop.value())
        self.p.vds_step = float(self.sp_vds_step.value())
        self.p.vds_ramp = float(self.sp_vds_ramp.value())
        self.p.vtg_set = float(self.sp_vtg.value())
        self.p.vbg_set = float(self.sp_vbg.value())
        self.p.delay = float(self.sp_delay.value())
        self.p.n_sample = int(self.sp_nsamp.value())
        self.p.plot_choice = self.cbo_y.currentText()
        self.p.sweep_both_ways = self.chk_sweep_bidirectional.isChecked()

    def start_run(self):
        if self.worker_thread:
            QtWidgets.QMessageBox.warning(self, "Busy", "Run already in progress")
            return
        mw = self.window()
        if hasattr(mw, "refresh_models_from_ui"):
            mw.refresh_models_from_ui()
        self.refresh_output_preview()
        if not self.validate_output_ready(self.save):
            return
        if not self._validate_required_sessions():
            return
        claimed, blocked = self.device_manager.mark_in_use(self._required_devices())
        if not claimed:
            QtWidgets.QMessageBox.warning(self, "Busy", f"Devices already in use: {', '.join(blocked).upper()}")
            return
        self.collect_params()
        self._plot_records = []
        self.plot.clear()
        self.plot.ax.set_xlabel("Vds (V)")
        self.set_plot_axis_source(self.p.plot_choice)
        try:
            self.begin_run_logging(self._planned_output, "Vds Sweep")
            amp_rate, lkn_rate = self.get_global_rates()
            self.worker = DualGateWorker(self.p, self.save, self.conns, g1=self.s_g1, g2=self.s_g2, g3=self.s_g3, daq=self.s_daq, plot_choice=self.p.plot_choice, amp_rate=amp_rate, lkn_rate=lkn_rate)
            self.worker_thread = QtCore.QThread()
            self.worker.moveToThread(self.worker_thread)
            self.worker_thread.started.connect(self.worker.run)
            self.worker.point_data.connect(self.on_point_data)
            self.worker.progress.connect(self.set_progress)
            self.worker.status.connect(lambda m: self.set_status(m, "running"))
            self.worker.log.connect(self.append_log)
            self.worker.finished.connect(self.on_finished)
            self.worker.stopped.connect(self.on_stopped)
            self.worker.error.connect(self.on_error)
            self.worker.finished.connect(self.worker_thread.quit)
            self.worker.stopped.connect(self.worker_thread.quit)
            self.worker.error.connect(self.worker_thread.quit)
            self.worker_thread.finished.connect(self._cleanup_thread)
            self.run_panel.set_running(True)
            self.set_status("Running...", "running")
            self.progress.setValue(0)
            self.worker_thread.start()
            self.append_log("[start] Worker thread started.")
        except Exception as ex:
            self.append_log(f"[start] ERROR while creating/starting worker: {ex}")
            self.end_run_logging("error", str(ex))
            self.device_manager.release(self._required_devices())

    def stop_run(self):
        if self.worker:
            self.set_status("Stopping safely...", "running", "Stop requested. Waiting for the worker to reach a safe checkpoint and ramp outputs to 0 V.")
            self.append_log("Stop requested by user.")
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
        fwd = [r for r in self._plot_records if r.get("direction", "forward") == "forward"]
        bwd = [r for r in self._plot_records if r.get("direction") == "backward"]
        if self.plot.current_plot_mode() == "4-Channel Compare":
            axes = self.plot.get_axes()
            channels = self.plot.compare_channels()
            for axis, channel in zip(axes, channels):
                axis.clear()
                if fwd:
                    axis.plot([r["x"] for r in fwd], [plot_channel_value(r, channel) for r in fwd], "b-o", markersize=3, label="Forward")
                if bwd:
                    axis.plot([r["x"] for r in bwd], [plot_channel_value(r, channel) for r in bwd], "r-o", markersize=3, label="Backward")
                    axis.legend(loc="best", fontsize=7)
                axis.relim()
                axis.autoscale_view()
                axis.set_ylabel(f"{channel} (A)")
                axis.grid(True)
            if axes:
                axes[-1].set_xlabel("Vds (V)")
        else:
            source = self.cbo_y.currentText()
            ax = self.plot.ax
            ax.clear()
            if fwd:
                ax.plot([r["x"] for r in fwd], [plot_channel_value(r, source) for r in fwd], "b-o", markersize=3, label="Forward")
            if bwd:
                ax.plot([r["x"] for r in bwd], [plot_channel_value(r, source) for r in bwd], "r-o", markersize=3, label="Backward")
                ax.legend(loc="best", fontsize=7)
            ax.relim()
            ax.autoscale_view()
            ax.set_xlabel("Vds (V)")
            ax.set_ylabel(f"{source} (A)")
            ax.grid(True)
        self.plot.canvas.draw_idle()

    def on_error(self, msg):
        self.set_status("Run error", "error", msg)
        self.append_log("ERROR: " + msg)
        self.end_run_logging("error", msg)
        self._output_run_id = None
        self.refresh_output_preview()

    def on_finished(self, csv_path: str):
        self.set_status("Finished", "done", csv_path)
        self.append_log(f"Saved: {csv_path}")
        self.end_run_logging("finished", csv_path)
        self._output_run_id = None
        self.refresh_output_preview()

    def on_stopped(self, message: str):
        self.set_status("Stopped by user", "done", message)
        self.append_log(message)
        self.end_run_logging("stopped", message)
        self._output_run_id = None
        self.refresh_output_preview()

    def on_ao_test(self):
        mw = self.window()
        if hasattr(mw, "refresh_models_from_ui"):
            mw.refresh_models_from_ui()
        if not self.device_manager.is_connected("daq"):
            QtWidgets.QMessageBox.warning(self, "AO Test", "Connect DAQ first from Instrument Setup.")
            return
        try:
            items = self.get_ao_items_if_available()
            idx = int(items[0][2:]) if items else 0
            _safe(self.s_daq, "ramp_voltage", idx, 0.1, 0.01)
            time.sleep(0.1)
            _safe(self.s_daq, "ramp_voltage", idx, 0.0, 0.01)
            QtWidgets.QMessageBox.information(self, "AO Test", f"Pulsed +0.1 V on {items[0] if items else 'ao0'}")
        except Exception as ex:
            QtWidgets.QMessageBox.critical(self, "AO Test", str(ex))
