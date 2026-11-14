"""Controlled polling-profile safety regressions using a simulated APS100."""
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from app.devices.aps100_attodry1000_adapter import (
    APS100AttoDry1000Adapter,
    APS100OperationCancelled, APS100SafetyError,
)
from tests.test_aps100_move_diagnostics import FakeOperations


class PollingAdapter(FakeOperations, APS100AttoDry1000Adapter):
    # Exercise the real fault helpers rather than the fixture's stub.
    _ensure_no_fault = APS100AttoDry1000Adapter._ensure_no_fault

    def __init__(self):
        super().__init__(increment=.0004)
        self.mode_calls = []
        self.status_calls = []
        self.thermal_calls = []
        self.mode_change_at = None
        self.fault_at = None
        self.reverse_at = None
        self.stop_at = None
        self.stop = threading.Event()
        self.thermal_guard = lambda status: self.thermal_calls.append(self.clock)

    def get_operating_mode(self):
        self.mode_calls.append(self.clock)
        return 'auto' if self.mode_change_at is not None and self.clock >= self.mode_change_at else 'manual'

    def get_status(self):
        self.status_calls.append(self.clock)
        fault = self.fault_at is not None and self.clock >= self.fault_at
        return SimpleNamespace(quench=fault, power_module_failure=False, menu_locked=False, sweep_active=False)

    def advance(self, seconds):
        super().advance(seconds)
        if self.reverse_at is not None and self.clock >= self.reverse_at:
            self.value -= .002
            self.reverse_at = None
        if self.stop_at is not None and self.clock >= self.stop_at:
            self.stop.set()


def move(adapter, profile=None, preparation=False, approach='linear_no_backward'):
    options = dict(approach=approach, acceptance_t=.0002,
                   reversal_t=.0002, lower_t=-.1, upper_t=.1, no_progress_s=15.)
    if profile is not None:
        options['polling_profile'] = profile
    if preparation:
        options.update(preparation=True, stability_s=.5, span_t=.0001,
                       drift_t_per_s=.0001, stability_timeout_s=2.)
        adapter.get_heater_state = lambda: 1
        adapter.read_snapshot = lambda: SimpleNamespace(field_t=adapter.value, heater_on=True,
            status=SimpleNamespace(sweep_active=False, faulted=False))
    history = {}
    with patch('app.devices.aps100_attodry1000_adapter.time.monotonic', side_effect=lambda: adapter.clock):
        adapter._hysteresis_field_move(.02, 1, .0002, 100., adapter.stop, None, options, history)
    return history


