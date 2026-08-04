from __future__ import annotations

import math
import time
from threading import Lock
from typing import Dict, Iterable, List, Optional, Set

from PyQt6 import QtCore

from app.keithley_modes import KEITHLEY_MODE_LABELS, KEITHLEY_MODE_OHM_4W, KEITHLEY_MODE_VOLTAGE_2W, keithley_mode_label
from app.models import Connections
from app.utils import _safe
from instruments import DaqCard, Keithley2400OhmMode, Keithley2400VoltMode, SP2300, SRSLockin


def _read_keithley_protection(session: object) -> dict[str, object]:
    if not hasattr(session, "read_protection_settings"):
        return {}
    result = session.read_protection_settings()
    return dict(result) if isinstance(result, dict) else {}


def _compliance_trip_detail(name: str, readback: dict[str, object]) -> str:
    if readback.get("current_compliance_tripped") is not True:
        return ""
    try:
        measured = abs(float(readback["current"]))
        limit = abs(float(readback["current_compliance_a"]))
    except (KeyError, TypeError, ValueError):
        return f"{name.upper()}: current compliance reached"
    ratio_text = f" ({measured / limit:.3g}x the limit)" if limit > 0.0 else ""
    return (
        f"{name.upper()}: current compliance reached; measured {measured:.3g} A; "
        f"instrument compliance {limit:.3g} A{ratio_text}"
    )


class ConnectWorker(QtCore.QThread):
    status_changed = QtCore.pyqtSignal(str, str, str)
    finished_ok = QtCore.pyqtSignal()
    failed = QtCore.pyqtSignal(str)

    def __init__(self, manager: "DeviceManager"):
        super().__init__()
        self.manager = manager

    def run(self):
        try:
            self.manager._connect_all_in_thread(self.status_changed)
            self.finished_ok.emit()
        except Exception as ex:
            self.failed.emit(str(ex))


class DisconnectWorker(QtCore.QThread):
    status_changed = QtCore.pyqtSignal(str, str, str)
    finished_ok = QtCore.pyqtSignal()
    failed = QtCore.pyqtSignal(str)

    def __init__(self, manager: "DeviceManager"):
        super().__init__()
        self.manager = manager

    def run(self):
        try:
            self.manager._disconnect_all_in_thread(self.status_changed)
            self.finished_ok.emit()
        except Exception as ex:
            self.failed.emit(str(ex))


class ManualControlWorker(QtCore.QThread):
    """Perform one manual hardware operation without blocking the Qt UI."""

    def __init__(self, name: str, session: object, target: float, parent=None):
        super().__init__(parent)
        self.name = name
        self.session = session
        self._target = float(target)
        self._target_lock = Lock()
        self.success = False
        self.message = ""
        self.gate_readback: dict[str, object] | None = None
        self.daq_readback: dict[str, float] | None = None
        self._cancel_requested = False

    @property
    def target(self) -> float:
        with self._target_lock:
            return self._target

    def update_target(self, target: float):
        """Replace the destination of an active manual gate ramp."""
        with self._target_lock:
            self._target = float(target)

    def request_cancel(self):
        self._cancel_requested = True

    def _check_cancelled(self):
        if self._cancel_requested:
            raise RuntimeError("Manual operation cancelled for emergency stop.")

    def run(self):
        try:
            if self.name in {"g1", "g2", "g3"}:
                from app.constants import (
                    GATE_BIAS_RAMP_STEP_T,
                    GATE_BIAS_RAMP_STEP_V,
                    SAFE_RAMP_STEP_T,
                    SAFE_RAMP_STEP_V,
                )
                # Query the source directly so the ramp begins at the present
                # programmed level even if the software cache is stale.
                start = float(self.session.get_voltage_setpoint())
                current = start
                set_voltage = getattr(self.session, "set_voltage_fast", None)
                if set_voltage is None:
                    set_voltage = self.session.set_voltage

                # Behave like a Keithley front-panel adjustment: button presses
                # replace the requested destination while this loop is active.
                # Only the final programmed setpoint is queried; measured
                # voltage/current and compliance remain explicit Read actions.
                while True:
                    self._check_cancelled()
                    requested = self.target
                    if not math.isclose(current, requested, abs_tol=1e-9):
                        zeroing = math.isclose(requested, 0.0, abs_tol=1e-9)
                        step_v, step_t = (
                            (SAFE_RAMP_STEP_V, SAFE_RAMP_STEP_T)
                            if zeroing
                            else (GATE_BIAS_RAMP_STEP_V, GATE_BIAS_RAMP_STEP_T)
                        )
                        direction = 1.0 if requested > current else -1.0
                        current = round(
                            current + direction * min(step_v, abs(requested - current)),
                            9,
                        )
                        set_voltage(current)
                        time.sleep(step_t)
                        continue

                    # Give rapid clicks one ramp interval to coalesce before the
                    # final lightweight setpoint confirmation query.
                    time.sleep(GATE_BIAS_RAMP_STEP_T)
                    if not math.isclose(self.target, requested, abs_tol=1e-9):
                        continue
                    confirmed = float(self.session.get_voltage_setpoint())
                    if not math.isclose(self.target, confirmed, abs_tol=1e-9):
                        current = confirmed
                        continue
                    current = confirmed
                    break

                final_target = self.target
                self.gate_readback = {
                    "connected": True,
                    "mode": KEITHLEY_MODE_VOLTAGE_2W,
                    "set_voltage": current,
                    "error": "",
                }
                action = (
                    "safely ramped to 0 V"
                    if abs(final_target) < 1e-9
                    else f"ramped to {final_target:g} V"
                )
                self.message = f"{self.name.upper()} {action} from {start:g} V."
            elif self.name.startswith("daq_ao"):
                from app.constants import SAFE_RAMP_STEP_T, SAFE_RAMP_STEP_V
                from app.utils import safe_ramp

                ao_index = int(self.name.removeprefix("daq_ao"))
                if hasattr(self.session, "adopt_measured_output_as_ramp_start"):
                    start = float(self.session.adopt_measured_output_as_ramp_start(ao_index))
                else:
                    start = float(self.session.get_ao_value(ao_index))
                safe_ramp(
                    lambda value: self.session.set_voltage(ao_index, value),
                    start,
                    self.target,
                    SAFE_RAMP_STEP_V,
                    SAFE_RAMP_STEP_T,
                    check_fn=self._check_cancelled,
                )
                self.session.acquire()
                self.daq_readback = self.session.get_ao_state(ao_index)
                action = "safely ramped to 0 V" if abs(self.target) < 1e-9 else f"ramped to {self.target:g} V"
                self.message = (
                    f"DAQ AO{ao_index} {action} from {start:g} V; other AO channels were unchanged."
                )
            else:
                self._check_cancelled()
                self.session.set_wavelength(self.target)
                self.message = f"Monochromator moved to {self.target:g} nm."
            self.success = True
        except Exception as ex:
            if self.name in {"g1", "g2", "g3"}:
                try:
                    confirmed = float(self.session.get_voltage_setpoint())
                except Exception:
                    confirmed = None
                self.gate_readback = {
                    "connected": True,
                    "mode": KEITHLEY_MODE_VOLTAGE_2W,
                    "set_voltage": confirmed,
                    "error": str(ex),
                }
            self.message = f"{self.name.upper()} manual control failed: {ex}"


