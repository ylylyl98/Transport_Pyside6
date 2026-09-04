import math
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from app.devices.lakeshore335_adapter import (
    LakeShore335Adapter, LakeShore335CommunicationError,
    LakeShore335TelemetryError, MockLakeShore335Adapter,
)
from app.thermal_safety import ThermalSafetyEvaluator, ThermalState
from utils.config import LakeShore335Config


class _Resource:
    def __init__(self, values):
        self.values = dict(values)
        self.commands = []
    def query(self, command):
        self.commands.append(command)
        return self.values[command]
    def close(self):
        pass


def _config(**kwargs):
    values = dict(enabled=True, verified_channel_mapping=True,
                  sample_warning_temperature_k=5.0, sample_trip_temperature_k=6.0,
                  sample_recovery_temperature_k=4.0, reservoir_warning_temperature_k=5.0,
                  reservoir_trip_temperature_k=6.0, reservoir_recovery_temperature_k=4.0,
                  required_stable_recovery_dwell_s=0.0,
                  minimum_interval_between_heater_activations_s=0.0)
    values.update(kwargs)
    return LakeShore335Config(**values)


class LakeShoreAdapterTests(unittest.TestCase):
    def test_normal_reading_and_a_b_mapping_are_read_only(self):
        resource = _Resource({"*IDN?": "Lake Shore,Model 335,SN,1",
                              "KRDG? A": "3.25", "KRDG? B": "4.50",
                              "RDGST? A": "000", "RDGST? B": "000"})
        adapter = LakeShore335Adapter(visa_resource=resource)
        adapter.connect()
        snapshot = adapter.read_snapshot("A", "B")
        self.assertEqual(snapshot.sample_temperature_k, 3.25)
        self.assertEqual(snapshot.reservoir_temperature_k, 4.5)
        self.assertEqual(resource.commands, ["*IDN?", "KRDG? A", "KRDG? B", "RDGST? A", "RDGST? B"])
        self.assertFalse(any("CONF" in command or "SETP" in command or "PID" in command for command in resource.commands))

    def test_nan_and_communication_failures_fail_closed(self):
        resource = _Resource({"*IDN?": "Lake Shore,Model 335,SN,1",
                              "KRDG? A": "nan", "KRDG? B": "4.50",
                              "RDGST? A": "0", "RDGST? B": "0"})
        adapter = LakeShore335Adapter(visa_resource=resource)
        adapter.connect()
        with self.assertRaises(LakeShore335TelemetryError):
            adapter.read_snapshot()
        broken = _Resource({"*IDN?": "Lake Shore,Model 335,SN,1"})
        adapter = LakeShore335Adapter(visa_resource=broken)
        adapter.connect()
        with self.assertRaises(LakeShore335CommunicationError):
            adapter.read_snapshot()

    def test_mock_supports_status_and_missing_sensor(self):
        adapter = MockLakeShore335Adapter(sample_temperature_k=None, reservoir_temperature_k=3.0,
                                          sample_sensor_status="MISSING")
        snapshot = adapter.read_snapshot()
        self.assertIsNone(snapshot.sample_temperature_k)
        self.assertEqual(snapshot.sample_sensor_status, "MISSING")

    def test_wrong_model_and_channel_injection_are_rejected(self):
        for identity in ("Lake Shore,Model 336,SN,1", "LSCI,MODEL1335,SN,1"):
            resource = _Resource({"*IDN?": identity})
            adapter = LakeShore335Adapter(visa_resource=resource)
            with self.assertRaises(LakeShore335TelemetryError):
                adapter.connect()
        resource = _Resource({"*IDN?": "LSCI,MODEL335,SN,1"})
        adapter = LakeShore335Adapter(visa_resource=resource)
        adapter.connect()
        with self.assertRaises(LakeShore335TelemetryError):
            adapter.read_snapshot("A\nPSHTR ON", "B")
        with self.assertRaises(LakeShore335TelemetryError):
            adapter._query("PSHTR ON")
        with self.assertRaises(LakeShore335TelemetryError):
            adapter._query("*IDN?\n")


