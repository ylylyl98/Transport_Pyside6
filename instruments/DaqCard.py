import math
from typing import Union, Dict
import nidaqmx
import numpy as np
# from nidaqmx._task_modules.channels import AIChannel, AOChannel
from nidaqmx.task.channels import AIChannel, AOChannel
from nidaqmx.constants import DigitalWidthUnits

from instruments import Instrument, InstrumentError


class DaqCard(Instrument):

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
            ch_out = self._ao_task.ao_channels.add_ao_voltage_chan(ch_out_name, max_val=max_output)
            self._ao_channels['ao{}'.format(ch_number)] = ch_out
            ch_in = self._ai_task.ai_channels.add_ai_voltage_chan(ch_in_name, max_val=max_output)
            self._ai_channels['ao{}_vs_ao_gnd'.format(ch_number)] = ch_in
        except nidaqmx.errors.Error as e:
            print('{}: Failed to add channel ao{}'.format(self._name, ch_number))
            raise e

    def connect(self):
        try:
            self._ai_task = nidaqmx.Task()
            self._ao_task = nidaqmx.Task()

            for i, max_input in zip(self._ai_channel_indexes, self._init_max_inputs):
                self._add_ai_channel(i, max_input=max_input)
            for j, max_output in zip(self._ao_channel_indexes, self._init_max_outputs):
                self._add_ao_channel(j, max_output=max_output)

            self.set_read_delay(self._init_read_delay)
            super().connect()
            self.refresh()
        except nidaqmx.errors.Error as e:
            print('DaqCard {}: connection failed.'.format(self._address))
            raise e

    def close(self):
        self._ai_task.close()
        self._ao_task.close()
        self._ai_task, self._ao_task = None, None

    def is_connected(self):
        if self._ai_task is None or self._ao_task is None:
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
        if self._ai_task is not None:
            return self._ao_task.timing.delay_from_samp_clk_delay
        else:
            return np.nan

    def set_write_delay(self, value: float):
        with self.lock:
            self._ao_task.timing.delay_from_samp_clk_delay_units = DigitalWidthUnits.SECONDS
            self._ao_task.timing.delay_from_samp_clk_delay = value

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

    def _write_voltages(self):
        values = [self._output_values['ao{}'.format(i)] for i in self._ao_channel_indexes]
        with self.lock:
            self._ao_task.write(values)

    def set_outputs(self, values: dict):
        super().set_outputs(values)
        self._write_voltages()

    def set_voltages(self, ao_indexes: Union[tuple, list], values: Union[tuple, list]):
        values = {'ao{}'.format(i): v for i, v in zip(ao_indexes, values)}
        self.set_outputs(values)

    def set_voltage(self, ao_index: int, value: float):
        self.set_outputs({'ao{}'.format(ao_index): value})
        self.acquire()

    def ramp_voltage(self, ao_index: int, target: float, step: float):
        if np.isclose(step, 0.0):
            raise InstrumentError(self.name, 'step cannot be 0.')
        self.refresh(keys=['ao{}'.format(ao_index)])
        start = self.get_ao_value(ao_index)
        if target > start:
            step = abs(step)
        else:
            step = -abs(step)
        for v in np.arange(start, target, step):
            self.set_voltage(ao_index, v)
        self.set_voltage(ao_index, target)

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

    def refresh(self, keys=None):
        keys = self.output_channels if keys is None else keys
        with self.lock:
            self.acquire()
            for k in keys:
                if k in self.output_channels:
                    self._output_values[k] = self._ao_vs_gnd_values[k]
