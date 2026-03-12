import numpy as np
import pyvisa

from instruments import PyvisaInstrument, InstrumentError
from typing import Union


class Keithley2400VoltMode(PyvisaInstrument):

    OUTPUT = 'voltage'
    INPUT = 'current'

    def __init__(self,
                 name: str = 'Keithley',
                 address: str = 'GPIB1::6',
                 curr_comp: float = 1E-8,
                 volt_comp: float = 10.0,
                 source_delay: float = 0.2,
                 test_mode=False):

        self._init_curr_comp: float = curr_comp
        self._init_volt_comp: float = volt_comp
        self._init_source_delay = source_delay

        super().__init__(name=name, address=address, output_channels=(self.OUTPUT,), input_channels=(self.INPUT,),
                         termination='\n', test_mode=test_mode)

    def connect(self):
        try:
            super().connect()
            self.get_identity()
            self._set_volt_mode()
            self.set_volt_step_mode()
            self.set_curr_limit(self._init_curr_comp)
            self.set_volt_limit(self._init_volt_comp)
            self.set_source_delay(self._init_source_delay)
            self.turn_on_output()
        except pyvisa.Error as e:
            print('Keithley {} connection failed.'.format(self.address))
            raise e

    @ property
    def source_function(self):
        return self._query(':SOUR:FUNC?')

    @ property
    def sense_function(self):
        return self._query(':SENS:FUNC?')

    @ property
    def voltage_mode(self):
        return self._query(':SOUR:VOLT:MODE?')

    @ property
    def trigger_count(self):
        return int(self._query('TRIG:COUN?'))

    def _set_volt_mode(self):
        with self.lock:
            if self.source_function != 'VOLT':
                self._write(':SOUR:FUNC VOLT')
            if self.sense_function != '\"VOLT:DC","CURR:DC\"':
                self._write(':SENS:FUNC \'CURR\'')
                self._write(':SENS:FUNC:CONC ON')
                self._write(':FORM:ELEM VOLT ,CURR')

    def turn_on_output(self):
        with self.lock:
            self._write(':OUTP ON')

    def set_volt_step_mode(self):
        with self.lock:
            if self.voltage_mode != 'FIX':
                self._write(':SOUR:VOLT:MODE FIXED')
            if self.trigger_count != 1:
                self._write('TRIG:COUN 1')

    @ property
    def curr_limit(self) -> float:
        if not self.connected:
            return np.nan
        try:
            return float(self._query(':SENS:CURR:PROT?'))
        except pyvisa.Error:
            return np.nan
        except TypeError:
            return np.nan

    def set_curr_limit(self, curr_compliance: float):
        with self.lock:
            self._write(':SENS:CURR:RANG %.6e' % curr_compliance)
            self._write(':SENS:CURR:PROT %.6e' % curr_compliance)

    @ property
    def source_delay(self) -> float:
        if not self.connected:
            return np.nan
        try:
            return float(self._query('SOUR:DEL?'))
        except pyvisa.Error:
            return np.nan
        except TypeError:
            return np.nan

    def set_source_delay(self, delay: float):
        with self.lock:
            self._write('SOUR:DEL %.6e' % delay)

    @ property
    def volt_limit(self) -> float:
        if not self.connected:
            return np.nan
        try:
            return float(self._query(':SOUR:VOLT:RANG?'))
        except pyvisa.Error:
            return np.nan
        except TypeError:
            return np.nan

    def set_volt_limit(self, volt_compliance: float):
        with self.lock:
            self._write(':SOUR:VOLT:RANG %.6e' % volt_compliance)
            self._init_volt_comp = volt_compliance

    @ property
    def voltage(self) -> Union[float, None]:
        return self._output_values.get(self.OUTPUT)

    def set_outputs(self, values: dict):
        super().set_outputs(values)
        self._write_voltage(self._output_values[self.OUTPUT])

    def set_voltage(self, value: float):
        self.set_outputs({self.OUTPUT: value})
        input_value = self.acquire()
        return input_value

    def _write_voltage(self, volt: float):
        with self.lock:
            self._write(':SOUR:VOLT:LEV %.6e' % volt)

    @ property
    def current(self) -> Union[float, None]:
        return self._input_values.get(self.INPUT)

    def _measure(self):
        with self.lock:
            raw_data = self._query('READ?')
            s = raw_data.split(',')
            volt = float(s[0])
            curr = float(s[1])
            return curr, volt

    def acquire(self):
        i, v = self._measure()
        self._input_values[self.INPUT] = i
        self._output_values[self.OUTPUT] = v
        return self._input_values.copy()

    def refresh(self, keys=None):
        self.acquire()

    def ramp_voltage(self, target: float, step: float):
        if np.isclose(step, 0.0):
            raise InstrumentError(self.name, 'step cannot be 0.')
        self.refresh()
        start = self.voltage
        if target > start:
            step = abs(step)
        else:
            step = -abs(step)
        for v in np.arange(start, target, step):
            self.set_voltage(v)
        input_value = self.set_voltage(target)
        return input_value

    # not tested yet.
    def set_volt_sweep_mode(self):
        with self.lock:
            self._write(':SOUR:VOLT:MODE SWE')
            # select linear staircase sweep
            self._write(':SOUR:SWE:SPAC LIN')

    # not tested yet.
    def sweep_voltage(self, start: float, stop: float, step: float):
        with self.lock:
            self.set_volt_sweep_mode()
            # select start
            self._write(':SOUR:VOLT:START %.6e' % start, print_command=True)
            # select stop
            self._write(':SOUR:VOLT:STOP %.6e' % stop, print_command=True)
            # select step
            if (stop - start) * step < 0:
                step = - step
            self._write(':SOUR:VOLT:STEP %.6e' % step, print_command=True)
            # set trigger count
            count = int((stop - start)/step + 1)
            self._write('TRIG:COUN %.0f' % count, print_command=True)
            # trigger sweep
            result = self._query('READ?', print_command=True)
            self.set_volt_step_mode()
            return result

