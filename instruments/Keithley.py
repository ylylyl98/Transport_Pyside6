import numpy as np
import pyvisa

from instruments import PyvisaInstrument, InstrumentError
from typing import Union

from app.keithley_modes import KEITHLEY_MODE_OHM_4W, KEITHLEY_MODE_VOLTAGE_2W


class Keithley2400Base(PyvisaInstrument):
    VOLT_OUTPUT = "voltage"
    CURR_INPUT = "current"
    RES_INPUT = "resistance"

    def __init__(
        self,
        name: str = "Keithley",
        address: str = "GPIB1::6",
        curr_comp: float = 1e-8,
        volt_comp: float = 10.0,
        source_delay: float = 0.2,
        test_mode=False,
    ):
        self._init_curr_comp = curr_comp
        self._init_volt_comp = volt_comp
        self._init_source_delay = source_delay
        self._operating_mode = KEITHLEY_MODE_VOLTAGE_2W
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
            self.get_identity()
            self._write("*CLS")
            self._write(":FORM:ELEM VOLT,CURR")
        except pyvisa.Error as e:
            print(f"Keithley {self.address} connection failed.")
            raise e
        return self

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
            self.turn_off_output()
            self._write("*CLS")
            self._write(":SYST:RSEN OFF")
            self._write(":SOUR:FUNC VOLT")
            self._write(":SOUR:VOLT:MODE FIXED")
            self._write(":SENS:FUNC 'CURR'")
            self._write(":SENS:FUNC:CONC ON")
            self._write(":FORM:ELEM VOLT,CURR")
            self._write("TRIG:COUN 1")
            self._write(":SENS:CURR:RANG %.6e" % self._init_curr_comp)
            self._write(":SENS:CURR:PROT %.6e" % self._init_curr_comp)
            self._write(":SOUR:VOLT:RANG %.6e" % self._init_volt_comp)
            self._write("SOUR:DEL %.6e" % self._init_source_delay)
            self.turn_on_output()
            self._operating_mode = KEITHLEY_MODE_VOLTAGE_2W

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
        with self.lock:
            self._write(":SOUR:VOLT:LEV %.6e" % volt)

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
        super().connect()
        self.configure_mode(KEITHLEY_MODE_VOLTAGE_2W)
        return self


class Keithley2400OhmMode(Keithley2400Base):
    def connect(self):
        super().connect()
        self.configure_mode(KEITHLEY_MODE_OHM_4W)
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
