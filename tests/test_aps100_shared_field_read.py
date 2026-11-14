"""Fresh shared APS100 thermal/history field observations during active sweeps."""
import time
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from PySide6.QtWidgets import QApplication
from app.devices.aps100_attodry1000_adapter import APS100SafetyError
from app.thermal_safety import ThermalSafetyEvaluator
from controllers.magnet_controller import _MagnetWorker
from tests.heating_interlock_fixtures import safe_interlock
from tests.test_aps100_polling_comparison import PollingAdapter, move
from utils.config import LakeShore335Config


class SharedFieldTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_worker_returns_the_exact_thermal_observation(self):
        worker = _MagnetWorker(safe_interlock())
        source = ThermalSafetyEvaluator(LakeShore335Config())
        source.latest_snapshot = SimpleNamespace(monotonic_s=time.monotonic(), connected=True,
            communication_valid=True, reservoir_temperature_k=3.5, sample_temperature_k=3.5,
            sample_sensor_status='0', reservoir_sensor_status='0')
        worker.thermal_source = source
        worker.adapter = Mock()
        worker.adapter.get_field_t.return_value = .4
        seen = []
        worker.thermal_field_updated.connect(seen.append)
        observation = worker._check_thermal(SimpleNamespace(sweep_active=True))
        self.assertEqual(observation.field_t, .4)
        self.assertIs(observation, seen[-1])
        worker.adapter.get_field_t.assert_called_once_with()
        worker.thermal_source = None
        self.assertIsNone(worker._check_thermal(SimpleNamespace(sweep_active=False)))

    def test_profiles_share_one_fresh_field_read_per_active_iteration(self):
        for approach in ('linear_no_backward', 'no_overshoot'):
            for profile in ('standard', 'reduced_mode_queries'):
                with self.subTest(profile=profile, approach=approach):
                    adapter = PollingAdapter()
                    observations = []
                    active_reads = []
                    read = adapter.get_field_t
                    def counted_read():
                        if adapter.active:
                            active_reads.append(1)
                        return read()
                    adapter.get_field_t = counted_read
                    def guard(status):
                        value = adapter.get_field_t()
                        observation = SimpleNamespace(field_t=value, monotonic_s=adapter.clock)
                        if adapter.active:
                            observations.append(value)
                        return observation
                    adapter.thermal_guard = guard
                    adapter.thermal_field_guard = guard
                    history = move(adapter, profile, approach=approach)
                    diag = history['polling_diagnostics']
                    self.assertEqual(diag['shared_field_reads'], diag['iterations'])
                    self.assertEqual(diag['status_checks'], diag['iterations'])
                    self.assertEqual(len(observations), diag['iterations'])
                    self.assertEqual(len(active_reads), diag['iterations'])
                    self.assertGreater(diag['iterations'], 1)
                    self.assertLessEqual(history['min_t'], min(observations))
                    self.assertGreaterEqual(history['max_t'], max(observations))
                    if approach == 'no_overshoot':
                        self.assertEqual(len(history['stages']), 2)
                    if profile == 'standard':
                        self.assertEqual(diag['mode_checks'], diag['iterations'])
                    else:
                        self.assertLess(diag['mode_checks'], diag['iterations'])

    def test_real_adapter_connection_binds_shared_guard(self):
        from app.devices.aps100_attodry1000_adapter import APS100AttoDry1000Adapter
        class ConnectedAdapter(APS100AttoDry1000Adapter):
            def connect(self):
                return 'fake identity'
            def close(self, **kwargs):
                pass
        worker = _MagnetWorker(safe_interlock())
        worker.refresh_snapshot = Mock()
        worker.refresh_rates = Mock()
        with patch('controllers.magnet_controller.APS100AttoDry1000Adapter', ConnectedAdapter):
            worker.connect_instrument('fake', False)
        self.assertIsInstance(worker.adapter, ConnectedAdapter)
        self.assertEqual(worker.adapter.thermal_field_guard, worker._check_thermal)
        self.assertEqual(worker.adapter.thermal_guard, worker._check_thermal)
        worker.disconnect_instrument()

    def test_invalid_observations_fail_closed_and_pause(self):
        for field, offset in ((float('nan'), 0), (True, 0), (.01, -4), (.01, 1), (.01, float('nan'))):
            with self.subTest(field=field, offset=offset):
                adapter = PollingAdapter()
                adapter.thermal_field_guard = lambda status: SimpleNamespace(field_t=field, monotonic_s=adapter.clock + offset)
                with self.assertRaises(APS100SafetyError):
                    move(adapter, 'standard')
                self.assertIn('pause', adapter.log)

    def test_none_falls_back_and_old_guard_remains_supported(self):
        for opt_in in (False, True):
            with self.subTest(opt_in=opt_in):
                adapter = PollingAdapter()
                if opt_in:
                    adapter.thermal_field_guard = lambda status: None
                history = move(adapter, 'reduced_mode_queries')
                diag = history['polling_diagnostics']
                self.assertEqual(diag['shared_field_reads'], 0)
                self.assertGreaterEqual(adapter.log.count('field'), diag['iterations'])

    def test_real_worker_and_adapter_share_and_stop_on_thermal_or_read_failure(self):
        for failure in (None, 'thermal', 'read'):
            with self.subTest(failure=failure):
                adapter = PollingAdapter()
                worker = _MagnetWorker(safe_interlock())
                worker.adapter = adapter
                class Source:
                    config = LakeShore335Config()
                    @property
                    def latest_snapshot(self):
                        return SimpleNamespace(monotonic_s=adapter.clock, connected=True,
                            communication_valid=True,
                            reservoir_temperature_k=6. if failure == 'thermal' and adapter.active else 3.5,
                            sample_temperature_k=3.5, sample_sensor_status='0', reservoir_sensor_status='0')
                worker.thermal_source = Source()
                adapter.thermal_guard = worker._check_thermal
                adapter.thermal_field_guard = worker._check_thermal
                get_field = adapter.get_field_t
                failures = []
                def read_field():
                    if failure == 'read' and adapter.active:
                        failures.append(1)
                        raise OSError('read failed')
                    return get_field()
                adapter.get_field_t = read_field
                with patch('app.thermal_safety.ThermalSafetyEvaluator',
                           side_effect=lambda config: ThermalSafetyEvaluator(config, clock=lambda: adapter.clock)):
                    if failure:
                        with self.assertRaises(APS100SafetyError):
                            move(adapter, 'standard')
                        self.assertIn('pause', adapter.log)
                        if failure == 'read':
                            self.assertEqual(len(failures), 1)
                    else:
                        history = move(adapter, 'standard')
                        self.assertEqual(history['polling_diagnostics']['shared_field_reads'],
                                         history['polling_diagnostics']['iterations'])

    def test_shared_observation_records_reversal_and_continues(self):
        adapter = PollingAdapter()
        calls = []
        def guard(status):
            value = adapter.get_field_t()
            calls.append(value)
            if len(calls) == 3:
                value -= .005
            return SimpleNamespace(field_t=value, monotonic_s=adapter.clock)
        adapter.thermal_field_guard = guard
        history = move(adapter, 'reduced_mode_queries')
        self.assertTrue(history['complete'])
        self.assertTrue(history['reversal_exceeded'])
        self.assertGreater(history['max_reverse_t'], .0002)
        self.assertIn('pause', adapter.log)

    def test_real_worker_without_source_falls_back(self):
        adapter = PollingAdapter()
        worker = _MagnetWorker(safe_interlock())
        worker.adapter = adapter
        worker.thermal_source = None
        adapter.thermal_guard = worker._check_thermal
        adapter.thermal_field_guard = worker._check_thermal
        history = move(adapter, 'standard')
        self.assertEqual(history['polling_diagnostics']['shared_field_reads'], 0)
        self.assertGreaterEqual(adapter.log.count('field'), history['polling_diagnostics']['iterations'])

    def test_guard_failure_pauses_without_retrying_guard(self):
        for error in (APS100SafetyError('thermal trip'), OSError('IMAG failed')):
            with self.subTest(error=error):
                adapter = PollingAdapter()
                calls = []
                def guard(status):
                    calls.append(1)
                    raise error
                adapter.thermal_field_guard = guard
                with self.assertRaises(type(error)):
                    move(adapter, 'standard')
                self.assertEqual(len(calls), 1)
                self.assertIn('pause', adapter.log)


if __name__ == '__main__':
    unittest.main()
