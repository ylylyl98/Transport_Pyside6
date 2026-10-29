import threading
import time
import unittest
from dataclasses import replace
from unittest.mock import Mock, patch

from app.sample_heating_interlock import SampleHeatingInterlock
from app.devices.aps100_attodry1000_adapter import APS100SafetyError
from app.devices.lakeshore335_adapter import MockLakeShore335Adapter
from controllers.lakeshore335_controller import _LakeShoreWorker, LakeShore335Controller
from controllers.magnet_controller import _MagnetWorker, MagnetController
from tests.heating_interlock_fixtures import safe_interlock, heater_off_state, zero_magnet_snapshot
from utils.config import cfg


class SampleHeatingInterlockTests(unittest.TestCase):
    def test_missing_magnet_and_heater_readbacks_fail_closed(self):
        gate = SampleHeatingInterlock()
        self.assertTrue(gate.heating_block_reason())
        self.assertTrue(gate.magnet_block_reason())
        with self.assertRaises(RuntimeError):
            gate.begin_sample_control()
        with self.assertRaises(RuntimeError):
            gate.begin_magnet()

    def test_only_fresh_zero_idle_switch_off_allows_temperature_commands(self):
        snapshot = zero_magnet_snapshot()
        cases = [None, replace(snapshot, field_t=1), replace(snapshot, field_t=-1),
                 replace(snapshot, output_field_t=.1), replace(snapshot, output_current_a=.1),
                 replace(snapshot, heater_on=True), replace(snapshot, heater_on=None),
                 replace(snapshot, monotonic_s=time.monotonic()-60),
                 replace(snapshot, monotonic_s=time.monotonic()+60),
                 replace(snapshot, field_t=float('nan')),
                 replace(snapshot, status=replace(snapshot.status, sweep_active=True)),
                 replace(snapshot, status=replace(snapshot.status, quench=True)),
                 replace(snapshot, status=replace(snapshot.status, standby=False)),
                 replace(snapshot, sweep_state='unknown')]
        for state in cases:
            with self.subTest(state=state):
                gate = safe_interlock()
                gate.note_magnet(state)
                self.assertTrue(gate.heating_block_reason())
        self.assertFalse(safe_interlock().heating_block_reason())

    def test_heater_range_output_zone_ramp_and_staleness_block_magnet(self):
        now = [time.monotonic()]
        gate = safe_interlock(clock=lambda: now[0])
        for change in ({'range': 1}, {'heater_output': .1}, {'heater_output': float('nan')},
                       {'control_mode': 2, 'ramp_enabled': True}, {'channel': 'wrong'},
                       {'control_mode': 3}):
            with self.subTest(change=change):
                state = heater_off_state()
                state.update(change)
                gate.note_control(state)
                self.assertTrue(gate.magnet_block_reason())
        gate.note_control(heater_off_state())
        self.assertFalse(gate.magnet_block_reason())
        now[0] += 60
        self.assertTrue(gate.magnet_block_reason())

    def test_atomic_reservations_prevent_simultaneous_starts(self):
        gate = safe_interlock()
        barrier = threading.Barrier(3)
        outcomes = []
        def attempt(name, begin):
            barrier.wait()
            try:
                begin()
                outcomes.append(name)
            except RuntimeError:
                pass
        threads = [threading.Thread(target=attempt, args=('sample', gate.begin_sample_control)),
                   threading.Thread(target=attempt, args=('magnet', gate.begin_magnet))]
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join(2)
            self.assertFalse(thread.is_alive())
        self.assertEqual(len(outcomes), 1)
        if outcomes == ['sample']:
            gate.end_sample_control()
        else:
            gate.end_magnet()

    def test_release_requires_new_magnet_readback(self):
        gate = safe_interlock()
        gate.begin_magnet()
        gate.note_magnet(zero_magnet_snapshot())
        self.assertTrue(gate.heating_block_reason())
        gate.end_magnet()
        self.assertTrue(gate.heating_block_reason())
        gate.note_magnet(zero_magnet_snapshot())
        self.assertFalse(gate.heating_block_reason())
        gate.reserve_magnet(True)
        gate.note_magnet(zero_magnet_snapshot())
        self.assertTrue(gate.heating_block_reason())
        gate.reserve_magnet(False)
        self.assertTrue(gate.heating_block_reason())


