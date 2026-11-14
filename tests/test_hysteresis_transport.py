"""Integration checks for readback-based hysteresis acquisition."""
import unittest
import json
import tempfile
from pathlib import Path
from unittest.mock import Mock

import tests.test_stepped_transport as fixtures
from app.engine.bfield_transport_sweep import TransportSweepPlan, write_series_manifest


class HysteresisAcceptanceTests(unittest.TestCase):
    setUpClass = fixtures.SteppedTransportTests.setUpClass
    reading = fixtures.SteppedTransportTests.reading
    move_result = fixtures.SteppedTransportTests.move_result

    def setUp(self):
        fixtures.SteppedTransportTests.setUp(self)
        p = self.params
        p.stop_field_t = .001
        p.field_points = 6
        p.field_tolerance_t = .0001
        p.field_endpoint_tolerance_t = .0001
        p.field_acceptance_t = .0003
        p.field_max_gap_t = .0006
        p.field_stability_s = 2.
        self.controller.plan = TransportSweepPlan.from_params(p)
        self.controller.plan.validation['aps100_rates'] = {'normal': {i: (10.*(i+1), .1) for i in range(5)}, 'coil_constant_t_per_a': .2}
        self.controller._reset_step_condition()
        self.controller._log = Mock()

    def at_point(self, index, field, previous=None):
        c = self.controller
        c._step_index = index
        c._last_point_field = previous
        c._move_step()
        result = self.move_result()
        result['field_history'].update(min_t=field, max_t=field, start_t=field, end_t=field)
        c._on_safe_move(result)
        c._on_magnet_operation('pause')

    def qualify(self, field, start=101):
        for when in range(start, start+6):
            self.reading(when, field=field)

    def test_stable_offset_accepts_readback_and_skips_passed_nominal_targets(self):
        c = self.controller
        self.at_point(1, .0005, previous=0.)
        self.qualify(.0005)
        self.assertEqual(len(c._results), 1)
        row = c._results[0]
        self.assertEqual(row['B_target_T'], .0002)
        self.assertEqual(row['B_measured_T'], .0005)
        self.assertEqual(row['Field_quality'], 'accepted_offset')
        self.assertFalse(row['Field_reversal_warning'])
        self.assertAlmostEqual(row['Field_gap_T'], .0005)
        self.assertAlmostEqual(self.magnet.moves[-1][0], .0006)
        self.assertFalse(c._thermal_hold)
        self.assertTrue(any('nominal_targets_crossed' in args[0] for args, _ in c._log.call_args_list))
        self.assertEqual(c._skipped_nominal[0]['index'], 3)

    def test_actual_gap_over_limit_is_recorded_without_fabricating_a_point(self):
        self.params.field_max_gap_t = .0004
        self.at_point(1, .0005, previous=0.)
        self.qualify(.0005)
        self.assertFalse(self.controller._cleanup_in_progress)
        self.assertEqual(len(self.controller._results), 1)
        self.assertEqual(self.controller._results[0]['Field_quality_warning'], 'coverage_gap')
        self.assertEqual(self.controller._results[0]['B_measured_T'], .0005)
        self.assertFalse(self.controller._results[0]['Field_reversal_warning'])
        self.assertEqual(self.controller.plan.validation['field_quality_warnings']['count'], 1)
        self.assertTrue(any('coverage_gap' in args[0] for args, _ in self.controller._log.call_args_list))

    def test_coverage_warning_is_in_manifest_before_point_commit(self):
        self.params.field_max_gap_t = .0004
        c = self.controller
        self.at_point(1, .0005, previous=0.)
        self.assertTrue(c._check_actual_point(.0005))
        self.assertEqual(c._results, [])
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'checkpoint.json'
            write_series_manifest(str(path), params=self.params, validation=c.plan.validation, results=[])
            saved = json.loads(path.read_text(encoding='utf-8'))
        observed = saved['hysteresis_policy']['field_quality_warnings_observed']
        self.assertEqual(observed['count'], 1)
        self.assertEqual(observed['events'][0]['reason'], 'coverage_gap')

    def test_endpoint_offset_is_recorded_at_actual_field(self):
        self.params.round_trip = True
        self.controller._reset_step_condition()
        self.at_point(5, .0012, previous=.0008)
        self.qualify(.0012)
        self.reading(107, field=.0012)
        self.assertEqual(len(self.controller._results), 1)
        row = self.controller._results[0]
        self.assertAlmostEqual(row['B_target_T'], .001)
        self.assertAlmostEqual(row['B_measured_T'], .0012)
        self.assertIn('endpoint_offset', row['Field_quality_warning'])
        self.assertAlmostEqual(self.magnet.moves[-1][0], .0008)
        self.assertEqual(self.magnet.moves[-1][1]['monotonic_direction'], -1)

    def test_interior_overshoot_skips_crossed_targets_without_reverse(self):
        for index, field, previous, expected, direction in ((1, .00055, 0., .0008, 1),
                                                            (6, .00045, .001, .0002, -1)):
            with self.subTest(direction=direction):
                self.setUp()
                self.params.round_trip = True
                self.controller._reset_step_condition()
                self.at_point(index, field, previous=previous)
                moves = len(self.magnet.moves)
                self.qualify(field)
                self.assertEqual(self.controller._results, [])
                self.assertGreater(self.controller.plan.validation['skipped_quality_points']['count'], 0)
                self.assertEqual(len(self.magnet.moves), moves + 1)
                self.assertAlmostEqual(self.magnet.moves[-1][0], expected)
                self.assertEqual(self.magnet.moves[-1][1]['monotonic_direction'], direction)

    def test_duplicate_turnaround_is_logged_and_moves_to_return_once(self):
        self.params.round_trip = True
        self.controller._reset_step_condition()
        self.at_point(5, .001, previous=.001)
        self.qualify(.001)
        self.assertEqual(self.controller._results, [])
        summary = self.controller.plan.validation['skipped_quality_points']
        self.assertEqual(summary['count'], 1)
        self.assertTrue(summary['events'][0]['endpoint'])
        self.assertAlmostEqual(self.magnet.moves[-1][0], .0008)
        self.assertEqual(self.magnet.moves[-1][1]['monotonic_direction'], -1)

    def test_typed_worker_overshoot_waits_for_pause_and_fresh_qualification(self):
        self.params.round_trip = True
        c = self.controller
        c._reset_step_condition()
        c._step_index = 5
        c._last_point_field = .0008
        c._move_step()
        result = self.move_result()
        result.update(success=False, overshoot=True, quality_overshoot=True)
        result['field_history'].update(start_t=.0008, end_t=.0012, min_t=.0008, max_t=.0012)
        c._on_safe_move(result)
        self.assertEqual(c._measurement_phase, 'step_pausing')
        self.assertEqual(c._results, [])
        c._on_magnet_operation('pause')
        self.qualify(.0012)
        self.reading(107, field=.0012)
        self.assertEqual(len(c._results), 1)
        self.assertAlmostEqual(c._results[0]['B_measured_T'], .0012)
        self.assertAlmostEqual(self.magnet.moves[-1][0], .0008)
        self.assertEqual(self.magnet.moves[-1][1]['monotonic_direction'], -1)

    def test_return_leg_uses_decreasing_readbacks(self):
        self.params.round_trip = True
        c = self.controller
        c._reset_step_condition()
        self.at_point(6, .0007, previous=.001)
        self.qualify(.0007)
        self.assertEqual(len(c._results), 1)
        self.assertEqual(c._results[0]['Direction'], 'backward')
        self.assertFalse(c._results[0]['Field_reversal_warning'])
        self.assertAlmostEqual(self.magnet.moves[-1][0], .0006)

    def test_manifest_serializes_warning_policy_and_row_flag(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'manifest.json'
            warnings = {'point_count': 1, 'events': [{'point_index': 1}]}
            write_series_manifest(str(path), params=self.params, validation={'readback_reversal_warnings': warnings}, results=[
                {'Field_reversal_warning': True, 'Field_quality': 'readback_reversal'}])
            data = json.loads(path.read_text(encoding='utf-8'))
            write_series_manifest(str(path), params=self.params, validation={'readback_reversal_warnings': warnings}, results=[])
            before_sample = json.loads(path.read_text(encoding='utf-8'))
        self.assertTrue(data['results'][0]['Field_reversal_warning'])
        self.assertEqual(data['hysteresis_policy']['reversal_action'], 'flag_and_continue')
        self.assertEqual(data['hysteresis_policy']['reversal_warning_points'], 1)
        self.assertEqual(data['hysteresis_policy']['version'], 5)
        self.assertEqual(data['hysteresis_policy']['duplicate_action'], 'skip_nominal_point')
        self.assertEqual(data['hysteresis_policy']['history_diagnostics_action'], 'flag_and_requalify')
        self.assertEqual(data['hysteresis_policy']['target_overshoot_action'], 'skip_interior_or_record_actual_endpoint')
        self.assertEqual(before_sample['hysteresis_policy']['reversal_warnings_observed']['point_count'], 1)

    def test_duplicate_stationary_field_skips_without_fabricated_row(self):
        self.at_point(2, .0003, previous=.0003)
        self.qualify(.0003)
        self.assertFalse(self.controller._cleanup_in_progress)
        self.assertEqual(self.acquisitions, [])
        self.assertEqual(self.controller.plan.validation['skipped_quality_points']['count'], 1)
        self.assertAlmostEqual(self.magnet.moves[-1][0], .0006)

    def test_distinct_backward_point_is_flagged_not_rejected(self):
        c = self.controller
        self.at_point(2, .0003, previous=.0005)
        self.qualify(.0003)
        self.assertFalse(c._cleanup_in_progress)
        self.assertTrue(c._results[0]['Field_reversal_warning'])
        self.assertEqual(c._results[0]['Field_quality'], 'readback_reversal')

    def test_warning_does_not_leak_to_next_point(self):
        c = self.controller
        self.at_point(2, .0004, previous=.0002)
        self.reading(101, field=.0006)
        self.qualify(.0003, start=102)
        self.assertTrue(c._results[0]['Field_reversal_warning'])
        self.assertFalse(c._point_history.get('reversal_exceeded', False))

    def test_post_acquisition_reversal_flags_verified_row(self):
        self.params.field_span_t = .001
        self.params.field_drift_t_per_s = .001
        c = self.controller
        self.at_point(2, .0005, previous=.0001)
        for when in range(101, 106):
            self.reading(when, field=.0005)
        self.assertEqual(c._measurement_phase, 'step_verifying')
        self.reading(106, field=.0002)
        self.assertEqual(len(c._results), 1)
        self.assertTrue(c._results[0]['Field_reversal_warning'])
        self.assertGreater(c._results[0]['B_max_reverse_T'], self.params.field_reversal_t)

    def test_history_reversal_is_retained_after_later_stability(self):
        self.at_point(2, .0005, previous=.0002)
        self.reading(101, field=.0006)
        self.reading(102, field=.0003)
        self.qualify(.0003, start=103)
        self.assertFalse(self.controller._cleanup_in_progress)
        self.assertEqual(len(self.acquisitions), 1)
        self.assertTrue(self.controller._results[0]['Field_reversal_warning'])
        self.assertEqual(self.controller._results[0]['Field_quality'], 'readback_reversal')
        self.assertGreater(self.controller._results[0]['B_max_reverse_T'], self.params.field_reversal_t)

    def test_preparation_history_is_recorded_but_fresh_first_point_requires_qualification(self):
        c = self.controller
        result = self.move_result(initial=True)
        result['field_history'].update(max_reverse_t=.0019, preparation_stable=True)
        self.assertTrue(c._accept_move_history(result, initial=True))
        self.assertEqual(c._initial_field_history['max_reverse_t'], 0.)
        self.assertEqual(c._initial_field_history['start_t'], result['field_history']['end_t'])
        self.assertTrue(any('move_history' in args[0] for args, _ in c._log.call_args_list))
        self.at_point(0, 0.)
        self.assertEqual(self.acquisitions, [])
        self.qualify(0.)
        self.assertEqual(len(self.acquisitions), 1)
        self.assertLessEqual(c._results[0]['B_max_reverse_T'], self.params.field_reversal_t)

    def test_reversal_between_preparation_and_first_point_warns(self):
        c = self.controller
        result = self.move_result(initial=True)
        result['field_history'].update(preparation_stable=True, direction=1)
        self.assertTrue(c._accept_move_history(result, initial=True))
        c._move_step()
        moved = self.move_result()
        moved['field_history'].update(start_t=-.0003, end_t=-.0003,
                                      min_t=-.0003, max_t=-.0003)
        self.assertTrue(c._accept_move_history(moved))
        self.assertTrue(c._point_history['reversal_exceeded'])
        self.assertEqual(self.acquisitions, [])

    def test_preparation_must_have_confirmed_stability(self):
        result = self.move_result(initial=True)
        result['field_history'].pop('preparation_stable')
        self.assertFalse(self.controller._accept_move_history(result, initial=True))
        self.assertTrue(self.controller._cleanup_in_progress)
        self.assertEqual(self.acquisitions, [])

    def test_cancelled_preparation_cannot_reuse_prior_stability(self):
        c = self.controller
        c._initial_field_history = {'preparation_stable': True}
        result = self.move_result()
        result.update(success=False, cancelled=True)
        result['field_history']['complete'] = False
        self.assertTrue(c._accept_move_history(result, initial=True))
        self.assertEqual(c._initial_field_history, {})

    def test_small_readback_noise_does_not_count_as_history_failure(self):
        self.at_point(2, .0004, previous=.0002)
        self.reading(101, field=.0005)
        self.qualify(.0004, start=102)
        self.assertFalse(self.controller._cleanup_in_progress)
        self.assertEqual(len(self.controller._results), 1)

    def test_stable_field_waits_for_temperature_independently(self):
        self.params.field_timeout_s = 6.
        self.params.temperature_timeout_s = 20.
        self.at_point(2, .0004, previous=.0002)
        for when in range(101, 113):
            self.reading(when, field=.0004, temperature=4.2)
        self.assertFalse(self.controller._cleanup_in_progress)
        self.assertEqual(self.acquisitions, [])
        self.qualify(.0004, start=113)
        self.assertEqual(len(self.controller._results), 1)

    def test_unstable_field_does_not_consume_long_temperature_budget(self):
        self.params.field_timeout_s = 6.
        self.params.temperature_timeout_s = 60.
        self.params.field_span_t = .0001
        self.at_point(2, .0004, previous=.0002)
        for when in range(101, 108):
            self.reading(when, field=.0004 + (.00015 if when % 2 else 0.))
        self.assertFalse(self.controller._cleanup_in_progress)
        self.assertFalse(self.controller._thermal_hold)
        self.assertEqual(self.controller._measurement_phase, 'step_settling')
        self.assertTrue(any('Field stability timeout' in args[0] for args, _ in self.controller._log.call_args_list))

    def test_field_timeout_waits_then_automatically_acquires_with_fresh_budget(self):
        self.params.field_timeout_s = 6.
        self.params.temperature_timeout_s = 60.
        self.params.field_span_t = .0001
        c = self.controller
        self.at_point(2, .0004, previous=.0002)
        c._point_history['reversal_exceeded'] = True
        c._point_history['quality_warning_reasons'] = ['coverage_gap']
        moves = len(self.magnet.moves)
        for when in range(101, 108):
            self.reading(when, field=.0004 + (.00015 if when % 2 else 0.))
        self.assertFalse(c._thermal_hold)
        self.assertEqual(c._step_index, 2)
        self.assertEqual(len(self.magnet.moves), moves)
        self.assertTrue(c._point_history['reversal_exceeded'])
        self.assertEqual(c._point_history['quality_warning_reasons'], ['coverage_gap'])
        self.assertEqual(c._step_wait_started, 106.)
        self.qualify(.0004, start=108)
        self.assertEqual(len(c._results), 1)
        self.assertTrue(c._results[0]['Field_reversal_warning'])
        self.assertEqual(c._results[0]['Field_quality_warning'], 'coverage_gap')

    def test_timeout_stale_field_enters_monitor_hold_not_continue_hold(self):
        c = self.controller
        self.at_point(2, .0004, previous=.0002)
        self.reading(101, field=.0004)
        self.now = 106.
        c._pause_for = lambda reason: None
        c._step_timeout(reason='Field stability timeout')
        self.assertIsNotNone(c._monitor_hold)
        self.assertFalse(c._thermal_hold)

    def test_repeated_temperature_timeouts_log_once_per_cycle_and_stop(self):
        self.params.temperature_timeout_s = 6.
        c = self.controller
        self.at_point(2, .0004, previous=.0002)
        moves = len(self.magnet.moves)
        for when in range(101, 120):
            self.reading(when, field=.0004, temperature=4.2)
        summary = c.plan.validation['qualification_waits']
        self.assertEqual(summary['count'], 3)
        self.assertEqual(sum('qualification_wait' in args[0] for args, _ in c._log.call_args_list), 3)
        self.assertEqual(len(self.magnet.moves), moves)
        self.assertEqual(c._results, [])
        self.assertFalse(c._thermal_hold)
        c.stop()
        self.assertTrue(c._cleanup_in_progress)

    def test_timeout_with_faulted_field_does_not_offer_continue(self):
        c = self.controller
        self.at_point(2, .0004, previous=.0002)
        snap = self.reading(101, field=.0004)
        snap.status.quench = True
        c._step_timeout(reason='Field stability timeout')
        self.assertTrue(c._cleanup_in_progress)
        self.assertFalse(c._thermal_hold)

    def test_stop_from_qualification_wait_starts_cleanup(self):
        c = self.controller
        self.at_point(2, .0004, previous=.0002)
        self.reading(101, field=.0004)
        c._step_timeout(reason='Field stability timeout')
        self.assertFalse(c._thermal_hold)
        c.stop()
        self.assertTrue(c._cleanup_in_progress)

    def test_thermal_denial_during_wait_still_requires_manual_recovery(self):
        c = self.controller
        self.at_point(2, .0004, previous=.0002)
        self.reading(101, field=.0004)
        c._step_timeout(reason='Field stability timeout')
        moves = len(self.magnet.moves)
        self.thermal.evaluate_continuation = lambda snapshot: type('Decision', (), {'magnet_permission': False, 'state': 'WARNING', 'reason': 'hot'})()
        c._pause_for = lambda reason: None
        self.reading(102, field=.0004)
        self.assertTrue(c._thermal_hold)
        self.assertTrue(c._thermal_manual_required)
        self.assertEqual(len(self.magnet.moves), moves)

    def test_cumulative_cancelled_history_survives_retry(self):
        c = self.controller
        self.at_point(2, .0004, previous=.0002)
        self.reading(101, field=.0005)
        c._thermal_hold = True
        result = self.move_result()
        result.update(success=False, cancelled=True, cancellation_reason='thermal')
        result['field_history'].update(start_t=.0005, min_t=.0004, max_t=.0005, end_t=.0004, max_reverse_t=.0001)
        self.assertTrue(c._accept_move_history(result))
        c._thermal_hold = False
        c._move_step()
        self.assertEqual(c._point_history['max_t'], .0005)
        next_result = self.move_result()
        next_result['field_history'].update(start_t=.0003, min_t=.0002, max_t=.0003, end_t=.0002, max_reverse_t=.0001)
        self.assertTrue(c._accept_move_history(next_result))
        self.assertFalse(c._cleanup_in_progress)
        self.assertTrue(c._point_history['reversal_exceeded'])

    def test_incomplete_history_gets_fresh_baseline(self):
        c = self.controller
        c._move_step()
        result = self.move_result()
        result['field_history']['complete'] = False
        c._on_safe_move(result)
        self.assertFalse(c._cleanup_in_progress)
        c._on_magnet_operation('pause')
        self.qualify(0.)
        self.assertEqual(len(c._results), 1)
        self.assertIn('history_uncertain', c._results[0]['Field_quality_warning'])

    def test_mismatched_history_direction_flags_but_wrong_command_fails(self):
        c = self.controller
        c._step_index = 1
        c._last_point_field = 0.
        c._move_step()
        result = self.move_result()
        result['field_history']['direction'] = -1
        c._on_safe_move(result)
        self.assertFalse(c._cleanup_in_progress)
        self.assertIn('history_uncertain', c._point_history['quality_warning_reasons'])
        c._on_magnet_operation('pause')
        self.qualify(.0002)
        self.assertEqual(len(c._results), 1)
        self.assertIn('history_uncertain', c._results[0]['Field_quality_warning'])
        c._on_safe_move({'success': True, 'monotonic_direction': -1})
        self.assertTrue(c._cleanup_in_progress)

    def test_incomplete_initial_diagnostics_still_require_first_point_qualification(self):
        c = self.controller
        self.assertTrue(c._accept_move_history({'success': True, 'field_history': None}, initial=True))
        uncertain = c._initial_history_uncertain
        c._reset_step_condition()
        c._initial_history_uncertain = uncertain
        self.at_point(0, 0.)
        self.assertEqual(c._results, [])
        self.qualify(0.)
        self.assertEqual(len(c._results), 1)
        self.assertIn('history_uncertain', c._results[0]['Field_quality_warning'])

    def test_temperature_hold_still_tracks_field_history(self):
        c = self.controller
        self.at_point(2, .0004, previous=.0002)
        c._thermal_hold = True
        self.reading(101, field=.0006)
        self.reading(102, field=.0003)
        self.assertFalse(c._cleanup_in_progress)
        self.assertTrue(c._point_history['reversal_exceeded'])
        self.assertEqual(self.acquisitions, [])

    def test_known_magnet_telemetry_gap_marks_history_after_recovery(self):
        c = self.controller
        self.at_point(2, .0004, previous=.0002)
        c._pause_for = lambda reason: None
        c._begin_monitor_hold('APS100 telemetry timeout')
        self.assertTrue(c._hysteresis_history_gap)
        c._monitor_hold = None
        c._thermal_hold = False
        moves_before = len(self.magnet.moves)
        c._move_step()
        self.assertFalse(c._cleanup_in_progress)
        self.assertEqual(len(self.magnet.moves), moves_before + 1)
        self.assertIn('history_uncertain', c._point_history['quality_warning_reasons'])

    def test_motion_budget_scales_with_distance_without_hour_floor(self):
        c = self.controller
        small = c._hysteresis_move_timeout(0., .0002)
        large = c._hysteresis_move_timeout(0., 1.)
        self.assertLess(small, 3600.)
        self.assertGreater(large, small)
        self.assertGreater(c._hysteresis_move_timeout(0., .0002, initial=True), small)

    def test_motion_budget_covers_quantized_slow_rate_below_twenty_percent(self):
        c = self.controller
        c.plan.validation['aps100_rates'] = {
            'normal': {i: (10. * (i+1), .0108) for i in range(5)},
            'coil_constant_t_per_a': .2,
        }
        # 0.9 mA/s reduced by 20% rounds down to 0.1 mA/s, not 0.18.
        self.assertGreaterEqual(c._hysteresis_move_timeout(0., 1.), 1. / .0012 * 60.)

    def logged_second_point_move(self):
        c = self.controller
        self.params.start_field_t = -.2
        self.params.stop_field_t = .2
        self.params.field_points = 21
        self.params.field_max_gap_t = 0.
        c.plan = TransportSweepPlan.from_params(self.params)
        c.plan.validation['aps100_rates'] = {
            'normal': {i: (10.*(i+1), .11952864) for i in range(5)},
            'coil_constant_t_per_a': .20328,
        }
        c._reset_step_condition()
        c._step_index = 1
        c._last_point_field = -.2
        self.reading(100., field=-.2)
        c._move_step()
        return c

    def test_logged_slow_approach_can_finish_after_old_eighty_second_deadline(self):
        c = self.logged_second_point_move()
        # Replay the observed duration without qualifying a moving field.
        for elapsed, field in ((10., -.1936), (70., -.1818), (82., -.1807), (95., -.1801)):
            self.now = 100. + elapsed
            c._on_transition_progress('ramping field', field)
            c._on_telemetry_watchdog()
            self.assertFalse(c._cleanup_in_progress)
            self.assertEqual(c._results, [])
        c._on_safe_move(self.move_result())
        self.assertEqual(c._measurement_phase, 'step_pausing')
        c._on_magnet_operation('pause')
        self.assertEqual(c._measurement_phase, 'step_settling')
        self.assertEqual(c._results, [])

    def test_missing_completion_still_expires_despite_progress_messages(self):
        c = self.logged_second_point_move()
        self.magnet.pause_for = Mock()
        # Allow bounded delivery time for the worker's final result.
        self.now = 100. + c._step_move_timeout + 1.
        c._on_telemetry_watchdog()
        self.assertFalse(c._cleanup_in_progress)
        self.now += c._communication_timeout_s()
        c._on_transition_progress('ramping field', -.1807)
        c._on_telemetry_watchdog()
        self.assertTrue(c._cleanup_in_progress)
        self.assertIn('Stepped field move timed out', c._cleanup_detail)
        self.magnet.pause_for.assert_called_once_with('failure')
        self.assertEqual(c._results, [])

    def test_failure_cleanup_keeps_automatic_cancellation_origin(self):
        c = self.logged_second_point_move()
        self.magnet.pause_for = Mock()
        c._fail('injected move failure')
        self.magnet.pause_for.assert_called_once_with('failure')

    def test_small_step_is_not_swallowed_by_approach_tolerance(self):
        self.params.stop_field_t = .0005
        c = self.controller
        c._reset_step_condition()
        c._step_index = 1
        c._last_point_field = 0.
        c._move_step()
        target, kwargs = self.magnet.moves[-1]
        self.assertAlmostEqual(target, .0001)
        self.assertLess(kwargs['tolerance_t'], target)

    def test_worker_failure_history_is_logged_after_error_started_cleanup(self):
        c = self.controller
        c._move_step()
        result = self.move_result()
        result.update(success=False, error='injected history failure')
        c._cleanup_in_progress = True
        c._on_safe_move(result)
        self.assertTrue(any('move_history' in args[0] and 'injected history failure' in args[0]
                            for args, _ in c._log.call_args_list))

    def test_max_gap_validation_and_finite_limits(self):
        for key, value in [('field_max_gap_t', .0001), ('field_acceptance_t', float('nan')),
                           ('field_no_progress_s', 0.), ('field_drift_t_per_s', float('inf'))]:
            original = getattr(self.params, key)
            setattr(self.params, key, value)
            with self.assertRaises(ValueError):
                TransportSweepPlan.from_params(self.params)
            setattr(self.params, key, original)


if __name__ == '__main__':
    unittest.main()
