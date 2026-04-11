from __future__ import annotations

from typing import Dict, Iterable, List, Optional, Set

from PyQt6 import QtCore

from app.keithley_modes import KEITHLEY_MODE_LABELS, KEITHLEY_MODE_OHM_4W, KEITHLEY_MODE_VOLTAGE_2W, keithley_mode_label
from app.models import Connections
from app.utils import _safe
from instruments import DaqCard, Keithley2400OhmMode, Keithley2400VoltMode, SP2300


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


class DeviceManager(QtCore.QObject):
    status_changed = QtCore.pyqtSignal(str, str, str)
    operation_changed = QtCore.pyqtSignal(bool, str)

    def __init__(self, connections: Connections):
        super().__init__()
        self.connections = connections
        self.sessions: Dict[str, object | None] = {"g1": None, "g2": None, "g3": None, "daq": None, "mono": None}
        self.states: Dict[str, str] = {name: "idle" for name in self.sessions}
        self.details: Dict[str, str] = {name: "" for name in self.sessions}
        self._connected_addresses: Dict[str, str] = {name: self._address_for(name) for name in self.sessions}
        self._connected_modes: Dict[str, str] = {name: self._mode_for(name) for name in self.sessions}
        self._in_use: Set[str] = set()
        self._operation_thread: Optional[QtCore.QThread] = None

    def _address_for(self, name: str) -> str:
        return {
            "g1": self.connections.gate1,
            "g2": self.connections.gate2,
            "g3": self.connections.gate3,
            "daq": self.connections.daq_dev,
            "mono": self.connections.mono,
        }[name]

    def _mode_for(self, name: str) -> str:
        return {
            "g1": self.connections.gate1_mode,
            "g2": self.connections.gate2_mode,
            "g3": self.connections.gate3_mode,
            "daq": "",
            "mono": "",
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
        return self._operation_thread is not None and self._operation_thread.isRunning()

    def current_in_use(self) -> Set[str]:
        return set(self._in_use)

    def mark_in_use(self, names: Iterable[str]) -> tuple[bool, List[str]]:
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

    def sync_addresses(self):
        changed = []
        for name in self.sessions:
            new_addr = self._address_for(name)
            new_mode = self._mode_for(name)
            if self.sessions[name] is not None and (new_addr != self._connected_addresses.get(name) or new_mode != self._connected_modes.get(name)):
                changed.append(name)
        for name in changed:
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

    def _disconnect_all_in_thread(self, emitter):
        for session_name in ("g1", "g2", "g3"):
            session = self.sessions.get(session_name)
            if session is not None:
                try:
                    session.ramp_voltage(0.0, 1.0)
                except Exception:
                    pass
        self._close_daq_outputs()
        for name in ("g1", "g2", "g3", "daq", "mono"):
            self._close_device(name)
            emitter.emit(name, "idle", "")

    def _connect_keithley(self, name: str, curr_comp: float, volt_comp: float, emitter):
        address = self._address_for(name)
        mode = self._mode_for(name)
        if not address:
            self._close_device(name)
            emitter.emit(name, "idle", "")
            return
        if self.sessions[name] is not None and self._connected_addresses.get(name) == address and self._connected_modes.get(name) == mode:
            emitter.emit(name, "ok", keithley_mode_label(mode))
            return
        self._close_device(name)
        try:
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

    def _close_device(self, name: str):
        session = self.sessions.get(name)
        if session is None:
            return
        _safe(session, "close")
        self.sessions[name] = None
        self._connected_addresses[name] = self._address_for(name)
        self._connected_modes[name] = self._mode_for(name)

    def _close_daq_outputs(self):
        daq = self.sessions.get("daq")
        if daq is None:
            return
        for ao in self.get_ao_items():
            try:
                idx = int(ao[2:])
                daq.ramp_voltage(idx, 0.0, 0.1)
            except Exception:
                pass

    def emergency_stop(self, daq_channels: Optional[Iterable[int]] = None):
        for name in ("g1", "g2", "g3"):
            session = self.sessions.get(name)
            if session is not None:
                try:
                    session.ramp_voltage(0.0, 1.0)
                except Exception:
                    pass
        daq = self.sessions.get("daq")
        if daq is not None:
            for chan in daq_channels or []:
                try:
                    daq.ramp_voltage(chan, 0.0, 0.1)
                except Exception:
                    pass

    def shutdown(self):
        self.emergency_stop()
        for name in ("g1", "g2", "g3", "daq", "mono"):
            self._close_device(name)
            self._emit_status(name, "idle", "")
