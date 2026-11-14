"""Simulation-only safety tests for the experimental short-tail geometry."""
import unittest
from unittest.mock import patch
from types import SimpleNamespace

from app.engine.field_approach import no_overshoot_intermediate
from app.devices.aps100_attodry1000_adapter import (
    APS100SafetyError, APS100OperationCancelled,
    APS100TimeoutError, APS100QualityOvershoot,
)
from tests.test_aps100_polling_comparison import PollingAdapter


class BidirectionalAdapter(PollingAdapter):
    def advance(self, seconds):
        self.clock += seconds
        if self.active:
            direction = 1 if self.target >= self.value else -1
            self.value += direction * min(abs(self.target - self.value), self.increment)
        if self.reverse_at is not None and self.clock >= self.reverse_at:
            self.value -= .002
            self.reverse_at = None
        if self.stop_at is not None and self.clock >= self.stop_at:
            self.stop.set()

    def _set_directional_target(self, target, current):
        self.log.append(('target', target, current))
        self.target = target
        return 'UP' if target >= current else 'DOWN'


def move(adapter, target=.02, direction=1, timeout=100., history=None, **extra):
    options = dict(approach='no_overshoot', acceptance_t=.0002, reversal_t=.0002,
                   lower_t=-.1, upper_t=.1, no_progress_s=15.,
                   no_overshoot_profile='short_tail')
    options.update(extra)
    if history is None:
        history = {}
    with patch('app.devices.aps100_attodry1000_adapter.time.monotonic', side_effect=lambda: adapter.clock):
        adapter._hysteresis_field_move(target, direction, .0002, timeout, adapter.stop, None, options, history)
    return history


class ShortTailPlannerTests(unittest.TestCase):
    def test_quantization_keeps_minimum_tail_in_both_directions(self):
        for start, target in ((.01, .02003), (.02, .00997)):
            with self.subTest(start=start):
                endpoint, info = no_overshoot_intermediate(start, target, .0003, 'short_tail')
                self.assertEqual(info['effective_profile'], 'short_tail')
                self.assertGreaterEqual(abs(target - endpoint), .0012 - 1e-12)
                self.assertAlmostEqual(endpoint / .0001, round(endpoint / .0001))

    def test_fallbacks_preserve_standard_endpoint(self):
        cases = [(.01, .02, {'preparation': True}, 'preparation'),
                 (0., .02, {}, 'near_or_crossing_zero'),
                 (-.01, .01, {}, 'near_or_crossing_zero'),
                 (.01, .02, {'same_rate_band': False}, 'rate_band_transition'),
                 (.01, .011, {}, 'short_move')]
        for start, target, kwargs, reason in cases:
            with self.subTest(reason=reason):
                endpoint, info = no_overshoot_intermediate(start, target, .0002, 'short_tail', **kwargs)
                standard, _ = no_overshoot_intermediate(start, target, .0002)
                self.assertAlmostEqual(endpoint, standard)
                self.assertEqual(info['reason'], reason)
                self.assertEqual(info['effective_profile'], 'standard')


