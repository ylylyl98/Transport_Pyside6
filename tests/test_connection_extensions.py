import unittest
from unittest.mock import patch, Mock
from app.device_manager import EmergencyRampWorker
from app.models import Connections
from app.device_manager import DeviceManager

class _Emitter:
    def __init__(self): self.events = []
    def emit(self, *event): self.events.append(event)

class ConnectionExtensionsTests(unittest.TestCase):
    def test_g4_blank_is_skipped_and_duplicate_canonical_gpib_rejected(self):
        c = Connections(gate1='GPIB0::8::INSTR', gate4='GPIB0::08::INSTR')
        manager = DeviceManager(c)
        emitter = _Emitter()
        with patch('app.device_manager.Keithley2400VoltMode') as driver:
            manager._connect_keithley('g4', emitter)
            driver.assert_not_called()
        self.assertEqual(emitter.events[-1][1], 'err')
        c.gate4 = ''
        manager._connect_keithley('g4', emitter)
        self.assertEqual(emitter.events[-1][1], 'idle')

    def test_connect_all_skips_blank_g4_without_opening_session(self):
        c = Connections(gate1='', gate2='', gate3='', gate4='', daq_dev='', mono='', lockin='', drive_lockin='')
        manager = DeviceManager(c)
        emitter = _Emitter()
        with patch('app.device_manager.Keithley2400VoltMode') as driver:
            manager._connect_all_in_thread(emitter)
            driver.assert_not_called()
        self.assertIn(('g4', 'idle', ''), emitter.events)

    def test_g4_emergency_and_shutdown_zero_before_close(self):
        manager = DeviceManager(Connections())
        source = Mock()
        source.get_voltage_setpoint.return_value = 0.5
        manager.sessions['g4'] = source
        with patch('app.utils.safe_ramp') as ramp:
            EmergencyRampWorker(manager.sessions, []).run()
            self.assertEqual(ramp.call_args.args[0], source.set_voltage)
            self.assertEqual(ramp.call_args.args[2], 0.0)
        source.get_voltage_setpoint.side_effect = [0.5, 0.0]
        with patch('app.utils.safe_ramp') as ramp:
            manager.shutdown()
            self.assertEqual(ramp.call_args.args[0], source.set_voltage)
            source.close.assert_called_once()
        self.assertIsNone(manager.sessions['g4'])

    def test_g4_failed_zero_preserves_session_on_disconnect(self):
        manager = DeviceManager(Connections())
        source = Mock()
        source.get_voltage_setpoint.return_value = 0.5
        manager.sessions['g4'] = source
        with patch('app.utils.safe_ramp', side_effect=RuntimeError('failure')):
            with self.assertRaisesRegex(RuntimeError, 'Disconnect aborted'):
                manager._disconnect_all_in_thread(_Emitter())
        self.assertIs(manager.sessions['g4'], source)
        source.close.assert_not_called()

    def test_single_mode_ai_layout_and_dual_compatibility(self):
        c = Connections()
        manager = DeviceManager(c)
        c.signal_chain_mode = 'single_drive'
        c.dc_enabled = False
        self.assertEqual(manager._daq_ai_indexes(), [0, 1])
        c.dc_enabled = True
        self.assertEqual(manager._daq_ai_indexes(), [0, 1, 2])
        c.signal_chain_mode = 'dual'
        self.assertEqual(manager._daq_ai_indexes(), [0, 1, 2, 3, 4])
