from __future__ import annotations

from typing import Dict, Iterable, List, Optional, Set

from PyQt6 import QtCore

from app.keithley_modes import KEITHLEY_MODE_LABELS, KEITHLEY_MODE_OHM_4W, KEITHLEY_MODE_VOLTAGE_2W, keithley_mode_label
from app.models import Connections
from app.utils import _safe
from instruments import DaqCard, Keithley2400OhmMode, Keithley2400VoltMode, SP2300, SR830


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
        self.target = float(target)
        self.success = False
        self.message = ""
        self.gate_readback: dict[str, object] | None = None
        self._cancel_requested = False

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
                from app.utils import safe_ramp

                # Query the source directly so the ramp begins at the present
                # programmed level even if the software cache is stale.
                start = float(self.session.get_voltage_setpoint())
                zeroing = abs(self.target) < 1e-9
                step_v, step_t = (
                    (SAFE_RAMP_STEP_V, SAFE_RAMP_STEP_T)
                    if zeroing
                    else (GATE_BIAS_RAMP_STEP_V, GATE_BIAS_RAMP_STEP_T)
                )
                safe_ramp(
                    self.session.set_voltage,
                    start,
                    self.target,
                    step_v,
                    step_t,
                    check_fn=self._check_cancelled,
                )
                readback_warning = ""
                try:
                    self.gate_readback = self._read_gate_readback()
                except Exception as ex:
                    self.gate_readback = {
                        "connected": True,
                        "mode": KEITHLEY_MODE_VOLTAGE_2W,
                        "set_voltage": None,
                        "measured_voltage": None,
                        "current": None,
                        "error": str(ex),
                    }
                    readback_warning = f" Readback unavailable: {ex}"
                action = "safely ramped to 0 V" if zeroing else f"ramped to {self.target:g} V"
                self.message = f"{self.name.upper()} {action} from {start:g} V.{readback_warning}"
            else:
                self._check_cancelled()
                self.session.set_wavelength(self.target)
                self.message = f"Monochromator moved to {self.target:g} nm."
            self.success = True
        except Exception as ex:
            self.message = f"{self.name.upper()} manual control failed: {ex}"

    def _read_gate_readback(self) -> dict[str, object]:
        set_voltage = self.session.get_voltage_setpoint()
        readings = self.session.acquire()
        measured_voltage = readings.get("voltage") if isinstance(readings, dict) else None
        current = readings.get("current") if isinstance(readings, dict) else None
        if measured_voltage is None:
            measured_voltage = getattr(self.session, "voltage", None)
        if current is None:
            current = getattr(self.session, "current", None)
        return {
            "connected": True,
            "mode": KEITHLEY_MODE_VOLTAGE_2W,
            "set_voltage": None if set_voltage is None else float(set_voltage),
            "measured_voltage": None if measured_voltage is None else float(measured_voltage),
            "current": None if current is None else float(current),
            "error": "",
        }


