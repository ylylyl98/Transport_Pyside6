"""Integration checks for readback-based hysteresis acquisition."""
import unittest
from unittest.mock import Mock

import tests.test_stepped_transport as fixtures
from app.engine.bfield_transport_sweep import TransportSweepPlan


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
        self.assertAlmostEqual(row['Field_gap_T'], .0005)
        self.assertAlmostEqual(self.magnet.moves[-1][0], .0006)
        self.assertFalse(c._thermal_hold)
        self.assertTrue(any('nominal_targets_crossed' in args[0] for args, _ in c._log.call_args_list))
        self.assertEqual(c._skipped_nominal[0]['index'], 3)

    def test_actual_gap_over_limit_fails_without_fabricating_a_point(self):
        self.params.field_max_gap_t = .0004
        self.at_point(1, .0005, previous=0.)
        self.qualify(.0005)
        self.assertTrue(self.controller._cleanup_in_progress)
        self.assertEqual(self.controller._results, [])
        self.assertEqual(self.acquisitions, [])
        self.assertTrue(any('coverage limit' in args[0] for args, _ in self.controller._log.call_args_list))

    def test_endpoint_does_not_use_looser_interior_acceptance(self):
        self.at_point(5, .0012, previous=.0008)
        self.reading(101, field=.0012)
        self.assertTrue(self.controller._cleanup_in_progress)
        self.assertEqual(self.acquisitions, [])

    def test_return_leg_uses_decreasing_readbacks(self):
        self.params.round_trip = True
        c = self.controller
        c._reset_step_condition()
        self.at_point(6, .0007, previous=.001)
        self.qualify(.0007)
        self.assertEqual(len(c._results), 1)
        self.assertEqual(c._results[0]['Direction'], 'backward')
        self.assertAlmostEqual(self.magnet.moves[-1][0], .0006)

    def test_duplicate_stationary_field_cannot_fill_another_point(self):
        self.at_point(2, .0003, previous=.0003)
        self.qualify(.0003)
        self.assertTrue(self.controller._cleanup_in_progress)
        self.assertEqual(self.acquisitions, [])

    def test_history_reversal_cannot_be_repaired_by_later_stability(self):
        self.at_point(2, .0005, previous=.0002)
        self.reading(101, field=.0006)
        self.reading(102, field=.0003)
        self.qualify(.0003, start=103)
        self.assertTrue(self.controller._cleanup_in_progress)
        self.assertEqual(self.acquisitions, [])
        self.assertGreater(self.controller._point_history['max_reverse_t'], self.params.field_reversal_t)

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
        self.assertTrue(self.controller._cleanup_in_progress)
        self.assertTrue(any('Field stability timeout' in args[0] for args, _ in self.controller._log.call_args_list))

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
        self.assertFalse(c._accept_move_history(next_result))
        self.assertTrue(c._cleanup_in_progress)

    def test_incomplete_history_is_never_accepted(self):
        c = self.controller
        c._move_step()
        result = self.move_result()
        result['field_history']['complete'] = False
        c._on_safe_move(result)
        self.assertTrue(c._cleanup_in_progress)

    def test_temperature_hold_still_tracks_field_history(self):
        c = self.controller
        self.at_point(2, .0004, previous=.0002)
        c._thermal_hold = True
        self.reading(101, field=.0006)
        self.reading(102, field=.0003)
        self.assertTrue(c._cleanup_in_progress)
        self.assertEqual(self.acquisitions, [])

    def test_known_magnet_telemetry_gap_blocks_reusing_branch(self):
        c = self.controller
        self.at_point(2, .0004, previous=.0002)
        c._pause_for = lambda reason: None
        c._begin_monitor_hold('APS100 telemetry timeout')
        self.assertTrue(c._hysteresis_history_gap)
        c._monitor_hold = None
        c._thermal_hold = False
        moves_before = len(self.magnet.moves)
        c._move_step()
        self.assertTrue(c._cleanup_in_progress)
        self.assertEqual(len(self.magnet.moves), moves_before)

    def test_motion_budget_scales_with_distance_without_hour_floor(self):
        c = self.controller
        small = c._hysteresis_move_timeout(0., .0002)
        large = c._hysteresis_move_timeout(0., 1.)
        self.assertLess(small, 60.)
        self.assertGreater(large, small)
        self.assertGreater(c._hysteresis_move_timeout(0., .0002, initial=True), small)

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
