import os
import unittest
from unittest.mock import patch
if os.environ.get("TRANSPORT_OFFLINE_VERIFICATION") != "1":
    raise unittest.SkipTest("Requires offline verification wrapper")
from PySide6 import QtCore, QtTest, QtWidgets
from app.device_manager import DeviceManager, GateStepWorker
from app.models import Connections
from app.ui.dock import ConnDock
from test_keithley_protection import ManualGateSession, ConnectedKeithleyStub


class GateStepTests(unittest.TestCase):
    def test_step_starts_at_live_setpoint(self):
        session = ManualGateSession(2.0)
        worker = GateStepWorker("g1", session, 0.1, 20.0)
        worker.run()
        self.assertTrue(worker.success, worker.message)
        self.assertAlmostEqual(session.setpoint, 2.1)
        self.assertEqual(len(session.writes), 1)

    def test_burst_combines_targets_with_only_start_and_end_queries(self):
        session = ManualGateSession(0.0)
        worker = GateStepWorker("g1", session, 0.1, 20.0)
        def clicked(value):
            if len(session.writes) == 1:
                worker.add_step(0.1)
                worker.add_step(0.1)
                worker.add_step(-0.1)
        session.on_write = clicked
        with patch.object(session, "get_voltage_setpoint", wraps=session.get_voltage_setpoint) as read:
            worker.run()
        self.assertTrue(worker.success, worker.message)
        self.assertAlmostEqual(session.setpoint, 0.2)
        self.assertEqual(read.call_count, 2)
        self.assertTrue(all(abs(b-a) <= 0.100001 for a,b in zip([0.0]+session.writes, session.writes)))

    def test_zero_replaces_pending_steps_and_preserves_safe_increment(self):
        session = ManualGateSession(0.5)
        worker = GateStepWorker("g1", session, 0.1, 20.0)
        def clicked(value):
            if len(session.writes) == 1:
                worker.update_target(0.0)
        session.on_write = clicked
        worker.run()
        self.assertTrue(worker.success, worker.message)
        self.assertAlmostEqual(session.setpoint, 0.0)
        self.assertTrue(all(abs(b-a) <= 0.050001 for a,b in zip(session.writes,session.writes[1:])))

    def test_invalid_or_over_limit_target_does_not_write(self):
        for delta in (float("nan"), float("inf"), 0.1):
            session = ManualGateSession(20.0)
            worker = GateStepWorker("g1", session, delta, 20.0)
            worker.run()
            self.assertFalse(worker.success)
            self.assertEqual(session.writes, [])

    def test_cancelled_step_does_not_query_or_write(self):
        session = ManualGateSession()
        worker = GateStepWorker("g1", session, 0.1, 20.0)
        worker.request_cancel()
        with patch.object(session, "get_voltage_setpoint", wraps=session.get_voltage_setpoint) as read:
            worker.run()
        self.assertEqual(read.call_count, 0)
        self.assertEqual(session.writes, [])

    def test_read_failure_never_falls_back_to_cached_voltage(self):
        session = ManualGateSession(5.0)
        worker = GateStepWorker("g1", session, 0.1, 20.0)
        with patch.object(session, "get_voltage_setpoint", side_effect=RuntimeError("timeout")):
            worker.run()
        self.assertFalse(worker.success)
        self.assertEqual(session.writes, [])
        self.assertIsNone(worker.gate_readback["set_voltage"])

    def test_target_arriving_during_confirmation_is_not_lost(self):
        session = ManualGateSession()
        worker = GateStepWorker("g1", session, 0.1, 20.0)
        def read():
            session.setpoint_queries += 1
            if session.setpoint_queries == 2:
                worker.add_step(0.1)
            return session.setpoint
        session.get_voltage_setpoint = read
        worker.run()
        self.assertTrue(worker.success, worker.message)
        self.assertAlmostEqual(session.setpoint, 0.2)

    def test_write_failure_is_not_reported_as_confirmed_voltage(self):
        session = ManualGateSession()
        worker = GateStepWorker("g1", session, 0.1, 20.0)
        with patch.object(session, "set_voltage_fast", side_effect=RuntimeError("write failed")):
            worker.run()
        self.assertFalse(worker.success)
        self.assertIsNone(worker.gate_readback["set_voltage"])


    def test_cancellation_during_confirmation_is_not_success(self):
        session = ManualGateSession()
        worker = GateStepWorker("g1", session, 0.1, 20.0)
        def read():
            session.setpoint_queries += 1
            if session.setpoint_queries == 2:
                worker.request_cancel()
            return session.setpoint
        session.get_voltage_setpoint = read
        worker.run()
        self.assertFalse(worker.success)