class GateCurrentReadWorker(QtCore.QThread):
    """Read connected gate voltage/current state without blocking the Qt UI."""

    def __init__(self, sessions: dict, modes: dict, parent=None, quiet: bool = False, names=None):
        super().__init__(parent)
        self.sessions = dict(sessions)
        self.modes = dict(modes)
        self.quiet = bool(quiet)
        self.names = tuple(names or ("g1", "g2", "g3"))
        self.readbacks: dict[str, dict[str, object]] = {}
        self.message = ""

    def run(self):
        failures: list[str] = []
        warnings: list[str] = []
        for name in self.names:
            session = self.sessions.get(name)
            mode = self.modes.get(name, "")
            if session is None:
                self.readbacks[name] = {
                    "connected": False,
                    "mode": mode,
                    "set_voltage": None,
                    "measured_voltage": None,
                    "current": None,
                    "error": "",
                }
                continue
            if mode != KEITHLEY_MODE_VOLTAGE_2W:
                self.readbacks[name] = {
                    "connected": True,
                    "mode": mode,
                    "set_voltage": None,
                    "measured_voltage": None,
                    "current": None,
                    "error": "",
                }
                continue
            try:
                set_voltage = session.get_voltage_setpoint()
                readings = session.acquire()
                measured_voltage = readings.get("voltage") if isinstance(readings, dict) else None
                current = readings.get("current") if isinstance(readings, dict) else None
                if measured_voltage is None:
                    measured_voltage = getattr(session, "voltage", None)
                if current is None:
                    current = getattr(session, "current", None)
                self.readbacks[name] = {
                    "connected": True,
                    "mode": mode,
                    "set_voltage": None if set_voltage is None else float(set_voltage),
                    "measured_voltage": None if measured_voltage is None else float(measured_voltage),
                    "current": None if current is None else float(current),
                    "error": "",
                }
                self.readbacks[name].update(_read_keithley_protection(session))
                trip_detail = _compliance_trip_detail(name, self.readbacks[name])
                if trip_detail:
                    warnings.append(trip_detail)
            except Exception as ex:
                self.readbacks[name] = {
                    "connected": True,
                    "mode": mode,
                    "set_voltage": None,
                    "measured_voltage": None,
                    "current": None,
                    "error": str(ex),
                }
                failures.append(f"{name.upper()}: {ex}")
        self.message = "Gate readback refresh complete."
        if failures:
            self.message += " Unavailable: " + "; ".join(failures)
        if warnings:
            self.message += " Compliance status: " + "; ".join(warnings)


class ProtectionApplyWorker(QtCore.QThread):
    """Apply one connected gate's protection profile off the Qt UI thread."""

    def __init__(self, name: str, session: object, max_voltage: float, current_compliance: float, parent=None):
        super().__init__(parent)
        self.name = name
        self.session = session
        self.max_voltage = float(max_voltage)
        self.current_compliance = float(current_compliance)
        self.success = False
        self.settings: dict[str, object] = {}
        self.message = ""

    def run(self):
        try:
            setpoint = float(self.session.get_voltage_setpoint())
            if not math.isclose(setpoint, 0.0, abs_tol=1e-9):
                raise RuntimeError(
                    f"{self.name.upper()} is at {setpoint:g} V. Ramp it to 0 V before changing protection limits."
                )
            result = self.session.apply_protection_settings(
                self.current_compliance,
                self.max_voltage,
            )
            self.settings = dict(result) if isinstance(result, dict) else {}
            actual_current = float(self.settings["current_compliance_a"])
            actual_voltage = float(self.settings["max_source_voltage_v"])
            current_range = self.settings.get("current_range_a")
            range_text = "" if current_range is None else f"; {float(current_range):.3g} A current range"
            self.message = (
                f"{self.name.upper()} protection applied and verified: "
                f"{actual_current:.3g} A compliance; +/-{actual_voltage:g} V maximum{range_text}."
            )
            self.success = True
        except Exception as ex:
            self.message = f"{self.name.upper()} protection apply failed: {ex}"