class WorkerInterlockTests(unittest.TestCase):
    def setUp(self):
        self.gate = safe_interlock()
        self.ls = _LakeShoreWorker(self.gate)
        self.ls.adapter = MockLakeShore335Adapter(sample_temperature_k=4.2, reservoir_temperature_k=3.)
        self.ls.adapter.connect()
        self.addCleanup(self.ls.disconnect_instrument)
        self.results = []
        self.ls.control_result.connect(self.results.append)

    def test_both_temperature_apis_check_at_execution_before_any_writes(self):
        self.gate.invalidate_magnet()
        self.ls.set_sample_control({'temperature_k': 10, 'heater_range': 1})
        self.ls.set_sample_setpoint(10)
        self.assertEqual(self.ls.adapter.control_commands, [])
        self.assertEqual(len(self.results), 2)
        self.assertTrue(all(not item['ok'] for item in self.results))

    def test_safe_heating_blocks_all_magnet_start_paths(self):
        self.ls.set_sample_control({'temperature_k': 10, 'heater_range': 1})
        self.assertTrue(self.results[-1]['ok'])
        worker = _MagnetWorker(self.gate)
        worker.adapter = Mock()
        worker.adapter.read_snapshot.return_value = zero_magnet_snapshot()
        outcomes, finished = [], []
        worker.safe_move_result.connect(outcomes.append)
        worker.transport_sweep_result.connect(outcomes.append)
        worker.operation_finished.connect(finished.append)
        worker.enter_driven_mode()
        worker.safe_move_to_field('test', 1., 'driven', False, True, 0., 1.)
        worker.start_transport_sweep(1.)
        self.assertIn('failed:enter_driven_mode', finished)
        self.assertTrue(all(not item['success'] for item in outcomes))
        worker.adapter.enter_driven_mode.assert_not_called()
        worker.adapter.safe_move_to_field.assert_not_called()
        worker.adapter.start_sweep_to.assert_not_called()
        self.ls.heater_off()
        self.assertFalse(self.gate.magnet_block_reason())
        worker.start_transport_sweep(1.)
        worker.adapter.start_sweep_to.assert_called_once_with(1.)

    def test_external_field_or_stale_monitor_turns_existing_sample_heat_off(self):
        for state in (None, replace(zero_magnet_snapshot(), field_t=1.)):
            with self.subTest(state=state):
                self.gate.note_magnet(zero_magnet_snapshot())
                self.ls.set_sample_control({'temperature_k': 10, 'heater_range': 1})
                self.gate.note_magnet(state)
                self.ls.refresh_control_configuration()
                self.assertEqual(self.ls.adapter.control_commands[-1], 'RANGE 1,0')
                self.assertFalse(self.gate.magnet_block_reason())
                self.gate.note_magnet(zero_magnet_snapshot())
                self.ls.refresh_control_configuration()
                self.assertEqual(self.ls.adapter.control_commands[-1], 'RANGE 1,0')

    def test_off_failure_does_not_unlock_magnet_and_off_is_never_gated(self):
        self.gate.begin_magnet(protective=True)
        with patch.object(self.ls.adapter, 'heater_off', side_effect=RuntimeError('no acknowledgement')) as off:
            self.ls.heater_off()
            off.assert_called_once()
        self.assertFalse(self.results[-1]['ok'])
        self.assertTrue(self.gate.magnet_block_reason())
        self.ls.heater_off()
        self.assertTrue(self.results[-1]['ok'])
        self.assertFalse(self.gate.magnet_block_reason())
        self.gate.end_magnet()

    def test_pause_and_persistent_cleanup_remain_available_with_heater_unknown(self):
        worker = _MagnetWorker()
        worker.adapter = Mock()
        worker.adapter.get_status.return_value.sweep_active = True
        worker.adapter.read_snapshot.return_value = zero_magnet_snapshot()
        worker.pause()
        worker.enter_persistent_mode(True)
        worker.adapter.pause.assert_called_once()
        worker.adapter.enter_persistent_mode.assert_called_once()

    def test_aps_read_failure_immediately_revokes_temperature_permission(self):
        worker = _MagnetWorker(self.gate)
        worker.adapter = Mock()
        worker.adapter.read_snapshot.side_effect = RuntimeError('communication failed')
        worker.refresh_snapshot()
        self.assertTrue(self.gate.heating_block_reason())
        self.ls.set_sample_setpoint(10)
        self.assertEqual(self.ls.adapter.control_commands, [])

    def test_independent_controllers_default_to_closed_and_shared_pair_is_bound(self):
        magnet = MagnetController(interlock=self.gate)
        ls = LakeShore335Controller(interlock=self.gate)
        try:
            self.assertIs(magnet._worker.heating_interlock, ls._worker.heating_interlock)
        finally:
            magnet.shutdown()
            ls.shutdown()

    def test_standalone_worker_does_not_skip_live_heater_interlock(self):
        worker = _MagnetWorker()
        worker.adapter = Mock()
        worker.adapter.get_status.return_value.sweep_active = True
        with self.assertRaisesRegex(APS100SafetyError, 'heater OFF readback'):
            worker._check_thermal()
        worker.adapter.pause.assert_called_once()

    def test_quick_service_owns_a_shared_interlock_without_connecting_devices(self):
        from app.quick.magnet import MagnetService
        service = MagnetService(offline=True)
        try:
            self.assertIs(service.heating_interlock, service.magnet1000.heating_interlock)
            self.assertIs(service.heating_interlock, service.lakeshore335.heating_interlock)
            self.assertTrue(service.heatingBlockReason)
            service.heating_interlock.note_magnet(zero_magnet_snapshot())
            self.assertFalse(service.heatingBlockReason)
            service.selectBackend('2100')
            service.heating_interlock.invalidate_magnet()
            self.assertFalse(service.heatingBlockReason)
        finally:
            service.close()


if __name__ == '__main__':
    unittest.main()
