import math

import pyvisa
import numpy as np
from threading import RLock
from typing import Union, List, Dict, Any, Tuple
from pyvisa.resources import GPIBInstrument, SerialInstrument


# base class for all the instruments. Other instruments inherit this class
class Instrument:
    def __init__(self,
                 name: str = 'null_instrument',
                 address: str = 'null_address',
                 output_channels: Union[tuple, list] = (),
                 input_channels: Union[tuple, list] = (),
                 test_mode=False,
                 ):
        self._name = name
        self._address = address
        self._output_channels = list(output_channels)
        self._input_channels = list(input_channels)
        self._output_values = {ch_out: None for ch_out in self.output_channels}
        self._input_values = {ch_in: None for ch_in in self.input_channels}
        self._test_mode = test_mode
        self.lock = RLock()

    @ property
    def name(self) -> str:
        return self._name

    @ property
    def address(self) -> str:
        return self._address

    @ property
    def output_channels(self):
        return self._output_channels

    @ property
    def input_channels(self):
        return self._input_channels

    @ property
    def output_values(self):
        return self._output_values.copy()

    @ property
    def input_values(self):
        return self._input_values.copy()

    @ property
    def test_mode(self) -> bool:
        return self._test_mode

    @ property
    def connected(self) -> bool:
        return self.is_connected()

    # override this
    def connect(self):
        pass

    # override this
    def close(self):
        pass

    # override this
    def is_connected(self):
        return True

    # override this
    def set_outputs(self, values: dict):
        for key in values.keys():
            if key in self.output_channels:
                self._output_values[key] = values[key]
            else:
                raise InstrumentError(self.name, 'Output channel {} not found.'.format(key))

    # override this
    def refresh(self, keys=None):
        pass

    # override this
    def acquire(self) -> dict:
        return self._input_values.copy()

    def __enter__(self):
        self.connect()

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()


class PyvisaInstrument(Instrument):

    def __init__(self,
                 name: str = None,
                 address: str = None,
                 output_channels: Union[tuple, list] = (),
                 input_channels: Union[tuple, list] = (),
                 termination: str = '\n',
                 timeout: float = 5000,
                 test_mode=False,
                 ):
        super().__init__(name=name, address=address, output_channels=output_channels, input_channels=input_channels,
                         test_mode=test_mode)
        # r/w termination, '\r' or '\n'
        self.termination: str = termination
        # pyvisa resource manager
        self._rm: pyvisa.ResourceManager = pyvisa.ResourceManager()
        self._my_instr: Union[GPIBInstrument, SerialInstrument, None] = None
        self._init_timeout: Union[float, None] = timeout

    def connect(self):
        self._my_instr = self._rm.open_resource(self._address, timeout=self._init_timeout)
        self._my_instr.read_termination = self.termination
        self._my_instr.write_termination = self.termination
        super().connect()
        return self

    def close(self):
        self._my_instr.close()
        self._my_instr = None
        super().close()

    def _query(self, command: str, print_command=False, print_response=False):
        if not self.is_connected():
            raise InstrumentError(self.name, 'Trying to query while not connected.')
        try:
            if print_command:
                print(command)
            response = self._my_instr.query(command)
            if print_response:
                print(response)
            return response
        except pyvisa.Error as e:
            print('{}: PYVISA query failed: {}'.format(self.address, command))
            raise e

    def _write(self, command: str, print_command=False):
        if not self.is_connected():
            raise InstrumentError(self.name, 'Trying to write while not connected.')
        try:
            if print_command:
                print(command)
            return self._my_instr.write(command)
        except pyvisa.Error as e:
            print('{}: PYVISA write failed: {}'.format(self.address, command))
            raise e

    def _read(self, print_response=False):
        if not self.is_connected():
            raise InstrumentError(self.name, 'Trying to read while not connected.')
        try:
            response = self._my_instr.read()
            if print_response:
                print(response)
            return response
        except pyvisa.Error as e:
            print('{}: PYVISA read failed.'.format(self.address))
            raise e

    def get_identity(self):
        return self._query('*IDN?')

    def is_connected(self):
        if self._my_instr is None:
            return False
        return True

    @ property
    def timeout(self):
        if self._my_instr is None:
            return None
        else:
            return self._my_instr.timeout

    @ timeout.setter
    def timeout(self, value: float):
        self._my_instr.timeout = value


class InstrumentError(Exception):
    def __init__(self, instr_name: str, message: str):
        super().__init__()
        self.message = '{}: {}'.format(instr_name, message)

    def __str__(self):
        return self.message


def next_value(tgt: float, step: float, value: float):
    if math.isclose(tgt, value):
        return tgt
    elif abs(tgt - value) <= step:
        return tgt
    elif tgt - value > step:
        return value + step
    elif value - tgt > step:
        return value - step

