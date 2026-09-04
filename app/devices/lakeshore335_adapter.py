"""VISA adapter for a Lake Shore Model 335.

The adapter deliberately exposes only identification and telemetry queries.  It
It permits only the narrowly scoped setpoint and heater-off writes needed by
the attoDRY1000 sample-temperature workflow.  Commissioned setup is never
written by this adapter.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
import re
import threading
import time
from typing import Callable, Optional


DEFAULT_RESOURCE = "ASRL6::INSTR"


class LakeShore335Error(RuntimeError):
    """Base Model 335 error."""


class LakeShore335CommunicationError(LakeShore335Error):
    """Communication, timeout, or disconnected-device error."""


class LakeShore335TelemetryError(LakeShore335Error):
    """Malformed or non-finite telemetry."""


class LakeShore335ConfigurationError(LakeShore335Error):
    """Missing or ambiguous commissioned control-output mapping."""


@dataclass(frozen=True)
class LakeShore335Snapshot:
    sample_temperature_k: Optional[float]
    reservoir_temperature_k: Optional[float]
    sample_sensor_status: object
    reservoir_sensor_status: object
    timestamp: float
    monotonic_s: float
    connected: bool = True
    communication_valid: bool = True
    identity: Optional[str] = None
    sample_slope_k_per_min: Optional[float] = None
    reservoir_slope_k_per_min: Optional[float] = None
    diagnostic_error: Optional[str] = None

    @property
    def acquisition_timestamp(self) -> float:
        return self.timestamp

    @property
    def reading_age_s(self) -> float:
        return max(0.0, time.monotonic() - self.monotonic_s)

    @property
    def sensor_status_a(self):
        return self.sample_sensor_status

    @property
    def sensor_status_b(self):
        return self.reservoir_sensor_status

    @property
    def sample_temperature(self):
        return self.sample_temperature_k

    @property
    def reservoir_temperature(self):
        return self.reservoir_temperature_k

    @property
    def sample_status(self):
        return self.sample_sensor_status

    @property
    def reservoir_status(self):
        return self.reservoir_sensor_status

    @property
    def valid(self) -> bool:
        return bool(self.connected and self.communication_valid)


@dataclass(frozen=True)
class LakeShore335Identity:
    manufacturer: str
    model: str
    serial: str = ""
    firmware: str = ""

    def __str__(self) -> str:
        return ",".join((self.manufacturer, self.model, self.serial, self.firmware)).rstrip(",")


def _finite(value: object, name: str) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise LakeShore335TelemetryError(f"{name} is not numeric: {value!r}") from exc
    if not math.isfinite(result):
        raise LakeShore335TelemetryError(f"{name} is non-finite")
    return result


class LakeShore335Adapter:
    """Thread-safe, read-only Model 335 serial/VISA adapter."""

    def __init__(self, resource_name: str = DEFAULT_RESOURCE, *, baud_rate: int = 57600,
                 data_bits: int = 7, parity: object = "odd", stop_bits: int = 1,
                 timeout_ms: int = 1500, resource_manager=None, visa_resource=None,
                 clock: Callable[[], float] = time.monotonic):
        self.resource_name = str(resource_name or DEFAULT_RESOURCE)
        self.baud_rate, self.data_bits, self.parity, self.stop_bits = int(baud_rate), int(data_bits), parity, int(stop_bits)
        self.timeout_ms = int(timeout_ms)
        self._resource_manager = resource_manager
        self._resource = visa_resource
        self._owns_resource_manager = False
        self._identity: Optional[str] = None
        self._lock = threading.RLock()
        self._clock = clock
        self._previous = None

    @property
    def connected(self) -> bool:
        return self._resource is not None and self._identity is not None

    @property
    def identity(self):
        return self._identity

    def _configure_serial(self, resource):
        # pyvisa uses constants for parity/stop bits, while simple fakes often
        # accept strings/integers.  Keep this best-effort and verify by queries.
        parity = self.parity
        stop_bits = self.stop_bits
        try:
            import pyvisa
            if isinstance(parity, str):
                parity = getattr(pyvisa.constants.Parity, parity.lower(), parity)
            if stop_bits == 1:
                stop_bits = getattr(pyvisa.constants.StopBits, "one", stop_bits)
        except Exception:
            pass
        for name, value in (("baud_rate", self.baud_rate), ("data_bits", self.data_bits),
                            ("parity", parity), ("stop_bits", stop_bits),
                            ("timeout", self.timeout_ms), ("write_termination", "\n"),
                            ("read_termination", "\n")):
            try:
                setattr(resource, name, value)
            except Exception:
                pass

    def connect(self):
        with self._lock:
            if self.connected:
                return self._identity
            try:
                if self._resource is None:
                    if self._resource_manager is None:
                        import pyvisa
                        self._resource_manager = pyvisa.ResourceManager()
                        self._owns_resource_manager = True
                    self._resource = self._resource_manager.open_resource(self.resource_name)
                self._configure_serial(self._resource)
                identity = self._query("*IDN?")
                self._identity = self._validate_identity(identity)
                return self._identity
            except LakeShore335Error:
                self._identity = None
                resource = self._resource
                self._resource = None
                if resource is not None:
                    try:
                        resource.close()
                    except Exception:
                        pass
                raise
            except Exception as exc:
                self._identity = None
                resource = self._resource
                self._resource = None
                if resource is not None:
                    try:
                        resource.close()
                    except Exception:
                        pass
                raise LakeShore335CommunicationError(f"Lake Shore connection failed: {exc}") from exc

    @staticmethod
    def _validate_identity(identity: str) -> str:
        text = str(identity).strip()
        parts = [part.strip() for part in text.split(",")]
        if len(parts) < 2 or not parts[0] or not parts[1]:
            raise LakeShore335TelemetryError(f"Unexpected Lake Shore identity: {identity!r}")
        manufacturer = re.sub(r"[^A-Z0-9]", "", parts[0].upper())
        model = re.sub(r"[^A-Z0-9]", "", parts[1].upper())
        valid_manufacturer = manufacturer in {
            "LSCI", "LAKESHORE", "LAKESHORECRYOTRONICS", "LAKESHORECRYOTRONICSINC"
        }
        if not valid_manufacturer or model not in {"335", "MODEL335"}:
            raise LakeShore335TelemetryError(
                f"Expected Lake Shore Model 335, received {identity!r}"
            )
        return text

    def _require_connected(self):
        if not self.connected:
            raise LakeShore335CommunicationError("Lake Shore 335 is not connected")
        return self._resource

    def _query(self, command: str) -> str:
        command = str(command)
        allowed = {"*IDN?", "KRDG? A", "KRDG? B", "RDGST? A", "RDGST? B"}
        allowed.update(f"{prefix}? {output}" for prefix in ("OUTMODE", "SETP", "RAMP", "RANGE", "MOUT") for output in (1, 2))
        if any(ord(char) < 32 for char in command) or command not in allowed:
            raise LakeShore335TelemetryError(f"Lake Shore command is not allowed: {command!r}")
        resource = self._require_connected() if self._identity is not None else self._resource
        if resource is None:
            raise LakeShore335CommunicationError("Lake Shore 335 resource is not open")
        try:
            return str(resource.query(str(command))).strip()
        except Exception:
            try:
                resource.write(str(command))
                return str(resource.read()).strip()
            except Exception as exc:
                raise LakeShore335CommunicationError(f"Lake Shore query {command!r} failed: {exc}") from exc

    def _write(self, command: str) -> None:
        """Write one allowlisted control command; never accept setup commands."""
        command = str(command).strip()
        if not re.fullmatch(r"(?:SETP|RANGE) [12],(?:[-+]?\d+(?:\.\d*)?|[-+]?\.\d+)", command):
            raise LakeShore335TelemetryError(f"Lake Shore command is not allowed: {command!r}")
        resource = self._require_connected()
        try:
            resource.write(command)
        except Exception as exc:
            raise LakeShore335CommunicationError(f"Lake Shore write {command!r} failed: {exc}") from exc

    @staticmethod
    def _first_field(value: str) -> str:
        return str(value).strip().split(",", 1)[0].strip().upper()

    @staticmethod
    def _parse_outmode(value: str):
        fields = [item.strip() for item in str(value).split(",")]
        if len(fields) != 3 or any(not re.fullmatch(r"[+-]?\d+", item) for item in fields):
            raise LakeShore335ConfigurationError(f"Malformed OUTMODE response: {value!r}")
        return tuple(int(item) for item in fields)

    def control_output_for_channel(self, channel: str) -> int:
        """Return the uniquely commissioned output controlling channel A/B."""
        channel = str(channel).strip().upper()
        if channel not in {"A", "B"}:
            raise LakeShore335ConfigurationError("Sample control channel must be A or B")
        matches = []
        for output in (1, 2):
            mode, input_number, _powerup = self._parse_outmode(self._query(f"OUTMODE? {output}"))
            # Model 335 input numbering is 1=A, 2=B. Modes 1 (closed-loop
            # PID) and 2 (zone) are the only modes suitable for setpoint use.
            if mode in {1, 2} and input_number == (1 if channel == "A" else 2):
                matches.append(output)
        if len(matches) != 1:
            raise LakeShore335ConfigurationError(
                f"Expected exactly one control output for channel {channel}; found {matches or 'none'}"
            )
        return matches[0]

    def read_control_configuration(self, sample_channel: str = "B") -> dict:
        output = self.control_output_for_channel(sample_channel)
        return {"channel": str(sample_channel).upper(), "output": output,
                "setpoint": _finite(self._query(f"SETP? {output}"), "setpoint"),
                "ramp": self._query(f"RAMP? {output}"),
                "range": self._query(f"RANGE? {output}"),
                "heater_output": self._query(f"MOUT? {output}")}

    def set_sample_setpoint(self, temperature_k: float, sample_channel: str = "B") -> dict:
        target = _finite(temperature_k, "sample setpoint")
        output = self.control_output_for_channel(sample_channel)
        self._write(f"SETP {output},{target:.9g}")
        readback = _finite(self._query(f"SETP? {output}"), "setpoint readback")
        if not math.isclose(readback, target, rel_tol=1e-6, abs_tol=1e-6):
            raise LakeShore335CommunicationError(f"Setpoint readback mismatch: requested {target}, got {readback}")
        range_readback = self._query(f"RANGE? {output}")
        return {"output": output, "setpoint": readback, "range": range_readback}

    def heater_off(self, sample_channel: str = "B") -> dict:
        output = self.control_output_for_channel(sample_channel)
        self._write(f"RANGE {output},0")
        range_readback = self._first_field(self._query(f"RANGE? {output}"))
        if range_readback not in {"0", "OFF", "0.0"}:
            raise LakeShore335CommunicationError(f"Heater-off readback failed: {range_readback!r}")
        return {"output": output, "range": range_readback, "confirmed": True}

    def read_snapshot(self, sample_channel: str = "A", reservoir_channel: str = "B") -> LakeShore335Snapshot:
        with self._lock:
            if not self.connected:
                raise LakeShore335CommunicationError("Lake Shore 335 is not connected")
            sample_channel = str(sample_channel)
            reservoir_channel = str(reservoir_channel)
            if sample_channel not in {"A", "B"} or reservoir_channel not in {"A", "B"}:
                raise LakeShore335TelemetryError("Lake Shore channels must be exactly A or B")
            try:
                sample = _finite(self._query(f"KRDG? {sample_channel}"), "sample temperature")
                reservoir = _finite(self._query(f"KRDG? {reservoir_channel}"), "reservoir temperature")
                sample_status = self._query(f"RDGST? {sample_channel}")
                reservoir_status = self._query(f"RDGST? {reservoir_channel}")
            except LakeShore335Error:
                resource = self._resource
                self._resource = None
                self._identity = None
                if resource is not None:
                    try:
                        resource.close()
                    except Exception:
                        pass
                raise
            except Exception as exc:
                raise LakeShore335CommunicationError(f"Lake Shore telemetry read failed: {exc}") from exc
            now_mono = self._clock()
            now_wall = time.time()
            sample_slope = reservoir_slope = None
            if self._previous is not None:
                old, old_mono = self._previous
                elapsed_min = (now_mono - old_mono) / 60.0
                if elapsed_min > 0:
                    sample_slope = (sample - old[0]) / elapsed_min
                    reservoir_slope = (reservoir - old[1]) / elapsed_min
            self._previous = ((sample, reservoir), now_mono)
            return LakeShore335Snapshot(sample, reservoir, sample_status, reservoir_status,
                                        now_wall, now_mono, True, True, self._identity,
                                        sample_slope, reservoir_slope)

    def close(self):
        with self._lock:
            resource = self._resource
            self._resource = None
            self._identity = None
            self._previous = None
            if resource is not None:
                try:
                    resource.close()
                except Exception:
                    pass
            # ResourceManager ownership is process-wide in PyVISA; only close
            # this adapter's resource.


class MockLakeShore335Adapter(LakeShore335Adapter):
    """Deterministic read-only fake used by tests and commissioning."""

    def __init__(self, *args, sample_temperature_k=None, reservoir_temperature_k=None,
                 sample_sensor_status="0", reservoir_sensor_status="0", **kwargs):
        super().__init__(*args, **kwargs)
        self._mock_values = [sample_temperature_k, reservoir_temperature_k,
                             sample_sensor_status, reservoir_sensor_status]
        self._identity = "Lake Shore,Model 335,MOCK,1"
        self._resource = object()

    def connect(self):
        self._identity = "Lake Shore,Model 335,MOCK,1"
        self._resource = object()
        return self._identity

    def set_readings(self, sample_temperature_k, reservoir_temperature_k,
                     sample_sensor_status="0", reservoir_sensor_status="0"):
        self._mock_values = [sample_temperature_k, reservoir_temperature_k,
                             sample_sensor_status, reservoir_sensor_status]

    def read_snapshot(self, *args, **kwargs):
        now_mono = self._clock()
        sample, reservoir, sample_status, reservoir_status = self._mock_values
        if self._previous is not None and sample is not None and reservoir is not None:
            old, old_mono = self._previous
            dt = (now_mono - old_mono) / 60.0
            slopes = ((float(sample) - old[0]) / dt, (float(reservoir) - old[1]) / dt) if dt > 0 else (None, None)
        else:
            slopes = (None, None)
        if sample is not None and reservoir is not None:
            try: self._previous = ((float(sample), float(reservoir)), now_mono)
            except (TypeError, ValueError): self._previous = None
        return LakeShore335Snapshot(sample, reservoir, sample_status, reservoir_status,
                                    time.time(), now_mono, True, True, self._identity,
                                    slopes[0], slopes[1])


LakeShore335TemperatureSnapshot = LakeShore335Snapshot
LakeShore335 = LakeShore335Adapter
