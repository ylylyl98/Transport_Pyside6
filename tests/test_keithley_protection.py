from __future__ import annotations

import os
import math
import tempfile
import unittest
from threading import RLock
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6 import QtCore, QtTest, QtWidgets

from app.device_manager import DeviceManager, GateCurrentReadWorker, ManualControlWorker, ProtectionApplyWorker
from app.keithley_modes import KEITHLEY_MODE_OHM_4W, KEITHLEY_MODE_VOLTAGE_2W
from app.models import Connections
from app.ui.dock import ConnDock
from instruments.Keithley import Keithley2400Base
from instruments.instrument import InstrumentError, PyvisaInstrument


class FakeKeithley(Keithley2400Base):
    def __init__(self, current_compliance: float = 1e-7, max_voltage: float = 12.0):
        self._name = "fake Keithley"
        self._address = "GPIB::1"
        self._init_curr_comp = current_compliance
        self._max_source_voltage = max_voltage
        self._init_source_delay = 0.2
        self._operating_mode = KEITHLEY_MODE_VOLTAGE_2W
        self._output_values = {self.VOLT_OUTPUT: 0.0}
        self._input_values = {self.CURR_INPUT: None, self.RES_INPUT: None}
        self.lock = RLock()
        self.commands: list[str] = []
        self.reported_compliance = current_compliance
        self.reported_current_range = self.recommended_current_range(current_compliance)
        self.reported_voltage_range = max_voltage
        self.current_autorange = False
        self.error_queue: list[str] = []
        self.trip = False

    def _write(self, command: str, print_command=False):
        self.commands.append(command)
        if command.startswith(":SENS:CURR:PROT "):
            self.reported_compliance = float(command.split()[-1])
            self.reported_current_range = self.recommended_current_range(self.reported_compliance)
        elif command.startswith(":SOUR:VOLT:RANG "):
            self.reported_voltage_range = float(command.split()[-1])

    def _query(self, command: str, print_command=False, print_response=False):
        if command == ":SYST:ERR?":
            return self.error_queue.pop(0) if self.error_queue else '0,"No error"'
        return {
            ":SENS:CURR:PROT?": str(self.reported_compliance),
            ":SENS:CURR:RANG:AUTO?": "1" if self.current_autorange else "0",
            ":SENS:CURR:RANG?": str(self.reported_current_range),
            ":SOUR:VOLT:RANG?": str(self.reported_voltage_range),
            ":SENS:CURR:PROT:TRIP?": "1" if self.trip else "0",
            ":SOUR:VOLT:LEV?": "0",
        }[command]


class ConnectionSafetyKeithley(FakeKeithley):
    def __init__(self, existing_voltage: float, source_function: str = "VOLT"):
        super().__init__(current_compliance=1e-6, max_voltage=20.0)
        self._my_instr = None
        self.existing_voltage = float(existing_voltage)
        self.existing_source_function = source_function
        self.events: list[tuple[str, str]] = []
        self.fail_voltage_write = False

    def _query(self, command: str, print_command=False, print_response=False):
        self.events.append(("query", command))
        if command == "*IDN?":
            return "KEITHLEY INSTRUMENTS INC.,MODEL 2400,1,1"
        if command == ":SOUR:FUNC?":
            return self.existing_source_function
        if command == ":SOUR:VOLT:LEV?":
            return str(self.existing_voltage)
        return super()._query(command, print_command, print_response)

    def _write(self, command: str, print_command=False):
        self.events.append(("write", command))
        if command.startswith(":SOUR:VOLT:LEV "):
            if self.fail_voltage_write:
                raise RuntimeError("simulated ramp failure")
            self.existing_voltage = float(command.split()[-1])
        return super()._write(command, print_command)


