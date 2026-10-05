import threading
import unittest
from unittest.mock import patch

import pyvisa
from PySide6 import QtCore, QtTest, QtWidgets

from app.device_manager import DeviceManager
from app.models import Connections
from app.ui.lockin_panel import LockinPanel, LockinWorker
from app.ui.dock import ConnDock
from app.ui.main_window import MainWindow
from instruments.SR830 import SRSLockin


class VisaSimulator:
    """Hardware boundary: supports delayed execution and rejected settings."""
    def __init__(self):
        self.interface_type = pyvisa.constants.InterfaceType.gpib
        self.timeout = 5000
        self.values = dict(PHAS=0, FMOD=0, FREQ=137, SLVL=.01, SENS=22,
                           RMOD=1, RSRV=2, OFLT=10, OFSL=1, ISRC=0, IGAN=0,
                           IGND=0, ICPL=0, ILIN=0, HARM=1, SMOD=0, ADSP=0)
        self.calls = []
        self.reject = False
        self.busy = 0
        self.pending = None
        self.esr = 0
        self.display_type = 2
        self.scale = 1.
        self.quantize = False

    def write(self, command):
        self.calls.append(command)
        key, _, value = command.partition(' ')
        if key == 'SENS' and not self.reject:
            self.pending = int(value)
            self.busy = 2
        elif key == 'AGAN':
            self.pending = 23
            self.busy = 3
        elif key == 'ASCL':
            self.scale = 2.
        elif key == 'DSCL':
            self.scale = float(value.split(',')[1])
        elif key in self.values and key != 'SENS':
            self.values[key] = float(value)
            if self.quantize:
                if key == 'PHAS':
                    self.values[key] = (float(value) + 180) % 360 - 180
                elif key == 'SLVL':
                    self.values[key] = round(float(value) / .002) * .002
                elif key == 'FREQ':
                    self.values[key] = float(f'{float(value):.5g}')

    def read_stb(self):
        self.calls.append('<serial poll>')
        if self.busy:
            self.busy -= 1
            return 0
        if self.pending is not None:
            self.values['SENS'] = self.pending
            self.pending = None
        return 2

    def query(self, command):
        self.calls.append(command)
        if command in ('*STB?', '*STB? 1'):
            # Actual SR850 logs: a text status query can itself clear IFC.
            # Its reply means earlier commands finished, not that IFC is 1.
            if self.pending is not None:
                self.values['SENS'] = self.pending
                self.pending = None
            return '0'
        if command == '*ESR?':
            value, self.esr = self.esr, 0
            return str(value)
        if command == 'SNAP?1,2,3,4':
            return '0.01,0.02,0.022,63.4'
        if command == 'LIAS?':
            return '0'
        if command.startswith('DTYP?'):
            return str(self.display_type)
        if command.startswith('DSCL?'):
            return str(self.scale)
        if command.startswith('DOFF?'):
            return '0'
        return str(self.values[command.rstrip('?')])


def make_session(model='SR850'):
    session = SRSLockin.__new__(SRSLockin)
    session._name = model
    session._address = 'GPIB0::8::INSTR'
    session._model = model
    session._identity = f'SRS,{model},123,1.0'
    session._input_values = {}
    session._last_snap_raw = ''
    session.lock = threading.RLock()
    session._my_instr = VisaSimulator()
    return session


