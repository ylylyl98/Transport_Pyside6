from __future__ import annotations

import time

from PySide6.QtCore import QObject, Signal
from app.drag_drive import acquisition_columns, acquisition_units, acquire_current_sample
from app.bias_control import bias2_target, validate_bias2, ramp_bias2, zero_bias2, read_bias2, ramp_pair


class RunStopped(RuntimeError):
    pass


class RunWorker(QObject):
    point = Signal(float, float)
    point_data = Signal(object)
    status = Signal(str)
    log = Signal(str)
    progress = Signal(float)
    finished = Signal(str)
    stopped = Signal(str)
    error = Signal(str)
    clear_plot = Signal()

    def __init__(self):
        super().__init__()
        self._stop = False
        self._pause = False

    def request_stop(self):
        self._stop = True

    def acquire_currents(self, averages):
        self._current_sample = acquire_current_sample(
            self.daq, averages, self.amp_rate, self.lkn_rate,
            self.signal_chain, self.check_abort_pause)
        return self._current_sample

    def extra_columns(self):
        return (*acquisition_columns(self.signal_chain), "Bias2", "Ibias2_Keithley")

    def extra_units(self):
        return (*acquisition_units(self.signal_chain), "V", "A")

    def extra_values(self):
        params = getattr(self, "_bias2_condition", self.p)
        self._bias2_current = read_bias2(getattr(self, "g4", None), params)
        self._current_sample.update(Bias2=getattr(self, "_bias2_voltage", None), Ibias2_Keithley=self._bias2_current)
        return [*(self._current_sample[key] for key in acquisition_columns(self.signal_chain)),
                getattr(self, "_bias2_voltage", None), self._bias2_current]

    def validate_source_trajectory(self, points, *, gate_ramp, vds_ramp, use_g1=True, use_g2=True, use_g3=True):
        """Verify every commanded Keithley target before any outputs begin."""
        from app.voltage_resolution import validate_source_targets
        for name, key, label, ramp, enabled in (
            ("g1", "vtg", "G1 / Vtg", gate_ramp, use_g1),
            ("g2", "vbg", "G2 / Vbg", gate_ramp, use_g2),
            ("g3", "vds", "G3 / Vds", vds_ramp, use_g3),
        ):
            session = getattr(self, name, None)
            if enabled and session is not None:
                validate_source_targets(session, [point[key] for point in points], label,
                                        ramp_step=ramp, refresh=True)

    def validate_bias2(self, params, points, bias1_enabled=True):
        if getattr(params, "bias2_mode", "Disabled") == "Disabled":
            return
        manager = getattr(self, "device_manager", None)
        if manager is None:
            raise RuntimeError("Bias2 requires the device manager for preflight safety validation.")
        validate_bias2(params, points, manager, bias1_enabled, self.p.vds_source, refresh=True)
        if self.g4 is not manager.get_session("g4"):
            raise RuntimeError("Bias2 session does not match the verified G4 session.")

    def apply_bias2(self, voltage, params=None):
        params = params or self.p
        self._bias2_condition = params
        if getattr(params, "bias2_mode", "Disabled") != "Disabled":
            self._bias2_touched = True
        elif getattr(self, "_bias2_touched", False):
            zero_bias2(self.g4, self._bias2_previous_condition)
            self._bias2_touched = False
        self._bias2_previous_condition = params
        self._bias2_voltage = ramp_bias2(getattr(self, "g4", None), params, voltage, self.check_abort_pause)

    def move_pair(self, voltage, params=None, step=None):
        params = params or self.p
        self._bias2_condition = self._bias2_previous_condition = params
        self._bias2_touched = True
        primary = self.g3 if self.p.vds_source == "Keithley 2400" else None
        self._bias2_voltage = ramp_pair(primary, self.daq, int(self.p.ao_channel),
                                        self.g4, params, voltage,
                                        step or self.p.vds_ramp, self.check_abort_pause)

    def cleanup_bias2(self, params=None):
        if getattr(self, "_bias2_touched", False):
            zero_bias2(getattr(self, "g4", None), getattr(self, "_bias2_previous_condition", params or self.p))

    def request_pause(self, paused: bool):
        self._pause = paused

    def check_abort_pause(self):
        if self._stop:
            raise RunStopped("Stopped by user")
        while self._pause:
            time.sleep(0.05)
            if self._stop:
                raise RunStopped("Stopped by user")

    def emit_safe_state_report(self, failures: list[str]):
        if failures:
            self.log.emit("Safe-state warning: " + "; ".join(failures))
        else:
            self.log.emit("Safe state confirmed: outputs returned to 0 V; sessions kept open.")
