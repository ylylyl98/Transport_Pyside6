"""Safety regression tests for direct, strictly monotonic hysteresis moves."""
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from app.devices.aps100_attodry1000_adapter import (
    APS100AttoDry1000Adapter, APS100FieldHistoryError, APS100OperationCancelled, APS100SafetyError,
    APS100QualityOvershoot, _ReversalWarnings,
)
from app.engine.bfield_transport_sweep import TransportSweepPlan
from app.engine.field_approach import normalize_approach
from tests.test_aps100_move_diagnostics import DiagnosticAdapter
import tests.test_stepped_transport as fixtures


class LinearAdapter(DiagnosticAdapter):
    def advance(self, seconds):
        self.clock += seconds
        if self.active:
            delta = self.target - self.value
            self.value += max(-self.increment, min(self.increment, delta))

    def _set_directional_target(self, target, current):
        self.log.append(('target', target, current))
        self.target = target
        return 'UP' if target > current else 'DOWN'


def move(adapter, target, direction, stop=None, **extra):
    options = dict(approach='linear_no_backward', acceptance_t=.0002,
                   reversal_t=.0002, lower_t=-.1, upper_t=.1, no_progress_s=15.)
    options.update(extra)
    history = {}
    with patch('app.devices.aps100_attodry1000_adapter.time.monotonic', side_effect=lambda: adapter.clock):
        adapter._hysteresis_field_move(target, direction, .0002, 10000., stop, None, options, history)
    return history


