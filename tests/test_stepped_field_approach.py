"""Offline routing and qualification checks for stepped field approaches."""
import unittest

import tests.test_stepped_transport as fixtures
from app.engine.stepped_transport import STEP_COLUMNS


class SteppedFieldApproachTests(unittest.TestCase):
    setUpClass = fixtures.SteppedTransportTests.setUpClass
    setUp = fixtures.SteppedTransportTests.setUp
    reading = fixtures.SteppedTransportTests.reading
    move_result = fixtures.SteppedTransportTests.move_result
    pause_at_point = fixtures.SteppedTransportTests.pause_at_point
    settle = fixtures.SteppedTransportTests.settle

    def test_auto_resolves_by_measurement_mode_and_defaults_to_strict_stepped(self):
        c = self.controller
        self.params.field_approach = "auto"
        self.assertEqual(c._field_approach(), "no_overshoot")
        self.assertTrue(c._strict_field_history())
        self.params.measurement_mode = "continuous"
        self.assertEqual(c._field_approach(), "linear")
        self.assertFalse(c._strict_field_history())

    def test_aps_routes_approaches_without_conflicting_options(self):
        for approach in ("auto", "no_overshoot", "linear", "oscillate"):
            with self.subTest(approach=approach):
                self.params.field_approach = approach
                self.controller._move_step()
                kwargs = self.magnet.moves[-1][1]
                if approach in ("auto", "no_overshoot"):
                    self.assertIn("monotonic_direction", kwargs)
                    self.assertIsInstance(kwargs["hysteresis_options"], dict)
                    self.assertNotIn("field_approach", kwargs)
                else:
                    self.assertEqual(kwargs["field_approach"], approach)
                    self.assertNotIn("monotonic_direction", kwargs)
                    self.assertNotIn("hysteresis_options", kwargs)

    def test_non_strict_move_allows_missing_or_reversed_history_but_keeps_coverage(self):
        c = self.controller
        for approach in ("linear", "oscillate"):
            self.params.field_approach = approach
            self.assertTrue(c._accept_move_history({"success": True}))
            c._step_index, c._target, c._step_direction = 1, .05, 1
            c._last_point_field = .05
            c._quality_failure = lambda reason: False
            self.assertFalse(c._check_actual_point(.05))

    def test_oscillate_requalifies_stable_field_after_reversal(self):
        self.params.field_approach = "oscillate"
        c = self.controller
        self.pause_at_point()
        self.reading(101, field=.0002)
        self.reading(102, field=-.0002)
        for when in range(103, 109):
            self.reading(when, field=0.)
        self.assertFalse(c._cleanup_in_progress)
        self.assertEqual(len(c._results), 1)
        self.assertEqual(c._results[0]["Field_approach"], "oscillate")
        self.assertIn("Field_approach", STEP_COLUMNS)

    def test_history_gap_warns_and_resets_baseline_after_monitor_recovery(self):
        c = self.controller
        for approach in ("linear", "oscillate", "no_overshoot", "linear_no_backward"):
            with self.subTest(approach=approach):
                self.params.field_approach = approach
                c._history_point_identity = (c._condition_index, c._step_index)
                c._point_history = dict(start_t=.008, end_t=.009,
                                        quality_warning_reasons=['coverage_gap'], reversal_exceeded=True)
                c._history_stamp, c._history_extreme = 90., .009
                c._hysteresis_history_gap = True
                moves_before = len(self.magnet.moves)
                c._monitor_hold = {'reason': 'APS100 telemetry timeout'}
                c._move_step()
                self.assertEqual(len(self.magnet.moves), moves_before)
                self.assertTrue(c._hysteresis_history_gap)

                c._monitor_hold = None
                c._move_step()
                self.assertFalse(c._cleanup_in_progress)
                self.assertEqual(len(self.magnet.moves), moves_before + 1)
                self.assertFalse(c._hysteresis_history_gap)
                self.assertEqual(c._point_history['quality_warning_reasons'],
                                 ['coverage_gap', 'history_uncertain'])
                self.assertTrue(c._point_history['reversal_exceeded'])
                self.assertNotIn('start_t', c._point_history)
                self.assertNotIn('end_t', c._point_history)
                self.assertEqual(c._history_stamp, float('-inf'))
                self.assertFalse(hasattr(c, '_history_extreme'))
                self.assertEqual(c._measurement_phase, 'step_moving')
                self.assertEqual(self.acquisitions, [])
