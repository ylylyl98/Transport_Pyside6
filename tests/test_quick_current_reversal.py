"""LS335 controls stay explicit and cannot race a measurement start."""
import time
import unittest
from types import SimpleNamespace as NS
from unittest.mock import Mock

from PySide6.QtCore import QCoreApplication
from app.quick.magnet import MagnetService
from app.lakeshore_input_state import current_reversal_summary, input_change_magnet_block
from tests.test_quick_magnet import Controller, SDKController


class QuickCurrentReversalTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QCoreApplication.instance() or QCoreApplication([])

    def setUp(self):
        self.aps, self.sdk, self.ls = Controller(), SDKController(), Controller()
        self.ls.set_current_reversal = Mock()
        self.ls.refresh_input_configurations = Mock()
        self.ls.input_configuration_pending = False
        self.s = MagnetService(magnet1000=self.aps, magnet2100=self.sdk, lakeshore=self.ls)
        self.s._timer.stop()
        self.addCleanup(self.s.close)
        self.s.acknowledgeReview(True)
        self.s._connected['temperature'] = True
        self.inputs = {'A': dict(supported=True, current_reversal=True),
                       'B': dict(supported=True, current_reversal=False)}
        self.publish()

    def publish(self, **extra):
        self.s._control_result(dict(operation='read_input_configurations', ok=True,
                                    inputs=self.inputs, monotonic_s=time.monotonic(), **extra))

    def test_only_explicit_action_writes_and_verified_result_updates_status(self):
        self.ls.set_current_reversal.assert_not_called()
        self.s.setCurrentReversal('A', False)
        self.ls.set_current_reversal.assert_called_once_with('A', False)
        self.assertFalse(self.s.is_ready())  # Even with temperature wait disabled.
        self.assertTrue(self.s.currentReversalChannels[0]['enabled'])  # No optimistic state.
        changed = {**self.inputs, 'A': dict(supported=True, current_reversal=False)}
        self.s._control_result(dict(operation='set_current_reversal', ok=True,
                                   inputs=changed, monotonic_s=time.monotonic()))
        self.assertFalse(self.s.currentReversalChannels[0]['enabled'])
        self.assertTrue(self.s.is_ready())
        self.assertIn('OFF (DC)', self.s.currentReversalStatus)
        self.assertFalse(self.s.experiment_context_snapshot()['ls335_inputs']['inputs']['A']['current_reversal'])

    def test_busy_offline_unsupported_or_stale_never_write(self):
        self.s.set_active_provider(lambda: True)
        self.s.setCurrentReversal('A', False)
        self.s.set_active_provider(lambda: False)
        self.s.offline = True
        self.s.setCurrentReversal('A', False)
        self.s.offline = False
        self.s.setCurrentReversal('C', False)
        self.s._input_configurations_stamp -= 100
        self.s.setCurrentReversal('A', False)
        self.ls.set_current_reversal.assert_not_called()

    def test_connected_magnet_requires_fresh_stationary_readback_but_not_zero(self):
        self.s._connected['1000'] = True
        self.s.setCurrentReversal('A', False)
        self.ls.set_current_reversal.assert_not_called()
        snap = NS(monotonic_s=time.monotonic(), field_t=3., heater_on=True,
                  sweep_state='paused', status=NS(sweep_active=False, faulted=False))
        self.s._snapshot('1000', snap)
        self.s.setCurrentReversal('A', False)
        self.ls.set_current_reversal.assert_called_once_with('A', False)
        snap.status.sweep_active = True
        self.assertTrue(input_change_magnet_block(snap, True))

    def test_failure_and_disconnect_clear_readback_and_pending(self):
        self.s.setCurrentReversal('A', False)
        self.s._control_result(dict(operation='set_current_reversal', ok=False, error='mismatch'))
        self.assertIn('unknown', self.s.currentReversalStatus)
        self.assertFalse(self.s.manualBusy)
        self.publish()
        self.s._connection('temperature', False)
        self.assertIn('disconnected', self.s.currentReversalStatus)
        self.assertTrue(all(not r['supported'] for r in self.s.currentReversalChannels))

    def test_controller_pending_blocks_all_backends_and_stale_is_not_reported_as_on(self):
        self.ls.input_configuration_pending = True
        self.s._backend = '2100'
        self.assertFalse(self.s.is_ready())
        self.assertTrue(self.s.manualBusy)
        self.assertIn('stale', current_reversal_summary(self.inputs, time.monotonic()-100))

    def test_measurement_service_rejects_start_until_input_transaction_completes(self):
        from app.quick.measurements import MeasurementService
        from app.models import SaveRoot
        from tests.test_quick_measurements import FakeManager
        manager = FakeManager()
        measurement = MeasurementService(manager, SaveRoot(), lakeshore_controller=self.ls)
        try:
            self.ls.input_configuration_pending = True
            self.assertFalse(measurement.canStart)
            measurement.start()
            self.assertFalse(measurement.active)
            self.assertEqual(manager.claimed, set())
            self.ls.input_configuration_pending = False
            self.assertTrue(measurement.canStart)
        finally:
            measurement.deleteLater()


if __name__ == '__main__':
    unittest.main()
