import unittest
from app.engine.aps100_rate_table import validate_rate_table, field_move_seconds
from app.devices.aps100_attodry1000_adapter import MockAPS100Adapter
from controllers.magnet_controller import _MagnetWorker

class RateTableTests(unittest.TestCase):
    def test_step_count_reflects_real_targets_for_both_backends(self):
        from app.models import BFieldTransportParams
        from app.engine.bfield_transport_sweep import TransportSweepPlan
        for backend in ("1000", "2100"):
            params = BFieldTransportParams(magnet_backend=backend, measurement_mode="stepped",
                                           start_field_t=0., stop_field_t=.3,
                                           field_spacing="step", field_step_t=.1, field_points=11)
            TransportSweepPlan.from_params(params)
            self.assertEqual(params.field_points, 4)

    def test_cross_zero_and_boundaries(self):
        rates = {0:(1., 1.), 1:(2., 2.), 2:(3., 3.), 3:(4., 4.), 4:(5., 5.)}
        validate_rate_table(rates, 1., 5., 1., 5.)
        self.assertAlmostEqual(field_move_seconds(-2., 2., rates, 1.), 180.)
        self.assertAlmostEqual(field_move_seconds(1., 2., rates, 1.), 30.)

    def test_bad_table_fails_closed(self):
        rates = {0:(1., 1.), 1:(2., 2.), 2:(3., 3.), 3:(4., 4.), 4:(5., 5.)}
        for index, entry in [(1, (1., 2.)), (2, (3., 0.)), (4, (4.5, 5.)), (1, (2., 100.))]:
            bad = dict(rates); bad[index] = entry
            with self.assertRaises(ValueError):
                validate_rate_table(bad, 1., 5., 1., 5.)

    def test_worker_applies_ceiling_and_restores_rates(self):
        adapter = MockAPS100Adapter(); adapter.connect(); adapter.set_limits_t(-1., 1.)
        before = adapter.get_rates()
        worker = _MagnetWorker(); worker.adapter = adapter
        worker._check_thermal = lambda *args, **kwargs: None
        configured, restored = [], []
        worker.transport_config_result.connect(configured.append)
        worker.transport_restore_result.connect(restored.append)
        worker.configure_transport(.01, 6.)
        self.assertTrue(configured[-1]['success'], configured[-1])
        self.assertTrue(all(rate <= .01 for _, rate in adapter.get_rates().values()))
        worker.restore_transport(configured[-1]['stored_rates'], configured[-1]['stored_limits'])
        self.assertTrue(restored[-1]['success'], restored[-1])
        self.assertEqual(before, adapter.get_rates())

    def test_excessive_request_fails_before_mutation(self):
        adapter = MockAPS100Adapter(); adapter.connect()
        before = adapter.get_rates()
        worker = _MagnetWorker(); worker.adapter = adapter
        configured = []
        worker.transport_config_result.connect(configured.append)
        worker.configure_transport(999., 6.)
        self.assertFalse(configured[-1]['success'])
        self.assertEqual(before, adapter.get_rates())

    def test_real_adapter_quantized_rate_table_and_partial_write_rollback(self):
        from app.devices.aps100_attodry1000_adapter import APS100AttoDry1000Adapter
        class Simulated(APS100AttoDry1000Adapter):
            def __init__(self):
                super().__init__()
                self.values = [.0343, .0171, .0050, .0002, .0002]
                self.fail_once = False
            def get_range_a(self, i): return [40., 44.28, 45., 89., 100.][i]
            def get_rate_a_per_s(self, i): return self.values[i] if i < 5 else .1
            def get_field_t(self): return 0.
            def read_snapshot(self): return object()
            def get_limits_t(self): return (-1., 1.)
            def get_voltage_limit_v(self): return 3.
            def take_remote(self): pass
            def get_status(self): return type("Status", (), {"sweep_active": False})()
            def set_limits_t(self, low, high): return (low, high)
            def _write(self, command):
                _, index, value = command.split()
                index = int(index)
                if self.fail_once and index == 2:
                    self.fail_once = False
                    raise RuntimeError("injected partial write")
                self.values[index] = round(float(value), 4)
        for requested in (.1, .12):
            adapter = Simulated(); before = list(adapter.values)
            worker = _MagnetWorker(); worker.adapter = adapter
            worker._check_thermal = lambda *args, **kwargs: None
            results = []; worker.transport_config_result.connect(results.append)
            worker.configure_transport(requested, 6.)
            self.assertTrue(results[-1]['success'], results[-1])
            for actual, original in zip(adapter.values, before):
                self.assertLessEqual(actual, original)
                self.assertLessEqual(actual * adapter.coil_constant_t_per_a * 60., requested + 1e-9)
            worker.restore_transport(results[-1]['stored_rates'], results[-1]['stored_limits'])
            self.assertEqual(adapter.values, before)
        adapter = Simulated(); before = list(adapter.values); adapter.fail_once = True
        worker = _MagnetWorker(); worker.adapter = adapter
        worker._check_thermal = lambda *args, **kwargs: None
        results = []; worker.transport_config_result.connect(results.append)
        worker.configure_transport(.1, 6.)
        self.assertFalse(results[-1]['success'])
        self.assertEqual(adapter.values, before)

    def test_directional_stages_restore_rates_and_never_reverse_on_overshoot(self):
        from app.devices.aps100_attodry1000_adapter import APS100AttoDry1000Adapter, APS100OvershootError
        class Sim(APS100AttoDry1000Adapter):
            def __init__(self):
                super().__init__(); self.value = 0.; self.target = 0.; self.events = []
                self.table = {i: (10. * (i + 1), .1) for i in range(5)}
            def get_field_t(self): return self.value
            def get_output_field_t(self): return self.value
            def get_rates(self): return dict(self.table)
            def restore_rates(self, rates): self.events.append(("rates", dict(rates))); self.table = dict(rates)
            def _ensure_no_fault(self): return None
            def _set_directional_target(self, target, current):
                self.target = target
                return "UP" if target >= current else "DOWN"
            def _write(self, command):
                self.events.append(("write", command))
                if command.startswith("SWEEP ") and command.endswith(" SLOW"):
                    self.value = self.target
            def pause(self, **kwargs): self.events.append(("pause", kwargs))
            def read_snapshot(self): return self.value
        adapter = Sim(); original = adapter.get_rates()
        self.assertEqual(adapter._monotonic_field_move(1., 1, .0002, 10., None, None), 1.)
        self.assertEqual(adapter.get_rates(), original)
        self.assertEqual([e[1] for e in adapter.events if e[0] == "write"], ["SWEEP UP SLOW", "SWEEP UP SLOW"])
        adapter.events.clear(); adapter.value = 1.001
        with self.assertRaises(APS100OvershootError):
            adapter._monotonic_field_move(1., 1, .0002, 10., None, None)
        self.assertFalse(any(e[0] == "write" for e in adapter.events))
        self.assertEqual(adapter.get_rates(), original)
        # No sweep may start after the original deadline.
        from unittest.mock import patch
        from app.devices.aps100_attodry1000_adapter import APS100TimeoutError
        adapter = Sim()
        with patch("app.devices.aps100_attodry1000_adapter.time.monotonic", side_effect=[0., 2.]):
            with self.assertRaises(APS100TimeoutError):
                adapter._monotonic_field_move(1., 1, .0002, 1., None, None)
        self.assertFalse(any(e[0] == "write" for e in adapter.events))
        # An unconfirmed pause must not be reported as a resumable overshoot.
        adapter = Sim(); adapter.value = 1.001
        def failed_pause(**kwargs): raise RuntimeError("pause not acknowledged")
        adapter.pause = failed_pause
        with self.assertRaisesRegex(RuntimeError, "pause not acknowledged"):
            adapter._monotonic_field_move(1., 1, .0002, 10., None, None)
        # A fault during the slow stage must restore the per-run table.
        adapter = Sim(); original = adapter.get_rates()
        original_write = adapter._write
        def fail_slow(command):
            if command == "SWEEP UP SLOW" and any(e[0] == "rates" for e in adapter.events):
                raise RuntimeError("injected sweep failure")
            original_write(command)
        adapter._write = fail_slow
        with self.assertRaisesRegex(RuntimeError, "injected"):
            adapter._monotonic_field_move(1., 1, .0002, 10., None, None)
        self.assertEqual(adapter.get_rates(), original)

    def test_hysteresis_history_acceptance_and_safety_limits(self):
        from app.devices.aps100_attodry1000_adapter import (APS100AttoDry1000Adapter,
            APS100FieldHistoryError, APS100SafetyError)
        from unittest.mock import patch
        class Sim(APS100AttoDry1000Adapter):
            def __init__(self, samples):
                super().__init__(); self.value = 0.; self.samples = list(samples); self.queue = []
                self.table = {i: (10. * (i + 1), .1) for i in range(5)}
                self.writes = []; self.clock = 0.
                self._sleep = lambda seconds: setattr(self, "clock", self.clock + seconds)
            def get_field_t(self):
                if self.queue: self.value = self.queue.pop(0)
                return self.value
            def get_output_field_t(self): return self.value
            def get_rates(self): return dict(self.table)
            def restore_rates(self, values): self.table = dict(values)
            def _ensure_no_fault(self): pass
            def _set_directional_target(self, target, current): return "UP" if target >= current else "DOWN"
            def _write(self, command): self.writes.append(command); self.queue = list(self.samples)
            def pause(self, **kwargs): self.writes.append("PAUSE")
            def read_snapshot(self): return self.value
            def _sleep(self, seconds): self.clock += seconds
        options = dict(acceptance_t=.0005, reversal_t=.0002, lower_t=-.001, upper_t=.002, no_progress_s=.2)
        adapter = Sim([.0005, .0013]); original = adapter.get_rates(); history = {}
        with patch("app.devices.aps100_attodry1000_adapter.time.monotonic", side_effect=lambda: adapter.clock):
            result = adapter._hysteresis_field_move(.001, 1, .0002, 30., None, None, options, history)
        self.assertAlmostEqual(result, .0013)
        self.assertEqual(adapter.get_rates(), original)
        self.assertIn("PAUSE", adapter.writes)
        self.assertGreaterEqual(history["read_count"], 3)
        self.assertAlmostEqual(history["max_t"], .0013)
        self.assertTrue(history["complete"])
        for readings in ([.0006, .0003], [.0021]):
            adapter = Sim(readings); history = {}; original = adapter.get_rates()
            with patch("app.devices.aps100_attodry1000_adapter.time.monotonic", side_effect=lambda: adapter.clock):
                with self.assertRaises(APS100FieldHistoryError):
                    adapter._hysteresis_field_move(.001, 1, .0002, 30., None, None, options, history)
            self.assertIn("PAUSE", adapter.writes)
            self.assertEqual(adapter.get_rates(), original)
            self.assertIsNotNone(history["finished_monotonic_s"])
        adapter = Sim([.0001]); history = {}
        with patch("app.devices.aps100_attodry1000_adapter.time.monotonic", side_effect=lambda: adapter.clock):
            with self.assertRaises(APS100SafetyError):
                adapter._hysteresis_field_move(.001, 1, .0002, 30., None, None, options, history)
        self.assertGreater(adapter.clock, options["no_progress_s"])
        self.assertIn("PAUSE", adapter.writes)
        adapter = Sim([.0006]); history = {}
        with patch("app.devices.aps100_attodry1000_adapter.time.monotonic", side_effect=lambda: adapter.clock):
            result = adapter._hysteresis_field_move(.001, 1, .0002, 30., None, None, options, history)
        self.assertAlmostEqual(result, .0006)
        self.assertIn("PAUSE", adapter.writes)

    def test_worker_propagates_preparation_bounds_and_history(self):
        adapter = MockAPS100Adapter(); adapter.connect()
        adapter._heater = True; adapter._field_t = -.01; adapter._output_t = -.01
        adapter._time_scale = 1e6
        worker = _MagnetWorker(); worker.adapter = adapter
        worker.heating_interlock.begin_magnet = lambda *args, **kwargs: None
        worker.heating_interlock.end_magnet = lambda *args, **kwargs: None
        worker._check_thermal = lambda *args, **kwargs: None
        results = []; worker.safe_move_result.connect(results.append)
        options = dict(acceptance_t=.0002, reversal_t=.0002, lower_t=-.0002,
                       upper_t=.0102, no_progress_s=1., preparation=True)
        worker.safe_move_to_field("request", 0., "driven", False, True, 0., 60., .0001, False, 0, options)
        self.assertTrue(results[-1]["success"], results[-1])
        self.assertEqual(results[-1]["field_history"], results[-1]["audit"]["field_history"])
        applied = results[-1]["audit"]["request"]["hysteresis_options"]
        self.assertAlmostEqual(applied["lower_t"], -.0102)
        self.assertEqual(applied["upper_t"], .0102)
        self.assertGreaterEqual(results[-1]["field_history"]["read_count"], 2)

    def test_slow_configuration_emits_fresh_real_field_between_rate_steps(self):
        from types import SimpleNamespace
        from unittest.mock import Mock, patch
        from app.thermal_safety import ThermalSafetyEvaluator
        from app.engine.bfield_transport_controller import BFieldTransportController
        from tests.test_stepped_transport import Magnet
        from utils.config import LakeShore335Config
        clock = [100.]
        config = LakeShore335Config(); config.maximum_field_reading_age_s = 3.
        config.required_stable_recovery_dwell_s = 0.
        thermal = ThermalSafetyEvaluator(config, clock=lambda: clock[0])
        thermal.note_magnet_snapshot(SimpleNamespace(field_t=0., monotonic_s=clock[0]))
        lake = SimpleNamespace(monotonic_s=clock[0], connected=True, communication_valid=True,
            sample_temperature_k=3.6, reservoir_temperature_k=3.3, sample_sensor_status=0, reservoir_sensor_status=0)
        controller = BFieldTransportController(Magnet(), SimpleNamespace(), SimpleNamespace(), thermal_safety=thermal)
        controller._active = True; controller._measurement_phase = "configuring"
        controller._latest_lakeshore = lambda: lake
        controller._capture_telemetry = lambda *args: None
        controller._fail = Mock(); controller._begin_monitor_hold = Mock(); controller._request_thermal_pause = Mock()
        class SlowAdapter(MockAPS100Adapter):
            def __init__(self):
                super().__init__(); self.events = []
            def get_field_t(self):
                self.events.append("field")
                return 0.
            def get_range_a(self, index):
                clock[0] += 1.
                lake.monotonic_s = clock[0]
                controller.on_lakeshore_snapshot(lake)
                return super().get_range_a(index)
            def start_sweep_to(self, target):
                raise AssertionError("configuration must never start motion")
        adapter = SlowAdapter(); adapter.connect()
        worker = _MagnetWorker(); worker.adapter = adapter
        worker.thermal_source = SimpleNamespace(config=config, latest_snapshot=lake)
        worker.heating_interlock.magnet_block_reason = lambda: ""
        worker.thermal_field_updated.connect(thermal.note_magnet_snapshot)
        results, snapshots = [], []
        worker.transport_config_result.connect(results.append)
        worker.snapshot_updated.connect(snapshots.append)
        with patch("app.thermal_safety.ThermalSafetyEvaluator", side_effect=lambda cfg: ThermalSafetyEvaluator(cfg, clock=lambda: clock[0])), \
             patch("controllers.magnet_controller.time.monotonic", side_effect=lambda: clock[0]):
            worker.configure_transport(.01, 6.)
        self.assertTrue(results[-1]["success"], results[-1])
        self.assertGreater(clock[0] - 100., 3.)
        self.assertTrue(snapshots)
        controller._fail.assert_not_called()
        controller._begin_monitor_hold.assert_not_called()
        controller._request_thermal_pause.assert_not_called()
        self.assertGreater(adapter.events.count("field"), 10)

    def test_real_thermal_guard_emits_field_observation_in_configuration(self):
        import time
        from types import SimpleNamespace
        from utils.config import LakeShore335Config
        config = LakeShore335Config()
        lake = SimpleNamespace(monotonic_s=time.monotonic(), connected=True, communication_valid=True,
            sample_temperature_k=3.6, reservoir_temperature_k=3.3, sample_sensor_status=0, reservoir_sensor_status=0)
        adapter = MockAPS100Adapter(); adapter.connect()
        worker = _MagnetWorker(); worker.adapter = adapter
        worker.thermal_source = SimpleNamespace(config=config, latest_snapshot=lake)
        worker.heating_interlock.magnet_block_reason = lambda: ""
        observed, result = [], []
        worker.thermal_field_updated.connect(observed.append)
        worker.transport_config_result.connect(result.append)
        worker.configure_transport(.01, 6.)
        self.assertTrue(result[-1]["success"], result[-1])
        self.assertGreater(len(observed), 10)
        self.assertTrue(all(abs(o.field_t - adapter.get_field_t()) < 1e-9 for o in observed))

    def test_thermal_failure_during_rate_configuration_aborts_and_restores(self):
        adapter = MockAPS100Adapter(); adapter.connect()
        old_rates = adapter.get_rates(); old_limits = adapter.get_limits_t()
        worker = _MagnetWorker(); worker.adapter = adapter
        calls = [0]
        def fail_after_mutation(*args, **kwargs):
            calls[0] += 1
            if calls[0] == 17:
                raise RuntimeError("simulated thermal interlock")
        worker._check_thermal = fail_after_mutation
        results = []; worker.transport_config_result.connect(results.append)
        worker.configure_transport(.01, 6.)
        self.assertFalse(results[-1]["success"])
        self.assertIn("simulated thermal interlock", results[-1]["error"])
        self.assertEqual(adapter.get_rates(), old_rates)
        self.assertEqual(adapter.get_limits_t(), old_limits)

    def test_public_configuration_clears_old_stop_before_queue_not_new_stop(self):
        from types import SimpleNamespace
        from controllers.magnet_controller import MagnetController
        worker = _MagnetWorker(); worker.request_stop("previous")
        observed = []
        def queue(rate, limit):
            observed.append(worker._stop_event.is_set())
            worker.request_stop("user")
        controller = SimpleNamespace(_worker=worker,
            _transport_config_requested=SimpleNamespace(emit=queue))
        MagnetController.configure_transport(controller, .01, 6.)
        self.assertEqual(observed, [False])
        self.assertTrue(worker._stop_event.is_set())
        self.assertEqual(worker._stop_event.reason, "user")

    def test_stop_during_rate_configuration_aborts_and_restores(self):
        adapter = MockAPS100Adapter(); adapter.connect()
        old_rates = adapter.get_rates(); old_limits = adapter.get_limits_t()
        worker = _MagnetWorker(); worker.adapter = adapter
        calls = [0]
        def stop_after_mutation(*args, **kwargs):
            calls[0] += 1
            if calls[0] == 17:
                worker.request_stop("user")
        worker._check_thermal = stop_after_mutation
        results = []; worker.transport_config_result.connect(results.append)
        worker.configure_transport(.01, 6.)
        self.assertFalse(results[-1]["success"])
        self.assertEqual(adapter.get_rates(), old_rates)
        self.assertEqual(adapter.get_limits_t(), old_limits)

    def test_worker_bad_table_fails_before_limits_mutate(self):
        adapter = MockAPS100Adapter(); adapter.connect(); adapter.set_limits_t(-1., 1.)
        adapter.get_range_a = lambda i: 1.
        adapter.set_limits_t = lambda *a: self.fail('bad table reached limit writes')
        worker = _MagnetWorker(); worker.adapter = adapter
        configured = []
        worker.transport_config_result.connect(configured.append)
        worker.configure_transport(.1, 6.)
        self.assertFalse(configured[-1]['success'])

if __name__ == '__main__': unittest.main()
