from .instrument import Instrument, PyvisaInstrument, InstrumentError, next_value
from .DaqCard import DaqCard
from .Keithley import Keithley2400CurrMode, Keithley2400OhmMode, Keithley2400VoltMode
from .monochromater import SP2300
from .motionController import EP300
from .SR830 import SR830, SR850, SRSLockin


_SUPPORTED_NAMES = ('Keithley2400VoltMode', 'Keithley2400OhmMode',
                    'Keithley2400CurrMode', 'SP2300', 'DaqCard', 'LightField6',
                    'EP300', 'SR830', 'SR850', 'SRSLockin')
__all__ = ['Instrument', 'PyvisaInstrument', 'InstrumentError', 'next_value',
           *_SUPPORTED_NAMES, 'supported_instruments']


def __getattr__(name):
    # Transport does not use LightField. Loading its optional CLR/vendor runtime
    # during every ordinary device-manager import adds substantial startup work.
    if name == 'LightField6':
        from .lightField6 import LightField6
        globals()[name] = LightField6
        return LightField6
    if name == 'supported_instruments':
        # Preserve the original public dict and actual class values when a
        # caller explicitly requests the complete registry.
        registry = {key: globals()[key] if key in globals() else __getattr__(key)
                    for key in _SUPPORTED_NAMES}
        globals()[name] = registry
        return registry
    raise AttributeError(f'module {__name__!r} has no attribute {name!r}')


def __dir__():
    return sorted(set(globals()) | set(__all__))
