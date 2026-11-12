"""Shared, fail-closed APS100 / LS335 command interlock (no hardware I/O).

Both workers must share one instance. Reservations and observations use one lock
so queued commands cannot authorize heating and a magnet start concurrently.
This supplements, and does not replace, the magnet's thermal protection.
"""
from __future__ import annotations

import math
import threading
import time

from utils.config import cfg


class SampleHeatingInterlock:
    def __init__(self, *, clock=time.monotonic):
        self._clock = clock
        self._lock = threading.RLock()
        self._magnet = None
        self._control = None
        self._control_time = None
        self._sample_command = False
        self._magnet_commands = 0
        self._reserved = False
        self._reserved_owner = None
        self._measurement_input_owner = None

    @staticmethod
    def _finite(value):
        return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)

    def _fresh(self, timestamp, maximum_age):
        return (self._finite(timestamp) and self._finite(maximum_age) and maximum_age > 0
                and 0 <= self._clock() - timestamp <= maximum_age)

    def note_magnet(self, snapshot):
        with self._lock:
            self._magnet = snapshot

    def invalidate_magnet(self, *_):
        self.note_magnet(None)

    def note_control(self, state):
        with self._lock:
            self._control = dict(state) if state is not None else None
            self._control_time = self._clock() if state is not None else None

    def invalidate_control(self, *_):
        self.note_control(None)

    def reserve_magnet(self, reserved, *, owner=None):
        with self._lock:
            if reserved and self._sample_command:
                raise RuntimeError("LS335 input or sample control in progress")
            if not reserved and self._measurement_input_owner is not None:
                raise RuntimeError("Measurement input session must end before releasing APS ownership")
            self._reserved = bool(reserved)
            self._reserved_owner = str(owner) if reserved and owner is not None else None
            # Require a new observation after a workflow releases ownership.
            self._magnet = None

    def heating_block_reason(self):
        with self._lock:
            if self._magnet_commands or self._reserved:
                return "APS100 operation/reservation active; LS335 heating is inhibited"
            snapshot = self._magnet
            if snapshot is None or not self._fresh(getattr(snapshot, "monotonic_s", None),
                                                   cfg.lakeshore335.maximum_field_reading_age_s):
                return "APS100 state missing or stale; LS335 heating is inhibited"
            status = getattr(snapshot, "status", None)
            sweep = getattr(snapshot, "sweep_state", None)
            if (getattr(status, "sweep_active", None) is not False
                    or getattr(status, "standby", None) is not True
                    or getattr(status, "faulted", None) is not False
                    or not isinstance(sweep, str) or sweep.strip().lower() not in {"idle", "standby"}):
                return "APS100 is not confirmed idle and fault-free; LS335 heating is inhibited"
            if getattr(snapshot, "heater_on", None) is not False:
                return "APS100 persistent switch is on or transitioning; LS335 heating is inhibited"
            for name, tolerance in (("field_t", cfg.magnet.field_tolerance_t),
                                    ("output_field_t", cfg.magnet.field_tolerance_t),
                                    ("output_current_a", cfg.magnet.current_match_tolerance_a)):
                value = getattr(snapshot, name, None)
                if (not self._finite(value) or not self._finite(tolerance) or tolerance < 0
                        or abs(value) > tolerance):
                    return "APS100 magnet/output is not confirmed zero; LS335 heating is inhibited"
            return ""

    def magnet_block_reason(self):
        with self._lock:
            if self._sample_command:
                return "LS335 control command in progress; magnet start is inhibited"
            state = self._control
            if (not cfg.lakeshore335.verified_channel_mapping or state is None
                    or state.get("channel") != cfg.lakeshore335.sample_channel
                    or not self._fresh(self._control_time, cfg.lakeshore335.maximum_reading_age_s)):
                return "LS335 sample heater OFF readback missing or stale; magnet start is inhibited"
            # Zero instantaneous output alone is insufficient: an enabled PID
            # range or Zone ramp can resume heating after the magnet starts.
            if (state.get("range") != 0 or not self._finite(state.get("heater_output"))
                    or state["heater_output"] != 0
                    or state.get("control_mode") not in (1, 2)
                    or (state["control_mode"] == 2 and state.get("ramp_enabled") is not False)):
                return "LS335 sample heater is not confirmed OFF; use Heater OFF before starting the magnet"
            return ""

    def begin_sample_control(self):
        with self._lock:
            reason = self.heating_block_reason()
            if reason or self._sample_command:
                raise RuntimeError(reason or "LS335 control command already active")
            self._sample_command = True
            self.invalidate_control()

    def begin_input_configuration(self):
        """Reserve a sensor setup change against magnet and sample commands."""
        with self._lock:
            if self._reserved or self._magnet_commands or self._sample_command:
                raise RuntimeError("Instrument operation active; input configuration is locked")
            self._sample_command = True
            self.invalidate_control()

    def begin_measurement_input_session(self, owner):
        owner = str(owner)
        with self._lock:
            if not owner or not self._reserved or self._reserved_owner != owner:
                raise RuntimeError("Measurement input session requires matching APS reservation")
            if self._measurement_input_owner is not None or self._sample_command or self._magnet_commands:
                raise RuntimeError("Instrument operation active; cannot start input session")
            self._measurement_input_owner = owner

    def _require_stationary_measurement_owner(self, owner):
        if (self._measurement_input_owner != str(owner) or not self._reserved
                or self._reserved_owner != str(owner)):
            raise RuntimeError("Measurement input session owner mismatch")
        snapshot = self._magnet
        if snapshot is None or not self._fresh(getattr(snapshot, "monotonic_s", None),
                                                cfg.lakeshore335.maximum_field_reading_age_s):
            raise RuntimeError("Fresh APS100 stationary readback is required for input change")
        status = getattr(snapshot, "status", None)
        if (getattr(status, "sweep_active", None) is not False
                or getattr(status, "faulted", None) is not False
                or str(getattr(snapshot, "sweep_state", "")).strip().lower() not in {"pause", "paused", "standby", "idle"}):
            raise RuntimeError("APS100 must be confirmed stationary and fault-free for input change")

    def begin_measurement_input_change(self, owner):
        with self._lock:
            self._require_stationary_measurement_owner(owner)
            if self._sample_command or self._magnet_commands:
                raise RuntimeError("Instrument operation active; input change is locked")
            self._sample_command = True
            self.invalidate_control()

    def check_measurement_input_change(self, owner):
        with self._lock:
            self._require_stationary_measurement_owner(owner)
            if not self._sample_command or self._magnet_commands:
                raise RuntimeError("Measurement input change lost its reservation")

    def end_measurement_input_session(self, owner):
        with self._lock:
            if self._measurement_input_owner != str(owner):
                raise RuntimeError("Measurement input session owner mismatch")
            if self._sample_command:
                raise RuntimeError("Input change still in progress")
            self._measurement_input_owner = None

    def measurement_input_session_active(self):
        with self._lock:
            return self._measurement_input_owner is not None

    def measurement_input_session_owned(self, owner):
        with self._lock:
            return self._measurement_input_owner == str(owner)

    def end_sample_control(self):
        with self._lock:
            self._sample_command = False

    def begin_magnet(self, *, protective=False):
        with self._lock:
            if not protective:
                reason = self.magnet_block_reason()
                if reason:
                    raise RuntimeError(reason)
            self._magnet_commands += 1
            self._magnet = None

    def end_magnet(self):
        with self._lock:
            self._magnet_commands -= 1
            # An asynchronous sweep may still be running after command return.
            self._magnet = None
