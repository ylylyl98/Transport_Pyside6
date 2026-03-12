from .instrument import Instrument, PyvisaInstrument, InstrumentError, next_value
from .DaqCard import DaqCard
from .Keithley import Keithley2400VoltMode, Keithley2400CurrMode
from .monochromater import SP2300
from .lightField6 import LightField6
from .motionController import EP300


supported_instruments = {'Keithley2400VoltMode': Keithley2400VoltMode,
                         'Keithley2400CurrMode': Keithley2400CurrMode,
                         'SP2300': SP2300,
                         'DaqCard': DaqCard,
                         'LightField6': LightField6,
                         'EP300': EP300}