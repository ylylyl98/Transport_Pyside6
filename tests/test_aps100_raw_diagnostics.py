import unittest

from app.devices.aps100_attodry1000_adapter import (
    APS100AttoDry1000Adapter, APS100CommandBlockedError, APS100Error,
    MockAPS100Adapter, field_response_to_tesla,
)
from controllers.magnet_controller import _MagnetWorker


class FakeVisa:
    def __init__(self, replies):
        self.replies = list(replies)
        self.writes = []

    def write(self, command):
        self.writes.append(command)

    def read(self):
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply

    def close(self):
        pass


class RawDiagnosticsTests(unittest.TestCase):
    def adapter(self, replies, echo=False):
        visa = FakeVisa(replies)
        adapter = APS100AttoDry1000Adapter(visa_resource=visa, expect_echo=echo)
        adapter._identity = object()
        return adapter, visa

    def test_original_response_order_and_no_extra_io(self):
        adapter, visa = self.adapter(['IMAG?\r\n', '-1.955 kG\r\n', '0\r\n', '0\r\n', '-1.936 kG\r\n'])
        context = adapter.capture_io_context()
        response = adapter._query('IMAG?')
        adapter._write('SWEEP DOWN SLOW')
        output = adapter._query('IOUT?')
        self.assertEqual(visa.writes, ['IMAG?', '*ESR?', 'SWEEP DOWN SLOW', '*ESR?', 'IOUT?'])
        self.assertEqual(response, '-1.955 kG')
        self.assertAlmostEqual(field_response_to_tesla(response, adapter.coil_constant_t_per_a), -.1955)
        self.assertEqual(output, '-1.936 kG')
        diagnostics = adapter.move_io_diagnostics(context)
        self.assertEqual([e['command'] for e in diagnostics['events']], ['IMAG?', 'SWEEP DOWN SLOW', 'IOUT?'])
        first = diagnostics['events'][0]
        self.assertEqual(first['raw_response'], '-1.955 kG\r\n')
        self.assertFalse(first['raw_truncated'])
        self.assertIn('response_received_utc_epoch_s', first)
        self.assertLessEqual(first['started_monotonic_s'], first['issued_monotonic_s'])
        self.assertLessEqual(first['issued_monotonic_s'], first['response_received_monotonic_s'])
        self.assertLessEqual(first['response_received_monotonic_s'], first['finished_monotonic_s'])
        self.assertIn('no independent magnetic field sensor', diagnostics['source'])
        self.assertEqual(diagnostics['dropped_events'], 0)

    def test_echo_blocked_and_io_errors_are_recorded_without_changing_failures(self):
        adapter, visa = self.adapter(['command blocked\r\n'])
        context = adapter.capture_io_context()
        with self.assertRaises(APS100CommandBlockedError):
            adapter._query('IMAG?')
        event = adapter.move_io_diagnostics(context)['events'][0]
        self.assertEqual(event['raw_response'], 'command blocked\r\n')
        self.assertIn('APS100CommandBlockedError', event['error'])
        self.assertEqual(visa.writes, ['IMAG?'])
        adapter, visa = self.adapter(['0\r\n', 'command blocked\r\n'], echo=True)
        context = adapter.capture_io_context()
        with self.assertRaises(APS100CommandBlockedError):
            adapter._write('SWEEP UP')
        event = adapter.move_io_diagnostics(context)['events'][0]
        self.assertIn('APS100CommandBlockedError', event['error'])
        self.assertEqual(event['raw_echo'], 'command blocked\r\n')
        self.assertIn('echo_received_utc_epoch_s', event)
        self.assertEqual(visa.writes, ['*ESR?', 'SWEEP UP'])
        adapter, visa = self.adapter([OSError('serial failure')])
        context = adapter.capture_io_context()
        with self.assertRaisesRegex(OSError, 'serial failure'):
            adapter._query('IOUT?')
        self.assertIn('serial failure', adapter.move_io_diagnostics(context)['events'][0]['error'])

    def test_context_bounds_truncation_and_reset(self):
        adapter, visa = self.adapter(['x' * 600 + '\r\n'] + ['1 A\r\n'] * 600)
        initial = adapter.capture_io_context()
        self.assertEqual(adapter._query('IMAG?'), 'x' * 600)
        event = adapter.move_io_diagnostics(initial)['events'][0]
        self.assertEqual(len(event['raw_response']), 512)
        self.assertTrue(event['raw_truncated'])
        for _ in range(70):
            adapter._query('IMAG?')
        context = adapter.capture_io_context()
        self.assertEqual(len(context['preceding']), 64)
        for _ in range(530):
            adapter._query('IOUT?')
        result = adapter.move_io_diagnostics(context)
        self.assertEqual(len(result['events']), 512)
        self.assertEqual(result['dropped_events'], 18)
        self.assertEqual(len(result['preceding']), 64)
        adapter.close()
        self.assertEqual(adapter.capture_io_context(), {'sequence': 0, 'control_count': 0, 'preceding': []})

    def test_worker_includes_context_on_success_and_failure(self):
        for fail in (False, True):
            with self.subTest(fail=fail):
                adapter = MockAPS100Adapter(); adapter.connect()
                adapter._heater = True; adapter._field_t = -.01; adapter._output_t = -.01
                adapter._time_scale = 1e6
                calls = []
                adapter.capture_io_context = lambda: calls.append('capture') or {'sequence': 4, 'preceding': [{'raw_response': '-0.1 kG\r\n'}]}
                adapter.move_io_diagnostics = lambda context: {'preceding': context['preceding'], 'events': [{'command': 'IMAG?'}]}
                original_read = adapter.read_snapshot
                def read_snapshot():
                    calls.append('snapshot')
                    return original_read()
                adapter.read_snapshot = read_snapshot
                if fail:
                    def fail_move(*args, **kwargs):
                        raise APS100Error('test failure')
                    adapter.safe_move_to_field = fail_move
                worker = _MagnetWorker(); worker.adapter = adapter
                worker.heating_interlock.begin_magnet = lambda *a, **k: None
                worker.heating_interlock.end_magnet = lambda *a, **k: None
                worker._check_thermal = lambda *a, **k: None
                results = []; worker.safe_move_result.connect(results.append)
                options = dict(acceptance_t=.0002, reversal_t=.0002, lower_t=-.0002,
                               upper_t=.0102, no_progress_s=1., preparation=True)
                worker.safe_move_to_field('request', 0., 'driven', False, True, 0., 60., .0001, False, 0, options)
                self.assertEqual(calls[:2], ['capture', 'snapshot'])
                self.assertEqual(results[-1]['success'], not fail)
                history = results[-1]['field_history']
                self.assertEqual(history['io_diagnostics']['preceding'][0]['raw_response'], '-0.1 kG\r\n')
                self.assertEqual(history, results[-1]['audit']['field_history'])


    def test_successful_worker_history_reaches_log_before_quality_warning(self):
        import json
        from types import SimpleNamespace
        from app.engine.hysteresis_transport import HysteresisTransportMixin

        class Engine(HysteresisTransportMixin):
            _condition_index = 0
            _cleanup_in_progress = True
            def __init__(self):
                self.lines = []
                self.failure = None
                self.plan = SimpleNamespace(validation={})
                self._step_index = 0
            def _strict_field_history(self):
                return True
            def _log(self, line):
                self.lines.append(line)
            def _fail(self, message):
                self.failure = message

        engine = Engine()
        diagnostics = {'preceding': [{'raw_response': '-0.1 kG\r\n'}],
                       'events': [{'command': 'IMAG?', 'raw_response': '-0.097 kG\r\n'}]}
        result = {'success': True, 'field_history': {'io_diagnostics': diagnostics}}
        self.assertTrue(engine._accept_move_history(result))
        first = json.loads(engine.lines[0].removeprefix('HYSTERESIS '))
        self.assertEqual(first['kind'], 'move_history')
        self.assertEqual(first['history']['io_diagnostics'], diagnostics)
        self.assertIsNone(engine.failure)
        self.assertIn('history_uncertain', engine._point_history['quality_warning_reasons'])
        self.assertEqual(engine.plan.validation['field_quality_warnings']['count'], 1)


    def test_diagnostic_capture_failure_does_not_prevent_move(self):
        adapter = MockAPS100Adapter(); adapter.connect()
        adapter._heater = True; adapter._field_t = -.01; adapter._output_t = -.01
        adapter._time_scale = 1e6
        def fail_capture():
            raise RuntimeError('capture unavailable')
        adapter.capture_io_context = fail_capture
        worker = _MagnetWorker(); worker.adapter = adapter
        worker.heating_interlock.begin_magnet = lambda *a, **k: None
        worker.heating_interlock.end_magnet = lambda *a, **k: None
        worker._check_thermal = lambda *a, **k: None
        results = []; worker.safe_move_result.connect(results.append)
        options = dict(acceptance_t=.0002, reversal_t=.0002, lower_t=-.0002,
                       upper_t=.0102, no_progress_s=1., preparation=True)
        worker.safe_move_to_field('request', 0., 'driven', False, True, 0., 60., .0001, False, 0, options)
        self.assertTrue(results[-1]['success'], results[-1])
        self.assertEqual(results[-1]['field_history']['io_diagnostics'], {'unavailable': 'capture unavailable'})


    def test_long_move_preserves_controls_and_reports_control_overflow(self):
        adapter, visa = self.adapter(['1 A\r\n'] * 530)
        context = adapter.capture_io_context()
        adapter._write('LLIM -2.000000', check_errors=False)
        adapter._write('SWEEP DOWN SLOW', check_errors=False)
        for _ in range(530):
            adapter._query('IMAG?')
        result = adapter.move_io_diagnostics(context)
        self.assertEqual(len(result['events']), 512)
        self.assertEqual(result['dropped_events'], 20)
        self.assertEqual([e['command'] for e in result['control_writes']],
                         ['LLIM -2.000000', 'SWEEP DOWN SLOW'])
        self.assertEqual(result['dropped_control_writes'], 0)
        for index in range(70):
            adapter._write(f'ULIM {index}', check_errors=False)
        result = adapter.move_io_diagnostics(context)
        self.assertEqual(result['control_limit'], 64)
        self.assertEqual(len(result['control_writes']), 64)
        self.assertEqual(result['dropped_control_writes'], 8)
        self.assertEqual(result['control_writes'][0]['command'], 'ULIM 6')
        self.assertEqual(result['control_writes'][-1]['command'], 'ULIM 69')
        self.assertEqual(len(visa.writes), 602)
        adapter.close()
        self.assertEqual(adapter.capture_io_context(),
                         {'sequence': 0, 'control_count': 0, 'preceding': []})
