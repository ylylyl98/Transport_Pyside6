"""Asynchronous lock-in presenter using the existing verified control protocol."""
from __future__ import annotations

from copy import deepcopy
import json
import math
import threading

from PySide6.QtCore import QObject, Property, Signal, Slot

from app.engine.transport_tasks import TaskLane
from app.experiment_metadata import timestamp
from app.lockin_diagnostics import save_control_log
from app.settings import get_app_settings
from app.quick.forms import field, checked_value
from instruments.SR830 import (LOCKIN_PROFILES, SENSITIVITY_LABELS, TIME_CONSTANT_LABELS,
    FILTER_SLOPE_LABELS, INPUT_COUPLING_LABELS, INPUT_GROUND_LABELS, LINE_FILTER_LABELS)
from instruments.srs_control import AUTO_COMMANDS


DEFAULTS = dict(sensitivity=18, time_constant=10, reserve=1, filter_slope=1, ref_source=1,
                input_config=0, current_gain=0, input_coupling=0, input_ground=0, line_filter=0,
                phase_deg=0., frequency_hz=1000., sine_out_v=.004, harmonic=1, reserve_level=0)


class LockinService(QObject):
    changed = Signal()
    fieldsChanged = Signal()
    messageRaised = Signal(str)
    verified = Signal(object)
    sensitivityInvalidated = Signal(str)
    stopForSettingsRequested = Signal()

    def __init__(self, manager, name="lockin", parent=None, *, offline=False, settings=None):
        super().__init__(parent)
        self.manager, self.name, self.offline = manager, name, offline
        self.settings = settings or get_app_settings()
        self._profile = dict(LOCKIN_PROFILES["SR830"])
        self._values = dict(DEFAULTS)
        self._dirty = set()
        self._data = {}
        self._verified_settings = {}
        self._readback_valid = False
        self._session_identity = None
        self._active = lambda: False
        self._closing = False
        self._busy = False
        self._message = "Connect and refresh the lock-in to verify its settings"
        self._cancel = threading.Event()
        self._lane = TaskLane("quick-" + name, self)
        self._load()
        manager.status_changed.connect(self._status)
        manager.resources_changed.connect(lambda *_: self.changed.emit())
        manager.operation_changed.connect(lambda *_: self.changed.emit())
        manager.gate_currents_read.connect(lambda *_: self.changed.emit())

    def _key(self, key):
        address = getattr(self.manager.connections, "lockin" if self.name == "lockin" else "drive_lockin")
        return f"{self.name}/{self._profile['model']}/{address}/{key}"

    def _load(self):
        for key, default in DEFAULTS.items():
            if key == "ref_source":
                default = self._profile["internal_reference_code"]
            model_specific = {"ref_source", "reserve", "input_config", "current_gain"}
            fallback = default if key in model_specific else self.settings.value(f"{self.name}/{key}", default)
            fallback = self.settings.value(f"{self.name}/{self._profile['model']}/{key}", fallback)
            try:
                self._values[key] = type(default)(self.settings.value(self._key(key), fallback))
                if isinstance(self._values[key], float) and not math.isfinite(self._values[key]):
                    raise ValueError("Nonfinite saved setting")
            except (TypeError, ValueError, OverflowError):
                self._values[key] = default
        self._dirty.clear()

    def set_active_provider(self, callback):
        self._active = callback

    @Property(bool, notify=changed)
    def busy(self):
        return self._busy

    @Property(bool, notify=changed)
    def available(self):
        return bool(not self.offline and not self._closing and not self._active() and not self._busy and self.manager.is_connected(self.name)
                    and not self.manager.is_busy() and not self.manager.is_in_use(self.name))

    @Property(str, notify=changed)
    def message(self):
        return self._message

    @Property(str, notify=changed)
    def identity(self):
        return str(self._data.get("identity", self._profile["model"]))

    @Property('QVariantMap', notify=changed)
    def readouts(self):
        return dict(self._data.get("outputs", {}))

    @Property('QVariantMap', notify=changed)
    def status(self):
        return dict(self._data.get("status", {}))

    pendingFields = Property('QVariantList', lambda s: sorted(s._dirty), notify=changed)
    pendingText = Property(str, lambda s: "Pending edits: " + ", ".join(sorted(s._dirty)) if s._dirty else "No pending edits", notify=changed)
    display = Property('QVariantMap', lambda s: dict(s._data.get("display", {})), notify=changed)
    diagnostics = Property(str, lambda s: json.dumps(s._data, ensure_ascii=False, indent=2, default=str), notify=changed)
    supportsDisplay = Property(bool, lambda s: s._profile["model"] == "SR850", notify=changed)
    displayAvailable = Property(bool, lambda s: s.available and s.supportsDisplay and bool(s.display.get("can_scale")), notify=changed)
    autoGainAvailable = Property(bool, lambda s: s.available and not (s._profile["model"] == "SR830" and s._verified_settings.get("time_constant", 0) > 10), notify=changed)
    lockedBySweep = Property(bool, lambda s: not s._busy and bool(s._active() or s.manager.is_in_use(s.name)), notify=changed)
    readbackValid = Property(bool, lambda s: s._readback_valid, notify=changed)

    @Slot()
    def requestStopForSettings(self):
        if self.lockedBySweep:
            self.stopForSettingsRequested.emit()
            self._report("Safe stop requested; wait for measurement cleanup before editing settings")

    @Slot()
    def discardPending(self):
        if self._busy or self._closing or self._active() or self.manager.is_in_use(self.name):
            self._report("Wait for measurement cleanup before discarding pending settings")
            return
        self._dirty.clear()
        self._values.update({k: v for k, v in self._verified_settings.items() if k in self._values})
        for key, value in self._values.items():
            self.settings.setValue(self._key(key), value)
        self.fieldsChanged.emit()
        self._report("Pending edits discarded; displayed values restored from last instrument readback")

    @Property('QVariantList', notify=fieldsChanged)
    def fields(self):
        p = self._profile
        choices = dict(sensitivity=SENSITIVITY_LABELS, time_constant=TIME_CONSTANT_LABELS,
                       reserve=p["reserve_labels"], filter_slope=FILTER_SLOPE_LABELS,
                       ref_source=p["reference_source_labels"], input_config=p["input_config_labels"],
                       input_coupling=INPUT_COUPLING_LABELS, input_ground=INPUT_GROUND_LABELS,
                       line_filter=LINE_FILTER_LABELS)
        if p.get("current_gain_labels") and self._values["input_config"] == 2:
            choices["current_gain"] = p["current_gain_labels"]
        labels = dict(sensitivity="Sensitivity", time_constant="Time constant", reserve="Reserve", filter_slope="Filter slope",
                      ref_source="Reference", input_config="Input", input_coupling="Coupling", input_ground="Ground",
                      line_filter="Line filter", current_gain="Current gain")
        groups = dict(input_config="Input", current_gain="Input", input_coupling="Input", input_ground="Input",
                      sensitivity="Input", reserve="Filter", time_constant="Filter", filter_slope="Filter",
                      line_filter="Filter", ref_source="Reference")
        order = ("input_config", "current_gain", "input_coupling", "input_ground", "sensitivity",
                 "time_constant", "filter_slope", "line_filter", "reserve", "ref_source")
        result = [field(key, labels[key], self._values[key], options=[dict(label=label, value=i) for i, label in enumerate(choices[key])],
                        group=groups[key]) for key in order if key in choices]
        result.extend([
            field("phase_deg", "Phase (°)", self._values["phase_deg"], min=p["phase_min"], max=p["phase_max"], group="Reference"),
            field("frequency_hz", "Frequency (Hz)", self._values["frequency_hz"], min=.001, max=102000., group="Reference",
                  readOnly=self._values["ref_source"] != p["internal_reference_code"]),
            field("harmonic", "Harmonic", self._values["harmonic"], min=1, max=p["harmonic_max"], group="Reference"),
            field("sine_out_v", "Sine output (V)", self._values["sine_out_v"], min=.004, max=5., group="Output"),
        ])
        if p["model"] == "SR850" and self._values["reserve"] == 1:
            result.append(field("reserve_level", "Manual reserve level", self._values["reserve_level"], min=0, max=5, group="Filter"))
        return result

    @Slot(str, 'QVariant')
    def setValue(self, key, value):
        try:
            if self._closing or self._active() or self._busy or self.manager.is_busy() or self.manager.is_in_use(self.name):
                raise RuntimeError("Stop the measurement and wait for cleanup before editing lock-in settings")
            spec = next(s for s in self.fields if s["key"] == key)
            if spec.get("readOnly"):
                raise ValueError("This setting is read-only in the selected reference mode")
            value = checked_value(spec, value)
            self._values[key] = value
            if value == self._verified_settings.get(key):
                self._dirty.discard(key)
            else:
                self._dirty.add(key)
            self.settings.setValue(self._key(key), value)
            self.fieldsChanged.emit()
            self._report("Edited settings are pending; Apply verifies instrument readback")
        except (ValueError, TypeError, RuntimeError, StopIteration) as exc:
            self._report(str(exc) or "Unknown lock-in setting")

    @Slot(str)
    def command(self, action):
        self.run_action(action)

    @Slot(float, float)
    def applyDisplay(self, scale, center):
        self.run_action("apply_display", dict(scale=scale, center=center, pane=self._data.get("display", {}).get("pane")))

    def run_action(self, action, payload=None):
        claimed = False
        try:
            if action not in ("refresh", "apply", "apply_display", *AUTO_COMMANDS):
                raise ValueError("Unknown lock-in action")
            if not self.available:
                raise RuntimeError("Lock-in unavailable or owned by an active measurement")
            if action == "auto_gain" and not self.autoGainAvailable:
                raise RuntimeError("SR830 Auto Gain requires time constant <= 1 s")
            if action in ("auto_scale", "apply_display") and not self.displayAvailable:
                raise RuntimeError("Display scaling requires an SR850 with an active Bar or Chart display; refresh first")
            if action == "apply_display":
                payload = dict(payload or {})
                scale, center = float(payload["scale"]), float(payload["center"])
                if not 1e-18 < scale < 1e18 or not math.isfinite(center):
                    raise ValueError("Display scale must be between 1e-18 and 1e18 and center must be finite")
            if action == "apply":
                specs = {spec["key"]: spec for spec in self.fields if not spec.get("readOnly")}
                payload = {key: checked_value(specs[key], self._values[key]) for key in self._dirty if key in specs}
                if not payload:
                    self._report("No applicable pending edits. Refresh to read the instrument")
                    return
            ok, blocked = self.manager.mark_in_use([self.name])
            if not ok:
                raise RuntimeError("Waiting for " + ", ".join(blocked))
            claimed = True
            self._busy = True
            self._cancel.clear()
            session = self.manager.get_session(self.name)
            payload = dict(payload or {})
            def work():
                data = session.run_control(action, payload, cancelled=self._cancel.is_set)
                try:
                    data.setdefault("control", {})["log_path"] = save_control_log(data)
                except OSError as exc:
                    data["control"]["log_error"] = str(exc)
                return data
            self._report("Working — awaiting verified readback")
            self._readback_valid = False
            self.sensitivityInvalidated.emit("Reading; unverified until readback completes")
            self._lane.submit(work, self._done)
        except Exception as exc:
            if claimed:
                self._busy = False
                self.manager.release([self.name])
            self._report(str(exc))

    def _done(self, data, error):
        self._busy = False
        try:
            if error:
                self._readback_valid = False
                self.sensitivityInvalidated.emit("Read failed; refresh required")
                self._report("Lock-in readback failed: " + str(error))
                return
            self._data = data
            model = (data.get("capabilities") or {}).get("model")
            if model in LOCKIN_PROFILES:
                self._profile = dict(LOCKIN_PROFILES[model])
            values = data.get("settings", {})
            control = data.get("control", {})
            self._verified_settings.update(values)
            for key in control.get("verified_keys", []):
                if key in values:
                    self._dirty.discard(key)
            for key, value in values.items():
                if key in self._values and key not in self._dirty:
                    self._values[key] = value
                    self.settings.setValue(self._key(key), value)
            self.fieldsChanged.emit()
            session = self.manager.get_session(self.name)
            self._readback_valid = bool(values) and not getattr(session, "_control_state_uncertain", False)
            if self._readback_valid:
                self.verified.emit(deepcopy(data))
            else:
                self.sensitivityInvalidated.emit("Read failed; instrument settings remain unverified")
            self._report("Settings verified by readback" if control.get("ok", False)
                         else "Operation unconfirmed: " + str(control.get("error", "refresh required")))
        finally:
            self.manager.release([self.name])
            self.changed.emit()

    def _status(self, name, state, _detail):
        if name != self.name:
            return
        if state == "ok" and self.manager.is_connected(name):
            session = self.manager.get_session(name)
            profile = getattr(session, "capabilities", None)
            address = getattr(self.manager.connections, "lockin" if self.name == "lockin" else "drive_lockin")
            context = ((profile or {}).get("model"), address, id(session))
            if profile and context != self._session_identity:
                self._profile = dict(LOCKIN_PROFILES.get(profile.get("model"), self._profile))
                self._load()
                self._session_identity = context
                self.fieldsChanged.emit()
        else:
            self._data = {}
            self._verified_settings.clear()
            self._readback_valid = False
            self._dirty.clear()
            self._session_identity = None
        self.changed.emit()

    def _report(self, message):
        self._message = str(message)
        self.changed.emit()
        self.messageRaised.emit(self._message)

    def settings_snapshot(self):
        values = {spec["key"]: self._values[spec["key"]] for spec in self.fields}
        for spec in self.fields:
            if spec["kind"] == "choice":
                selected = next((item for item in spec["options"] if item["value"] == values[spec["key"]]), None)
                if selected:
                    values[spec["key"] + "_label"] = selected["label"]
        return dict(source="saved/manual", captured_at=timestamp(), model=self._profile["model"],
                    address=getattr(self.manager.connections, "lockin" if self.name == "lockin" else "drive_lockin"),
                    scope="displayed front-panel settings; not instrument-verified", values=values)

    def close(self):
        self._closing = True
        self._cancel.set()
        self._lane.close()
