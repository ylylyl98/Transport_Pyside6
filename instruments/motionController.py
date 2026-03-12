import pyvisa

from .import PyvisaInstrument, InstrumentError
import time
import numpy as np


class EP300(PyvisaInstrument):
    available_axes = (1, 2, 3)

    def __init__(self,
                 name: str = 'EP300',
                 address: str = 'GPIB0::1::INSTR',
                 active_axes: tuple = (2,),
                 init_positions: tuple = None,
                 init_speeds: tuple = None,
                 test_mode=False):

        for axis in active_axes:
            if axis not in self.available_axes:
                raise InstrumentError(name, 'Axis number must be 1, 2 or 3.')

        outputs = ['axis{}'.format(i) for i in active_axes]
        super().__init__(name=name,
                         address=address,
                         output_channels=outputs,
                         test_mode=test_mode)

        self._active_axes = active_axes
        self._init_positions = init_positions
        self._init_speeds = init_speeds

    def connect(self):
        super().connect()
        if self._init_speeds is not None:
            for i, spd in zip(self._active_axes, self._init_speeds):
                self.set_speed(i, spd)
        if self._init_positions is not None:
            for i, pos in zip(self._active_axes, self._init_positions):
                self._set_position(i, pos)

    def close(self):
        for i in self._active_axes:
            self._turn_off_motor(i)
        super().close()

    def get_speed(self, i_axis: int) -> float:
        if i_axis not in self._active_axes:
            return np.nan
        try:
            return float(self._query('{}VA?'.format(i_axis)))
        except pyvisa.Error:
            return np.nan

    def set_speed(self, i_axis: int, spd: float):
        with self.lock:
            self._check_axis(i_axis)
            self._write('{}VA{}'.format(i_axis, spd))

    def motion_done(self, i_axis: int) -> bool:
        if int(self._query('{}MD?'.format(i_axis))) == 0:
            return False
        elif int(self._query('{}MD?'.format(i_axis))) == 1:
            return True

    def _turn_on_motor(self, i_axis: int):
        with self.lock:
            if not self.motor_is_on(i_axis):
                self._write('{}MO'.format(i_axis))

    def _turn_off_motor(self, i_axis: int):
        with self.lock:
            if self.motor_is_on(i_axis):
                self._write('{}MF'.format(i_axis))

    def motor_is_on(self, i_axis: int) -> bool:
        try:
            return int(self._query('{}MO?'.format(i_axis))) == 1
        except pyvisa.Error:
            return False

    def _set_position(self, i_axis: int, pos: float):
        with self.lock:
            self._check_axis(i_axis)
            self._write('{}PA{};{}WS100'.format(i_axis, pos, i_axis))
        time.sleep(0.1)
        while not self.motion_done(i_axis):
            time.sleep(0.1)

    def get_position(self, i_axis: int) -> float:
        if i_axis not in self._active_axes:
            return np.nan
        try:
            return float(self._query('{}PA?'.format(i_axis)))
        except pyvisa.Error:
            return np.nan

    def _check_axis(self, i_axis: int):
        if i_axis not in self._active_axes:
            raise InstrumentError(self.name, 'axis {} is not active.'.format(i_axis))
