"""Hardware-blocked branch preparation and rate validation regressions."""
import unittest
from types import SimpleNamespace as NS

from app.engine.bfield_transport_sweep import BFieldTransportSafetyError, BFieldTransportSweep, TransportSweepPlan
import tests.test_stepped_transport as fixtures


class ContinuousWorkflowTests(unittest.TestCase):
    setUpClass = fixtures.SteppedTransportTests.setUpClass

    def setUp(self):
        fixtures.SteppedTransportTests.setUp(self)
        self.params.measurement_mode = 'continuous'
        self.params.wait_for_temperature = True
        self.params.pre_sweep_settle_s = 2.
        self.params.temperature_timeout_s = 20.
        self.controller.plan = TransportSweepPlan.from_params(self.params)
        self.magnet.start_transport_sweep = lambda target: self.magnet.moves.append(target)
        self.magnet.set_polling_enabled = lambda value: None
        self.addCleanup(self.controller._telemetry_watchdog.stop)
        self.addCleanup(self.controller._cleanup_timer.stop)

    def observe(self, when, temperature=4., field=0., active=False, valid=True):
        self.now = float(when)
        self.magnet.latest_snapshot = NS(monotonic_s=self.now, field_t=field, heater_on=True,
            status=NS(sweep_active=active, faulted=False, quench=False, power_module_failure=False))
        self.thermal.latest_snapshot = NS(monotonic_s=self.now, sample_temperature_k=temperature,
            connected=True, communication_valid=valid, sample_sensor_status=0)
        self.controller._on_telemetry_watchdog()

    def test_wait_for_temperature_then_extra_delay_before_any_ramp(self):
        c = self.controller
        c._begin_leg(0, .1)
        self.assertEqual(c._measurement_phase, 'sweep_wait')
        for when in range(101, 107):
            self.observe(when)
            self.assertEqual(self.magnet.moves, [])
            self.assertEqual(c._results, [])
        self.observe(107)
        self.assertEqual(self.magnet.moves, [.1])
        self.assertEqual(c._measurement_phase, 'starting_sweep')

    def test_temperature_excursion_restarts_stability_and_delay(self):
        self.controller._begin_leg(0, .1)
        for when in range(101, 106):
            self.observe(when)
        self.observe(106, temperature=4.2)
        for when in range(107, 113):
            self.observe(when)
            self.assertEqual(self.magnet.moves, [])
        self.observe(113)
        self.assertEqual(self.magnet.moves, [.1])

    def test_return_delay_runs_only_after_endpoint_pause_acknowledgement(self):
        c = self.controller
        self.params.wait_for_temperature = False
        self.params.pre_sweep_settle_s = 0.
        self.params.turnaround_delay_s = 3.
        self.params.round_trip = True
        c._leg_index = 0
        c._target = .1
        c._begin_endpoint_hold()
        self.assertEqual(c._measurement_phase, 'endpoint_hold')
        self.assertEqual(self.magnet.moves, [])
        c._on_magnet_operation('pause')
        self.assertEqual(c._measurement_phase, 'sweep_wait')
        for when in (101, 102, 103):
            self.observe(when, field=.1)
            self.assertEqual(self.magnet.moves, [])
        self.observe(104, field=.1)
        self.assertEqual(self.magnet.moves, [0.])

    def test_missing_or_moving_field_cannot_complete_preparation(self):
        self.params.wait_for_temperature = False
        self.controller._begin_leg(0, .1)
        for when in range(101, 110):
            self.observe(when, active=True)
        self.assertEqual(self.magnet.moves, [])
        self.now = 100. + self.params.field_timeout_s + 2.
        self.controller._on_telemetry_watchdog()
        self.assertTrue(self.controller._cleanup_in_progress)
        self.assertIn('no ramp started', self.controller._cleanup_detail)

    def test_stale_or_invalid_temperature_does_not_qualify(self):
        self.controller._begin_leg(0, .1)
        for when in range(101, 109):
            self.observe(when, valid=False)
        self.assertEqual(self.magnet.moves, [])
        self.observe(109)
        self.now = 113.
        self.controller._on_telemetry_watchdog()
        self.assertEqual(self.magnet.moves, [])

    def test_stop_during_wait_cannot_start_late_ramp(self):
        self.controller._begin_leg(0, .1)
        self.observe(101)
        self.controller._cleanup('stopped', 'User stop')
        self.observe(108)
        self.assertEqual(self.magnet.moves, [])

    def test_endpoint_wait_allows_queued_read_delays_but_remains_bounded(self):
        c = self.controller
        self.params.averages = 5
        self.params.read_delay_s = 100.
        c._measurement_phase = 'endpoint_hold'
        c._phase_started = 100.
        c._sample_pending = 2
        self.now = 500.
        c._on_telemetry_watchdog()
        self.assertFalse(c._cleanup_in_progress)
        self.now = 101. + 2 * c._acquisition_timeout_s()
        c._on_telemetry_watchdog()
        self.assertTrue(c._cleanup_in_progress)
        self.assertIn('endpoint_hold', c._cleanup_detail)

    def test_legacy_continuous_recipe_has_no_new_wait(self):
        self.params.wait_for_temperature = False
        self.params.pre_sweep_settle_s = 0.
        self.controller._begin_leg(0, .1)
        self.assertEqual(self.magnet.moves, [.1])

    def test_slow_range_inside_branch_rejected_but_unused_range_allowed(self):
        self.params.rate_t_per_min = .12
        rates = {0: (10., .12), 1: (20., .06), 2: (50., .12)}
        self.controller._verify_continuous_rates(rates, .2)
        self.params.stop_field_t = 3.
        with self.assertRaisesRegex(ValueError, '10 Oe/s'):
            self.controller._verify_continuous_rates(rates, .2)

    def test_continuous_interruption_is_recorded_in_manifest_validation(self):
        c = self.controller
        c._measurement_phase = 'sweeping'
        c._note_continuous_interruption('temperature')
        self.assertEqual(c.plan.validation['continuous_interruptions'][0]['reason'], 'temperature')
        self.assertEqual(c.plan.validation['continuous_interruptions'][0]['condition'], 1)

    def test_recovery_qualifies_paused_field_without_returning_to_start(self):
        c = self.controller
        c._measurement_phase = 'sweeping'
        c._note_continuous_interruption('temperature')
        c._begin_leg(0, .1)
        for when in range(101, 107):
            self.observe(when, field=.05)
            self.assertEqual(self.magnet.moves, [])
        self.observe(107, field=.05)
        self.assertEqual(self.magnet.moves, [.1])

    def test_default_recovery_requires_fresh_stationary_field(self):
        c = self.controller
        self.params.wait_for_temperature = False
        self.params.pre_sweep_settle_s = 0.
        c._measurement_phase = 'sweeping'
        c._note_continuous_interruption('telemetry')
        c._begin_leg(0, .1)
        self.assertEqual(self.magnet.moves, [])
        self.observe(101, field=.05, active=True)
        self.assertEqual(self.magnet.moves, [])
        self.observe(102, field=.05)
        self.assertEqual(self.magnet.moves, [.1])

    def test_one_rate_increment_below_requested_is_rejected(self):
        self.params.rate_t_per_min = .12
        rates = {0: (10., .1188), 1: (20., .12), 2: (50., .12)}
        with self.assertRaisesRegex(ValueError, '19.8 Oe/s'):
            self.controller._verify_continuous_rates(rates, .2)

    def test_interrupted_turnaround_wait_restarts_full_delay(self):
        c = self.controller
        self.params.wait_for_temperature = False
        self.params.pre_sweep_settle_s = 0.
        self.params.turnaround_delay_s = 3.
        c._begin_leg(1, 0.)
        self.observe(101, field=.1)
        c._note_continuous_interruption('telemetry')
        c._begin_leg(1, 0.)
        for when in (102, 103, 104):
            self.observe(when, field=.1)
            self.assertEqual(self.magnet.moves, [])
        self.observe(105, field=.1)
        self.assertEqual(self.magnet.moves, [0.])

    def test_interruption_during_recovery_wait_keeps_paused_field(self):
        c = self.controller
        c._measurement_phase = 'sweeping'
        c._note_continuous_interruption('temperature')
        c._begin_leg(0, .1)
        self.observe(101, field=.05)
        c._note_continuous_interruption('telemetry')
        c._begin_leg(0, .1)
        for when in range(102, 109):
            self.observe(when, field=.05)
        self.assertEqual(self.magnet.moves, [.1])

    def test_observed_rate_uses_field_timestamps_and_resets_between_segments(self):
        c = self.controller
        first = c._continuous_sample_metadata(NS(monotonic_s=100., field_t=.1))
        self.assertIsNone(first['Observed_sweep_rate_Oe_per_s'])
        row = c._continuous_sample_metadata(NS(monotonic_s=102., field_t=.096))
        self.assertAlmostEqual(row['Observed_sweep_rate_Oe_per_s'], -20.)
        self.assertEqual(row['B_readback_monotonic_s'], 102.)
        self.params.wait_for_temperature = False
        self.params.pre_sweep_settle_s = 0.
        c._begin_leg(0, .1)
        c._on_sweep_started({'success': True})
        row = c._continuous_sample_metadata(NS(monotonic_s=110., field_t=.08))
        self.assertIsNone(row['Observed_sweep_rate_Oe_per_s'])
        self.assertEqual(row['Continuous_segment'], 1)

    def test_synchronous_runner_rejects_preparation_wait_before_hardware(self):
        class NoHardware:
            def __getattr__(self, name):
                raise AssertionError('adapter must not be touched')
        runner = BFieldTransportSweep(NoHardware(), self.controller.plan)
        with self.assertRaisesRegex(BFieldTransportSafetyError, 'acknowledged Qt controller'):
            runner.run()

    def test_invalid_wait_settings_rejected_before_start(self):
        for key, value in (('pre_sweep_settle_s', -1.), ('turnaround_delay_s', float('nan')),
                           ('temperature_window_s', 100.), ('field_timeout_s', 0.)):
            with self.subTest(key=key):
                original = getattr(self.params, key)
                setattr(self.params, key, value)
                with self.assertRaises(ValueError):
                    TransportSweepPlan.from_params(self.params)
                setattr(self.params, key, original)


if __name__ == '__main__':
    unittest.main()
