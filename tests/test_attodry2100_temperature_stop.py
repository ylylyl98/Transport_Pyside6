import concurrent.futures
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from app.devices.attodry2100_adapter import AttoDRY2100Adapter, AttoDRY2100StoppedError


class TemperatureStopTests(unittest.TestCase):
    def test_stop_during_snapshot_prevents_later_setpoint(self):
        entered, resume, stop = threading.Event(), threading.Event(), threading.Event()
        calls = []
        class Sample:
            def setSetPoint(self, target): calls.append(("set", target))
            def startTempControl(self): calls.append(("start",))
        adapter = AttoDRY2100Adapter("unused")
        adapter._device = object()
        adapter._sample = Sample()
        def snapshot():
            entered.set()
            if not resume.wait(2): raise RuntimeError("test timeout")
            return SimpleNamespace(sample_setpoint_k=3.0, sample_control_active=False)
        adapter.read_temperature_snapshot = snapshot
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(adapter.configure_sample_temperature, 4.0, 1.0, stop)
            try:
                self.assertTrue(entered.wait(1))
                stop.set()
            finally:
                resume.set()
            with self.assertRaises(AttoDRY2100StoppedError):
                future.result(2)
        self.assertEqual(calls, [])


class SnapshotFreshnessTests(unittest.TestCase):
    def test_snapshot_timestamps_precede_later_slow_sdk_reads(self):
        adapter = AttoDRY2100Adapter("unused")
        adapter._device = object()
        clock = [10.0]
        adapter.read_field = lambda: 0.5
        adapter.read_sample_temperature = lambda: 4.0
        def delayed_optional(name, *args):
            clock[0] += 1.0
            return None
        def delayed_temperature(component, name, *args, **kwargs):
            clock[0] += 1.0
            return 4.0 if name == "getSetPoint" else True if name == "getTempControlStatus" else None
        adapter._optional = delayed_optional
        adapter._temperature_call = delayed_temperature
        with patch("app.devices.attodry2100_adapter.time.monotonic", side_effect=lambda: clock[0]):
            field = adapter.read_snapshot()
            self.assertEqual(field.monotonic_s, 10.0)
            self.assertGreater(clock[0] - field.monotonic_s, 3.0)
            start = clock[0]
            temperature = adapter.read_temperature_snapshot()
            self.assertEqual(temperature.monotonic_s, start)
            self.assertGreater(clock[0] - temperature.monotonic_s, 3.0)