class EmergencyRampWorker(QtCore.QThread):
    ramp_finished = QtCore.pyqtSignal(str)

    def __init__(self, sessions: dict, daq_channels: list):
        super().__init__()
        self._sessions = dict(sessions)
        self._daq_channels = list(daq_channels or [])

    def run(self):
        from app.constants import SAFE_RAMP_STEP_T, SAFE_RAMP_STEP_V
        from app.utils import safe_ramp

        failures: list[str] = []
        for name in ("g1", "g2", "g3"):
            session = self._sessions.get(name)
            if session is not None:
                try:
                    safe_ramp(
                        session.set_voltage,
                        session.get_voltage_setpoint(),
                        0.0,
                        SAFE_RAMP_STEP_V,
                        SAFE_RAMP_STEP_T,
                    )
                except Exception as ex:
                    failures.append(f"{name.upper()} zero failed: {ex}")
        daq = self._sessions.get("daq")
        if daq is not None:
            for chan in self._daq_channels:
                try:
                    safe_ramp(
                        lambda v, c=chan: daq.set_voltage(c, v),
                        daq.get_ao_value(chan),
                        0.0,
                        SAFE_RAMP_STEP_V,
                        SAFE_RAMP_STEP_T,
                    )
                except Exception as ex:
                    failures.append(f"DAQ ao{chan} zero failed: {ex}")
        if failures:
            self.ramp_finished.emit("Emergency ramp completed with warnings: " + "; ".join(failures))
        else:
            self.ramp_finished.emit("Emergency safe ramp complete: all requested outputs at 0 V.")


