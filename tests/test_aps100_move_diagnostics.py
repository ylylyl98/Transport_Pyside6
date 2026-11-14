"""Offline regression checks for no-traffic hysteresis diagnostics."""
import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from app.devices.aps100_attodry1000_adapter import APS100AttoDry1000Adapter, APS100OperationCancelled
from app.engine.hysteresis_transport import HysteresisTransportMixin


class FakeOperations:
    def __init__(self, increment=.001):
        super().__init__()
        self.value = .01
        self.target = self.value
        self.table = {i: (10. * (i + 1), .12) for i in range(5)}
        self.clock = 0.
        self.log = []
        self.active = False
        self.increment = increment
        self._sleep = self.advance

    def advance(self, seconds):
        self.clock += seconds
        if self.active:
            self.value = min(self.target, self.value + self.increment)

    def get_field_t(self):
        self.log.append('field')
        return self.value

    def get_output_field_t(self):
        self.log.append('output')
        return self.value

    def get_rates(self):
        self.log.append('get_rates')
        return dict(self.table)

    def restore_rates(self, rates):
        self.log.append(('restore', tuple(rates.items())))
        self.table = dict(rates)

    def _ensure_no_fault(self):
        self.log.append('status')
        return SimpleNamespace(sweep_active=False)

    def _set_directional_target(self, target, current):
        self.log.append(('target', target, current))
        self.target = target
        return 'UP'

    def _write(self, command):
        self.log.append(command)
        self.active = True

    def pause(self, **kwargs):
        self.log.append('pause')
        self.active = False

    def read_snapshot(self):
        self.log.append('snapshot')
        return SimpleNamespace(field_t=self.value)


class DiagnosticAdapter(FakeOperations, APS100AttoDry1000Adapter):
    pass


def run_move(adapter, **options):
    opts = dict(acceptance_t=.0002, reversal_t=.0002, lower_t=-.1,
                upper_t=.1, no_progress_s=15.)
    opts.update(options)
    history = {}
    with patch('app.devices.aps100_attodry1000_adapter.time.monotonic', side_effect=lambda: adapter.clock):
        adapter._hysteresis_field_move(.02, 1, .0002, 10000., None, None, opts, history)
    return history