class KeithleyProtectionDriverTests(unittest.TestCase):
    @staticmethod
    def _open_fake_visa(instance):
        instance._my_instr = object()
        return instance

    @staticmethod
    def _close_fake_visa(instance):
        instance._my_instr = None

    def test_connect_reads_and_safely_zeros_existing_voltage_without_output_toggle(self):
        driver = ConnectionSafetyKeithley(existing_voltage=0.12)
        with (
            patch.object(PyvisaInstrument, "connect", autospec=True, side_effect=self._open_fake_visa),
            patch.object(PyvisaInstrument, "close", autospec=True, side_effect=self._close_fake_visa),
            patch("app.utils.time.sleep"),
        ):
            driver.connect()

        level_query = driver.events.index(("query", ":SOUR:VOLT:LEV?"))
        level_writes = [
            index
            for index, event in enumerate(driver.events)
            if event[0] == "write" and event[1].startswith(":SOUR:VOLT:LEV ")
        ]
        clear_status = driver.events.index(("write", "*CLS"))
        self.assertTrue(level_writes)
        self.assertLess(level_query, level_writes[0])
        self.assertLess(level_writes[-1], clear_status)
        self.assertNotIn(("write", ":OUTP OFF"), driver.events)
        self.assertAlmostEqual(driver.existing_voltage, 0.0)
        self.assertAlmostEqual(driver.connection_start_voltage, 0.12)

    def test_connect_does_not_write_voltage_when_existing_setpoint_is_zero(self):
        driver = ConnectionSafetyKeithley(existing_voltage=0.0)
        with (
            patch.object(PyvisaInstrument, "connect", autospec=True, side_effect=self._open_fake_visa),
            patch.object(PyvisaInstrument, "close", autospec=True, side_effect=self._close_fake_visa),
        ):
            driver.connect()

        self.assertFalse(
            any(event[0] == "write" and event[1].startswith(":SOUR:VOLT:LEV ") for event in driver.events)
        )
        self.assertNotIn(("write", ":OUTP OFF"), driver.events)
        self.assertEqual(driver.connection_start_voltage, 0.0)

    def test_connect_aborts_if_existing_voltage_cannot_be_ramped(self):
        driver = ConnectionSafetyKeithley(existing_voltage=0.12)
        driver.fail_voltage_write = True
        with (
            patch.object(PyvisaInstrument, "connect", autospec=True, side_effect=self._open_fake_visa),
            patch.object(PyvisaInstrument, "close", autospec=True, side_effect=self._close_fake_visa),
            patch("app.utils.time.sleep"),
        ):
            with self.assertRaisesRegex(RuntimeError, "simulated ramp failure"):
                driver.connect()

        self.assertIsNone(driver._my_instr)
        self.assertNotIn(("write", ":OUTP OFF"), driver.events)

    def test_configuration_is_verified_before_output_is_enabled(self):
        driver = FakeKeithley(current_compliance=2e-7, max_voltage=12.0)
        driver.set_2wire_voltage_source_mode()

        self.assertNotIn(":OUTP OFF", driver.commands)
        self.assertEqual(driver.commands[-1], ":OUTP ON")
        self.assertLess(
            driver.commands.index(":SENS:CURR:PROT 2.000000e-07"),
            driver.commands.index(":OUTP ON"),
        )
        self.assertIn(":SENS:CURR:PROT:RSYN ON", driver.commands)
        self.assertFalse(any(command.startswith(":SENS:CURR:RANG ") for command in driver.commands))
        settings = driver.read_protection_settings()
        self.assertEqual(settings["max_source_voltage_v"], 12.0)
        self.assertAlmostEqual(settings["current_compliance_a"], 2e-7)

    def test_readback_mismatch_does_not_enable_output(self):
        driver = FakeKeithley(current_compliance=1e-7, max_voltage=10.0)

        def ignore_compliance(command: str, print_command=False):
            driver.commands.append(command)
            if command.startswith(":SOUR:VOLT:RANG "):
                driver.reported_voltage_range = float(command.split()[-1])

        driver._write = ignore_compliance
        driver.reported_compliance = 1e-3
        with self.assertRaisesRegex(InstrumentError, "verification failed"):
            driver.set_2wire_voltage_source_mode()
        self.assertNotIn(":OUTP ON", driver.commands)

    def test_programmed_voltage_cannot_exceed_profile(self):
        driver = FakeKeithley(max_voltage=5.0)
        driver._write_voltage(-5.0)
        self.assertEqual(driver.commands[-1], ":SOUR:VOLT:LEV -5.000000e+00")
        with self.assertRaisesRegex(InstrumentError, "exceeds the configured"):
            driver._write_voltage(5.01)

    def test_invalid_limits_are_rejected(self):
        with self.assertRaises(ValueError):
            Keithley2400Base._validated_current_compliance(0.0)
        with self.assertRaises(ValueError):
            Keithley2400Base._validated_source_voltage_limit(float("nan"))

    def test_live_apply_uses_range_sync_without_enabling_output(self):
        driver = FakeKeithley(current_compliance=500e-9, max_voltage=20.0)
        driver.commands.clear()

        settings = driver.apply_protection_settings(10e-6, 12.0)

        self.assertEqual(
            driver.commands,
            [
                "*CLS",
                ":SENS:CURR:RANG:AUTO OFF",
                ":SENS:CURR:PROT:RSYN ON",
                ":SENS:CURR:PROT 1.000000e-05",
                ":SOUR:VOLT:RANG 1.200000e+01",
            ],
        )
        self.assertNotIn(":OUTP ON", driver.commands)
        self.assertAlmostEqual(settings["current_compliance_a"], 10e-6)
        self.assertAlmostEqual(settings["current_range_a"], 10e-6)

    def test_error_queue_824_fails_verification(self):
        driver = FakeKeithley(current_compliance=500e-9, max_voltage=20.0)
        driver.error_queue.append('+824,"Cannot exceed compliance range"')

        with self.assertRaisesRegex(InstrumentError, "824"):
            driver.apply_protection_settings(10e-6, 12.0)


