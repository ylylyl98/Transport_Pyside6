from .instrument import Instrument, PyvisaInstrument, InstrumentError, next_value
from .DaqCard import DaqCard
from .Keithley import Keithley2400CurrMode, Keithley2400OhmMode, Keithley2400VoltMode
from .monochromater import SP2300
from .lightField6 import LightField6
from .motionController import EP300
from .SR830 import SR830


supported_instruments = {'Keithley2400VoltMode': Keithley2400VoltMode,
                         'Keithley2400OhmMode': Keithley2400OhmMode,
                         'Keithley2400CurrMode': Keithley2400CurrMode,
                         'SP2300': SP2300,
                         'DaqCard': DaqCard,
                         'LightField6': LightField6,
                         'EP300': EP300,
                         'SR830': SR830}
