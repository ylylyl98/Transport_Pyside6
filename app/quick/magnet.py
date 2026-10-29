"""Magnet/temperature presentation with session-bound commissioning review."""
from __future__ import annotations

from dataclasses import asdict, is_dataclass
from copy import deepcopy
import json
import math
import time
import uuid

from PySide6.QtCore import QObject, Property, Signal, Slot, QTimer

from controllers.magnet_controller import MagnetController
from controllers.attodry2100_controller import AttoDRY2100Controller
from controllers.lakeshore335_controller import LakeShore335Controller
from app.thermal_safety import ThermalSafetyEvaluator, _sensor_status_is_clear
from app.quick.forms import field, checked_value
from app.sample_heater_ranges import validate_heater_ranges, select_heater_range
from app.sample_heating_interlock import SampleHeatingInterlock
from utils.config import cfg


class MagnetService(QObject):
    changed = Signal()
    fieldsChanged = Signal()
    heaterBandFieldsChanged = Signal()
    readinessChanged = Signal(bool)
    messageRaised = Signal(str)
    confirmationRequested = Signal(str, str, str)

    def __init__(self, parent=None, *, offline=False, magnet1000=None, magnet2100=None, lakeshore=None):
        super().__init__(parent)
        self.offline = offline
        self.heating_interlock = (getattr(magnet1000, "heating_interlock", None)
                                  or getattr(lakeshore, "heating_interlock", None)
                                  or SampleHeatingInterlock())
        self.magnet1000 = magnet1000 or MagnetController(self, interlock=self.heating_interlock)
        self.magnet2100 = magnet2100 or AttoDRY2100Controller(config=cfg.attodry2100, parent=self)
        self.lakeshore335 = lakeshore or LakeShore335Controller(self, interlock=self.heating_interlock)
        if (getattr(self.lakeshore335, "heating_interlock", self.heating_interlock)
                is not self.heating_interlock):
            raise ValueError("APS100 and LS335 controllers must share their heating interlock")
        self.thermal_safety = ThermalSafetyEvaluator(cfg.lakeshore335)
        self._backend = "1000"
        self._auto = True
        self._review = None
        self._connected = {"1000": False, "2100": False, "temperature": False}
        self._pending = set()
        self._snapshots = {}
        self._message = "Review configured limits before connecting"
        self._log = []
        self._active = lambda: False
        self._confirmation = None
        self._pending_start_2100 = False
        self._target = None
        self._stable_since = None
        self._control_failed = False
        self._control_state = None
        self._controls_seen = False
        self._last_ready = None
        self._closing = False
        self._last_temperature_poll = 0.
        self._last_heating_block = None
        self._bands = deepcopy(cfg.lakeshore335.sample_auto_heater_ranges)
        self._values = dict(target_field=0., final_mode="persistent", target_temperature=4.,
                            heater_range=None, ramp_enabled=None, ramp_rate=1., wait_temperature=False)
        self.magnet1000._worker.thermal_source = self.thermal_safety
        self.magnet1000._worker.thermal_field_updated.connect(self.thermal_safety.note_magnet_snapshot)
        self.magnet1000.snapshot_updated.connect(self.thermal_safety.note_magnet_snapshot)
        self.magnet1000.snapshot_updated.connect(lambda s: self._snapshot("1000", s))
        self.magnet2100.snapshot_updated.connect(lambda s: self._snapshot("2100", s))
        self.magnet2100.temperature_updated.connect(lambda s: self._snapshot("temperature2100", s))
        self.lakeshore335.snapshot_updated.connect(self._temperature_snapshot)
        self.lakeshore335.control_result.connect(self._control_result)
        for key, controller in (("1000", self.magnet1000), ("2100", self.magnet2100), ("temperature", self.lakeshore335)):
            controller.connected.connect(lambda _identity, name=key: self._connection(name, True))
            controller.disconnected.connect(lambda name=key: self._connection(name, False))
            controller.error.connect(lambda msg, name=key: self._error(name, msg))
            controller.fault.connect(lambda msg, name=key: self._error(name, msg))
        self.magnet1000.operation_finished.connect(self._operation1000)
        self.magnet1000.safe_move_result.connect(self._move_result)
        self.magnet1000.transition_progress.connect(lambda label, value: self._report(f"{label}: {value:g}"))
        self.magnet1000.exclusive_changed.connect(lambda *_: self.changed.emit())
        self.magnet2100.operation_finished.connect(self._operation2100)
        self._timer = QTimer(self)
        self._timer.setInterval(500)
        self._timer.timeout.connect(self._tick)
        self._timer.start()

    def set_active_provider(self, callback):
        self._active = callback

    def selected_backend(self):
        return self._backend

    def _fingerprint(self):
        return json.dumps([asdict(cfg.magnet), asdict(cfg.attodry2100), asdict(cfg.lakeshore335)], sort_keys=True, default=str)

    def review_valid(self):
        return self._review is not None and self._review == self._fingerprint()

    @Property(str, notify=changed)
    def backend(self):
        return self._backend

    @Property(bool, notify=changed)
    def reviewed(self):
        return self.review_valid()

    @Property(bool, notify=changed)
    def busy(self):
        return bool(self._closing or self.manualBusy or self._active())

    manualBusy = Property(bool, lambda s: bool(s._pending or s.magnet1000.exclusive_owner or s.magnet2100.has_pending_work), notify=changed)
    pendingOperations = Property('QVariantList', lambda s: sorted(s._pending), notify=changed)
    temperatureAvailable = Property(bool, lambda s: s._connected["temperature"] if s._backend == "1000" else s._connected["2100"] and bool(getattr(getattr(s._snapshots.get("2100"), "capabilities", None), "sample_temperature_control", False)), notify=changed)
    heatingBlockReason = Property(str, lambda s: s.heating_interlock.heating_block_reason() if s._backend == "1000" else "", notify=changed)
    thermalStatus = Property(str, lambda s: f"{s.thermal_safety.state.value}: {s.thermal_safety.reason}", notify=changed)

    @Property(str, notify=changed)
    def temperatureStatus(self):
        if self._backend == "2100":
            snapshot = self._snapshots.get("temperature2100")
            if snapshot is None:
                return "SDK sample temperature unavailable; refresh supported telemetry"
            return (f"Sample: {getattr(snapshot, 'sample_temperature_k', None)} K; "
                    f"VTI: {getattr(snapshot, 'vti_temperature_k', None)} K; "
                    f"target: {getattr(snapshot, 'sample_setpoint_k', None)} K; "
                    f"active: {getattr(snapshot, 'sample_control_active', None)}")
        valid = self._temperature_valid()
        sample = str(getattr(self._snapshots.get("temperature"), "sample_temperature_k", None)) if valid else "unavailable/stale"
        state = ("pending readback" if self._temperature_pending() else "control failed" if self._control_failed
                 else "Stability wait disabled" if not self._values["wait_temperature"]
                 else "ready" if self.is_ready() else "waiting for stability")
        target = self.measurement_settings()[1]
        return f"LS335 sample: {sample} K; target: {target if target is not None else 'unconfirmed'} K; {state}"

    @Property('QVariantMap', notify=changed)
    def telemetry(self):
        result = {}
        for label, key in (("magnet", self._backend), ("temperature", "temperature" if self._backend == "1000" else "temperature2100")):
            value = self._snapshots.get(key)
            data = asdict(value) if is_dataclass(value) else dict(vars(value)) if value is not None and hasattr(value, "__dict__") else value
            result[label] = json.loads(json.dumps(data, default=str)) if data is not None else {}
        result["sample_control"] = dict(self._control_state or {}) if self._backend == "1000" else {}
        return result

    @Slot()
    def clearLog(self):
        self._log.clear()
        self.changed.emit()

    @Property(str, notify=changed)
    def message(self):
        return self._message

    @Property(str, notify=changed)
    def logText(self):
        return "\n".join(self._log)

    @Property(str, notify=changed)
    def reviewDetails(self):
        m, a, t = cfg.magnet, cfg.attodry2100, cfg.lakeshore335
        def number(value):
            return "not configured" if value is None else f"{value:g}"
        return (f"APS100: {m.visa_resource}; coil {m.coil_constant_t_per_a:g} T/A; "
                f"control ±{m.safe_control_max_field_t:g} T; current {m.maximum_current_a:g} A; "
                f"rate {m.maximum_rate_a_per_s:g} A/s; heater warm/cool {m.heater_warm_s:g}/{m.heater_cool_s:g} s.\n"
                f"2100: {a.host}, channel {a.channel}; field ±{number(a.maximum_field_t)} T; "
                f"temperature {number(a.minimum_temperature_k)}–{number(a.maximum_temperature_k)} K.\n"
                f"LS335: {t.visa_resource}; sample channel {t.sample_channel}; "
                f"temperature {t.sample_minimum_temperature_k:g}–{t.sample_maximum_temperature_k:g} K.")

    @Property('QVariantMap', notify=changed)
    def connections(self):
        return dict(self._connected)

    @Property('QVariantList', notify=fieldsChanged)
    def fields(self):
        v = self._values
        maximum = cfg.magnet.safe_control_max_field_t if self._backend == "1000" else min(cfg.attodry2100.maximum_field_t or 6., 6.)
        result = [field("target_field", "Target field (T)", v["target_field"], min=-maximum, max=maximum, group="Magnetic field")]
        if self._backend == "1000":
            result.append(field("final_mode", "Final magnet mode", v["final_mode"], options=["persistent", "driven"], group="Magnetic field"))
        lower = cfg.lakeshore335.sample_minimum_temperature_k if self._backend == "1000" else cfg.attodry2100.minimum_temperature_k or .001
        upper = cfg.lakeshore335.sample_maximum_temperature_k if self._backend == "1000" else cfg.attodry2100.maximum_temperature_k or 300.
        result.append(field("target_temperature", "Sample target (K)", v["target_temperature"], min=lower, max=upper, group="Sample temperature"))
        result.append(field("ramp_rate", "Temperature ramp (K/min)", v["ramp_rate"], min=.001, max=100, group="Sample temperature"))
        if self._backend == "1000":
            result.extend([
                field("heater_range", "Heater range", v["heater_range"], options=[dict(label="Keep current", value=None),
                    dict(label="Auto", value="auto"), dict(label="OFF", value=0), dict(label="Low / ON", value=1),
                    dict(label="Medium", value=2), dict(label="High", value=3)], group="Sample temperature"),
                field("ramp_enabled", "Temperature ramp", v["ramp_enabled"], options=[dict(label="Keep current", value=None),
                    dict(label="Enabled", value=True), dict(label="Disabled", value=False)], group="Sample temperature"),
                field("wait_temperature", "Wait for stable temperature before measurement", v["wait_temperature"], group="Sample temperature"),
            ])
        return result

    @Property('QVariantList', notify=heaterBandFieldsChanged)
    def heaterBandFields(self):
        return [field("heater_bands", "Automatic heater temperature bands", self._bands, kind="rows", rows=self._bands,
            columns=[field("upper_temperature_k", "Upper temperature (K)", 1., min=.001, max=300.),
                     field("heater_range", "Heater range", 0, options=[dict(label="OFF", value=0),
                         dict(label="Low / ON", value=1), dict(label="Medium", value=2), dict(label="High", value=3)])],
            description="Save after review. With no application bands, use the commissioned LS335 Zone table.")]

    @Property(str, notify=changed)
    def heaterAutoPreview(self):
        if self._backend != "1000" or self._values["heater_range"] != "auto":
            return ""
        try:
            if not self._bands:
                return "Auto: use the commissioned LS335 Zone table; verified range is resolved on Apply"
            band = select_heater_range(self._values["target_temperature"], self._bands)
            value = band["heater_range"]
            voltage = (self._control_state or {}).get("output_type") == "voltage"
            if value > cfg.lakeshore335.sample_maximum_heater_range or voltage and value > 1:
                raise ValueError("Selected band exceeds the commissioned output range")
            label = ("OFF", "ON" if voltage else "Low", "Medium", "High")[value]
            return f"Auto: {self._values['target_temperature']:g} K → {label} (band ≤ {band['upper_temperature_k']:g} K)"
        except (ValueError, TypeError, IndexError) as exc:
            return "Auto unavailable: " + str(exc)

    @Slot(str, int, str, 'QVariant')
    def setRowValue(self, key, index, column, value):
        try:
            if self.busy or key != "heater_bands":
                raise RuntimeError("Wait for active operations before editing heater bands")
            spec = next(item for item in self.heaterBandFields[0]["columns"] if item["key"] == column)
            self._bands[index][column] = checked_value(spec, value)
            self.heaterBandFieldsChanged.emit()
            self.changed.emit()
        except (IndexError, StopIteration, TypeError, ValueError, RuntimeError) as exc:
            self._report(str(exc))

    @Slot(str)
    def addRow(self, key):
        if key == "heater_bands" and not self.busy and len(self._bands) < 10:
            upper = self._bands[-1]["upper_temperature_k"] + 1 if self._bands else 5.
            self._bands.append(dict(upper_temperature_k=upper, heater_range=0))
            self.heaterBandFieldsChanged.emit()
            self.changed.emit()

    @Slot(str, int)
    def removeRow(self, key, index):
        if key == "heater_bands" and not self.busy and 0 <= index < len(self._bands):
            self._bands.pop(index)
            self.heaterBandFieldsChanged.emit()
            self.changed.emit()

    @Slot()
    def saveHeaterBands(self):
        previous = cfg.lakeshore335.sample_auto_heater_ranges
        try:
            if self.busy:
                raise RuntimeError("Wait for active operations before changing heater bands")
            bands = validate_heater_ranges(self._bands) if self._bands else []
            if any(row["heater_range"] > cfg.lakeshore335.sample_maximum_heater_range for row in bands):
                raise ValueError("A heater band exceeds the commissioned maximum range")
            cfg.lakeshore335.sample_auto_heater_ranges = bands
            cfg.save()
            self._review = None
            self._report("Heater bands saved. Review the changed configuration before connecting or moving.")
        except Exception as exc:
            cfg.lakeshore335.sample_auto_heater_ranges = previous
            self._report(str(exc))

    @Property(str, notify=changed)
    def summary(self):
        snapshot = self._snapshots.get(self._backend)
        field_t = getattr(snapshot, "field_t", None)
        field_text = f"{field_t:+.6g} T" if isinstance(field_t, (int, float)) and math.isfinite(field_t) else "unavailable"
        temp = self._snapshots.get("temperature" if self._backend == "1000" else "temperature2100")
        value = getattr(temp, "sample_temperature_k", None)
        if self._backend == "1000" and not self._temperature_valid():
            value = None
        temperature = f"{value:.4g} K" if isinstance(value, (int, float)) and math.isfinite(value) else "unavailable"
        state = "connected" if self._connected[self._backend] else "disconnected"
        return f"attoDRY{self._backend} · {state} · B: {field_text} · sample: {temperature}"

    @Slot(str)
    def selectBackend(self, backend):
        if backend not in ("1000", "2100") or self.busy:
            self._report("Wait for active operations before selecting a temperature system")
            return
        self._backend = backend
        self._auto = False
        self._confirmation = None
        self.fieldsChanged.emit()
        self.changed.emit()

    @Slot(bool)
    def acknowledgeReview(self, accepted):
        self._review = self._fingerprint() if accepted else None
        self.changed.emit()

    @Slot(str, 'QVariant')
    def setValue(self, key, value):
        try:
            if self.busy:
                raise RuntimeError("Wait for active operations before editing targets")
            spec = next(s for s in self.fields if s["key"] == key)
            self._values[key] = checked_value(spec, value)
            self._confirmation = None
            self.fieldsChanged.emit()
            self.changed.emit()
        except (StopIteration, ValueError, TypeError, RuntimeError) as exc:
            self._report(str(exc) or "Unknown setting")

    def _guard(self, *, connected=True, review=True):
        if self.offline:
            raise RuntimeError("Offline preview: instrument commands are disabled")
        if review and not self.review_valid():
            raise RuntimeError("Review the configured limits and connection settings first")
        if self.busy:
            raise RuntimeError("Wait for the active instrument operation and measurement cleanup")
        if connected and not self._connected[self._backend]:
            raise RuntimeError("Connect the selected system first")

    @Slot(str)
    def command(self, action):
        try:
            if action in ("stop", "heater_off", "temperature_stop"):
                if self.offline:
                    raise RuntimeError("Offline preview: instrument commands are disabled")
                if action == "stop":
                    self._pending_start_2100 = False
                    self._confirmation = None
                    self.magnet1000.pause() if self._backend == "1000" else self.magnet2100.request_stop()
                elif self._backend == "1000":
                    if not self._connected["temperature"] or "heater_off" in self._pending:
                        return
                    self._submit("heater_off", self.lakeshore335.heater_off)
                else:
                    if not self._connected["2100"] or "stop_temperature" in self._pending:
                        return
                    self._submit("stop_temperature", self.magnet2100.stop_sample_temperature_control_async)
                self._tick()
                self.changed.emit()
                return
            if action == "clear_log":
                self.clearLog()
                return
            self._guard(connected=action not in ("connect", "detect", "connect_temperature", "disconnect_temperature", "temperature"),
                        review=action not in ("refresh", "disconnect", "disconnect_temperature"))
            if action in ("connect", "detect"):
                targets = ("1000", "2100") if action == "detect" else (self._backend,)
                self._auto = action == "detect"
                for backend in targets:
                    if not self._connected[backend]:
                        try:
                            if backend == "1000":
                                self._submit("connect1000", self.magnet1000.connect_instrument, cfg.magnet.visa_resource, use_mock=False)
                            else:
                                self._submit("connect2100", self.magnet2100.connect_async)
                        except Exception as exc:
                            self._report(f"{backend} connection failed: {exc}")
            elif action == "disconnect":
                self.magnet1000.disconnect_instrument() if self._backend == "1000" else self.magnet2100.disconnect_async()
            elif action == "refresh":
                self.magnet1000.refresh_snapshot() if self._backend == "1000" else self.magnet2100.read_snapshot_async()
                if self._backend == "1000" and self._connected["temperature"]:
                    self.lakeshore335.refresh_snapshot()
                    self.lakeshore335.refresh_control_configuration()
                elif self._backend == "2100":
                    self._poll_2100_temperature(force=True)
            elif action == "connect_temperature":
                if self._backend != "1000":
                    raise RuntimeError("LS335 belongs to the attoDRY1000 system")
                self._submit("connecttemperature", self.lakeshore335.connect_instrument, use_mock=False)
            elif action == "disconnect_temperature":
                if self._backend != "1000":
                    raise RuntimeError("LS335 belongs to the attoDRY1000 system")
                self.lakeshore335.disconnect_instrument()
            elif action == "move":
                self._move()
            elif action == "temperature":
                self._apply_temperature()
            else:
                raise ValueError("Unknown magnet command")
            self.changed.emit()
        except Exception as exc:
            self._report(str(exc))

    def _submit(self, operation, function, *args, **kwargs):
        self._pending.add(operation)
        self.changed.emit()
        try:
            return function(*args, **kwargs)
        except Exception:
            self._pending.discard(operation)
            if operation in ("sample_control", "heater_off"):
                self._control_failed = True
                self._control_state = None
                self._stable_since = None
            self.changed.emit()
            raise

    def _persistent_observation(self):
        snapshot = self._snapshots.get("1000")
        try:
            value = float(snapshot.field_t)
            age = time.monotonic() - float(snapshot.monotonic_s)
            maximum = float(cfg.lakeshore335.maximum_field_reading_age_s)
            if not math.isfinite(value) or not math.isfinite(maximum) or maximum <= 0 or not 0 <= age <= maximum:
                raise ValueError
            return round(value, 6), snapshot.heater_on
        except (AttributeError, TypeError, ValueError):
            raise RuntimeError("Magnet field telemetry is unavailable or stale; refresh before confirming a move")

    def _move(self, confirmed=False):
        v = self._values
        checked_value(next(item for item in self.fields if item["key"] == "target_field"), v["target_field"])
        if self._backend == "1000":
            snapshot = self._snapshots.get("1000")
            heater = getattr(snapshot, "heater_on", None)
            if not isinstance(heater, bool):
                raise RuntimeError("Heater state is unknown; refresh telemetry before moving")
            if not confirmed and (not heater or v["final_mode"] == "persistent"):
                token = uuid.uuid4().hex
                self._confirmation = (token, dict(v), self._fingerprint(), self._persistent_observation())
                current = getattr(snapshot, "field_t", None)
                text = (f"Current / stored field: {current} T.\nTarget field: {v['target_field']:g} T.\n"
                        f"Final mode: {v['final_mode']}.\nHeater currently: {'ON' if heater else 'OFF'}.\n"
                        "Confirm the stored field and polarity. The supply matches persistent leads before heater ON. "
                        + ("Completion turns the heater OFF, observes cooling, then zeros supply leads."
                           if v['final_mode'] == "persistent" else "Completion leaves the heater ON with the supply driving the field."))
                self.confirmationRequested.emit(token, "Confirm persistent-field move", text)
                return
            self._submit("move1000", self.magnet1000.safe_move_to_field, v["target_field"], final_mode=v["final_mode"],
                                               zero_leads=True, persistent_field_confirmed=confirmed)
        else:
            self._pending_start_2100 = True
            try:
                self._submit("move2100", self.magnet2100.set_h_setpoint_async, v["target_field"])
            except Exception:
                self._pending_start_2100 = False
                raise

    @Slot(str, bool)
    def confirm(self, token, accepted):
        pending, self._confirmation = self._confirmation, None
        if not pending or token != pending[0] or not accepted:
            return
        try:
            self._guard()
            if self._backend != "1000" or pending[1] != self._values or pending[2] != self._fingerprint():
                raise RuntimeError("Settings changed; review the move again")
            if pending[3] != self._persistent_observation():
                raise RuntimeError("Displayed field or heater state changed; review the persistent-field move again")
            self._move(True)
        except Exception as exc:
            self._report(str(exc))

    def _apply_temperature(self):
        v = self._values
        checked_value(next(item for item in self.fields if item["key"] == "target_temperature"), v["target_temperature"])
        checked_value(next(item for item in self.fields if item["key"] == "ramp_rate"), v["ramp_rate"])
        self._stable_since = None
        self._control_failed = False
        if self._backend == "1000":
            if not self._connected["temperature"]:
                raise RuntimeError("Connect LS335 before changing sample temperature")
            self._submit("sample_control", self.lakeshore335.set_sample_control, v["target_temperature"], heater_range=v["heater_range"],
                ramp_enabled=v["ramp_enabled"], ramp_rate_k_per_min=v["ramp_rate"])
        else:
            if not self.temperatureAvailable:
                raise RuntimeError("Connected SDK does not report sample temperature control capability; refresh first")
            self._submit("configure_temperature", self.magnet2100.configure_sample_temperature_async, v["target_temperature"], v["ramp_rate"])

    def _connection(self, name, connected):
        self._connected[name] = connected
        if connected:
            self._pending.discard("connect" + name)
            if name == "2100":
                self.magnet2100.set_polling_enabled(True)
                self._last_temperature_poll = 0.
        if not connected:
            self._snapshots.pop(name, None)
            if name == "temperature":
                self.thermal_safety.latest_snapshot = None
                self.thermal_safety.evaluate(None)
                self._stable_since = None
                self._control_state = None
                self._target = None
                self._controls_seen = False
                self._control_failed = False
                self._pending.difference_update(("sample_control", "heater_off"))
            elif name == "1000":
                self.thermal_safety.note_magnet_snapshot(None)
            elif name == "2100":
                self._snapshots.pop("temperature2100", None)
                self._pending_start_2100 = False
                self._pending.difference_update(("move2100", "configure_temperature", "stop_temperature"))
        if self._auto and not self.manualBusy and not self._active():
            connected_names = [key for key in ("1000", "2100") if self._connected[key]]
            if len(connected_names) == 1:
                if self._backend != connected_names[0]:
                    self._backend = connected_names[0]
                    self.fieldsChanged.emit()
            elif len(connected_names) == 2:
                self._report("Both systems are online; select the intended system")
        self.changed.emit()

    def _snapshot(self, name, snapshot):
        self._snapshots[name] = snapshot
        self.changed.emit()

    def _temperature_snapshot(self, snapshot):
        self._snapshots["temperature"] = snapshot
        self.thermal_safety.evaluate(snapshot)
        if self._temperature_valid() and self._target is not None and not self._control_failed and not self._temperature_pending():
            if abs(snapshot.sample_temperature_k - self._target) <= cfg.lakeshore335.sample_stability_tolerance_k:
                self._stable_since = self._stable_since or time.monotonic()
            else:
                self._stable_since = None
        else:
            self._stable_since = None
        self.changed.emit()

    def _temperature_pending(self):
        return bool(self._pending.intersection(("sample_control", "heater_off", "configure_temperature")))

    def _temperature_valid(self):
        snapshot = self._snapshots.get("temperature")
        if snapshot is None or not getattr(snapshot, "connected", False) or not getattr(snapshot, "communication_valid", False):
            return False
        try:
            t = float(snapshot.sample_temperature_k)
            status_clear = _sensor_status_is_clear(snapshot.sample_sensor_status)
            age = time.monotonic() - float(snapshot.monotonic_s)
            maximum = float(cfg.lakeshore335.maximum_reading_age_s)
            return math.isfinite(t) and t > 0 and status_clear and math.isfinite(maximum) and maximum > 0 and 0 <= age <= maximum
        except (AttributeError, TypeError, ValueError):
            return False

    def _control_result(self, result):
        operation = result.get("operation")
        if operation not in {"sample_control", "setpoint", "read_control", "heater_off"}:
            return
        self._pending.discard(operation)
        self._controls_seen = True
        if not result.get("ok"):
            self._control_state = None
            self._stable_since = None
            if operation != "read_control":
                self._control_failed = True
            self._report("Temperature operation unconfirmed: " + str(result.get("error", "readback failed")))
        elif operation == "heater_off":
            self._control_state = None
            self._stable_since = None
            self._control_failed = False
            self._report("Heater OFF — confirmed by readback")
        else:
            try:
                setpoint = float(result["setpoint"])
                if not math.isfinite(setpoint) or setpoint <= 0:
                    raise ValueError("invalid setpoint")
            except (ValueError, TypeError, KeyError):
                self._control_state = None
                self._control_failed = True
                self._stable_since = None
                self._report("Temperature operation unconfirmed: invalid setpoint readback")
                return
            self._control_state = result
            if not self._temperature_pending() and (operation != "read_control" or not self._control_failed):
                target = float(result["setpoint"])
                if target != self._target:
                    self._stable_since = None
                self._target = target
            if operation != "read_control":
                self._control_failed = False
                self._stable_since = None
            self._report(f"Sample setpoint verified: {result['setpoint']:g} K")
        self._notify_readiness()

    def is_ready(self):
        if self._backend != "1000" or not self._values["wait_temperature"]:
            return True
        return bool(not self._temperature_pending() and not self._control_failed
                    and self._control_state is not None and self._temperature_valid()
                    and self._target is not None and self._stable_since is not None
                    and abs(self._snapshots["temperature"].sample_temperature_k - self._target) <= cfg.lakeshore335.sample_stability_tolerance_k
                    and time.monotonic() - self._stable_since >= cfg.lakeshore335.sample_stability_dwell_s)

    def measurement_settings(self):
        return self._values["wait_temperature"], self._target if not self._control_failed and not self._temperature_pending() and self._control_state is not None else None

    def _tick(self):
        heating_block = self.heatingBlockReason
        if heating_block != self._last_heating_block:
            self._last_heating_block = heating_block
            self.changed.emit()
        self.thermal_safety.evaluate(self._snapshots.get("temperature"))
        if not self._temperature_valid():
            self._stable_since = None
        self._notify_readiness()
        if not self.offline and self._connected["2100"] and self._backend == "2100" and not self._closing:
            self._poll_2100_temperature()
        if self._auto and not self.manualBusy and not self._active():
            online = [key for key in ("1000", "2100") if self._connected[key]]
            if len(online) == 1 and self._backend != online[0]:
                self._backend = online[0]
                self.fieldsChanged.emit()
                self.changed.emit()
        if self._review is not None and not self.review_valid():
            self._review = None
            # Commissioned field and temperature limits may have changed.
            self.fieldsChanged.emit()
            self.changed.emit()

    def _notify_readiness(self):
        ready = self.is_ready()
        if ready != self._last_ready:
            self._last_ready = ready
            self.readinessChanged.emit(ready)
            self.changed.emit()

    def _poll_2100_temperature(self, force=False):
        capabilities = getattr(self._snapshots.get("2100"), "capabilities", None)
        if not getattr(capabilities, "sample_temperature_readback", False) or self.magnet2100.has_pending_work and not force:
            return
        now = time.monotonic()
        if not force and now - self._last_temperature_poll < 2.:
            return
        try:
            self._last_temperature_poll = now
            self.magnet2100.read_temperature_snapshot_async()
        except Exception as exc:
            self._report("SDK temperature refresh failed: " + str(exc))

    def _operation1000(self, name):
        if name != "safe_move":
            self._report("APS100: " + str(name))

    def _move_result(self, result):
        self._pending.discard("move1000")
        self._report("Magnet move complete" if result.get("success") else str(result.get("error", "Move failed")))

    def _operation2100(self, name, success, error):
        if name == "setpoint":
            start, self._pending_start_2100 = success and self._pending_start_2100, False
            if start:
                try:
                    self.magnet2100.start_field_control_async()
                except Exception as exc:
                    self._pending.discard("move2100")
                    self._report("SDK Start was not issued: " + str(exc))
                return
            self._pending.discard("move2100")
        if name in ("start", "stop", "disconnect", "shutdown"):
            self._pending.discard("move2100")
        self._pending.discard(name)
        if name == "connect":
            self._pending.discard("connect2100")
        self._report(f"2100 {name}: " + ("complete" if success else str(error)))

    def _error(self, name, message):
        self._pending.discard("connect" + name)
        if name == "temperature":
            self._snapshots.pop("temperature", None)
            self._stable_since = None
            self._control_failed = True
            self._control_state = None
            self.thermal_safety.latest_snapshot = None
            self.thermal_safety.evaluate(None)
        elif name == "1000":
            self._snapshots.pop("1000", None)
            self._confirmation = None
            self.thermal_safety.note_magnet_snapshot(None)
            self.thermal_safety.evaluate(None)
        elif name == "2100":
            self._snapshots.pop("2100", None)
            self._snapshots.pop("temperature2100", None)
        self._notify_readiness()
        self._report(f"{name}: {message}")

    def _report(self, message):
        self._message = str(message)
        self._log.append(time.strftime("%H:%M:%S ") + self._message)
        self._log = self._log[-500:]
        self.changed.emit()
        self.messageRaised.emit(self._message)

    def experiment_context_snapshot(self):
        result = dict(source="cached UI telemetry", scope="run-start conditions; not per-point telemetry", magnet_backend=self._backend)
        for key, name in (("magnet1000", "1000"), ("magnet2100", "2100"), ("temperature", "temperature"), ("temperature2100", "temperature2100")):
            value = self._snapshots.get(name)
            data = asdict(value) if is_dataclass(value) else deepcopy(value) if isinstance(value, dict) else None
            age = time.monotonic() - value.monotonic_s if value is not None and hasattr(value, "monotonic_s") else None
            result[key] = dict(available=data is not None, values=data, age_s=age)
        return result

    def request_stop(self):
        self._pending_start_2100 = False
        self._confirmation = None
        if self.offline:
            return
        if self._connected["1000"]:
            self.magnet1000.pause()
        if self._connected["2100"]:
            self.magnet2100.request_stop()

    def beginShutdown(self):
        self._closing = True
        self._timer.stop()
        self._confirmation = None
        self._pending_start_2100 = False
        self.changed.emit()

    def cancelShutdown(self):
        self._closing = False
        self._timer.start()
        self.changed.emit()

    def close(self):
        self._closing = True
        self._confirmation = None
        self._timer.stop()
        self.magnet1000.shutdown()
        self.lakeshore335.shutdown()
        return self.magnet2100.shutdown()
