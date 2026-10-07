import time
import unittest
from types import SimpleNamespace as NS
from unittest.mock import Mock

from PySide6.QtWidgets import QApplication
from app.thermal_safety import ThermalSafetyEvaluator, ThermalState
from app.devices.aps100_attodry1000_adapter import APS100SafetyError
from controllers.magnet_controller import _MagnetWorker
from utils.config import LakeShore335Config


class LiveFieldThermalTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.config = LakeShore335Config()
        self.safety = ThermalSafetyEvaluator(self.config)

    def snapshot(self, temp=4.833):
        return NS(monotonic_s=time.monotonic(), connected=True, communication_valid=True,
                  reservoir_temperature_k=temp, sample_temperature_k=3.5,
                  sample_sensor_status='0', reservoir_sensor_status='0')

    def decision(self, field, temp, **kwargs):
        self.safety.note_magnet_snapshot(NS(field_t=field, monotonic_s=time.monotonic()))
        return self.safety.evaluate(self.snapshot(temp), for_continuation=True, **kwargs)

    def test_recorded_low_field_event_is_allowed_but_high_field_is_not(self):
        for sign in (-1, 1):
            self.assertTrue(self.decision(sign*.4, 4.833).magnet_permission)
            self.assertEqual(self.decision(sign*8, 4.833).state, ThermalState.TRIPPED)

    def test_boundaries_and_stop_margin_in_both_polarities(self):
        for field, ceiling in [(0,5.5),(6,5.5),(6.001,5),(7,5),(7.001,4.5),(8,4.5),(8.001,4.2),(9,4.2)]:
            for sign in (-1,1):
                self.assertEqual(self.decision(sign*field, ceiling).state,ThermalState.TRIPPED)
                self.assertEqual(self.decision(sign*field, ceiling-.05).state,ThermalState.WARNING)
                self.assertTrue(self.decision(sign*field, ceiling-.2).magnet_permission)

    def test_missing_stale_nonfinite_field_and_invalid_margin_fail_closed(self):
        self.assertFalse(self.safety.evaluate(self.snapshot(), for_continuation=True).magnet_permission)
        self.safety.note_magnet_snapshot(NS(field_t=.4, monotonic_s=time.monotonic()-10))
        self.assertFalse(self.safety.evaluate(self.snapshot(), for_continuation=True).magnet_permission)
        for field in (float('nan'), float('inf'), 9.01):
            self.assertFalse(self.decision(field,3.5).magnet_permission)
        self.config.field_stop_margin_k = float('nan')
        self.assertFalse(self.decision(.4,3.5).magnet_permission)

    def worker(self, field, temp):
        worker = _MagnetWorker()
        worker.thermal_source = self.safety
        self.safety.latest_snapshot = self.snapshot(temp)
        worker.adapter = Mock()
        worker.adapter.get_field_t.return_value = field
        worker.adapter.get_status.return_value = NS(sweep_active=True)
        return worker

    def test_persistent_measurement_does_not_require_new_heater_cycle_recovery(self):
        self.config.required_stable_recovery_dwell_s = 0
        self.safety.note_heater_activation()
        self.safety.note_magnet_snapshot(NS(field_t=-.4, monotonic_s=time.monotonic()))
        snap = self.snapshot(4.833)
        self.assertFalse(self.safety.evaluate(snap).magnet_permission)
        self.assertTrue(self.safety.evaluate_measurement(snap).magnet_permission)
        self.safety.note_magnet_snapshot(NS(field_t=8, monotonic_s=time.monotonic()))
        self.assertEqual(self.safety.evaluate_measurement(snap).state, ThermalState.TRIPPED)

    def test_measurement_keeps_sensor_freshness_and_sample_limits(self):
        self.config.required_stable_recovery_dwell_s = 0
        self.safety.note_magnet_snapshot(NS(field_t=.4, monotonic_s=time.monotonic()))
        snap = self.snapshot(3.5)
        snap.sample_temperature_k = 5.6
        self.assertEqual(self.safety.evaluate_measurement(snap).state, ThermalState.TRIPPED)
        snap = self.snapshot(3.5)
        snap.monotonic_s -= 10
        self.assertEqual(self.safety.evaluate_measurement(snap).state, ThermalState.MONITOR_FAULT)
        snap = self.snapshot(3.5)
        snap.reservoir_sensor_status = '1'
        self.assertFalse(self.safety.evaluate_measurement(snap).magnet_permission)

    def test_measurement_target_dwell_resets_after_fault_or_target_change(self):
        now = 100.
        def check(sample=4.4, target=4.4, invalid=False):
            self.safety.note_magnet_snapshot(NS(field_t=.4, monotonic_s=now))
            snap = self.snapshot(4.1)
            snap.monotonic_s = now
            snap.sample_temperature_k = sample
            if invalid:
                snap.sample_sensor_status = '1'
            return self.safety.evaluate_measurement(snap, now, sample_target_k=target)
        self.assertFalse(check().magnet_permission)
        now += 6
        self.assertTrue(check().magnet_permission)
        self.assertFalse(check(target=4.41).magnet_permission)
        now += 6
        self.assertTrue(check(target=4.41).magnet_permission)
        self.assertFalse(check(target=4.41, invalid=True).magnet_permission)
        self.assertFalse(check(target=4.41).magnet_permission)
        self.assertEqual(check(target=4.6).state, ThermalState.DISARMED)

    def test_nine_tesla_destination_does_not_apply_high_field_limit_at_zero(self):
        worker = self.worker(0,4.833)
        worker._thermal_target = 9
        worker._check_thermal()
        worker.adapter.pause.assert_not_called()

    def test_next_band_stops_before_crossing_and_cooling_does_not_clear_stop(self):
        for field, target in [(5.98,7),(-5.98,-7)]:
            worker = self.worker(field,5.1)
            worker._thermal_target = target
            with self.assertRaises(APS100SafetyError):
                worker._check_thermal()
            worker.adapter.pause.assert_called_once_with()
            self.assertTrue(worker._stop_event.is_set())
            self.safety.latest_snapshot = self.snapshot(3.5)
            worker._check_thermal()
            self.assertTrue(worker._stop_event.is_set())
            worker.adapter.enter_persistent_mode.assert_not_called()
            worker.adapter.enter_driven_mode.assert_not_called()
            worker.adapter.zero_output.assert_not_called()

    def test_zero_crossing_uses_magnitude_and_precharge_needs_no_first_stage(self):
        worker = self.worker(-.02,4.833)
        worker._thermal_target = .4
        worker._check_thermal()
        self.safety.latest_snapshot = self.snapshot(4.1)
        worker._check_thermal(precharge=True)
        self.safety.latest_snapshot = self.snapshot(4.3)
        with self.assertRaises(APS100SafetyError):
            worker._check_thermal(precharge=True)

    def test_stale_temperature_stops_worker(self):
        worker = self.worker(.4,3.5)
        self.safety.latest_snapshot.monotonic_s -= 10
        with self.assertRaises(APS100SafetyError):
            worker._check_thermal()
        worker.adapter.pause.assert_called_once()

    def test_temperature_stop_preserves_mandatory_switch_dwell(self):
        worker = self.worker(.4,5.45)
        worker.adapter.thermal_dwell_active = True
        worker._check_thermal()
        self.assertTrue(worker._stop_event.is_set())
        worker.adapter.thermal_dwell_active = False
        with self.assertRaises(APS100SafetyError):
            worker._check_thermal()

    def test_pause_failure_is_reported_without_claiming_pause_succeeded(self):
        worker = self.worker(.4,5.45)
        worker.adapter.pause.side_effect = RuntimeError('no acknowledgement')
        faults = []
        worker.fault.connect(faults.append)
        with self.assertRaisesRegex(APS100SafetyError, 'PAUSE NOT CONFIRMED'):
            worker._check_thermal()
        self.assertIn('PAUSE NOT CONFIRMED', faults[0])

    def test_old_reservoir_warning_and_trip_do_not_gate_live_policy(self):
        self.config.reservoir_trip_temperature_k = None
        self.config.reservoir_warning_temperature_k = None
        self.assertTrue(self.decision(-.4,4.833).magnet_permission)

    def test_unreadable_field_stops_and_reports_failure(self):
        worker = self.worker(.4,3.5)
        worker.adapter.get_field_t.side_effect = RuntimeError('field unavailable')
        faults = []
        worker.fault.connect(faults.append)
        with self.assertRaisesRegex(APS100SafetyError, 'cannot verify magnet field'):
            worker._check_thermal()
        worker.adapter.pause.assert_called_once()
        self.assertTrue(worker._stop_event.is_set())
        self.assertEqual(len(faults),1)

    def test_invalid_destination_cannot_be_hidden_by_lookahead(self):
        worker = self.worker(0,3.5)
        for target in (10, float('nan')):
            with self.assertRaises(APS100SafetyError):
                worker._check_thermal(target=target)