class ThermalEvaluatorTests(unittest.TestCase):
    def test_armed_commissioning_defaults_and_restart_does_not_restore_safe(self):
        config = LakeShore335Config()
        self.assertTrue(config.enabled)
        self.assertTrue(config.verified_channel_mapping)
        self.assertEqual((config.sample_channel, config.reservoir_channel), ("B", "A"))
        self.assertEqual(
            (
                config.sample_recovery_temperature_k,
                config.sample_warning_temperature_k,
                config.sample_trip_temperature_k,
                config.reservoir_recovery_temperature_k,
                config.reservoir_warning_temperature_k,
                config.reservoir_trip_temperature_k,
            ),
            (4.2, 4.6, 5.5, 3.9, 4.2, 4.8),
        )
        self.assertEqual(config.required_stable_recovery_dwell_s, 30.0)
        self.assertEqual(config.minimum_interval_between_heater_activations_s, 60.0)
        self.assertIsNone(config.maximum_positive_slope_k_per_min)
        evaluator = ThermalSafetyEvaluator(config)
        self.assertTrue(evaluator.is_armed)
        self.assertEqual(evaluator.evaluate().state, ThermalState.MONITOR_FAULT)
        snapshot = SimpleNamespace(
            sample_temperature_k=1, reservoir_temperature_k=1,
            sample_sensor_status="0", reservoir_sensor_status="0",
            monotonic_s=time.monotonic(), connected=True,
            communication_valid=True, sample_slope_k_per_min=None,
            reservoir_slope_k_per_min=None,
        )
        self.assertEqual(evaluator.evaluate(snapshot).state, ThermalState.COOLDOWN_HOLD)
        restarted = ThermalSafetyEvaluator(config)
        self.assertFalse(restarted.magnet_permission)
        self.assertEqual(restarted.evaluate().state, ThermalState.MONITOR_FAULT)
        evaluator = ThermalSafetyEvaluator(_config())
        self.assertFalse(evaluator.magnet_permission)
        self.assertEqual(evaluator.evaluate().state, ThermalState.MONITOR_FAULT)

    def test_warning_trip_recovery_hysteresis_and_status_fault(self):
        evaluator = ThermalSafetyEvaluator(_config())
        now = [100.0]
        evaluator.clock = lambda: now[0]
        def snap(sample, reservoir, status="0", age=0):
            return SimpleNamespace(sample_temperature_k=sample, reservoir_temperature_k=reservoir,
                sample_sensor_status=status, reservoir_sensor_status="0", monotonic_s=now[0]-age,
                connected=True, communication_valid=True, sample_slope_k_per_min=0.0,
                reservoir_slope_k_per_min=0.0)
        self.assertEqual(evaluator.evaluate(snap(5.1, 2)).state, ThermalState.WARNING)
        self.assertEqual(evaluator.evaluate(snap(6.1, 2)).state, ThermalState.TRIPPED)
        self.assertEqual(evaluator.evaluate(snap(3.0, 3)).state, ThermalState.SAFE)
        self.assertEqual(evaluator.evaluate(snap(3.0, 3, "OVERLOAD")).state, ThermalState.MONITOR_FAULT)
        self.assertEqual(evaluator.evaluate(snap(3.0, 3, age=10)).state, ThermalState.MONITOR_FAULT)

    def test_rdgst_zero_formats_are_clear_and_nonzero_or_malformed_fail_closed(self):
        evaluator = ThermalSafetyEvaluator(_config())
        now = time.monotonic()

        def snap(status):
            return SimpleNamespace(
                sample_temperature_k=3.0, reservoir_temperature_k=3.0,
                sample_sensor_status=status, reservoir_sensor_status="000",
                monotonic_s=now, connected=True, communication_valid=True,
                sample_slope_k_per_min=0.0, reservoir_slope_k_per_min=0.0,
            )

        for status in ("000", "0", "0.0", 0):
            with self.subTest(clear_status=status):
                decision = evaluator.evaluate(snap(status), now=now)
                self.assertEqual(decision.state, ThermalState.SAFE)
                self.assertTrue(decision.magnet_permission)

        for status in ("001", "016", "OVERLOAD", "", None, "nan"):
            with self.subTest(fault_status=status):
                decision = evaluator.evaluate(snap(status), now=now)
                self.assertEqual(decision.state, ThermalState.MONITOR_FAULT)
                self.assertFalse(decision.magnet_permission)

    def test_stable_dwell_slope_and_heater_interval(self):
        evaluator = ThermalSafetyEvaluator(_config(required_stable_recovery_dwell_s=10,
                                                    minimum_interval_between_heater_activations_s=20,
                                                    maximum_positive_slope_k_per_min=0.5))
        now = [100.0]
        evaluator.clock = lambda: now[0]
        def snap(slope=0.0):
            return SimpleNamespace(sample_temperature_k=3.0, reservoir_temperature_k=3.0,
                sample_sensor_status="0", reservoir_sensor_status="0", monotonic_s=now[0],
                connected=True, communication_valid=True, sample_slope_k_per_min=slope,
                reservoir_slope_k_per_min=0.0)
        self.assertEqual(evaluator.evaluate(snap(1.0)).state, ThermalState.COOLDOWN_HOLD)
        self.assertEqual(evaluator.evaluate(snap()).state, ThermalState.COOLDOWN_HOLD)
        now[0] += 10
        evaluator.note_heater_activation()
        self.assertEqual(evaluator.evaluate(snap()).state, ThermalState.COOLDOWN_HOLD)
        now[0] += 20
        self.assertTrue(evaluator.evaluate(snap()).magnet_permission)
        evaluator.note_heater_activation()
        evidence = evaluator.snapshot_dict()
        self.assertIn("heater_activation_events", evidence)
        self.assertIn("utc_timestamp", evidence["heater_activation_events"][-1])
        self.assertEqual(evidence["sample_slope_k_per_min"], 0.0)
        self.assertTrue(evidence["connected"])
        self.assertTrue(evidence["communication_valid"])

    def test_timeout_disconnect_and_nonfinite_are_not_permissive(self):
        evaluator = ThermalSafetyEvaluator(_config())
        now = time.monotonic()
        common = dict(sample_temperature_k=3.0, reservoir_temperature_k=3.0,
                      sample_sensor_status="0", reservoir_sensor_status="0", monotonic_s=now,
                      connected=False, communication_valid=False)
        self.assertFalse(evaluator.evaluate(SimpleNamespace(**common)).magnet_permission)
        common.update(connected=True, communication_valid=True, monotonic_s=now, sample_temperature_k=math.nan)
        self.assertFalse(evaluator.evaluate(SimpleNamespace(**common)).magnet_permission)

    def test_missing_or_invalid_timing_and_max_age_never_arm(self):
        snapshot = SimpleNamespace(sample_temperature_k=3.0, reservoir_temperature_k=3.0,
                                   sample_sensor_status="0", reservoir_sensor_status="0",
                                   monotonic_s=time.monotonic(), connected=True, communication_valid=True)
        for age in (None, 0.0, -1.0, math.inf):
            config = _config(maximum_reading_age_s=age)
            evaluator = ThermalSafetyEvaluator(config)
            self.assertFalse(evaluator.is_armed)
        evaluator = ThermalSafetyEvaluator(_config())
        del snapshot.monotonic_s
        self.assertEqual(evaluator.evaluate(snapshot).state, ThermalState.MONITOR_FAULT)


if __name__ == "__main__":
    unittest.main()