class Keithley2400CurrMode(PyvisaInstrument):
    """
    Force current, measure voltage.
    Mirrors the style of your Keithley2400VoltMode class.
    """

    OUTPUT = 'current'
    INPUT = 'voltage'

    def __init__(self,
                name: str = 'Keithley',
                address: str = 'GPIB1::6',
                curr_comp: float = 1e-3,     # A
                volt_comp: float = 3.0,       # V
                source_delay: float = 0.2,
                use_4w: bool = True,
                test_mode: bool = False):

        # store init parameters as instance variables
        self._init_curr_comp = curr_comp
        self._init_volt_comp = volt_comp
        self._init_source_delay = source_delay
        self._use_4w = use_4w

        super().__init__(name=name, address=address,
                        output_channels=(self.OUTPUT,),
                        input_channels=(self.INPUT,),
                        termination='\n', test_mode=test_mode)


    # ---------- Connection / basic setup ----------

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
            print(f'Keithley {self.address} connection failed.')
            raise e
        

    @property
    def source_function(self):
        return self._query(':SOUR:FUNC?')

    @property
    def sense_function(self):
        return self._query(':SENS:FUNC?')

    @property
    def current_mode(self):
        return self._query(':SOUR:CURR:MODE?')

    @property
    def trigger_count(self):
        return int(self._query('TRIG:COUN?'))
    

    # ---------- Mode config ----------

    def _set_curr_mode(self):
        with self.lock:
            if self.source_function != 'CURR':
                self._write(':SOUR:FUNC CURR')
            # Sense voltage primarily; keep current readback via concurrent measure
            if self.sense_function != '\"VOLT:DC","CURR:DC\"':
                self._write(":SENS:FUNC 'VOLT'")
                self._write(':SENS:FUNC:CONC ON')
                # Return VOLT, CURR (same order you used)
                self._write(':FORM:ELEM VOLT,CURR')

    def turn_on_output(self):
        with self.lock:
            self._write(':OUTP ON')

    def turn_off_output(self):
        with self.lock:
            self._write(':OUTP OFF')

    def enable_4wire(self):
        with self.lock:
            self._write('SYST:RSEN ON')

    def disable_4wire(self):
        with self.lock:
            self._write('SYST:RSEN OFF')

    def set_curr_step_mode(self):
        with self.lock:
            if self.current_mode != 'FIX':
                self._write(':SOUR:CURR:MODE FIXED')
            if self.trigger_count != 1:
                self._write('TRIG:COUN 1')

    # ---------- Compliance / ranges / timing ----------

    @property
    def volt_limit(self) -> float:
        """Voltage compliance (V)."""
        if not self.connected:
            return np.nan
        try:
            return float(self._query(':SENS:VOLT:PROT?'))
        except (pyvisa.Error, TypeError):
            return np.nan

    def set_volt_limit(self, volt_compliance: float):
        """Set voltage compliance (V)."""
        with self.lock:
            self._write(':SENS:VOLT:PROT %.6e' % volt_compliance)
            # auto-range for voltage measurement unless you want manual:
            self._write(':SENS:VOLT:RANG:AUTO ON')
            self._init_volt_comp = volt_compliance

    @property
    def curr_range(self) -> float:
        """Source current range (A)."""
        if not self.connected:
            return np.nan
        try:
            return float(self._query(':SOUR:CURR:RANG?'))
        except (pyvisa.Error, TypeError):
            return np.nan

    def set_curr_range(self, curr_range_a: float):
        with self.lock:
            self._write(':SOUR:CURR:RANG %.6e' % curr_range_a)

    @property
    def source_delay(self) -> float:
        if not self.connected:
            return np.nan
        try:
            return float(self._query('SOUR:DEL?'))
        except (pyvisa.Error, TypeError):
            return np.nan

    def set_source_delay(self, delay: float):
        with self.lock:
            self._write('SOUR:DEL %.6e' % delay)

    def set_nplc(self, nplc: float = 1.0):
        """Integration time for voltage measurement."""
        with self.lock:
            self._write('SENS:VOLT:NPLC %.6g' % nplc)

    # ---------- Output getters/setters ----------

    @property
    def current(self) -> Union[float, None]:
        return self._output_values.get(self.OUTPUT)

    def _write_current(self, curr_a: float):
        with self.lock:
            self._write(':SOUR:CURR:LEV %.6e' % curr_a)

    def set_outputs(self, values: dict):
        super().set_outputs(values)
        self._write_current(self._output_values[self.OUTPUT])

    def set_current(self, value_a: float):
        """Set current (A) and return measurement dict {voltage: V}."""
        self.set_outputs({self.OUTPUT: value_a})
        input_value = self.acquire()
        return input_value

    @property
    def voltage(self) -> Union[float, None]:
        return self._input_values.get(self.INPUT)

    # ---------- Measurement / acquisition ----------

    def _measure(self):
        with self.lock:
            raw = self._query('READ?')
            s = raw.split(',')
            volt = float(s[0])
            curr = float(s[1])
            return volt, curr

    def acquire(self):
        v, i = self._measure()
        self._input_values[self.INPUT] = v      # measured voltage
        self._output_values[self.OUTPUT] = i    # sourced current (read back)
        return self._input_values.copy()

    def refresh(self, keys=None):
        self.acquire()

    # ---------- Ramping & sweeping ----------

    # def ramp_current(self, target: float, step: float):
    #     """
    #     Ramp source current to 'target' in steps of 'step' (A).
    #     Returns the final measurement dict.
    #     """
    #     if np.isclose(step, 0.0):
    #         raise InstrumentError(self.name, 'step cannot be 0.')
    #     self.refresh()
    #     start = self.current
    #     if start is None:
    #         # Query actual source level if cache is empty
    #         try:
    #             start = float(self._query(':SOUR:CURR:LEV?'))
    #         except Exception:
    #             start = 0.0

    #     step = abs(step) if target > start else -abs(step)
    #     for c in np.arange(start, target, step):
    #         self.set_current(c)
    #     return self.set_current(target)


    def ramp_current(self, target: float, step: float):
        """
        Ramp source current to 'target' in steps of 'step' (A).
        If step is too small for the instrument resolution, clamp it.
        """
        # Figure out current range to decide resolution
        try:
            curr_range = float(self._query(':SOUR:CURR:RANG?'))
        except Exception:
            curr_range = 1e-3  # fallback 1 mA range

        # Instrument resolution ~1e-4 of range (conservative)
        min_step = curr_range * 1e-4

        if np.isclose(step, 0.0) or abs(step) < min_step:
            step = min_step
            print(f"[warn] Ramp step too small, clamped to {step:.2e} A")

        self.refresh()
        start = self.current
        if start is None:
            try:
                start = float(self._query(':SOUR:CURR:LEV?'))
            except Exception:
                start = 0.0

        # If already at target, just set once
        if np.isclose(start, target, rtol=1e-6, atol=1e-12):
            return self.set_current(target)

        step = abs(step) if target > start else -abs(step)
        for c in np.arange(start, target, step):
            self.set_current(c)
        return self.set_current(target)



    
    # not tested yet.
    def set_curr_sweep_mode(self):
        with self.lock:
            self._write(':SOUR:CURR:MODE SWE')
            self._write(':SOUR:SWE:SPAC LIN')

    # not tested yet.
    def sweep_current(self, start: float, stop: float, step: float):
        """
        Program a linear staircase current sweep using the internal sweeper.
        Returns the raw READ? response string from the instrument.
        """
        with self.lock:
            self.set_curr_sweep_mode()
            self._write(':SOUR:CURR:START %.6e' % start, print_command=True)
            self._write(':SOUR:CURR:STOP %.6e' % stop, print_command=True)
            if (stop - start) * step < 0:
                step = -step
            self._write(':SOUR:CURR:STEP %.6e' % step, print_command=True)
            count = int((stop - start) / step + 1)
            self._write('TRIG:COUN %.0f' % count, print_command=True)
            result = self._query('READ?', print_command=True)
            self.set_curr_step_mode()
            return result