from __future__ import annotations

from PyQt6 import QtCore, QtWidgets

from app.constants import GATE_BIAS_RAMP_STEP_T, GATE_BIAS_RAMP_STEP_V
from app.device_manager import DeviceManager
from app.models import Connections, PhotocurrentParams, SaveRoot
from app.result_channels import compare_channel_options, plot_channel_options, plot_channel_value
from app.ui.helpers import apply_tooltip, configure_volt_spinbox, flash_button_success, set_standard_input_height, style_form_layout
from app.ui.tabs.base_tab import BaseMeasurementTab
from app.ui.widgets.collapsible_section import CollapsibleSection
from app.ui.widgets.status_panel import SectionHeader, StatusPanel
from app.utils import safe_ramp
from app.workers.photocurrent import PhotocurrentWorker

SET_BUTTON_WIDTH = 48


class PhotocurrentTab(BaseMeasurementTab):
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
        super().__init__("START PHOTOCURRENT", "Wavelength (nm)", "Ids (A)", ["g1", "g2", "g3", "daq", "mono"])
        self._wire()
        self.btn_start.setToolTip("Connect instruments first")
        self.device_manager.status_changed.connect(self._on_device_status_changed)
        self.device_manager.operation_changed.connect(self._on_operation_changed)
        self._sync_sessions_from_manager()
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
        self.sp_wls = QtWidgets.QDoubleSpinBox()
        self.sp_wls.setRange(200, 2000)
        self.sp_wls.setValue(550)
        self.sp_wle = QtWidgets.QDoubleSpinBox()
        self.sp_wle.setRange(200, 2000)
        self.sp_wle.setValue(740)
        self.sp_wld = QtWidgets.QDoubleSpinBox()
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
        self.chk_use_vds = QtWidgets.QCheckBox("Enable Vds Bias")
        self.sp_vds = QtWidgets.QDoubleSpinBox()
        configure_volt_spinbox(self.sp_vds, 0.0)
        self.btn_set_vds = QtWidgets.QPushButton("Set")
        self.btn_set_vds.setFixedWidth(SET_BUTTON_WIDTH)
        self.sp_vds_ramp = QtWidgets.QDoubleSpinBox()
        self.sp_vds_ramp.setDecimals(3)
        self.sp_vds_ramp.setValue(0.01)
        form_vds.addRow(self.chk_use_vds)
        lbl_vds = QtWidgets.QLabel("Set (V):")
        lbl_vds_ramp = QtWidgets.QLabel("Vds Step (V):")
        form_vds.addRow(lbl_vds, self._make_set_row(self.sp_vds, self.btn_set_vds))
        form_vds.addRow(lbl_vds_ramp, self.sp_vds_ramp)
        self.exp_vds = CollapsibleSection("Optional Vds Bias", grp_vds, expanded=False)
        ctl_layout.addWidget(self.exp_vds)

        grp_gate = QtWidgets.QGroupBox("Fixed Gates")
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
        self.sp_delay.setValue(0.01)
        self.sp_nsamp = QtWidgets.QSpinBox()
        self.sp_nsamp.setValue(1)
        lbl_delay = QtWidgets.QLabel("Delay (s):")
        lbl_avg = QtWidgets.QLabel("Averages:")
        form_time.addRow(lbl_delay, self.sp_delay)
        form_time.addRow(lbl_avg, self.sp_nsamp)
        ctl_layout.addWidget(grp_time)

        ctl_layout.addWidget(SectionHeader("Advanced"))
        grp_output = QtWidgets.QGroupBox("Output Settings")
        form_output = QtWidgets.QFormLayout(grp_output)
        style_form_layout(form_output)
        self.ed_base = QtWidgets.QLineEdit(self.p.base_name)
        self.cbo_source = QtWidgets.QComboBox()
        self.cbo_source.addItems(["None", "Keithley 2400"])
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
            self.sp_vds, self.sp_vds_ramp, self.sp_vtg, self.sp_vbg,
            self.sp_delay, self.sp_nsamp,
        ]:
            set_standard_input_height(widget)

        apply_tooltip("First wavelength in the scan.", lbl_wls, self.sp_wls)
        apply_tooltip("Last wavelength included in the scan.", lbl_wle, self.sp_wle)
        apply_tooltip("Wavelength spacing between points. The Go button moves the mono immediately.", lbl_wld, self.sp_wld, self.btn_go_wl)
        apply_tooltip("Enable and hold a Vds bias during the wavelength scan.", self.chk_use_vds)
        apply_tooltip("Manual Vds setpoint used when Vds bias is enabled.", lbl_vds, self.sp_vds, self.btn_set_vds)
        apply_tooltip("Voltage step size used when changing the Vds bias.", lbl_vds_ramp, self.sp_vds_ramp)
        apply_tooltip(
            (
                "Fixed top-gate bias during the scan. "
                f"Set ramps at {GATE_BIAS_RAMP_STEP_V:g} V/step, {GATE_BIAS_RAMP_STEP_T:g} s/step."
            ),
            lbl_vtg,
            self.sp_vtg,
            self.btn_set_vtg,
        )
        apply_tooltip(
            (
                "Fixed back-gate bias during the scan. "
                f"Set ramps at {GATE_BIAS_RAMP_STEP_V:g} V/step, {GATE_BIAS_RAMP_STEP_T:g} s/step."
            ),
            lbl_vbg,
            self.sp_vbg,
            self.btn_set_vbg,
        )
        apply_tooltip("Wait time after each wavelength move before measuring.", lbl_delay, self.sp_delay)
        apply_tooltip("Number of DAQ reads averaged per wavelength point.", lbl_avg, self.sp_nsamp)
        apply_tooltip("Base filename for the saved spectrum.", lbl_base, self.ed_base)
        apply_tooltip("Select the instrument used when Vds bias is enabled.", lbl_source, self.cbo_source)
        apply_tooltip("Choose which current channel is shown in the live plot.", lbl_y, self.cbo_y)

    def _wire(self):
        self.btn_start.clicked.connect(self.start_run)
        self.btn_stop.clicked.connect(self.stop_run)
        self.btn_set_vtg.clicked.connect(self.on_set_vtg)
        self.btn_set_vbg.clicked.connect(self.on_set_vbg)
        self.btn_set_vds.clicked.connect(self.on_set_vds)
        self.btn_go_wl.clicked.connect(self.on_go_wl)
        self.chk_use_vds.toggled.connect(self._update_connection_hint)
        self.chk_use_vds.toggled.connect(self._update_manual_buttons)
        self.chk_use_vds.toggled.connect(self._update_plot_axis_choices)
        self.chk_use_vds.toggled.connect(self._update_vds_bias_state)
        self.cbo_source.currentIndexChanged.connect(self._update_connection_hint)
        self.cbo_source.currentIndexChanged.connect(self._update_manual_buttons)
        self.cbo_source.currentIndexChanged.connect(self._update_plot_axis_choices)
        self.cbo_y.currentTextChanged.connect(self.set_plot_axis_source)
        self._update_plot_axis_choices()
        self.plot.y_axis_changed.connect(self.set_plot_axis_source)
        self.plot.plot_mode_changed.connect(lambda _mode: self._redraw_plot())
        self._update_vds_bias_state()

    def _update_manual_buttons(self):
        self._sync_sessions_from_manager()
        self.btn_set_vtg.setEnabled(self.s_g1 is not None and self.device_manager.is_voltage_source_mode("g1"))
        self.btn_set_vbg.setEnabled(self.s_g2 is not None and self.device_manager.is_voltage_source_mode("g2"))
        self.btn_set_vds.setEnabled(
            self.chk_use_vds.isChecked()
            and (
                ("NI DAQ" in self.cbo_source.currentText() and self.s_daq is not None)
                or (self.s_g3 is not None and self.device_manager.is_voltage_source_mode("g3"))
            )
        )
        self.btn_go_wl.setEnabled(self.s_mono is not None)
        source_ready = (
            not self.chk_use_vds.isChecked()
            or self.cbo_source.currentText() != "Keithley 2400"
            or (self.s_g3 is not None and self.device_manager.is_voltage_source_mode("g3"))
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
        items = ["None", "Keithley 2400"] + [f"NI DAQ {a}" for a in self.get_ao_items()]
        cur = self.cbo_source.currentText()
        self.cbo_source.blockSignals(True)
        self.cbo_source.clear()
        self.cbo_source.addItems(items)
        if cur in items:
            self.cbo_source.setCurrentText(cur)
        self.cbo_source.blockSignals(False)
        self._update_plot_axis_choices()

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
        self._update_connection_hint()

    def _required_devices(self) -> list[str]:
        required = ["daq", "mono"]
        if self.chk_use_vds.isChecked() and self.cbo_source.currentText() == "Keithley 2400":
            required.append("g3")
        return required

    def _validate_required_sessions(self) -> bool:
        self._sync_sessions_from_manager()
        missing = [name for name in self._required_devices() if not self.device_manager.is_connected(name)]
        if missing:
            QtWidgets.QMessageBox.warning(self, "Missing Device", f"Connect required devices first: {', '.join(missing).upper()}")
            return False
        if self.chk_use_vds.isChecked() and self.cbo_source.currentText() == "Keithley 2400" and not self.device_manager.is_voltage_source_mode("g3"):
            QtWidgets.QMessageBox.warning(self, "Keithley Mode", "G3 must be in 2-wire voltage source mode when photocurrent uses Keithley Vds bias.")
            return False
        return True

    def _update_connection_hint(self):
        required = self._required_devices()
        optional = ["g1", "g2"]
        if not self.chk_use_vds.isChecked():
            optional.append("g3")
        missing_required = [name.upper() for name in required if not self.device_manager.is_connected(name)]
        missing_optional = [name.upper() for name in optional if not self.device_manager.is_connected(name)]
        if self.device_manager.is_connected("g1") and not self.device_manager.is_voltage_source_mode("g1"):
            missing_optional.append("G1 mode")
        if self.device_manager.is_connected("g2") and not self.device_manager.is_voltage_source_mode("g2"):
            missing_optional.append("G2 mode")
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
        enabled = self.chk_use_vds.isChecked()
        self.sp_vds.setEnabled(enabled)
        self.sp_vds_ramp.setEnabled(enabled)
        self.cbo_source.setEnabled(enabled)
        self.btn_set_vds.setEnabled(
            enabled
            and (
                ("NI DAQ" in self.cbo_source.currentText() and self.s_daq is not None)
                or (self.s_g3 is not None and self.device_manager.is_voltage_source_mode("g3"))
            )
        )

    def on_set_vtg(self):
        if self.s_g1:
            try:
                val = self.sp_vtg.value()
                self.log.appendPlainText(
                    f"[Manual] Ramping Gate1/Vtg to {val} V ({GATE_BIAS_RAMP_STEP_V:g} V/step)"
                )
                safe_ramp(
                    self.s_g1.set_voltage,
                    getattr(self.s_g1, "voltage", None) or 0.0,
                    val,
                    GATE_BIAS_RAMP_STEP_V,
                    GATE_BIAS_RAMP_STEP_T,
                )
                self.log.appendPlainText(f"[Manual] Gate1 set to {val} V ({GATE_BIAS_RAMP_STEP_V:g} V/step)")
                flash_button_success(self.btn_set_vtg)
            except Exception as ex:
                self.log.appendPlainText(f"Error setting G1: {ex}")

    def on_set_vbg(self):
        if self.s_g2:
            try:
                val = self.sp_vbg.value()
                self.log.appendPlainText(
                    f"[Manual] Ramping Gate2/Vbg to {val} V ({GATE_BIAS_RAMP_STEP_V:g} V/step)"
                )
                safe_ramp(
                    self.s_g2.set_voltage,
                    getattr(self.s_g2, "voltage", None) or 0.0,
                    val,
                    GATE_BIAS_RAMP_STEP_V,
                    GATE_BIAS_RAMP_STEP_T,
                )
                self.log.appendPlainText(f"[Manual] Gate2 set to {val} V ({GATE_BIAS_RAMP_STEP_V:g} V/step)")
                flash_button_success(self.btn_set_vbg)
            except Exception as ex:
                self.log.appendPlainText(f"Error setting G2: {ex}")

    def on_set_vds(self):
        src = self.cbo_source.currentText()
        val = self.sp_vds.value()
        if src == "Keithley 2400" and self.s_g3:
            self.s_g3.ramp_voltage(val, 0.5)
            flash_button_success(self.btn_set_vds)
        elif "NI DAQ" in src and self.s_daq:
            self.s_daq.ramp_voltage(int(src.split()[-1].replace("ao", "")), val, 0.5)
            flash_button_success(self.btn_set_vds)

    def on_go_wl(self):
        if self.s_mono:
            self.s_mono.set_wavelength(self.sp_wls.value())
            flash_button_success(self.btn_go_wl)

    def collect_params(self):
        self.p.base_name = self.ed_base.text()
        self.p.use_vds = self.chk_use_vds.isChecked()
        src = self.cbo_source.currentText()
        if "NI DAQ" in src:
            self.p.vds_source = "NI DAQ AO"
            self.p.ao_channel = int(src.split()[-1].replace("ao", ""))
        else:
            self.p.vds_source = src
        self.p.vds_set = self.sp_vds.value()
        self.p.vtg_set = self.sp_vtg.value()
        self.p.vbg_set = self.sp_vbg.value()
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
        if not self._validate_required_sessions():
            return
        claimed, blocked = self.device_manager.mark_in_use(self._required_devices())
        if not claimed:
            QtWidgets.QMessageBox.warning(self, "Busy", f"Devices already in use: {', '.join(blocked).upper()}")
            return
        self.collect_params()
        self._plot_records = []
        self.plot.clear()
        self.plot.ax.set_xlabel("Wavelength (nm)")
        self.set_plot_axis_source(self.p.plot_choice)
        try:
            amp, lkn = self.get_global_rates()
            self.worker = PhotocurrentWorker(self.p, self.save, self.conns, g1=self.s_g1, g2=self.s_g2, g3=self.s_g3, daq=self.s_daq, mono=self.s_mono, plot_choice=self.p.plot_choice, amp_rate=amp, lkn_rate=lkn)
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
        xs = [record["x"] for record in self._plot_records]
        if self.plot.current_plot_mode() == "4-Channel Compare":
            axes = self.plot.get_axes()
            channels = self.plot.compare_channels()
            for axis, channel in zip(axes, channels):
                axis.clear()
                ys = [plot_channel_value(record, channel) for record in self._plot_records]
                if xs:
                    axis.plot(xs, ys, marker="o")
                    axis.relim()
                    axis.autoscale_view()
                axis.set_ylabel(f"{channel} (A)")
                axis.grid(True)
            if axes:
                axes[-1].set_xlabel("Wavelength (nm)")
        else:
            source = self.cbo_y.currentText()
            ax = self.plot.ax
            ax.clear()
            ys = [plot_channel_value(record, source) for record in self._plot_records]
            if xs:
                ax.plot(xs, ys, marker="o")
                ax.relim()
                ax.autoscale_view()
            ax.set_xlabel("Wavelength (nm)")
            ax.set_ylabel(f"{source} (A)")
            ax.grid(True)
        self.plot.canvas.draw_idle()

    def on_finished(self, path):
        self.set_status("Finished", "done")
        self.log.appendPlainText(f"Saved: {path}")

    def on_error(self, msg):
        self.set_status("Run error", "error", msg)