class ShortTailSimulationTests(unittest.TestCase):
    def test_geometry_rates_and_polling_in_both_directions(self):
        for start, target, direction in ((.01, .02, 1), (.02, .01, -1),
                                         (-.5, -.49, 1), (-.49, -.5, -1)):
            for profile in ('standard', 'reduced_mode_queries'):
                with self.subTest(start=start, profile=profile):
                    adapter = BidirectionalAdapter()
                    adapter.value = start
                    original = dict(adapter.table)
                    history = move(adapter, target, direction, polling_profile=profile, lower_t=-.6)
                    stages = history['stages']
                    self.assertEqual([s['rate_factor'] for s in stages], [1., .2])
                    self.assertAlmostEqual(abs(stages[0]['endpoint_t'] - start), .009)
                    self.assertAlmostEqual(abs(target - stages[0]['endpoint_t']), .001)
                    self.assertEqual(adapter.table, original)
                    diag = history['polling_diagnostics']
                    self.assertEqual(diag['status_checks'], diag['iterations'])
                    self.assertGreaterEqual(len(adapter.thermal_calls), diag['iterations'])
                    self.assertGreaterEqual(adapter.log.count('field'), diag['iterations'])

    def test_shared_thermal_field_observation_for_both_polling_profiles(self):
        for profile in ('standard', 'reduced_mode_queries'):
            adapter = BidirectionalAdapter()
            active_reads = []
            read = adapter.get_field_t
            def guard(status):
                value = read()
                if adapter.active:
                    active_reads.append(value)
                return SimpleNamespace(field_t=value, monotonic_s=adapter.clock)
            adapter.thermal_guard = guard
            adapter.thermal_field_guard = guard
            history = move(adapter, polling_profile=profile)
            diag = history['polling_diagnostics']
            self.assertEqual(history['no_overshoot_diagnostics']['effective_profile'], 'short_tail')
            self.assertEqual(diag['shared_field_reads'], diag['iterations'])
            self.assertEqual(len(active_reads), diag['iterations'])
            self.assertGreater(diag['iterations'], 1)

    def test_driver_fallbacks(self):
        for start, target, table, reason in (
                (0., .02, None, 'near_or_crossing_zero'),
                (-.01, .01, None, 'near_or_crossing_zero'),
                (.01, .011, None, 'short_move'),
                (.01, .02, {i: (.07 * (i + 1), .12) for i in range(5)}, 'rate_band_transition')):
            with self.subTest(reason=reason):
                adapter = BidirectionalAdapter()
                adapter.value = start
                if table is not None:
                    adapter.table = table
                history = move(adapter, target)
                self.assertEqual(history['no_overshoot_diagnostics']['reason'], reason)
                self.assertEqual(history['no_overshoot_diagnostics']['effective_profile'], 'standard')

    def test_preparation_driver_fallback(self):
        from tests.test_aps100_preparation import PreparationAdapter, PreparationTests
        adapter = PreparationAdapter(sign=1)
        adapter.injected = True
        options = PreparationTests().options()
        options['no_overshoot_profile'] = 'short_tail'
        history = {}
        with patch('app.devices.aps100_attodry1000_adapter.time.monotonic', side_effect=lambda: adapter.clock):
            adapter._hysteresis_field_move(.2, 1, .0002, 30., None, None, options, history)
        self.assertEqual(history['no_overshoot_diagnostics']['reason'], 'preparation')
        self.assertEqual(history['no_overshoot_diagnostics']['effective_profile'], 'standard')

    def test_invalid_profile_rejected_before_io(self):
        for options in ({'no_overshoot_profile': 'invalid'},
                        {'approach': 'linear_no_backward'}):
            adapter = BidirectionalAdapter()
            with self.assertRaises(APS100SafetyError):
                move(adapter, **options)
            self.assertEqual(adapter.log, [])
            self.assertEqual(adapter.mode_calls, [])
            self.assertEqual(adapter.status_calls, [])

    def test_second_stage_failures_pause_and_restore_changed_rates(self):
        for kind, error in (('cancel', APS100OperationCancelled),
                            ('quench', APS100SafetyError),
                            ('thermal', APS100SafetyError),
                            ('timeout', APS100TimeoutError),
                            ('no_progress', APS100SafetyError)):
            with self.subTest(kind=kind):
                adapter = BidirectionalAdapter()
                adapter.value = .01
                original = dict(adapter.table)
                history = {}
                write = adapter._write
                injection = []

                def inject(command):
                    write(command)
                    if adapter.table == original:
                        return
                    injection.append((command, dict(adapter.table)))
                    if kind == 'cancel':
                        adapter.stop.set()
                    elif kind == 'quench':
                        adapter.fault_at = adapter.clock
                    elif kind == 'thermal':
                        def fail(status):
                            raise APS100SafetyError('thermal failure')
                        adapter.thermal_guard = fail
                    elif kind == 'timeout':
                        adapter.clock += 101.
                    elif kind == 'no_progress':
                        adapter.increment = 0.

                adapter._write = inject
                with self.assertRaises(error):
                    move(adapter, polling_profile='reduced_mode_queries', no_progress_s=.2, history=history)
                self.assertEqual(len(injection), 1)
                self.assertEqual(injection[0][0], 'SWEEP UP SLOW')
                self.assertNotEqual(injection[0][1], original)
                self.assertIn('pause', adapter.log)
                self.assertEqual(adapter.table, original)
                self.assertEqual(history['no_overshoot_diagnostics']['effective_profile'], 'short_tail')
                self.assertIn(('restore', tuple(original.items())), adapter.log)

    def test_second_stage_reversal_warns_with_progress_guard_and_rate_restoration(self):
        for profile in ('standard', 'reduced_mode_queries'):
            for budget in (.2, 15.):
                with self.subTest(profile=profile, no_progress_s=budget):
                    adapter = BidirectionalAdapter()
                    original = dict(adapter.table)
                    history = {}
                    write = adapter._write

                    def inject(command):
                        write(command)
                        if adapter.table != original:
                            adapter.reverse_at = adapter.clock

                    adapter._write = inject
                    if budget == .2:
                        # The warning is nonfatal, but an independent short
                        # no-progress deadline still pauses outside acceptance.
                        with self.assertRaisesRegex(APS100SafetyError, 'outside acceptance'):
                            move(adapter, polling_profile=profile, no_progress_s=budget, history=history)
                        self.assertEqual(history['stages'][-1]['exit_reason'], 'no_progress')
                    else:
                        move(adapter, polling_profile=profile, no_progress_s=budget, history=history)
                        self.assertEqual(history['stages'][-1]['exit_reason'], 'target')
                        self.assertLessEqual(abs(history['end_t'] - .02), .0002 + 1e-12)
                    self.assertTrue(history['reversal_exceeded'])
                    self.assertEqual(history['reversal_event_count'], 1)
                    event = history['reversal_events'][0]
                    self.assertGreater(event['excursion_t'], event['limit_t'])
                    self.assertGreater(event['monotonic_s'], history['stages'][1]['sweep_command_monotonic_s'])
                    self.assertEqual(history['no_overshoot_diagnostics']['effective_profile'], 'short_tail')
                    self.assertEqual([command for command in adapter.log
                                      if isinstance(command, str) and command.startswith('SWEEP')],
                                     ['SWEEP UP SLOW', 'SWEEP UP SLOW'])
                    self.assertIn('pause', adapter.log)
                    self.assertFalse(adapter.active)
                    self.assertEqual(adapter.table, original)
                    self.assertIn(('restore', tuple(original.items())), adapter.log)

    def test_overshoot_first_stage_never_commands_reverse(self):
        adapter = BidirectionalAdapter()
        adapter.value = .01
        history = {}
        original_advance = adapter.advance
        def overshoot(seconds):
            original_advance(seconds)
            if adapter.active and adapter.target < .02 and adapter.value >= .0188:
                adapter.value = .0203
        adapter._sleep = overshoot
        original = dict(adapter.table)
        with self.assertRaises(APS100QualityOvershoot):
            move(adapter, history=history)
        self.assertEqual(history['no_overshoot_diagnostics']['effective_profile'], 'short_tail')
        self.assertIn('pause', adapter.log)
        self.assertFalse(adapter.active)
        self.assertEqual(adapter.table, original)
        self.assertNotIn('SWEEP DOWN SLOW', adapter.log)
        self.assertFalse(any(isinstance(item, tuple) and item[0] == 'target' and item[1] < item[2]
                             for item in adapter.log))


if __name__ == '__main__':
    unittest.main()
