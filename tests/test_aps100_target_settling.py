"""Offline replay of drift between ramp pause and persistent heater OFF."""
import threading
import unittest
from unittest.mock import patch

from app.devices.aps100_attodry1000_adapter import APS100AttoDry1000Adapter, APS100SafetyError
from spectra_app.test_aps100_adapter import _InstantSweepAPS100Resource


class PersistentTargetTests(unittest.TestCase):
    def setUp(self):
        self.clock = 0.0
        self.fake = _InstantSweepAPS100Resource()
        self.adapter = APS100AttoDry1000Adapter(
            visa_resource=self.fake, resource_name="mock",
            heater_warm_s=0, heater_cool_s=0, sleep_fn=self.sleep,
        )
        self.adapter.connect()
        self.adapter.take_remote()
        self.timer = patch("app.devices.aps100_attodry1000_adapter.time.monotonic", side_effect=lambda: self.clock)
        self.timer.start()
        self.addCleanup(self.timer.stop)

    def sleep(self, seconds):
        self.clock += seconds

    def enter(self, target=7.2, **kwargs):
        self.adapter.enter_persistent_mode(target_t=target, settle_s=2,
                                          zero_leads=False, **kwargs)

    def test_recorded_drift_and_negative_mirror_correct_before_off(self):
        for sign in (1, -1):
            with self.subTest(sign=sign):
                self.fake.heater = 1
                self.fake.commands.clear()
                readings = iter([7.2016, 7.2005, 7.1999, 7.1994, 7.1990, 7.1986, 7.1982, 7.1978])
                corrected = []
                def field():
                    return sign * (7.2 if corrected else next(readings, 7.1978))
                def correct(target, **kwargs):
                    self.assertEqual(self.fake.heater, 1)
                    self.assertNotIn("PSHTR OFF", self.fake.commands)
                    corrected.append(target)
                with patch.object(self.adapter, "get_field_t", side_effect=field), patch.object(
                    self.adapter, "get_output_field_t", side_effect=lambda: sign * (7.2 if corrected else 7.1978)
                ), patch.object(self.adapter, "move_to_field", side_effect=correct):
                    self.enter(sign * 7.2)
                self.assertEqual(corrected, [sign * 7.2])
                self.assertIn("PSHTR OFF", self.fake.commands)

    def test_failure_is_bounded_and_never_turns_heater_off(self):
        with patch.object(self.adapter, "get_field_t", return_value=7.1978), patch.object(
            self.adapter, "get_output_field_t", return_value=7.1978
        ), patch.object(self.adapter, "move_to_field") as move:
            with self.assertRaisesRegex(APS100SafetyError, "after 2 corrections"):
                self.enter()
        self.assertEqual(move.call_count, 2)
        self.assertNotIn("PSHTR OFF", self.fake.commands)

    def test_stable_target_needs_dwell_but_no_correction(self):
        self.fake.field_kg = self.fake.output_kg = 72
        with patch.object(self.adapter, "move_to_field") as move:
            self.enter()
        move.assert_not_called()
        self.assertGreaterEqual(self.clock, 2)
        self.assertIn("PSHTR OFF", self.fake.commands)

    def test_stop_during_verification_prevents_correction_and_off(self):
        self.fake.field_kg = self.fake.output_kg = 71.978
        stop = threading.Event()
        with patch.object(self.adapter, "move_to_field") as move:
            with self.assertRaises(APS100SafetyError):
                self.enter(stop_event=stop, progress=lambda *_: stop.set())
        move.assert_not_called()
        self.assertNotIn("PSHTR OFF", self.fake.commands)

    def test_current_mismatch_does_not_trigger_target_correction(self):
        self.fake.field_kg, self.fake.output_kg = 72, 71
        with patch.object(self.adapter, "move_to_field") as move:
            with self.assertRaisesRegex(APS100SafetyError, "current mismatch"):
                self.enter()
        move.assert_not_called()
        self.assertNotIn("PSHTR OFF", self.fake.commands)

    def test_nonfinite_and_switch_change_block_off(self):
        for value, heater in ((float("nan"), 1), (7.2, 2)):
            with self.subTest(value=value, heater=heater):
                self.fake.heater = heater
                with patch.object(self.adapter, "get_field_t", return_value=value):
                    with self.assertRaises(APS100SafetyError):
                        self.enter()
                self.assertNotIn("PSHTR OFF", self.fake.commands)

    def test_real_safe_move_uses_guard_and_preserves_final_check(self):
        self.fake.field_kg = self.fake.output_kg = 76
        result = self.adapter.safe_move_to_field(7.2, final_mode="persistent", settle_s=2,
                                                max_magnet_voltage_v=0.1)
        self.assertFalse(result.heater_on)
        self.assertLessEqual(abs(result.field_t - 7.2), 0.002)
        self.assertLessEqual(abs(result.output_field_t), 0.002)
        self.assertGreaterEqual(self.clock, 2)

    def test_drift_after_switch_is_still_rejected_by_final_check(self):
        self.fake.field_kg = self.fake.output_kg = 72
        original_write = self.fake.write
        def write(command):
            original_write(command)
            if command == "PSHTR OFF":
                self.fake.field_kg = 71.978
        with patch.object(self.fake, "write", side_effect=write):
            with self.assertRaisesRegex(APS100SafetyError, "Measurement not ready.*7.1978"):
                self.adapter.safe_move_to_field(7.2, final_mode="persistent", settle_s=2,
                                                max_magnet_voltage_v=0.1)

    def test_full_move_corrects_drift_using_normal_ramp_before_trapping(self):
        self.fake.field_kg = self.fake.output_kg = 76
        original_write = self.fake.write
        pauses = []
        rates = list(self.fake.rates)
        def write(command):
            original_write(command)
            if command == "SWEEP PAUSE" and self.fake.heater == 1:
                pauses.append(command)
                if len(pauses) == 1:
                    self.fake.field_kg = self.fake.output_kg = 71.978
        with patch.object(self.fake, "write", side_effect=write):
            result = self.adapter.safe_move_to_field(7.2, final_mode="persistent", settle_s=2,
                                                    max_magnet_voltage_v=0.1)
        self.assertEqual(result.field_t, 7.2)
        before_off = self.fake.commands[:self.fake.commands.index("PSHTR OFF")]
        self.assertTrue(any(command.startswith("SWEEP UP") for command in before_off))
        self.assertFalse(any("FAST" in command for command in before_off))
        self.assertEqual(self.fake.rates[:5], rates[:5])

    def test_hardware_fault_during_dwell_blocks_off_and_correction(self):
        self.fake.field_kg = self.fake.output_kg = 72
        def fault(*_):
            self.fake.status = 4
        with patch.object(self.adapter, "move_to_field") as move:
            with self.assertRaises(APS100SafetyError):
                self.enter(progress=fault)
        move.assert_not_called()
        self.assertNotIn("PSHTR OFF", self.fake.commands)


if __name__ == "__main__":
    unittest.main()
