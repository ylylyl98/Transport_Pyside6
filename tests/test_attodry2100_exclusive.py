import threading
import time
import unittest

from PySide6.QtCore import QCoreApplication
from controllers.attodry2100_controller import AttoDRY2100Controller, ControllerState
from app.devices.attodry2100_adapter import AttoDRY2100StateError, AttoDRY2100StoppedError
from utils.config import AttoDRY2100Config


class FakeAdapter:
    def __init__(self):
        self.entered = threading.Event()
        self.proceed = threading.Event()
        self.mutations = []
    def connect(self): return "fake"
    def close(self): pass
    def read_snapshot(self): return object()
    def read_temperature_snapshot(self): return object()
    def set_h_setpoint(self, target, stop_event=None):
        self.mutations.append(("set", target))
        return target
    def start_field_control(self, target, stop_event=None):
        self.mutations.append(("start", target))
        return True
    def stop_field_control(self):
        self.mutations.append(("stop",))
        return True
    def configure_sample_temperature(self, target, rate, stop_event=None):
        self.entered.set()
        self.proceed.wait(3)
        if stop_event.is_set():
            raise AttoDRY2100StoppedError("temperature stop requested")
        self.mutations.append(("temperature", target))
        return object()


class Exclusive2100Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QCoreApplication.instance() or QCoreApplication([])

    def setUp(self):
        self.adapter = FakeAdapter()
        self.controller = AttoDRY2100Controller(config=AttoDRY2100Config(poll_interval_s=100),
            adapter_factory=lambda _: self.adapter, request_timeout_s=5, shutdown_wait_s=2)
        handle = self.controller.connect_async()
        handle.result(2)
        handle.wait_drained(2)
        self._until(lambda: self.controller.state == ControllerState.IDLE)

    def tearDown(self):
        self.adapter.proceed.set()
        self.assertTrue(self.controller.shutdown(2))

    def _until(self, condition):
        end = time.monotonic() + 2
        while time.monotonic() < end:
            self.app.processEvents()
            if condition(): return
            time.sleep(.005)
        self.fail("timed out waiting for controller")

    def test_owner_rejects_other_commands_without_global_fault(self):
        self.assertTrue(self.controller.acquire_exclusive("experiment"))
        faults = []
        self.controller.fault.connect(faults.append)
        for handle in (self.controller.set_h_setpoint_async(1),
                       self.controller.configure_sample_temperature_async(4, 1),
                       self.controller.disconnect_async(), self.controller.read_snapshot_async()):
            with self.assertRaises(AttoDRY2100StateError): handle.result(1)
        self.app.processEvents()
        self.assertEqual(faults, [])
        read = self.controller.read_snapshot_async(owner="experiment")
        read.result(2); read.wait_drained(2)
        self._until(lambda: self.controller.release_exclusive("experiment"))
        self.assertEqual(self.controller.exclusive_owner, "")

    def test_external_stop_cancels_inflight_temperature_and_latches_mutations(self):
        self.assertTrue(self.controller.acquire_exclusive("experiment"))
        signals = []
        self.controller.external_stop_requested.connect(lambda: signals.append(True))
        temp = self.controller.configure_sample_temperature_async(4, 1, owner="experiment")
        self.assertTrue(self.adapter.entered.wait(1))
        self.assertFalse(self.controller.release_exclusive("experiment"))
        stop = self.controller.request_stop()
        self.adapter.proceed.set()
        with self.assertRaises(AttoDRY2100StoppedError): temp.result(2)
        with self.assertRaises(AttoDRY2100StoppedError): temp.wait_drained(2)
        stop.result(2); stop.wait_drained(2)
        self.app.processEvents()
        self.assertTrue(signals)
        self.assertNotIn(("temperature", 4), self.adapter.mutations)
        with self.assertRaises(AttoDRY2100StoppedError):
            self.controller.set_h_setpoint_async(1, owner="experiment").result(1)
        self._until(lambda: self.controller.release_exclusive("experiment"))

    def test_failed_stop_can_retry_without_external_notification(self):
        calls = []
        def flaky_stop():
            calls.append(True)
            if len(calls) == 1:
                raise RuntimeError("first stop failed")
            return True
        self.adapter.stop_field_control = flaky_stop
        self.assertTrue(self.controller.acquire_exclusive("experiment"))
        signals = []
        self.controller.external_stop_requested.connect(lambda: signals.append(True))
        failed = self.controller.request_stop(owner="experiment")
        with self.assertRaises(RuntimeError): failed.result(2)
        with self.assertRaises(RuntimeError): failed.wait_drained(2)
        retry = self.controller.retry_stop(owner="experiment")
        retry.result(2); retry.wait_drained(2)
        self.app.processEvents()
        self.assertEqual(len(calls), 2)
        self.assertEqual(signals, [])
        self._until(lambda: self.controller.release_exclusive("experiment"))

    def test_owner_stop_does_not_emit_external_signal(self):
        self.assertTrue(self.controller.acquire_exclusive("experiment"))
        signals = []
        self.controller.external_stop_requested.connect(lambda: signals.append(True))
        handle = self.controller.request_stop(owner="experiment")
        handle.result(2); handle.wait_drained(2)
        self.app.processEvents()
        self.assertEqual(signals, [])
        self._until(lambda: self.controller.release_exclusive("experiment"))