class GateCurrentReadWorker(QtCore.QThread):
    """Read connected gate voltage/current state without blocking the Qt UI."""

    def __init__(self, sessions: dict, modes: dict, parent=None):
        super().__init__(parent)
        self.sessions = dict(sessions)
        self.modes = dict(modes)
        self.readbacks: dict[str, dict[str, object]] = {}
        self.message = ""

    def run(self):
        failures: list[str] = []
        for name in ("g1", "g2", "g3"):
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
    manual_control_finished = QtCore.pyqtSignal(str, bool, str)
    gate_currents_read = QtCore.pyqtSignal(dict, str)

    def __init__(self, connections: Connections):
        super().__init__()
        self.connections = connections
        self.sessions: Dict[str, object | None] = {"g1": None, "g2": None, "g3": None, "daq": None, "mono": None, "lockin": None}
        self.states: Dict[str, str] = {name: "idle" for name in self.sessions}
        self.details: Dict[str, str] = {name: "" for name in self.sessions}
        self._connected_addresses: Dict[str, str] = {name: self._address_for(name) for name in self.sessions}
        self._connected_modes: Dict[str, str] = {name: self._mode_for(name) for name in self.sessions}
        self._in_use: Set[str] = set()
        self._operation_thread: Optional[QtCore.QThread] = None
        self._manual_worker: Optional[ManualControlWorker] = None
        self._gate_current_worker: Optional[GateCurrentReadWorker] = None
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
            or (self._emergency_worker is not None and self._emergency_worker.isRunning())
        )

    def current_in_use(self) -> Set[str]:
        return set(self._in_use)

    def mark_in_use(self, names: Iterable[str]) -> tuple[bool, List[str]]:
        if self.is_busy():
            return False, ["hardware operation"]
        requested = {name for name in names if name}
        blocked = sorted(requested & self._in_use)
        if blocked:
            return False, blocked
        self._in_use.update(requested)
        return True, []

    def release(self, names: Iterable[str]):
        for name in names:
            self._in_use.discard(name)

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
            if self.sessions[name] is not None and (new_addr != self._connected_addresses.get(name) or new_mode != self._connected_modes.get(name)):
                changed.append(name)
        for name in changed:
            try:
                self._safe_zero_keithley_before_close(name)
            except Exception as ex:
                self._emit_status(name, "err", f"Zero before reconnect failed: {ex}")
                continue
            self._close_device(name)
            self._emit_status(name, "idle", "Address changed")
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
        worker.finished.connect(self._clear_operation_thread)
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
        if not self._manual_control_available(name):
            return False
        self._start_manual_worker(name, self.sessions[name], target)
        return True

    def set_monochromator_wavelength(self, wavelength_nm: float) -> bool:
        """Move the connected monochromator without blocking the UI."""
        if not self._manual_control_available("mono"):
            return False
        self._start_manual_worker("mono", self.sessions["mono"], wavelength_nm)
        return True

    def read_gate_currents(self) -> bool:
        """Read the present voltage/current state from connected gates."""
        if self.is_busy():
            self.operation_changed.emit(False, "Wait for the current hardware operation to finish.")
            return False
        if self._in_use:
            self.operation_changed.emit(False, "Gate readback cannot be refreshed while a measurement is running.")
            return False
        sessions = {
            name: self.sessions[name] if self.is_connected(name) else None
            for name in ("g1", "g2", "g3")
        }
        if not any(sessions.values()):
            self.operation_changed.emit(False, "Connect at least one gate before refreshing gate readback.")
            return False
        modes = {name: self.connected_mode(name) for name in ("g1", "g2", "g3")}
        worker = GateCurrentReadWorker(sessions, modes, self)
        self._gate_current_worker = worker
        worker.finished.connect(self._finish_gate_current_read)
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
        if worker.success and worker.name in {"g1", "g2", "g3"} and worker.gate_readback:
            self.gate_currents_read.emit({worker.name: worker.gate_readback}, worker.message)
        self.manual_control_finished.emit(worker.name, worker.success, worker.message)
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
        self.operation_changed.emit(False, worker.message)

    def _clear_operation_thread(self):
        if self._operation_thread is not None:
            self._operation_thread.deleteLater()
            self._operation_thread = None

    def _connect_all_in_thread(self, emitter):
        self._connect_keithley("g1", curr_comp=1e-7, volt_comp=40, emitter=emitter)
        self._connect_keithley("g2", curr_comp=1e-7, volt_comp=40, emitter=emitter)
        self._connect_keithley("g3", curr_comp=1e-6, volt_comp=20, emitter=emitter)
        self._connect_daq(emitter=emitter)
        self._connect_mono(emitter=emitter)
        self._connect_lockin(emitter=emitter)

    def _disconnect_all_in_thread(self, emitter):
        for session_name in ("g1", "g2", "g3"):
            try:
                self._safe_zero_keithley_before_close(session_name)
            except Exception:
                pass
        self._close_daq_outputs()
        for name in ("g1", "g2", "g3", "daq", "mono", "lockin"):
            self._close_device(name)
            emitter.emit(name, "idle", "")

    def _connect_keithley(self, name: str, curr_comp: float, volt_comp: float, emitter):
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
        if self.sessions[name] is not None and self._connected_addresses.get(name) == address and self._connected_modes.get(name) == mode:
            emitter.emit(name, "ok", keithley_mode_label(mode))
            return
        try:
            self._safe_zero_keithley_before_close(name)
            self._close_device(name)
            session_cls = Keithley2400OhmMode if mode == KEITHLEY_MODE_OHM_4W else Keithley2400VoltMode
            session = session_cls(name, address, curr_comp=curr_comp, volt_comp=volt_comp)
            session.connect()
            self.sessions[name] = session
            self._connected_addresses[name] = address
            self._connected_modes[name] = mode
            emitter.emit(name, "ok", keithley_mode_label(mode))
        except Exception as ex:
            self.sessions[name] = None
            emitter.emit(name, "err", str(ex))

    def _connect_daq(self, emitter):
        address = self._address_for("daq")
        if not address:
            self._close_device("daq")
            emitter.emit("daq", "idle", "")
            return
        if self.sessions["daq"] is not None and self._connected_addresses.get("daq") == address:
            emitter.emit("daq", "ok", "")
            return
        self._close_device("daq")
        try:
            ao_items = self.get_ao_items()
            ao_indexes = [int(i[2:]) for i in ao_items if i.startswith("ao")]
            session = DaqCard(address=address, ao_channel_indexes=ao_indexes, ai_channel_indexes=[0, 1, 2, 3], read_delay=0.5)
            session.connect()
            self.sessions["daq"] = session
            self._connected_addresses["daq"] = address
            emitter.emit("daq", "ok", "")
        except Exception as ex:
            self.sessions["daq"] = None
            emitter.emit("daq", "err", str(ex))

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
            emitter.emit("lockin", "ok", "")
            return
        self._close_device("lockin")
        try:
            session = SR830("lockin", address)
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

    def _close_daq_outputs(self):
        from app.constants import SAFE_RAMP_STEP_T, SAFE_RAMP_STEP_V
        from app.utils import safe_ramp

        daq = self.sessions.get("daq")
        if daq is None:
            return
        for idx in self._daq_output_channels():
            try:
                safe_ramp(
                    lambda v, i=idx: daq.set_voltage(i, v),
                    daq.get_ao_value(idx),
                    0.0,
                    SAFE_RAMP_STEP_V,
                    SAFE_RAMP_STEP_T,
                )
            except Exception:
                pass

    def emergency_stop(self, daq_channels: Optional[Iterable[int]] = None):
        daq_channels = self._daq_output_channels(daq_channels)
        if self._emergency_worker is not None and self._emergency_worker.isRunning():
            return
        if self._manual_worker is not None and self._manual_worker.isRunning():
            self._pending_emergency_daq_channels = list(daq_channels)
            self._manual_worker.request_cancel()
            self.operation_changed.emit(True, "Emergency stop requested: stopping manual control before zeroing outputs...")
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

    def shutdown(self):
        if self._manual_worker is not None and self._manual_worker.isRunning():
            self._manual_worker.request_cancel()
            self._manual_worker.wait()
            self._finish_manual_worker()
        if self._gate_current_worker is not None and self._gate_current_worker.isRunning():
            self._gate_current_worker.wait()
            self._finish_gate_current_read()
        if self._emergency_worker is not None and self._emergency_worker.isRunning():
            self._emergency_worker.wait()
        self._disconnect_all_in_thread(self.status_changed)
        for name in ("g1", "g2", "g3", "daq", "mono", "lockin"):
            self._close_device(name)
            self._emit_status(name, "idle", "")