class DiagnosticTests(unittest.TestCase):
    def test_normal_step_and_command_sequence(self):
        adapter = DiagnosticAdapter()
        history = run_move(adapter)
        self.assertEqual(history['start_rates_t_per_min'], {str(i): [10. * (i + 1), .12] for i in range(5)})
        self.assertEqual([s['rate_factor'] for s in history['stages']], [1., .2])
        self.assertEqual([s['exit_reason'] for s in history['stages']], ['endpoint', 'target'])
        self.assertAlmostEqual(history['stages'][0]['endpoint_t'], .018)
        self.assertAlmostEqual(history['stages'][1]['endpoint_t'], .02)
        self.assertAlmostEqual(history['stages'][0]['start_field_t'], .01)
        self.assertAlmostEqual(history['stages'][0]['end_field_t'], .018)
        self.assertAlmostEqual(history['stages'][1]['end_field_t'], .02)
        self.assertAlmostEqual(history['stages'][0]['active_rate_t_per_min'], .12)
        self.assertAlmostEqual(history['stages'][1]['active_rate_t_per_min'], .02317392, places=6)
        self.assertEqual(history['trace'][0][1], history['start_t'])
        self.assertEqual(history['trace'][-1][1], round(history['end_t'], 5))
        self.assertGreaterEqual(history['overhead_s'], 0.)
        self.assertTrue(all(a[0] <= b[0] and a[1] <= b[1]
                            for a, b in zip(history['trace'], history['trace'][1:])))
        for stage in history['stages']:
            self.assertGreaterEqual(stage['end_monotonic_s'], stage['sweep_command_monotonic_s'])
        # GOLDEN_COMMANDS captured from the pre-change adapter, with this same fake.
        self.assertEqual(adapter.log, GOLDEN_COMMANDS)

    def test_final_trace_sample_is_kept_within_one_second(self):
        history = run_move(DiagnosticAdapter(increment=.002))
        self.assertEqual(len(history['trace']), 2)
        self.assertLess(history['trace'][-1][0] - history['trace'][0][0], 1.)
        self.assertEqual(history['trace'][-1][1], round(history['end_t'], 5))

    def test_skipped_stage_has_no_sweep_timestamp(self):
        adapter = DiagnosticAdapter()
        set_target = adapter._set_directional_target
        def reach_first_endpoint(target, current):
            result = set_target(target, current)
            if target < .02:
                adapter.value = target
            return result
        adapter._set_directional_target = reach_first_endpoint
        history = run_move(adapter)
        skipped, swept = history['stages']
        self.assertEqual(skipped['exit_reason'], 'already_there')
        self.assertIsNone(skipped['sweep_command_monotonic_s'])
        self.assertEqual(skipped['start_field_t'], skipped['end_field_t'])
        self.assertEqual(swept['exit_reason'], 'target')
        self.assertEqual(adapter.log.count('SWEEP UP SLOW'), 1)

    def test_no_progress_exit_preserves_offset_acceptance(self):
        adapter = DiagnosticAdapter()
        advance = adapter.advance
        def stop_short(seconds):
            advance(seconds)
            adapter.value = min(adapter.value, .0196)
        adapter._sleep = stop_short
        history = run_move(adapter, acceptance_t=.0005, no_progress_s=.2)
        self.assertEqual([s['exit_reason'] for s in history['stages']], ['endpoint', 'no_progress'])
        self.assertAlmostEqual(history['stages'][-1]['end_field_t'], .0196)
        self.assertAlmostEqual(history['end_t'], .0196)
        self.assertTrue(history['complete'])

    def test_band_lookup_and_nonzero_overhead(self):
        adapter = DiagnosticAdapter()
        adapter.table = {i: (.04 * (i + 1), .12 * (i + 1)) for i in range(5)}
        pause, restore = adapter.pause, adapter.restore_rates
        def delayed_pause(**kwargs):
            pause(**kwargs)
            adapter.clock += 2.
        def delayed_restore(rates):
            restore(rates)
            adapter.clock += 3.
        adapter.pause, adapter.restore_rates = delayed_pause, delayed_restore
        history = run_move(adapter)
        self.assertAlmostEqual(history['stages'][0]['active_rate_t_per_min'], .24)
        self.assertAlmostEqual(history['overhead_s'], 10.)

    def test_long_trace_spacing_and_cap(self):
        adapter = DiagnosticAdapter(increment=.000002)
        history = run_move(adapter, no_progress_s=30.)
        trace = history['trace']
        self.assertEqual(len(trace), 240)
        self.assertEqual(trace[0][1], history['start_t'])
        self.assertEqual(trace[-1][1], round(history['end_t'], 5))
        self.assertTrue(all(b[0] - a[0] >= 1.0 - 1e-9 for a, b in zip(trace[:-2], trace[1:-1])))
        self.assertGreaterEqual(trace[-1][0], trace[-2][0])

    def test_history_passes_real_acceptance_and_json_logger(self):
        history = run_move(DiagnosticAdapter())
        class Controller(HysteresisTransportMixin):
            def _strict_field_history(self): return True
            def _log(self, line): self.lines.append(line)
            def _checkpoint_runtime(self, *args): pass
        controller = Controller()
        controller.lines = []
        controller._condition_index = 0
        controller._step_index = 0
        controller._cleanup_in_progress = False
        controller._step_direction = 1
        controller.plan = SimpleNamespace(params=SimpleNamespace(
            field_reversal_t=.0002, start_field_t=.01, stop_field_t=.02,
            field_endpoint_tolerance_t=.0002), validation={})
        self.assertTrue(controller._accept_move_history(dict(success=True, field_history=history)))
        event = json.loads(controller.lines[0].removeprefix('HYSTERESIS '))
        self.assertEqual(event['kind'], 'move_history')
        for key in ('start_rates_t_per_min', 'stages', 'trace', 'overhead_s'):
            self.assertEqual(event['history'][key], history[key])

    def test_mock_diagnostics(self):
        from app.devices.aps100_attodry1000_adapter import MockAPS100Adapter
        adapter = MockAPS100Adapter()
        adapter._heater = True
        def move(target, **kwargs):
            adapter._field_t = target
            kwargs['progress'](target)
        adapter.move_to_field = move
        history = {}
        options = dict(acceptance_t=.0002, reversal_t=.0002, lower_t=-.1, upper_t=.1)
        adapter.safe_move_to_field(.02, final_mode='driven', tolerance_t=.0002,
                                   hysteresis_options=options, field_history=history)
        self.assertEqual(history['start_rates_t_per_min']['0'][0], 40.)
        self.assertEqual(history['stages'][0]['exit_reason'], 'target')
        self.assertEqual(history['trace'][-1][1], .02)
        json.dumps(history, allow_nan=False)

    def test_preparation_records_diagnostics(self):
        from tests.test_aps100_preparation import PreparationAdapter, PreparationTests
        adapter = PreparationAdapter(sign=1)
        adapter.injected = True
        history = {}
        options = PreparationTests().options()
        with patch('app.devices.aps100_attodry1000_adapter.time.monotonic', side_effect=lambda: adapter.clock):
            adapter._hysteresis_field_move(.2, 1, .0002, 30., None, None, options, history)
        self.assertEqual(history['start_rates_t_per_min']['0'], [10., .12])
        self.assertEqual(history['trace'][0][1], history['start_t'])
        self.assertEqual(history['trace'][-1][1], round(history['end_t'], 5))
        self.assertGreaterEqual(history['overhead_s'], 0.)
        self.assertTrue(history['preparation_stable'])

    def test_cancellation_after_rate_change_restores_rates(self):
        adapter = DiagnosticAdapter()
        original = dict(adapter.table)
        def cancel(command):
            if adapter.table != original:
                raise APS100OperationCancelled()
            adapter.log.append(command)
            adapter.active = True
        adapter._write = cancel
        with self.assertRaises(APS100OperationCancelled):
            run_move(adapter)
        self.assertEqual(adapter.table, original)
        self.assertIn('pause', adapter.log)


GOLDEN_COMMANDS = (
    ['field', 'get_rates', 'status', 'field', 'output',
     ('target', 0.018000000000000002, 0.01), 'field', 'output', 'status',
     'SWEEP UP SLOW'] + ['status', 'field'] * 9 +
    ['pause', 'field', 'status',
     ('restore', tuple((i, (10. * (i + 1), .02317392)) for i in range(5))),
     'field', 'output', ('target', .02, .018000000000000002), 'field',
     'output', 'status', 'SWEEP UP SLOW'] + ['status', 'field'] * 3 +
    ['pause', 'field',
     ('restore', tuple((i, (10. * (i + 1), .12)) for i in range(5))),
     'snapshot', 'field'])
