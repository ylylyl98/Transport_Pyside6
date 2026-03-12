from . import Instrument
import subprocess
from typing import Union, Tuple
import numpy as np
try:
    # Import the .NET class library
    import clr, ctypes

    # Import python sys module
    import sys, os

    # numpy import
    import numpy as np

    # Import c compatible List and String
    from System import *
    from System.IO import *
    clr.AddReference('System.Collections')
    from System.Collections.Generic import List
    from System.Runtime.InteropServices import Marshal
    from System.Runtime.InteropServices import GCHandle, GCHandleType

    # Add needed dll references
    sys.path.append(os.environ['LIGHTFIELD_ROOT'])
    sys.path.append(os.environ['LIGHTFIELD_ROOT']+"\\AddInViews")
    clr.AddReference('PrincetonInstruments.LightFieldViewV5')
    clr.AddReference('PrincetonInstruments.LightField.AutomationV5')
    clr.AddReference('PrincetonInstruments.LightFieldAddInSupportServices')

    # PI imports
    from PrincetonInstruments.LightField.Automation import *
    from PrincetonInstruments.LightField.AddIns import *

    lf6_ready = True
except (ImportError, KeyError):
    lf6_ready = False


class LightField6(Instrument):

    SPEC_KEY = 'spectrum'
    WL_KEY = 'WL_calibration'

    def __init__(self, init_exp='PL', test_mode=False):

        self._auto = None
        self._application = None
        self._experiment = None
        self._init_exp = init_exp
        super().__init__(name='LightField',
                         address='LightField',
                         input_channels=(self.SPEC_KEY, self.WL_KEY),
                         test_mode=test_mode)

    def connect(self):
        # subprocess.call("TASKKILL /F /IM AddInProcess.exe", shell=True)
        self._auto = Automation(True, List[String]())
        self._application = self._auto.LightFieldApplication
        self._experiment = self._application.Experiment
        if self._experiment is not None:
            self.load_experiment(self._init_exp)

    def close(self):
        self.save_experiment('last_used')
        self._auto = None
        self._application = None
        self._experiment = None
        subprocess.call("TASKKILL /F /IM AddInProcess.exe", shell=True)
        super().close()

    def is_connected(self):
        if self._experiment is None:
            return False
        else:
            try:
                self._experiment.GetSavedExperiments()
                return True
            except:
                return False

    @ property
    def last_spectrum(self):
        return self._input_values[self.SPEC_KEY].copy()

    @ property
    def last_calibration(self):
        return self._input_values[self.WL_KEY].copy()

    def acquire(self):
        self._input_values[self.SPEC_KEY] = self._exposure()
        self._input_values[self.WL_KEY] = self.wavelength_calibration
        return super().acquire()

    @ property
    def saved_experiments(self):
        if self._experiment is None:
            return None
        else:
            return self._experiment.GetSavedExperiments()

    def print_saved_experiments(self):
        print("My Saved Experiments:")
        for saved_experiment in self._experiment.GetSavedExperiments():
            print("\t" + saved_experiment)

    def load_experiment(self, exp_name: str):
        load_success = self._experiment.Load(exp_name)
        if load_success:
            print('loading experiment {} successful'.format(exp_name))
        else:
            print('loading experiment {} failed'.format(exp_name))

    def save_experiment(self, experiment_name: str):
        self._experiment.SaveAs(experiment_name)

    def _exposure(self) -> np.ndarray:
        frames = 1
        dataset = self._experiment.Capture(frames)
        image_data = dataset.GetFrame(0, frames - 1).GetData()
        image_frame = dataset.GetFrame(0, frames - 1)

        # Possible data types returned from acquisition
        image_format = image_frame.Format
        if (image_format == ImageDataFormat.MonochromeUnsigned16):
            data_type = ctypes.c_ushort
        elif (image_format == ImageDataFormat.MonochromeUnsigned32):
            data_type = ctypes.c_uint
        elif (image_format == ImageDataFormat.MonochromeFloating32):
            data_type = ctypes.c_float
        array = self._convert_buffer(image_data, data_type)
        return array

    def _change_exp_setting(self, setting, value):
        if self._experiment is None:
            return
        if self._experiment.Exists(setting):
            self._experiment.SetValue(setting, value)

    def _get_exp_setting(self, setting):
        if self._experiment is None:
            return None
        elif self._experiment.Exists(setting):
            return self._experiment.GetValue(setting)
        else:
            return None

    @ property
    def center_wavelength(self) -> float:
        if self._experiment is not None:
            return self._get_exp_setting(SpectrometerSettings.GratingCenterWavelength)
        else:
            return np.nan

    def set_center_wavelength(self, value: float):
        self._change_exp_setting(SpectrometerSettings.GratingCenterWavelength, value)

    @ property
    def exposure_time(self) -> float:
        if self._experiment is not None:
            return self._get_exp_setting(CameraSettings.ShutterTimingExposureTime)
        else:
            return np.nan

    def set_exposure_time(self, t: float):
        self._change_exp_setting(CameraSettings.ShutterTimingExposureTime, t)

    @ property
    def shutter_timing_mode(self):
        if self._experiment is not None:
            return self._get_exp_setting(CameraSettings.ShutterTimingMode)
        else:
            return np.nan

    def set_shutter_timing_mode(self, mode: int):
        '''
                1: open for exposure then close
                2: always closed
                3: always open
                4: open when acquisition starts
                '''
        assert mode in [1, 2, 3, 4]
        self._change_exp_setting(CameraSettings.ShutterTimingMode, mode)

    # Creates a numpy array from our acquired buffer
    def _convert_buffer(self, net_array, data_type):
        src_hndl = GCHandle.Alloc(net_array, GCHandleType.Pinned)
        try:
            src_ptr = src_hndl.AddrOfPinnedObject().ToInt64()
            buf_type = data_type*len(net_array)
            cbuf = buf_type.from_address(src_ptr)
            resultArray = np.frombuffer(cbuf, dtype=cbuf._type_)

        # Free the handle
        finally:
            if src_hndl.IsAllocated: src_hndl.Free()

        # Make a copy of the buffer
        return np.copy(resultArray)

    def get_wavelength_calibration(self, as_array=True) -> Union[list, np.ndarray]:
        net_array = self._experiment.SystemColumnCalibration
        if as_array:
            return self._convert_buffer(net_array, ctypes.c_double)
        else:
            return list(self._convert_buffer(net_array, ctypes.c_double))

    @ property
    def wavelength_calibration(self):
        if self._experiment is not None:
            return self.get_wavelength_calibration(as_array=True)
        else:
            return np.array([])


if __name__ == '__main__':
    print(lf6_ready)
