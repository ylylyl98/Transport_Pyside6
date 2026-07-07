from __future__ import annotations

from typing import Tuple

from PyQt6 import QtCore, QtGui, QtWidgets
from PyQt6.QtCore import Qt

from app.constants import SETTINGS_APP, SETTINGS_ORG
from app.device_manager import DeviceManager
from app.hw_discovery import scan_all
from app.keithley_modes import KEITHLEY_MODE_LABELS, keithley_mode_label, keithley_mode_options
from app.models import Connections, SaveRoot
from app.settings import get_app_settings
from app.ui.helpers import apply_tooltip, configure_volt_spinbox, flash_button_success, set_standard_input_height, style_form_layout
from app.ui.widgets.collapsible_section import CollapsibleSection
from app.ui.widgets.status_panel import StatusPanel
from app.ui.widgets.resource_combo import ResourceComboBox
from instruments.SR830 import SENSITIVITY_LABELS, sensitivity_value


class ScanWorker(QtCore.QThread):
    results_ready = QtCore.pyqtSignal(dict)
    scan_failed = QtCore.pyqtSignal(str)

    def run(self):
        try:
            self.results_ready.emit(scan_all())
        except Exception as ex:
            self.scan_failed.emit(str(ex))


class ScientificDoubleSpinBox(QtWidgets.QDoubleSpinBox):
    def validate(self, text: str, pos: int):
        if text.strip() in {"", "-", "+", ".", "-.", "+."}:
            return (QtGui.QValidator.State.Intermediate, text, pos)
        try:
            float(text)
            return (QtGui.QValidator.State.Acceptable, text, pos)
        except ValueError:
            return (QtGui.QValidator.State.Invalid, text, pos)

    def valueFromText(self, text: str) -> float:
        try:
            return float(text)
        except ValueError:
            return self.minimum()

    def textFromValue(self, value: float) -> str:
        return f"{value:.0e}"

    def stepBy(self, steps: int):
        current = max(self.value(), self.minimum())
        exponent = int(f"{current:.0e}".split("e")[1])
        next_value = current + steps * (10 ** exponent)
        self.setValue(min(max(next_value, self.minimum()), self.maximum()))