class DirectHysteresisTests(unittest.TestCase):
    def test_warning_episode_count_is_bounded_and_deduplicated(self):
        history = {}
        warnings = _ReversalWarnings(history)
        for episode in range(120):
            warnings.observe(.009, .01, 1, .0002, episode * 3)
            warnings.observe(.008, .01, 1, .0002, episode * 3 + 1)
            warnings.observe(.01, .01, 1, .0002, episode * 3 + 2)
        self.assertEqual(history['reversal_event_count'], 120)
        self.assertEqual(len(history['reversal_events']), 100)
        self.assertAlmostEqual(history['reversal_events'][0]['excursion_t'], .002)

    def test_both_directions_and_zero_start_use_one_unmodified_rate_sweep(self):
        for start, target in ((.01, .02), (.02, .01), (0., .01), (0., -.01)):
            with self.subTest(start=start, target=target):
                adapter = LinearAdapter()
                adapter.value = start
                original = dict(adapter.table)
                direction = 1 if target > start else -1
                history = move(adapter, target, direction)
                self.assertEqual(adapter.table, original)
                self.assertFalse(any(isinstance(x, tuple) and x[0] == 'restore' for x in adapter.log))
                self.assertEqual([x[1] for x in adapter.log if isinstance(x, tuple) and x[0] == 'target'], [target])
                self.assertEqual([x for x in adapter.log if isinstance(x, str) and x.startswith('SWEEP')],
                                 ['SWEEP UP SLOW' if direction > 0 else 'SWEEP DOWN SLOW'])
                self.assertEqual(len(history['stages']), 1)
                self.assertEqual(history['stages'][0]['rate_factor'], 1.)
                self.assertTrue(history['complete'])
                self.assertTrue(history['trace'])
                self.assertIn('start_rates_t_per_min', history)

    def test_passed_target_outside_acceptance_reports_quality_and_pauses_without_reverse(self):
        for start, target, direction, approach in ((.021, .02, 1, 'linear_no_backward'), (.009, .01, -1, 'linear_no_backward'),
                                                    (.021, .02, 1, 'no_overshoot'), (.009, .01, -1, 'no_overshoot')):
            with self.subTest(direction=direction, approach=approach):
                adapter = LinearAdapter()
                adapter.value = start
                with self.assertRaises(APS100QualityOvershoot):
                    move(adapter, target, direction, approach=approach)
                self.assertIn('pause', adapter.log)
                self.assertFalse(any(isinstance(x, str) and x.startswith('SWEEP') for x in adapter.log))

    def test_passed_target_inside_acceptance_is_accepted_without_correction(self):
        adapter = LinearAdapter()
        adapter.value = .0201
        history = move(adapter, .02, 1)
        self.assertTrue(history['complete'])
        self.assertFalse(any(isinstance(x, str) and x.startswith('SWEEP') for x in adapter.log))

    def test_cancel_before_sweep_pauses_without_a_command(self):
        adapter = LinearAdapter()
        stop = threading.Event()
        stop.set()
        with self.assertRaises(APS100OperationCancelled):
            move(adapter, .02, 1, stop)
        self.assertIn('pause', adapter.log)
        self.assertFalse(any(isinstance(x, str) and x.startswith('SWEEP') for x in adapter.log))

    def test_already_at_target_still_checks_cancellation_and_fault(self):
        for condition in ('cancel', 'fault'):
            with self.subTest(condition=condition):
                adapter = LinearAdapter()
                adapter.value = .02
                stop = threading.Event()
                if condition == 'cancel':
                    stop.set()
                else:
                    adapter._ensure_no_fault = lambda: (_ for _ in ()).throw(APS100SafetyError('fault'))
                with self.assertRaises(APS100OperationCancelled if condition == 'cancel' else APS100SafetyError):
                    move(adapter, .02, 1, stop)
                self.assertIn('pause', adapter.log)
                self.assertFalse(any(isinstance(x, str) and x.startswith('SWEEP') for x in adapter.log))

    def test_preparation_remains_progressive_with_linear_acquisition(self):
        from tests.test_aps100_preparation import PreparationAdapter, PreparationTests
        for direction in (-1, 1):
            with self.subTest(direction=direction):
                adapter = PreparationAdapter(direction)
                adapter.injected = True
                original = adapter.get_rates()
                options = PreparationTests().options()
                options['approach'] = 'linear_no_backward'
                history = {}
                with patch('app.devices.aps100_attodry1000_adapter.time.monotonic', side_effect=lambda: adapter.clock):
                    adapter._hysteresis_field_move(direction * .2, direction, .0002, 30., None, None, options, history)
                self.assertGreater(len(history['stages']), 1)
                self.assertTrue(any(stage['rate_factor'] < 1. for stage in history['stages']))
                self.assertTrue(history['preparation_stable'])
                self.assertEqual(adapter.get_rates(), original)

    def test_overshoot_during_sweep_reports_quality_and_never_commands_reverse(self):
        for start, target, direction, approach in ((.01, .02, 1, 'linear_no_backward'), (.02, .01, -1, 'linear_no_backward'),
                                                    (.01, .02, 1, 'no_overshoot'), (.02, .01, -1, 'no_overshoot')):
            with self.subTest(direction=direction, approach=approach):
                adapter = LinearAdapter()
                adapter.value = start
                def overshoot(seconds):
                    adapter.clock += seconds
                    if adapter.active:
                        adapter.value = target + direction * .001
                adapter._sleep = overshoot
                with self.assertRaises(APS100QualityOvershoot):
                    move(adapter, target, direction, approach=approach)
                commands = [x for x in adapter.log if isinstance(x, str) and x.startswith('SWEEP')]
                self.assertEqual(commands, ['SWEEP UP SLOW' if direction > 0 else 'SWEEP DOWN SLOW'])
                self.assertIn('pause', adapter.log)

    def test_reverse_readback_warns_and_continues_forward(self):
        for approach in ('linear_no_backward', 'no_overshoot'):
            for direction in (1, -1):
                with self.subTest(approach=approach, direction=direction):
                    adapter = LinearAdapter()
                    adapter.value = direction * .01
                    injected = [False]
                    original = adapter._sleep
                    def reverse(seconds):
                        original(seconds)
                        if adapter.active and not injected[0] and direction * adapter.value > .011:
                            adapter.value -= direction * .002
                            injected[0] = True
                    adapter._sleep = reverse
                    history = move(adapter, direction * .02, direction, approach=approach)
                    self.assertTrue(history['complete'])
                    self.assertTrue(history['reversal_exceeded'])
                    self.assertGreater(history['max_reverse_t'], .0002)
                    self.assertEqual(history['reversal_event_count'], 1)
                    event = history['reversal_events'][0]
                    self.assertGreater(event['excursion_t'], event['limit_t'])
                    self.assertIn('monotonic_s', event)
                    commands = [x for x in adapter.log if isinstance(x, str) and x.startswith('SWEEP')]
                    self.assertTrue(commands)
                    self.assertTrue(all(x == ('SWEEP UP SLOW' if direction > 0 else 'SWEEP DOWN SLOW') for x in commands))

    def test_generic_approach_does_not_accept_hysteresis_only_mode(self):
        with self.assertRaises(ValueError):
            normalize_approach('linear_no_backward')
        self.assertEqual(normalize_approach('linear_no_backward', allow_hysteresis=True), 'linear_no_backward')
        adapter = LinearAdapter()
        with self.assertRaises(ValueError):
            APS100AttoDry1000Adapter.approach_field(adapter, .02, 'linear_no_backward')


