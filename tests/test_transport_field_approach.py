"""Offline approach routing and continuous endpoint regressions."""
import threading
import unittest
from types import SimpleNamespace as NS
from unittest.mock import patch
from utils.config import cfg

from app.models import BFieldTransportParams
from app.engine.bfield_transport_sweep import TransportSweepPlan, BFieldTransportSweep
from app.engine.field_sampling import FieldAssociation
from tests import test_stepped_transport as fixtures


class TransportApproachTests(unittest.TestCase):
    setUpClass = fixtures.SteppedTransportTests.setUpClass

    def setUp(self):
        fixtures.SteppedTransportTests.setUp(self)
        self.params.measurement_mode = 'continuous'
        self.params.field_approach = 'no_overshoot'
        self.controller.plan.validation['field_approach'] = 'no_overshoot'
        self.magnet.start_transport_sweep = lambda target, **kw: self.magnet.moves.append((target, kw))
        self.magnet.set_polling_enabled = lambda value: None
        self.addCleanup(self.controller._telemetry_watchdog.stop)
        self.addCleanup(self.controller._cleanup_timer.stop)

    def test_legacy_defaults_follow_acquisition_mode(self):
        self.params.field_approach = 'auto'
        self.assertEqual(self.controller._field_approach(), 'linear')
        self.params.measurement_mode = 'stepped'
        self.assertEqual(self.controller._field_approach(), 'no_overshoot')

    def test_nonlinear_branch_passes_bounded_options(self):
        self.controller._begin_leg(0, .1)
        target, options = self.magnet.moves[-1]
        self.assertEqual(target, .1)
        self.assertEqual(options['field_approach'], 'no_overshoot')
        self.assertGreater(options['timeout_s'], 0.)
        self.assertGreater(options['no_progress_s'], 0.)

    def snapshot(self, when, field, complete=False, mode='no_overshoot'):
        self.now = when
        return NS(monotonic_s=when, field_t=field, heater_on=True, approach_mode=mode,
            approach_complete=complete, approach_phase='settled' if complete else 'approach',
            status=NS(sweep_active=not complete, quench=False, power_module_failure=False, faulted=False))

    def test_target_readback_requires_completed_endpoint_approach(self):
        c = self.controller
        c._thermal_permission = lambda *args: (True, None)
        recorded, ended = [], []
        c._record_snapshot = recorded.append
        c._begin_endpoint_hold = lambda: ended.append(True)
        c._begin_leg(0, .1)
        c._on_sweep_started({'success': True})
        for when, field in ((101., .099), (102., .0999), (103., .1)):
            c._process_snapshot(self.snapshot(when, field))
        self.assertEqual(len(recorded), 3)
        self.assertEqual(ended, [])
        c._process_snapshot(self.snapshot(104., .1, True, mode='linear'))
        self.assertEqual(ended, [])
        c._process_snapshot(self.snapshot(105., .1, True))
        self.assertEqual(ended, [True])

    def test_nonlinear_rate_uses_stage_readback(self):
        c = self.controller
        self.assertIsNone(c._current_range_rate(NS(field_t=.05)))
        self.assertEqual(c._current_range_rate(NS(field_t=.05, approach_rate_t_per_min=.02)), .02)

    def test_linear_optional_settling_restarts_after_outside_snapshot(self):
        c = self.controller
        self.params.field_approach = 'linear'
        c._thermal_permission = lambda *args: (True, None)
        c._record_snapshot = lambda s: None
        ended = []
        c._begin_endpoint_hold = lambda: ended.append(True)
        c._begin_leg(0, .1)
        c._on_sweep_started({'success': True})
        with patch.object(cfg.mcd, 'transport_endpoint_settling_enabled', True), \
             patch.object(cfg.mcd, 'endpoint_stable_reads', 3), \
             patch.object(cfg.mcd, 'endpoint_dwell_s', 0., create=True):
            for when, field in ((101., .1), (102., .1), (103., .11), (104., .1), (105., .1)):
                c._process_snapshot(self.snapshot(when, field, mode='linear'))
                self.assertEqual(ended, [])
            c._process_snapshot(self.snapshot(106., .1, mode='linear'))
        self.assertEqual(ended, [True])

    def test_recovery_accepts_paused_field_inside_branch(self):
        c = self.controller
        c._thermal_permission = lambda *args: (True, None)
        c._measurement_phase = 'sweeping'
        c._note_continuous_interruption('telemetry')
        c._begin_leg(0, .1)
        self.magnet.latest_snapshot = self.snapshot(101., .075, True)
        c._continuous_wait_tick()
        self.assertFalse(c._cleanup_in_progress)
        self.assertEqual(self.magnet.moves[-1][0], .1)

    def test_initial_position_routes_selected_approach(self):
        self.magnet.latest_snapshot = NS(field_t=.05)
        self.controller._position_at_start()
        self.assertEqual(self.magnet.moves[-1][1]['field_approach'], 'no_overshoot')
        self.assertNotIn('monotonic_direction', self.magnet.moves[-1][1])

    def test_oscillation_envelope_validated_before_hardware(self):
        p = BFieldTransportParams(start_field_t=0., stop_field_t=1., field_approach='oscillate',
                                 measurement_mode='stepped', field_step_t=1.)
        with self.assertRaisesRegex(ValueError, 'excursion'):
            TransportSweepPlan.from_params(p, max_field_t=1.1)
        p.field_approach = 'linear'
        TransportSweepPlan.from_params(p, max_field_t=1.1)

    def test_unsupported_mode_and_2100_custom_approach_rejected(self):
        for p in (BFieldTransportParams(field_approach='bad'),
                  BFieldTransportParams(magnet_backend='2100', measurement_mode='stepped', field_approach='oscillate')):
            with self.assertRaises(ValueError):
                TransportSweepPlan.from_params(p)

    def test_synchronous_runner_rejects_nonlinear_approach(self):
        self.params.field_approach = 'no_overshoot'
        plan = TransportSweepPlan.from_params(self.params)
        with self.assertRaisesRegex(ValueError, 'acknowledged Qt'):
            BFieldTransportSweep(object(), plan).run()

    def test_continuous_oscillate_is_rejected_before_execution(self):
        self.params.field_approach = 'oscillate'
        with self.assertRaisesRegex(ValueError, 'Oscillate.*Stepped'):
            TransportSweepPlan.from_params(self.params)

    def test_continuous_endpoint_choices_and_stepped_approaches(self):
        for mode, choices in (('continuous', ('linear', 'no_overshoot')),
                              ('stepped', ('linear', 'no_overshoot', 'oscillate'))):
            for approach in choices:
                with self.subTest(mode=mode, approach=approach):
                    p = BFieldTransportParams(start_field_t=0., stop_field_t=.1,
                                             measurement_mode=mode, field_approach=approach)
                    self.assertEqual(TransportSweepPlan.from_params(p).validation['field_approach'], approach)

    def test_averaging_window_crossing_stage_boundary_is_marked_mixed(self):
        timing = FieldAssociation()
        timing.daq = [(1., 2.)]
        history = [NS(monotonic_s=0., field_t=0., approach_phase='ramp', approach_rate_t_per_min=.1),
                   NS(monotonic_s=3., field_t=.1, approach_phase='approach', approach_rate_t_per_min=.05)]
        row = timing.finish(dict(Field_approach='no_overshoot', Sweep_rate_T_per_min=.1),
                            lambda: history, threading.Event(), lambda: None)
        self.assertEqual(row['Field_approach_phase'], 'mixed')
        self.assertTrue(row['Endpoint_approach'])
        self.assertIsNone(row['Sweep_rate_T_per_min'])


if __name__ == '__main__':
    unittest.main()
