from __future__ import annotations

import os
import tempfile
import unittest
from threading import RLock
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6 import QtCore, QtWidgets

from app.device_manager import DeviceManager, GateCurrentReadWorker
from app.keithley_modes import KEITHLEY_MODE_OHM_4W, KEITHLEY_MODE_VOLTAGE_2W
from app.models import Connections
from app.ui.dock import ConnDock
from instruments.Keithley import Keithley2400Base
from instruments.instrument import InstrumentError


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
        self.reported_voltage_range = max_voltage
        self.trip = False

    def _write(self, command: str, print_command=False):
        self.commands.append(command)
        if command.startswith(":SENS:CURR:PROT "):
            self.reported_compliance = float(command.split()[-1])
        elif command.startswith(":SOUR:VOLT:RANG "):
            self.reported_voltage_range = float(command.split()[-1])

    def _query(self, command: str, print_command=False, print_response=False):
        return {
            ":SENS:CURR:PROT?": str(self.reported_compliance),
            ":SOUR:VOLT:RANG?": str(self.reported_voltage_range),
            ":SENS:CURR:PROT:TRIP?": "1" if self.trip else "0",
            ":SOUR:VOLT:LEV?": "0",
        }[command]


class KeithleyProtectionDriverTests(unittest.TestCase):
    def test_configuration_is_verified_before_output_is_enabled(self):
        driver = FakeKeithley(current_compliance=2e-7, max_voltage=12.0)
        driver.set_2wire_voltage_source_mode()

        self.assertEqual(driver.commands[0], ":OUTP OFF")
        self.assertEqual(driver.commands[-1], ":OUTP ON")
        self.assertLess(
            driver.commands.index(":SENS:CURR:PROT 2.000000e-07"),
            driver.commands.index(":OUTP ON"),
        )
        settings = driver.read_protection_settings()
        self.assertEqual(settings["max_source_voltage_v"], 12.0)
        self.assertAlmostEqual(settings["current_compliance_a"], 2e-7)

    def test_readback_mismatch_leaves_output_disabled(self):
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
            "current_compliance_a": self.curr_comp,
            "current_compliance_tripped": False if include_trip else None,
        }


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

    def test_ohms_mode_disables_voltage_source_protection_controls(self):
        dock = ConnDock()
        dock._set_combo_data(dock.cbo_g1_mode, KEITHLEY_MODE_OHM_4W)
        dock._update_protection_controls()
        self.assertFalse(dock._protection_controls["g1"][0].isEnabled())
        self.assertFalse(dock._protection_controls["g1"][1].isEnabled())
        self.assertIn("Inactive", dock._protection_status_labels["g1"].text())
        dock.close()

    def test_connected_gate_locks_limits_and_manual_range_tracks_profile(self):
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
        self.assertFalse(dock._protection_controls["g1"][0].isEnabled())
        self.assertFalse(dock._protection_controls["g1"][1].isEnabled())
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

    def test_device_manager_passes_the_selected_profile_to_the_driver(self):
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

    def test_gate_readback_reports_compliance_trip(self):
        worker = GateCurrentReadWorker(
            {"g1": ReadbackSession(), "g2": None, "g3": None},
            {"g1": KEITHLEY_MODE_VOLTAGE_2W, "g2": "", "g3": ""},
        )
        worker.run()
        self.assertTrue(worker.readbacks["g1"]["current_compliance_tripped"])
        self.assertIn("current compliance reached", worker.message)


if __name__ == "__main__":
    unittest.main()
