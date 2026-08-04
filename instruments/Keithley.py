import math

import numpy as np
import pyvisa

from instruments import PyvisaInstrument, InstrumentError
from typing import Union

from app.keithley_modes import KEITHLEY_MODE_OHM_4W, KEITHLEY_MODE_VOLTAGE_2W


class Keithley2400Base(PyvisaInstrument):
    VOLT_OUTPUT = "voltage"
    CURR_INPUT = "current"
    RES_INPUT = "resistance"
    MIN_CURRENT_COMPLIANCE_A = 1e-9
    MAX_CURRENT_COMPLIANCE_A = 1.0
    CURRENT_RANGES_A = (1e-6, 10e-6, 100e-6, 1e-3, 10e-3, 100e-3, 1.0)
    DEFAULT_CURRENT_COMPLIANCE_A = 1e-6
    DEFAULT_SOURCE_VOLTAGE_LIMIT_V = 20.0
    MIN_SOURCE_VOLTAGE_LIMIT_V = 1e-3
    MAX_SOURCE_VOLTAGE_LIMIT_V = 200.0

    def __init__(
        self,
        name: str = "Keithley",
        address: str = "GPIB1::6",
        curr_comp: float = 1e-8,
        max_source_voltage: float = 20.0,
        source_delay: float = 0.2,
        test_mode=False,
        volt_comp: float | None = None,
    ):
        # ``volt_comp`` was historically passed as a voltage range. Keep it as
        # a compatibility alias while using an accurate name internally.
        if volt_comp is not None:
            max_source_voltage = volt_comp
        self._init_curr_comp = self._validated_current_compliance(curr_comp)
        self._max_source_voltage = self._validated_source_voltage_limit(max_source_voltage)
        self._init_source_delay = source_delay
        self._operating_mode = KEITHLEY_MODE_VOLTAGE_2W
        self._identity = ""
        self._connection_start_voltage: float | None = None
        super().__init__(
            name=name,
            address=address,
            output_channels=(self.VOLT_OUTPUT,),
            input_channels=(self.CURR_INPUT, self.RES_INPUT),
            termination="\n",
            test_mode=test_mode,
        )

    def connect(self):
        try:
            super().connect()
            self._identity = self.get_identity().strip()
            self._connection_start_voltage = self._ramp_existing_voltage_source_to_zero()
            self._write("*CLS")
            self._write(":FORM:ELEM VOLT,CURR")
        except Exception:
            if self.connected:
                try:
                    super().close()
                except Exception:
                    pass
            print(f"Keithley {self.address} connection failed.")
            raise
        return self

    def _ramp_existing_voltage_source_to_zero(self) -> float | None:
        """Read and safely zero an existing voltage-source setpoint on connect."""
        source_function = str(self._query(":SOUR:FUNC?")).strip().upper().replace('"', "").replace("'", "")
        if "VOLT" not in source_function:
            return None
        start = float(self._query(":SOUR:VOLT:LEV?"))
        if not math.isfinite(start):
            raise InstrumentError(self.name, "Existing voltage setpoint is not finite; connection was aborted.")
        if math.isclose(start, 0.0, abs_tol=1e-9):
            self._output_values[self.VOLT_OUTPUT] = 0.0
            return start

        from app.constants import SAFE_RAMP_STEP_T, SAFE_RAMP_STEP_V
        from app.utils import safe_ramp

        def write_existing_voltage(value: float):
            self._write(":SOUR:VOLT:LEV %.9g" % float(value))

        safe_ramp(
            write_existing_voltage,
            start,
            0.0,
            SAFE_RAMP_STEP_V,
            SAFE_RAMP_STEP_T,
        )
        self._output_values[self.VOLT_OUTPUT] = 0.0
        return start

    def close(self):
        try:
            self.turn_off_output()
        except Exception:
            pass
        super().close()

    @property
    def operating_mode(self) -> str:
        return self._operating_mode

    @property
    def identity(self) -> str:
        return self._identity

    @property
    def connection_start_voltage(self) -> float | None:
        return self._connection_start_voltage

    @property
    def max_source_voltage(self) -> float:
        return self._max_source_voltage

    @property
    def current_compliance(self) -> float:
        if not self.connected:
            return float(self._init_curr_comp)
        return float(self._query(":SENS:CURR:PROT?"))

    @property
    def source_function(self):
        return self._query(":SOUR:FUNC?")

    @property
    def sense_function(self):
        return self._query(":SENS:FUNC?")

    @property
    def trigger_count(self):
        return int(float(self._query("TRIG:COUN?")))

    @property
    def source_delay(self) -> float:
        if not self.connected:
            return np.nan
        try:
            return float(self._query("SOUR:DEL?"))
        except (pyvisa.Error, TypeError, ValueError):
            return np.nan

    def set_source_delay(self, delay: float):
        with self.lock:
            self._write("SOUR:DEL %.6e" % delay)

    def turn_on_output(self):
        with self.lock:
            self._write(":OUTP ON")

    def turn_off_output(self):
        with self.lock:
            self._write(":OUTP OFF")

    def set_2wire_voltage_source_mode(self):
        with self.lock:
            self._write("*CLS")
            self._write(":SYST:RSEN OFF")
            self._write(":SOUR:FUNC VOLT")
            self._write(":SOUR:VOLT:MODE FIXED")
            self._write(":SENS:FUNC 'CURR'")
            self._write(":SENS:FUNC:CONC ON")
            self._write(":FORM:ELEM VOLT,CURR")
            self._write("TRIG:COUN 1")
            self.set_current_compliance(self._init_curr_comp)
            self._write(":SOUR:VOLT:RANG %.6e" % self._max_source_voltage)
            self._write("SOUR:DEL %.6e" % self._init_source_delay)
            self._write(":SOUR:VOLT:LEV 0")
            self._output_values[self.VOLT_OUTPUT] = 0.0
            self._operating_mode = KEITHLEY_MODE_VOLTAGE_2W
            self.verify_protection_settings()
            self.turn_on_output()

    def set_4wire_ohm_mode(self):
        with self.lock:
            self.turn_off_output()
            self._write("*CLS")
            self._write(":SYST:RSEN ON")
            self._write(":CONF:FRES")
            self._write(":FORM:ELEM RES")
            self._write("TRIG:COUN 1")
            self._write("SOUR:DEL %.6e" % self._init_source_delay)
            self.turn_on_output()
            self._operating_mode = KEITHLEY_MODE_OHM_4W

    def configure_mode(self, mode: str):
        if mode == KEITHLEY_MODE_OHM_4W:
            self.set_4wire_ohm_mode()
        else:
            self.set_2wire_voltage_source_mode()

    @property
    def voltage(self) -> Union[float, None]:
        return self._output_values.get(self.VOLT_OUTPUT)

    @property
    def current(self) -> Union[float, None]:
        return self._input_values.get(self.CURR_INPUT)

    @property
    def resistance(self) -> Union[float, None]:
        return self._input_values.get(self.RES_INPUT)

    def _measure_voltage_mode(self):
        raw_data = self._query("READ?")
        s = raw_data.split(",")
        volt = float(s[0])
        curr = float(s[1])
        self._input_values[self.CURR_INPUT] = curr
        self._input_values[self.RES_INPUT] = None
        self._output_values[self.VOLT_OUTPUT] = volt
        return {self.CURR_INPUT: curr, self.VOLT_OUTPUT: volt}

    def _measure_ohm_mode(self):
        raw_data = self._query("READ?")
        s = raw_data.split(",")
        res = float(s[0])
        self._input_values[self.RES_INPUT] = res
        self._input_values[self.CURR_INPUT] = None
        return {self.RES_INPUT: res}

    def acquire(self):
        with self.lock:
            if self._operating_mode == KEITHLEY_MODE_OHM_4W:
                return self._measure_ohm_mode()
            return self._measure_voltage_mode()

    def refresh(self, keys=None):
        self.acquire()

    def set_outputs(self, values: dict):
        if self._operating_mode != KEITHLEY_MODE_VOLTAGE_2W:
            raise InstrumentError(self.name, "Voltage output is unavailable unless the Keithley is in 2-wire voltage mode.")
        super().set_outputs(values)
        self._write_voltage(self._output_values[self.VOLT_OUTPUT])

    def set_voltage_fast(self, value: float):
        if self._operating_mode != KEITHLEY_MODE_VOLTAGE_2W:
            raise InstrumentError(self.name, "Keithley is not configured for 2-wire voltage source mode.")
        super().set_outputs({self.VOLT_OUTPUT: value})
        self._write_voltage(self._output_values[self.VOLT_OUTPUT])
        return self.output_values

    def set_voltage(self, value: float):
        self.set_voltage_fast(value)
        return self.acquire()

    def get_voltage_setpoint(self) -> float:
        """Return the voltage level currently programmed into the source.

        Ramps must begin at the instrument's programmed source level instead of
        relying on a potentially stale UI-side cache.
        """
        if self._operating_mode != KEITHLEY_MODE_VOLTAGE_2W:
            raise InstrumentError(self.name, "Keithley is not configured for 2-wire voltage source mode.")
        with self.lock:
            return float(self._query(":SOUR:VOLT:LEV?"))

    def _write_voltage(self, volt: float):
        volt = float(volt)
        if not math.isfinite(volt):
            raise InstrumentError(self.name, "Voltage setpoint must be finite.")
        if abs(volt) > self._max_source_voltage + 1e-12:
            raise InstrumentError(
                self.name,
                f"Requested {volt:g} V exceeds the configured ±{self._max_source_voltage:g} V limit.",
            )
        with self.lock:
            self._write(":SOUR:VOLT:LEV %.6e" % volt)

    def set_current_compliance(self, current_a: float):
        current_a = self._validated_current_compliance(current_a)
        with self.lock:
            # A Keithley 2400 maintains a separate compliance range. Writing
            # the measurement range first can conflict with the old
            # compliance range and enqueue +824 ("Cannot exceed compliance
            # range"). Let the instrument move both ranges together.
            self._write(":SENS:CURR:RANG:AUTO OFF")
            self._write(":SENS:CURR:PROT:RSYN ON")
            self._write(":SENS:CURR:PROT %.6e" % current_a)
            self._init_curr_comp = current_a

    def apply_protection_settings(
        self,
        current_compliance_a: float,
        max_source_voltage_v: float,
    ) -> dict[str, object]:
        """Apply and verify live voltage-source limits without changing output."""
        current_compliance_a = self._validated_current_compliance(current_compliance_a)
        max_source_voltage_v = self._validated_source_voltage_limit(max_source_voltage_v)
        self.recommended_current_range(current_compliance_a)
        with self.lock:
            old_compliance = self._init_curr_comp
            old_voltage_limit = self._max_source_voltage
            try:
                self._write("*CLS")
                self.set_current_compliance(current_compliance_a)
                self._write(":SOUR:VOLT:RANG %.6e" % max_source_voltage_v)
                self._max_source_voltage = max_source_voltage_v
                return self.verify_protection_settings()
            except Exception:
                # Keep the software guard conservative and consistent with
                # the last verified profile after any partial instrument error.
                self._init_curr_comp = old_compliance
                self._max_source_voltage = old_voltage_limit
                raise

    def read_protection_settings(self, include_trip: bool = True) -> dict[str, object]:
        with self.lock:
            current_compliance = float(self._query(":SENS:CURR:PROT?"))
            current_autorange = bool(int(float(self._query(":SENS:CURR:RANG:AUTO?"))))
            current_range = float(self._query(":SENS:CURR:RANG?"))
            source_voltage_range = float(self._query(":SOUR:VOLT:RANG?"))
            tripped = None
            if include_trip:
                try:
                    tripped = bool(int(float(self._query(":SENS:CURR:PROT:TRIP?"))))
                except Exception:
                    tripped = None
            return {
                "max_source_voltage_v": float(self._max_source_voltage),
                "source_voltage_range_v": source_voltage_range,
                "current_range_a": current_range,
                "current_autorange": current_autorange,
                "current_compliance_a": current_compliance,
                "current_compliance_tripped": tripped,
            }

    def verify_protection_settings(self) -> dict[str, object]:
        errors = self.read_instrument_errors()
        if errors:
            raise InstrumentError(self.name, "Keithley error queue: " + "; ".join(errors))
        settings = self.read_protection_settings(include_trip=False)
        actual_compliance = float(settings["current_compliance_a"])
        actual_range = abs(float(settings["source_voltage_range_v"]))
        if not math.isclose(actual_compliance, self._init_curr_comp, rel_tol=0.02, abs_tol=1e-15):
            raise InstrumentError(
                self.name,
                f"Current compliance verification failed: requested {self._init_curr_comp:.6g} A, "
                f"instrument reports {actual_compliance:.6g} A.",
            )
        if actual_range + 1e-12 < self._max_source_voltage:
            raise InstrumentError(
                self.name,
                f"Voltage range verification failed: ±{self._max_source_voltage:g} V requested, "
                f"instrument reports ±{actual_range:g} V.",
            )
        return settings

    def read_instrument_errors(self, maximum: int = 32) -> list[str]:
        """Drain and return nonzero entries from the Keithley error queue."""
        errors: list[str] = []
        with self.lock:
            for _ in range(maximum):
                response = str(self._query(":SYST:ERR?")).strip()
                code_text = response.split(",", 1)[0].strip()
                try:
                    code = int(float(code_text))
                except ValueError:
                    errors.append(response or "Unparseable empty error response")
                    break
                if code == 0:
                    break
                errors.append(response)
            else:
                errors.append("Error queue did not clear")
        return errors

    @classmethod
    def recommended_current_range(cls, current_a: float) -> float:
        """Return the smallest 2400 range compatible with a compliance value."""
        value = cls._validated_current_compliance(current_a)
        for current_range in cls.CURRENT_RANGES_A:
            if current_range * 0.001 <= value <= current_range * 1.05:
                return float(current_range)
        raise ValueError(f"No Keithley current range supports {value:g} A compliance.")

    @classmethod
    def _validated_current_compliance(cls, current_a: float) -> float:
        value = float(current_a)
        if not math.isfinite(value) or not cls.MIN_CURRENT_COMPLIANCE_A <= value <= cls.MAX_CURRENT_COMPLIANCE_A:
            raise ValueError(
                f"Current compliance must be between {cls.MIN_CURRENT_COMPLIANCE_A:g} A "
                f"and {cls.MAX_CURRENT_COMPLIANCE_A:g} A."
            )
        return value

    @classmethod
    def _validated_source_voltage_limit(cls, voltage_v: float) -> float:
        value = float(voltage_v)
        if not math.isfinite(value) or not cls.MIN_SOURCE_VOLTAGE_LIMIT_V <= value <= cls.MAX_SOURCE_VOLTAGE_LIMIT_V:
            raise ValueError(
                f"Maximum source voltage must be between {cls.MIN_SOURCE_VOLTAGE_LIMIT_V:g} V "
                f"and {cls.MAX_SOURCE_VOLTAGE_LIMIT_V:g} V."
            )
        return value

    def ramp_voltage(self, target: float, step: float):
        if self._operating_mode != KEITHLEY_MODE_VOLTAGE_2W:
            raise InstrumentError(self.name, "Keithley is not configured for 2-wire voltage source mode.")
        if np.isclose(step, 0.0):
            raise InstrumentError(self.name, "step cannot be 0.")
        start = self.get_voltage_setpoint()
        step = abs(step) if target > start else -abs(step)
        for v in np.arange(start, target, step):
            self.set_voltage_fast(v)
        return self.set_voltage_fast(target)


