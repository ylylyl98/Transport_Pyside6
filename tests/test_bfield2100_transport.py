import csv
import json
import tempfile
import unittest
from concurrent.futures import Future
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import patch

from PySide6 import QtCore
from app.engine.bfield2100_transport_controller import BField2100TransportController
from app.engine.bfield_transport_sweep import TransportSweepPlan
from app.models import BFieldTransportParams, SaveRoot
from utils.config import AttoDRY2100Config


class Handle:
    def __init__(self, value=None, error=None, drained=True):
        self.future, self.drain = Future(), Future()
        if error:
            self.future.set_exception(error)
        else:
            self.future.set_result(value)
        if drained:
            self.drain.set_result(None)

    def wait_drained(self, timeout):
        return self.drain.result(timeout)


class SDK(QtCore.QObject):
    external_stop_requested = QtCore.Signal()
    fault = QtCore.Signal(str)
    disconnected = QtCore.Signal()

    def __init__(self, clock):
        super().__init__()
        self.clock = clock
        self.config = AttoDRY2100Config()
        self.state = NS(name='IDLE')
        self.exclusive_owner = ''
        self.has_pending_work = False
        self.target = 0.
        self.field_offset = 0.
        self.sample = self.sample_target = 4.
        self.sample_active = False
        self.field_active = False
        self.field_state = 'IDLE'
        self.magnet_temperature = 3.8
        self.quench = False
        self.calls = []
        self.stop_error = False
        self.stops_drained = True
        self.last_stop = None

    def acquire_exclusive(self, owner):
        if self.exclusive_owner:
            return False
        self.exclusive_owner = owner
        return True

    def release_exclusive(self, owner):
        if self.has_pending_work:
            return False
        self.exclusive_owner = ''
        self.calls.append(('release',))
        return True

    def set_polling_enabled(self, enabled):
        self.calls.append(('polling', enabled))

    def read_snapshot_async(self, *, owner):
        self.calls.append(('read_field',))
        return Handle(NS(monotonic_s=self.clock(), field_t=self.target + self.field_offset, setpoint_t=self.target,
            temperature_k=self.magnet_temperature, capabilities=NS(sample_temperature_control=True, sample_temperature_readback=True),
            status=NS(quench=self.quench, driven_mode=True, persistent_mode=False,
                      field_control_state=self.field_state, backend_details={'field_control': self.field_active})))

    def read_temperature_snapshot_async(self, *, owner):
        self.calls.append(('read_temperature',))
        return Handle(NS(monotonic_s=self.clock(), sample_temperature_k=self.sample,
            sample_setpoint_k=self.sample_target, sample_control_active=self.sample_active, vti_temperature_k=4.2))

    def configure_sample_temperature_async(self, target, rate, *, owner):
        self.calls.append(('temperature', target))
        self.sample_target, self.sample_active = target, True
        return self.read_temperature_snapshot_async(owner=owner)

    def set_h_setpoint_async(self, target, *, owner):
        self.calls.append(('field', target))
        self.target = target
        return Handle(target)

    def start_field_control_async(self, *, owner):
        self.calls.append(('start_field',))
        self.field_active = True
        return Handle(True)

    def request_stop(self, *, owner):
        self.calls.append(('stop_field',))
        self.field_active = False
        self.last_stop = Handle(error=RuntimeError('stop failed') if self.stop_error else None,
                                drained=self.stops_drained)
        return self.last_stop

    def retry_stop(self, *, owner):
        return self.request_stop(owner=owner)


class ImmediateLane:
    def __init__(self, *args):
        self.failed = NS(connect=lambda callback: None)
        self.pending = 0

    def submit(self, work, done):
        try:
            value = work()
        except Exception as exc:
            done(None, exc)
        else:
            done(value, None)

    def close(self):
        pass


class Source:
    voltage = 0.
    identity = "KEITHLEY INSTRUMENTS INC.,MODEL 2400,123,1"
    cached_source_voltage_range_v = 20.
    def refresh_source_voltage_range(self): return self.cached_source_voltage_range_v
    def set_voltage(self, value): self.voltage = value
    def acquire(self): return {'current': .1}


class Manager:
    def __init__(self):
        self.sessions = {name: Source() for name in ('g1', 'g2', 'g3')}
        self.sessions['daq'] = NS(acquire=lambda: [1., 2., 3.])
        self.claimed = []
    def get_session(self, key): return self.sessions.get(key)
    def is_voltage_source_mode(self, key): return True
    def applied_gate_voltage_limit(self, key): return 20.
    def mark_in_use(self, keys):
        self.claimed = list(keys)
        return True, []
    def release(self, keys): self.claimed = []