class ConnDock(QtWidgets.QWidget):
    stop_requested = QtCore.pyqtSignal()

    AMP_MIN_A = 1e-12
    LIA_MIN_V = 1e-6

    def __init__(self, device_manager: DeviceManager | None = None):
        super().__init__()
        self.conns = Connections()
        self.save_root = SaveRoot()
        self.device_manager = device_manager
        self._scan_thread = None
        self._lockin_sensitivity_from_sr830 = False
        self._build()
        QtCore.QTimer.singleShot(0, self._start_scan)
        self._bind_device_manager()

    def _build(self):
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(8)

        grp_hw = QtWidgets.QGroupBox("Hardware Addresses")
        form_hw = QtWidgets.QFormLayout(grp_hw)
        style_form_layout(form_hw)
        self.btn_scan = QtWidgets.QPushButton("Scan Hardware")
        self.btn_scan.clicked.connect(self._start_scan)
        self.lbl_scan_status = QtWidgets.QLabel("Click Scan to detect connected instruments.")
        self.lbl_scan_status.setWordWrap(True)
        scan_row = QtWidgets.QHBoxLayout()
        scan_row.setContentsMargins(0, 0, 0, 0)
        scan_row.addWidget(self.btn_scan)
        scan_wrap = QtWidgets.QWidget()
        scan_wrap.setLayout(scan_row)

        self.cbo_g1 = ResourceComboBox()
        self.cbo_g2 = ResourceComboBox()
        self.cbo_g3 = ResourceComboBox()
        self.cbo_g1_mode = QtWidgets.QComboBox()
        self.cbo_g2_mode = QtWidgets.QComboBox()
        self.cbo_g3_mode = QtWidgets.QComboBox()
        self.cbo_daq = ResourceComboBox()
        self.cbo_mono = ResourceComboBox()
        self.cbo_lockin = ResourceComboBox()
        self.ed_g1 = self.cbo_g1
        self.ed_g2 = self.cbo_g2
        self.ed_g3 = self.cbo_g3
        self.ed_daq = self.cbo_daq
        self.ed_mono = self.cbo_mono
        self.ed_lockin = self.cbo_lockin
        self.cbo_g1.setCurrentText(self.conns.gate1)
        self.cbo_g2.setCurrentText(self.conns.gate2)
        self.cbo_g3.setCurrentText(self.conns.gate3)
        for value, label in keithley_mode_options():
            self.cbo_g1_mode.addItem(label, value)
            self.cbo_g2_mode.addItem(label, value)
            self.cbo_g3_mode.addItem(label, value)
        self._set_combo_data(self.cbo_g1_mode, self.conns.gate1_mode)
        self._set_combo_data(self.cbo_g2_mode, self.conns.gate2_mode)
        self._set_combo_data(self.cbo_g3_mode, self.conns.gate3_mode)
        self.cbo_daq.setCurrentText(self.conns.daq_dev)
        self.cbo_mono.setCurrentText(self.conns.mono)
        self.cbo_lockin.setCurrentText(self.conns.lockin)
        for widget in (self.cbo_g1, self.cbo_g2, self.cbo_g3, self.cbo_daq, self.cbo_mono, self.cbo_lockin, self.cbo_g1_mode, self.cbo_g2_mode, self.cbo_g3_mode):
            widget.currentTextChanged.connect(self._update_reconnect_indicators)

        lbl_g1 = QtWidgets.QLabel("G1 / Vtg:")
        lbl_g2 = QtWidgets.QLabel("G2 / Vbg:")
        lbl_g3 = QtWidgets.QLabel("G3 / Vds:")
        lbl_daq = QtWidgets.QLabel("DAQ:")
        lbl_mono = QtWidgets.QLabel("Mono:")
        lbl_lockin = QtWidgets.QLabel("Lock-in:")
        form_hw.addRow("", scan_wrap)
        form_hw.addRow(lbl_g1, self._make_address_mode_row(self.cbo_g1, self.cbo_g1_mode))
        form_hw.addRow(lbl_g2, self._make_address_mode_row(self.cbo_g2, self.cbo_g2_mode))
        form_hw.addRow(lbl_g3, self._make_address_mode_row(self.cbo_g3, self.cbo_g3_mode))
        form_hw.addRow(lbl_daq, self.cbo_daq)
        form_hw.addRow(lbl_mono, self.cbo_mono)
        form_hw.addRow(lbl_lockin, self.cbo_lockin)
        self.lbl_reconnect_hint = QtWidgets.QLabel("")
        self.lbl_reconnect_hint.setWordWrap(True)
        self.lbl_reconnect_hint.setProperty("role", "warning-hint")
        form_hw.addRow("", self.lbl_reconnect_hint)
        form_hw.addRow("", self.lbl_scan_status)
        self.exp_hw = CollapsibleSection("Hardware Addresses", grp_hw, expanded=False)
        layout.addWidget(self.exp_hw)

        grp_save = QtWidgets.QGroupBox("Save Settings")
        form_save = QtWidgets.QFormLayout(grp_save)
        style_form_layout(form_save)
        self.ed_user = QtWidgets.QLineEdit(self.save_root.user)
        self.ed_device_id = QtWidgets.QLineEdit(self.save_root.device_id)
        self.ed_base = QtWidgets.QLineEdit(self.save_root.base)
        lbl_user = QtWidgets.QLabel("Operator:")
        lbl_device_id = QtWidgets.QLabel("Device ID:")
        lbl_base = QtWidgets.QLabel("Data Root:")
        form_save.addRow(lbl_user, self.ed_user)
        form_save.addRow(lbl_device_id, self.ed_device_id)
        form_save.addRow(lbl_base, self.ed_base)
        self.exp_save = CollapsibleSection("Save Settings", grp_save, expanded=True)
        layout.addWidget(self.exp_save)

        grp_rate = QtWidgets.QGroupBox("Signal Chain")
        form_rate = QtWidgets.QFormLayout(grp_rate)
        style_form_layout(form_rate)
        self.sp_amp = ScientificDoubleSpinBox()
        self.sp_amp.setDecimals(12)
        self.sp_amp.setRange(self.AMP_MIN_A, 1.0)
        self.sp_amp.setValue(1e-7)
        self.sp_lkn = QtWidgets.QDoubleSpinBox()
        self.sp_lkn.setDecimals(6)
        self.sp_lkn.setRange(self.LIA_MIN_V, 10.0)
        self.sp_lkn.setSingleStep(0.001)
        self.sp_lkn.setValue(0.1)
        self.sp_amp.valueChanged.connect(self._save_signal_chain_settings)
        self.sp_lkn.valueChanged.connect(self._on_manual_lockin_sensitivity_changed)
        lbl_amp = QtWidgets.QLabel("Pre-amp (A):")
        lbl_lkn = QtWidgets.QLabel("Lock-in (V):")
        self.lbl_lkn_source = QtWidgets.QLabel("Manual value")
        self.lbl_lkn_source.setWordWrap(True)
        self.lbl_lkn_source.setProperty("role", "hint")
        form_rate.addRow(lbl_amp, self.sp_amp)
        form_rate.addRow(lbl_lkn, self.sp_lkn)
        form_rate.addRow("", self.lbl_lkn_source)
        layout.addWidget(grp_rate)

        grp_conn = QtWidgets.QGroupBox("Connections")
        lay_conn = QtWidgets.QVBoxLayout(grp_conn)
        lay_conn.setContentsMargins(10, 18, 10, 10)
        lay_conn.setSpacing(8)
        row_conn = QtWidgets.QVBoxLayout()
        row_conn.setSpacing(6)
        self.btn_connect_all = QtWidgets.QPushButton("Connect All")
        self.btn_disconnect_all = QtWidgets.QPushButton("Disconnect All")
        row_conn.addWidget(self.btn_connect_all)
        row_conn.addWidget(self.btn_disconnect_all)
        lay_conn.addLayout(row_conn)
        status_row = QtWidgets.QHBoxLayout()
        status_row.setContentsMargins(0, 0, 0, 0)
        status_row.setSpacing(6)
        self.lbl_connection_status = QtWidgets.QLabel("Ready")
        self.lbl_connection_status.setProperty("role", "hint")
        self.btn_connection_details = QtWidgets.QToolButton()
        self.btn_connection_details.setText("Details")
        self.btn_connection_details.setAutoRaise(True)
        self.btn_connection_details.setProperty("role", "status-detail")
        self.btn_connection_details.clicked.connect(self._show_connection_details)
        self.btn_connection_details.hide()
        status_row.addWidget(self.lbl_connection_status, 1)
        status_row.addWidget(self.btn_connection_details)
        self._connection_detail = "Connect hardware from here. Tabs will reuse the same sessions."
        self.dock_status_panel = StatusPanel(["g1", "g2", "g3", "daq", "mono", "lockin"], columns=1)
        lay_conn.addLayout(status_row)
        lay_conn.addWidget(self.dock_status_panel)
        layout.addWidget(grp_conn)

        grp_manual = QtWidgets.QGroupBox("Manual Controls")
        form_manual = QtWidgets.QFormLayout(grp_manual)
        style_form_layout(form_manual)
        self.sp_manual_g1 = QtWidgets.QDoubleSpinBox()
        self.sp_manual_g2 = QtWidgets.QDoubleSpinBox()
        self.sp_manual_g3 = QtWidgets.QDoubleSpinBox()
        for spinbox in (self.sp_manual_g1, self.sp_manual_g2, self.sp_manual_g3):
            configure_volt_spinbox(spinbox, 0.0)
        self.btn_manual_g1_set = QtWidgets.QPushButton("Ramp")
        self.btn_manual_g2_set = QtWidgets.QPushButton("Ramp")
        self.btn_manual_g3_set = QtWidgets.QPushButton("Ramp")
        self.btn_manual_g1_zero = QtWidgets.QPushButton("Zero")
        self.btn_manual_g2_zero = QtWidgets.QPushButton("Zero")
        self.btn_manual_g3_zero = QtWidgets.QPushButton("Zero")
        self._manual_gate_controls = {
            "g1": (self.sp_manual_g1, self.btn_manual_g1_set, self.btn_manual_g1_zero),
            "g2": (self.sp_manual_g2, self.btn_manual_g2_set, self.btn_manual_g2_zero),
            "g3": (self.sp_manual_g3, self.btn_manual_g3_set, self.btn_manual_g3_zero),
        }
        for spinbox, set_button, zero_button in self._manual_gate_controls.values():
            set_button.clicked.connect(lambda _checked=False, sb=spinbox: self._on_manual_gate_ramp(sb))
            zero_button.clicked.connect(lambda _checked=False, sb=spinbox: self._on_manual_gate_zero(sb))

        self.sp_manual_wavelength = QtWidgets.QDoubleSpinBox()
        self.sp_manual_wavelength.setDecimals(3)
        self.sp_manual_wavelength.setRange(200.0, 2000.0)
        self.sp_manual_wavelength.setValue(633.0)
        self.btn_manual_wavelength = QtWidgets.QPushButton("Go")
        self.btn_manual_wavelength.clicked.connect(self._on_manual_wavelength_move)

        lbl_manual_g1 = QtWidgets.QLabel("G1 / Vtg (V):")
        lbl_manual_g2 = QtWidgets.QLabel("G2 / Vbg (V):")
        lbl_manual_g3 = QtWidgets.QLabel("G3 / Vds (V):")
        lbl_manual_wavelength = QtWidgets.QLabel("Mono (nm):")
        form_manual.addRow(lbl_manual_g1, self._make_manual_control_row(*self._manual_gate_controls["g1"]))
        form_manual.addRow(lbl_manual_g2, self._make_manual_control_row(*self._manual_gate_controls["g2"]))
        form_manual.addRow(lbl_manual_g3, self._make_manual_control_row(*self._manual_gate_controls["g3"]))
        form_manual.addRow(lbl_manual_wavelength, self._make_manual_control_row(self.sp_manual_wavelength, self.btn_manual_wavelength))
        readback_wrap = QtWidgets.QWidget()
        readback_layout = QtWidgets.QGridLayout(readback_wrap)
        readback_layout.setContentsMargins(0, 0, 0, 0)
        readback_layout.setHorizontalSpacing(8)
        readback_layout.setVerticalSpacing(3)
        headers = ("Gate", "Set V", "Meas V", "Current", "Mode")
        for col, text in enumerate(headers):
            label = QtWidgets.QLabel(text)
            label.setProperty("role", "hint")
            label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            readback_layout.addWidget(label, 0, col)

        self.gate_readback_labels: dict[str, dict[str, QtWidgets.QLabel]] = {}
        for row, name in enumerate(("g1", "g2", "g3"), start=1):
            gate_label = QtWidgets.QLabel(name.upper())
            gate_label.setProperty("role", "status-name")
            readback_layout.addWidget(gate_label, row, 0)
            self.gate_readback_labels[name] = {}
            for col, key in enumerate(("set_voltage", "measured_voltage", "current", "mode"), start=1):
                label = QtWidgets.QLabel("--")
                label.setProperty("role", "hint")
                label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                readback_layout.addWidget(label, row, col)
                self.gate_readback_labels[name][key] = label

        self.btn_read_gate_currents = QtWidgets.QPushButton("Refresh Gate Readback")
        self.btn_read_gate_currents.clicked.connect(self._on_read_gate_currents)
        readback_layout.addWidget(self.btn_read_gate_currents, 4, 0, 1, len(headers))
        form_manual.addRow("Gate Readback:", readback_wrap)
        self.lbl_manual_hint = QtWidgets.QLabel(
            "Connect a gate in 2-wire voltage-source mode to enable it. Gate moves are ramped; Zero safely ramps the current source setpoint to 0 V."
        )
        self.lbl_manual_hint.setWordWrap(True)
        self.lbl_manual_hint.setProperty("role", "hint")
        form_manual.addRow("", self.lbl_manual_hint)
        layout.addWidget(grp_manual)

        sep = QtWidgets.QFrame()
        sep.setProperty("role", "separator")
        sep.setFrameShape(QtWidgets.QFrame.Shape.HLine)
        layout.addWidget(sep)

        self.btn_stop = QtWidgets.QPushButton("STOP / ZERO ALL")
        self.btn_stop.setProperty("role", "danger")
        self.btn_stop.setMinimumHeight(44)
        self.btn_stop.clicked.connect(self.stop_requested.emit)
        layout.addWidget(self.btn_stop)
        layout.addStretch()

        for widget in [self.cbo_g1, self.cbo_g2, self.cbo_g3, self.cbo_g1_mode, self.cbo_g2_mode, self.cbo_g3_mode, self.cbo_daq, self.cbo_mono, self.cbo_lockin, self.ed_user, self.ed_device_id, self.ed_base]:
            set_standard_input_height(widget, 26)
            widget.setMinimumWidth(0)
            widget.setSizePolicy(QtWidgets.QSizePolicy.Policy.Ignored, QtWidgets.QSizePolicy.Policy.Fixed)

        for widget in (self.sp_amp, self.sp_lkn, self.sp_manual_g1, self.sp_manual_g2, self.sp_manual_g3, self.sp_manual_wavelength):
            widget.setMinimumWidth(0)
            widget.setSizePolicy(QtWidgets.QSizePolicy.Policy.Ignored, QtWidgets.QSizePolicy.Policy.Fixed)

        apply_tooltip("Scan VISA, DAQ, and serial resources and refresh the address lists.", self.btn_scan)
        apply_tooltip("Address used for the top-gate instrument session.", lbl_g1, self.cbo_g1)
        apply_tooltip("Address used for the back-gate instrument session.", lbl_g2, self.cbo_g2)
        apply_tooltip("Address used when a tab drives Vds from a Keithley source.", lbl_g3, self.cbo_g3)
        apply_tooltip("Choose whether this Keithley should be configured as a 2-wire voltage source or a 4-wire ohms meter after connection.", self.cbo_g1_mode, self.cbo_g2_mode, self.cbo_g3_mode)
        apply_tooltip("DAQ device used for current acquisition and any NI AO-based Vds output.", lbl_daq, self.cbo_daq)
        apply_tooltip("Monochromator / serial resource used by the Photocurrent tab.", lbl_mono, self.cbo_mono)
        apply_tooltip("GPIB resource for the SR830 lock-in amplifier control panel.", lbl_lockin, self.cbo_lockin)
        apply_tooltip("Operator name added to the save path.", lbl_user, self.ed_user)
        apply_tooltip("Device identifier added to the save path.", lbl_device_id, self.ed_device_id)
        apply_tooltip("Root folder where all CSV output is stored.", lbl_base, self.ed_base)
        apply_tooltip("Pre-amp sensitivity shown on the amplifier front panel, entered in amps.", lbl_amp, self.sp_amp)
        apply_tooltip("Lock-in voltage sensitivity used for Ids_X/Ids_Y scaling. If the SR830 is connected, this is updated from SENS?.", lbl_lkn, self.sp_lkn, self.lbl_lkn_source)
        apply_tooltip("Open all configured hardware sessions so tabs can reuse them.", self.btn_connect_all)
        apply_tooltip("Close all instrument sessions managed by the dock.", self.btn_disconnect_all)
        apply_tooltip("Ramp this gate from its current source setpoint to the requested voltage.", lbl_manual_g1, self.sp_manual_g1, self.btn_manual_g1_set)
        apply_tooltip("Ramp this gate from its current source setpoint to the requested voltage.", lbl_manual_g2, self.sp_manual_g2, self.btn_manual_g2_set)
        apply_tooltip("Ramp this gate from its current source setpoint to the requested voltage.", lbl_manual_g3, self.sp_manual_g3, self.btn_manual_g3_set)
        apply_tooltip("Safely ramp this gate from its current source setpoint to 0 V.", self.btn_manual_g1_zero, self.btn_manual_g2_zero, self.btn_manual_g3_zero)
        apply_tooltip("Move the connected monochromator to this wavelength.", lbl_manual_wavelength, self.sp_manual_wavelength, self.btn_manual_wavelength)
        apply_tooltip("Refresh connected gate voltage and current readback once.", self.btn_read_gate_currents)
        apply_tooltip("Immediately request stop and ramp outputs back to 0 V where supported.", self.btn_stop)
        self._update_manual_controls()

    def get_rates(self):
        self.refresh_lockin_sensitivity_from_session()
        preamp_sensitivity_a = max(float(self.sp_amp.value()), self.AMP_MIN_A)
        lockin_sensitivity_v = max(float(self.sp_lkn.value()), self.LIA_MIN_V)
        amp_v_per_a = 1.0 / preamp_sensitivity_a
        lockin_legacy_scale = lockin_sensitivity_v * 1000.0
        return amp_v_per_a, lockin_legacy_scale

    def set_lockin_sensitivity_from_sr830(self, sensitivity_v: float, label: str = ""):
        try:
            sensitivity_v = float(sensitivity_v)
        except (TypeError, ValueError):
            return False
        if sensitivity_v <= 0.0:
            return False
        previous = self.sp_lkn.blockSignals(True)
        try:
            self.sp_lkn.setValue(max(sensitivity_v, self.LIA_MIN_V))
        finally:
            self.sp_lkn.blockSignals(previous)
        self._lockin_sensitivity_from_sr830 = True
        suffix = f" ({label})" if label else ""
        self._set_lockin_sensitivity_source(f"Using SR830 SENS{suffix}")
        self.lbl_lkn_source.setToolTip("")
        self._save_signal_chain_settings()
        return True

    def refresh_lockin_sensitivity_from_session(self) -> bool:
        if self.device_manager is None or not self.device_manager.is_connected("lockin"):
            if not self._lockin_sensitivity_from_sr830:
                self._set_lockin_sensitivity_source("Manual value")
            return False
        session = self.device_manager.get_session("lockin")
        if session is None:
            return False
        try:
            settings = session.read_sensitivity() if hasattr(session, "read_sensitivity") else session.read_settings()
            index = int(settings.get("sensitivity"))
        except Exception as ex:
            self._set_lockin_sensitivity_source("SR830 read failed; using last value", warning=True)
            self.lbl_lkn_source.setToolTip(str(ex))
            return False
        sensitivity_v = settings.get("sensitivity_v")
        if sensitivity_v is None:
            sensitivity_v = sensitivity_value(index, use_current=False)
        if sensitivity_v is None:
            self._set_lockin_sensitivity_source("SR830 sensitivity unknown; using last value", warning=True)
            return False
        label = str(settings.get("sensitivity_label") or "")
        if not label:
            label = SENSITIVITY_LABELS[index] if 0 <= index < len(SENSITIVITY_LABELS) else f"Code {index}"
        return self.set_lockin_sensitivity_from_sr830(float(sensitivity_v), label)

    def _on_manual_lockin_sensitivity_changed(self, *_args):
        self._lockin_sensitivity_from_sr830 = False
        self._set_lockin_sensitivity_source("Manual value")
        self._save_signal_chain_settings()

    def _set_lockin_sensitivity_source(self, text: str, warning: bool = False):
        if not hasattr(self, "lbl_lkn_source"):
            return
        self.lbl_lkn_source.setText(text)
        self.lbl_lkn_source.setProperty("role", "warning-hint" if warning else "hint")
        self.lbl_lkn_source.style().unpolish(self.lbl_lkn_source)
        self.lbl_lkn_source.style().polish(self.lbl_lkn_source)

    def _make_address_mode_row(self, address_widget: QtWidgets.QWidget, mode_widget: QtWidgets.QWidget) -> QtWidgets.QWidget:
        wrap = QtWidgets.QWidget()
        row = QtWidgets.QVBoxLayout(wrap)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(4)
        row.addWidget(address_widget)
        row.addWidget(mode_widget)
        return wrap

    def _make_manual_control_row(self, spinbox: QtWidgets.QWidget, *buttons: QtWidgets.QPushButton) -> QtWidgets.QWidget:
        wrap = QtWidgets.QWidget()
        if len(buttons) == 1:
            row = QtWidgets.QHBoxLayout(wrap)
            row.setContentsMargins(0, 0, 0, 0)
            row.setSpacing(4)
            row.addWidget(spinbox, 1)
            row.addWidget(buttons[0])
            return wrap

        column = QtWidgets.QVBoxLayout(wrap)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(4)
        column.addWidget(spinbox)
        actions = QtWidgets.QHBoxLayout()
        actions.setContentsMargins(0, 0, 0, 0)
        actions.setSpacing(4)
        for button in buttons:
            button.setSizePolicy(QtWidgets.QSizePolicy.Policy.Expanding, QtWidgets.QSizePolicy.Policy.Fixed)
            actions.addWidget(button)
        column.addLayout(actions)
        return wrap

    def _set_combo_data(self, combo: QtWidgets.QComboBox, value: str):
        idx = combo.findData(value)
        combo.setCurrentIndex(idx if idx >= 0 else 0)

    def to_models(self) -> Tuple[Connections, SaveRoot, bool]:
        c = Connections(
            gate1=self.cbo_g1.current_address(),
            gate2=self.cbo_g2.current_address(),
            gate3=self.cbo_g3.current_address(),
            gate1_mode=self.cbo_g1_mode.currentData(),
            gate2_mode=self.cbo_g2_mode.currentData(),
            gate3_mode=self.cbo_g3_mode.currentData(),
            daq_dev=self.cbo_daq.current_address(),
            mono=self.cbo_mono.current_address(),
            lockin=self.cbo_lockin.current_address(),
        )
        s = SaveRoot(user=self.ed_user.text(), device_id=self.ed_device_id.text(), base=self.ed_base.text())
        return c, s, True

    def save_settings(self):
        s = get_app_settings()
        s.setValue("addr/g1", self.cbo_g1.current_address())
        s.setValue("addr/g2", self.cbo_g2.current_address())
        s.setValue("addr/g3", self.cbo_g3.current_address())
        s.setValue("mode/g1", self.cbo_g1_mode.currentData())
        s.setValue("mode/g2", self.cbo_g2_mode.currentData())
        s.setValue("mode/g3", self.cbo_g3_mode.currentData())
        s.setValue("addr/daq", self.cbo_daq.current_address())
        s.setValue("addr/mono", self.cbo_mono.current_address())
        s.setValue("addr/lockin", self.cbo_lockin.current_address())
        s.setValue("path/user", self.ed_user.text())
        s.setValue("path/device_id", self.ed_device_id.text())
        s.setValue("path/base", self.ed_base.text())
        s.setValue("rates/amp", float(self.sp_amp.value()))
        s.setValue("rates/lkn", float(self.sp_lkn.value()))
        s.sync()

    def _save_signal_chain_settings(self, *_args):
        """Persist signal-chain values as soon as an operator changes them."""
        s = get_app_settings()
        s.setValue("rates/amp", float(self.sp_amp.value()))
        s.setValue("rates/lkn", float(self.sp_lkn.value()))
        s.sync()

    def load_settings(self):
        s = get_app_settings()
        self.cbo_g1.setCurrentText(str(s.value("addr/g1", self.conns.gate1)))
        self.cbo_g2.setCurrentText(str(s.value("addr/g2", self.conns.gate2)))
        self.cbo_g3.setCurrentText(str(s.value("addr/g3", self.conns.gate3)))
        self._set_combo_data(self.cbo_g1_mode, str(s.value("mode/g1", self.conns.gate1_mode)))
        self._set_combo_data(self.cbo_g2_mode, str(s.value("mode/g2", self.conns.gate2_mode)))
        self._set_combo_data(self.cbo_g3_mode, str(s.value("mode/g3", self.conns.gate3_mode)))
        self.cbo_daq.setCurrentText(str(s.value("addr/daq", self.conns.daq_dev)))
        self.cbo_mono.setCurrentText(str(s.value("addr/mono", self.conns.mono)))
        self.cbo_lockin.setCurrentText(str(s.value("addr/lockin", self.conns.lockin)))
        self.ed_user.setText(str(s.value("path/user", self.save_root.user)))
        device_id = str(s.value("path/device_id", s.value("path/sample", "YZ315")))
        self.ed_device_id.setText(device_id)
        self.ed_base.setText(str(s.value("path/base", self.save_root.base)))
        saved_amp = float(s.value("rates/amp", 1e7))
        saved_lkn = float(s.value("rates/lkn", 100.0))

        # Backward compatibility:
        # Old builds stored pre-amp as V/A and lock-in sensitivity as a millivolt-style scalar.
        display_amp = (1.0 / saved_amp) if saved_amp > 1.0 else saved_amp
        display_lkn = (saved_lkn / 1000.0) if saved_lkn > 10.0 else saved_lkn
        self.sp_amp.setValue(max(display_amp, self.AMP_MIN_A))
        self.sp_lkn.setValue(max(display_lkn, self.LIA_MIN_V))

    def set_device_manager(self, device_manager: DeviceManager):
        self.device_manager = device_manager
        self._bind_device_manager()

    def _bind_device_manager(self):
        if self.device_manager is None:
            return
        self.btn_connect_all.clicked.connect(self._on_connect_all_clicked)
        self.btn_disconnect_all.clicked.connect(self._on_disconnect_all_clicked)
        self.device_manager.status_changed.connect(self._on_device_status_changed)
        self.device_manager.operation_changed.connect(self._on_operation_changed)
        self.device_manager.manual_control_finished.connect(self._on_manual_control_finished)
        self.device_manager.gate_currents_read.connect(self._on_gate_currents_read)
        for name in ("g1", "g2", "g3", "daq", "mono", "lockin"):
            self._on_device_status_changed(name, self.device_manager.state(name), self.device_manager.detail(name))
        self._update_reconnect_indicators()
        self._update_manual_controls()

    def _on_connect_all_clicked(self):
        if hasattr(self.window(), "refresh_models_from_ui"):
            self.window().refresh_models_from_ui()
        self.device_manager.connect_all()

    def _on_disconnect_all_clicked(self):
        self.device_manager.disconnect_all()

    def _on_device_status_changed(self, name: str, state: str, detail: str):
        self.dock_status_panel.set_status(name, state, detail if state == "err" else None)
        if name in getattr(self, "gate_readback_labels", {}) and state != "ok":
            self._set_gate_readback_row(name, {})
        if name == "lockin":
            if state == "ok":
                QtCore.QTimer.singleShot(0, self.refresh_lockin_sensitivity_from_session)
            elif self._lockin_sensitivity_from_sr830:
                self._set_lockin_sensitivity_source("Last SR830 value; reconnect to update")
        self._update_reconnect_indicators()
        self._update_manual_controls()

    def _on_operation_changed(self, busy: bool, message: str):
        self.btn_connect_all.setEnabled(not busy)
        self.btn_disconnect_all.setEnabled(not busy)
        self._connection_detail = message
        if busy:
            summary = "Working..."
            role = "hint"
        elif "fail" in message.lower() or "cannot" in message.lower():
            summary = "Connection issue"
            role = "warning-hint"
        elif "disconnect" in message.lower():
            summary = "Disconnected"
            role = "hint"
        elif "connect" in message.lower():
            summary = "Connected"
            role = "hint"
        else:
            summary = "Ready"
            role = "hint"
        self.lbl_connection_status.setText(summary)
        self.lbl_connection_status.setProperty("role", role)
        self.lbl_connection_status.style().unpolish(self.lbl_connection_status)
        self.lbl_connection_status.style().polish(self.lbl_connection_status)
        self.lbl_connection_status.setToolTip(message)
        self.btn_connection_details.setVisible(bool(message))
        self._update_reconnect_indicators()
        self._update_manual_controls()

    def _on_manual_gate_ramp(self, spinbox: QtWidgets.QDoubleSpinBox):
        name = next(name for name, (control, _set, _zero) in self._manual_gate_controls.items() if control is spinbox)
        self.device_manager.ramp_gate(name, spinbox.value())

    def _on_manual_gate_zero(self, spinbox: QtWidgets.QDoubleSpinBox):
        name = next(name for name, (control, _set, _zero) in self._manual_gate_controls.items() if control is spinbox)
        self.device_manager.ramp_gate(name, 0.0)

    def _on_manual_wavelength_move(self):
        self.device_manager.set_monochromator_wavelength(self.sp_manual_wavelength.value())

    def _on_read_gate_currents(self):
        self.device_manager.read_gate_currents()

    def _on_manual_control_finished(self, name: str, success: bool, message: str):
        self.lbl_manual_hint.setText(message)
        self.lbl_manual_hint.setProperty("role", "hint" if success and "unavailable" not in message.lower() else "warning-hint")
        self.lbl_manual_hint.style().unpolish(self.lbl_manual_hint)
        self.lbl_manual_hint.style().polish(self.lbl_manual_hint)
        if not success:
            return
        if name in self._manual_gate_controls:
            spinbox, set_button, zero_button = self._manual_gate_controls[name]
            if "0 V" in message:
                spinbox.setValue(0.0)
                flash_button_success(zero_button)
            else:
                flash_button_success(set_button)
        elif name == "mono":
            flash_button_success(self.btn_manual_wavelength)

    def _on_gate_currents_read(self, currents: dict, message: str):
        for name, readback in currents.items():
            self._set_gate_readback_row(name, readback if isinstance(readback, dict) else {})
            if isinstance(readback, dict):
                self._sync_gate_target_from_readback(name, readback)
        self.lbl_manual_hint.setText(message)
        self.lbl_manual_hint.setProperty("role", "warning-hint" if "unavailable" in message.lower() else "hint")
        self.lbl_manual_hint.style().unpolish(self.lbl_manual_hint)
        self.lbl_manual_hint.style().polish(self.lbl_manual_hint)
        return

    def _set_gate_readback_row(self, name: str, readback: dict):
        labels = self.gate_readback_labels.get(name)
        if not labels:
            return
        error = str(readback.get("error") or "") if isinstance(readback, dict) else ""
        connected = bool(readback.get("connected")) if isinstance(readback, dict) else False
        mode = str(readback.get("mode") or "") if isinstance(readback, dict) else ""

        labels["set_voltage"].setText(self._format_voltage(readback.get("set_voltage")))
        labels["measured_voltage"].setText(self._format_voltage(readback.get("measured_voltage")))
        labels["current"].setText(self._format_current(readback.get("current")))
        labels["mode"].setText(self._short_gate_mode(mode) if connected else "--")

        tooltip = error or ("Connected" if connected else "Disconnected")
        for label in labels.values():
            label.setToolTip(tooltip)

    def _sync_gate_target_from_readback(self, name: str, readback: dict):
        if name not in self._manual_gate_controls:
            return
        value = readback.get("set_voltage")
        if value is None:
            value = readback.get("measured_voltage")
        if value is None:
            return
        spinbox = self._manual_gate_controls[name][0]
        previous = spinbox.blockSignals(True)
        try:
            spinbox.setValue(float(value))
        except (TypeError, ValueError):
            pass
        finally:
            spinbox.blockSignals(previous)

    @staticmethod
    def _format_voltage(value) -> str:
        try:
            return f"{float(value):+.4f} V"
        except (TypeError, ValueError):
            return "--"

    @staticmethod
    def _format_current(value) -> str:
        try:
            return f"{float(value):.3e} A"
        except (TypeError, ValueError):
            return "--"

    @staticmethod
    def _short_gate_mode(mode: str) -> str:
        if mode == "voltage_2w":
            return "2-wire V"
        if mode == "ohm_4w":
            return "4-wire Ohm"
        return "--"

    def _update_manual_controls(self):
        if self.device_manager is None:
            for spinbox, set_button, zero_button in self._manual_gate_controls.values():
                spinbox.setEnabled(False)
                set_button.setEnabled(False)
                zero_button.setEnabled(False)
            self.sp_manual_wavelength.setEnabled(False)
            self.btn_manual_wavelength.setEnabled(False)
            self.btn_read_gate_currents.setEnabled(False)
            return
        enabled = not self.device_manager.is_busy() and not self.device_manager.current_in_use()
        for name, (spinbox, set_button, zero_button) in self._manual_gate_controls.items():
            gate_enabled = enabled and self.device_manager.is_connected(name) and self.device_manager.is_voltage_source_mode(name)
            spinbox.setEnabled(gate_enabled)
            set_button.setEnabled(gate_enabled)
            zero_button.setEnabled(gate_enabled)
        mono_enabled = enabled and self.device_manager.is_connected("mono")
        self.sp_manual_wavelength.setEnabled(mono_enabled)
        self.btn_manual_wavelength.setEnabled(mono_enabled)
        self.btn_read_gate_currents.setEnabled(
            enabled
            and any(
                self.device_manager.is_connected(name)
                for name in ("g1", "g2", "g3")
            )
        )

    def _show_connection_details(self):
        if not self._connection_detail:
            return
        dialog = QtWidgets.QMessageBox(self)
        dialog.setWindowTitle("Connection Status Details")
        dialog.setIcon(QtWidgets.QMessageBox.Icon.Information)
        dialog.setText(self.lbl_connection_status.text())
        dialog.setDetailedText(self._connection_detail)
        dialog.setStandardButtons(QtWidgets.QMessageBox.StandardButton.Ok)
        dialog.exec()

    def _set_reconnect_state(self, widget: QtWidgets.QWidget, needs_reconnect: bool, tooltip: str = ""):
        widget.setProperty("reconnect", "true" if needs_reconnect else "false")
        widget.style().unpolish(widget)
        widget.style().polish(widget)
        widget.setToolTip(tooltip)

    def _update_reconnect_indicators(self):
        if self.device_manager is None:
            return
        mapping = {
            "g1": self.cbo_g1,
            "g2": self.cbo_g2,
            "g3": self.cbo_g3,
            "daq": self.cbo_daq,
            "mono": self.cbo_mono,
            "lockin": self.cbo_lockin,
        }
        mode_widgets = {
            "g1": self.cbo_g1_mode,
            "g2": self.cbo_g2_mode,
            "g3": self.cbo_g3_mode,
        }
        changed = []
        for name, widget in mapping.items():
            needs = self.device_manager.needs_reconnect(name)
            tooltip = ""
            if needs:
                changed.append(name.upper())
                tooltip = (
                    f"Live session still uses {self.device_manager.connected_address(name)}"
                    f" in {keithley_mode_label(self.device_manager.connected_mode(name)) if name in {'g1','g2','g3'} else 'its current mode'}. "
                    "Reconnect from Instrument Setup to apply the edited address or mode."
                )
            self._set_reconnect_state(widget, needs, tooltip)
            if name in mode_widgets:
                self._set_reconnect_state(mode_widgets[name], needs, tooltip)
        self.lbl_reconnect_hint.setText(
            f"Reconnect required to apply edited address or mode for: {', '.join(changed)}" if changed else ""
        )

    def _start_scan(self):
        if self._scan_thread is not None and self._scan_thread.isRunning():
            return
        self.btn_scan.setEnabled(False)
        self.lbl_scan_status.setText("Scanning...")
        self.lbl_scan_status.setStyleSheet("color: #757575;")
        self._scan_thread = ScanWorker(self)
        self._scan_thread.results_ready.connect(self._on_scan_results)
        self._scan_thread.scan_failed.connect(self._on_scan_failed)
        self._scan_thread.finished.connect(self._on_scan_finished)
        self._scan_thread.start()

    def _on_scan_results(self, results: dict):
        gpib = results.get("gpib", [])
        daq = results.get("daq", [])
        asrl = results.get("asrl", [])
        self.cbo_g1.populate(gpib, self.cbo_g1.current_address())
        self.cbo_g2.populate(gpib, self.cbo_g2.current_address())
        self.cbo_g3.populate(gpib, self.cbo_g3.current_address())
        self.cbo_lockin.populate(gpib, self.cbo_lockin.current_address())
        self.cbo_daq.populate(daq, self.cbo_daq.current_address())
        self.cbo_mono.populate(asrl, self.cbo_mono.current_address())

        total = len(gpib) + len(daq) + len(asrl)
        if total == 0:
            self.lbl_scan_status.setText("No devices found - check cable connections.")
            self.lbl_scan_status.setStyleSheet("color: #E65100;")
        else:
            self.lbl_scan_status.setText(f"Found: {len(gpib)} GPIB, {len(daq)} DAQ, {len(asrl)} ASRL")
            self.lbl_scan_status.setStyleSheet("color: #2E7D32;")

    def _on_scan_failed(self, message: str):
        self.lbl_scan_status.setText(f"Scan failed - {message or 'check VISA installation.'}")
        self.lbl_scan_status.setStyleSheet("color: #E65100;")

    def _on_scan_finished(self):
        self.btn_scan.setEnabled(True)
        if self._scan_thread is not None:
            self._scan_thread.deleteLater()
            self._scan_thread = None