class ManualUiTests(unittest.TestCase):
    def setUp(self):
        self.app = QtWidgets.QApplication.instance()
        self.manager = DeviceManager(Connections(gate1="GPIB::1"))
        self.session = ConnectedKeithleyStub("g1", "GPIB::1", 1e-6, 20.0)
        self.manager.sessions["g1"] = self.session
        self.manager.states["g1"] = "ok"
        self.manager._connected_protections["g1"] = self.manager._protection_for("g1")
        with patch.object(ConnDock, "_start_scan"):
            self.dock = ConnDock(self.manager)

    def tearDown(self):
        self.manager._manual_readback_timer.stop()
        self.dock.close()

    def test_step_does_not_use_unapplied_target_editor(self):
        self.dock.sp_manual_g1.setValue(5.0)
        with patch.object(self.manager, "step_gate", return_value=True) as command:
            self.dock._on_manual_gate_step("g1", 0.1)
        command.assert_called_once_with("g1", 0.1)
        self.assertNotEqual(self.dock.sp_manual_g1.value(), 5.1)

    def test_enter_submits_typed_absolute_target(self):
        self.dock.sp_manual_g1.lineEdit().setText("1.25")
        with patch.object(self.manager, "ramp_gate", return_value=True) as command:
            self.dock.sp_manual_g1.lineEdit().returnPressed.emit()
        command.assert_called_once_with("g1", 1.25)

    def test_new_editor_value_survives_old_command_completion(self):
        self.dock.sp_manual_g1.setValue(3.0)
        self.dock._on_manual_control_finished("g1", True, "Done", {"target": 0.1, "set_voltage": 0.1})
        self.assertEqual(self.dock.sp_manual_g1.value(), 3.0)

    def test_pending_target_does_not_overwrite_measured_voltage(self):
        self.dock.gate_readback_labels["g1"]["measured_voltage"].setText("Voltage: 0.05 V")
        self.dock._on_manual_gate_progress("g1", 0.2, 0.1)
        self.assertEqual(self.dock.gate_readback_labels["g1"]["measured_voltage"].text(), "Voltage: 0.05 V")

    def test_measurement_blocks_steps(self):
        self.manager._in_use.add("g1")
        self.assertFalse(self.manager.step_gate("g1", 0.1))
        self.assertIsNone(self.manager._manual_worker)

    def test_step_refreshes_measured_readings_automatically(self):
        session = ManualGateSession(0.5)
        self.manager.sessions["g1"] = session
        spy = QtTest.QSignalSpy(self.manager.gate_currents_read)
        self.dock.sp_manual_g1.setValue(5.0)
        self.dock._on_manual_gate_step("g1", 0.1)
        deadline = QtCore.QDeadlineTimer(3000)
        while spy.count() == 0 and not deadline.hasExpired():
            QtTest.QTest.qWait(10)
        self.assertEqual(spy.count(), 1)
        self.assertAlmostEqual(session.setpoint, 0.6)
        self.assertEqual(session.acquire_calls, 1)
        self.assertAlmostEqual(self.dock.sp_manual_g1.value(), 0.6)
        self.assertNotIn("--", self.dock.gate_readback_labels["g1"]["measured_voltage"].text())

    def test_selected_step_size_updates_buttons(self):
        self.dock.sp_manual_step.setValue(0.025)
        self.assertEqual(self.dock._manual_gate_step_buttons["g1"][1].text(), "+0.025 V")

    def test_busy_deferred_readback_is_retained(self):
        self.manager._manual_readback_names.add("g1")
        with patch.object(self.manager, "read_gate_currents", return_value=False):
            self.manager._refresh_manual_readbacks()
        self.assertEqual(self.manager._manual_readback_names, {"g1"})
        self.assertTrue(self.manager._manual_readback_timer.isActive())

    def test_emergency_waits_for_inflight_readback(self):
        from threading import Event
        class ReadingSession(ManualGateSession):
            entered = Event()
            release_read = Event()
            def acquire(self):
                self.entered.set()
                self.release_read.wait(2)
                return super().acquire()
        session = ReadingSession(0.0)
        self.manager.sessions["g1"] = session
        self.assertTrue(self.manager.read_gate_currents(quiet=True, names=("g1",)))
        self.assertTrue(session.entered.wait(1))
        try:
            self.manager.emergency_stop([])
            self.assertIsNone(self.manager._emergency_worker)
            self.assertEqual(self.manager._pending_emergency_daq_channels, [])
        finally:
            session.release_read.set()
            deadline = QtCore.QDeadlineTimer(3000)
            while self.manager.is_busy() and not deadline.hasExpired():
                QtTest.QTest.qWait(10)
            self.manager._manual_readback_timer.stop()

    def test_pending_emergency_rejects_new_steps(self):
        self.manager._pending_emergency_daq_channels = []
        self.assertFalse(self.manager.step_gate("g1", 0.1))
        self.assertIsNone(self.manager._manual_worker)

    def test_completed_but_unhandled_worker_still_owns_device(self):
        worker = GateStepWorker("g1", ManualGateSession(), 0.1, 20.0)
        self.manager._manual_worker = worker
        try:
            accepted, blocked = self.manager.mark_in_use(["g1"])
            self.assertFalse(accepted)
            self.assertEqual(blocked, ["hardware operation"])
        finally:
            self.manager._manual_worker = None

    def test_repeated_emergency_does_not_replace_unhandled_owner(self):
        owner = QtCore.QThread()
        self.manager._emergency_worker = owner
        try:
            with patch("app.device_manager.EmergencyRampWorker.start"):
                self.manager.emergency_stop([])
            self.assertIs(self.manager._emergency_worker, owner)
        finally:
            self.manager._emergency_worker = None
