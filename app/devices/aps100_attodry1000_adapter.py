"""Direct APS100 control for the attoDRY1000 magnet system.

The attoDRY2100 SDK exposes a high-level RPC magnet service.  The attoDRY1000
installation instead connects directly to the Attocube APS100 virtual COM port,
so the matching, sweep and persistent-switch sequencing live here.

Application-facing field values are tesla and sweep rates are tesla/minute.
The APS100 field protocol uses kG for limit writes and A/s for rate writes.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from collections import Counter, deque
from contextlib import contextmanager
import math
import re
import threading
import time
from typing import Callable, Optional

from app.engine.aps100_rate_table import reduced_rate_table, validate_rate_table
from app.engine.field_approach import normalize_approach, approach_targets, no_overshoot_intermediate


DEFAULT_RESOURCE = "ASRL5::INSTR"


class _ReversalWarnings:
    """Record threshold-crossing episodes without unbounded per-read growth."""

    def __init__(self, history):
        self.history = history
        history.setdefault("reversal_exceeded", False)
        history.setdefault("reversal_events", [])
        history.setdefault("reversal_event_count", 0)
        self.active_event = None
        self.active = False

    def observe(self, field, peak, direction, limit, now):
        excursion = peak - direction * field
        if excursion <= limit + 1e-12:
            self.active = False
            self.active_event = None
            return
        self.history["reversal_exceeded"] = True
        if not self.active:
            self.active = True
            self.history["reversal_event_count"] += 1
            if len(self.history["reversal_events"]) < 100:
                self.active_event = dict(monotonic_s=now, readback_t=field,
                                         peak_t=direction * peak, excursion_t=excursion,
                                         limit_t=limit)
                self.history["reversal_events"].append(self.active_event)
        elif self.active_event is not None and excursion > self.active_event["excursion_t"]:
            self.active_event.update(monotonic_s=now, readback_t=field,
                                     peak_t=direction * peak, excursion_t=excursion)


class _FieldMoveDiagnostics:
    """Bounded bookkeeping using only observations already acquired by a move."""

    def __init__(self, history, started):
        self.history, self.started = history, started
        self.trace, self.stages = [], []
        history.update(trace=self.trace, stages=self.stages, overhead_s=0.0)
        history["trace_started_monotonic_s"] = started
        self.last_sample = None
        self.last_kept_at = None
        self.stage = None
        self.sweep_started = None
        self.sweep_duration = 0.0

    def observe(self, field, now):
        if not math.isfinite(field):
            return  # Existing move validation owns invalid readback failures.
        self.last_sample = [round(now - self.started, 2), round(field, 5)]
        # Reserve the final slot for the last observation, even on a long move.
        if len(self.trace) < 239 and (self.last_kept_at is None or now - self.last_kept_at >= 1.0):
            self.trace.append(self.last_sample)
            self.last_kept_at = now

    def begin_stage(self, endpoint, factor, field, rate):
        self.stage = dict(endpoint_t=endpoint, rate_factor=factor,
                          active_rate_t_per_min=rate, start_field_t=field,
                          sweep_command_monotonic_s=None)
        self.stages.append(self.stage)

    def begin_sweep(self, now):
        self.stage["sweep_command_monotonic_s"] = now
        self.sweep_started = now

    def end_stage(self, field, now, reason=None):
        if self.stage is None or "end_monotonic_s" in self.stage:
            return
        self.stage.update(end_field_t=field, end_monotonic_s=now)
        if reason is not None:
            self.stage["exit_reason"] = reason
        if self.sweep_started is not None:
            self.sweep_duration += now - self.sweep_started
            self.sweep_started = None

    def finish(self, now):
        if self.last_sample is not None and (not self.trace or self.trace[-1] != self.last_sample):
            self.trace.append(self.last_sample)
        self.history["overhead_s"] = max(0.0, now - self.started - self.sweep_duration)


class APS100Error(RuntimeError):
    """Base error for APS100 communication and command failures."""


class APS100IdentityError(APS100Error):
    """Raised when the connected instrument is not an Attocube APS100."""


class APS100FieldHistoryError(APS100Error):
    """The measured field left its approved range or violated preparation limits."""


class APS100OvershootError(APS100Error):
    """A directional move passed its target; reverse movement is forbidden."""


class APS100QualityOvershoot(APS100OvershootError):
    """A paused strict acquisition move passed its nominal target."""


class APS100SafetyError(APS100Error):
    """Raised when a requested operation violates a configured guard."""


class APS100OperationCancelled(APS100SafetyError):
    """An explicit cancellation, distinct from hardware/communication failure."""
    def __init__(self, stop_event=None):
        self.reason = getattr(stop_event, "reason", "user")
        super().__init__(f"Magnet operation cancelled: {self.reason}")


class APS100CommandBlockedError(APS100Error):
    """Raised when the front panel or local state blocks a remote command."""


class APS100TimeoutError(APS100Error):
    """Raised when a field or state transition does not complete in time."""


@dataclass(frozen=True)
class APS100Status:
    raw: int
    sweep_active: bool
    standby: bool
    quench: bool
    power_module_failure: bool
    message_available: bool
    event_summary: bool
    master_summary: bool
    menu_locked: bool

    @property
    def faulted(self) -> bool:
        return self.quench or self.power_module_failure


@dataclass(frozen=True)
class APS100Identity:
    manufacturer: str
    model: str
    serial: str
    firmware: str
    build: str

    @property
    def display_name(self) -> str:
        return (
            f"{self.manufacturer} {self.model} "
            f"S/N {self.serial} FW {self.firmware}.{self.build}"
        )


@dataclass(frozen=True)
class MagnetSnapshot:
    monotonic_s: float
    field_t: float
    output_field_t: float
    output_current_a: float
    # ``None`` means the APS100 reported its documented transition state
    # (PSHTR?=2); never turn that state into a false On/Off assertion.
    heater_on: Optional[bool]
    sweep_state: str
    status: APS100Status
    lower_limit_t: float
    upper_limit_t: float
    voltage_limit_v: float
    magnet_voltage_v: float
    output_voltage_v: float
    units: str
    operating_mode: str
    heater_state: Optional[int] = None
    field_monotonic_s: Optional[float] = None
    approach_mode: str = ''
    approach_phase: str = ''
    approach_complete: bool = False
    approach_rate_t_per_min: Optional[float] = None

    @property
    def driven_mode(self) -> bool:
        return self.heater_on is True

    @property
    def persistent_mode(self) -> bool:
        return self.heater_on is False


_VALUE_UNIT_RE = re.compile(
    r"^\s*([-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?)\s*([A-Za-z]+(?:/[A-Za-z]+)?)?\s*$"
)


def _parse_number_unit(value: str) -> tuple[float, str]:
    match = _VALUE_UNIT_RE.match(str(value))
    if not match:
        raise APS100Error(f"Could not parse APS100 value: {value!r}")
    return float(match.group(1)), (match.group(2) or "")


def field_response_to_tesla(value: str, coil_constant_t_per_a: float) -> float:
    number, unit = _parse_number_unit(value)
    normalized = unit.lower()
    if normalized == "t":
        return number
    if normalized == "kg":
        return number * 0.1
    if normalized == "g":
        return number / 10_000.0
    if normalized == "a":
        return number * coil_constant_t_per_a
    raise APS100Error(f"Unexpected APS100 field/current unit {unit!r} in {value!r}")


def voltage_response_to_volts(value: str) -> float:
    number, unit = _parse_number_unit(value)
    if unit and unit.lower() != "v":
        raise APS100Error(f"Unexpected APS100 voltage unit {unit!r} in {value!r}")
    return number


def parse_heater_state(response: str | int | float) -> int:
    """Parse the APS100 PSHTR? state without collapsing transition state 2."""
    text = str(response).strip()
    try:
        value = float(text)
    except (TypeError, ValueError) as exc:
        raise APS100Error(
            f"Invalid PSHTR? response {response!r}; expected 0 (OFF), 1 (ON), or 2 (transition)"
        ) from exc
    if not math.isfinite(value) or not value.is_integer() or int(value) not in {0, 1, 2}:
        raise APS100Error(
            f"Invalid PSHTR? response {response!r}; expected 0 (OFF), 1 (ON), or 2 (transition)"
        )
    return int(value)


def decode_status_byte(raw: int | str) -> APS100Status:
    value = int(raw)
    if not 0 <= value <= 255:
        raise APS100Error(f"Invalid APS100 status byte: {value}")
    return APS100Status(
        raw=value,
        sweep_active=bool(value & 0x01),
        standby=bool(value & 0x02),
        quench=bool(value & 0x04),
        power_module_failure=bool(value & 0x08),
        message_available=bool(value & 0x10),
        event_summary=bool(value & 0x20),
        master_summary=bool(value & 0x40),
        menu_locked=bool(value & 0x80),
    )


class APS100AttoDry1000Adapter:
    """Thread-safe, direct serial adapter for one APS100 magnet channel."""

    def __init__(
        self,
        resource_name: str = DEFAULT_RESOURCE,
        *,
        baud_rate: int = 9600,
        timeout_ms: int = 1500,
        coil_constant_t_per_a: float = 0.20328,
        maximum_field_t: float = 9.0,
        maximum_current_a: float = 44.27,
        maximum_rate_a_per_s: float = 0.0343,
        heater_warm_s: float = 60.0,
        heater_cool_s: float = 120.0,
        heater_transition_timeout_s: float = 30.0,
        current_match_tolerance_a: Optional[float] = None,
        current_match_tolerance_t: float = 0.010,
        lead_zero_tolerance_t: float = 0.010,
        expect_echo: bool = True,
        resource_manager=None,
        visa_resource=None,
        sleep_fn: Callable[[float], None] = time.sleep,
    ) -> None:
        if not math.isfinite(float(coil_constant_t_per_a)) or coil_constant_t_per_a <= 0:
            raise ValueError("coil_constant_t_per_a must be positive and finite")
        self.resource_name = str(resource_name).strip() or DEFAULT_RESOURCE
        self.baud_rate = int(baud_rate)
        self.timeout_ms = int(timeout_ms)
        self.coil_constant_t_per_a = float(coil_constant_t_per_a)
        self.maximum_field_t = float(maximum_field_t)
        self.maximum_current_a = float(maximum_current_a)
        self.maximum_rate_a_per_s = float(maximum_rate_a_per_s)
        self.heater_warm_s = float(heater_warm_s)
        self.heater_cool_s = float(heater_cool_s)
        self.heater_transition_timeout_s = max(0.1, float(heater_transition_timeout_s))
        self.current_match_tolerance_a = (float(current_match_tolerance_a) if current_match_tolerance_a is not None
                                          else float(current_match_tolerance_t) / self.coil_constant_t_per_a)
        if not math.isfinite(self.current_match_tolerance_a) or self.current_match_tolerance_a <= 0:
            raise ValueError("current-match tolerance must be positive and finite")
        self.lead_zero_tolerance_t = float(lead_zero_tolerance_t)
        if not math.isfinite(self.lead_zero_tolerance_t) or self.lead_zero_tolerance_t <= 0:
            raise ValueError("lead_zero_tolerance_t must be positive and finite")
        self.expect_echo = bool(expect_echo)
        self._resource_manager = resource_manager
        self._resource = visa_resource
        # Resource managers are process-wide in PyVISA.  This adapter owns only
        # its resource; closing a manager here can invalidate other devices.
        self._owns_resource_manager = False
        self._lock = threading.RLock()
        self._sleep = sleep_fn
        self._identity: Optional[APS100Identity] = None
        self._remote = False
        self._last_target_t: Optional[float] = None
        self.command_observer = None
        self._io_events = deque(maxlen=512)
        self._io_sequence = 0
        self._io_controls = deque(maxlen=64)
        self._io_control_count = 0
        self._query_counts = Counter()

    @contextmanager
    def _io_transaction(self, kind, command):
        """Record existing serial traffic only; APS feedback is not a field sensor."""
        selected = (command in {"IMAG?", "IOUT?", "VMAG?", "VOUT?", "SWEEP?", "PSHTR?", "UNITS?"}
                    or kind == "write" and command.startswith(("SWEEP ", "PAUSE", "LLIM ", "ULIM ", "RATE ", "PSHTR ", "UNITS ")))
        if not selected:
            yield None
            return
        event = dict(kind=kind, command=command, started_monotonic_s=time.monotonic(),
                     started_utc_epoch_s=time.time())
        try:
            yield event
        except Exception as exc:
            event['error'] = f'{type(exc).__name__}: {exc}'[:512]
            raise
        finally:
            event['finished_monotonic_s'] = time.monotonic()
            event['finished_utc_epoch_s'] = time.time()
            # Diagnostics cannot replace an instrument exception or prevent cleanup.
            try:
                self._io_sequence += 1
                event['sequence'] = self._io_sequence
                self._io_events.append(event)
                if kind == 'write':
                    self._io_control_count += 1
                    self._io_controls.append(event)
            except Exception:
                pass

    def capture_io_context(self):
        """Capture preceding polling/settling replies without requesting new ones."""
        with self._lock:
            return dict(sequence=self._io_sequence, control_count=self._io_control_count,
                        preceding=[dict(event) for event in list(self._io_events)[-64:]])

    def move_io_diagnostics(self, context):
        with self._lock:
            events = [dict(event) for event in self._io_events if event['sequence'] > context['sequence']]
            controls = [dict(event) for event in self._io_controls if event['sequence'] > context['sequence']]
            return dict(schema='aps100_feedback_io_v1',
                        source='APS100 current feedback / equivalent field; no independent magnetic field sensor',
                        coil_constant_t_per_a=self.coil_constant_t_per_a,
                        timing='Sequential serial transactions, not simultaneous measurements; issued marks the host write call',
                        preceding=context['preceding'], events=events,
                        control_writes=controls, control_limit=64,
                        dropped_control_writes=max(0, self._io_control_count - context['control_count'] - len(controls)),
                        dropped_events=max(0, self._io_sequence - context['sequence'] - len(events)),
                        event_limit=512, preceding_limit=64, raw_character_limit=512)

    @property
    def connected(self) -> bool:
        return self._resource is not None and self._identity is not None

    @property
    def identity(self) -> Optional[APS100Identity]:
        return self._identity

    def connect(self) -> APS100Identity:
        """Open and identify the supply without changing any APS100 setting."""
        with self._lock:
            if self.connected:
                assert self._identity is not None
                return self._identity
            self._io_events.clear()
            self._io_sequence = 0
            self._io_controls.clear()
            self._io_control_count = 0
            self._query_counts.clear()
            try:
                if self._resource is None:
                    if self._resource_manager is None:
                        import pyvisa

                        self._resource_manager = pyvisa.ResourceManager()
                        self._owns_resource_manager = True
                    self._resource = self._resource_manager.open_resource(self.resource_name)
                resource = self._resource
                for attr, value in (
                ("baud_rate", self.baud_rate),
                ("timeout", self.timeout_ms),
                ("write_termination", "\r"),
                ("read_termination", "\n"),
                ):
                    try:
                        setattr(resource, attr, value)
                    except Exception:
                        pass
                identity = self._parse_identity(self._query("*IDN?"))
                if identity.manufacturer.lower() != "attocube" or identity.model.upper() != "APS100":
                    self._identity = None
                    raise APS100IdentityError(
                    f"Expected Attocube APS100, received {identity.manufacturer} {identity.model}"
                    )
                self._identity = identity
                return identity
            except Exception:
                # A failed identification must not leave a half-open resource
                # attached to this adapter (or poison a shared manager).
                resource = self._resource
                self._resource = None
                self._identity = None
                if resource is not None:
                    try:
                        resource.close()
                    except Exception:
                        pass
                raise

    @staticmethod
    def _parse_identity(response: str) -> APS100Identity:
        parts = [part.strip() for part in str(response).split(",")]
        if len(parts) < 5:
            raise APS100IdentityError(f"Unexpected *IDN? response: {response!r}")
        return APS100Identity(*parts[:5])

    @staticmethod
    def _normalized_line(value: str) -> str:
        return " ".join(str(value).replace("\r", " ").replace("\n", " ").split())

    def _require_connected(self):
        if self._resource is None or self._identity is None:
            raise APS100Error("APS100 is not connected")
        return self._resource

    def _read_line(self, diagnostic=None) -> str:
        resource = self._require_connected()
        raw = resource.read()
        if diagnostic is not None:
            diagnostic['raw_echo'] = str(raw)[:512]
            diagnostic['echo_truncated'] = len(str(raw)) > 512
            diagnostic['echo_received_monotonic_s'] = time.monotonic()
            diagnostic['echo_received_utc_epoch_s'] = time.time()
        return self._normalized_line(raw)

    def _query(self, command: str) -> str:
        with self._lock, self._io_transaction("query", command) as diagnostic:
            self._query_counts[command.split()[0]] += 1
            resource = self._resource
            if resource is None:
                raise APS100Error("APS100 resource is not open")
            if diagnostic is not None:
                diagnostic['issued_monotonic_s'] = time.monotonic()
                diagnostic['issued_utc_epoch_s'] = time.time()
            resource.write(command)
            command_norm = self._normalized_line(command).lower()
            last = ""
            for _ in range(8):
                raw_line = resource.read()
                line = self._normalized_line(raw_line)
                if not line:
                    continue
                last = line
                if line.lower() == command_norm:
                    continue
                if diagnostic is not None:
                    diagnostic['raw_response'] = str(raw_line)[:512]
                    diagnostic['raw_truncated'] = len(str(raw_line)) > 512
                    diagnostic['response_received_monotonic_s'] = time.monotonic()
                    diagnostic['response_received_utc_epoch_s'] = time.time()
                if "command blocked" in line.lower():
                    raise APS100CommandBlockedError(
                        f"APS100 blocked {command!r}; close the front-panel menu "
                        "and enable remote operation"
                    )
                observer = self.command_observer
                if callable(observer):
                    observer(
                        {
                            "utc_epoch_s": time.time(),
                            "kind": "query",
                            "command": command,
                            "response": line,
                        }
                    )
                return line
            raise APS100Error(f"APS100 returned no response to {command!r}; last line={last!r}")

    def _write(self, command: str, *, check_errors: bool = True) -> None:
        with self._lock, self._io_transaction("write", command) as diagnostic:
            resource = self._require_connected()
            # *ESR? is cumulative and clears on read. Clear historical events
            # before a mutation so only errors caused by this command are
            # attributed to it. Live safety state is checked separately via
            # *STB?, SWEEP?, heater, current, and voltage telemetry.
            preexisting_event_status = 0
            if check_errors and not command.startswith(("*ESR", "*CLS")):
                preexisting_event_status = int(float(self._query("*ESR?")))
            observer = self.command_observer
            if callable(observer):
                observer(
                    {
                        "utc_epoch_s": time.time(),
                        "kind": "write",
                        "command": command,
                        "preexisting_esr": preexisting_event_status,
                    }
                )
            if diagnostic is not None:
                diagnostic['issued_monotonic_s'] = time.monotonic()
                diagnostic['issued_utc_epoch_s'] = time.time()
            resource.write(command)
            if diagnostic is not None:
                diagnostic['write_returned_monotonic_s'] = time.monotonic()
            if self.expect_echo:
                try:
                    echoed = self._read_line(diagnostic)
                except Exception as exc:
                    raise APS100Error(
                        f"Did not receive APS100 USB echo for {command!r}: {exc}"
                    ) from exc
                if echoed and echoed.lower() != self._normalized_line(command).lower():
                    if "command blocked" in echoed.lower():
                        raise APS100CommandBlockedError(
                            f"APS100 blocked {command!r}; close the front-panel "
                            "menu and enable remote operation"
                        )
                    raise APS100Error(
                        f"Unexpected APS100 response while writing {command!r}: {echoed!r}"
                    )
            if check_errors and not command.startswith("*ESR"):
                event_status = int(float(self._query("*ESR?")))
                if event_status & 0x3C:
                    raise APS100Error(
                        f"APS100 reported ESR={event_status} after {command!r}"
                    )

    def take_remote(self) -> None:
        self._write("REMOTE")
        self._remote = True

    def return_local(self) -> None:
        self._write("LOCAL", check_errors=False)
        self._remote = False

    def get_units(self) -> str:
        return self._query("UNITS?").strip()

    def get_operating_mode(self) -> str:
        """Return the APS100 operating mode (the Z magnet requires Manual)."""
        return self._query("MODE?").strip()

    def require_manual_mode(self) -> str:
        mode = self.get_operating_mode()
        if mode.lower() != "manual":
            raise APS100SafetyError(
                f"APS100 operating mode is {mode!r}; the Z magnet requires Manual mode"
            )
        return mode

    def select_field_units(self) -> str:
        self._write("UNITS G")
        units = self.get_units()
        if units.lower() not in {"g", "kg", "t"}:
            raise APS100Error(f"APS100 did not enter field units; UNITS? returned {units!r}")
        return units

    def get_status(self) -> APS100Status:
        return decode_status_byte(self._query("*STB?"))

    def get_sweep_state(self) -> str:
        return self._query("SWEEP?").strip().lower()

    def get_heater_state(self) -> int:
        """Return raw PSHTR? state: 0=OFF, 1=ON, 2=transition."""
        return parse_heater_state(self._query("PSHTR?"))

    def get_heater_status(self) -> Optional[bool]:
        """Return stable heater state, or ``None`` while PSHTR? reports 2."""
        state = self.get_heater_state()
        return {0: False, 1: True}.get(state)

    def get_field_t(self) -> float:
        return field_response_to_tesla(
            self._query("IMAG?"), self.coil_constant_t_per_a
        )

    def get_output_field_t(self) -> float:
        return field_response_to_tesla(
            self._query("IOUT?"), self.coil_constant_t_per_a
        )

    def get_limits_t(self) -> tuple[float, float]:
        low = field_response_to_tesla(
            self._query("LLIM?"), self.coil_constant_t_per_a
        )
        high = field_response_to_tesla(
            self._query("ULIM?"), self.coil_constant_t_per_a
        )
        return low, high

    def get_voltage_limit_v(self) -> float:
        return voltage_response_to_volts(self._query("VLIM?"))

    def get_magnet_voltage_v(self) -> float:
        return voltage_response_to_volts(self._query("VMAG?"))

    def get_output_voltage_v(self) -> float:
        return voltage_response_to_volts(self._query("VOUT?"))

    def get_rate_a_per_s(self, index: int) -> float:
        if index not in range(6):
            raise ValueError("APS100 rate index must be 0..5")
        value, unit = _parse_number_unit(self._query(f"RATE? {index}"))
        if unit and unit.lower() not in {"a", "a/s", "aps"}:
            raise APS100Error(f"Unexpected rate unit {unit!r}")
        return value

    def get_rate_t_per_min(self, index: int) -> float:
        return self.get_rate_a_per_s(index) * self.coil_constant_t_per_a * 60.0

    def get_fast_rate_a_per_s(self) -> float:
        """Return the configured APS100 Fast Mode readback (RATE? 5)."""
        value = float(self.get_rate_a_per_s(5))
        if not math.isfinite(value) or value <= 0.0:
            raise APS100SafetyError(
                f"APS100 Fast Mode RATE? 5 is invalid: {value!r} A/s"
            )
        return value

    def get_fast_rate_t_per_min(self) -> float:
        return self.get_fast_rate_a_per_s() * self.coil_constant_t_per_a * 60.0

    def get_range_a(self, index: int) -> float:
        if index not in range(5):
            raise ValueError("APS100 range index must be 0..4")
        value, _unit = _parse_number_unit(self._query(f"RANGE? {index}"))
        return value

    def get_rates(self) -> dict[int, tuple[float, float]]:
        rates = {}
        for index in range(5):
            boundary = float(self.get_range_a(index))
            rate_a = float(self.get_rate_a_per_s(index))
            if not math.isfinite(boundary) or boundary < 0.0:
                raise APS100SafetyError(f"APS100 RANGE? {index} readback is invalid: {boundary!r} A")
            if not math.isfinite(rate_a) or rate_a <= 0.0:
                raise APS100SafetyError(f"APS100 RATE? {index} readback is invalid: {rate_a!r} A/s")
            rates[index] = (boundary, rate_a * self.coil_constant_t_per_a * 60.0)
        return rates

    def restore_rates(self, rates: dict[int, tuple[float, float]], *, step_callback=None) -> dict[int, float]:
        """Restore captured RATE values with command/readback verification.

        APS100 RANGE entries are hardware-defined read-only boundaries; the
        captured range is therefore checked while only the programmable RATE
        value is written back. Read every rate afresh; unchanged entries need
        no write. Changed entries retain post-write verification. No cache is
        trusted across approach stages or restoration attempts.
        """
        restored = {}
        failures = []
        for index, captured in dict(rates or {}).items():
            try:
                index = int(index)
                if not isinstance(captured, (tuple, list)) or len(captured) < 2:
                    raise APS100Error(f"Invalid captured APS100 RATE entry for range {index}")
                expected_range, expected_rate = float(captured[0]), float(captured[1])
                if (not math.isfinite(expected_range) or expected_range < 0
                        or not math.isfinite(expected_rate) or expected_rate <= 0):
                    raise APS100Error(f"Invalid captured APS100 RATE entry for range {index}")
                actual_range = self.get_range_a(index)
                if not math.isfinite(actual_range) or abs(actual_range - expected_range) > 1e-6:
                    raise APS100Error(
                        f"APS100 range {index} boundary changed from {expected_range:g} A to {actual_range:g} A"
                    )
                rate_a = expected_rate / (self.coil_constant_t_per_a * 60.0)
                actual_rate = self.get_rate_a_per_s(index)
                if not math.isfinite(actual_rate) or actual_rate <= 0:
                    raise APS100Error(f"APS100 RATE? {index} readback is invalid: {actual_rate!r} A/s")
                if not math.isclose(actual_rate, rate_a, rel_tol=0.0, abs_tol=1e-12):
                    self._write(f"RATE {index} {rate_a:.7f}")
                    actual_rate = self.get_rate_a_per_s(index)
                if not math.isfinite(actual_rate) or actual_rate <= 0 or abs(actual_rate - rate_a) > 1e-6:
                    raise APS100Error(
                        f"APS100 RATE {index} restoration verification failed: "
                        f"requested {rate_a:g}, read {actual_rate:g} A/s"
                    )
                restored[index] = actual_rate * self.coil_constant_t_per_a * 60.0
            except Exception as exc:
                failures.append(f"range {index}: {exc}")
            if step_callback is not None:
                step_callback()
        if failures:
            raise APS100Error("; ".join(failures))
        return restored

    def _ensure_no_fault(self) -> APS100Status:
        self.require_manual_mode()
        return self._ensure_status_no_fault()

    def _ensure_status_no_fault(self) -> APS100Status:
        """Always read fault status and run the thermal guard; never cached."""
        status = self._read_fault_status()
        thermal_guard = getattr(self, "thermal_guard", None)
        if callable(thermal_guard):
            thermal_guard(status)
        return status

    def _read_fault_status(self) -> APS100Status:
        """Read and validate status before any thermal/field observation."""
        status = self.get_status()
        if status.quench:
            raise APS100SafetyError("APS100 reports a quench condition")
        if status.power_module_failure:
            raise APS100SafetyError("APS100 reports a power-module failure")
        if status.menu_locked:
            raise APS100SafetyError("APS100 front-panel menu is locking remote commands")
        return status

    def _hysteresis_safety_observation(self, check_mode: bool):
        """Return only this iteration's thermal-checked field, never a cache.

        The application worker explicitly opts into this callback contract.
        Standalone adapters and existing custom safety overrides keep their
        normal checks and let the caller obtain a separate fresh observation.
        """
        guard = getattr(self, "thermal_field_guard", None)
        if not callable(guard):
            if check_mode:
                self._ensure_no_fault()
            else:
                self._ensure_status_no_fault()
            return None
        if check_mode:
            self.require_manual_mode()
        status = self._read_fault_status()
        observation = guard(status)
        if observation is None:
            return None
        value = getattr(observation, "field_t", None)
        stamp = getattr(observation, "monotonic_s", None)
        if (not all(isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)
                    for v in (value, stamp))
                or not 0.0 <= time.monotonic() - stamp <= 3.0):
            raise APS100SafetyError("Invalid or stale field observation from thermal check")
        return observation

    def read_snapshot(self) -> MagnetSnapshot:
        with self._lock:
            units = self.get_units()
            operating_mode = self.get_operating_mode()
            field_started = time.monotonic()
            field_t = self.get_field_t()
            field_stamp = (field_started + time.monotonic()) / 2.
            output_field_t = self.get_output_field_t()
            low_t, high_t = self.get_limits_t()
            heater_state = self.get_heater_state()
            return MagnetSnapshot(
                monotonic_s=time.monotonic(),
                field_t=field_t,
                field_monotonic_s=field_stamp,
                output_field_t=output_field_t,
                output_current_a=output_field_t / self.coil_constant_t_per_a,
                heater_on={0: False, 1: True}.get(heater_state),
                sweep_state=self.get_sweep_state(),
                status=self.get_status(),
                lower_limit_t=low_t,
                upper_limit_t=high_t,
                voltage_limit_v=self.get_voltage_limit_v(),
                magnet_voltage_v=self.get_magnet_voltage_v(),
                output_voltage_v=self.get_output_voltage_v(),
                units=units,
                operating_mode=operating_mode,
                heater_state=heater_state,
            )

    def _validate_field(self, field_t: float) -> float:
        value = float(field_t)
        if not math.isfinite(value):
            raise APS100SafetyError("Magnetic field must be finite")
        if abs(value) > self.maximum_field_t + 1e-12:
            raise APS100SafetyError(
                f"Requested {value:.6g} T exceeds configured ±{self.maximum_field_t:g} T"
            )
        return value

    @staticmethod
    def _tesla_to_kg(field_t: float) -> float:
        return float(field_t) * 10.0

    def set_limits_t(self, low_t: float, high_t: float) -> tuple[float, float]:
        low = self._validate_field(low_t)
        high = self._validate_field(high_t)
        if low >= high:
            raise APS100SafetyError("Lower field limit must be less than upper limit")
        self._ensure_no_fault()
        self.select_field_units()
        old_low, old_high = self.get_limits_t()
        commands = (
            [("LLIM", low), ("ULIM", high)]
            if low <= old_high
            else [("ULIM", high), ("LLIM", low)]
        )
        for name, value in commands:
            self._write(f"{name} {self._tesla_to_kg(value):.6f}")
        actual_low, actual_high = self.get_limits_t()
        tolerance = 2e-4
        if abs(actual_low - low) > tolerance or abs(actual_high - high) > tolerance:
            raise APS100Error(
                "APS100 limit verification failed: "
                f"requested ({low:g}, {high:g}) T, "
                f"read ({actual_low:g}, {actual_high:g}) T"
            )
        return actual_low, actual_high

    def set_rate_t_per_min(
        self,
        rate_t_per_min: float,
        *,
        max_abs_field_t: Optional[float] = None,
    ) -> dict[int, float]:
        rate = float(rate_t_per_min)
        if not math.isfinite(rate) or rate <= 0:
            raise APS100SafetyError("Sweep rate must be positive and finite")
        rate_a_per_s = rate / (self.coil_constant_t_per_a * 60.0)
        if rate_a_per_s > self.maximum_rate_a_per_s + 1e-12:
            max_t_per_min = (
                self.maximum_rate_a_per_s * self.coil_constant_t_per_a * 60.0
            )
            raise APS100SafetyError(
                f"Requested {rate:g} T/min exceeds configured {max_t_per_min:.6g} T/min"
            )
        self._ensure_no_fault()
        max_current = self.maximum_current_a
        if max_abs_field_t is not None:
            max_current = min(
                max_current,
                abs(self._validate_field(max_abs_field_t)) / self.coil_constant_t_per_a,
            )
        boundaries = [self.get_range_a(index) for index in range(5)]
        changed: dict[int, float] = {}
        lower = 0.0
        for index, upper in enumerate(boundaries):
            if lower <= max_current + 1e-12:
                self._write(f"RATE {index} {rate_a_per_s:.7f}")
                actual = self.get_rate_a_per_s(index)
                if abs(actual - rate_a_per_s) > 1.5e-4:
                    raise APS100Error(
                        f"APS100 RATE {index} verification failed: "
                        f"requested {rate_a_per_s:g}, read {actual:g} A/s"
                    )
                changed[index] = actual * self.coil_constant_t_per_a * 60.0
            lower = upper
            if lower > max_current:
                break
        return changed

    def _set_directional_target(self, target_t: float, current_t: float) -> str:
        target = self._validate_field(target_t)
        self.select_field_units()
        low, high = self.get_limits_t()
        # Lead zeroing can leave IOUT outside the previous sweep limits. The
        # stored magnet readback may also lie just beyond the previous endpoint.
        # Keep the opposite limit on the current side of the target before
        # programming the endpoint; otherwise APS100 rejects crossed limits.
        upward = target >= current_t
        if target == current_t and target <= low:
            upward = False
        if upward:
            if target < low:
                self.set_limits_t(current_t, target)
            else:
                self._write(f"ULIM {self._tesla_to_kg(target):.6f}")
            actual = field_response_to_tesla(
                self._query("ULIM?"), self.coil_constant_t_per_a
            )
            direction = "UP"
        else:
            if target > high:
                self.set_limits_t(target, current_t)
            else:
                self._write(f"LLIM {self._tesla_to_kg(target):.6f}")
            actual = field_response_to_tesla(
                self._query("LLIM?"), self.coil_constant_t_per_a
            )
            direction = "DOWN"
        if abs(actual - target) > 2e-4:
            raise APS100Error(
                f"APS100 target verification failed: requested {target:g} T, read {actual:g} T"
            )
        self._last_target_t = target
        return direction

    def start_sweep_to(self, target_t: float) -> str:
        self._ensure_no_fault()
        if self.get_heater_status() is not True:
            raise APS100SafetyError("Field sweep requires driven mode (heater ON)")
        current = self.get_field_t()
        direction = self._set_directional_target(target_t, current)
        self._write(f"SWEEP {direction} SLOW")
        return direction.lower()

    def _start_output_sweep_to(self, target_t: float) -> str:
        self._ensure_no_fault()
        if self.get_heater_state() != 0:
            raise APS100SafetyError(
                "Fast lead matching requires APS100 heater OFF confirmation"
            )
        self.get_fast_rate_a_per_s()
        current = self.get_output_field_t()
        direction = self._set_directional_target(target_t, current)
        self._write(f"SWEEP {direction} FAST")
        return direction.lower()

    def pause(self, *, confirm: bool = True, timeout_s: float = 3.0) -> None:
        self._write("SWEEP PAUSE")
        if not confirm:
            return
        deadline = time.monotonic() + max(0.1, float(timeout_s))
        last_state = ""
        last_status = None
        while time.monotonic() < deadline:
            state = self.get_sweep_state()
            status = self.get_status()
            last_state = state
            last_status = status
            # The APS100 manual documents "sweep paused". Firmware 1.67.323
            # also returns "standby" or the short form "pause" after
            # SWEEP PAUSE. In any case the sweep-active status bit must be
            # clear before the pause is safe.
            paused_state = "pause" in state or "standby" in state
            if paused_state and not status.sweep_active:
                return
            self._sleep(0.05)
        status_text = (
            f"STB={last_status.raw} (sweep_active={last_status.sweep_active}, "
            f"standby={last_status.standby})"
            if last_status is not None
            else "STB unavailable"
        )
        raise APS100TimeoutError(
            "APS100 did not confirm sweep paused: "
            f"SWEEP?={last_state!r}, {status_text}"
        )

    def wait_for_field(
        self,
        target_t: float,
        *,
        tolerance_t: float = 0.002,
        timeout_s: float = 900.0,
        read_output: bool = False,
        stop_event=None,
        progress: Optional[Callable[[float], None]] = None,
    ) -> float:
        target = self._validate_field(target_t)
        started = time.monotonic()
        deadline = started + float(timeout_s)
        value = None
        last_progress = started
        previous = None
        reader = self.get_output_field_t if read_output else self.get_field_t
        while True:
            now = time.monotonic()
            if now >= deadline:
                if now - last_progress >= 180.0:
                    break
                deadline = now + 180.0
            if stop_event is not None and stop_event.is_set():
                self.pause(confirm=False)
                raise APS100OperationCancelled(stop_event)
            self._ensure_no_fault()
            value = reader()
            if not math.isfinite(value):
                self.pause(confirm=False)
                raise APS100SafetyError("Nonfinite field during magnet movement")
            if previous is None or abs(value - target) < abs(previous - target) - 0.0001:
                previous, last_progress = value, time.monotonic()
            if progress is not None:
                progress(value)
            if abs(value - target) <= abs(float(tolerance_t)):
                self.pause()
                return value
            state = self.get_sweep_state()
            if (
                "pause" in state or "standby" in state
            ) and not self.get_status().sweep_active:
                raise APS100SafetyError(
                    f"APS100 paused at {value:.6g} T before reaching {target:.6g} T"
                )
            self._sleep(0.1)
        self.pause(confirm=False)
        raise APS100TimeoutError(
            f"Timed out moving magnet to {target:g} T; "
            f"last field {value} T, elapsed {time.monotonic() - started:.1f} s, "
            f"timeout {timeout_s:g} s"
        )

    def move_to_field(
        self,
        target_t: float,
        *,
        tolerance_t: float = 0.002,
        timeout_s: float = 900.0,
        stop_event=None,
        progress: Optional[Callable[[float], None]] = None,
    ) -> float:
        self.start_sweep_to(target_t)
        return self.wait_for_field(
            target_t,
            tolerance_t=tolerance_t,
            timeout_s=timeout_s,
            stop_event=stop_event,
            progress=progress,
        )

    def zero_output(
        self,
        *,
        tolerance_t: Optional[float] = None,
        timeout_s: float = 900.0,
        verify_persistent_switch: bool = False,
        max_magnet_voltage_v: Optional[float] = None,
        stop_event=None,
        progress: Optional[Callable[[float], None]] = None,
        transition_progress: Optional[Callable[[str, float], None]] = None,
    ) -> float:
        """Use the APS100 ZERO operation and require its automatic Standby state."""
        if tolerance_t is None:
            tolerance_t = self.lead_zero_tolerance_t
        if not math.isfinite(float(tolerance_t)) or float(tolerance_t) <= 0:
            raise ValueError("zero tolerance must be positive and finite")
        initial_output = self.get_output_field_t()
        if not math.isfinite(initial_output):
            raise APS100SafetyError("Nonfinite output before ZERO")
        if abs(initial_output) <= abs(float(tolerance_t)):
            return initial_output
        if verify_persistent_switch:
            if max_magnet_voltage_v is None or not math.isfinite(
                float(max_magnet_voltage_v)
            ) or float(max_magnet_voltage_v) <= 0:
                raise APS100SafetyError(
                    "Persistent lead zeroing requires a commissioned positive "
                    "VMAG safety limit"
                )
            if self.get_heater_state() != 0:
                raise APS100SafetyError("Fast lead zeroing requires heater OFF")
        self._ensure_no_fault()
        self._write("SWEEP ZERO SLOW")
        if transition_progress is not None:
            transition_progress("zeroing leads SLOW: checking persistent voltage", initial_output)
        deadline = time.monotonic() + float(timeout_s)
        last_progress = time.monotonic()
        previous = abs(initial_output)
        fast_selected = False
        while True:
            now = time.monotonic()
            if now >= deadline:
                if now - last_progress >= 180.0:
                    break
                deadline = now + 180.0
            if stop_event is not None and stop_event.is_set():
                self.pause(confirm=False)
                raise APS100OperationCancelled(stop_event)
            self._ensure_no_fault()
            output_t = self.get_output_field_t()
            if not math.isfinite(output_t):
                self.pause(confirm=False)
                raise APS100SafetyError("Nonfinite output while zeroing")
            if abs(output_t) < previous - 0.0001:
                previous, last_progress = abs(output_t), time.monotonic()
            if verify_persistent_switch:
                if self.get_heater_state() != 0:
                    self.pause(confirm=False)
                    raise APS100SafetyError("Heater changed state while zeroing leads")
                magnet_v = abs(self.get_magnet_voltage_v())
                if not math.isfinite(magnet_v) or magnet_v > float(max_magnet_voltage_v):
                    self.pause(confirm=False)
                    raise APS100SafetyError(
                        f"VMAG {magnet_v:.6g} V exceeded the commissioned "
                        f"{float(max_magnet_voltage_v):.6g} V limit while zeroing leads"
                    )
            if progress is not None:
                progress(output_t)
            state = self.get_sweep_state()
            status = self.get_status()
            at_zero = abs(output_t) <= abs(float(tolerance_t))
            if at_zero and status.standby and not status.sweep_active:
                return output_t
            if verify_persistent_switch and not fast_selected and not at_zero:
                rate = self.get_fast_rate_a_per_s()
                expected_s = abs(output_t) / (rate * self.coil_constant_t_per_a)
                deadline = max(deadline, time.monotonic() + expected_s * 2.0 + 180.0)
                self._write("SWEEP ZERO FAST")
                if transition_progress is not None:
                    transition_progress(f"zeroing leads FAST: {rate:g} A/s (RATE 5)", output_t)
                fast_selected = True
                self._sleep(0.1)
                continue
            if not status.sweep_active and "zero" not in state:
                raise APS100SafetyError(
                    "APS100 stopped without confirming Standby after SWEEP ZERO: "
                    f"IOUT={output_t:.6g} T, SWEEP?={state!r}, STB={status.raw}"
                )
            self._sleep(0.1)
        self.pause(confirm=False)
        raise APS100TimeoutError("Timed out waiting for SWEEP ZERO and Standby")

    def _hold_transition(
        self, seconds: float, *, label: str,
        progress: Optional[Callable[[str, float], None]] = None,
    ) -> None:
        self.thermal_dwell_active = True
        try:
            self._hold_transition_impl(seconds, label=label, progress=progress)
        finally:
            self.thermal_dwell_active = False

    def _hold_transition_impl(
        self,
        seconds: float,
        *,
        label: str,
        progress: Optional[Callable[[str, float], None]] = None,
    ) -> None:
        duration = max(0.0, float(seconds))
        deadline = time.monotonic() + duration
        while True:
            remaining = max(0.0, deadline - time.monotonic())
            if progress is not None:
                progress(label, remaining)
            if remaining <= 0:
                return
            self._ensure_no_fault()
            self._sleep(min(0.5, remaining))

    def _confirm_heater_state(
        self,
        expected: int,
        *,
        timeout_s: float,
        stop_event=None,
        progress: Optional[Callable[[str, float], None]] = None,
        label: str,
    ) -> int:
        """Poll PSHTR? until a stable requested state is confirmed.

        APS100 firmware 1.67 reports ``2`` while the persistent-switch heater
        is changing state.  State 2 (and the opposite stable state) is safe
        to observe while waiting, but neither may start a warm/cool dwell.
        """
        expected = int(expected)
        if expected not in {0, 1}:
            raise ValueError("heater confirmation target must be 0 or 1")
        timeout = min(
            max(0.1, float(timeout_s)),
            max(0.1, float(self.heater_transition_timeout_s)),
        )
        deadline = time.monotonic() + timeout
        last_state: Optional[int] = None
        while True:
            self._raise_if_stopped(stop_event)
            # This also verifies the VISA/session state and APS fault bits on
            # every confirmation poll; failures remain fail-closed.
            self._ensure_no_fault()
            state = self.get_heater_state()
            last_state = state
            remaining = max(0.0, deadline - time.monotonic())
            if progress is not None:
                progress(label, remaining)
            if state == expected:
                return state
            if remaining <= 0.0:
                break
            self._sleep(min(0.2, remaining))
        state_text = "unknown" if last_state is None else str(last_state)
        target_text = "ON" if expected == 1 else "OFF"
        raise APS100TimeoutError(
            f"Timed out confirming APS100 heater {target_text}; last PSHTR?={state_text}"
        )

    @staticmethod
    def _raise_if_stopped(stop_event) -> None:
        if stop_event is not None and stop_event.is_set():
            raise APS100OperationCancelled(stop_event)

    def _settled_current_match(self, *, heater_state, action, stop_event=None, progress=None):
        """Require two matched readings after pause, without relaxing the limit."""
        tolerance = float(self.current_match_tolerance_a)
        if not math.isfinite(tolerance) or tolerance <= 0:
            raise APS100SafetyError("Current-match tolerance must be positive and finite")
        deadline = time.monotonic() + 5.0
        matched = 0
        mismatch_a = float("inf")
        for _ in range(20):
            self._raise_if_stopped(stop_event)
            self._ensure_no_fault()
            if self.get_heater_state() != heater_state:
                raise APS100SafetyError(f"Cannot {action} heater: switch state changed during current verification")
            magnet_t = self.get_field_t()
            output_t = self.get_output_field_t()
            if not math.isfinite(magnet_t) or not math.isfinite(output_t):
                raise APS100SafetyError("Nonfinite field readback during current matching")
            mismatch_a = abs(output_t - magnet_t) / self.coil_constant_t_per_a
            matched = matched + 1 if mismatch_a <= tolerance else 0
            if progress is not None:
                progress(f"Current mismatch {mismatch_a * 1000:.3f} mA; allowed {tolerance * 1000:g} mA; verifying stable match", mismatch_a)
            if matched >= 2:
                return True, magnet_t, output_t, mismatch_a
            if time.monotonic() >= deadline:
                break
            self._sleep(0.25)
        return False, magnet_t, output_t, mismatch_a

    def enter_driven_mode(
        self,
        *,
        tolerance_t: float = 0.002,
        timeout_s: float = 900.0,
        stop_event=None,
        progress: Optional[Callable[[str, float], None]] = None,
    ) -> None:
        """Safely match the leads, turn the heater on, and wait for warm-up."""
        self._ensure_no_fault()
        heater_state = self.get_heater_state()
        if heater_state == 1:
            return
        if heater_state == 2:
            self._confirm_heater_state(
                1,
                timeout_s=timeout_s,
                stop_event=stop_event,
                progress=progress,
                label="heater transition to ON",
            )
            self._hold_transition(self.heater_warm_s, label="heater warming", progress=progress)
            self._ensure_no_fault()
            self._raise_if_stopped(stop_event)
            return
        magnet_t = self.get_field_t()
        output_t = self.get_output_field_t()
        tolerance_a = self.current_match_tolerance_a
        if not math.isfinite(tolerance_a) or tolerance_a <= 0.0:
            raise APS100SafetyError("Current-match tolerance must be positive and finite")
        requested_tolerance_t = abs(float(tolerance_t))
        if not math.isfinite(requested_tolerance_t) or requested_tolerance_t <= 0.0:
            raise APS100SafetyError("Field tolerance must be positive and finite")
        for attempt in range(3):
            if abs(output_t - magnet_t) / self.coil_constant_t_per_a > tolerance_a:
                # RATE? 5 is read-only verification here. FAST is permitted by the
                # APS100 only while the persistent heater is confirmed OFF.
                if self.get_heater_state() != 0:
                    raise APS100SafetyError(
                        "Fast lead matching requires APS100 heater OFF confirmation"
                    )
                fast_rate_a_per_s = self.get_fast_rate_a_per_s()
                fast_rate_t_per_s = fast_rate_a_per_s * self.coil_constant_t_per_a
                mismatch_t = abs(output_t - magnet_t)
                expected_s = mismatch_t / fast_rate_t_per_s
                # A configured slow fast-rate must not hit the old fixed 900 s
                # cap while making normal progress. Allow several serial reads.
                match_timeout_s = max(float(timeout_s), expected_s * 2.0 + 30.0)
                self._start_output_sweep_to(magnet_t)
                self.wait_for_field(
                    magnet_t,
                    tolerance_t=min(requested_tolerance_t, tolerance_a * self.coil_constant_t_per_a),
                    timeout_s=match_timeout_s,
                    read_output=True,
                    stop_event=stop_event,
                    progress=(
                        (lambda value: progress("matching leads", value))
                        if progress is not None else None
                    ),
                )
            self.pause()
            matched, magnet_t, output_t, mismatch_a = self._settled_current_match(
                heater_state=0, action="enable", stop_event=stop_event, progress=progress,
            )
            if matched:
                break
            if attempt == 2:
                raise APS100SafetyError(
                    f"Cannot enable heater: current mismatch is {mismatch_a:.6g} A "
                    f"after settling and 2 rematching attempts (limit {tolerance_a:g} A)"
                )
            if progress is not None:
                progress("retrying lead matching", float(attempt + 1))
        self._raise_if_stopped(stop_event)
        self._write("PSHTR ON")
        self._confirm_heater_state(
            1,
            timeout_s=timeout_s,
            stop_event=stop_event,
            progress=progress,
            label="heater transition to ON",
        )
        self._hold_transition(
            self.heater_warm_s,
            label="heater warming",
            progress=progress,
        )
        self._ensure_no_fault()
        # A stop requested while the heater was warming is honored only after
        # the full warm-up interval, leaving the switch in a known safe state.
        self._raise_if_stopped(stop_event)

    def enter_persistent_mode(
        self,
        *,
        zero_leads: bool = True,
        timeout_s: float = 900.0,
        max_magnet_voltage_v: Optional[float] = None,
        stop_event=None,
        progress: Optional[Callable[[str, float], None]] = None,
        target_t: Optional[float] = None,
        tolerance_t: float = 0.002,
        settle_s: float = 2.0,
    ) -> None:
        """Pause, turn the heater off, cool, and optionally zero the leads."""
        self._ensure_no_fault()
        status = self.get_status()
        if status.sweep_active:
            self.pause()
        try:
            heater_state = self.get_heater_state()
        except APS100Error as exc:
            # A malformed status cannot authorize lead zeroing. Still issue
            # the conservative OFF command once so cleanup attempts to leave
            # the switch safe; the original parse error remains fatal and no
            # cooling/zeroing path is entered without later confirmation.
            try:
                self._write("PSHTR OFF")
            except Exception as off_exc:
                raise APS100SafetyError(
                    f"Invalid PSHTR? response during persistent cleanup; OFF command failed: {off_exc}"
                ) from exc
            raise APS100SafetyError(
                f"Invalid PSHTR? response during persistent cleanup; heater OFF is unconfirmed: {exc}"
            ) from exc
        if heater_state == 0:
            if zero_leads and abs(self.get_output_field_t()) > self.lead_zero_tolerance_t:
                self.zero_output(
                    tolerance_t=self.lead_zero_tolerance_t,
                    timeout_s=timeout_s,
                    transition_progress=progress,
                    verify_persistent_switch=True,
                    max_magnet_voltage_v=max_magnet_voltage_v,
                    stop_event=stop_event,
                    progress=(
                        (lambda value: progress("zeroing leads", value))
                        if progress is not None else None
                    ),
                )
            return
        if target_t is not None:
            # Target accuracy and current matching must hold together in the
            # last readings before OFF, not in separate settling windows.
            self._settle_persistent_target(
                target_t, tolerance_t, settle_s, timeout_s,
                stop_event=stop_event, progress=progress,
            )
            matched = True
        else:
            matched, magnet_t, output_t, mismatch_a = self._settled_current_match(
                heater_state=1, action="disable", stop_event=stop_event, progress=progress,
            )
        if not matched:
            raise APS100SafetyError(
                f"Cannot disable heater: current mismatch is {mismatch_a:.6g} A "
                f"after settling (limit {self.current_match_tolerance_a:g} A)"
            )
        self._raise_if_stopped(stop_event)
        self._write("PSHTR OFF")
        self._confirm_heater_state(
            0,
            timeout_s=timeout_s,
            stop_event=stop_event,
            progress=progress,
            label="heater transition to OFF",
        )
        self._hold_transition(
            self.heater_cool_s,
            label="heater cooling",
            progress=progress,
        )
        # Do not interrupt the mandatory cooling dwell. A stop at this point
        # leaves the magnet persistent with the leads still matched.
        self._raise_if_stopped(stop_event)
        if zero_leads:
            self.zero_output(
                tolerance_t=self.lead_zero_tolerance_t,
                timeout_s=timeout_s,
                transition_progress=progress,
                verify_persistent_switch=True,
                max_magnet_voltage_v=max_magnet_voltage_v,
                stop_event=stop_event,
                progress=(
                    (lambda value: progress("zeroing leads", value))
                    if progress is not None else None
                ),
            )

    def _settle_persistent_target(self, target, tolerance, settle_s, timeout_s, *, stop_event=None, progress=None):
        """Verify a stable target before trapping flux; allow two driven corrections."""
        target = self._validate_field(target)
        if not math.isfinite(tolerance) or tolerance <= 0:
            raise APS100SafetyError("Field tolerance must be positive and finite")
        if not math.isfinite(settle_s) or settle_s < 0:
            raise APS100SafetyError("Field settling time must be nonnegative and finite")
        if not math.isfinite(self.current_match_tolerance_a) or self.current_match_tolerance_a <= 0:
            raise APS100SafetyError("Current-match tolerance must be positive and finite")
        # Reserve half of the final acceptance band for switch-time drift.
        inner = tolerance / 2.0
        reason = "No stable target readings"
        for attempt in range(3):
            deadline = time.monotonic() + max(5.0, settle_s + 5.0)
            stable_since = None
            fields, outputs = [], []
            for _ in range(max(40, int(settle_s / 0.25) + 40)):
                self._raise_if_stopped(stop_event)
                status = self._ensure_no_fault()
                if status.sweep_active or self.get_heater_state() != 1:
                    raise APS100SafetyError("Persistent target verification requires paused driven mode")
                field, output = self.get_field_t(), self.get_output_field_t()
                if not all(math.isfinite(v) for v in (field, output)):
                    raise APS100SafetyError("Nonfinite field during persistent target verification")
                mismatch = abs(field - output) / self.coil_constant_t_per_a
                reason = (f"field {field:.6g} T; supply {output:.6g} T; target {target:g} T; "
                          f"pre-switch tolerance {inner:g} T; current mismatch {mismatch:g} A")
                if max(abs(field - target), abs(output - target)) <= inner and mismatch <= self.current_match_tolerance_a:
                    fields.append(field)
                    outputs.append(output)
                    if stable_since is None:
                        stable_since = time.monotonic()
                    if max(max(fields) - min(fields), max(outputs) - min(outputs)) > inner / 2.0:
                        fields, outputs = [field], [output]
                        stable_since = time.monotonic()
                    if len(fields) >= 3 and time.monotonic() - stable_since >= settle_s:
                        return
                else:
                    fields, outputs, stable_since = [], [], None
                if progress is not None:
                    progress("Verifying persistent target: " + reason, field)
                if time.monotonic() >= deadline:
                    break
                self._sleep(0.25)
            if attempt == 2:
                break
            if mismatch > self.current_match_tolerance_a:
                raise APS100SafetyError("Cannot correct persistent target: current mismatch after settling; " + reason)
            self._raise_if_stopped(stop_event)
            self._ensure_no_fault()
            if self.get_heater_state() != 1:
                raise APS100SafetyError("Cannot correct target: heater is not ON")
            if progress is not None:
                progress(f"Correcting persistent target ({attempt + 1}/2): " + reason, field)
            # Use the normal driven ramp and its configured rates, never the
            # FAST lead-matching path while the switch is open.
            self.move_to_field(target, tolerance_t=inner / 2.0, timeout_s=timeout_s,
                               stop_event=stop_event,
                               progress=(lambda value: progress("correcting field", value)) if progress else None)
        raise APS100SafetyError("Persistent target not stable after 2 corrections; heater OFF withheld: " + reason)

    def _verify_final_readings(self, target, mode, zero_leads, tolerance, *, stop_event=None, progress=None):
        deadline = time.monotonic() + 5.0
        consecutive = 0
        reason = "No stable readings"
        for _ in range(20):
            self._raise_if_stopped(stop_event)
            self._ensure_no_fault()
            snapshot = self.read_snapshot()
            if snapshot.status.quench or snapshot.status.power_module_failure:
                raise APS100SafetyError("APS100 hardware fault in final readings")
            if snapshot.heater_on is not (mode == "driven"):
                raise APS100SafetyError(f"Final APS100 state is not {mode} mode")
            if snapshot.status.sweep_active:
                raise APS100SafetyError("Final APS100 sweep is still active")
            if not all(math.isfinite(v) for v in (snapshot.field_t, snapshot.output_field_t, snapshot.output_current_a)):
                raise APS100SafetyError("Nonfinite final APS100 readings")
            reason = ""
            if abs(snapshot.field_t - target) > abs(tolerance):
                reason = f"field {snapshot.field_t:.6g} T; target {target:g} T; tolerance {tolerance:g} T"
            elif mode == "persistent" and zero_leads and (
                abs(snapshot.output_field_t) > self.lead_zero_tolerance_t or abs(snapshot.output_current_a) > self.lead_zero_tolerance_t / self.coil_constant_t_per_a
            ):
                reason = f"lead current {snapshot.output_current_a:.6g} A; zero limit {self.lead_zero_tolerance_t / self.coil_constant_t_per_a:g} A"
            consecutive = consecutive + 1 if not reason else 0
            if consecutive >= 2:
                return snapshot
            if progress is not None:
                progress("Waiting for stable final readings: " + (reason or "confirming second reading"), 0.0)
            if time.monotonic() >= deadline:
                break
            self._sleep(0.25)
        raise APS100SafetyError("Measurement not ready after final settling: " + reason)

    def _hysteresis_field_move(self, target, direction, tolerance, timeout, stop_event, progress, options, history):
        """Run a directed sweep and validate every acquired field observation."""
        try:
            acceptance = float(options["acceptance_t"])
            reversal = float(options["reversal_t"])
            lower, upper = float(options["lower_t"]), float(options["upper_t"])
            no_progress = float(options["no_progress_s"])
        except (KeyError, TypeError, ValueError) as exc:
            raise APS100SafetyError("Invalid hysteresis move options") from exc
        if (not all(math.isfinite(v) for v in (acceptance, reversal, lower, upper, no_progress))
                or acceptance < tolerance or reversal <= 0 or no_progress <= 0
                or lower >= upper or abs(lower) > self.maximum_field_t or abs(upper) > self.maximum_field_t
                or not lower <= target <= upper or direction not in (-1, 1)):
            raise APS100SafetyError("Unsafe hysteresis acceptance, history limits, or direction")
        preparation = options.get("preparation") is True
        polling_profile = options.get("polling_profile", "standard")
        if polling_profile not in {"standard", "reduced_mode_queries"}:
            raise APS100SafetyError("Invalid APS100 polling comparison profile")
        reduced_polling = polling_profile == "reduced_mode_queries" and not preparation
        approach = options.get("approach", "no_overshoot")
        if approach not in {"no_overshoot", "linear_no_backward"}:
            raise APS100SafetyError("Invalid hysteresis approach")
        no_overshoot_profile = options.get("no_overshoot_profile", "standard")
        if no_overshoot_profile not in {"standard", "short_tail"}:
            raise APS100SafetyError("Invalid APS100 No Overshoot profile")
        if no_overshoot_profile != "standard" and approach != "no_overshoot":
            raise APS100SafetyError("Shorter slow approach requires No Overshoot")
        direct = approach == "linear_no_backward" and not preparation
        if preparation:
            try:
                stability = float(options["stability_s"])
                span_limit = float(options["span_t"])
                drift_limit = float(options["drift_t_per_s"])
                stability_timeout = float(options["stability_timeout_s"])
            except (KeyError, TypeError, ValueError) as exc:
                raise APS100SafetyError("Invalid preparation stability options") from exc
            if (not all(math.isfinite(v) for v in (stability, span_limit, drift_limit, stability_timeout))
                    or stability <= 0 or span_limit <= 0 or drift_limit <= 0 or stability_timeout < stability):
                raise APS100SafetyError("Unsafe preparation stability options")
        start = time.monotonic()
        deadline = start + float(timeout)
        if not history:
            history.update(direction=direction, started_monotonic_s=start, finished_monotonic_s=None,
                           min_t=None, max_t=None, max_reverse_t=0.0, read_count=0,
                           start_t=None, end_t=None, complete=False)
        elif history.get("direction") != direction:
            raise APS100SafetyError("History direction disagrees with requested direction")
        diagnostics = _FieldMoveDiagnostics(history, start)
        query_counts_before = dict(self._query_counts)
        polling = dict(requested_profile=polling_profile,
                       effective_profile="reduced_mode_queries" if reduced_polling else "standard",
                       mode_check_interval_s=1.0 if reduced_polling else 0.0,
                       iterations=0, mode_checks=0, status_checks=0,
                       shared_field_reads=0,
                       started_monotonic_s=None, finished_monotonic_s=None)
        history["polling_diagnostics"] = polling
        history["approach"] = approach
        reversal_warnings = _ReversalWarnings(history)
        peak = (None if history.get("min_t") is None else
                direction * (history["max_t"] if direction > 0 else history["min_t"]))
        active_rates = None
        def observe_value(field, observed_at=None):
            nonlocal peak
            field = float(field)
            if not math.isfinite(field):
                raise APS100SafetyError("Nonfinite field history readback")
            if history["start_t"] is None:
                history["start_t"] = field
            history["end_t"] = field
            history["read_count"] += 1
            history["min_t"] = field if history["min_t"] is None else min(history["min_t"], field)
            history["max_t"] = field if history["max_t"] is None else max(history["max_t"], field)
            diagnostics.observe(field, time.monotonic() if observed_at is None else observed_at)
            oriented = direction * field
            peak = oriented if peak is None else max(peak, oriented)
            history["max_reverse_t"] = max(history["max_reverse_t"], peak - oriented)
            if field < lower - 1e-12 or field > upper + 1e-12:
                raise APS100FieldHistoryError(f"Field {field:g} T outside approved range [{lower:g}, {upper:g}] T")
            if not preparation:
                reversal_warnings.observe(field, peak, direction, reversal,
                                          time.monotonic() if observed_at is None else observed_at)
            if preparation and active_rates is not None:
                current_a = abs(field) / self.coil_constant_t_per_a
                rate = next((r for b, r in active_rates.values() if current_a <= b + 1e-9), None)
                if rate is None or not math.isfinite(rate) or rate <= 0:
                    raise APS100SafetyError("No valid active APS100 rate for preparation")
                budget = max(no_progress, .0001 * 60.0 / rate * 1.5)
                excursion = max(tolerance, .0001) + rate * budget / 60.0
                history["preparation_last_excursion_limit_t"] = excursion
                history["preparation_min_excursion_limit_t"] = min(
                    history.get("preparation_min_excursion_limit_t", excursion), excursion)
                history["preparation_max_excursion_limit_t"] = max(
                    history.get("preparation_max_excursion_limit_t", excursion), excursion)
                if peak - oriented > excursion + 1e-12:
                    raise APS100FieldHistoryError(f"Preparation reversal exceeds rate-based bound {excursion:g} T")
            return field
        def observe():
            return observe_value(self.get_field_t())
        def confirm_pause():
            self.pause()
            if reduced_polling:
                # Stop first, then fully verify mode at the stage boundary.
                self._ensure_no_fault()
            return observe()
        def accept(field):
            nonlocal changed, active_rates
            if abs(field - target) > acceptance + 1e-12:
                if not preparation and direction * (field - target) > acceptance + 1e-12:
                    raise APS100QualityOvershoot(f"Paused field {field:g} T passed target {target:g} T")
                raise APS100SafetyError(f"Field stopped {abs(field-target):g} T from target, outside acceptance {acceptance:g} T")
            # Restore before taking the completion snapshot: these serial
            # transactions can outlast the transport telemetry freshness limit.
            # A snapshot captured first would be stale by the time it is emitted.
            if changed:
                self.restore_rates(saved)
                changed = False
                active_rates = saved
            if preparation:
                # Preparation stability must also be qualified after restoration.
                stable_deadline = min(deadline, time.monotonic() + stability_timeout)
                values = []
                def qualified():
                    if len(values) < 3 or values[-1][0]-values[0][0] < stability:
                        return False
                    xmean = sum(t-values[0][0] for t, _ in values) / len(values)
                    ymean = sum(v for _, v in values) / len(values)
                    denom = sum((t-values[0][0]-xmean)**2 for t, _ in values)
                    drift = (sum((t-values[0][0]-xmean)*(v-ymean) for t, v in values) / denom
                             if denom else math.inf)
                    return (max(v for _, v in values)-min(v for _, v in values) <= span_limit + 1e-12
                            and abs(drift) <= drift_limit + 1e-12)
                while True:
                    self._raise_if_stopped(stop_event)
                    status = self._ensure_no_fault()
                    if status.sweep_active or self.get_heater_state() != 1:
                        raise APS100SafetyError("Preparation stability requires paused driven mode")
                    field = observe()
                    now = time.monotonic()
                    if now >= stable_deadline:
                        raise APS100TimeoutError("Preparation field stability timeout")
                    if abs(field - target) <= acceptance + 1e-12:
                        if values and now-values[-1][0] > 3.:
                            values.clear()
                        values.append((now, field))
                        while len(values) > 3 and values[1][0] <= now - stability:
                            values.pop(0)
                        if qualified():
                            snapshot = self.read_snapshot()
                            observed = observe_value(snapshot.field_t)
                            now = time.monotonic()
                            if now >= stable_deadline:
                                raise APS100TimeoutError("Preparation field stability timeout")
                            if (snapshot.status.sweep_active or snapshot.status.faulted
                                    or snapshot.heater_on is not True):
                                raise APS100SafetyError("Preparation snapshot is not paused and driven")
                            if abs(observed-target) <= acceptance + 1e-12:
                                if now-values[-1][0] > 3.:
                                    values.clear()
                                values.append((now, observed))
                                while len(values) > 3 and values[1][0] <= now - stability:
                                    values.pop(0)
                                if qualified():
                                    history["preparation_stable"] = True
                                    return snapshot
                            else:
                                values.clear()
                    else:
                        values.clear()
                    if progress:
                        progress("preparation field", field)
                    self._sleep(.1)
            snapshot = self.read_snapshot()
            observed = observe_value(getattr(snapshot, "field_t", snapshot))
            if abs(observed - target) > acceptance + 1e-12:
                if direction * (observed - target) > acceptance + 1e-12:
                    raise APS100QualityOvershoot(f"Paused field {observed:g} T passed target {target:g} T")
                raise APS100SafetyError("Paused snapshot is outside field acceptance")
            return snapshot
        saved = None
        changed = False
        try:
            current = observe()
            saved = self.get_rates()
            active_rates = saved
            history["start_rates_t_per_min"] = {str(i): list(entry) for i, entry in saved.items()}
            distance = direction * (target - current)
            if approach == "no_overshoot":
                def band_at(field):
                    return next((i for i, (boundary, _) in saved.items()
                                 if abs(field) / self.coil_constant_t_per_a <= boundary + 1e-9), None)
                initial_band, target_band = band_at(current), band_at(target)
                intermediate, approach_info = no_overshoot_intermediate(
                    current, target, tolerance, no_overshoot_profile, preparation=preparation,
                    same_rate_band=initial_band is not None and initial_band == target_band)
                if preparation:
                    # Preparation generates its own progressive endpoints.
                    approach_info.pop("intermediate_t", None)
                    approach_info.pop("planned_slow_distance_t", None)
                history["no_overshoot_diagnostics"] = approach_info
            if distance <= tolerance:
                if direct:
                    self._raise_if_stopped(stop_event)
                    self._ensure_no_fault()
                return accept(confirm_pause())
            zero_start_slow = not preparation and not direct and abs(current) <= tolerance
            if zero_start_slow:
                history["zero_start_slow"] = True
                history["zero_start_rates_t_per_min"] = reduced_rate_table(saved, self.coil_constant_t_per_a, .2)
            def stages():
                if direct:
                    # One directional command at the verified per-run rate.
                    # All observation, acceptance and safety checks stay shared.
                    yield target, 1.0
                    return
                if not preparation:
                    if direction * (intermediate-current) > tolerance and direction * (target-intermediate) > tolerance:
                        yield intermediate, .2 if zero_start_slow else 1.0
                    yield target, .2
                    return
                index = 0
                previous_endpoint = None
                while True:
                    position = observe()
                    remaining = direction * (target-position)
                    final = remaining <= max(4. * tolerance, .0004)
                    endpoint = target if final else round((position + direction * remaining * .7) / .0001) * .0001
                    if previous_endpoint is not None and not final:
                        endpoint = direction * max(direction * endpoint, direction * previous_endpoint + .0001)
                        if direction * (target-endpoint) <= tolerance:
                            endpoint, final = target, True
                    if direction * (endpoint-position) <= tolerance or direction * (target-endpoint) <= tolerance:
                        endpoint, final = target, True
                    yield endpoint, max(.2, .7 ** index)
                    if final:
                        return
                    previous_endpoint = endpoint
                    index += 1
            for endpoint, rate_factor in stages():
                self._raise_if_stopped(stop_event)
                if time.monotonic() >= deadline:
                    raise APS100TimeoutError("Directional field move timed out before stage")
                self._ensure_no_fault()
                if rate_factor < 1.0:
                    reduced = reduced_rate_table(saved, self.coil_constant_t_per_a, rate_factor)
                    changed = True
                    self.restore_rates(reduced)
                    active_rates = reduced
                else:
                    active_rates = saved
                actual = observe()
                output = float(self.get_output_field_t())
                if not math.isfinite(output):
                    raise APS100SafetyError("Nonfinite output field before sweep")
                current_a = abs(actual) / self.coil_constant_t_per_a
                rate = next((r for b, r in active_rates.values() if current_a <= b + 1e-9), None)
                diagnostics.begin_stage(endpoint, rate_factor, actual, rate)
                if direction * (target - actual) <= tolerance:
                    diagnostics.end_stage(actual, time.monotonic(), "already_there")
                    return accept(confirm_pause())
                if direction * (endpoint - actual) <= tolerance:
                    diagnostics.end_stage(actual, time.monotonic(), "already_there")
                    continue
                if direction * (endpoint - output) < -tolerance:
                    raise APS100FieldHistoryError("Output crossed the directional stage target")
                expected = "UP" if direction > 0 else "DOWN"
                selected = self._set_directional_target(endpoint, actual)
                fresh = observe()
                current_a = abs(fresh) / self.coil_constant_t_per_a
                diagnostics.stage.update(start_field_t=fresh, active_rate_t_per_min=next(
                    (r for b, r in active_rates.values() if current_a <= b + 1e-9), None))
                output = float(self.get_output_field_t())
                if not math.isfinite(output):
                    raise APS100SafetyError("Nonfinite output field before sweep")
                if direction * (target - fresh) <= tolerance:
                    diagnostics.end_stage(fresh, time.monotonic(), "already_there")
                    return accept(confirm_pause())
                if direction * (endpoint - fresh) <= tolerance:
                    diagnostics.end_stage(fresh, time.monotonic(), "already_there")
                    continue
                if selected != expected or direction * (endpoint - output) < -tolerance:
                    raise APS100FieldHistoryError("Output crossed the directional stage before command")
                self._raise_if_stopped(stop_event)
                self._ensure_no_fault()
                if time.monotonic() >= deadline:
                    raise APS100TimeoutError("Directional field move timed out before sweep command")
                self._write(f"SWEEP {expected} SLOW")
                last_progress_at = time.monotonic()
                diagnostics.begin_sweep(last_progress_at)
                best = direction * fresh
                last_mode_check = None  # No cached mode crosses a stage boundary.
                while True:
                    self._raise_if_stopped(stop_event)
                    polling["iterations"] += 1
                    if polling["started_monotonic_s"] is None:
                        polling["started_monotonic_s"] = time.monotonic()
                    polling["status_checks"] += 1
                    check_mode = (not reduced_polling or last_mode_check is None
                                  or time.monotonic() - last_mode_check >= 1.0)
                    if check_mode:
                        polling["mode_checks"] += 1
                    observation = self._hysteresis_safety_observation(check_mode)
                    if check_mode:
                        last_mode_check = time.monotonic()
                    if observation is None:
                        actual = observe()
                    else:
                        polling["shared_field_reads"] += 1
                        actual = observe_value(observation.field_t, observed_at=observation.monotonic_s)
                    now = time.monotonic()
                    polling["finished_monotonic_s"] = now
                    if progress: progress("ramping field", actual)
                    if direction * (actual-target) >= -tolerance:
                        diagnostics.end_stage(actual, time.monotonic(), "target")
                        return accept(confirm_pause())
                    if direction * (endpoint-actual) <= tolerance:
                        diagnostics.end_stage(actual, time.monotonic(), "endpoint")
                        confirm_pause()
                        break
                    if direction * actual - best >= .0001:
                        best, last_progress_at = direction * actual, now
                    # 1 Oe is 0.0001 T. Use the active band's verified field rate.
                    current_a = abs(actual) / self.coil_constant_t_per_a
                    band_rate = next((r for b,r in active_rates.values() if current_a <= b + 1e-9), None)
                    if band_rate is None or band_rate <= 0:
                        raise APS100SafetyError("No valid active APS100 rate for field")
                    budget = max(no_progress, .0001 * 60.0 / band_rate * 1.5)
                    if now >= deadline:
                        raise APS100TimeoutError("Directional field move timed out")
                    if now - last_progress_at >= budget:
                        diagnostics.end_stage(actual, time.monotonic(), "no_progress")
                        return accept(confirm_pause())
                    self._sleep(.1)
            return accept(confirm_pause())
        except Exception:
            # Every history violation and all ordinary failures first request a
            # confirmed pause; inability to confirm remains an ordinary failure.
            diagnostics.end_stage(history["end_t"], time.monotonic())
            self.pause()
            try:
                observe()
            except APS100FieldHistoryError:
                pass
            raise
        finally:
            try:
                if changed:
                    self.restore_rates(saved)
            finally:
                try:
                    if not (preparation and history.get("preparation_stable")):
                        observe()
                    history["complete"] = True
                finally:
                    history["finished_monotonic_s"] = time.monotonic()
                    diagnostics.finish(history["finished_monotonic_s"])
                    # Full-move counts include setup and cleanup reads, not just
                    # the polling loop. No extra hardware transactions are made.
                    counts = {key: value - query_counts_before.get(key, 0)
                              for key, value in self._query_counts.items()
                              if value > query_counts_before.get(key, 0)}
                    elapsed = history["finished_monotonic_s"] - start
                    history["communication_diagnostics"] = dict(
                        query_attempts=counts, total_query_attempts=sum(counts.values()),
                        started_monotonic_s=start, elapsed_s=elapsed,
                        queries_per_s=sum(counts.values()) / elapsed if elapsed > 0 else None)

    def _monotonic_field_move(self, target, direction, tolerance, timeout, stop_event, progress):
        """Directed, two-stage sweep with a strict deadline and per-run rate restoration."""
        direction = int(direction)
        if direction not in (-1, 0, 1):
            raise APS100SafetyError("Invalid monotonic direction")
        current = self.get_field_t()
        if not math.isfinite(current):
            raise APS100SafetyError("Nonfinite field before directional sweep")
        direction = direction or (1 if target >= current else -1)
        deadline = time.monotonic() + float(timeout)
        saved = self.get_rates()
        changed = False
        try:
            distance = direction * (target - current)
            if distance < -tolerance:
                raise APS100OvershootError(f"Directional target {target:g} T exceeded at {current:g} T")
            if distance <= tolerance:
                self.pause()
                return self.read_snapshot()
            # First part uses the verified per-run table; the final part uses
            # 20% rates (never above the per-run band rate), quantized down.
            intermediate = current + direction * distance * .8
            intermediate = round(intermediate / .0001) * .0001
            if direction * (intermediate - current) > tolerance and direction * (target - intermediate) > tolerance:
                stages = [(intermediate, False), (target, True)]
            else:
                stages = [(target, True)]
            for endpoint, slow in stages:
                if time.monotonic() >= deadline:
                    raise APS100TimeoutError("Directional field move timed out before stage")
                self._raise_if_stopped(stop_event)
                self._ensure_no_fault()
                if slow:
                    unit = self.coil_constant_t_per_a * 60.0 * .0001
                    reduced = {i: (b, min(r, max(unit, math.floor((r * .2 / unit) + 1e-10) * unit))) for i, (b, r) in saved.items()}
                    changed = True
                    self.restore_rates(reduced)
                # Re-read on the device worker immediately before choosing the
                # limit and again before SWEEP. Never issue the opposite sweep.
                actual = self.get_field_t()
                output = self.get_output_field_t()
                if not math.isfinite(actual) or not math.isfinite(output):
                    raise APS100SafetyError("Nonfinite field readback before directional sweep")
                if direction * (target - actual) < -tolerance or direction * (target - output) < -tolerance:
                    raise APS100OvershootError(f"Directional target {target:g} T exceeded at {actual:g} T")
                if direction * (endpoint - actual) <= tolerance:
                    continue
                if direction * (endpoint - output) < -tolerance:
                    raise APS100OvershootError("Output passed the directional stage target")
                expected = "UP" if direction > 0 else "DOWN"
                selected = self._set_directional_target(endpoint, actual)
                fresh = self.get_field_t()
                output = self.get_output_field_t()
                if not math.isfinite(fresh) or not math.isfinite(output):
                    raise APS100SafetyError("Nonfinite field readback before sweep command")
                if selected != expected or direction * (endpoint - fresh) < -tolerance or direction * (endpoint - output) < -tolerance:
                    raise APS100OvershootError("Field passed the directional stage before the sweep command")
                self._raise_if_stopped(stop_event)
                self._ensure_no_fault()
                if time.monotonic() >= deadline:
                    raise APS100TimeoutError("Directional field move timed out before sweep command")
                self._write(f"SWEEP {expected} SLOW")
                while True:
                    self._raise_if_stopped(stop_event)
                    self._ensure_no_fault()
                    actual = self.get_field_t()
                    if not math.isfinite(actual):
                        raise APS100SafetyError("Nonfinite field during directional sweep")
                    if progress: progress("ramping field", actual)
                    if direction * (actual - target) > tolerance:
                        self.pause()
                        raise APS100OvershootError(f"Directional target {target:g} T exceeded at {actual:g} T")
                    if direction * (endpoint - actual) <= tolerance:
                        self.pause()
                        break
                    if time.monotonic() >= deadline:
                        self.pause(confirm=False)
                        raise APS100TimeoutError("Directional field move timed out")
                    self._sleep(.1)
            return self.read_snapshot()
        except APS100OvershootError:
            # Manual hold is only safe after the hardware acknowledges pause.
            # A failed confirmation propagates as an ordinary failure instead.
            self.pause()
            raise
        except Exception:
            self.pause(confirm=False)
            raise
        finally:
            if changed:
                self.restore_rates(saved)

    def approach_field(self, target_t, approach, *, tolerance_t=0.002, timeout_s=3600.,
                       stop_event=None, progress=None, snapshot_callback=None,
                       no_progress_s=180., rate_t_per_min=None, safe_limit_t=None,
                       poll_interval_s=.1):
        """Execute a bounded driven approach; each intermediate limit is verified."""
        mode = normalize_approach(approach)
        tolerance = float(tolerance_t)
        timeout = float(timeout_s)
        stall = float(no_progress_s)
        poll_interval = float(poll_interval_s)
        if not all(math.isfinite(v) and v > 0 for v in (tolerance, timeout, stall, poll_interval)):
            raise APS100SafetyError('Invalid approach tolerance or timeout')
        deadline = time.monotonic() + timeout
        self._raise_if_stopped(stop_event)
        self._ensure_no_fault()
        if self.get_heater_status() is not True:
            raise APS100SafetyError('Field approach requires driven mode')
        start = self.get_field_t()
        limit = min(float(self.maximum_field_t), float(safe_limit_t)) if safe_limit_t is not None else float(self.maximum_field_t)
        try:
            targets = approach_targets(start, target_t, tolerance, mode, limit)
        except ValueError as exc:
            raise APS100SafetyError(str(exc)) from exc
        saved = self.get_rates()
        try:
            extent = max(abs(start), abs(float(target_t)), *(abs(value) for value in targets), tolerance)
            saved = validate_rate_table(saved, self.coil_constant_t_per_a, self.maximum_current_a,
                                        self.maximum_rate_a_per_s, extent)
        except (ValueError, TypeError) as exc:
            raise APS100SafetyError(f'Invalid approach rate table: {exc}') from exc
        changed = False
        last_publish = [0.]
        restoring = [False]
        restoration_callback_error = [None]
        def publish_during_rates():
            try:
                if not restoring[0]:
                    self._raise_if_stopped(stop_event)
                    if time.monotonic() >= deadline:
                        raise APS100TimeoutError('Field approach timed out while setting rates')
                if snapshot_callback is not None and time.monotonic() - last_publish[0] >= poll_interval:
                    self._ensure_no_fault()
                    snap = self.read_snapshot()
                    snapshot_callback(replace(snap, approach_mode=mode,
                        approach_phase=('oscillate' if mode == 'oscillate' else 'approach'),
                        approach_complete=False, approach_rate_t_per_min=None))
                    last_publish[0] = time.monotonic()
            except Exception as exc:
                if not restoring[0]:
                    raise
                restoration_callback_error[0] = exc
        try:
            if rate_t_per_min is not None:
                base_rate = float(rate_t_per_min)
                if not math.isfinite(base_rate) or base_rate <= 0:
                    raise APS100SafetyError('Invalid field approach rate')
            else:
                base_rate = min(float(rate) for _, rate in saved.values())
            active_rates = saved
            def apply_table(table):
                actual = self.restore_rates(table, step_callback=publish_during_rates)
                return {i: (table[i][0], actual[i]) for i in table}
            if rate_t_per_min is not None:
                unit = self.coil_constant_t_per_a * 60. * .0001
                if base_rate < unit:
                    raise APS100SafetyError('Approach rate is below APS100 rate resolution')
                capped = {i: (boundary, min(rate, math.floor(base_rate / unit) * unit))
                          for i, (boundary, rate) in saved.items()}
                changed = True
                active_rates = apply_table(capped)
            def active_rate(field):
                current = abs(field) / self.coil_constant_t_per_a
                for i in sorted(active_rates):
                    if current <= active_rates[i][0]:
                        return active_rates[i][1]
                raise APS100SafetyError('Field exceeds configured rate ranges')
            if not targets:
                self.pause()
            for index, stage in enumerate(targets):
                self._raise_if_stopped(stop_event)
                self._ensure_no_fault()
                if time.monotonic() >= deadline:
                    raise APS100TimeoutError('Field approach timed out before stage')
                if mode == 'no_overshoot':
                    current = self.get_field_t()
                    if (target_t - start) * (target_t - current) < 0:
                        raise APS100OvershootError('No-overshoot approach crossed the final target')
                    if (target_t - start) * (stage - current) < 0:
                        continue
                if mode == 'no_overshoot' and index:
                    changed = True
                    active_rates = apply_table(reduced_rate_table(
                        active_rates, self.coil_constant_t_per_a, .5))
                if mode == 'no_overshoot':
                    direction = 1 if target_t > start else -1
                    current, output = self.get_field_t(), self.get_output_field_t()
                    if not math.isfinite(current) or not math.isfinite(output):
                        raise APS100SafetyError('Nonfinite field before approach stage')
                    if direction * (stage - current) <= 0:
                        continue
                    if direction * (stage - output) < -tolerance:
                        raise APS100OvershootError('Output passed the approach stage')
                    expected = 'UP' if direction > 0 else 'DOWN'
                    selected = self._set_directional_target(stage, current)
                    current, output = self.get_field_t(), self.get_output_field_t()
                    if (not math.isfinite(current) or not math.isfinite(output) or selected != expected
                            or direction * (stage-current) < 0 or direction * (stage-output) < -tolerance):
                        raise APS100OvershootError('Field passed approach stage before sweep')
                    self._raise_if_stopped(stop_event)
                    self._ensure_no_fault()
                    if time.monotonic() >= deadline:
                        raise APS100TimeoutError('Field approach timed out before sweep')
                    self._write(f'SWEEP {expected} SLOW')
                else:
                    current = self.get_field_t()
                    if not math.isfinite(current):
                        raise APS100SafetyError('Nonfinite field before approach stage')
                    if abs(stage-current) <= tolerance:
                        continue
                    expected = 'UP' if stage > current else 'DOWN'
                    selected = self._set_directional_target(stage, current)
                    current = self.get_field_t()
                    if not math.isfinite(current) or selected != expected or ((stage-current) * (1 if expected == 'UP' else -1) < 0):
                        raise APS100SafetyError('Field crossed approach stage before sweep')
                    self._raise_if_stopped(stop_event)
                    self._ensure_no_fault()
                    if time.monotonic() >= deadline:
                        raise APS100TimeoutError('Field approach timed out before sweep')
                    self._write(f'SWEEP {expected} SLOW')
                last_distance = None
                last_progress = time.monotonic()
                while True:
                    self._raise_if_stopped(stop_event)
                    self._ensure_no_fault()
                    snapshot = self.read_snapshot()
                    value = float(snapshot.field_t)
                    if not math.isfinite(value) or abs(value) > limit:
                        raise APS100SafetyError('Invalid field during approach')
                    if snapshot_callback is not None:
                        phase = 'oscillate' if mode == 'oscillate' else ('ramp' if index == 0 else 'approach')
                        snapshot_callback(replace(snapshot, approach_mode=mode, approach_phase=phase,
                                                  approach_complete=False, approach_rate_t_per_min=active_rate(value)))
                        last_publish[0] = time.monotonic()
                    if progress is not None:
                        progress('ramping field', value)
                    now = time.monotonic()
                    distance = abs(stage - value)
                    if last_distance is None or distance < last_distance - .0001:
                        last_distance, last_progress = distance, now
                    if mode == 'no_overshoot' and (target_t - start) * (target_t - value) < 0:
                        raise APS100OvershootError('No-overshoot approach crossed the final target')
                    if distance <= tolerance:
                        self.pause()
                        break
                    progress_budget = max(stall, .0001 * 60. / active_rate(value) * 1.5)
                    if now >= deadline or now - last_progress >= progress_budget:
                        raise APS100TimeoutError('Field approach timed out or stopped progressing')
                    if not getattr(snapshot.status, 'sweep_active', False):
                        raise APS100SafetyError('APS100 stopped before approach stage')
                    if stop_event is not None:
                        stop_event.wait(poll_interval)
                    else:
                        self._sleep(poll_interval)
        except Exception:
            self.pause(confirm=False)
            raise
        finally:
            if changed:
                restoring[0] = True
                self.restore_rates(saved, step_callback=publish_during_rates)
        if restoration_callback_error[0] is not None:
            raise restoration_callback_error[0]
        self._raise_if_stopped(stop_event)
        if time.monotonic() >= deadline:
            raise APS100TimeoutError('Field approach timed out before final verification')
        self._ensure_no_fault()
        final = self.read_snapshot()
        if (not math.isfinite(float(final.field_t)) or abs(float(final.field_t) - float(target_t)) > tolerance
                or getattr(final.status, 'sweep_active', None) is not False or final.heater_on is not True):
            raise APS100SafetyError('Final approach field or paused driven state is invalid')
        final = replace(final, approach_mode=mode, approach_phase='settled', approach_complete=True,
                        approach_rate_t_per_min=None)
        if snapshot_callback is not None:
            snapshot_callback(final)
        return final

    def safe_move_to_field(
        self,
        target_t: float,
        *,
        final_mode: str = "driven",
        zero_leads: bool = True,
        tolerance_t: float = 0.002,
        settle_s: float = 2.0,
        timeout_s: float = 3600.0,
        persistent_field_confirmed: bool = False,
        max_magnet_voltage_v: Optional[float] = None,
        stop_event=None,
        progress: Optional[Callable[[str, float], None]] = None,
        monotonic_direction: Optional[int] = None,
        hysteresis_options: Optional[dict] = None,
        field_history: Optional[dict] = None,
        field_approach=None,
        rate_t_per_min=None,
        safe_limit_t=None,
        poll_interval_s=.1,
    ) -> MagnetSnapshot:
        """Move from either magnet mode to a target and verified final mode."""
        target = self._validate_field(target_t)
        mode = str(final_mode).strip().lower()
        if mode not in {"driven", "persistent"}:
            raise APS100SafetyError("Final magnet mode must be driven or persistent")
        self.take_remote()
        status = self._ensure_no_fault()
        if status.sweep_active:
            self.pause()
            self._ensure_no_fault()
        self._raise_if_stopped(stop_event)

        heater_state = self.get_heater_state()
        if heater_state == 2:
            # Resolve an in-flight switch transition before making any mode
            # decision; state 2 is never treated as either safe mode.
            heater_state = self._confirm_heater_state(
                1 if mode == "driven" else 0,
                timeout_s=timeout_s,
                stop_event=stop_event,
                progress=progress,
                label=("heater transition to ON" if mode == "driven" else "heater transition to OFF"),
            )
        heater_on = heater_state == 1
        field_t = self.get_field_t()
        output_t = self.get_output_field_t()
        if field_approach is not None:
            limit = min(float(self.maximum_field_t), float(safe_limit_t)) if safe_limit_t is not None else float(self.maximum_field_t)
            try:
                approach_targets(field_t, target, tolerance_t, field_approach, limit)
            except ValueError as exc:
                raise APS100SafetyError(str(exc)) from exc
        if not heater_on and mode == "persistent" and abs(field_t - target) <= abs(
            float(tolerance_t)
        ):
            if zero_leads and abs(output_t) > self.lead_zero_tolerance_t:
                self.zero_output(
                    tolerance_t=self.lead_zero_tolerance_t,
                    timeout_s=timeout_s,
                    verify_persistent_switch=True,
                    transition_progress=progress,
                    max_magnet_voltage_v=max_magnet_voltage_v,
                    stop_event=stop_event,
                    progress=(
                        (lambda value: progress("zeroing leads", value))
                        if progress is not None else None
                    ),
                )
            return self._verify_final_readings(target, mode, zero_leads, tolerance_t, stop_event=stop_event, progress=progress)

        if not heater_on:
            if not persistent_field_confirmed:
                raise APS100SafetyError(
                    "Confirm the APS100 stored persistent field and polarity before "
                    "matching the power-supply leads"
                )
            if progress is not None:
                progress("entering driven mode", field_t)
            self.enter_driven_mode(
                tolerance_t=tolerance_t,
                timeout_s=timeout_s,
                stop_event=stop_event,
                progress=progress,
            )

        self._raise_if_stopped(stop_event)
        if field_approach is not None and monotonic_direction is not None:
            raise APS100SafetyError('Field approach conflicts with directional protection')
        if monotonic_direction is not None:
            if mode != "driven":
                raise APS100SafetyError("Directional approach requires driven mode")
            if hysteresis_options is not None:
                direction = int(monotonic_direction)
                if direction == 0:
                    direction = 1 if target >= self.get_field_t() else -1
                return self._hysteresis_field_move(target, direction, abs(float(tolerance_t)), timeout_s, stop_event, progress, hysteresis_options, field_history if field_history is not None else {})
            return self._monotonic_field_move(target, monotonic_direction, abs(float(tolerance_t)), timeout_s, stop_event, progress)
        if field_approach is not None:
            self.approach_field(target, field_approach, tolerance_t=tolerance_t, timeout_s=timeout_s,
                                stop_event=stop_event, progress=progress, rate_t_per_min=rate_t_per_min,
                                safe_limit_t=safe_limit_t, poll_interval_s=poll_interval_s)
        elif abs(self.get_field_t() - target) > abs(float(tolerance_t)):
            field_progress = (
                (lambda value: progress("ramping field", value))
                if progress is not None else None
            )
            if abs(target) <= abs(float(tolerance_t)):
                self.zero_output(
                    tolerance_t=tolerance_t,
                    timeout_s=timeout_s,
                    stop_event=stop_event,
                    progress=field_progress,
                )
            else:
                self.move_to_field(
                    target,
                    tolerance_t=tolerance_t,
                    timeout_s=timeout_s,
                    stop_event=stop_event,
                    progress=field_progress,
                )
        else:
            self.pause()

        # Persistent moves use the joint target/current stability check directly
        # before OFF; an earlier independent dwell could fail before correction.
        stable_deadline = time.monotonic() + (max(0.0, float(settle_s)) if mode == "driven" else 0.0)
        settling_limit = stable_deadline + 5.0
        while time.monotonic() < stable_deadline:
            self._raise_if_stopped(stop_event)
            self._ensure_no_fault()
            field_t = self.get_field_t()
            if abs(field_t - target) > abs(float(tolerance_t)):
                if time.monotonic() >= settling_limit:
                    raise APS100SafetyError(
                        f"Field did not settle: {field_t:.6g} T; target {target:.6g} T; tolerance {tolerance_t:g} T"
                    )
                stable_deadline = min(settling_limit, time.monotonic() + max(0.25, float(settle_s)))
            if not math.isfinite(field_t):
                raise APS100SafetyError("Nonfinite field while settling")
            if progress is not None:
                progress("field settling", max(0.0, stable_deadline - time.monotonic()))
            self._sleep(min(0.1, max(0.0, stable_deadline - time.monotonic())))

        if mode == "persistent":
            self.enter_persistent_mode(
                target_t=target,
                tolerance_t=tolerance_t,
                settle_s=settle_s,
                zero_leads=zero_leads,
                timeout_s=timeout_s,
                max_magnet_voltage_v=max_magnet_voltage_v,
                stop_event=stop_event,
                progress=progress,
            )
        return self._verify_final_readings(
            target, mode, zero_leads, tolerance_t, stop_event=stop_event, progress=progress,
        )

    def close(self, *, pause_if_sweeping: bool = True, return_local: bool = True) -> None:
        with self._lock:
            resource = self._resource
            if resource is None:
                return
            if self._identity is not None:
                if pause_if_sweeping:
                    try:
                        if self.get_status().sweep_active:
                            self.pause()
                    except Exception:
                        pass
                if return_local and self._remote:
                    try:
                        self.return_local()
                    except Exception:
                        pass
            try:
                resource.close()
            finally:
                self._resource = None
                self._identity = None
                self._remote = False
                self._io_events.clear()
                self._io_sequence = 0
                self._io_controls.clear()
                self._io_control_count = 0
                self._query_counts.clear()
            # Do not close ResourceManager here.  PyVISA shares manager
            # instances, so doing so can invalidate another instrument.


class MockAPS100Adapter:
    """Deterministic APS100 substitute used by the app's mock mode and tests."""

    _range_boundaries_a = (40.0, 44.28, 45.0, 89.0, 100.0)

    def __init__(
        self,
        resource_name: str = DEFAULT_RESOURCE,
        *,
        coil_constant_t_per_a: float = 0.20328,
        maximum_field_t: float = 9.0,
        maximum_rate_a_per_s: float = 0.0343,
        heater_warm_s: float = 0.01,
        heater_cool_s: float = 0.01,
        time_scale: float = 600.0,
        current_match_tolerance_a: Optional[float] = None,
        current_match_tolerance_t: float = 0.010,
        lead_zero_tolerance_t: float = 0.010,
        **_kwargs,
    ) -> None:
        self.resource_name = resource_name
        self.coil_constant_t_per_a = coil_constant_t_per_a
        self.maximum_field_t = maximum_field_t
        self.maximum_current_a = float(_kwargs.get("maximum_current_a", 44.27))
        self.maximum_rate_a_per_s = maximum_rate_a_per_s
        self.heater_warm_s = heater_warm_s
        self.heater_cool_s = heater_cool_s
        self._time_scale = max(1.0, float(time_scale))
        if not math.isfinite(float(coil_constant_t_per_a)) or coil_constant_t_per_a <= 0:
            raise ValueError("coil_constant_t_per_a must be positive and finite")
        self.current_match_tolerance_a = (float(current_match_tolerance_a) if current_match_tolerance_a is not None
                                          else float(current_match_tolerance_t) / self.coil_constant_t_per_a)
        if not math.isfinite(self.current_match_tolerance_a) or self.current_match_tolerance_a <= 0:
            raise ValueError("current-match tolerance must be positive and finite")
        self.lead_zero_tolerance_t = float(lead_zero_tolerance_t)
        if not math.isfinite(self.lead_zero_tolerance_t) or self.lead_zero_tolerance_t <= 0:
            raise ValueError("lead_zero_tolerance_t must be positive and finite")
        self._identity: Optional[APS100Identity] = None
        self._field_t = 0.0
        self._output_t = 0.0
        self._heater = True
        self._low_t = 0.0
        self._high_t = 0.0
        self._rate_t_per_min = 0.1
        # The mock has no persisted instrument setting; mirror the normal
        # configured rate while still exercising the RATE? 5/FAST path.
        self._fast_rate_t_per_min = self._rate_t_per_min
        self._target_t: Optional[float] = None
        self._standby = False
        self._magnet_voltage_v = 0.0
        self._zero_commands = 0
        self._last_update = time.monotonic()
        self._remote = False

    @property
    def identity(self):
        return self._identity

    @property
    def connected(self) -> bool:
        return self._identity is not None

    def connect(self):
        self._identity = APS100Identity("Attocube", "APS100", "MOCK", "1.67", "323")
        return self._identity

    def take_remote(self):
        self._remote = True

    def return_local(self):
        self._remote = False

    def _update(self):
        now = time.monotonic()
        elapsed = now - self._last_update
        self._last_update = now
        if self._target_t is None:
            return
        rate = self._rate_t_per_min if self._heater else self._fast_rate_t_per_min
        step = rate / 60.0 * elapsed * self._time_scale
        delta = self._target_t - self._output_t
        if abs(delta) <= step or step <= 0:
            self._output_t = self._target_t
            if self._heater:
                self._field_t = self._output_t
            self._target_t = None
        else:
            self._output_t += math.copysign(step, delta)
            if self._heater:
                self._field_t = self._output_t

    def get_status(self):
        self._update()
        raw = 1 if self._target_t is not None else (2 if self._standby else 0)
        return decode_status_byte(raw)

    def get_sweep_state(self):
        self._update()
        if self._target_t is None:
            return "standby" if self._standby else "pause"
        return "sweep up" if self._target_t > self._output_t else "sweep down"

    def get_heater_status(self):
        return self._heater

    def get_field_t(self):
        self._update()
        return self._field_t

    def get_output_field_t(self):
        self._update()
        return self._output_t

    def get_limits_t(self):
        return self._low_t, self._high_t

    def get_voltage_limit_v(self):
        return 3.0

    def get_magnet_voltage_v(self):
        return self._magnet_voltage_v

    def get_output_voltage_v(self):
        return 0.0

    def get_units(self):
        return "kG"

    def get_operating_mode(self):
        return "Manual"

    def require_manual_mode(self):
        return self.get_operating_mode()

    def get_range_a(self, index):
        return self._range_boundaries_a[index]

    def get_rate_a_per_s(self, index):
        rate = self._fast_rate_t_per_min if int(index) == 5 else self._rate_t_per_min
        return rate / (self.coil_constant_t_per_a * 60.0)

    def get_rate_t_per_min(self, index):
        return self.get_rate_a_per_s(index) * self.coil_constant_t_per_a * 60.0

    def get_rates(self):
        return {i: (self.get_range_a(i), self.get_rate_t_per_min(i)) for i in range(5)}

    def get_fast_rate_a_per_s(self):
        value = float(self.get_rate_a_per_s(5))
        if not math.isfinite(value) or value <= 0.0:
            raise APS100SafetyError("Invalid mock APS100 Fast Mode rate")
        return value

    def get_fast_rate_t_per_min(self):
        return self.get_fast_rate_a_per_s() * self.coil_constant_t_per_a * 60.0

    def read_snapshot(self):
        self._update()
        return MagnetSnapshot(
            monotonic_s=time.monotonic(),
            field_t=self._field_t,
            output_field_t=self._output_t,
            output_current_a=self._output_t / self.coil_constant_t_per_a,
            heater_on=self._heater,
            sweep_state=self.get_sweep_state(),
            status=self.get_status(),
            lower_limit_t=self._low_t,
            upper_limit_t=self._high_t,
            voltage_limit_v=3.0,
            magnet_voltage_v=self._magnet_voltage_v,
            output_voltage_v=0.0,
            units="kG",
            operating_mode=self.get_operating_mode(),
            heater_state=1 if self._heater else 0,
        )

    def set_limits_t(self, low_t, high_t):
        if low_t >= high_t or max(abs(low_t), abs(high_t)) > self.maximum_field_t:
            raise APS100SafetyError("Invalid mock APS100 limits")
        self._low_t, self._high_t = float(low_t), float(high_t)
        return self.get_limits_t()

    def set_rate_t_per_min(self, rate_t_per_min, *, max_abs_field_t=None):
        del max_abs_field_t
        max_rate = self.maximum_rate_a_per_s * self.coil_constant_t_per_a * 60.0
        if not 0 < rate_t_per_min <= max_rate:
            raise APS100SafetyError("Invalid mock APS100 rate")
        self._rate_t_per_min = float(rate_t_per_min)
        return {0: self._rate_t_per_min}

    def restore_rates(self, rates):
        if not rates:
            return {}
        # The mock exposes one shared programmable rate while preserving the
        # real adapter's captured ``{index: (range, rate)}`` contract.
        first = next(iter(dict(rates).values()))
        self._rate_t_per_min = float(first[1] if isinstance(first, (tuple, list)) else first)
        return {index: self.get_rate_t_per_min(index) for index in dict(rates)}

    def start_sweep_to(self, target_t):
        if not self._heater:
            raise APS100SafetyError("Field sweep requires driven mode")
        self._update()
        self._standby = False
        target_t = float(target_t)
        if abs(target_t) > self.maximum_field_t:
            raise APS100SafetyError("Target is outside mock field limits")
        direction = "up" if target_t >= self._field_t else "down"
        if direction == "up":
            self._high_t = target_t
        else:
            self._low_t = target_t
        self._target_t = target_t
        return direction

    def _start_output_sweep_to(self, target_t):
        if self.get_heater_status() is not False:
            raise APS100SafetyError("Fast lead matching requires APS100 heater OFF")
        self.get_fast_rate_a_per_s()
        self._update()
        target_t = float(target_t)
        if abs(target_t) > self.maximum_field_t:
            raise APS100SafetyError("Target is outside mock field limits")
        self._target_t = target_t
        return "up" if target_t >= self._output_t else "down"

    def pause(self, **_kwargs):
        self._update()
        self._target_t = None
        self._standby = False

    def wait_for_field(
        self,
        target_t,
        *,
        tolerance_t=0.002,
        timeout_s=900.0,
        read_output=False,
        stop_event=None,
        progress=None,
    ):
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if stop_event is not None and stop_event.is_set():
                self.pause()
                raise APS100OperationCancelled(stop_event)
            value = self.get_output_field_t() if read_output else self.get_field_t()
            if progress:
                progress(value)
            if abs(value - target_t) <= tolerance_t:
                self.pause()
                return value
            time.sleep(0.005)
        raise APS100TimeoutError("Mock field timeout")

    def move_to_field(self, target_t, **kwargs):
        self.start_sweep_to(target_t)
        return self.wait_for_field(target_t, **kwargs)

    def zero_output(
        self,
        *,
        tolerance_t=None,
        verify_persistent_switch=False,
        max_magnet_voltage_v=None,
        stop_event=None,
        progress=None,
        **_kwargs,
    ):
        if stop_event is not None and stop_event.is_set():
            raise APS100OperationCancelled(stop_event)
        if tolerance_t is None:
            tolerance_t = self.lead_zero_tolerance_t
        if not math.isfinite(float(tolerance_t)) or float(tolerance_t) <= 0:
            raise ValueError("zero tolerance must be positive and finite")
        if abs(self._output_t) <= abs(float(tolerance_t)):
            return self._output_t
        if verify_persistent_switch:
            if max_magnet_voltage_v is None or float(max_magnet_voltage_v) <= 0:
                raise APS100SafetyError(
                    "Persistent lead zeroing requires a commissioned positive VMAG safety limit"
                )
            if abs(self._magnet_voltage_v) > float(max_magnet_voltage_v):
                raise APS100SafetyError("VMAG exceeded the commissioned limit while zeroing leads")
        self._zero_commands += 1
        self._output_t = 0.0
        if self._heater:
            self._field_t = 0.0
        self._target_t = None
        self._standby = True
        if progress:
            progress(0.0)
        return self._output_t

    def enter_driven_mode(self, *, progress=None, **_kwargs):
        if not self._heater:
            magnet_t = self.get_field_t()
            output_t = self.get_output_field_t()
            tolerance_t = self.current_match_tolerance_a * self.coil_constant_t_per_a
            if abs(output_t - magnet_t) > tolerance_t:
                fast_rate = self.get_fast_rate_t_per_min()
                expected_s = abs(output_t - magnet_t) / (fast_rate / 60.0)
                self._start_output_sweep_to(magnet_t)
                self.wait_for_field(
                    magnet_t,
                    tolerance_t=tolerance_t,
                    timeout_s=max(1.0, expected_s * 2.0 + 2.0),
                    read_output=True,
                    stop_event=_kwargs.get("stop_event"),
                    progress=(lambda value: progress("matching leads", value)) if progress else None,
                )
                self.pause()
            if abs(self.get_output_field_t() - self.get_field_t()) > tolerance_t:
                raise APS100SafetyError("Cannot enable heater: mock current mismatch")
        self._output_t = self._field_t
        self._heater = True
        self._standby = False
        if progress:
            progress("heater warming", 0.0)

    def enter_persistent_mode(
        self,
        *,
        zero_leads=True,
        max_magnet_voltage_v=None,
        progress=None,
        **_kwargs,
    ):
        if self._target_t is not None:
            self.pause()
        self._heater = False
        if progress:
            progress("heater cooling", 0.0)
        if zero_leads and abs(self._output_t) > self.lead_zero_tolerance_t:
            self.zero_output(
                tolerance_t=self.lead_zero_tolerance_t,
                verify_persistent_switch=True,
                max_magnet_voltage_v=max_magnet_voltage_v,
                progress=(
                    (lambda value: progress("zeroing leads", value))
                    if progress is not None else None
                ),
            )

    def approach_field(self, target_t, approach, *, tolerance_t=.002, timeout_s=3600.,
                       stop_event=None, progress=None, snapshot_callback=None,
                       no_progress_s=180., rate_t_per_min=None, safe_limit_t=None, poll_interval_s=.1):
        del no_progress_s, rate_t_per_min, poll_interval_s
        mode = normalize_approach(approach)
        limit = min(self.maximum_field_t, safe_limit_t) if safe_limit_t is not None else self.maximum_field_t
        targets = approach_targets(self.get_field_t(), target_t, tolerance_t, mode, limit)
        deadline = time.monotonic() + timeout_s
        for index, stage in enumerate(targets):
            def report(value):
                if progress:
                    progress('ramping field', value)
                if snapshot_callback:
                    phase = 'oscillate' if mode == 'oscillate' else ('ramp' if index == 0 else 'approach')
                    snapshot_callback(replace(self.read_snapshot(), approach_mode=mode, approach_phase=phase))
            self.move_to_field(stage, tolerance_t=tolerance_t, timeout_s=max(0., deadline-time.monotonic()),
                               stop_event=stop_event, progress=report)
        self.pause()
        snapshot = replace(self.read_snapshot(), approach_mode=mode, approach_phase='settled', approach_complete=True)
        if snapshot_callback:
            snapshot_callback(snapshot)
        return snapshot

    def safe_move_to_field(
        self,
        target_t,
        *,
        final_mode="driven",
        zero_leads=True,
        tolerance_t=0.002,
        settle_s=0.0,
        timeout_s=3600.0,
        persistent_field_confirmed=False,
        max_magnet_voltage_v=None,
        stop_event=None,
        progress=None,
        monotonic_direction=None,
        hysteresis_options=None,
        field_history=None,
        field_approach=None,
        rate_t_per_min=None,
        safe_limit_t=None,
        poll_interval_s=.1,
    ):
        del settle_s
        self.take_remote()
        if stop_event is not None and stop_event.is_set():
            raise APS100OperationCancelled(stop_event)
        if self.get_status().sweep_active:
            self.pause()
        mode = str(final_mode).strip().lower()
        if mode not in {"driven", "persistent"}:
            raise APS100SafetyError("Final magnet mode must be driven or persistent")
        target_t = float(target_t)
        if (
            not self._heater
            and mode == "persistent"
            and abs(self._field_t - target_t) <= tolerance_t
        ):
            if zero_leads and abs(self._output_t) > self.lead_zero_tolerance_t:
                self.zero_output(
                    tolerance_t=self.lead_zero_tolerance_t,
                    verify_persistent_switch=True,
                    max_magnet_voltage_v=max_magnet_voltage_v,
                    progress=(
                        (lambda value: progress("zeroing leads", value))
                        if progress is not None else None
                    ),
                )
            return self.read_snapshot()
        if not self._heater:
            if not persistent_field_confirmed:
                raise APS100SafetyError(
                    "Confirm the APS100 stored persistent field and polarity before matching"
                )
            self.enter_driven_mode(progress=progress)
        if field_approach is not None and monotonic_direction is not None:
            raise APS100SafetyError('Field approach conflicts with directional protection')
        if field_approach is not None:
            self.approach_field(target_t, field_approach, tolerance_t=tolerance_t, timeout_s=timeout_s,
                                stop_event=stop_event, progress=progress, rate_t_per_min=rate_t_per_min,
                                safe_limit_t=safe_limit_t, poll_interval_s=poll_interval_s)
        elif hysteresis_options is not None:
            history = field_history if field_history is not None else {}
            approach = hysteresis_options.get("approach", "no_overshoot")
            if approach not in {"no_overshoot", "linear_no_backward"}:
                raise APS100SafetyError("Invalid hysteresis approach")
            no_overshoot_profile = hysteresis_options.get("no_overshoot_profile", "standard")
            if no_overshoot_profile not in {"standard", "short_tail"}:
                raise APS100SafetyError("Invalid APS100 No Overshoot profile")
            if no_overshoot_profile != "standard" and approach != "no_overshoot":
                raise APS100SafetyError("Shorter slow approach requires No Overshoot")
            current = self.get_field_t()
            direction = int(monotonic_direction or (1 if target_t >= current else -1))
            started = time.monotonic()
            if not history:
                history.update(direction=direction, start_t=current, end_t=current, min_t=current, max_t=current,
                               max_reverse_t=0., read_count=1, started_monotonic_s=started,
                               finished_monotonic_s=None, complete=False)
            diagnostics = _FieldMoveDiagnostics(history, started)
            history["approach"] = approach
            reversal_warnings = _ReversalWarnings(history)
            if approach == "no_overshoot":
                history["no_overshoot_diagnostics"] = dict(
                    requested_profile=no_overshoot_profile, effective_profile="mock",
                    reason="mock_single_stage")
            # The mock's table is local state: do not introduce readback calls.
            history["start_rates_t_per_min"] = {
                str(i): [boundary, self._rate_t_per_min]
                for i, boundary in enumerate(self._range_boundaries_a)
            }
            peak = direction * (history["max_t"] if direction > 0 else history["min_t"])
            def observe(value):
                nonlocal peak
                value = float(value)
                if not math.isfinite(value):
                    raise APS100SafetyError("Nonfinite field history readback")
                history["end_t"] = value
                history["min_t"] = min(history["min_t"], value)
                history["max_t"] = max(history["max_t"], value)
                history["read_count"] += 1
                diagnostics.observe(value, time.monotonic())
                peak = max(peak, direction * value)
                history["max_reverse_t"] = max(history["max_reverse_t"], peak - direction * value)
                if not hysteresis_options["lower_t"] <= value <= hysteresis_options["upper_t"]:
                    raise APS100FieldHistoryError("Mock field history violated limits")
                if not hysteresis_options.get("preparation"):
                    reversal_warnings.observe(value, peak, direction,
                                              hysteresis_options["reversal_t"], time.monotonic())
                if progress: progress("ramping field", value)
            try:
                observe(current)
                if direction * (target_t-current) > tolerance_t:
                    # Mock motion remains one unreduced stage; its simulated
                    # sweep starts at the existing move_to_field invocation.
                    diagnostics.begin_stage(target_t, 1.0, current, self._rate_t_per_min)
                    diagnostics.begin_sweep(time.monotonic())
                    self.move_to_field(target_t, tolerance_t=tolerance_t, timeout_s=timeout_s,
                                       stop_event=stop_event, progress=observe)
                    diagnostics.end_stage(history["end_t"], time.monotonic(), "target")
                self.pause()
                observe(self.get_field_t())
                if abs(history["end_t"] - target_t) > hysteresis_options["acceptance_t"]:
                    if (not hysteresis_options.get("preparation") and direction * (history["end_t"] - target_t)
                            > hysteresis_options["acceptance_t"]):
                        raise APS100QualityOvershoot("Mock paused field passed nominal target")
                    raise APS100SafetyError("Mock directional field outside acceptance")
                if hysteresis_options.get("preparation"):
                    history["preparation_stable"] = True
                return self.read_snapshot()
            except Exception:
                diagnostics.end_stage(history["end_t"], time.monotonic())
                self.pause()
                raise
            finally:
                try:
                    observe(self.get_field_t())
                    history["complete"] = True
                finally:
                    history["finished_monotonic_s"] = time.monotonic()
                    diagnostics.finish(history["finished_monotonic_s"])
        if monotonic_direction is not None:
            current = self.get_field_t()
            direction = int(monotonic_direction) or (1 if target_t >= current else -1)
            if direction not in (-1, 1):
                raise APS100SafetyError("Invalid monotonic direction")
            if direction * (current - target_t) > abs(float(tolerance_t)):
                self.pause()
                raise APS100OvershootError("Directional target already exceeded")
        if abs(self.get_field_t() - target_t) > abs(float(tolerance_t)):
            field_progress = (
                (lambda value: progress("ramping field", value))
                if progress is not None else None
            )
            if abs(target_t) <= tolerance_t:
                self.zero_output(tolerance_t=tolerance_t, stop_event=stop_event, progress=field_progress)
            else:
                self.move_to_field(
                    target_t,
                    tolerance_t=tolerance_t,
                    timeout_s=timeout_s,
                    stop_event=stop_event,
                    progress=field_progress,
                )
        if mode == "persistent":
            self.enter_persistent_mode(
                zero_leads=zero_leads,
                max_magnet_voltage_v=max_magnet_voltage_v,
                progress=progress,
            )
        return self.read_snapshot()

    def close(self, **_kwargs):
        self.pause()
        self._identity = None
        self._remote = False