class Keithley2400VoltMode(Keithley2400Base):
    def connect(self):
        try:
            super().connect()
            self.configure_mode(KEITHLEY_MODE_VOLTAGE_2W)
        except Exception:
            if self.connected:
                self.close()
            raise
        return self


class Keithley2400OhmMode(Keithley2400Base):
    def connect(self):
        try:
            super().connect()
            self.configure_mode(KEITHLEY_MODE_OHM_4W)
        except Exception:
            if self.connected:
                self.close()
            raise
        return self


class Keithley2400CurrMode(PyvisaInstrument):
    OUTPUT = "current"
    INPUT = "voltage"

    def __init__(
        self,
        name: str = "Keithley",
        address: str = "GPIB1::6",
        curr_comp: float = 1e-3,
        volt_comp: float = 3.0,
        source_delay: float = 0.2,
        use_4w: bool = True,
        test_mode: bool = False,
    ):
        self._init_curr_comp = curr_comp
        self._init_volt_comp = volt_comp
        self._init_source_delay = source_delay
        self._use_4w = use_4w
        super().__init__(
            name=name,
            address=address,
            output_channels=(self.OUTPUT,),
            input_channels=(self.INPUT,),
            termination="\n",
            test_mode=test_mode,
        )

    def connect(self):
        try:
            super().connect()
            self.get_identity()
            self._set_curr_mode()
            self.set_curr_step_mode()
            self.set_curr_range(self._init_curr_comp)
            self.set_volt_limit(self._init_volt_comp)
            self.set_source_delay(self._init_source_delay)
            if self._use_4w:
                self.enable_4wire()
            else:
                self.disable_4wire()
            self.turn_on_output()
        except pyvisa.Error as e:
            print(f"Keithley {self.address} connection failed.")
            raise e

    @property
    def source_function(self):
        return self._query(":SOUR:FUNC?")

    @property
    def sense_function(self):
        return self._query(":SENS:FUNC?")

    @property
    def current_mode(self):
        return self._query(":SOUR:CURR:MODE?")

    @property
    def trigger_count(self):
        return int(self._query("TRIG:COUN?"))

    def _set_curr_mode(self):
        with self.lock:
            if self.source_function != "CURR":
                self._write(":SOUR:FUNC CURR")
            if self.sense_function != '"VOLT:DC","CURR:DC"':
                self._write(":SENS:FUNC 'VOLT'")
                self._write(":SENS:FUNC:CONC ON")
                self._write(":FORM:ELEM VOLT,CURR")

    def turn_on_output(self):
        with self.lock:
            self._write(":OUTP ON")

    def turn_off_output(self):
        with self.lock:
            self._write(":OUTP OFF")

    def enable_4wire(self):
        with self.lock:
            self._write("SYST:RSEN ON")

    def disable_4wire(self):
        with self.lock:
            self._write("SYST:RSEN OFF")

    def set_curr_step_mode(self):
        with self.lock:
            if self.current_mode != "FIX":
                self._write(":SOUR:CURR:MODE FIXED")
            if self.trigger_count != 1:
                self._write("TRIG:COUN 1")

    @property
    def volt_limit(self) -> float:
        if not self.connected:
            return np.nan
        try:
            return float(self._query(":SENS:VOLT:PROT?"))
        except (pyvisa.Error, TypeError):
            return np.nan

    def set_volt_limit(self, volt_compliance: float):
        with self.lock:
            self._write(":SENS:VOLT:PROT %.6e" % volt_compliance)
            self._write(":SENS:VOLT:RANG:AUTO ON")
            self._init_volt_comp = volt_compliance

    @property
    def curr_range(self) -> float:
        if not self.connected:
            return np.nan
        try:
            return float(self._query(":SOUR:CURR:RANG?"))
        except (pyvisa.Error, TypeError):
            return np.nan

    def set_curr_range(self, curr_range_a: float):
        with self.lock:
            self._write(":SOUR:CURR:RANG %.6e" % curr_range_a)

    @property
    def source_delay(self) -> float:
        if not self.connected:
            return np.nan
        try:
            return float(self._query("SOUR:DEL?"))
        except (pyvisa.Error, TypeError):
            return np.nan

    def set_source_delay(self, delay: float):
        with self.lock:
            self._write("SOUR:DEL %.6e" % delay)

    def set_nplc(self, nplc: float = 1.0):
        with self.lock:
            self._write("SENS:VOLT:NPLC %.6g" % nplc)

    @property
    def current(self) -> Union[float, None]:
        return self._output_values.get(self.OUTPUT)

    def _write_current(self, curr_a: float):
        with self.lock:
            self._write(":SOUR:CURR:LEV %.6e" % curr_a)

    def set_outputs(self, values: dict):
        super().set_outputs(values)
        self._write_current(self._output_values[self.OUTPUT])

    def set_current(self, value_a: float):
        self.set_outputs({self.OUTPUT: value_a})
        return self.acquire()

    @property
    def voltage(self) -> Union[float, None]:
        return self._input_values.get(self.INPUT)

    def _measure(self):
        with self.lock:
            raw = self._query("READ?")
            s = raw.split(",")
            volt = float(s[0])
            curr = float(s[1])
            return volt, curr

    def acquire(self):
        v, i = self._measure()
        self._input_values[self.INPUT] = v
        self._output_values[self.OUTPUT] = i
        return self._input_values.copy()

    def refresh(self, keys=None):
        self.acquire()

    def ramp_current(self, target: float, step: float):
        try:
            curr_range = float(self._query(":SOUR:CURR:RANG?"))
        except Exception:
            curr_range = 1e-3
        min_step = curr_range * 1e-4
        if np.isclose(step, 0.0) or abs(step) < min_step:
            step = min_step
            print(f"[warn] Ramp step too small, clamped to {step:.2e} A")
        self.refresh()
        start = self.current
        if start is None:
            try:
                start = float(self._query(":SOUR:CURR:LEV?"))
            except Exception:
                start = 0.0
        if np.isclose(start, target, rtol=1e-6, atol=1e-12):
            return self.set_current(target)
        step = abs(step) if target > start else -abs(step)
        for c in np.arange(start, target, step):
            self.set_current(c)
        return self.set_current(target)

    def set_curr_sweep_mode(self):
        with self.lock:
            self._write(":SOUR:CURR:MODE SWE")
            self._write(":SOUR:SWE:SPAC LIN")

    def sweep_current(self, start: float, stop: float, step: float):
        with self.lock:
            self.set_curr_sweep_mode()
            self._write(":SOUR:CURR:START %.6e" % start, print_command=True)
            self._write(":SOUR:CURR:STOP %.6e" % stop, print_command=True)
            if (stop - start) * step < 0:
                step = -step
            self._write(":SOUR:CURR:STEP %.6e" % step, print_command=True)
            count = int((stop - start) / step + 1)
            self._write("TRIG:COUN %.0f" % count, print_command=True)
            result = self._query("READ?", print_command=True)
            self.set_curr_step_mode()
            return result
