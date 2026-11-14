import threading
import unittest
from types import SimpleNamespace

from app.engine.field_approach import approach_targets, normalize_approach
from app.devices.aps100_attodry1000_adapter import APS100AttoDry1000Adapter, APS100OperationCancelled, MockAPS100Adapter
from controllers.magnet_controller import _MagnetWorker
from dataclasses import replace
from unittest.mock import patch, MagicMock
from tests.heating_interlock_fixtures import safe_interlock
from utils.config import cfg


class FieldApproachTests(unittest.TestCase):
    def test_plans(self):
        self.assertEqual(normalize_approach('auto'), 'linear')
        self.assertEqual(approach_targets(0, 1, .01, 'linear', 2), (1.,))
        self.assertEqual(approach_targets(1, 1, .01, 'oscillate', 2), ())
        stages = approach_targets(0, 1, .01, 'no_overshoot', 2)
        self.assertEqual(stages[-1], 1.)
        self.assertTrue(all(a < b for a, b in zip(stages, stages[1:])))
        stages = approach_targets(0, 1, .01, 'oscillate', 2)
        self.assertGreater(stages[0], 1.)
        self.assertLess(stages[1], 1.)
        with self.assertRaises(ValueError):
            approach_targets(0, 1, .01, 'oscillate', 1.1)

    def test_cancel_before_motion(self):
        stop = threading.Event()
        stop.set()
        fake = SimpleNamespace(_raise_if_stopped=APS100AttoDry1000Adapter._raise_if_stopped)
        with self.assertRaises(APS100OperationCancelled):
            APS100AttoDry1000Adapter.approach_field(fake, 1., 'linear', stop_event=stop)

    def test_real_engine_stages_publish_and_restore_rates(self):
        class Fake(MockAPS100Adapter):
            def __init__(self):
                super().__init__()
                self._heater = True
                self.commands, self.restored = [], False
            def _ensure_no_fault(self):
                return self.get_status()
            def _raise_if_stopped(self, stop):
                APS100AttoDry1000Adapter._raise_if_stopped(stop)
            def start_sweep_to(self, target):
                self.commands.append(target)
                self._field_t = self._output_t = target
            def _set_directional_target(self, target, current):
                self.next_stage = target
                return 'UP' if target > current else 'DOWN'
            def _write(self, command):
                self.start_sweep_to(self.next_stage)
            def restore_rates(self, rates, *, step_callback=None):
                self.restored = True
                if step_callback:
                    step_callback()
                return {i: rate for i, (_, rate) in rates.items()}
            def set_rate_t_per_min(self, rate, **kwargs):
                self.rate = rate
        fake = Fake()
        seen = []
        result = APS100AttoDry1000Adapter.approach_field(fake, .2, 'no_overshoot',
            tolerance_t=.002, rate_t_per_min=.1, safe_limit_t=1., snapshot_callback=seen.append)
        self.assertTrue(fake.restored)
        self.assertTrue(result.approach_complete)
        self.assertEqual(seen[-1].approach_phase, 'settled')
        self.assertTrue(all(a < b for a, b in zip(fake.commands, fake.commands[1:])))
        self.assertTrue(all(x <= .2 for x in fake.commands))
        fake = Fake()
        seen = []
        APS100AttoDry1000Adapter.approach_field(fake, .2, 'oscillate', tolerance_t=.002,
            safe_limit_t=1., snapshot_callback=seen.append)
        self.assertGreater(fake.commands[0], .2)
        self.assertLess(fake.commands[1], .2)
        self.assertEqual(seen[0].approach_phase, 'oscillate')
        stop = threading.Event()
        class Stopped(Fake):
            def _set_directional_target(self, target, current):
                value = super()._set_directional_target(target, current)
                stop.set()
                return value
        fake = Stopped()
        with self.assertRaises(APS100OperationCancelled):
            APS100AttoDry1000Adapter.approach_field(fake, .2, 'no_overshoot',
                rate_t_per_min=.1, stop_event=stop, safe_limit_t=1.)
        self.assertEqual(fake.commands, [])
        self.assertTrue(fake.restored)
        class RestoreFails(Fake):
            def restore_rates(self, rates, *, step_callback=None):
                raise RuntimeError('restore failed')
        fake, seen = RestoreFails(), []
        with self.assertRaisesRegex(RuntimeError, 'restore failed'):
            APS100AttoDry1000Adapter.approach_field(fake, .2, 'no_overshoot',
                rate_t_per_min=.1, safe_limit_t=1., snapshot_callback=seen.append)
        self.assertFalse(any(item.approach_complete for item in seen))
        class Stalled(Fake):
            def _write(self, command):
                self.commands.append(self.next_stage)
            def read_snapshot(self):
                return replace(super().read_snapshot(), status=SimpleNamespace(sweep_active=True))
        fake = Stalled()
        elapsed = [0.]
        cancel = SimpleNamespace(is_set=lambda: False, wait=lambda dt: elapsed.__setitem__(0, elapsed[0]+dt))
        with patch('time.monotonic', side_effect=lambda: elapsed[0]):
            with self.assertRaisesRegex(Exception, 'stopped progressing'):
                APS100AttoDry1000Adapter.approach_field(fake, .2, 'no_overshoot',
                    rate_t_per_min=.1, stop_event=cancel, no_progress_s=15., safe_limit_t=1.)
        self.assertTrue(fake.restored)

    def test_worker_ack_precedes_snapshots_and_retains_completion(self):
        noop = lambda *args, **kwargs: None
        interlock = SimpleNamespace(note_magnet=noop, invalidate_magnet=noop, begin_magnet=noop, end_magnet=noop)
        worker = _MagnetWorker(interlock)
        worker._check_thermal = noop
        adapter = MockAPS100Adapter()
        adapter._heater = True
        events = []
        def approach(target, mode, *, snapshot_callback, **kwargs):
            events.append('motion')
            snapshot_callback(replace(adapter.read_snapshot(), approach_mode=mode,
                                      approach_phase='settled', approach_complete=True))
        adapter.approach_field = approach
        worker.adapter = adapter
        worker.transport_sweep_result.connect(lambda result: events.append('ack' if result['success'] else 'failure'))
        seen = []
        worker.snapshot_updated.connect(seen.append)
        worker.start_transport_sweep(.1, {'field_approach': 'no_overshoot'})
        self.assertEqual(events, ['ack', 'motion'])
        self.assertTrue(seen[-1].approach_complete)
        worker.refresh_snapshot()
        self.assertTrue(seen[-1].approach_complete)
        self.assertEqual(seen[-1].approach_phase, 'settled')

    def test_worker_rejects_unsafe_oscillation_before_ack(self):
        noop = lambda *args, **kwargs: None
        worker = _MagnetWorker(SimpleNamespace(note_magnet=noop, invalidate_magnet=noop,
                                               begin_magnet=noop, end_magnet=noop))
        worker._check_thermal = noop
        worker.adapter = MockAPS100Adapter(maximum_field_t=.1)
        results = []
        worker.transport_sweep_result.connect(results.append)
        worker.start_transport_sweep(.1, {'field_approach': 'oscillate', 'tolerance_t': .001})
        self.assertEqual(len(results), 1)
        self.assertFalse(results[0]['success'])

    def test_rate_restore_calls_progress_for_each_verified_unchanged_rate(self):
        writes, callbacks = [], []
        coil = .2
        fake = SimpleNamespace(coil_constant_t_per_a=coil, get_range_a=lambda index: 10. * (index+1),
                               get_rate_a_per_s=lambda index: .01,
                               _write=writes.append)
        rates = {i: (10. * (i+1), .01 * coil * 60.) for i in range(5)}
        result = APS100AttoDry1000Adapter.restore_rates(fake, rates,
                            step_callback=lambda: callbacks.append(len(writes)))
        self.assertEqual(writes, [])
        self.assertEqual(callbacks, [0, 0, 0, 0, 0])
        self.assertEqual(set(result), set(rates))

    def test_explicit_gate_rate_estimates_approach_and_records_audit(self):
        worker = _MagnetWorker(safe_interlock())
        worker._check_thermal = lambda *a, **kw: None
        adapter = MockAPS100Adapter()
        snapshot = adapter.read_snapshot()
        adapter.safe_move_to_field = MagicMock(return_value=snapshot)
        worker.adapter = adapter
        results = []
        worker.safe_move_result.connect(results.append)
        worker.safe_move_to_field('request', 2., 'driven', False, True, 0., 100., .002,
                                  True, None, None, 'no_overshoot', .002)
        self.assertTrue(results[-1]['success'], results[-1]['error'])
        request = results[-1]['audit']['request']
        self.assertEqual(request['field_approach'], 'no_overshoot')
        self.assertEqual(request['rate_t_per_min'], .002)
        plan = results[-1]['audit']['gate_scan_rate_plan']
        self.assertGreater(plan['timeout_s'], 3600.)
        self.assertGreater(plan['progress_timeout_s'], 119.)
        self.assertEqual(adapter.safe_move_to_field.call_args.kwargs['poll_interval_s'],
                         max(.1, float(cfg.magnet.poll_interval_s)))

    def test_approach_rejects_invalid_rate_table_before_sweep(self):
        adapter = MockAPS100Adapter()
        adapter._heater = True
        adapter._ensure_no_fault = lambda: adapter.get_status()
        adapter._raise_if_stopped = APS100AttoDry1000Adapter._raise_if_stopped
        adapter.get_rates = lambda: {0: (40., float('nan'))}
        adapter.start_sweep_to = MagicMock()
        with self.assertRaisesRegex(Exception, 'Invalid approach rate table'):
            APS100AttoDry1000Adapter.approach_field(adapter, .1, 'linear')
        adapter.start_sweep_to.assert_not_called()