class DeviceManager(QtCore.QObject):
    status_changed = QtCore.pyqtSignal(str, str, str)
    operation_changed = QtCore.pyqtSignal(bool, str)
    resources_changed = QtCore.pyqtSignal(object)
    manual_control_finished = QtCore.pyqtSignal(str, bool, str, dict)
    gate_currents_read = QtCore.pyqtSignal(dict, str)
    daq_output_finished = QtCore.pyqtSignal(int, bool, str, dict)
    protection_changed = QtCore.pyqtSignal(str, bool, str, dict)

    def __init__(self, connections: Connections):
        super().__init__()
        self.connections = connections
        self.sessions: Dict[str, object | None] = {"g1": None, "g2": None, "g3": None, "daq": None, "mono": None, "lockin": None}
        self.states: Dict[str, str] = {name: "idle" for name in self.sessions}
        self.details: Dict[str, str] = {name: "" for name in self.sessions}
        self._connected_addresses: Dict[str, str] = {name: self._address_for(name) for name in self.sessions}
        self._connected_modes: Dict[str, str] = {name: self._mode_for(name) for name in self.sessions}
        self._connected_protections: Dict[str, tuple[float, float]] = {
            name: (0.0, 0.0) for name in self.sessions
        }
        self._in_use: Set[str] = set()
        self._operation_thread: Optional[QtCore.QThread] = None
        self._manual_worker: Optional[ManualControlWorker] = None
        self._gate_current_worker: Optional[GateCurrentReadWorker] = None
        self._protection_worker: Optional[ProtectionApplyWorker] = None
        self._emergency_worker: Optional[QtCore.QThread] = None
        self._pending_emergency_daq_channels: Optional[list[int]] = None

    def _address_for(self, name: str) -> str:
        return {
            "g1": self.connections.gate1,
            "g2": self.connections.gate2,
            "g3": self.connections.gate3,
            "daq": self.connections.daq_dev,
            "mono": self.connections.mono,
            "lockin": self.connections.lockin,
        }[name]

    def _mode_for(self, name: str) -> str:
        return {
            "g1": self.connections.gate1_mode,
            "g2": self.connections.gate2_mode,
            "g3": self.connections.gate3_mode,
            "daq": "",
            "mono": "",
            "lockin": "",
        }[name]

    def _protection_for(self, name: str) -> tuple[float, float]:
        if name not in {"g1", "g2", "g3"}:
            return (0.0, 0.0)
        number = name[-1]
        return (
            float(getattr(self.connections, f"gate{number}_max_voltage_v")),
            float(getattr(self.connections, f"gate{number}_current_compliance_a")),
        )

    def _emit_status(self, name: str, state: str, detail: str = ""):
        self.states[name] = state
        self.details[name] = detail
        self.status_changed.emit(name, state, detail)

    def state(self, name: str) -> str:
        return self.states.get(name, "idle")

    def detail(self, name: str) -> str:
        return self.details.get(name, "")

    def connected_address(self, name: str) -> str:
        return self._connected_addresses.get(name, "")

    def connected_mode(self, name: str) -> str:
        return self._connected_modes.get(name, "")

    def needs_reconnect(self, name: str) -> bool:
        session = self.sessions.get(name)
        if session is None:
            return False
        return (
            self._address_for(name) != self._connected_addresses.get(name, "")
            or self._mode_for(name) != self._connected_modes.get(name, "")
        )

    def protection_is_applied(self, name: str) -> bool:
        if name not in {"g1", "g2", "g3"} or self.sessions.get(name) is None:
            return False
        if self.connected_mode(name) != KEITHLEY_MODE_VOLTAGE_2W:
            return True
        requested = self._protection_for(name)
        applied = self._connected_protections.get(name, (0.0, 0.0))
        return all(
            math.isclose(float(actual), float(target), rel_tol=0.02, abs_tol=1e-15)
            for actual, target in zip(applied, requested)
        )

    def applied_gate_voltage_limit(self, name: str) -> float:
        return float(self._connected_protections.get(name, (0.0, 0.0))[0])

    def is_voltage_source_mode(self, name: str) -> bool:
        return self.connected_mode(name) == KEITHLEY_MODE_VOLTAGE_2W

    def is_ohm_mode(self, name: str) -> bool:
        return self.connected_mode(name) == KEITHLEY_MODE_OHM_4W

    def mode_summary(self, name: str) -> str:
        mode = self.connected_mode(name)
        if name in {"g1", "g2", "g3"} and mode:
            return keithley_mode_label(mode)
        return ""

    def get_session(self, name: str):
        return self.sessions.get(name)

    def is_connected(self, name: str) -> bool:
        return self.get_session(name) is not None and self.state(name) == "ok"

    def is_busy(self) -> bool:
        return (
            (self._operation_thread is not None and self._operation_thread.isRunning())
            or (self._manual_worker is not None and self._manual_worker.isRunning())
            or (self._gate_current_worker is not None and self._gate_current_worker.isRunning())
            or (self._protection_worker is not None and self._protection_worker.isRunning())
            or (self._emergency_worker is not None and self._emergency_worker.isRunning())
        )

    def current_in_use(self) -> Set[str]:
        return set(self._in_use)

    def is_in_use(self, name: str) -> bool:
        return name in self._in_use

    def mark_in_use(self, names: Iterable[str]) -> tuple[bool, List[str]]:
        if self.is_busy():
            return False, ["hardware operation"]
        requested = {name for name in names if name}
        unapplied = sorted(
            name
            for name in requested
            if name in {"g1", "g2", "g3"}
            and self.is_connected(name)
            and self.is_voltage_source_mode(name)
            and not self.protection_is_applied(name)
        )
        if unapplied:
            return False, [f"{name.upper()} protection limits" for name in unapplied]
        blocked = sorted(requested & self._in_use)
        if blocked:
            return False, blocked
        previous = set(self._in_use)
        self._in_use.update(requested)
        if self._in_use != previous:
            self.resources_changed.emit(set(self._in_use))
        return True, []

    def release(self, names: Iterable[str]):
        previous = set(self._in_use)
        for name in names:
            self._in_use.discard(name)
        if self._in_use != previous:
            self.resources_changed.emit(set(self._in_use))

    def get_ao_items(self) -> List[str]:
        items: List[str] = []
        try:
            from nidaqmx.system import System

            dev = next((d for d in System.local().devices if d.name == self.connections.daq_dev), None)
            if dev:
                items = [ch.name.split("/")[-1] for ch in dev.ao_physical_chans]
        except Exception:
            pass
        return items or ["ao0", "ao1"]

    def daq_output_channels(self) -> list[int]:
        return self._daq_output_channels()

    def sync_addresses(self):
        changed = []
        for name in self.sessions:
            new_addr = self._address_for(name)
            new_mode = self._mode_for(name)
            if self.sessions[name] is not None and (
                new_addr != self._connected_addresses.get(name)
                or new_mode != self._connected_modes.get(name)
            ):
                changed.append(name)
        for name in changed:
            try:
                self._safe_zero_keithley_before_close(name)
            except Exception as ex:
                self._emit_status(name, "err", f"Zero before reconnect failed: {ex}")
                continue
            self._close_device(name)
            self._emit_status(name, "idle", "Connection settings changed")
            self._connected_addresses[name] = self._address_for(name)
            self._connected_modes[name] = self._mode_for(name)

    def connect_all(self) -> bool:
        if self.is_busy():
            return False
        if self._in_use:
            self.operation_changed.emit(False, "Cannot reconnect while a measurement is running.")
            return False
        worker = ConnectWorker(self)
        self._operation_thread = worker
        worker.status_changed.connect(self._emit_status)
        worker.finished_ok.connect(lambda: self.operation_changed.emit(False, "Connected"))
        worker.failed.connect(lambda msg: self.operation_changed.emit(False, msg or "Connection failed"))
        worker.finished.connect(self._finish_connect_thread)
        self.operation_changed.emit(True, "Connecting...")
        worker.start()
        return True

    def disconnect_all(self) -> bool:
        if self.is_busy():
            return False
        if self._in_use:
            self.operation_changed.emit(False, "Cannot disconnect while a measurement is running.")
            return False
        worker = DisconnectWorker(self)
        self._operation_thread = worker
        worker.status_changed.connect(self._emit_status)
        worker.finished_ok.connect(lambda: self.operation_changed.emit(False, "Disconnected"))
        worker.failed.connect(lambda msg: self.operation_changed.emit(False, msg or "Disconnect failed"))
        worker.finished.connect(self._clear_operation_thread)
        self.operation_changed.emit(True, "Disconnecting...")
        worker.start()
        return True

    def ramp_gate(self, name: str, target: float) -> bool:
        """Safely ramp one connected gate source to a requested voltage."""
        if name not in {"g1", "g2", "g3"}:
            raise ValueError(f"Unknown gate: {name}")
        if not self.can_accept_gate_target(name):
            if self._in_use:
                self.operation_changed.emit(False, "Manual control is unavailable while a measurement is running.")
            elif not self.is_connected(name):
                self.operation_changed.emit(False, f"Connect {name.upper()} before using manual control.")
            elif not self.is_voltage_source_mode(name):
                self.operation_changed.emit(
                    False,
                    f"{name.upper()} must use 2-wire voltage source mode for manual voltage control.",
                )
            else:
                self.operation_changed.emit(False, "Wait for the current hardware operation to finish.")
            return False
        if not math.isclose(float(target), 0.0, abs_tol=1e-9) and not self.protection_is_applied(name):
            self.operation_changed.emit(False, f"Apply and verify {name.upper()} protection limits before changing its output.")
            return False
        applied_limit = self.applied_gate_voltage_limit(name)
        if abs(float(target)) > applied_limit + 1e-12:
            self.operation_changed.emit(
                False,
                f"{name.upper()} target {float(target):g} V exceeds its applied +/-{applied_limit:g} V source limit. "
                "Set a larger Max source voltage under Keithley Protection, ramp the gate to 0 V, and click Apply.",
            )
            return False
        worker = self._manual_worker
        if worker is not None:
            worker.update_target(float(target))
            action = (
                "safely ramping to 0 V"
                if abs(float(target)) < 1e-9
                else f"ramping to {float(target):g} V"
            )
            self.operation_changed.emit(True, f"{name.upper()} target updated; {action}...")
            return True
        self._start_manual_worker(name, self.sessions[name], target)
        return True

    def can_accept_gate_target(self, name: str) -> bool:
        """Return whether a gate can start or update a manual voltage ramp."""
        if name not in {"g1", "g2", "g3"}:
            return False
        blockers = (
            self._operation_thread,
            self._gate_current_worker,
            self._protection_worker,
            self._emergency_worker,
        )
        if any(worker is not None and worker.isRunning() for worker in blockers):
            return False
        if self._in_use or not self.is_connected(name) or not self.is_voltage_source_mode(name):
            return False
        return self._manual_worker is None or self._manual_worker.name == name

    def apply_gate_protection(self, name: str, max_voltage: float, current_compliance: float) -> bool:
        if name not in {"g1", "g2", "g3"}:
            raise ValueError(f"Unknown gate: {name}")
        if self.is_busy():
            self.operation_changed.emit(False, "Wait for the current hardware operation to finish.")
            return False
        if self._in_use:
            self.operation_changed.emit(False, "Protection limits cannot be changed while a measurement is running.")
            return False
        if not self.is_connected(name):
            self.operation_changed.emit(False, f"Connect {name.upper()} before applying protection limits.")
            return False
        if not self.is_voltage_source_mode(name):
            self.operation_changed.emit(False, f"{name.upper()} protection limits are inactive in 4-wire Ohms mode.")
            return False
        number = name[-1]
        setattr(self.connections, f"gate{number}_max_voltage_v", float(max_voltage))
        setattr(self.connections, f"gate{number}_current_compliance_a", float(current_compliance))
        worker = ProtectionApplyWorker(name, self.sessions[name], max_voltage, current_compliance, self)
        self._protection_worker = worker
        worker.finished.connect(self._finish_protection_apply)
        self.operation_changed.emit(True, f"Applying {name.upper()} protection limits...")
        worker.start()
        return True

    def ramp_daq_output(self, ao_index: int, target: float) -> bool:
        ao_index = int(ao_index)
        if self.is_busy():
            self.operation_changed.emit(False, "Wait for the current hardware operation to finish.")
            return False
        if self._in_use:
            self.operation_changed.emit(False, "DAQ manual control is unavailable while a measurement is running.")
            return False
        if not self.is_connected("daq"):
            self.operation_changed.emit(False, "Connect the DAQ before using AO manual control.")
            return False
        session = self.sessions["daq"]
        if ao_index not in getattr(session, "ao_channel_indexes", []):
            self.operation_changed.emit(False, f"DAQ AO{ao_index} is unavailable.")
            return False
        self._start_manual_worker(f"daq_ao{ao_index}", session, float(target))
        return True

    def set_monochromator_wavelength(self, wavelength_nm: float) -> bool:
        """Move the connected monochromator without blocking the UI."""
        if not self._manual_control_available("mono"):
            return False
        self._start_manual_worker("mono", self.sessions["mono"], wavelength_nm)
        return True

    def read_gate_currents(self, *, quiet: bool = False, names=None) -> bool:
        """Read the present voltage/current state from connected gates."""
        if self.is_busy():
            if not quiet:
                self.operation_changed.emit(False, "Wait for the current hardware operation to finish.")
            return False
        if self._in_use:
            if not quiet:
                self.operation_changed.emit(False, "Gate readback cannot be refreshed while a measurement is running.")
            return False
        requested_names = tuple(names or ("g1", "g2", "g3"))
        if any(name not in {"g1", "g2", "g3"} for name in requested_names):
            raise ValueError("Gate readback names must be g1, g2, or g3.")
        sessions = {
            name: self.sessions[name] if self.is_connected(name) else None
            for name in requested_names
        }
        if not any(sessions.values()):
            if not quiet:
                self.operation_changed.emit(False, "Connect at least one gate before refreshing gate readback.")
            return False
        modes = {name: self.connected_mode(name) for name in requested_names}
        worker = GateCurrentReadWorker(sessions, modes, self, quiet=quiet, names=requested_names)
        self._gate_current_worker = worker
        worker.finished.connect(self._finish_gate_current_read)
        if not quiet:
            self.operation_changed.emit(True, "Refreshing gate readback...")
        worker.start()
        return True

    def _manual_control_available(self, name: str) -> bool:
        if self.is_busy():
            self.operation_changed.emit(False, "Wait for the current hardware operation to finish.")
            return False
        if self._in_use:
            self.operation_changed.emit(False, "Manual control is unavailable while a measurement is running.")
            return False
        if not self.is_connected(name):
            self.operation_changed.emit(False, f"Connect {name.upper()} before using manual control.")
            return False
        if name in {"g1", "g2", "g3"} and not self.is_voltage_source_mode(name):
            self.operation_changed.emit(False, f"{name.upper()} must use 2-wire voltage source mode for manual voltage control.")
            return False
        return True

    def _start_manual_worker(self, name: str, session: object, target: float):
        worker = ManualControlWorker(name, session, target, self)
        self._manual_worker = worker
        worker.finished.connect(self._finish_manual_worker)
        if name in {"g1", "g2", "g3"}:
            action = "safely ramping to 0 V" if abs(target) < 1e-9 else f"ramping to {target:g} V"
            message = f"{name.upper()} {action}..."
        elif name.startswith("daq_ao"):
            ao_index = int(name.removeprefix("daq_ao"))
            action = "safely ramping to 0 V" if abs(target) < 1e-9 else f"ramping to {target:g} V"
            message = f"DAQ AO{ao_index} {action}..."
        else:
            message = f"Moving monochromator to {target:g} nm..."
        self.operation_changed.emit(True, message)
        worker.start()

    def _finish_manual_worker(self):
        worker = self._manual_worker
        if worker is None:
            return
        self._manual_worker = None
        worker.deleteLater()
        if worker.name.startswith("daq_ao"):
            ao_index = int(worker.name.removeprefix("daq_ao"))
            self.daq_output_finished.emit(
                ao_index,
                worker.success,
                worker.message,
                dict(worker.daq_readback or {}),
            )
        result: dict[str, object] = {"target": worker.target}
        if worker.gate_readback:
            result.update(worker.gate_readback)

        # A target can arrive after the worker's last stability check but
        # before Qt handles its finished signal. Continue to that destination
        # without dropping the click or publishing an intermediate completion.
        if (
            worker.success
            and worker.name in {"g1", "g2", "g3"}
            and result.get("set_voltage") is not None
            and not math.isclose(float(result["set_voltage"]), worker.target, abs_tol=1e-9)
            and self._pending_emergency_daq_channels is None
        ):
            self._start_manual_worker(worker.name, worker.session, worker.target)
            return
        self.manual_control_finished.emit(worker.name, worker.success, worker.message, result)
        if self._pending_emergency_daq_channels is not None:
            daq_channels = self._pending_emergency_daq_channels
            self._pending_emergency_daq_channels = None
            self._start_emergency_worker(daq_channels)
        else:
            self.operation_changed.emit(False, worker.message)

    def _finish_gate_current_read(self):
        worker = self._gate_current_worker
        if worker is None:
            return
        self._gate_current_worker = None
        worker.deleteLater()
        self.gate_currents_read.emit(worker.readbacks, worker.message)
        if not worker.quiet:
            self.operation_changed.emit(False, worker.message)

    def _finish_protection_apply(self):
        worker = self._protection_worker
        if worker is None:
            return
        self._protection_worker = None
        if worker.success:
            max_voltage = float(worker.settings.get("max_source_voltage_v", worker.max_voltage))
            current_compliance = float(worker.settings.get("current_compliance_a", worker.current_compliance))
            self._connected_protections[worker.name] = (max_voltage, current_compliance)
            self._emit_status(worker.name, "ok", worker.message)
        worker.deleteLater()
        pending_emergency = self._pending_emergency_daq_channels
        if pending_emergency is not None:
            self._pending_emergency_daq_channels = None
            self._start_emergency_worker(pending_emergency)
        else:
            self.operation_changed.emit(False, worker.message)
        self.protection_changed.emit(worker.name, worker.success, worker.message, dict(worker.settings))
        self._queue_gate_readback()

    def _clear_operation_thread(self):
        if self._operation_thread is not None:
            self._operation_thread.deleteLater()
            self._operation_thread = None

    def _finish_connect_thread(self):
        self._clear_operation_thread()
        self._queue_gate_readback()

    def _queue_gate_readback(self):
        if any(self.sessions.get(name) is not None for name in ("g1", "g2", "g3")):
            QtCore.QTimer.singleShot(0, lambda: self.read_gate_currents(quiet=True))

    def _connect_all_in_thread(self, emitter):
        self._connect_keithley("g1", emitter=emitter)
        self._connect_keithley("g2", emitter=emitter)
        self._connect_keithley("g3", emitter=emitter)
        self._connect_daq(emitter=emitter)
        self._connect_mono(emitter=emitter)
        self._connect_lockin(emitter=emitter)

    def _disconnect_all_in_thread(self, emitter):
        for session_name in ("g1", "g2", "g3"):
            try:
                self._safe_zero_keithley_before_close(session_name)
            except Exception:
                pass
        # Do not change DAQ AO on a normal disconnect. The last held values
        # may belong to equipment outside this run; only explicit Ramp/Zero
        # controls or Emergency Stop are allowed to modify them.
        for name in ("g1", "g2", "g3", "daq", "mono", "lockin"):
            self._close_device(name)
            emitter.emit(name, "idle", "")

    def _connect_keithley(self, name: str, emitter):
        address = self._address_for(name)
        mode = self._mode_for(name)
        if not address:
            try:
                self._safe_zero_keithley_before_close(name)
            except Exception as ex:
                emitter.emit(name, "err", f"Zero before disconnect failed: {ex}")
                return
            self._close_device(name)
            emitter.emit(name, "idle", "")
            return
        if (
            self.sessions[name] is not None
            and self._connected_addresses.get(name) == address
            and self._connected_modes.get(name) == mode
        ):
            emitter.emit(name, "ok", self._keithley_detail(name, self.sessions[name], readback=False))
            return
        session = None
        try:
            self._safe_zero_keithley_before_close(name)
            self._close_device(name)
            session_cls = Keithley2400OhmMode if mode == KEITHLEY_MODE_OHM_4W else Keithley2400VoltMode
            session = session_cls(
                name,
                address,
                curr_comp=float(getattr(session_cls, "DEFAULT_CURRENT_COMPLIANCE_A", 1e-6)),
                max_source_voltage=float(getattr(session_cls, "DEFAULT_SOURCE_VOLTAGE_LIMIT_V", 20.0)),
            )
            session.connect()
            self.sessions[name] = session
            self._connected_addresses[name] = address
            self._connected_modes[name] = mode
            protection_warning = ""
            if mode == KEITHLEY_MODE_VOLTAGE_2W:
                default_protection = session.read_protection_settings(include_trip=False)
                self._connected_protections[name] = (
                    float(default_protection["max_source_voltage_v"]),
                    float(default_protection["current_compliance_a"]),
                )
                requested_max_voltage, requested_current = self._protection_for(name)
                try:
                    protection = session.apply_protection_settings(
                        requested_current,
                        requested_max_voltage,
                    )
                    self._connected_protections[name] = (
                        float(protection["max_source_voltage_v"]),
                        float(protection["current_compliance_a"]),
                    )
                except Exception as profile_error:
                    default_max_voltage = float(default_protection["max_source_voltage_v"])
                    default_current = float(default_protection["current_compliance_a"])
                    try:
                        restored = session.apply_protection_settings(default_current, default_max_voltage)
                        self._connected_protections[name] = (
                            float(restored["max_source_voltage_v"]),
                            float(restored["current_compliance_a"]),
                        )
                        protection_warning = (
                            f" Saved profile apply failed: {profile_error}. "
                            "Verified safe defaults remain active; edit the profile and click Apply to retry."
                        )
                    except Exception as restore_error:
                        protection_warning = (
                            f" Saved profile apply failed: {profile_error}. "
                            f"The fallback profile could not be verified: {restore_error}. "
                            "Only Zero is available until protection is successfully applied."
                        )
            else:
                self._connected_protections[name] = (0.0, 0.0)
            emitter.emit(name, "ok", self._keithley_detail(name, session, readback=False) + protection_warning)
        except Exception as ex:
            if session is not None:
                _safe(session, "close")
            self.sessions[name] = None
            emitter.emit(name, "err", str(ex))

    def _keithley_detail(self, name: str, session: object, readback: bool = True) -> str:
        mode = self._mode_for(name)
        mode_text = keithley_mode_label(mode)
        if mode != KEITHLEY_MODE_VOLTAGE_2W:
            return f"{mode_text}; voltage-source protection settings are inactive."
        if readback:
            protection = session.read_protection_settings(include_trip=False)
            max_voltage = float(protection["max_source_voltage_v"])
            current_compliance = float(protection["current_compliance_a"])
        else:
            max_voltage, current_compliance = self._connected_protections[name]
        start_voltage = getattr(session, "connection_start_voltage", None)
        zero_detail = ""
        if start_voltage is not None and not math.isclose(float(start_voltage), 0.0, abs_tol=1e-9):
            zero_detail = f" Existing {float(start_voltage):g} V setpoint was safely ramped to 0 V before configuration."
        return (
            f"{mode_text}; ±{max_voltage:g} V maximum; "
            f"{current_compliance:.3g} A current compliance. "
            + (
                "Requested profile is applied and verified."
                if self.protection_is_applied(name)
                else "Connected with default limits; edit the profile and click Apply."
            )
            + zero_detail
        )

    def _connect_daq(self, emitter):
        address = self._address_for("daq")
        if not address:
            self._close_device("daq")
            emitter.emit("daq", "idle", "")
            return
        if self.sessions["daq"] is not None and self._connected_addresses.get("daq") == address:
            emitter.emit("daq", "ok", self._daq_detail(self.sessions["daq"]))
            return
        self._close_device("daq")
        session = None
        try:
            ao_items = self.get_ao_items()
            ao_indexes = [int(i[2:]) for i in ao_items if i.startswith("ao")]
            session = DaqCard(address=address, ao_channel_indexes=ao_indexes, ai_channel_indexes=[0, 1, 2, 3], read_delay=0.5)
            session.connect()
            self.sessions["daq"] = session
            self._connected_addresses["daq"] = address
            emitter.emit("daq", "ok", self._daq_detail(session))
        except Exception as ex:
            if session is not None:
                _safe(session, "close")
            self.sessions["daq"] = None
            emitter.emit("daq", "err", str(ex))

    @staticmethod
    def _daq_detail(session: object) -> str:
        parts = []
        nonzero = False
        for ao_index in getattr(session, "ao_channel_indexes", []):
            state = session.get_ao_state(ao_index)
            voltage = float(state["commanded_v"])
            nonzero = nonzero or abs(voltage) > 0.005
            parts.append(f"AO{ao_index}={voltage:+.6g} V")
        values = ", ".join(parts) if parts else "no AO channels"
        if nonzero:
            return (
                f"Existing DAQ output detected and preserved: {values}. "
                "Use Manual Controls to ramp safely to zero or a new value."
            )
        return f"DAQ outputs preserved on connection: {values}."

    def _connect_mono(self, emitter):
        address = self._address_for("mono")
        if not address:
            self._close_device("mono")
            emitter.emit("mono", "idle", "")
            return
        if self.sessions["mono"] is not None and self._connected_addresses.get("mono") == address:
            emitter.emit("mono", "ok", "")
            return
        self._close_device("mono")
        try:
            session = SP2300("m", address)
            session.connect()
            self.sessions["mono"] = session
            self._connected_addresses["mono"] = address
            emitter.emit("mono", "ok", "")
        except Exception as ex:
            self.sessions["mono"] = None
            emitter.emit("mono", "err", str(ex))

    def _connect_lockin(self, emitter):
        address = self._address_for("lockin")
        if not address:
            self._close_device("lockin")
            emitter.emit("lockin", "idle", "")
            return
        if self.sessions["lockin"] is not None and self._connected_addresses.get("lockin") == address:
            emitter.emit("lockin", "ok", getattr(self.sessions["lockin"], "identity", ""))
            return
        self._close_device("lockin")
        try:
            session = SRSLockin("lockin", address)
            session.connect()
            self.sessions["lockin"] = session
            self._connected_addresses["lockin"] = address
            emitter.emit("lockin", "ok", getattr(session, "identity", ""))
        except Exception as ex:
            self.sessions["lockin"] = None
            emitter.emit("lockin", "err", str(ex))

    def _close_device(self, name: str):
        session = self.sessions.get(name)
        if session is None:
            return
        _safe(session, "close")
        self.sessions[name] = None
        self._connected_addresses[name] = self._address_for(name)
        self._connected_modes[name] = self._mode_for(name)
        self._connected_protections[name] = (0.0, 0.0)

    def _safe_zero_keithley_before_close(self, name: str):
        if name not in {"g1", "g2", "g3"}:
            return
        session = self.sessions.get(name)
        if session is None or self.connected_mode(name) != KEITHLEY_MODE_VOLTAGE_2W:
            return
        from app.constants import SAFE_RAMP_STEP_T, SAFE_RAMP_STEP_V
        from app.utils import safe_ramp

        safe_ramp(
            session.set_voltage,
            session.get_voltage_setpoint(),
            0.0,
            SAFE_RAMP_STEP_V,
            SAFE_RAMP_STEP_T,
        )

    def _daq_output_channels(self, requested: Optional[Iterable[int]] = None) -> list[int]:
        if requested is not None:
            channels = requested
        else:
            daq = self.sessions.get("daq")
            if daq is None:
                channels = []
            else:
                channels = getattr(daq, "ao_channel_indexes", [])
            if daq is not None and not channels:
                channels = [int(ao[2:]) for ao in self.get_ao_items() if ao.startswith("ao")]
        unique: list[int] = []
        for channel in channels:
            try:
                index = int(channel)
            except (TypeError, ValueError):
                continue
            if index not in unique:
                unique.append(index)
        return sorted(unique)

    def emergency_stop(self, daq_channels: Optional[Iterable[int]] = None):
        daq_channels = self._daq_output_channels(daq_channels)
        if self._emergency_worker is not None and self._emergency_worker.isRunning():
            return
        if self._manual_worker is not None and self._manual_worker.isRunning():
            self._pending_emergency_daq_channels = list(daq_channels)
            self._manual_worker.request_cancel()
            self.operation_changed.emit(True, "Emergency stop requested: stopping manual control before zeroing outputs...")
            return
        if self._protection_worker is not None and self._protection_worker.isRunning():
            self._pending_emergency_daq_channels = list(daq_channels)
            self.operation_changed.emit(True, "Emergency stop requested: waiting for the active protection command before zeroing outputs...")
            return
        self._start_emergency_worker(list(daq_channels))

    def _start_emergency_worker(self, daq_channels: list[int]):
        worker = EmergencyRampWorker(self.sessions, self._daq_output_channels(daq_channels))
        self._emergency_worker = worker
        worker.ramp_finished.connect(lambda msg: self.operation_changed.emit(False, msg))
        worker.finished.connect(self._clear_emergency_worker)
        worker.finished.connect(worker.deleteLater)
        self.operation_changed.emit(True, "Emergency stop: ramping all outputs to 0 V safely...")
        worker.start()

    def _clear_emergency_worker(self):
        self._emergency_worker = None
        self._queue_gate_readback()

    def shutdown(self):
        if self._manual_worker is not None and self._manual_worker.isRunning():
            self._manual_worker.request_cancel()
            self._manual_worker.wait()
            self._finish_manual_worker()
        if self._gate_current_worker is not None and self._gate_current_worker.isRunning():
            self._gate_current_worker.wait()
            self._finish_gate_current_read()
        if self._protection_worker is not None and self._protection_worker.isRunning():
            self._protection_worker.wait()
            self._finish_protection_apply()
        if self._emergency_worker is not None and self._emergency_worker.isRunning():
            self._emergency_worker.wait()
        self._disconnect_all_in_thread(self.status_changed)
        for name in ("g1", "g2", "g3", "daq", "mono", "lockin"):
            self._close_device(name)
            self._emit_status(name, "idle", "")
