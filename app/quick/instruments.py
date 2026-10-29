"""Instrument and calibration presentation without QWidget ownership."""
from __future__ import annotations

import math

from PySide6.QtCore import QObject, Property, Signal, Slot

from app.device_manager import DeviceManager
from app.engine.transport_tasks import TaskLane
from app.models import Connections, SaveRoot
from app.settings import get_app_settings
from app.signal_chain import SignalChainSnapshot, preamp_gain_v_per_a, sr830_xy_output_gain
from app.quick.forms import field, checked_value
from app.keithley_modes import keithley_mode_options


ADDRESS_KEYS = {"gate1": "g1", "gate2": "g2", "gate3": "g3", "daq_dev": "daq",
                "mono": "mono", "lockin": "lockin", "drive_lockin": "drive_lockin"}
MODE_OPTIONS = [{"label": label, "value": key} for key, label in keithley_mode_options()]


class InstrumentService(QObject):
    changed = Signal()
    connectionFieldsChanged = Signal()
    sampleFieldsChanged = Signal()
    signalFieldsChanged = Signal()
    messageRaised = Signal(str)
    calibrationChanged = Signal()

    def __init__(self, parent=None, *, manager=None, settings=None, offline=False):
        super().__init__(parent)
        self.settings = settings or get_app_settings()
        self.connections = manager.connections if manager else Connections()
        self.save_root = SaveRoot()
        self.offline = offline
        self._active = lambda: False
        self._message = "Offline preview — hardware access is blocked" if offline else "Ready"
        self._readbacks = {}
        self._resources = {}
        try:
            self._manual_step = checked_value(field("step", "Manual step", .1, min=.001, max=1.), self.settings.value("manual/step_v", .1))
        except (ValueError, TypeError, OverflowError):
            self._manual_step = .1
        self._scan_busy = False
        self._closing = False
        self._daq_readbacks = {}
        self._lockin_calibration = {}
        self._load()
        self.manager = manager or DeviceManager(self.connections)
        self._scan_lane = TaskLane("quick-resource-scan", self)
        self.manager.status_changed.connect(self._status)
        self.manager.resources_changed.connect(lambda *_: self.changed.emit())
        self.manager.operation_changed.connect(self._operation)
        self.manager.gate_currents_read.connect(self._read)
        self.manager.manual_control_finished.connect(self._manual_done)
        self.manager.daq_output_finished.connect(self._daq_done)
        self.manager.protection_changed.connect(lambda _name, _ok, msg, _value: self._report(msg))
        self.lockins = {}
        self.context_provider = lambda: {}

    def _load(self):
        s = self.settings
        for attr, short in ADDRESS_KEYS.items():
            setattr(self.connections, attr, str(s.value("addr/" + short, getattr(self.connections, attr))))
        for i in range(1, 4):
            prefix = f"gate{i}"
            setattr(self.connections, prefix + "_mode", str(s.value(f"mode/g{i}", getattr(self.connections, prefix + "_mode"))))
            for key in ("max_voltage_v", "current_compliance_a"):
                attr = prefix + "_" + key
                setattr(self.connections, attr, float(s.value(f"protection/g{i}/{key}", getattr(self.connections, attr))))
        self.connections.drag_drive_enabled = s.value("drag_drive/enabled", False, type=bool)
        self.save_root.user = str(s.value("path/user", self.save_root.user))
        self.save_root.device_id = str(s.value("path/device_id", s.value("path/sample", self.save_root.device_id)))
        self.save_root.base = str(s.value("path/base", self.save_root.base))
        amp = float(s.value("rates/amp", 1e-7))
        sensitivity = float(s.value("rates/lkn", .1))
        self.calibration = {
            "preamp_sensitivity_a": 1 / amp if amp > 1 else amp,
            "lockin_sensitivity_v": sensitivity / 1000 if sensitivity > 10 else sensitivity,
            "frequency_hz": float(s.value("lockin/frequency_hz", 1000)),
            "ac_contact": str(s.value("signal_chain/ac_contact", "")),
            "ac_voltage_ratio": float(s.value("signal_chain/ac_voltage_ratio", 0) or 0),
            "series_resistance_ohm": float(s.value("drag_drive/rs_ohm", 100000)),
        }

    def set_active_provider(self, callback):
        self._active = callback

    @Property(bool, notify=changed)
    def busy(self):
        return bool(self._closing or self._scan_busy or self.manager.is_busy() or self.manager.current_in_use() or self._active())

    @Property(str, notify=changed)
    def message(self):
        return self._message

    @Property(float, notify=changed)
    def manualStep(self):
        return self._manual_step

    @Slot(str)
    def setManualStep(self, text):
        try:
            value = checked_value(field("step", "Manual step", self._manual_step, min=.001, max=1.), text)
            if self.busy:
                raise RuntimeError("Wait for active operations before editing the manual step")
            self._manual_step = value
            self.settings.setValue("manual/step_v", value)
            self.changed.emit()
        except (ValueError, TypeError, RuntimeError) as exc:
            self._report(str(exc))

    @Property('QVariantList', notify=connectionFieldsChanged)
    def connectionFields(self):
        values = []
        for attr, short in ADDRESS_KEYS.items():
            values.append(field(attr, {"daq_dev": "NI DAQ device", "mono": "Monochromator",
                                      "lockin": "Drag / primary lock-in", "drive_lockin": "Drive lock-in"}.get(attr, short.upper()),
                                getattr(self.connections, attr), group="Instrument addresses"))
        for i in range(1, 4):
            prefix = f"gate{i}"
            values.extend([
                field(prefix + "_mode", "Mode", getattr(self.connections, prefix + "_mode"), options=MODE_OPTIONS, group=f"G{i} protection"),
                field(prefix + "_max_voltage_v", "Maximum source voltage (V)", getattr(self.connections, prefix + "_max_voltage_v"), min=1e-6, max=200, group=f"G{i} protection"),
                field(prefix + "_current_compliance_a", "Current compliance (A)", getattr(self.connections, prefix + "_current_compliance_a"), min=1e-12, max=1.05, group=f"G{i} protection"),
            ])
        return values

    @Property('QVariantList', notify=sampleFieldsChanged)
    def sampleFields(self):
        return [field("user", "Operator", self.save_root.user), field("device_id", "Device / sample", self.save_root.device_id),
                field("base", "Data folder", self.save_root.base)]

    @Property('QVariantList', notify=signalFieldsChanged)
    def signalFields(self):
        v = self.calibration
        return [field("drag_drive_enabled", "Use Drive lock-in for Drag / Drive", self.connections.drag_drive_enabled, group="Optional Drive channel"),
                field("series_resistance_ohm", "Drive series resistance (Ω)", v["series_resistance_ohm"], min=.001, max=1e12, group="Optional Drive channel", visible=self.connections.drag_drive_enabled),
                field("preamp_sensitivity_a", "Preamp sensitivity (A/V)", v["preamp_sensitivity_a"], min=1e-15, max=1, group="Manual signal conversion"),
                field("lockin_sensitivity_v", "Lock-in sensitivity (V)", v["lockin_sensitivity_v"], min=1e-12, max=10,
                      readOnly=self.manager.is_connected("lockin"),
                      group="Lock-in calibration", description="Read from the connected primary lock-in; verified again before acquisition"),
                field("frequency_hz", "Reference frequency (Hz)", v["frequency_hz"], min=.001, max=1e7, group="Lock-in calibration"),
                field("ac_contact", "AC contact", v["ac_contact"], group="Sample AC estimate"),
                field("ac_voltage_ratio", "Sample / source voltage ratio", v["ac_voltage_ratio"], min=0, max=1e9, group="Sample AC estimate")]

    @Property('QVariantList', notify=changed)
    def devices(self):
        result = []
        for name in self.manager.sessions:
            rb = self._readbacks.get(name, {})
            gate = name in ("g1", "g2", "g3")
            target_available = gate and not self._closing and not self._active() and self.manager.can_accept_gate_target(name)
            protection = gate and self.manager.protection_is_applied(name)
            result.append(dict(name=name, label=name.upper(), state=self.manager.state(name),
                               detail=self.manager.detail(name), connected=self.manager.is_connected(name),
                               inUse=self.manager.is_in_use(name), reconnect=self.manager.needs_reconnect(name),
                               canRamp=bool(target_available and protection), canZero=bool(target_available),
                               canRead=bool(gate and not self.busy and self.manager.is_connected(name)),
                               protectionApplied=bool(protection), mode=str(rb.get("mode", "")),
                               voltage=self._number(rb.get("measured_voltage")),
                               setVoltage=self._number(rb.get("set_voltage")),
                               measuredVoltage=self._number(rb.get("measured_voltage")),
                               maxVoltage=self._number(rb.get("max_source_voltage_v")),
                               currentCompliance=self._number(rb.get("current_compliance_a")),
                               complianceTripped=rb.get("current_compliance_tripped"),
                               readbackError=str(rb.get("error") or ""),
                               current=self._number(rb.get("current"))))
        return result

    @staticmethod
    def _number(value):
        try:
            number = float(value)
            return f"{number:.9g}" if math.isfinite(number) else "—"
        except (TypeError, ValueError):
            return "—"

    @Property('QVariantList', notify=changed)
    def daqOutputs(self):
        return [dict(channel=channel, commanded=self._number(state.get("commanded_v")),
                     measured=self._number(state.get("measured_v")), error=str(state.get("error", "")))
                for channel, state in sorted(self._daq_readbacks.items())]

    @Property('QVariantMap', notify=changed)
    def discovered(self):
        return self._resources

    calibrationStatus = Property('QVariantMap', lambda s: dict(s._lockin_calibration), notify=changed)

    @Property(str, notify=changed)
    def acVoltageEstimate(self):
        primary = self.lockins.get("lockin")
        amplitude = getattr(primary, "_verified_settings", {}).get("sine_out_v") if primary else None
        ratio = self.calibration["ac_voltage_ratio"]
        if amplitude is None or not ratio:
            return "Sample AC estimate requires a verified source amplitude and configured voltage ratio"
        return f"Estimated sample AC: {float(amplitude) * ratio:.9g} V ({float(amplitude):.9g} V × {ratio:g})"

    def setVerifiedLockin(self, name, data):
        values = data.get("settings", {})
        try:
            if values.get("input_config") not in (0, 1):
                raise ValueError("Current-input mode: voltage conversion unavailable")
            from instruments.SR830 import sensitivity_value
            sensitivity = float(values.get("sensitivity_v") or sensitivity_value(values.get("sensitivity")))
            if not math.isfinite(sensitivity) or sensitivity <= 0:
                raise ValueError("Sensitivity readback is invalid")
            self._lockin_calibration[name] = f"Verified from instrument: {sensitivity:g} V"
            if name == "lockin":
                self.calibration["lockin_sensitivity_v"] = sensitivity
                frequency = float(values.get("frequency_hz", self.calibration["frequency_hz"]))
                if math.isfinite(frequency) and frequency > 0:
                    self.calibration["frequency_hz"] = frequency
                self.settings.setValue("rates/lkn", sensitivity)
            self.changed.emit()
            self.signalFieldsChanged.emit()
            self.calibrationChanged.emit()
        except (ValueError, TypeError) as exc:
            self.invalidateLockin(name, str(exc))

    def invalidateLockin(self, name, reason):
        self._lockin_calibration[name] = "Unverified: " + str(reason)
        self.changed.emit()

    @Slot(str, 'QVariant')
    def setValue(self, key, value):
        try:
            if self.busy:
                raise RuntimeError("Wait for measurement and instrument cleanup before changing configuration")
            specs = {s["key"]: s for s in self.connectionFields + self.sampleFields + self.signalFields}
            if key not in specs:
                raise ValueError("Unknown setting")
            if specs[key].get("readOnly"):
                raise ValueError("Connected lock-in sensitivity is read from the instrument; refresh its panel")
            value = checked_value(specs[key], value)
            if key in ("user", "device_id", "base"):
                setattr(self.save_root, key, value)
                setting_key = "path/" + key
            elif key in self.calibration:
                self.calibration[key] = value
                setting_key = {"preamp_sensitivity_a": "rates/amp", "lockin_sensitivity_v": "rates/lkn",
                               "frequency_hz": "lockin/frequency_hz", "series_resistance_ohm": "drag_drive/rs_ohm"}.get(key, "signal_chain/" + key)
            else:
                setattr(self.connections, key, value)
                if key in ADDRESS_KEYS:
                    setting_key = "addr/" + ADDRESS_KEYS[key]
                elif key == "drag_drive_enabled":
                    setting_key = "drag_drive/enabled"
                else:
                    gate, suffix = key.split("_", 1)
                    short = "g" + gate[-1]
                    setting_key = "mode/" + short if suffix == "mode" else "protection/" + short + "/" + suffix
                self.manager.sync_addresses()
            self.settings.setValue(setting_key, value)
            self.settings.sync()
            if key in ("user", "device_id", "base"):
                self.sampleFieldsChanged.emit()
            elif key in self.calibration or key == "drag_drive_enabled":
                self.signalFieldsChanged.emit()
            else:
                self.connectionFieldsChanged.emit()
            self.changed.emit()
            self.calibrationChanged.emit()
        except (ValueError, TypeError, RuntimeError, OverflowError) as exc:
            self._report(str(exc))

    @Slot(str, 'QVariantMap')
    def command(self, name, arguments):
        try:
            if self.offline:
                raise RuntimeError("Offline preview: hardware commands are disabled")
            if self._closing or self._active() or self._scan_busy or self.manager.current_in_use():
                raise RuntimeError("Wait for measurement, manual operations and cleanup before using instrument commands")
            args = dict(arguments)
            if name == "connect":
                self.manager.connect_all()
            elif name == "disconnect":
                self.manager.disconnect_all()
            elif name == "scan":
                if self.busy:
                    raise RuntimeError("Wait for instrument operations before scanning")
                from app.hw_discovery import scan_all
                self._scan_busy = True
                self._scan_lane.submit(scan_all, self._scan_done)
            elif name == "ramp":
                from app.constants import V_LIMIT
                target = checked_value(field("target", "Manual gate target", 0., min=-V_LIMIT, max=V_LIMIT), args["target"])
                self.manager.ramp_gate(str(args["device"]), target)
            elif name == "step":
                delta = checked_value(field("delta", "Manual step", .1, min=-1., max=1.), args["delta"])
                self.manager.step_gate(str(args["device"]), delta)
            elif name == "read":
                self.manager.read_gate_currents(names=[str(args["device"])])
            elif name == "daq":
                target = float(args["target"])
                if not math.isfinite(target):
                    raise ValueError("Enter a finite target")
                channel = checked_value(field("channel", "DAQ AO channel", 0, min=0), args["channel"])
                self.manager.ramp_daq_output(channel, target)
            elif name == "wavelength":
                target = checked_value(field("wavelength", "Wavelength (nm)", 633., min=200., max=2000.), args["target"])
                self.manager.set_monochromator_wavelength(target)
            elif name == "protection":
                device = str(args["device"])
                if device not in ("g1", "g2", "g3"):
                    raise ValueError("Unknown gate")
                prefix = "gate" + device[-1]
                self.manager.apply_gate_protection(device, getattr(self.connections, prefix + "_max_voltage_v"),
                                                   getattr(self.connections, prefix + "_current_compliance_a"))
            else:
                raise ValueError("Unknown instrument command")
            self.changed.emit()
        except Exception as exc:
            self._report(str(exc))

    def _scan_done(self, data, error):
        self._scan_busy = False
        self._resources = data if not error else {}
        self._report(str(error) if error else "Hardware scan finished")

    def _status(self, name, state, _detail):
        if state != "ok":
            self._readbacks.pop(name, None)
            if name == "daq":
                self._daq_readbacks.clear()
            if name in ("lockin", "lockin_drive"):
                self._lockin_calibration[name] = "Disconnected; manual fallback applies"
        elif name == "daq":
            session = self.manager.get_session("daq")
            if session and hasattr(session, "get_ao_state"):
                self._daq_readbacks = {int(channel): dict(session.get_ao_state(channel))
                                      for channel in getattr(session, "ao_channel_indexes", [])}
        self.changed.emit()
        if name == "lockin":
            self.signalFieldsChanged.emit()

    def _operation(self, _busy, message):
        self._report(message)

    def _read(self, currents, message):
        self._readbacks.update({name: dict(value) if isinstance(value, dict) else {} for name, value in currents.items()})
        self._report(message)

    def _manual_done(self, name, _ok, message, result):
        self._readbacks[name] = {**self._readbacks.get(name, {}), **result}
        self._report(message)

    def _daq_done(self, channel, ok, message, state):
        self._daq_readbacks[int(channel)] = {**state, **({"error": str(message)} if not ok else {})}
        self._report(message)

    def _report(self, text):
        self._message = str(text)
        self.changed.emit()
        if text:
            self.messageRaised.emit(str(text))

    def get_rates(self):
        sensitivity = self.calibration["lockin_sensitivity_v"]
        if self.manager.is_connected("lockin"):
            data = self.manager.get_session("lockin").read_sensitivity()
            if data.get("input_config", 0) not in (0, 1):
                raise RuntimeError("DAQ current conversion requires voltage-mode lock-in sensitivity")
            from instruments.SR830 import sensitivity_value
            sensitivity = float(data.get("sensitivity_v") or sensitivity_value(data.get("sensitivity")))
            if not math.isfinite(sensitivity) or sensitivity <= 0:
                raise RuntimeError("Connected lock-in sensitivity is not verified")
            self.calibration["lockin_sensitivity_v"] = sensitivity
        return preamp_gain_v_per_a(self.calibration["preamp_sensitivity_a"]), sr830_xy_output_gain(sensitivity)

    def signal_chain_snapshot(self):
        primary = self.lockins.get("lockin")
        v = self.calibration
        dual = dict(enabled=self.connections.drag_drive_enabled, series_resistance_ohm=v["series_resistance_ohm"])
        return SignalChainSnapshot(frequency_hz=v["frequency_hz"], lockin_sensitivity_v=v["lockin_sensitivity_v"],
            preamp_sensitivity_a=v["preamp_sensitivity_a"], lockin_settings=primary.settings_snapshot() if primary else None,
            experiment_context=self.context_provider(), ac_contact=v["ac_contact"], ac_voltage_ratio=v["ac_voltage_ratio"] or None,
            drag_drive=dual)

    def close(self):
        self._closing = True
        self.changed.emit()
        self._scan_lane.close()
