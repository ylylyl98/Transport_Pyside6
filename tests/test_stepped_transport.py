import unittest
from copy import deepcopy
from types import SimpleNamespace as NS
from unittest.mock import Mock, patch

from PySide6 import QtCore
from app.models import BFieldTransportParams
from app.engine.bfield_transport_sweep import TransportSweepPlan
from app.engine.bfield_transport_controller import BFieldTransportController
from app.engine.transport_stability import TemperatureWindow


class Magnet(QtCore.QObject):
    transport_config_result = QtCore.Signal(object)
    safe_move_result = QtCore.Signal(object)
    transport_sweep_result = QtCore.Signal(object)
    snapshot_updated = QtCore.Signal(object)
    fault = QtCore.Signal(str)
    operation_finished = QtCore.Signal(str)

    def __init__(self):
        super().__init__()
        self.moves, self.pauses = [], 0
        self.latest_snapshot = None

    def safe_move_to_field(self, value, **kwargs):
        self.moves.append((value, kwargs))

    def pause(self):
        self.pauses += 1

    def refresh_snapshot(self):
        pass


class SteppedTransportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtCore.QCoreApplication.instance() or QtCore.QCoreApplication([])

    def setUp(self):
        self.now = 100.
        self.clock = patch('app.engine.stepped_transport.time.monotonic', side_effect=lambda: self.now)
        self.clock.start()
        self.addCleanup(self.clock.stop)
        self.params = BFieldTransportParams(
            measurement_mode='stepped', start_field_t=0., stop_field_t=.1,
            field_spacing='points', field_points=3, round_trip=False, temperature_target_k=4.,
            temperature_window_s=4., temperature_timeout_s=12., field_stability_s=.1, field_settle_s=0.)
        self.magnet = Magnet()
        self.thermal = NS(is_armed=True, latest_snapshot=None,
                          evaluate=lambda s: NS(magnet_permission=True),
                          evaluate_continuation=lambda s: NS(magnet_permission=True))
        self.controller = c = BFieldTransportController(self.magnet, NS(), NS(), self.thermal)
        c.plan = TransportSweepPlan.from_params(self.params)
        c.plan.validation["aps100_rates"] = {"normal": {i: (10. * (i + 1), .1) for i in range(5)}, "coil_constant_t_per_a": .2}
        c._active = True
        c._zero_biases = lambda: None
        c._read_sample = lambda row, condition: dict(row, Ids_DC=1.)
        c._reset_step_condition()
        c._initial_step_direction = 1
        self.acquisitions = []
        original = c._read_sample
        def read(row, condition):
            self.acquisitions.append(row.copy())
            return original(row, condition)
        c._read_sample = read

    def reading(self, when, temperature=4., field=None, active=False):
        self.now = float(when)
        c = self.controller
        self.thermal.latest_snapshot = NS(monotonic_s=self.now, sample_temperature_k=temperature,
            reservoir_temperature_k=3.5, connected=True, communication_valid=True, sample_sensor_status=0)
        self.magnet.latest_snapshot = snap = NS(monotonic_s=self.now, field_t=c._target if field is None else field,
            heater_on=True, sweep_state="sweeping" if active else "paused", reading_age_s=0., status=NS(sweep_active=active, quench=False,
                                                       power_module_failure=False, faulted=False))
        c._on_snapshot(snap)
        return snap

    def move_result(self, *, initial=False, direction=None):
        c = self.controller
        target = self.params.start_field_t if initial else c._target
        direction = direction if direction is not None else (getattr(c, "_step_direction", 1) or 1)
        history = dict(min_t=target, max_t=target, start_t=target, end_t=target,
                       max_reverse_t=0., started_monotonic_s=self.now - .1,
                       finished_monotonic_s=self.now, read_count=2, direction=direction, complete=True)
        return {"success": True, "monotonic_direction": direction, "field_history": history}

    def pause_at_point(self):
        c = self.controller
        c._move_step()
        c._on_safe_move(self.move_result())
        c._on_magnet_operation('pause')

    def settle(self, first=101):
        for when in range(first, first + 5):
            self.reading(when)

    def test_field_window_span_and_additional_settle_delay(self):
        self.params.field_reversal_t = .001
        self.params.temperature_window_s = 4.
        self.params.field_stability_s = 2.
        self.params.field_settle_s = 2.
        self.pause_at_point()
        for when, field in ((101, -.00019), (102, .00019), (103, -.00019), (104, .00019)):
            self.reading(when, field=field)
        self.assertEqual(self.acquisitions, [])
        for when in (105, 106, 107):
            self.reading(when, field=.00005)
        self.assertEqual(self.acquisitions, [])
        for when in (108, 109, 110, 111):
            self.reading(when, field=.00005)
        self.assertEqual(len(self.acquisitions), 1)
        self.assertAlmostEqual(self.acquisitions[0]['B_measured_T'], .00005)

    def test_field_excursion_resets_additional_settle_delay(self):
        self.params.field_reversal_t = .001
        self.params.field_stability_s = .1
        self.params.field_settle_s = 2.
        self.params.temperature_timeout_s = 30.
        self.pause_at_point()
        for when in range(101, 106):
            self.reading(when)
        self.assertEqual(self.acquisitions, [])
        self.reading(106, field=.00015)
        self.reading(107, field=-.00015)
        self.assertEqual(self.acquisitions, [])
        for when in range(110, 117):
            self.reading(when, field=0.)
        self.assertEqual(len(self.acquisitions), 1)
        self.assertGreaterEqual(self.now, 113.)

    def test_requires_pause_ack_and_post_acquisition_readbacks(self):
        c = self.controller
        c._move_step()
        self.reading(101)
        c._on_safe_move(self.move_result())
        self.assertEqual(self.magnet.pauses, 1)
        self.reading(102)
        self.assertEqual(self.acquisitions, [])
        c._on_magnet_operation('pause')
        self.settle(103)
        self.assertEqual(len(self.acquisitions), 1)
        self.assertEqual(c._results, [])
        self.assertEqual(c._measurement_phase, 'step_verifying')
        self.reading(110)
        self.assertEqual(len(c._results), 1)
        self.assertEqual(c._results[0]['Temperature_target_K'], 4.)
        self.assertEqual(c._results[0]['Field_point_index'], 1)
        self.assertEqual(self.magnet.moves[-1][0], .05)

    def test_initial_and_point_positioning_use_configured_tolerance(self):
        c = self.controller
        self.params.stop_field_t = .002
        c._reset_step_condition()
        c._position_at_start()
        self.assertEqual(self.magnet.moves[-1][1]['tolerance_t'], .0002)
        c._move_step()
        self.assertEqual(self.magnet.moves[-1][1]['tolerance_t'], .0002)
        c._step_index = 1
        c._move_step()
        self.assertEqual(self.magnet.moves[-1][1]['tolerance_t'], .0002)

    def test_stationary_in_tolerance_error_can_qualify(self):
        self.pause_at_point()
        for when in range(101, 108):
            self.reading(when, field=.0001)
        self.assertEqual(len(self.acquisitions), 1)
        self.reading(108, field=.0001)
        self.assertEqual(len(self.controller._results), 1)

    def test_cancelled_initial_preparation_reuses_direction(self):
        c = self.controller
        c._step_move_inflight = False
        c._measurement_phase = "positioning"
        c._thermal_hold = True
        c._on_safe_move({"success": False, "cancelled": True, "cancellation_reason": "thermal",
                         "monotonic_direction": -1})
        c._position_at_start()
        self.assertEqual(self.magnet.moves[-1][1]["monotonic_direction"], -1)

    def test_initial_success_retains_preparation_direction_for_first_point(self):
        c = self.controller
        c._step_move_inflight = False
        c._measurement_phase = "positioning"
        c._io_lane = object()
        c._start_condition = lambda: None
        c._on_safe_move(self.move_result(initial=True, direction=-1))
        self.assertEqual(c._initial_step_direction, -1)
        c._io_lane = None
        c._move_step()
        self.assertEqual(self.magnet.moves[-1][1]["monotonic_direction"], -1)

    def test_persistent_row_transition_reinfers_new_condition_direction(self):
        from app.models import BFieldTransportCondition
        for round_trip in (False, True):
            with self.subTest(round_trip=round_trip):
                self.params.round_trip = round_trip
                self.params.conditions = [BFieldTransportCondition(name="first"),
                                          BFieldTransportCondition(name="second")]
                c = self.controller
                c.plan = TransportSweepPlan.from_params(self.params)
                c.plan.validation["aps100_rates"] = {"normal": {i: (10. * (i + 1), .1) for i in range(5)}, "coil_constant_t_per_a": .2}
                c._condition_index = 0
                c._thermal_hold = True
                c._thermal_pause_pending = False
                c._persistent_row_transition = True
                c._persistent_row_entered = True
                c._row_persistent_ready_at = None
                c._initial_direction_locked = True
                c._initial_step_direction = 1
                self.magnet.moves.clear()
                c._resume_after_thermal_recovery(manual=True)
                self.assertEqual(c._condition_index, 1)
                self.assertEqual(self.magnet.moves[-1][1]["monotonic_direction"], 0)
                self.assertFalse(c._initial_direction_locked)

    def test_field_excursion_during_pending_acquisition_does_not_move(self):
        c = self.controller
        queued = []
        c._io_lane = NS(submit=lambda work, callback: queued.append((work, callback)))
        self.pause_at_point()
        self.settle()
        self.reading(108, field=-.0122)
        self.assertTrue(c._cleanup_in_progress)
        self.assertEqual(len(self.magnet.moves), 1)
        self.assertEqual(c._sample_pending, 1)

    def test_faulted_or_stale_field_never_triggers_reposition(self):
        c = self.controller
        self.pause_at_point()
        for when in (101, 102, 103, 104):
            self.now = float(when)
            snap = NS(monotonic_s=self.now, field_t=.0122, heater_on=True,
                      status=NS(sweep_active=False, quench=True))
            self.assertFalse(c._step_correct_field(snap, self.now))
            snap.status.quench = False
            snap.monotonic_s -= 4.
            self.assertFalse(c._step_correct_field(snap, self.now))
        self.assertEqual(len(self.magnet.moves), 1)

    def test_ready_temperature_at_deadline_does_not_start_acquisition(self):
        self.params.temperature_timeout_s = 7.
        self.pause_at_point()
        for when in range(101, 108):
            self.reading(when, temperature=4.2)
        self.reading(108)
        self.assertTrue(self.controller._cleanup_in_progress)
        self.assertEqual(self.acquisitions, [])

    def test_out_of_range_during_acquisition_records_quality_failure(self):
        c = self.controller
        queued = []
        c._io_lane = NS(submit=lambda work, callback: queued.append((work, callback)))
        self.pause_at_point()
        self.settle()
        c._log = Mock()
        self.reading(108, field=-.0122)
        self.assertTrue(c._cleanup_in_progress)
        self.assertTrue(any("quality_failed" in args[0] for args, _ in c._log.call_args_list))
        self.assertEqual(c._results, [])

    def test_wrong_temperature_times_out_and_aborts(self):
        c = self.controller
        self.pause_at_point()
        for when in range(101, 115):
            self.reading(when, temperature=4.2)
        self.assertTrue(c._cleanup_in_progress)
        self.assertEqual(self.acquisitions, [])
        self.assertEqual(len(self.magnet.moves), 1)

    def test_missing_motion_history_fails_closed(self):
        c = self.controller
        c._move_step()
        c._on_safe_move({"success": True})
        self.assertTrue(c._cleanup_in_progress)
        self.assertEqual(self.acquisitions, [])

    def test_moving_or_out_of_band_field_cannot_qualify(self):
        self.pause_at_point()
        for when in range(101, 108):
            self.reading(when, active=True)
        self.assertEqual(self.acquisitions, [])
        self.reading(108, field=.011)
        self.assertTrue(self.controller._cleanup_in_progress)
        self.assertEqual(self.acquisitions, [])

    def test_excursion_during_acquisition_discards_point(self):
        c = self.controller
        self.pause_at_point()
        self.settle()
        self.assertEqual(c._measurement_phase, 'step_verifying')
        self.reading(108, temperature=4.2)
        self.assertEqual(c._results, [])
        self.assertEqual(c._step_index, 0)
        self.assertEqual(c._measurement_phase, 'step_settling')

    def test_window_gap_and_cooling_drift_do_not_qualify(self):
        window = TemperatureWindow(self.params, 3.)
        for stamp in range(5):
            self.assertFalse(window.observe(stamp, 4.02 - stamp * .005, stamp))
        self.assertFalse(window.observe(20., 4., 20.))
        self.assertEqual(len(window.samples), 1)
        for unused in range(10):
            self.assertFalse(window.observe(20., 4., 20.))
        self.assertEqual(len(window.samples), 1)

    def test_long_safe_move_uses_worker_timeout_not_stationary_read_age(self):
        c = self.controller
        c._move_step()
        c._last_telemetry_monotonic = 90.
        self.now = 120.
        c._on_telemetry_watchdog()
        self.assertIsNone(c._monitor_hold)
        self.assertEqual(c._measurement_phase, 'step_moving')

    def test_thermal_cancel_preserves_current_point_on_resume(self):
        c = self.controller
        c._step_index = 1
        c._move_step()
        c._request_thermal_pause()
        c._on_safe_move({'cancelled': True, 'cancellation_reason': 'thermal'})
        self.assertEqual(c._thermal_resume_action, 'step')
        c._on_magnet_operation('pause')
        c.continue_after_temperature_check()
        self.assertEqual(c._step_index, 1)
        self.assertEqual(self.magnet.moves[-1][0], .05)

    def test_stop_rejects_delayed_acquisition(self):
        c = self.controller
        self.pause_at_point()
        self.settle()
        row = c._step_pending_row
        c._cleanup_in_progress = True
        c._step_sample_ready(row)
        self.assertEqual(c._results, [])

    def test_stepped_acquisition_is_not_throttled_by_continuous_delay(self):
        self.params.acquisition_delay_s = 10000.
        self.pause_at_point()
        self.settle()
        self.assertEqual(len(self.acquisitions), 1)

    def test_async_acquisition_waits_and_discards_interrupted_generation(self):
        c = self.controller
        queued = []
        c._io_lane = NS(submit=lambda work, callback: queued.append((work, callback)))
        self.pause_at_point()
        self.settle()
        self.assertEqual(c._sample_pending, 1)
        self.reading(108)
        self.assertEqual(len(queued), 1)
        work, callback = queued.pop()
        row = work()
        c._request_thermal_pause()
        c._on_magnet_operation('pause')
        c.continue_after_temperature_check()
        self.assertEqual(c._measurement_phase, 'step_retry')
        callback(row, None)
        c._on_telemetry_watchdog()
        self.assertEqual(c._sample_pending, 0)
        self.assertEqual(c._results, [])
        self.assertEqual(c._step_index, 0)
        self.assertEqual(c._measurement_phase, 'step_moving')

    def test_round_trip_multiple_conditions_visit_each_point_once(self):
        c = self.controller
        self.params.round_trip = True
        self.params.conditions.append(deepcopy(self.params.conditions[0]))
        c.plan = TransportSweepPlan.from_params(self.params)
        c.plan.validation["aps100_rates"] = {"normal": {i: (10. * (i + 1), .1) for i in range(5)}, "coil_constant_t_per_a": .2}
        c._reset_step_condition()
        c._apply_biases = lambda condition: None
        completed = []
        def cleanup(status, detail=''):
            completed.append(status)
            c._active = False
        c._cleanup = cleanup
        c._move_step()
        for index in range(10):
            c._on_safe_move(self.move_result())
            c._on_magnet_operation('pause')
            start = int(self.now) + 1
            self.settle(start)
            self.reading(start + 7)
        self.assertEqual(completed, ['finished'])
        self.assertEqual([row['B_target_T'] for row in c._results], [0., .05, .1, .05, 0.] * 2)
        self.assertEqual([row['Condition_index'] for row in c._results], [1] * 5 + [2] * 5)
        self.assertEqual([row['Direction'] for row in c._results[:5]], ['forward'] * 3 + ['backward'] * 2)

    def test_cached_or_faulty_temperature_cannot_qualify(self):
        c = self.controller
        self.pause_at_point()
        self.reading(101)
        for when in range(102, 109):
            self.now = float(when)
            c._step_tick()
        self.assertEqual(self.acquisitions, [])
        self.assertFalse(c._step_window.samples)
        for when in range(109, 112):
            self.reading(when)
            self.thermal.latest_snapshot.sample_sensor_status = '16'
            c._step_tick()
        self.assertEqual(self.acquisitions, [])

    def test_sensor_decimal_zero_format_is_accepted(self):
        self.pause_at_point()
        for when in range(101, 108):
            self.reading(when)
            self.thermal.latest_snapshot.sample_sensor_status = ' 0.000 '
            self.controller._step_tick()
        self.assertEqual(len(self.acquisitions), 1)

    def test_nonrepresentable_field_targets_are_rejected(self):
        self.params.field_points = 4
        with self.assertRaisesRegex(ValueError, '0.0001 T resolution'):
            TransportSweepPlan.from_params(self.params)

    def test_window_rejects_oscillation_even_with_zero_net_drift(self):
        window = TemperatureWindow(self.params, 3.)
        for stamp, value in enumerate((4., 4.04, 4., 3.96, 4.)):
            self.assertFalse(window.observe(float(stamp), value, float(stamp)))


if __name__ == '__main__':
    unittest.main()
