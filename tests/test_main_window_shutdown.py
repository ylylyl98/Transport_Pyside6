import os
import time
import unittest
from unittest.mock import patch

if os.environ.get("TRANSPORT_OFFLINE_VERIFICATION") != "1":
    raise unittest.SkipTest("Requires offline wrapper")

from PySide6 import QtCore, QtGui, QtWidgets

from app.devices.attodry2100_adapter import (
    AttoDRY2100Snapshot,
    AttoDRY2100TemperatureSnapshot,
)
from app.keithley_modes import KEITHLEY_MODE_VOLTAGE_2W
from app.ui.main_window import MainWindow
from utils.config import cfg


class Recoverable2100Adapter:
    def __init__(self):
        self.fail_close = False
        self.fail_stop = False
        self.close_calls = 0
        self.stop_calls = 0
        self.field_active = False

    def connect(self):
        return "mock-2100"

    def close(self):
        self.close_calls += 1
        if self.fail_close:
            raise RuntimeError("simulated SDK close failure")

    def read_snapshot(self):
        return AttoDRY2100Snapshot(field_t=0.0)

    def read_temperature_snapshot(self):
        return AttoDRY2100TemperatureSnapshot(sample_temperature_k=4.0)

    def set_h_setpoint(self, target, stop_event=None):
        return target

    def start_field_control(self, target, stop_event=None):
        self.field_active = True
        return True

    def stop_field_control(self):
        self.stop_calls += 1
        if self.fail_stop:
            raise RuntimeError("simulated field Stop failure")
        self.field_active = False
        return True


class RecoverableGate:
    def __init__(self):
        self.fail_zero = True
        self.setpoint = 0.1
        self.closed = False

    def get_voltage_setpoint(self):
        return self.setpoint

    def set_voltage(self, value):
        if self.fail_zero:
            raise RuntimeError("simulated zero failure")
        self.setpoint = float(value)

    def is_output_enabled(self):
        return True

    def close(self):
        self.closed = True


class MockMonochromator:
    def __init__(self, *_args):
        self.connected = False

    def connect(self):
        self.connected = True

    def close(self):
        self.connected = False


class MainWindowShutdownRecoveryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def setUp(self):
        self.backend_patch = patch.object(cfg.setup, "magnet_backend", "2100")
        self.source_patch = patch.object(cfg.setup, "temperature_source", "2100")
        self.backend_patch.start(); self.source_patch.start()
        self.window = MainWindow()
        self.sdk = Recoverable2100Adapter()
        self.window.magnet2100._owner.adapter_factory = lambda _config: self.sdk

    def tearDown(self):
        self.sdk.fail_close = False
        self.sdk.fail_stop = False
        manager = self.window.device_manager
        gate = manager.sessions.get("g1")
        if isinstance(gate, RecoverableGate):
            gate.fail_zero = False
        self.window.magnet2100.shutdown(1.0)
        manager.shutdown()
        self.window.png_coordinator.shutdown()
        self.window.png_exporter.shutdown()
        self.window.deleteLater()
        self.app.processEvents()
        self.source_patch.stop(); self.backend_patch.stop()

    def wait_for(self, predicate):
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            self.app.processEvents()
            if predicate():
                return
            QtCore.QThread.msleep(5)
        self.fail("Expected controller acknowledgement did not arrive")

    def assert_other_controllers_can_connect(self):
        # This setup has no APS100 or LS335 owner; the SDK owner remains alive.
        self.assertFalse(hasattr(self.window.magnet1000, "_thread"))
        self.assertFalse(hasattr(self.window.lakeshore335, "_thread"))
        self.assertTrue(self.window.magnet2100._thread.isRunning())

    def assert_device_manager_can_reconnect(self):
        manager = self.window.device_manager
        for field in ("gate1", "gate2", "gate3", "daq_dev", "lockin", "drive_lockin"):
            setattr(manager.connections, field, "")
        manager.connections.mono = "MOCK::MONO"
        with patch("app.device_manager.SP2300", MockMonochromator):
            self.assertTrue(manager.connect_all())
            self.wait_for(lambda: manager._operation_thread is None)
        self.assertIsInstance(manager.sessions["mono"], MockMonochromator)
        self.assertTrue(manager.sessions["mono"].connected)

    def test_sdk_close_failure_keeps_other_owners_and_reconnection_available(self):
        window = self.window
        window.magnet2100.connect_async().result(1.0)
        self.app.processEvents()
        self.sdk.fail_close = True
        event = QtGui.QCloseEvent()
        with patch("app.ui.main_window.QtWidgets.QMessageBox.critical") as message:
            window.closeEvent(event)
        self.assertFalse(event.isAccepted())
        self.assertTrue(window.isEnabled())
        self.assertEqual(message.call_args.args[1], "Magnet shutdown not confirmed")
        self.assertTrue(window.magnet2100._thread.isRunning())
        self.assert_other_controllers_can_connect()
        self.assert_device_manager_can_reconnect()
        # The same SDK owner can retry its failed close once communication
        # recovers; no replacement controller or process is needed.
        self.sdk.fail_close = False
        self.assertTrue(window.magnet2100.shutdown(1.0))
        self.assertEqual(self.sdk.close_calls, 2)

    def test_zero_failure_preserves_sessions_and_all_controller_owners(self):
        window = self.window
        manager = window.device_manager
        gate = RecoverableGate()
        manager.sessions["g1"] = gate
        manager._connected_modes["g1"] = KEITHLEY_MODE_VOLTAGE_2W
        event = QtGui.QCloseEvent()
        with patch("app.ui.main_window.QtWidgets.QMessageBox.critical") as message:
            window.closeEvent(event)
        self.assertFalse(event.isAccepted())
        self.assertTrue(window.isEnabled())
        self.assertEqual(message.call_args.args[1], "Keithley shutdown not confirmed")
        self.assertIs(manager.sessions["g1"], gate)
        self.assertFalse(gate.closed)
        self.assertTrue(window.magnet2100._thread.isRunning())
        self.assert_other_controllers_can_connect()
        self.assertEqual(window.magnet2100.connect_async().result(1.0), "mock-2100")
        gate.fail_zero = False
        manager.shutdown()
        self.assertTrue(gate.closed)
        self.assertEqual(gate.setpoint, 0.0)
        self.assert_device_manager_can_reconnect()

    def test_successful_cleanup_blocks_new_work_while_exports_finish(self):
        event = QtGui.QCloseEvent()
        with (
            patch("app.ui.main_window.QtWidgets.QMessageBox.critical") as message,
            patch.object(self.window.tab_curve_compare, "prepare_shutdown", side_effect=[False, True]),
        ):
            self.window.closeEvent(event)
            self.assertFalse(event.isAccepted())
            self.assertFalse(self.window.isEnabled())
            self.assertTrue(self.window._png_close_pending)
            final_event = QtGui.QCloseEvent()
            self.window.closeEvent(final_event)
            self.assertTrue(final_event.isAccepted())
        message.assert_not_called()
        self.assertFalse(self.window.magnet2100._thread.isRunning())

    def test_active_2100_stops_before_gate_zero_failure_and_owner_remains_usable(self):
        window = self.window
        manager = window.device_manager
        gate = RecoverableGate()
        manager.sessions["g1"] = gate
        manager._connected_modes["g1"] = KEITHLEY_MODE_VOLTAGE_2W
        window.magnet2100.connect_async().result(1.0)
        window.magnet2100.set_h_setpoint_async(.01).result(1.0)
        window.magnet2100.start_field_control_async().result(1.0)
        self.assertTrue(self.sdk.field_active)
        shutdown = manager.shutdown

        def verify_stop_then_zero():
            self.assertFalse(self.sdk.field_active)
            self.assertEqual(self.sdk.stop_calls, 1)
            shutdown()

        event = QtGui.QCloseEvent()
        with (
            patch.object(manager, "shutdown", side_effect=verify_stop_then_zero),
            patch("app.ui.main_window.QtWidgets.QMessageBox.critical") as message,
        ):
            window.closeEvent(event)
        self.assertFalse(event.isAccepted())
        self.assertEqual(message.call_args.args[1], "Keithley shutdown not confirmed")
        self.assertEqual(self.sdk.close_calls, 0)
        self.assertTrue(window.magnet2100._thread.isRunning())
        self.assertFalse(gate.closed)
        self.assertTrue(window.isEnabled())
        # The failed close did not destroy the 2100 owner's ability to recover.
        window.magnet2100.set_h_setpoint_async(.02).result(1.0)
        window.magnet2100.start_field_control_async().result(1.0)
        self.assertTrue(self.sdk.field_active)

    def test_failed_2100_stop_preserves_all_owners_and_retry_close_can_finish(self):
        window = self.window
        window.magnet2100.connect_async().result(1.0)
        window.magnet2100.set_h_setpoint_async(.01).result(1.0)
        window.magnet2100.start_field_control_async().result(1.0)
        self.sdk.fail_stop = True
        event = QtGui.QCloseEvent()
        with (
            patch.object(window.device_manager, "shutdown") as manager_shutdown,
            patch("app.ui.main_window.QtWidgets.QMessageBox.critical") as message,
        ):
            window.closeEvent(event)
        self.assertFalse(event.isAccepted())
        self.assertEqual(message.call_args.args[1], "Magnet Stop not confirmed")
        manager_shutdown.assert_not_called()
        self.assertTrue(self.sdk.field_active)
        self.assertEqual(self.sdk.close_calls, 0)
        self.assertTrue(window.isEnabled())
        self.assertTrue(window.magnet2100._thread.isRunning())
        self.assert_other_controllers_can_connect()
        self.sdk.fail_stop = False
        final_event = QtGui.QCloseEvent()
        with patch("app.ui.main_window.QtWidgets.QMessageBox.critical") as message:
            window.closeEvent(final_event)
        message.assert_not_called()
        self.assertTrue(final_event.isAccepted())
        self.assertFalse(self.sdk.field_active)
        self.assertEqual(self.sdk.stop_calls, 2)
        self.assertEqual(self.sdk.close_calls, 1)
        self.assertFalse(window.magnet2100._thread.isRunning())


if __name__ == "__main__":
    unittest.main()