class ReadbackSession:
    def get_voltage_setpoint(self):
        return 1.0

    def acquire(self):
        return {"voltage": 1.0, "current": 1e-7}

    def read_protection_settings(self):
        return {
            "max_source_voltage_v": 10.0,
            "current_compliance_a": 1e-7,
            "current_compliance_tripped": True,
        }


class CaptureEmitter:
    def __init__(self):
        self.events = []

    def emit(self, *args):
        self.events.append(args)


class ConnectedKeithleyStub:
    instances = []

    def __init__(self, name, address, curr_comp, max_source_voltage):
        self.name = name
        self.address = address
        self.curr_comp = curr_comp
        self.max_source_voltage = max_source_voltage
        self.setpoint = 0.0
        self.closed = False
        self.__class__.instances.append(self)

    def connect(self):
        return self

    def close(self):
        self.closed = True

    def read_protection_settings(self, include_trip=True):
        return {
            "max_source_voltage_v": self.max_source_voltage,
            "source_voltage_range_v": self.max_source_voltage,
            "current_range_a": Keithley2400Base.recommended_current_range(self.curr_comp),
            "current_autorange": False,
            "current_compliance_a": self.curr_comp,
            "current_compliance_tripped": False if include_trip else None,
        }

    def get_voltage_setpoint(self):
        return self.setpoint

    def apply_protection_settings(self, current_compliance, max_voltage):
        self.curr_comp = float(current_compliance)
        self.max_source_voltage = float(max_voltage)
        return self.read_protection_settings(include_trip=False)


