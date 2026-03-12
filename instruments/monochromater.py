import pyvisa
import numpy as np
from .import PyvisaInstrument


class SP2300(PyvisaInstrument):
    OUTPUT = 'center_wl'

    def __init__(self,
                 name: str = 'SP2300',
                 address: str = '',
                 initial_wl: float = 633.0,
                 speed: float = 300.0,
                 test_mode=False
                 ):
        super().__init__(name=name, output_channels=(self.OUTPUT,), address=address, termination='\r',
                         test_mode=test_mode)
        self._init_wl = initial_wl
        self._init_speed = speed

    def connect(self):
        super().connect()
        print(self.get_identity())
        self.set_speed(self._init_speed)
        self._wl_goto(self._init_wl)

    def close(self):
        super().close()

    @ property
    def speed(self):
        if not self.connected:
            return np.nan
        else:
            return float(self._query('?NM/MIN'))

    def set_speed(self, speed: float):
        with self.lock:
            statement = '%.5f NM/MIN' % speed
            self._query(statement)

    @ property
    def center_wavelength(self):
        if not self.connected:
            return np.nan
        else:
            string = self._query('?NM')
            wavelength = float(string.split(' ')[1])
            return wavelength

    def _wl_goto(self, wavelength: float):
        with self.lock:
            statement = '%.5f GOTO' % wavelength
            # check the format of the data here
            self._query(statement)

    def set_outputs(self, values: dict):
        super().set_outputs(values)
        self._wl_goto(self._output_values[self.OUTPUT])

    def set_wavelength(self, wavelength: float):
        self.set_outputs({self.OUTPUT: wavelength})

    def refresh(self, keys=None):
        self._output_values[self.OUTPUT] = self.center_wavelength

