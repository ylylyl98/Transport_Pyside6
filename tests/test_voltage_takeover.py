"""Regression coverage for front-panel voltage changes between app ramps."""
import unittest
from unittest.mock import patch

from app.utils import safe_ramp
from app.workers.photocurrent import PhotocurrentWorker
from instruments.instrument import InstrumentError
from test_keithley_protection import ConnectionSafetyKeithley as ConnectionStub


class ConnectionSafetyKeithley(ConnectionStub):
    def __init__(self, voltage):
        super().__init__(voltage)
        self._output_channels = (self.VOLT_OUTPUT,)


class VoltageTakeoverTests(unittest.TestCase):
    def writes(self, source):
        return [float(command.split()[-1]) for command in source.commands
                if command.startswith(":SOUR:VOLT:LEV ")]

    def test_ramp_starts_at_front_panel_setpoint_for_both_setters(self):
        for setter in ("set_voltage", "set_voltage_fast"):
            for start in (5.0, -5.0):
                with self.subTest(setter=setter, start=start):
                    source = ConnectionSafetyKeithley(start)
                    with patch("app.utils.time.sleep"):
                        safe_ramp(getattr(source, setter), 0.0, 1.0, 0.1, 0.02)
                    values = [start] + self.writes(source)
                    self.assertAlmostEqual(source.existing_voltage, 1.0)
                    self.assertTrue(all(abs(b - a) <= 0.100001
                                        for a, b in zip(values, values[1:])))

    def test_zero_is_not_skipped_when_cache_says_zero(self):
        source = ConnectionSafetyKeithley(0.2)
        with patch("app.utils.time.sleep"):
            safe_ramp(source.set_voltage, 0.0, 0.0, 0.05, 0.05)
        self.assertEqual(self.writes(source), [0.15, 0.1, 0.05, 0.0])

    def test_read_failure_aborts_without_voltage_write(self):
        source = ConnectionSafetyKeithley(5.0)
        with patch.object(source, "get_voltage_setpoint", side_effect=RuntimeError("read failed")):
            with self.assertRaisesRegex(RuntimeError, "read failed"):
                safe_ramp(source.set_voltage, 0.0, 0.0, 0.1, 0)
        self.assertEqual(self.writes(source), [])

    def test_nonfinite_instrument_setpoint_is_rejected(self):
        for value in (float("nan"), float("inf"), -float("inf")):
            with self.subTest(value=value):
                source = ConnectionSafetyKeithley(value)
                with self.assertRaises(InstrumentError):
                    source.get_voltage_setpoint()
                self.assertEqual(self.writes(source), [])

    def test_source_is_read_after_initial_pause_check(self):
        source = ConnectionSafetyKeithley(0.0)
        resumed = False
        def resume():
            nonlocal resumed
            if not resumed:
                source.existing_voltage = 0.2
                resumed = True
        with patch("app.utils.time.sleep"):
            safe_ramp(source.set_voltage, 0.0, 0.0, 0.05, 0, resume)
        self.assertTrue(self.writes(source))
        self.assertEqual(self.writes(source)[0], 0.15)

    def test_initial_cancellation_prevents_queries_and_writes(self):
        source = ConnectionSafetyKeithley(5.0)
        def cancel():
            raise RuntimeError("cancelled")
        with self.assertRaisesRegex(RuntimeError, "cancelled"):
            safe_ramp(source.set_voltage, 0.0, 1.0, 0.1, 0, cancel)
        self.assertEqual(source.events, [])

    def test_current_setpoint_at_target_does_not_write_despite_stale_cache(self):
        source = ConnectionSafetyKeithley(1.0)
        with patch("app.utils.time.sleep"):
            safe_ramp(source.set_voltage, 5.0, 1.0, 0.1, 0)
        self.assertEqual(self.writes(source), [])
        self.assertIn(("query", ":SOUR:VOLT:LEV?"), source.events)

    def test_safe_ramp_rejects_nonfinite_readback_from_other_sources(self):
        source = ConnectionSafetyKeithley(5.0)
        with patch.object(source, "get_voltage_setpoint", return_value=float("nan")):
            with self.assertRaisesRegex(ValueError, "non-finite"):
                safe_ramp(source.set_voltage, 0.0, 0.0, 0.1, 0)
        self.assertEqual(self.writes(source), [])

    def test_photocurrent_does_not_fall_back_to_cache_on_read_failure(self):
        source = ConnectionSafetyKeithley(5.0)
        with patch.object(source, "get_voltage_setpoint", side_effect=RuntimeError("read failed")):
            with self.assertRaisesRegex(RuntimeError, "read failed"):
                PhotocurrentWorker._source_voltage(None, source)

    def test_plain_callback_keeps_explicit_start(self):
        writes = []
        safe_ramp(writes.append, 0.2, 0.0, 0.1, 0)
        self.assertEqual(writes, [0.1, 0.0])


if __name__ == "__main__":
    unittest.main()