class ControllerLinearTests(unittest.TestCase):
    setUpClass = fixtures.SteppedTransportTests.setUpClass
    reading = fixtures.SteppedTransportTests.reading
    move_result = fixtures.SteppedTransportTests.move_result

    def setUp(self):
        fixtures.SteppedTransportTests.setUp(self)
        self.params.field_approach = 'linear_no_backward'
        self.controller.plan = TransportSweepPlan.from_params(self.params)
        self.controller.plan.validation['aps100_rates'] = {'normal': {i: (10. * (i+1), .1) for i in range(5)}, 'coil_constant_t_per_a': .2}
        self.controller._reset_step_condition()

    def test_only_stepped_aps100_plan_accepts_mode(self):
        for backend, mode in (('1000', 'continuous'), ('2100', 'stepped'), ('2100', 'continuous')):
            with self.subTest(backend=backend, mode=mode):
                self.params.magnet_backend, self.params.measurement_mode = backend, mode
                with self.assertRaises(ValueError):
                    TransportSweepPlan.from_params(self.params)

    def test_strict_routing_and_history_reversal_warning(self):
        c = self.controller
        self.assertTrue(c._strict_field_history())
        c._step_index = 1
        c._last_point_field = 0.
        c._move_step()
        kwargs = self.magnet.moves[-1][1]
        self.assertEqual(kwargs['hysteresis_options']['approach'], 'linear_no_backward')
        self.assertEqual(kwargs['monotonic_direction'], 1)
        result = self.move_result()
        result['field_history']['max_reverse_t'] = self.params.field_reversal_t * 2
        self.assertTrue(c._accept_move_history(result))
        self.assertFalse(c._cleanup_in_progress)
        self.assertTrue(c._point_history['reversal_exceeded'])
        self.assertEqual(self.acquisitions, [])

    def test_initial_preparation_routes_through_strict_worker(self):
        self.controller._position_at_start()
        kwargs = self.magnet.moves[-1][1]
        self.assertEqual(kwargs['hysteresis_options']['approach'], 'linear_no_backward')
        self.assertTrue(kwargs['hysteresis_options']['preparation'])
        self.assertIn('monotonic_direction', kwargs)

    def test_settling_overshoot_waits_for_qualification_without_repositioning(self):
        c = self.controller
        c._step_index = 1
        c._last_point_field = 0.
        c._move_step()
        c._on_safe_move(self.move_result())
        c._on_magnet_operation('pause')
        moves = len(self.magnet.moves)
        snapshot = SimpleNamespace(monotonic_s=101., field_t=c._target + .005,
                                   heater_on=True, sweep_state='paused',
                                   status=SimpleNamespace(sweep_active=False))
        self.assertTrue(c._step_correct_field(snapshot, 101.))
        self.assertFalse(c._cleanup_in_progress)
        self.assertTrue(c._step_overshoot_pending)
        self.assertEqual(len(self.magnet.moves), moves)
        self.assertEqual(self.acquisitions, [])

    def test_round_trip_commands_reverse_only_at_planned_turnaround(self):
        self.params.round_trip = True
        c = self.controller
        c._reset_step_condition()
        c._step_index = 3
        c._last_point_field = .1
        c._move_step()
        self.assertEqual(self.magnet.moves[-1][1]['monotonic_direction'], -1)
        self.assertEqual(self.magnet.moves[-1][1]['hysteresis_options']['approach'], 'linear_no_backward')


if __name__ == '__main__':
    unittest.main()