class BField2100Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtCore.QCoreApplication.instance() or QtCore.QCoreApplication([])

    def setUp(self):
        self.now = 100.
        self.scratch = tempfile.TemporaryDirectory()
        self.addCleanup(self.scratch.cleanup)
        self.clock = patch('app.engine.bfield2100_transport_controller.time.monotonic', side_effect=lambda: self.now)
        self.clock.start()
        self.addCleanup(self.clock.stop)
        lane = patch('app.engine.bfield2100_transport_controller.TaskLane', ImmediateLane)
        lane.start()
        self.addCleanup(lane.stop)
        self.params = BFieldTransportParams(magnet_backend='2100', measurement_mode='stepped',
            start_field_t=0., stop_field_t=.1, field_spacing="points", field_points=3, round_trip=False,
            temperature_window_s=2., temperature_timeout_s=5.)
        self.params.conditions[0].settle_s = 0.
        self.sdk, self.manager = SDK(lambda: self.now), Manager()
        self.records, self.errors, self.states = [], [], []
        tab = NS(collect_params=lambda: deepcopy(self.params), save=SaveRoot(base=self.scratch.name),
            get_global_rates=lambda: (1e7, 100.), get_signal_chain=lambda: None,
            set_sweep_locked=lambda value: None, set_progress=lambda value: None,
            accept_transport_record=self.records.append)
        self.engine = BField2100TransportController(self.sdk, tab, self.manager, review_valid_callable=lambda: True)
        self.engine.error.connect(self.errors.append)
        self.engine.state_changed.connect(lambda state, detail: self.states.append(state))
        self.addCleanup(self.engine._timer.stop)

    def run_until(self, condition, maximum=200):
        for _ in range(maximum):
            if condition():
                return
            self.now += .25
            self.engine._tick()
        self.fail(f'Timed out in {self.engine._measurement_phase}: {self.errors}')

    def test_missing_or_invalid_lockin_capture_fails_before_any_mutation(self):
        chain = {"drag_drive": {"mode": "single_drive"}}
        self.engine.tab.get_signal_chain = lambda: chain
        self.assertFalse(self.engine.start())
        self.assertEqual(self.sdk.calls, [])
        self.assertEqual(self.manager.claimed, [])
        self.assertEqual(self.sdk.exclusive_owner, "")
        def reject(_chain):
            raise ValueError("invalid lock-in calibration")
        self.engine.tab.capture_run_signal_chain = reject
        self.assertFalse(self.engine.start())
        self.assertEqual(self.sdk.calls, [])
        self.assertEqual(self.manager.claimed, [])
        self.assertEqual(self.sdk.exclusive_owner, "")

    def test_unknown_fresh_range_rejected_before_field_or_voltage_output(self):
        self.manager.sessions["g1"].cached_source_voltage_range_v = 3.
        self.assertFalse(self.engine.start())
        self.assertTrue(self.errors)
        self.assertEqual(self.sdk.calls, [('release',)])
        self.assertEqual(self.manager.claimed, [])
        self.assertTrue(all(self.manager.sessions[name].voltage == 0. for name in ("g1", "g2", "g3")))

    def test_active_temperature_and_each_target_are_verified_and_saved(self):
        self.assertTrue(self.engine.start())
        self.run_until(lambda: Path(self.engine._checkpoint).exists())
        paths = self.engine._transport_outputs
        for path in (paths.manifest_path, paths.checkpoint_path):
            saved = json.loads(Path(path).read_text(encoding="utf-8"))
            self.assertEqual(saved["measurement"], "bfield_transport")
            self.assertEqual(saved["csv_paths"], list(paths.condition_csv_paths))
        self.run_until(lambda: not self.engine.active)
        self.assertEqual(self.errors, [])
        self.assertEqual([row['B_target_T'] for row in self.records], [0., .05, .1])
        self.assertEqual([call for call in self.sdk.calls if call[0] == 'temperature'], [('temperature', 4.)])
        self.assertTrue(self.sdk.sample_active)
        self.assertFalse(self.sdk.field_active)
        self.assertEqual(self.sdk.exclusive_owner, '')
        self.assertEqual(self.manager.claimed, [])
        paths = self.engine._transport_outputs
        with open(paths.condition_csv_paths[0], newline='', encoding='utf-8') as handle:
            rows = list(csv.DictReader(handle))
        self.assertEqual(len(rows), 3)
        self.assertTrue(all(float(row['Ids_Keithley']) == .1 and row['Ids_Keithley'] == row['Keithley_current'] for row in rows))
        self.assertTrue(all(row['Magnet_backend'] == '2100' and row['Sweep_rate_A_per_s'] == '' for row in rows))
        metadata = json.loads(Path(paths.manifest_path).read_text(encoding='utf-8'))
        self.assertEqual(metadata['status'], 'finished')
        self.assertEqual(metadata['csv_paths'], list(paths.condition_csv_paths))
        checkpoint = json.loads(Path(paths.checkpoint_path).read_text(encoding='utf-8'))
        self.assertEqual(checkpoint['csv_paths'], list(paths.condition_csv_paths))
        self.assertNotIn('rate_t_per_min', metadata['params'])

    def test_queued_checkpoint_freezes_csv_path_mapping(self):
        self.assertTrue(self.engine.start())
        self.run_until(lambda: Path(self.engine._checkpoint).exists())
        original_outputs = self.engine._transport_outputs
        original_lane = self.engine._storage_lane
        pending = []
        self.engine._storage_lane = NS(submit=lambda work, done: pending.append(work))
        self.engine._save_checkpoint("running")
        self.engine._transport_outputs = NS(condition_csv_paths=("other-run.csv",))
        pending[0]()
        saved = json.loads(Path(original_outputs.checkpoint_path).read_text(encoding="utf-8"))
        self.assertEqual(saved["csv_paths"], list(original_outputs.condition_csv_paths))
        self.engine._transport_outputs = original_outputs
        self.engine._storage_lane = original_lane
        self.engine.stop()
        self.run_until(lambda: not self.engine.active)

    def test_timeout_hold_keeps_monitoring_and_manual_continue_retries_same_point(self):
        self.sdk.sample = 4.2
        self.engine.start()
        self.run_until(lambda: self.engine._hold and not self.engine._hold_stopping)
        reads = self.sdk.calls.count(('read_field',))
        for _ in range(12):
            self.now += .25
            self.engine._tick()
        self.assertGreater(self.sdk.calls.count(('read_field',)), reads)
        self.assertEqual(self.records, [])
        self.assertEqual(self.engine._point_index, 0)
        self.sdk.sample = 4.
        self.engine.continue_after_temperature_check()
        self.run_until(lambda: not self.engine.active)
        self.assertEqual([row['B_target_T'] for row in self.records], [0., .05, .1])

    def test_quench_during_hold_aborts_without_auto_resume(self):
        self.sdk.sample = 4.2
        self.engine.start()
        self.run_until(lambda: self.engine._hold and not self.engine._hold_stopping)
        self.sdk.quench = True
        self.run_until(lambda: not self.engine.active)
        self.assertEqual(self.records, [])
        self.assertEqual(self.engine._cleanup_status, 'failed')

    def test_magnet_temperature_not_confused_with_sample_target(self):
        self.params.temperature_target_k = 20.
        self.sdk.sample = 20.
        self.engine.start()
        self.run_until(lambda: not self.engine.active)
        self.assertEqual(self.errors, [])
        self.assertEqual(len(self.records), 3)
        self.assertTrue(all(row['Sample_temperature_K'] == 20. and row['Magnet_temperature_K'] == 3.8 for row in self.records))

    def test_stationary_field_outside_tolerance_cannot_acquire(self):
        self.sdk.field_offset = .011
        self.engine.start()
        for _ in range(60):
            self.now += .25
            self.engine._tick()
        self.assertEqual(self.records, [])
        self.assertEqual(self.engine._measurement_phase, 'positioning')
        self.sdk.field_offset = 0.
        self.run_until(lambda: not self.engine.active)
        self.assertEqual(len(self.records), 3)

    def test_temperature_excursion_after_acquisition_discards_sample(self):
        self.params.temperature_timeout_s = 30.
        self.engine.start()
        self.run_until(lambda: self.engine._measurement_phase == 'step_verifying')
        self.sdk.sample = 4.2
        self.run_until(lambda: self.engine._measurement_phase == 'step_settling')
        self.assertEqual(self.records, [])
        self.assertEqual(self.engine._point_index, 0)
        self.sdk.sample = 4.
        self.run_until(lambda: not self.engine.active)
        self.assertEqual([row['B_target_T'] for row in self.records], [0., .05, .1])

    def test_stop_ack_without_owner_drain_does_not_release(self):
        self.engine.start()
        self.run_until(lambda: self.engine._measurement_phase == 'step_settling')
        self.sdk.stops_drained = False
        self.engine.stop()
        for _ in range(8):
            self.now += .25
            self.engine._tick()
        self.assertTrue(self.engine.active)
        self.assertTrue(self.sdk.exclusive_owner)
        self.assertTrue(self.manager.claimed)
        self.sdk.last_stop.drain.set_result(None)
        self.run_until(lambda: not self.engine.active)
        self.assertTrue(self.sdk.sample_active)

    def test_failed_stop_retains_ownership_and_retry_succeeds(self):
        self.engine.start()
        self.run_until(lambda: self.engine._measurement_phase == 'step_settling')
        self.sdk.stop_error = True
        self.engine.stop()
        self.run_until(lambda: self.engine._stop_error_reported)
        self.assertTrue(self.sdk.exclusive_owner)
        self.sdk.stop_error = False
        self.engine.stop()
        self.run_until(lambda: not self.engine.active)
        self.assertEqual(self.sdk.exclusive_owner, '')

    def test_external_stop_does_not_restart_or_disable_sample_temperature(self):
        self.engine.start()
        self.run_until(lambda: self.engine._measurement_phase == 'step_settling')
        self.sdk.external_stop_requested.emit()
        self.run_until(lambda: not self.engine.active)
        self.assertEqual(self.records, [])
        self.assertTrue(self.sdk.sample_active)

    def test_sdk_plan_does_not_apply_aps_rate_or_quantization(self):
        self.params.field_points = 4
        self.params.rate_t_per_min = 100.
        plan = TransportSweepPlan.from_params(self.params)
        self.assertIsNone(plan.validation['rate_a_per_s'])
        self.params.measurement_mode = 'continuous'
        with self.assertRaisesRegex(ValueError, 'Stepped'):
            TransportSweepPlan.from_params(self.params)


if __name__ == '__main__':
    unittest.main()
