import math
from typing import Union, Dict
import nidaqmx
import numpy as np
# from nidaqmx._task_modules.channels import AIChannel, AOChannel
from nidaqmx.task.channels import AIChannel, AOChannel
from nidaqmx.constants import DigitalWidthUnits

from instruments import Instrument, InstrumentError


class DaqCard(Instrument):

    MAX_SINGLE_STEP_V = 0.05

    def __init__(self,
                 name: str = 'Daq1',
                 address: str = 'Dev1',
                 ao_channel_indexes: Union[tuple, list] = (0, 1),
                 ai_channel_indexes: Union[tuple, list] = (0, 1, 2, 3),
                 max_outputs: Union[float, int, list, tuple] = 10.0,
                 max_inputs: Union[float, int, list, tuple] = 10.0,
                 read_delay: float = 0.1,
                 test_mode=False):

        self._ao_task: Union[nidaqmx.Task, None] = None
        self._ao_tasks: Dict[str, nidaqmx.Task] = {}
        self._ai_task: Union[nidaqmx.Task, None] = None

        self._ao_channels: Dict[str, AOChannel] = {}
        self._ai_channels: Dict[str, AIChannel] = {}

        self._ao_channel_indexes = list(ao_channel_indexes)
        self._ai_channel_indexes = list(ai_channel_indexes)

        n_output = len(ao_channel_indexes)
        n_input = len(ai_channel_indexes)

        self._init_max_outputs = [max_outputs]*n_output if isinstance(max_outputs, (float, int)) else list(max_outputs)
        self._init_max_inputs = [max_inputs] * n_input if isinstance(max_inputs, (float, int)) else list(max_inputs)

        self._init_read_delay = read_delay

        try:
            assert len(self._init_max_outputs) == n_output
            assert len(self._init_max_inputs) == n_input
        except AssertionError:
            raise InstrumentError(self.name, 'Check the dimensionality of max_outputs, max_inputs.')

        self._ao_vs_gnd_values: Dict[str, float] = {'ao{}'.format(i): np.nan for i in self._ao_channel_indexes}
        self._initial_ao_values: Dict[str, float] = {'ao{}'.format(i): np.nan for i in self._ao_channel_indexes}

        super().__init__(name=name, address=address,
                         output_channels=['ao{}'.format(i) for i in self._ao_channel_indexes],
                         input_channels=['ai{}'.format(i) for i in self._ai_channel_indexes],
                         test_mode=test_mode)

    def _add_ai_channel(self, ch_number: int, max_input: float = 10.0):
        ch_address = self._ai_address(ch_number)
        try:
            ch = self._ai_task.ai_channels.add_ai_voltage_chan(ch_address, max_val=max_input)
            self._ai_channels['ai{}'.format(ch_number)] = ch
        except nidaqmx.errors.Error as e:
            print('{}: Failed to add channel {}'.format(self._name, ch_address))
            raise e

    def _add_ao_channel(self, ch_number: int, max_output: float = 10.0):
        try:
            ch_out_name, ch_in_name = self._ao_address(ch_number), self._ao_vs_gnd_address(ch_number)
            task = nidaqmx.Task()
            ch_out = task.ao_channels.add_ao_voltage_chan(ch_out_name, max_val=max_output)
            self._ao_tasks['ao{}'.format(ch_number)] = task
            self._ao_channels['ao{}'.format(ch_number)] = ch_out
            ch_in = self._ai_task.ai_channels.add_ai_voltage_chan(ch_in_name, max_val=max_output)
            self._ai_channels['ao{}_vs_ao_gnd'.format(ch_number)] = ch_in
        except nidaqmx.errors.Error as e:
            print('{}: Failed to add channel ao{}'.format(self._name, ch_number))
            raise e

    def connect(self):
        try:
            self._ai_task = nidaqmx.Task()
            self._ao_task = None

            for i, max_input in zip(self._ai_channel_indexes, self._init_max_inputs):
                self._add_ai_channel(i, max_input=max_input)
            for j, max_output in zip(self._ao_channel_indexes, self._init_max_outputs):
                self._add_ao_channel(j, max_output=max_output)

            self.set_read_delay(self._init_read_delay)
            super().connect()
            self.acquire()
            # Adopt the measured voltages as ramp starting points without
            # issuing any AO write. Existing hardware outputs are preserved.
            for key in self.output_channels:
                measured = float(self._ao_vs_gnd_values[key])
                if not math.isfinite(measured):
                    raise InstrumentError(self.name, f'Unable to establish the existing {key} voltage safely.')
                self._output_values[key] = measured
                self._initial_ao_values[key] = measured
        except Exception:
            try:
                self.close()
            except Exception:
                pass
            print('DaqCard {}: connection failed.'.format(self._address))
            raise
        return self

    def close(self):
        if self._ai_task is not None:
            self._ai_task.close()
        for task in self._ao_tasks.values():
            task.close()
        self._ao_tasks.clear()
        self._ao_channels.clear()
        self._ai_channels.clear()
        self._ai_task, self._ao_task = None, None

    def is_connected(self):
        if self._ai_task is None or len(self._ao_tasks) != len(self._ao_channel_indexes):
            return False
        else:
            try:
                self._ai_task.read()
                return True
            except nidaqmx.errors.Error:
                return False

    @ property
    def ao_channel_indexes(self):
        return self._ao_channel_indexes

    @ property
    def ai_channel_indexes(self):
        return self._ai_channel_indexes

    @ property
    def n_output(self):
        return len(self._ao_channel_indexes)

    @ property
    def n_input(self):
        return len(self._ai_channel_indexes)

    @ property
    def output_vs_gnd_values(self):
        return self._ao_vs_gnd_values.copy()

    @ property
    def initial_output_values(self):
        return self._initial_ao_values.copy()

    def get_max_input(self, i: int) -> float:
        return self._get_ai_channel(i).ai_max

    def set_max_input(self, i: int, value: float):
        with self.lock:
            self._get_ai_channel(i).ai_max = value

    def get_max_output(self, i: int) -> float:
        return self._get_ao_channel(i).ao_max

    def set_max_output(self, i: int, value: float):
        with self.lock:
            self._get_ao_channel(i).ao_max = value

    @ property
    def write_delay(self):
        if self._ao_tasks:
            return next(iter(self._ao_tasks.values())).timing.delay_from_samp_clk_delay
        else:
            return np.nan

    def set_write_delay(self, value: float):
        with self.lock:
            for task in self._ao_tasks.values():
                task.timing.delay_from_samp_clk_delay_units = DigitalWidthUnits.SECONDS
                task.timing.delay_from_samp_clk_delay = value

    @ property
    def read_delay(self):
        if self._ai_task is not None:
            return self._ai_task.timing.delay_from_samp_clk_delay
        else:
            return np.nan

    def set_read_delay(self, value: float):
        # set the unit to second
        with self.lock:
            self._ai_task.timing.delay_from_samp_clk_delay_units = DigitalWidthUnits.SECONDS
            self._ai_task.timing.delay_from_samp_clk_delay = value

    @ property
    def read_delay_unit(self):
        if self._ai_task is not None:
            return self._ai_task.timing.delay_from_samp_clk_delay_units
        else:
            return None

    def _ai_address(self, i: int) -> str:
        return '{}/ai{}'.format(self._address, i)

    def _ao_address(self, i: int) -> str:
        return '{}/ao{}'.format(self._address, i)

    def _ao_vs_gnd_address(self, i: int) -> str:
        return '{}/_ao{}_vs_aognd'.format(self._address, i)

    def _get_ai_channel(self, i: int) -> AIChannel:
        return self._ai_channels.get('ai{}'.format(i))

    def _get_ao_channel(self, i: int) -> AOChannel:
        return self._ao_channels.get('ao{}'.format(i))

    def _write_voltage(self, key: str, value: float):
        task = self._ao_tasks.get(key)
        if task is None:
            raise InstrumentError(self.name, f'Output channel {key} is unavailable.')
        with self.lock:
            task.write(float(value))
            try:
                task.stop()
            except Exception:
                pass

    def set_outputs(self, values: dict):
        with self.lock:
            checked = {}
            for key, raw_value in values.items():
                if key not in self.output_channels:
                    raise InstrumentError(self.name, 'Output channel {} not found.'.format(key))
                value = float(raw_value)
                current = float(self._output_values[key])
                channel_index = int(key[2:])
                max_output = abs(float(self.get_max_output(channel_index)))
                if not math.isfinite(value):
                    raise InstrumentError(self.name, f'{key} target must be finite.')
                if abs(value) > max_output + 1e-12:
                    raise InstrumentError(self.name, f'{key} target {value:g} V exceeds its ±{max_output:g} V range.')
                if not math.isfinite(current):
                    raise InstrumentError(self.name, f'{key} present voltage is unknown; refusing an unbounded output change.')
                if abs(value - current) > self.MAX_SINGLE_STEP_V + 1e-9:
                    raise InstrumentError(
                        self.name,
                        f'{key} step from {current:g} V to {value:g} V exceeds the {self.MAX_SINGLE_STEP_V:g} V safety limit; use ramp_voltage.',
                    )
                checked[key] = value
            # Only requested channels are written. Unused AO channels are
            # never included and retain their held hardware value.
            for key, value in checked.items():
                self._write_voltage(key, value)
                self._output_values[key] = value

    def set_voltages(self, ao_indexes: Union[tuple, list], values: Union[tuple, list]):
        values = {'ao{}'.format(i): v for i, v in zip(ao_indexes, values)}
        self.set_outputs(values)

    def set_voltage(self, ao_index: int, value: float):
        self.set_outputs({'ao{}'.format(ao_index): value})
        self.acquire()

    def ramp_voltage(self, ao_index: int, target: float, step: float):
        target = float(target)
        requested_step = abs(float(step))
        if not math.isfinite(target):
            raise InstrumentError(self.name, 'target must be finite.')
        if not math.isfinite(requested_step) or np.isclose(requested_step, 0.0):
            raise InstrumentError(self.name, 'step cannot be 0.')
        max_output = abs(float(self.get_max_output(int(ao_index))))
        if abs(target) > max_output + 1e-12:
            raise InstrumentError(self.name, f'ao{ao_index} target {target:g} V exceeds its ±{max_output:g} V range.')
        step = min(requested_step, self.MAX_SINGLE_STEP_V)
        start = self.adopt_measured_output_as_ramp_start(ao_index)
        current = start
        while not np.isclose(current, target, rtol=0.0, atol=1e-12):
            direction = 1.0 if target > current else -1.0
            next_value = current + direction * min(step, abs(target - current))
            self.set_voltage(ao_index, next_value)
            current = next_value

    def acquire(self):
        with self.lock:
            voltages = self._ai_task.read()
            if not isinstance(voltages, list):
                voltages = [voltages]
            for i, v in zip(self._ai_channel_indexes, voltages[: self.n_input]):
                self._input_values['ai{}'.format(i)] = v
            for i, v in zip(self._ao_channel_indexes, voltages[self.n_input:]):
                self._ao_vs_gnd_values['ao{}'.format(i)] = v
        return self._input_values.copy()

    def get_ai_value(self, ai_index: int):
        return self._input_values['ai{}'.format(ai_index)]

    def get_ao_value(self, ao_index: int):
        return self._output_values['ao{}'.format(ao_index)]

    def get_ao_vs_gnd_value(self, ao_index: int):
        return self._ao_vs_gnd_values.get('ao{}'.format(ao_index))

    def adopt_measured_output_as_ramp_start(self, ao_index: int) -> float:
        """Resample one AO and adopt it as the start of an explicit ramp."""
        key = 'ao{}'.format(ao_index)
        if key not in self.output_channels:
            raise InstrumentError(self.name, f'Output channel {key} is unavailable.')
        self.acquire()
        measured = float(self._ao_vs_gnd_values[key])
        if not math.isfinite(measured):
            raise InstrumentError(self.name, f'Unable to establish the existing {key} voltage safely.')
        with self.lock:
            self._output_values[key] = measured
        return measured

    def get_ao_state(self, ao_index: int) -> dict[str, float]:
        key = 'ao{}'.format(ao_index)
        if key not in self.output_channels:
            raise InstrumentError(self.name, f'Output channel {key} is unavailable.')
        return {
            'commanded_v': float(self._output_values[key]),
            'measured_v': float(self._ao_vs_gnd_values[key]),
            'initial_v': float(self._initial_ao_values[key]),
        }

    def refresh(self, keys=None):
        # Refresh readback only. Never replace the commanded/held AO state
        # with a noisy ADC measurement.
        self.acquire()
