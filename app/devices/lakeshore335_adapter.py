"""VISA adapter for a Lake Shore Model 335.

Only sample setpoint, heater range and setpoint ramp can be written. Input
mapping, units, PID and heater setup are read back and never overwritten.
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
    """Thread-safe Model 335 telemetry and explicit sample controls."""

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
        allowed.update(f"INTYPE? {channel}" for channel in ("A", "B"))
        allowed.update(f"ZONE? {output},{zone}" for output in (1, 2) for zone in range(1, 11))
        allowed.update(f"{prefix}? {output}" for prefix in ("OUTMODE", "SETP", "RAMP", "RANGE", "MOUT", "HTR", "HTRSET") for output in (1, 2))
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
        number = r"(?:\d+(?:\.\d*)?|\.\d+)"
        if not re.fullmatch(rf"(?:SETP [12],{number}|RANGE [12],[0-3]|RAMP [12],[01],{number})", command):
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
        return self._control_output_mapping(channel)[0]

    def _control_output_mapping(self, channel):
        channel = str(channel).strip().upper()
        if channel not in {"A", "B"}:
            raise LakeShore335ConfigurationError("Sample control channel must be A or B")
        matches = []
        for output in (1, 2):
            mode, input_number, _powerup = self._parse_outmode(self._query(f"OUTMODE? {output}"))
            # Model 335 input numbering is 1=A, 2=B. Modes 1 (closed-loop
            # PID) and 2 (zone) are the only modes suitable for setpoint use.
            if mode in {1, 2} and input_number == (1 if channel == "A" else 2):
                matches.append((output, mode))
        if len(matches) != 1:
            raise LakeShore335ConfigurationError(
                f"Expected exactly one control output for channel {channel}; found {matches or 'none'}"
            )
        return matches[0]

    def read_control_configuration(self, sample_channel: str = "B") -> dict:
        with self._lock:
            channel = str(sample_channel).strip().upper()
            output, mode = self._control_output_mapping(channel)
            self._require_kelvin_input(channel)
            setup = self._query(f"HTRSET? {output}").split(",")
            if len(setup) != 5 or setup[0].strip() not in {"0", "1"}:
                raise LakeShore335ConfigurationError("Malformed heater setup readback")
            voltage = output == 2 and int(setup[0]) == 1
            ramp = self._parse_ramp(self._query(f"RAMP? {output}"))
            range_value = self._parse_range(self._query(f"RANGE? {output}"))
            if voltage and range_value > 1:
                raise LakeShore335ConfigurationError("Invalid voltage-output heater range")
            return {"channel": channel, "output": output, "control_mode": mode,
                    "setpoint": _finite(self._query(f"SETP? {output}"), "setpoint"),
                    "ramp_enabled": ramp[0], "ramp_rate_k_per_min": ramp[1],
                    "range": range_value, "output_type": "voltage" if voltage else "current",
                    "heater_output": _finite(self._query(f"HTR? {output}"), "heater output")}

    def _read_zone_ranges(self, output, channel):
        bands = []
        unused = False
        for zone in range(1, 11):
            fields = self._query(f"ZONE? {output},{zone}").split(",")
            if len(fields) != 8:
                raise LakeShore335ConfigurationError(f"Malformed Zone {zone} readback")
            values = [_finite(value, f"Zone {zone}") for value in fields]
            upper, heater_range, input_number = values[0], self._parse_range(fields[5]), values[6]
            if input_number not in {0, 1, 2}:
                raise LakeShore335ConfigurationError(f"Invalid input mapping in Zone {zone}")
            if upper == 0:
                unused = True
                continue
            if unused:
                raise LakeShore335ConfigurationError("LS335 Zone table has a gap; configure contiguous zones")
            if input_number not in {0, 1 if channel == "A" else 2}:
                raise LakeShore335ConfigurationError(f"Zone {zone} uses a different sensor than sample {channel}")
            bands.append({"upper_temperature_k": upper, "heater_range": heater_range})
        from app.sample_heater_ranges import validate_heater_ranges
        try:
            return validate_heater_ranges(bands)
        except ValueError as exc:
            raise LakeShore335ConfigurationError(f"LS335 Zone table: {exc}") from exc

    def _require_kelvin_input(self, channel):
        fields = self._query(f"INTYPE? {channel}").split(",")
        if (len(fields) != 5 or any(not re.fullmatch(r"\d+", item.strip()) for item in fields)
                or fields[-1].strip() != "1" or fields[0].strip() not in {"1", "2", "3", "4"}):
            raise LakeShore335ConfigurationError(
                f"Sample input {channel} must be enabled and commissioned in kelvin")

    @staticmethod
    def _parse_range(value):
        number = _finite(value, "heater range")
        if not number.is_integer() or not 0 <= number <= 3:
            raise LakeShore335TelemetryError(f"Invalid heater range: {value!r}")
        return int(number)

    @staticmethod
    def _parse_ramp(value):
        fields = str(value).split(",")
        if len(fields) != 2 or fields[0].strip() not in {"0", "1"}:
            raise LakeShore335TelemetryError(f"Invalid ramp configuration: {value!r}")
        rate = _finite(fields[1], "ramp rate")
        if not 0 <= rate <= 100:
            raise LakeShore335TelemetryError(f"Invalid ramp rate: {value!r}")
        return bool(int(fields[0])), rate

    def set_sample_control(self, temperature_k, sample_channel="B", *, heater_range=None,
                           ramp_enabled=None, ramp_rate_k_per_min=None,
                           minimum_temperature_k=0.001, maximum_temperature_k=300.0,
                           maximum_heater_range=3, auto_heater_ranges=None):
        """Verify settings before enabling an explicitly selected heater range.

        None preserves an existing range/ramp. Partial failures never advance
        to heater activation; an already active output is shut down if a
        written setting cannot be confirmed.
        """
        with self._lock:
            target = _finite(temperature_k, "sample setpoint")
            minimum = _finite(minimum_temperature_k, "minimum sample temperature")
            maximum = _finite(maximum_temperature_k, "maximum sample temperature")
            if not 0 < minimum <= target <= maximum:
                raise LakeShore335ConfigurationError(f"Sample target must be within {minimum:g}–{maximum:g} K")
            automatic = heater_range == "auto"
            auto_native = False
            auto_info = {}
            maximum_range = self._parse_range(maximum_heater_range)
            if heater_range is not None and not automatic:
                heater_range = self._parse_range(heater_range)
                if heater_range > maximum_range:
                    raise LakeShore335ConfigurationError("Selected heater range exceeds configured limit")
            if ramp_enabled is not None:
                if not isinstance(ramp_enabled, bool):
                    raise LakeShore335ConfigurationError("Ramp enable must be a boolean")
                rate = _finite(ramp_rate_k_per_min, "ramp rate")
                if not 0.1 <= rate <= 100:
                    raise LakeShore335ConfigurationError("Ramp rate must be within 0.1–100 K/min")
            state = self.read_control_configuration(sample_channel)
            output, channel = state["output"], state["channel"]
            if state["control_mode"] == 2 and not automatic:
                if heater_range == 0:
                    raise LakeShore335ConfigurationError(
                        "Use Heater Off to stop Zone control; changing its setpoint can re-enable the heater")
                if heater_range is not None or ramp_enabled is not None:
                    raise LakeShore335ConfigurationError(
                        "LS335 is in Zone mode: use Keep heater range and Keep ramp, or Auto with its Zone table")
                # A new target can select a different native profile even with
                # Keep selected. Verify its sensor and range limits first.
                from app.sample_heater_ranges import select_heater_range
                bands = self._read_zone_ranges(output, channel)
                try:
                    select_heater_range(target, bands)
                except ValueError as exc:
                    raise LakeShore335ConfigurationError(str(exc)) from exc
                if any(band["heater_range"] > maximum_range for band in bands):
                    raise LakeShore335ConfigurationError("LS335 Zone heater range exceeds configured limit")
                if state["output_type"] == "voltage" and any(band["heater_range"] > 1 for band in bands):
                    raise LakeShore335ConfigurationError("Voltage output Zone table supports only OFF or ON")
            if automatic:
                from app.sample_heater_ranges import select_heater_range
                bands = auto_heater_ranges
                source = "configured"
                if bands is None or bands == []:
                    bands = self._read_zone_ranges(output, channel)
                    source = "ls335_zone"
                try:
                    selected = select_heater_range(target, bands)
                except ValueError as exc:
                    raise LakeShore335ConfigurationError(str(exc)) from exc
                if selected["heater_range"] > maximum_range:
                    raise LakeShore335ConfigurationError("Automatic heater range exceeds configured limit")
                auto_native = state["control_mode"] == 2
                if auto_native:
                    if source != "ls335_zone" or ramp_enabled is not None:
                        raise LakeShore335ConfigurationError(
                            "LS335 is in Zone mode: use its Zone table and Keep ramp, or commission Closed Loop PID for custom automatic ranges")
                    # Let commissioned Zone profiles control the ramp/range.
                    # Do not overwrite a currently active zone's parameters.
                    if any(band["heater_range"] > maximum_range for band in bands):
                        raise LakeShore335ConfigurationError("LS335 Zone heater range exceeds configured limit")
                    if state["output_type"] == "voltage" and any(band["heater_range"] > 1 for band in bands):
                        raise LakeShore335ConfigurationError("Voltage output Zone table supports only OFF or ON")
                heater_range = None if auto_native else selected["heater_range"]
                auto_info = {"auto_range_source": source, "auto_native_zone": auto_native,
                             "auto_target_range": selected["heater_range"],
                             "auto_upper_temperature_k": selected["upper_temperature_k"]}
            if state["output_type"] == "voltage" and heater_range not in {None, 0, 1}:
                raise LakeShore335ConfigurationError("Voltage output supports only OFF or ON")
            if heater_range != 0 and state["range"] > maximum_range:
                raise LakeShore335ConfigurationError("Existing heater range exceeds configured limit; use Heater Off before applying a new target")
            heating_active = state["control_mode"] == 2 or state["range"] > 0
            if heater_range != 0 and (heating_active or (heater_range is not None and heater_range > 0)):
                self._verify_sample_sensor(channel)
            # OFF is applied first, even if a later setpoint/ramp write fails.
            if heater_range == 0:
                self.heater_off(channel)
                heating_active = False
            enabling = heater_range is not None and heater_range > 0
            activation_attempted = False
            try:
                if ramp_enabled is not None:
                    activation_attempted = heating_active
                    self._write(f"RAMP {output},{int(ramp_enabled)},{rate:.9g}")
                    enabled_readback, rate_readback = self._parse_ramp(self._query(f"RAMP? {output}"))
                    if enabled_readback != ramp_enabled or not math.isclose(rate_readback, rate, abs_tol=1e-6):
                        raise LakeShore335CommunicationError("Ramp readback mismatch")
                if self.control_output_for_channel(channel) != output:
                    raise LakeShore335ConfigurationError("Sample output mapping changed during control")
                self._require_kelvin_input(channel)
                activation_attempted = heating_active
                self._set_output_setpoint(output, target)
                if heating_active:
                    self._verify_sample_sensor(channel)
                if enabling:
                    if self.control_output_for_channel(channel) != output:
                        raise LakeShore335ConfigurationError("Sample output mapping changed before heater activation")
                    self._require_kelvin_input(channel)
                    self._verify_sample_sensor(channel)
                    activation_attempted = True
                    self._write(f"RANGE {output},{heater_range}")
                    if self._parse_range(self._query(f"RANGE? {output}")) != heater_range:
                        raise LakeShore335CommunicationError("Heater-range readback mismatch")
                result = self.read_control_configuration(channel)
                if result["output"] != output or not math.isclose(result["setpoint"], target, rel_tol=1e-6, abs_tol=1e-6):
                    raise LakeShore335CommunicationError("Final setpoint readback mismatch")
                if heater_range is not None and result["range"] != heater_range:
                    raise LakeShore335CommunicationError("Final heater-range readback mismatch")
                if ramp_enabled is not None and (result["ramp_enabled"] != ramp_enabled
                        or not math.isclose(result["ramp_rate_k_per_min"], rate, abs_tol=1e-6)):
                    raise LakeShore335CommunicationError("Final ramp readback mismatch")
                if auto_native and result["control_mode"] != 2:
                    raise LakeShore335CommunicationError("LS335 left Zone mode during automatic control")
                if result["range"] > maximum_range:
                    raise LakeShore335CommunicationError("Active heater range exceeds configured limit")
                if (auto_native and result["range"] == 0 and selected["heater_range"] > 0
                        and (not result["ramp_enabled"] or result["ramp_rate_k_per_min"] == 0)):
                    # Resume an explicitly stopped output at a stationary
                    # setpoint, using its commissioned target-zone range.
                    self._verify_sample_sensor(channel)
                    self._write(f"RANGE {output},{selected['heater_range']}")
                    result = self.read_control_configuration(channel)
                    if (result["output"] != output or result["control_mode"] != 2
                            or result["range"] != selected["heater_range"]
                            or not math.isclose(result["setpoint"], target, rel_tol=1e-6, abs_tol=1e-6)):
                        raise LakeShore335CommunicationError("Stationary Zone activation readback mismatch")
                return {**result, **auto_info}
            except LakeShore335Error as exc:
                if not activation_attempted:
                    raise
                # A failed write can still reach the device. Shut down that
                # same output rather than leaving an unconfirmed activation.
                try:
                    self._shutdown_output(output, stop_zone_ramp=state["control_mode"] == 2)
                except LakeShore335Error as off_exc:
                    raise LakeShore335CommunicationError(f"{exc}; heater OFF unconfirmed: {off_exc}") from exc
                raise LakeShore335CommunicationError(f"{exc}; heater OFF confirmed") from exc

    def _verify_sample_sensor(self, channel):
        temperature = _finite(self._query(f"KRDG? {channel}"), "sample temperature")
        status = _finite(self._query(f"RDGST? {channel}"), "sample sensor status")
        if temperature <= 0 or status != 0:
            raise LakeShore335TelemetryError("A valid sample sensor is required to enable heating")

    def _set_output_setpoint(self, output, target):
        self._write(f"SETP {output},{target:.9g}")
        readback = _finite(self._query(f"SETP? {output}"), "setpoint readback")
        if not math.isclose(readback, target, rel_tol=1e-6, abs_tol=1e-6):
            raise LakeShore335CommunicationError(f"Setpoint readback mismatch: requested {target}, got {readback}")
        range_readback = self._query(f"RANGE? {output}")
        return {"output": output, "setpoint": readback, "range": range_readback}

    def set_sample_setpoint(self, temperature_k: float, sample_channel: str = "B") -> dict:
        with self._lock:
            target = _finite(temperature_k, "sample setpoint")
            if target <= 0:
                raise LakeShore335ConfigurationError("Sample setpoint must be positive")
            output = self.control_output_for_channel(sample_channel)
            self._require_kelvin_input(str(sample_channel).strip().upper())
            return self._set_output_setpoint(output, target)

    def heater_off(self, sample_channel: str = "B") -> dict:
        with self._lock:
            output, mode = self._control_output_mapping(sample_channel)
            return self._shutdown_output(output, stop_zone_ramp=mode == 2)

    def _shutdown_output(self, output, *, stop_zone_ramp=False):
        ramp_error = None
        if stop_zone_ramp:
            try:
                enabled, rate = self._parse_ramp(self._query(f"RAMP? {output}"))
                if enabled:
                    self._write(f"RAMP {output},0,{rate:.9g}")
                    if self._parse_ramp(self._query(f"RAMP? {output}"))[0]:
                        raise LakeShore335CommunicationError("Zone ramp stop readback mismatch")
            except LakeShore335Error as exc:
                ramp_error = exc
        # Always attempt range OFF even if ramp-stop confirmation failed.
        self._write(f"RANGE {output},0")
        range_readback = self._first_field(self._query(f"RANGE? {output}"))
        if range_readback not in {"0", "OFF", "0.0"}:
            raise LakeShore335CommunicationError(f"Heater-off readback failed: {range_readback!r}")
        if ramp_error is not None:
            raise LakeShore335CommunicationError(f"Range OFF confirmed; Zone ramp stop unconfirmed: {ramp_error}")
        return {"output": output, "range": range_readback, "confirmed": True,
                "zone_ramp_stopped": stop_zone_ramp}

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
    """Deterministic telemetry/control fake; no physical heater is driven."""

    def __init__(self, *args, sample_temperature_k=None, reservoir_temperature_k=None,
                 sample_sensor_status="0", reservoir_sensor_status="0", **kwargs):
        super().__init__(*args, **kwargs)
        self._mock_values = [sample_temperature_k, reservoir_temperature_k,
                             sample_sensor_status, reservoir_sensor_status]
        self._identity = "Lake Shore,Model 335,MOCK,1"
        self._resource = object()
        self.control_commands = []
        self._mock_control = {
            "OUTMODE? 1": "1,2,0", "OUTMODE? 2": "0,1,0",
            "INTYPE? A": "3,1,0,1,1", "INTYPE? B": "3,1,0,1,1",
            "HTRSET? 1": "0,2,1,0,1", "HTRSET? 2": "0,2,1,0,1",
            "SETP? 1": "4.2", "SETP? 2": "4.2",
            "RANGE? 1": "0", "RANGE? 2": "0",
            "RAMP? 1": "0,1", "RAMP? 2": "0,1",
            "HTR? 1": "0", "HTR? 2": "0",
        }
        self._mock_control.update({f"ZONE? {output},{zone}": "0,10,10,0,0,0,0,1"
                                   for output in (1, 2) for zone in range(1, 11)})

    def _query(self, command):
        self._require_connected()
        if command == "*IDN?":
            return self._identity
        if command.startswith("KRDG? "):
            return str(self._mock_values[0 if command.endswith("B") else 1])
        if command.startswith("RDGST? "):
            return str(self._mock_values[2 if command.endswith("B") else 3])
        try:
            return self._mock_control[command]
        except KeyError as exc:
            raise LakeShore335TelemetryError(f"Unsupported mock query: {command}") from exc

    def _write(self, command):
        self._require_connected()
        prefix, arguments = command.split(" ", 1)
        output, value = arguments.split(",", 1)
        if prefix not in {"SETP", "RAMP", "RANGE"} or output not in {"1", "2"}:
            raise LakeShore335TelemetryError(f"Unsupported mock write: {command}")
        self.control_commands.append(command)
        self._mock_control[f"{prefix}? {output}"] = value

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