class PollingComparisonTests(unittest.TestCase):
    def test_no_overshoot_keeps_same_stages_rates_and_full_boundary_checks(self):
        results = []
        for profile in ('standard', 'reduced_mode_queries'):
            adapter = PollingAdapter()
            original_write = adapter._write
            def write(command):
                if command.startswith('SWEEP'):
                    self.assertEqual(adapter.mode_calls[-1], adapter.clock)
                original_write(command)
            adapter._write = write
            history = move(adapter, profile, approach='no_overshoot')
            self.assertEqual([s['rate_factor'] for s in history['stages']], [1., .2])
            results.append((adapter, history))
        # All field observations, targets, sweep writes and rate restorations match.
        self.assertEqual(results[0][0].log, results[1][0].log)
        self.assertEqual(results[0][0].table, results[1][0].table)
        self.assertLess(results[1][1]['polling_diagnostics']['mode_checks'],
                        results[0][1]['polling_diagnostics']['mode_checks'])
        self.assertEqual(results[1][0].mode_calls[-1], results[1][0].clock)

    def test_standard_and_reduced_preserve_status_and_field_checks(self):
        results = []
        for profile in (None, 'reduced_mode_queries'):
            adapter = PollingAdapter()
            history = move(adapter, profile)
            diag = history['polling_diagnostics']
            self.assertEqual(diag['effective_profile'], profile or 'standard')
            self.assertEqual(diag['status_checks'], diag['iterations'])
            self.assertGreaterEqual(len(adapter.thermal_calls), diag['iterations'])
            self.assertGreaterEqual(adapter.log.count('field'), diag['iterations'])
            self.assertEqual(history['trace_started_monotonic_s'], history['started_monotonic_s'])
            self.assertGreaterEqual(diag['finished_monotonic_s'], diag['started_monotonic_s'])
            results.append((adapter, diag))
        self.assertEqual(results[0][1]['mode_checks'], results[0][1]['iterations'])
        self.assertLess(results[1][1]['mode_checks'], results[1][1]['iterations'])
        self.assertLess(len(results[1][0].mode_calls), len(results[0][0].mode_calls))

    def test_mode_change_detected_after_interval_and_pauses(self):
        adapter = PollingAdapter()
        adapter.mode_change_at = .2
        with self.assertRaisesRegex(APS100SafetyError, 'Manual'):
            move(adapter, 'reduced_mode_queries')
        self.assertIn('pause', adapter.log)
        self.assertGreaterEqual(adapter.mode_calls[-1], 1.)
        self.assertLessEqual(adapter.mode_calls[-1], 1.2)

    def test_fault_and_stop_remain_active_between_mode_queries(self):
        for event, error in (('fault_at', APS100SafetyError),
                             ('stop_at', APS100OperationCancelled)):
            with self.subTest(event=event):
                adapter = PollingAdapter()
                setattr(adapter, event, .2)
                with self.assertRaises(error):
                    move(adapter, 'reduced_mode_queries')
                self.assertIn('pause', adapter.log)
                self.assertLess(adapter.clock, 1.)

    def test_reversal_warns_between_mode_queries_and_continues_forward(self):
        for profile in ('standard', 'reduced_mode_queries'):
            with self.subTest(profile=profile):
                adapter = PollingAdapter()
                adapter.reverse_at = .2
                history = move(adapter, profile)
                self.assertAlmostEqual(history['end_t'], .02)
                self.assertTrue(history['reversal_exceeded'])
                self.assertEqual(history['reversal_event_count'], 1)
                event = history['reversal_events'][0]
                self.assertAlmostEqual(event['monotonic_s'], .2)
                self.assertGreater(event['excursion_t'], event['limit_t'])
                if profile == 'reduced_mode_queries':
                    self.assertFalse(any(0. < stamp < 1. for stamp in adapter.mode_calls))
                self.assertEqual([command for command in adapter.log
                                  if isinstance(command, str) and command.startswith('SWEEP')],
                                 ['SWEEP UP SLOW'])
                self.assertIn('pause', adapter.log)
                self.assertFalse(adapter.active)

    def test_preparation_forces_standard(self):
        adapter = PollingAdapter()
        history = move(adapter, 'reduced_mode_queries', preparation=True)
        diag = history['polling_diagnostics']
        self.assertEqual(diag['requested_profile'], 'reduced_mode_queries')
        self.assertEqual(diag['effective_profile'], 'standard')
        self.assertEqual(diag['mode_checks'], diag['iterations'])


if __name__ == '__main__':
    unittest.main()

class PollingBoundaryTests(unittest.TestCase):
    def test_invalid_profile_rejected_before_io(self):
        adapter = PollingAdapter()
        with self.assertRaises(APS100SafetyError):
            move(adapter, 'invalid')
        self.assertEqual(adapter.log, [])
        self.assertEqual(adapter.mode_calls, [])
        self.assertEqual(adapter.status_calls, [])

    def test_cached_mode_keeps_thermal_failure(self):
        adapter = PollingAdapter()
        def guard(status):
            if adapter.clock >= .2:
                raise APS100SafetyError('thermal failure')
        adapter.thermal_guard = guard
        with self.assertRaisesRegex(APS100SafetyError, 'thermal failure'):
            move(adapter, 'reduced_mode_queries')
        self.assertLess(adapter.clock, 1.)
        self.assertIn('pause', adapter.log)

    def test_old_history_has_new_trace_origin(self):
        adapter = PollingAdapter()
        adapter.clock = 20.
        history = dict(direction=1, started_monotonic_s=1., finished_monotonic_s=None,
                       min_t=None, max_t=None, max_reverse_t=0., read_count=0,
                       start_t=None, end_t=None, complete=False)
        options = dict(approach='linear_no_backward', acceptance_t=.0002,
                       reversal_t=.0002, lower_t=-.1, upper_t=.1, no_progress_s=15.,
                       polling_profile='reduced_mode_queries')
        with patch('app.devices.aps100_attodry1000_adapter.time.monotonic', side_effect=lambda: adapter.clock):
            adapter._hysteresis_field_move(.02, 1, .0002, 100., None, None, options, history)
        self.assertEqual(history['started_monotonic_s'], 1.)
        self.assertEqual(history['trace_started_monotonic_s'], 20.)

    def test_queries_count_attempts_without_extra_io(self):
        from tests.test_aps100_raw_diagnostics import FakeVisa
        visa = FakeVisa(['manual\r\n', OSError('read failed')])
        adapter = APS100AttoDry1000Adapter(visa_resource=visa)
        adapter._identity = object()
        self.assertEqual(adapter._query('MODE?'), 'manual')
        with self.assertRaises(OSError):
            adapter._query('IMAG?')
        self.assertEqual(visa.writes, ['MODE?', 'IMAG?'])
        self.assertEqual(dict(adapter._query_counts), {'MODE?': 1, 'IMAG?': 1})
