import unittest
import threading
from unittest.mock import patch

from app.devices.aps100_attodry1000_adapter import APS100AttoDry1000Adapter, APS100SafetyError
from spectra_app.test_aps100_adapter import _FakeAPS100Resource


class CurrentMatchingTests(unittest.TestCase):
    def setUp(self):
        self.fake = _FakeAPS100Resource()
        self.adapter = APS100AttoDry1000Adapter(
            visa_resource=self.fake, resource_name="mock",
            heater_warm_s=0, heater_cool_s=0, sleep_fn=lambda _: None,
        )
        self.adapter.connect()
        self.adapter.take_remote()
        self.fake.heater = 0
        self.fake.field_kg = self.fake.output_kg = 20.005

    def test_transient_after_pause_recovers_without_rematching(self):
        outputs = iter([2.0005, 1.9979, 2.0005, 2.0005])
        with patch.object(self.adapter, "get_output_field_t", side_effect=lambda: next(outputs)), patch.object(self.adapter, "_start_output_sweep_to") as ramp:
            self.adapter.enter_driven_mode()
        ramp.assert_not_called()
        self.assertIn("PSHTR ON", self.fake.commands)

    def test_recorded_mismatch_is_rematched_before_heater_on(self):
        attempts = []
        def output():
            return 0.0 if not attempts else (1.9979 if len(attempts) == 1 else 2.0005)
        with patch.object(self.adapter, "get_output_field_t", side_effect=output), patch.object(self.adapter, "_start_output_sweep_to", side_effect=lambda target: attempts.append(target)), patch.object(self.adapter, "wait_for_field"):
            self.adapter.enter_driven_mode()
        self.assertEqual(len(attempts), 2)
        self.assertIn("PSHTR ON", self.fake.commands)

    def test_persistent_mismatch_stays_off_after_bounded_attempts(self):
        with patch.object(self.adapter, "get_output_field_t", return_value=1.9979), patch.object(self.adapter, "_start_output_sweep_to") as ramp, patch.object(self.adapter, "wait_for_field"):
            with self.assertRaisesRegex(APS100SafetyError, "2 rematching attempts"):
                self.adapter.enter_driven_mode()
        self.assertEqual(ramp.call_count, 3)
        self.assertNotIn("PSHTR ON", self.fake.commands)

    def test_heater_off_transient_settles_without_fast_ramp(self):
        self.fake.heater = 1
        outputs = iter([1.9979, 2.0005, 2.0005])
        with patch.object(self.adapter, "get_output_field_t", side_effect=lambda: next(outputs)), patch.object(self.adapter, "_start_output_sweep_to") as ramp:
            self.adapter.enter_persistent_mode(zero_leads=False)
        ramp.assert_not_called()
        self.assertIn("PSHTR OFF", self.fake.commands)

    def test_stop_during_settling_never_enables_heater(self):
        stop = threading.Event()
        with self.assertRaisesRegex(APS100SafetyError, "cancelled: user"):
            self.adapter.enter_driven_mode(stop_event=stop, progress=lambda *_: stop.set())
        self.assertNotIn("PSHTR ON", self.fake.commands)

    def test_switch_change_during_settling_is_fatal(self):
        def changed(*_):
            self.fake.heater = 1
        with self.assertRaisesRegex(APS100SafetyError, "switch state changed"):
            self.adapter.enter_driven_mode(progress=changed)
        self.assertNotIn("PSHTR ON", self.fake.commands)

    def test_nonfinite_readback_never_authorizes_heater(self):
        with patch.object(self.adapter, "get_output_field_t", return_value=float("nan")):
            with self.assertRaisesRegex(APS100SafetyError, "Nonfinite"):
                self.adapter.enter_driven_mode()
        self.assertNotIn("PSHTR ON", self.fake.commands)

    def test_one_good_reading_is_not_enough(self):
        from itertools import cycle
        readings = cycle([2.0005, 1.9979])
        with patch.object(self.adapter, "get_output_field_t", side_effect=lambda: next(readings)):
            matched, *_ = self.adapter._settled_current_match(heater_state=0, action="enable")
        self.assertFalse(matched)
        self.assertNotIn("PSHTR ON", self.fake.commands)

    def test_fault_during_settling_stops_without_heater_command(self):
        def fault(*_):
            self.fake.status = 4
        with self.assertRaises(APS100SafetyError):
            self.adapter.enter_driven_mode(progress=fault)
        self.assertNotIn("PSHTR ON", self.fake.commands)

    def test_final_readings_recover_from_transient_then_require_two_matches(self):
        from dataclasses import replace
        from app.devices.aps100_attodry1000_adapter import MockAPS100Adapter
        good = replace(MockAPS100Adapter().read_snapshot(), heater_on=False,
                       field_t=2.0, output_field_t=0.0, output_current_a=0.0)
        bad = replace(good, field_t=2.003)
        with patch.object(self.adapter, "read_snapshot", side_effect=[bad, good, good]) as reader:
            result = self.adapter._verify_final_readings(2.0, "persistent", True, 0.002)
        self.assertEqual(result.field_t, 2.0)
        self.assertEqual(reader.call_count, 3)

    def test_final_lead_offset_does_not_pass_settling(self):
        from dataclasses import replace
        from app.devices.aps100_attodry1000_adapter import MockAPS100Adapter
        bad = replace(MockAPS100Adapter().read_snapshot(), heater_on=False,
                      field_t=2.0, output_field_t=0.003, output_current_a=0.02)
        with patch.object(self.adapter, "read_snapshot", return_value=bad):
            with self.assertRaisesRegex(APS100SafetyError, "lead current"):
                self.adapter._verify_final_readings(2.0, "persistent", True, 0.002)
