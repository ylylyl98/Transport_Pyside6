import unittest
from unittest.mock import Mock
from PySide6 import QtCore
from tests import test_stepped_transport as base


class Lake(QtCore.QObject):
    control_result = QtCore.Signal(object)

    def __init__(self, clock):
        super().__init__()
        self.clock = clock
        self.input_configurations = {'A': {'current_reversal': True}, 'B': {'current_reversal': True}}
        self.requests = []
        self.ended = False

    @property
    def input_configurations_monotonic_s(self):
        return self.clock()

    def begin_measurement_input_session(self, owner):
        return self.input_configurations

    def set_measurement_sample_reversal(self, owner, enabled, request_id):
        self.requests.append((enabled, request_id))
        return True

    def refresh_snapshot(self):
        pass

    def end_measurement_input_session(self, owner):
        self.ended = True

    def ack(self, ok=True, request=None):
        enabled, ident = request or self.requests[-1]
        if ok:
            self.input_configurations['B']['current_reversal'] = enabled
        self.control_result.emit(dict(operation='measurement_current_reversal', request_id=ident,
            ok=ok, inputs=self.input_configurations, monotonic_s=self.clock(), error='simulated'))


class ReversalWorkflowTests(unittest.TestCase):
    setUpClass = classmethod(base.SteppedTransportTests.setUpClass.__func__)
    reading = base.SteppedTransportTests.reading
    move_result = base.SteppedTransportTests.move_result
    pause_at_point = base.SteppedTransportTests.pause_at_point

    def setUp(self):
        base.SteppedTransportTests.setUp(self)
        self.params.sample_reversal_mode = 'per_point'
        self.params.sample_reversal_settle_s = 2.
        c = self.controller
        self.lake = Lake(lambda: self.now)
        c.lakeshore_controller = self.lake
        c._position_at_start = Mock()
        self.reading(100., field=0.)
        c._reversal_start()
        c._reversal_tick()
        self.lake.ack()
        self.reading(103., field=0.)
        self.assertTrue(c._position_at_start.called)
        self.pause_at_point()

    def qualify_off(self):
        for when in range(104, 110):
            self.reading(when)
        self.assertEqual(self.controller._measurement_phase, 'step_input_switch')
        self.reading(110.)
        self.assertFalse(self.lake.requests[-1][0])

    def acquire_off(self):
        self.qualify_off()
        self.lake.ack()
        self.reading(111.)
        self.assertEqual(len(self.acquisitions), 0)
        self.reading(112.1)
        self.assertEqual(len(self.acquisitions), 1)
        self.assertEqual(self.controller._results, [])

    def test_point_requires_off_ack_wait_and_fresh_on_postcheck(self):
        self.acquire_off()
        self.reading(113.)
        self.reading(114.)
        self.assertTrue(self.lake.requests[-1][0])
        self.lake.ack()
        for when in range(115, 124):
            self.reading(when)
        c = self.controller
        self.assertEqual(len(c._results), 1)
        row = c._results[0]
        self.assertEqual(row['Sample_reversal_mode'], 'per_point')
        self.assertEqual(row['Sample_ON_before_K'], 4.)
        self.assertEqual(row['Sample_ON_after_K'], 4.)
        self.assertIn('accuracy_unverified', row['Sample_temperature_quality'])
        self.assertTrue(self.lake.requests[-1][0])

    def test_no_ack_never_acquires_and_times_out(self):
        self.qualify_off()
        self.reading(111.)
        self.reading(142.)
        self.assertEqual(self.acquisitions, [])
        self.assertTrue(self.controller._cleanup_in_progress)

    def test_failed_off_never_acquires(self):
        self.qualify_off()
        self.lake.ack(False)
        self.assertEqual(self.acquisitions, [])
        self.assertTrue(self.controller._cleanup_in_progress)

    def test_field_excursion_while_switching_rejects_point(self):
        self.qualify_off()
        self.reading(111., field=.03)
        self.assertEqual(self.acquisitions, [])
        self.assertTrue(self.controller._cleanup_in_progress)

    def test_off_temperature_not_mixed_into_on_stability_window(self):
        self.qualify_off()
        self.lake.ack()
        self.reading(111., temperature=4.2)
        self.reading(112.1, temperature=4.2)
        self.assertEqual(len(self.acquisitions), 1)
        self.assertEqual(self.controller._results, [])

    def test_postcheck_temperature_difference_rejects_acquired_point(self):
        self.acquire_off()
        self.reading(113.)
        self.reading(114.)
        self.lake.ack()
        for when in range(115, 124):
            self.reading(when, temperature=4.03)
        self.assertEqual(self.controller._results, [])
        self.assertTrue(self.controller._cleanup_in_progress)

    def test_stop_drains_late_off_before_restore_without_acquisition(self):
        self.qualify_off()
        c = self.controller
        c.stop()
        self.lake.ack()
        self.reading(112.1)
        c._reversal_cleanup()
        self.assertEqual(c._reversal_action[1], 'restore')
        c._reversal_tick()
        self.assertTrue(self.lake.requests[-1][0])
        self.assertEqual(self.acquisitions, [])
        self.lake.ack()
        c._finish_cleanup = Mock()
        self.reading(115.)
        c._reversal_tick()
        self.assertTrue(self.lake.ended)
        self.assertTrue(c._finish_cleanup.called)

    def test_whole_sweep_off_still_requires_temperature_stability(self):
        c = self.controller
        self.params.temperature_timeout_s = 30.
        self.params.sample_reversal_mode = 'whole_sweep'
        c._reversal_confirmed = False
        c._reversal_off_stamp = 100.
        for when in range(104, 112):
            self.reading(when, temperature=4.03 if when % 2 else 3.97)
        self.assertEqual(self.acquisitions, [])
        for when in range(112, 120):
            self.reading(when)
        self.assertEqual(len(self.acquisitions), 1)
        self.assertEqual(c._results[0]['Sample_temperature_quality'], 'DC_stable_accuracy_unverified')

    def test_wrong_request_ack_does_not_unlock_acquisition(self):
        self.qualify_off()
        self.lake.ack(request=(False, 'other-run'))
        self.reading(113.)
        self.assertEqual(self.acquisitions, [])

    def test_field_window_excursion_inside_target_tolerance_rejects(self):
        self.qualify_off()
        self.params.field_span_t = .00002
        self.reading(111., field=.0001)
        self.assertEqual(self.acquisitions, [])
        self.assertTrue(self.controller._cleanup_in_progress)

    def test_a_changed_on_front_panel_aborts(self):
        self.lake.input_configurations['A']['current_reversal'] = False
        self.lake.control_result.emit(dict(operation='read_input_configurations', ok=True,
                                           inputs=self.lake.input_configurations))
        self.assertTrue(self.controller._cleanup_in_progress)
        self.assertEqual(self.acquisitions, [])

    def test_restore_failure_retains_session_until_stop_retry(self):
        c = self.controller
        c.stop()
        c._reversal_cleanup()
        self.lake.ack(False)
        self.assertTrue(c._reversal_session)
        self.assertFalse(self.lake.ended)
        self.assertTrue(c._reversal_restore_error)
        c.stop()
        self.lake.ack()
        c._finish_cleanup = Mock()
        self.reading(106.)
        c._reversal_tick()
        self.assertTrue(self.lake.ended)

    def test_long_magnet_cleanup_does_not_expire_final_restore(self):
        self.qualify_off()
        c = self.controller
        c.stop()
        self.lake.ack()
        self.reading(500.)
        c._reversal_cleanup()
        self.assertEqual(c._reversal_action, (True, 'restore'))
        c._reversal_tick()
        self.assertTrue(self.lake.requests[-1][0])
        self.assertIsNone(c._reversal_restore_error)

    def test_stop_before_off_dispatched_never_sends_late_off(self):
        for when in range(104,110):
            self.reading(when)
            if self.controller._reversal_action == (False, 'acquire'):
                break
        c = self.controller
        self.assertEqual(c._reversal_action, (False, 'acquire'))
        self.assertIsNone(c._reversal_pending)
        c.stop()
        c._reversal_cleanup()
        c._reversal_tick()
        self.assertTrue(all(enabled for enabled,_ in self.lake.requests))

    def test_whole_sweep_start_off_commit_and_restore_original(self):
        c = self.controller
        self.lake.control_result.disconnect(c._reversal_result)
        self.lake.end_measurement_input_session(c._owner)
        self.lake.ended = False
        c._reversal_session = False
        self.params.sample_reversal_mode = 'whole_sweep'
        c._reversal_start()
        c._reversal_tick()
        self.assertFalse(self.lake.requests[-1][0])
        self.lake.ack()
        self.reading(106.)
        self.pause_at_point()
        for when in range(107,114):
            self.reading(when)
        self.assertEqual(len(self.acquisitions),1)
        self.assertEqual(c._results[0]['Sample_reversal_during_acquisition'], 'OFF')
        self.assertEqual([enabled for enabled,_ in self.lake.requests], [True,False])
        c.stop()
        c._reversal_cleanup()
        self.assertTrue(self.lake.requests[-1][0])
        self.lake.ack()
        c._finish_cleanup = Mock()
        self.reading(116.)
        c._reversal_tick()
        self.assertTrue(self.lake.ended)


if __name__ == '__main__':
    unittest.main()