class ManualGateSession:
    def __init__(self, setpoint=0.0):
        self.setpoint = float(setpoint)
        self.writes = []
        self.setpoint_queries = 0
        self.acquire_calls = 0
        self.protection_calls = 0
        self.on_write = None

    def get_voltage_setpoint(self):
        self.setpoint_queries += 1
        return self.setpoint

    def set_voltage_fast(self, value):
        self.setpoint = float(value)
        self.writes.append(self.setpoint)
        if self.on_write is not None:
            self.on_write(self.setpoint)

    def set_voltage(self, value):
        self.set_voltage_fast(value)

    def acquire(self):
        self.acquire_calls += 1
        return {"voltage": self.setpoint, "current": 1e-9}

    def read_protection_settings(self):
        self.protection_calls += 1
        return {}

class ProfileFailingKeithleyStub(ConnectedKeithleyStub):
    def apply_protection_settings(self, current_compliance, max_voltage):
        if not (
            math.isclose(float(current_compliance), 1e-6)
            and math.isclose(float(max_voltage), 20.0)
        ):
            raise RuntimeError("simulated profile failure")
        return super().apply_protection_settings(current_compliance, max_voltage)


class KeithleyProtectionIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
        cls.settings_dir = tempfile.TemporaryDirectory()
        QtCore.QSettings.setPath(
            QtCore.QSettings.Format.IniFormat,
            QtCore.QSettings.Scope.UserScope,
            cls.settings_dir.name,
        )

    @classmethod
    def tearDownClass(cls):
        cls.settings_dir.cleanup()

    def test_each_gate_has_an_independent_persisted_profile(self):
        dock = ConnDock()
        dock._protection_controls["g1"][0].setValue(5.0)
        dock._protection_controls["g1"][1].setValue(10e-9)
        dock._protection_controls["g2"][0].setValue(10.0)
        dock._protection_controls["g2"][1].setValue(100e-9)
        dock._protection_controls["g3"][0].setValue(15.0)
        dock._protection_controls["g3"][1].setValue(1e-6)
        dock.save_settings()

        connections = dock.to_models()[0]
        self.assertEqual(connections.gate1_max_voltage_v, 5.0)
        self.assertAlmostEqual(connections.gate2_current_compliance_a, 100e-9)
        self.assertEqual(connections.gate3_max_voltage_v, 15.0)
        dock.close()

        restored = ConnDock()
        restored.load_settings()
        self.assertEqual(restored._protection_controls["g1"][0].value(), 5.0)
        self.assertAlmostEqual(restored._protection_controls["g1"][1].value(), 10e-9)
        restored.close()

    def test_protection_voltage_limit_accepts_typed_200_v_in_plain_units(self):
        dock = ConnDock()
        voltage = dock._protection_controls["g1"][0]

        voltage.setValue(2.0)
        self.assertEqual(voltage.text(), "2 V")
        voltage.lineEdit().selectAll()
        QtTest.QTest.keyClicks(voltage.lineEdit(), "200")
        QtTest.QTest.keyClick(voltage.lineEdit(), QtCore.Qt.Key.Key_Return)
        self.assertEqual(voltage.value(), 200.0)
        self.assertEqual(voltage.text(), "200 V")
        voltage.setValue(12.345678901)
        self.assertEqual(voltage.text(), "12.345678901 V")
        self.assertEqual(voltage.maximum(), 200.0)
        self.assertEqual(
            voltage.buttonSymbols(),
            QtWidgets.QAbstractSpinBox.ButtonSymbols.UpDownArrows,
        )
        voltage.setValue(200.0)
        dock._update_protection_controls()
        self.assertEqual(dock.sp_manual_g1.maximum(), 20.0)
        dock.close()

    def test_ohms_mode_disables_voltage_source_protection_controls(self):
        dock = ConnDock()
        dock._set_combo_data(dock.cbo_g1_mode, KEITHLEY_MODE_OHM_4W)
        dock._update_protection_controls()
        self.assertFalse(dock._protection_controls["g1"][0].isEnabled())
        self.assertFalse(dock._protection_controls["g1"][1].isEnabled())
        self.assertIn("Inactive", dock._protection_status_labels["g1"].text())
        dock.close()

    def test_connected_gate_allows_live_limits_and_manual_range_tracks_profile(self):
        connections = Connections()
        manager = DeviceManager(connections)
        dock = ConnDock(manager)
        dock._protection_controls["g1"][0].setValue(6.0)
        dock._update_protection_controls()
        self.assertEqual(dock.sp_manual_g1.minimum(), -6.0)
        self.assertEqual(dock.sp_manual_g1.maximum(), 6.0)

        manager.sessions["g1"] = object()
        manager.states["g1"] = "ok"
        dock._update_protection_controls()
        self.assertTrue(dock._protection_controls["g1"][0].isEnabled())
        self.assertTrue(dock._protection_controls["g1"][1].isEnabled())
        self.assertTrue(dock._protection_apply_buttons["g1"].isEnabled())
        dock.close()

    def test_device_manager_reads_distinct_profiles(self):
        connections = Connections(
            gate1_max_voltage_v=5.0,
            gate2_max_voltage_v=10.0,
            gate3_max_voltage_v=15.0,
            gate1_current_compliance_a=10e-9,
            gate2_current_compliance_a=100e-9,
            gate3_current_compliance_a=1e-6,
        )
        manager = DeviceManager(connections)
        self.assertEqual(manager._protection_for("g1"), (5.0, 10e-9))
        self.assertEqual(manager._protection_for("g2"), (10.0, 100e-9))
        self.assertEqual(manager._protection_for("g3"), (15.0, 1e-6))

    def test_device_manager_applies_saved_profile_after_default_connection(self):
        ConnectedKeithleyStub.instances.clear()
        connections = Connections(
            gate1="GPIB::11",
            gate1_max_voltage_v=7.0,
            gate1_current_compliance_a=25e-9,
        )
        manager = DeviceManager(connections)
        emitter = CaptureEmitter()
        with patch("app.device_manager.Keithley2400VoltMode", ConnectedKeithleyStub):
            manager._connect_keithley("g1", emitter)

        session = ConnectedKeithleyStub.instances[-1]
        self.assertEqual(session.max_source_voltage, 7.0)
        self.assertAlmostEqual(session.curr_comp, 25e-9)
        self.assertIs(manager.sessions["g1"], session)
        self.assertEqual(emitter.events[-1][0:2], ("g1", "ok"))
        self.assertIn("2.5e-08 A current compliance", emitter.events[-1][2])
        self.assertIn("applied and verified", emitter.events[-1][2])
        self.assertTrue(manager.protection_is_applied("g1"))

    def test_failed_saved_profile_keeps_verified_defaults_and_allows_retry(self):
        ProfileFailingKeithleyStub.instances.clear()
        connections = Connections(
            gate1="GPIB::11",
            gate1_max_voltage_v=7.0,
            gate1_current_compliance_a=25e-9,
        )
        manager = DeviceManager(connections)
        emitter = CaptureEmitter()
        with patch("app.device_manager.Keithley2400VoltMode", ProfileFailingKeithleyStub):
            manager._connect_keithley("g1", emitter)

        session = ProfileFailingKeithleyStub.instances[-1]
        self.assertIs(manager.sessions["g1"], session)
        self.assertEqual(session.max_source_voltage, 20.0)
        self.assertAlmostEqual(session.curr_comp, 1e-6)
        self.assertFalse(manager.protection_is_applied("g1"))
        self.assertEqual(emitter.events[-1][0:2], ("g1", "ok"))
        self.assertIn("Saved profile apply failed", emitter.events[-1][2])
        self.assertIn("defaults remain active", emitter.events[-1][2])

    def test_successful_connection_profile_enables_manual_voltage_control(self):
        ConnectedKeithleyStub.instances.clear()
        connections = Connections(
            gate1="GPIB::11",
            gate1_max_voltage_v=7.0,
            gate1_current_compliance_a=25e-9,
        )
        manager = DeviceManager(connections)
        with patch("app.device_manager.Keithley2400VoltMode", ConnectedKeithleyStub):
            manager._connect_keithley("g1", CaptureEmitter())
        manager.states["g1"] = "ok"
        dock = ConnDock(manager)

        dock._update_manual_controls()

        self.assertTrue(dock.sp_manual_g1.isEnabled())
        self.assertTrue(dock.btn_manual_g1_set.isEnabled())
        dock.close()

    def test_manual_ramp_completion_keeps_verified_nonzero_voltage(self):
        dock = ConnDock()
        dock.sp_manual_g1.setValue(0.5)

        dock._on_manual_control_finished(
            "g1",
            True,
            "G1 ramped to 0.5 V from 0 V.",
            {"target": 0.5, "set_voltage": 0.5, "measured_voltage": 0.4999},
        )

        self.assertAlmostEqual(dock.sp_manual_g1.value(), 0.5)
        self.assertFalse(dock._manual_gate_dirty["g1"])
        dock.close()

    def test_first_gate_result_message_does_not_resize_the_sidebar(self):
        with patch.object(ConnDock, "_start_scan"):
            dock = ConnDock()
        scroll = QtWidgets.QScrollArea()
        scroll.resize(430, 700)
        scroll.setWidgetResizable(True)
        scroll.setWidget(dock)
        scroll.show()
        self.app.processEvents()
        before = (dock.height(), dock.lbl_manual_hint.height(), scroll.verticalScrollBar().maximum())

        dock._on_manual_control_finished(
            "g1",
            True,
            "G1 ramped to 0.1 V from 0 V.",
            {"target": 0.1, "set_voltage": 0.1},
        )
        self.app.processEvents()
        after = (dock.height(), dock.lbl_manual_hint.height(), scroll.verticalScrollBar().maximum())

        self.assertEqual(after, before)
        self.assertEqual(dock.lbl_manual_hint.toolTip(), "G1 ramped to 0.1 V from 0 V.")
        scroll.close()
        dock.close()

    def test_readback_does_not_overwrite_an_unapplied_user_target(self):
        dock = ConnDock()
        dock.sp_manual_g1.setValue(0.75)

        dock._on_gate_currents_read(
            {"g1": {"connected": True, "mode": "voltage_2w", "set_voltage": 0.25}},
            "Gate readback refresh complete.",
        )

        self.assertAlmostEqual(dock.sp_manual_g1.value(), 0.75)
        dock.close()

    def test_gate_read_button_requests_only_the_selected_gate(self):
        connections = Connections(gate1="GPIB::1")
        manager = DeviceManager(connections)
        dock = ConnDock(manager)

        with patch.object(manager, "read_gate_currents", return_value=True) as read_gate_currents:
            dock._on_read_gate("g1")

        read_gate_currents.assert_called_once_with(names=("g1",))
        self.assertEqual(dock.lbl_gate_readback_status.text(), "Reading G1...")
        dock.close()

    def test_gate_step_changes_target_and_uses_safe_ramp(self):
        manager = DeviceManager(Connections())
        dock = ConnDock(manager)
        dock.sp_manual_g1.setValue(0.2)

        with patch.object(manager, "ramp_gate", return_value=True) as ramp_gate:
            dock._on_manual_gate_step("g1", 0.1)

        self.assertAlmostEqual(dock.sp_manual_g1.value(), 0.3)
        ramp_gate.assert_called_once_with("g1", 0.3)
        dock.close()

    def test_manual_gate_worker_coalesces_rapid_steps_without_measurement_reads(self):
        session = ManualGateSession()
        worker = ManualControlWorker("g1", session, 0.1)
        updated = False

        def queue_rapid_clicks(_value):
            nonlocal updated
            if updated:
                return
            updated = True
            for target in (0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0):
                worker.update_target(target)

        session.on_write = queue_rapid_clicks
        with patch("app.device_manager.time.sleep"):
            worker.run()

        self.assertTrue(worker.success)
        self.assertAlmostEqual(session.setpoint, 1.0)
        self.assertAlmostEqual(worker.target, 1.0)
        self.assertEqual(session.acquire_calls, 0)
        self.assertEqual(session.protection_calls, 0)
        self.assertEqual(session.setpoint_queries, 2)
        self.assertTrue(all(abs(b - a) <= 0.100000001 for a, b in zip([0.0, *session.writes], session.writes)))

    def test_zero_replaces_an_active_gate_target_and_reverses_safely(self):
        session = ManualGateSession(0.5)
        worker = ManualControlWorker("g1", session, 0.8)
        zero_requested = False

        def request_zero(_value):
            nonlocal zero_requested
            if not zero_requested:
                zero_requested = True
                worker.update_target(0.0)

        session.on_write = request_zero
        with patch("app.device_manager.time.sleep"):
            worker.run()

        self.assertTrue(worker.success)
        self.assertAlmostEqual(session.setpoint, 0.0)
        self.assertLessEqual(max(session.writes), 0.600000001)
        self.assertTrue(all(abs(b - a) <= 0.050000001 for a, b in zip(session.writes, session.writes[1:])))

    def test_active_gate_worker_accepts_a_new_target_and_keeps_its_controls_enabled(self):
        connections = Connections(gate1="GPIB::1")
        manager = DeviceManager(connections)
        manager.sessions["g1"] = ConnectedKeithleyStub("g1", "GPIB::1", 1e-7, 2.0)
        manager.states["g1"] = "ok"
        manager._connected_modes["g1"] = KEITHLEY_MODE_VOLTAGE_2W
        manager._connected_protections["g1"] = manager._protection_for("g1")

        class ActiveWorker:
            name = "g1"

            def __init__(self):
                self.targets = []

            @staticmethod
            def isRunning():
                return True

            def update_target(self, target):
                self.targets.append(float(target))

        active_worker = ActiveWorker()
        manager._manual_worker = active_worker
        dock = ConnDock(manager)
        dock._update_manual_controls()

        self.assertTrue(dock.sp_manual_g1.isEnabled())
        self.assertTrue(dock._manual_gate_step_buttons["g1"][1].isEnabled())
        self.assertFalse(dock._manual_gate_read_buttons["g1"].isEnabled())
        self.assertTrue(manager.ramp_gate("g1", 0.4))
        self.assertEqual(active_worker.targets, [0.4])
        manager._manual_worker = None
        dock.close()

    def test_failed_gate_ramp_restores_target_to_confirmed_setpoint(self):
        dock = ConnDock()
        dock.sp_manual_g1.setValue(0.8)

        dock._on_manual_control_finished(
            "g1",
            False,
            "G1 manual control failed: simulated error",
            {"target": 0.8, "set_voltage": 0.3, "error": "simulated error"},
        )

        self.assertAlmostEqual(dock.sp_manual_g1.value(), 0.3)
        self.assertFalse(dock._manual_gate_dirty["g1"])
        dock.close()

    def test_compliance_trip_is_informational_and_does_not_block_ramp_controls(self):
        connections = Connections(gate1="GPIB::1")
        manager = DeviceManager(connections)
        manager.sessions["g1"] = ConnectedKeithleyStub("g1", "GPIB::1", 1e-6, 20.0)
        manager.states["g1"] = "ok"
        manager._connected_modes["g1"] = KEITHLEY_MODE_VOLTAGE_2W
        manager._connected_protections["g1"] = manager._protection_for("g1")
        dock = ConnDock(manager)

        dock._update_manual_controls()

        self.assertTrue(dock.sp_manual_g1.isEnabled())
        self.assertTrue(dock.btn_manual_g1_set.isEnabled())
        self.assertTrue(dock._manual_gate_step_buttons["g1"][0].isEnabled())
        self.assertTrue(dock._manual_gate_step_buttons["g1"][1].isEnabled())
        self.assertTrue(dock.btn_manual_g1_zero.isEnabled())
        with patch.object(manager, "_start_manual_worker") as start_worker:
            self.assertTrue(manager.ramp_gate("g1", 0.5))
        start_worker.assert_called_once_with("g1", manager.sessions["g1"], 0.5)
        dock.close()

    def test_live_apply_updates_only_the_selected_gate_profile(self):
        connections = Connections(
            gate1_max_voltage_v=7.0,
            gate1_current_compliance_a=25e-9,
            gate2_max_voltage_v=9.0,
            gate2_current_compliance_a=50e-9,
        )
        manager = DeviceManager(connections)
        g1 = ConnectedKeithleyStub("g1", "GPIB::1", 1e-6, 20.0)
        g2 = ConnectedKeithleyStub("g2", "GPIB::2", 1e-6, 20.0)
        manager.sessions["g1"] = g1
        manager.sessions["g2"] = g2
        manager.states["g1"] = manager.states["g2"] = "ok"
        manager._connected_modes["g1"] = manager._connected_modes["g2"] = KEITHLEY_MODE_VOLTAGE_2W
        manager._connected_protections["g1"] = manager._connected_protections["g2"] = (20.0, 1e-6)

        worker = ProtectionApplyWorker("g1", g1, 7.0, 25e-9)
        worker.run()
        manager._protection_worker = worker
        manager._finish_protection_apply()

        self.assertTrue(worker.success)
        self.assertTrue(manager.protection_is_applied("g1"))
        self.assertFalse(manager.protection_is_applied("g2"))
        self.assertEqual(g2.max_source_voltage, 20.0)
        self.assertAlmostEqual(g2.curr_comp, 1e-6)

    def test_protection_edit_does_not_disconnect_live_session(self):
        connections = Connections(gate1="GPIB::1")
        manager = DeviceManager(connections)
        session = ConnectedKeithleyStub("g1", "GPIB::1", 1e-6, 20.0)
        manager.sessions["g1"] = session
        manager.states["g1"] = "ok"
        manager._connected_addresses["g1"] = "GPIB::1"
        manager._connected_modes["g1"] = KEITHLEY_MODE_VOLTAGE_2W
        manager._connected_protections["g1"] = (20.0, 1e-6)

        connections.gate1_current_compliance_a = 25e-9
        manager.sync_addresses()

        self.assertIs(manager.sessions["g1"], session)
        self.assertFalse(session.closed)
        self.assertFalse(manager.needs_reconnect("g1"))
        self.assertFalse(manager.protection_is_applied("g1"))

    def test_live_apply_requires_zero_setpoint(self):
        session = ConnectedKeithleyStub("g1", "GPIB::1", 1e-6, 20.0)
        session.setpoint = 0.5
        worker = ProtectionApplyWorker("g1", session, 7.0, 25e-9)

        worker.run()

        self.assertFalse(worker.success)
        self.assertIn("Ramp it to 0 V", worker.message)
        self.assertEqual(session.max_source_voltage, 20.0)
        self.assertAlmostEqual(session.curr_comp, 1e-6)

    def test_gate_readback_reports_compliance_trip(self):
        worker = GateCurrentReadWorker(
            {"g1": ReadbackSession(), "g2": None, "g3": None},
            {"g1": KEITHLEY_MODE_VOLTAGE_2W, "g2": "", "g3": ""},
        )
        worker.run()
        self.assertTrue(worker.readbacks["g1"]["current_compliance_tripped"])
        self.assertIn("current compliance reached", worker.message)
        self.assertIn("measured 1e-07 A", worker.message)
        self.assertIn("instrument compliance 1e-07 A", worker.message)


if __name__ == "__main__":
    unittest.main()