class LockinControlTests(unittest.TestCase):
    def run_operation(self, session, action='apply', payload=None, **kwargs):
        return session.run_control(action, payload or {}, poll_interval=0, **kwargs)

    def test_refresh_uses_serial_poll_when_text_ifc_reply_is_zero(self):
        for model in ('SR830', 'SR850'):
            for address in ('GPIB0::8::INSTR', 'GPIB0::9::INSTR'):
                with self.subTest(model=model, address=address):
                    session = make_session(model)
                    session._address = address
                    data = self.run_operation(session, 'refresh', timeout=.01)
                    self.assertTrue(data['control']['ok'], data['control'].get('error'))
                    self.assertEqual(data['settings']['sensitivity'], 22)
                    self.assertEqual(data['outputs']['x'], .01)
                    self.assertNotIn('*STB? 1', session._my_instr.calls)
                    self.assertTrue(all('?' in c or c == '<serial poll>' for c in session._my_instr.calls))
                    polls = [e for e in data['control']['trace'] if e['kind'] == 'serial_poll']
                    self.assertTrue(polls)
                    self.assertEqual(polls[0]['response'], '2')

    def test_serial_query_reply_is_completion_even_with_ifc_zero(self):
        session = make_session()
        session._my_instr.interface_type = pyvisa.constants.InterfaceType.asrl
        session._address = 'ASRL3::INSTR'
        data = self.run_operation(session, 'auto_gain', timeout=.01)
        self.assertTrue(data['control']['ok'], data['control'].get('error'))
        self.assertEqual(data['settings']['sensitivity'], 23)
        self.assertNotIn('<serial poll>', session._my_instr.calls)
        self.assertEqual(session._my_instr.calls.count('*STB?'), 2)

    def test_serial_barrier_uses_operation_timeout_and_restores_it_on_error(self):
        for fails in (False, True):
            with self.subTest(fails=fails):
                session = make_session()
                resource = session._my_instr
                resource.interface_type = pyvisa.constants.InterfaceType.asrl
                def query(command):
                    self.assertEqual(resource.timeout, 20000)
                    if fails:
                        raise pyvisa.errors.VisaIOError(pyvisa.constants.StatusCode.error_timeout)
                    return '0'
                resource.query = query
                if fails:
                    with self.assertRaises(pyvisa.errors.VisaIOError):
                        session.wait_command_complete(timeout=20)
                else:
                    session.wait_command_complete(timeout=20)
                self.assertEqual(resource.timeout, 5000)

    def test_unsupported_serial_poll_falls_back_but_transport_failure_does_not(self):
        for code in (pyvisa.constants.StatusCode.error_nonsupported_operation,
                     pyvisa.constants.StatusCode.error_connection_lost):
            with self.subTest(code=code):
                session = make_session()
                def read_stb():
                    raise pyvisa.errors.VisaIOError(code)
                session._my_instr.read_stb = read_stb
                data = self.run_operation(session, 'refresh', timeout=.01)
                if code == pyvisa.constants.StatusCode.error_nonsupported_operation:
                    self.assertTrue(data['control']['ok'], data['control'].get('error'))
                    self.assertEqual(data['settings']['sensitivity'], 22)
                else:
                    self.assertFalse(data['control']['ok'])
                    self.assertNotIn('settings', data)
                    self.assertEqual(session._my_instr.calls, [])
                    self.assertTrue(session._control_state_uncertain)
                self.assertEqual(session.timeout, 5000)

    def test_serial_poll_masks_ifc_bit_and_limits_io_timeout(self):
        session = make_session()
        replies = iter((64, 66))  # SRQ alone is not ready; SRQ + IFC is ready.
        def read_stb():
            self.assertGreater(session.timeout, 0)
            self.assertLessEqual(session.timeout, 50)
            return next(replies)
        session._my_instr.read_stb = read_stb
        session.wait_command_complete(timeout=.05, poll_interval=0)
        self.assertEqual(list(replies), [])
        self.assertEqual(session.timeout, 5000)

    def test_cancelled_wait_does_not_poll_or_query(self):
        session = make_session()
        with self.assertRaisesRegex(RuntimeError, 'cancelled'):
            session.wait_command_complete(cancelled=lambda: True)
        self.assertEqual(session._my_instr.calls, [])
        self.assertEqual(session.timeout, 5000)

    def test_sensitivity_waits_for_completion_and_only_writes_changed_field(self):
        session = make_session()
        data = self.run_operation(session, payload={'sensitivity': 23})
        self.assertTrue(data['control']['ok'])
        self.assertEqual(data['settings']['sensitivity'], 23)
        writes = [c for c in session._my_instr.calls if '?' not in c and c != '<serial poll>']
        self.assertEqual(writes, ['SENS 23'])
        self.assertIn('SENS 23', str(data['control']['trace']))

    def test_rejected_setting_returns_actual_value_and_explicit_failure(self):
        session = make_session()
        session._my_instr.reject = True
        data = self.run_operation(session, payload={'sensitivity': 23})
        self.assertFalse(data['control']['ok'])
        self.assertEqual(data['settings']['sensitivity'], 22)
        self.assertIn('sensitivity', data['control']['error'])
        self.assertIn('23', data['control']['error'])
        self.assertIn('22', data['control']['error'])

    def test_auto_gain_waits_before_readback(self):
        session = make_session()
        data = self.run_operation(session, 'auto_gain')
        self.assertTrue(data['control']['ok'])
        self.assertEqual(data['settings']['sensitivity'], 23)
        calls = session._my_instr.calls
        self.assertGreater(calls[calls.index('AGAN'):].count('<serial poll>'), 3)

    def test_busy_timeout_does_not_write_or_report_success(self):
        session = make_session()
        session._my_instr.busy = 100000
        data = self.run_operation(session, payload={'sensitivity': 23}, timeout=0.001)
        self.assertFalse(data['control']['ok'])
        self.assertIn('timed out', data['control']['error'])
        self.assertNotIn('SENS 23', session._my_instr.calls)

    def test_auto_gain_restriction_is_only_sr830(self):
        for model, allowed in [('SR830', False), ('SR850', True)]:
            session = make_session(model)
            session._my_instr.values['OFLT'] = 11
            data = self.run_operation(session, 'auto_gain')
            self.assertEqual(data['control']['ok'], allowed)
            self.assertEqual('AGAN' in session._my_instr.calls, allowed)

    def test_auto_scale_requires_sr850_bar_or_chart(self):
        for model, display, allowed in [('SR830', 2, False), ('SR850', 0, False),
                                         ('SR850', 2, True), ('SR850', 3, True)]:
            session = make_session(model)
            session._my_instr.display_type = display
            data = self.run_operation(session, 'auto_scale')
            self.assertEqual(data['control']['ok'], allowed)
            self.assertEqual('ASCL' in session._my_instr.calls, allowed)
            self.assertNotIn('SENS 23', session._my_instr.calls)

    def test_invalid_batch_is_rejected_before_any_setting_write(self):
        session = make_session()
        data = self.run_operation(session, payload={'sensitivity': 23, 'harmonic': 0})
        self.assertFalse(data['control']['ok'])
        self.assertNotIn('SENS 23', session._my_instr.calls)

    def test_timeout_blocks_daq_calibration_until_successful_refresh(self):
        session = make_session()
        session._my_instr.busy = 100000
        self.run_operation(session, 'auto_gain', timeout=.001)
        with self.assertRaisesRegex(RuntimeError, 'Refresh'):
            session.read_sensitivity()
        session._my_instr.busy = 0
        self.assertTrue(self.run_operation(session, 'refresh')['control']['ok'])
        self.assertEqual(session.read_sensitivity()['sensitivity'], 22)

    def test_display_edit_cannot_change_measurement_sensitivity(self):
        session = make_session()
        data = self.run_operation(session, 'apply_display', {'pane': 0, 'scale': .2})
        self.assertTrue(data['control']['ok'])
        self.assertEqual(data['display']['scale'], .2)
        self.assertEqual(session._my_instr.values['SENS'], 22)
        session._my_instr.values.update(SMOD=1, ADSP=1)
        data = self.run_operation(session, 'apply_display', {'pane': 0, 'scale': .4})
        self.assertFalse(data['control']['ok'])
        self.assertEqual(session._my_instr.scale, .2)

    def test_instrument_error_is_logged_with_model_address_and_commands(self):
        session = make_session()
        original_write = session._my_instr.write
        def reject(command):
            original_write(command)
            session._my_instr.esr = 16
        session._my_instr.write = reject
        data = self.run_operation(session, payload={'sensitivity': 23})
        self.assertFalse(data['control']['ok'])
        self.assertEqual(data['control']['esr'], 16)
        self.assertEqual(data['control']['address'], 'GPIB0::8::INSTR')
        self.assertEqual(data['control']['model'], 'SR850')
        self.assertEqual(data['settings']['sensitivity'], 23)

    def test_worker_does_not_claim_rejected_setting_was_applied(self):
        session = make_session()
        session._my_instr.reject = True
        worker = LockinWorker(session, 'apply', {'sensitivity': 23})
        received = []
        worker.data_ready.connect(lambda data, message: received.append((data, message)))
        worker.run()
        self.assertEqual(len(received), 1)
        self.assertIn('failed', received[0][1])
        self.assertIn('log_path', received[0][0]['control'])

    def test_busy_control_read_does_not_block_gui_calibration(self):
        session = make_session()
        held = threading.Event()
        release = threading.Event()
        def hold_instrument():
            with session.lock:
                held.set()
                release.wait(.3)
        thread = threading.Thread(target=hold_instrument)
        thread.start()
        held.wait(1)
        try:
            with self.assertRaisesRegex(RuntimeError, 'busy'):
                session.read_sensitivity()
        finally:
            release.set()
            thread.join()

    def test_phase_resolution_wrapping_and_oscillator_quantization(self):
        for model, phase in [('SR850', .001), ('SR830', .01)]:
            session = make_session(model)
            session._my_instr.quantize = True
            result = self.run_operation(session, payload={'phase_deg': phase})
            self.assertTrue(result['control']['ok'])
            self.assertIn('PHAS 0.001' if model == 'SR850' else 'PHAS 0.01', session._my_instr.calls)
        for values, expected in [({'phase_deg': 270}, {'phase_deg': -90}),
                                 ({'sine_out_v': .005}, {'sine_out_v': .004}),
                                 ({'frequency_hz': 12345.4}, {'frequency_hz': 12345}),
                                 ({'frequency_hz': 12345.4999}, {'frequency_hz': 12345})]:
            session = make_session()
            session._my_instr.quantize = True
            result = self.run_operation(session, payload=values)
            self.assertTrue(result['control']['ok'], result['control'].get('error'))
            for key, value in expected.items():
                self.assertEqual(result['settings'][key], value)

    def test_femto_scale_mismatch_is_not_hidden_by_absolute_tolerance(self):
        session = make_session()
        session._my_instr.scale = 5e-13
        session._my_instr.write = lambda command: session._my_instr.calls.append(command)
        result = self.run_operation(session, 'apply_display', {'pane': 0, 'scale': 1e-15})
        self.assertFalse(result['control']['ok'])
        self.assertIn('scale', result['control']['error'])


class LockinDraftTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def setUp(self):
        self.panel = LockinPanel(DeviceManager(Connections()))
        self.addCleanup(self.panel.close)
        self.panel._apply_data({'model': 'SR850', 'capabilities': {'model': 'SR850'},
                                'settings': {'sensitivity': 22}})

    def test_apply_submits_only_user_edits(self):
        self.panel.cbo_sensitivity.setCurrentIndex(23)
        with patch.object(self.panel, '_start_worker') as start:
            self.panel.apply_settings()
        self.assertEqual(start.call_args.args[1], {'sensitivity': 23})

    def test_failed_apply_retains_draft_but_emits_actual_for_daq(self):
        self.panel.cbo_sensitivity.setCurrentIndex(23)
        received = []
        self.panel.sensitivity_read.connect(lambda value, label: received.append(value))
        self.panel._apply_data({'settings': {'sensitivity': 22},
                                'control': {'ok': False, 'verified_keys': []}})
        self.assertEqual(self.panel.cbo_sensitivity.currentData(), 23)
        self.assertEqual(received, [.05])
        self.assertIn('50 mV', self.panel.lbl_verified_sensitivity.text())
        self.panel.set_verified_sensitivity(.05, '50 mV/nA')
        self.assertEqual(self.panel.cbo_sensitivity.currentData(), 23)

    def test_success_clears_draft_and_auto_readback_can_update_it(self):
        self.panel.cbo_sensitivity.setCurrentIndex(23)
        self.panel._apply_data({'settings': {'sensitivity': 23},
                                'control': {'ok': True, 'verified_keys': ['sensitivity']}})
        self.panel._apply_data({'settings': {'sensitivity': 24}})
        self.assertEqual(self.panel.cbo_sensitivity.currentData(), 24)

    def test_auto_scale_visibility_follows_model(self):
        self.assertFalse(self.panel.action_buttons['auto_scale'].isHidden())
        self.panel._apply_capabilities({'model': 'SR830'})
        self.assertTrue(self.panel.action_buttons['auto_scale'].isHidden())

    def test_small_phase_edit_is_pending(self):
        self.panel._apply_data({'settings': {'phase_deg': 0.}})
        self.panel.sp_phase.setValue(.001)
        with patch.object(self.panel, '_start_worker') as start:
            self.panel.apply_settings()
        self.assertEqual(start.call_args.args[1], {'phase_deg': .001})

    def test_full_sensitivity_range_is_preserved_in_daq_conversion(self):
        with patch.object(ConnDock, '_start_scan'):
            dock = ConnDock()
        self.addCleanup(dock.close)
        dock.set_lockin_sensitivity_from_sr830(2e-9, '2 nV/fA')
        self.assertEqual(dock.sp_lkn.value(), 2e-9)
        self.assertEqual(dock.get_rates()[1], 5e9)

    def test_main_window_does_not_destroy_running_lockin_worker(self):
        from types import SimpleNamespace
        from unittest.mock import Mock
        panel = SimpleNamespace(prepare_shutdown=lambda: False)
        event = Mock()
        MainWindow.closeEvent(SimpleNamespace(lockin_panel=panel), event)
        event.ignore.assert_called_once()
        event.accept.assert_not_called()

    def test_apply_button_worker_preserves_rejected_draft_and_releases_device(self):
        session = make_session()
        session._my_instr.reject = True
        manager = self.panel.device_manager
        manager.sessions['lockin'] = session
        manager.states['lockin'] = 'ok'
        self.panel._update_enabled()
        self.panel.cbo_sensitivity.setCurrentIndex(23)
        self.panel.btn_apply.click()
        self.assertTrue(manager.is_in_use('lockin'))
        self.assertFalse(self.panel.prepare_shutdown())
        timer = QtCore.QElapsedTimer()
        timer.start()
        while self.panel._worker is not None and timer.elapsed() < 4000:
            QtTest.QTest.qWait(20)
        self.assertIsNone(self.panel._worker)
        self.assertFalse(manager.is_in_use('lockin'))
        self.assertIn('Readback mismatch', self.panel.lbl_message.text())
        self.assertEqual(self.panel.cbo_sensitivity.currentData(), 23)
        self.assertEqual(self.panel._verified_settings['sensitivity'], 22)
        self.assertTrue(self.panel.prepare_shutdown())


if __name__ == '__main__':
    unittest.main()
