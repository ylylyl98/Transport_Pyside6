import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

if os.environ.get("TRANSPORT_OFFLINE_VERIFICATION") != "1":
    raise unittest.SkipTest("Run with verify_offline.py")

from PySide6.QtCore import QSettings
from app.quick.instruments import InstrumentService
from app.quick.forms import checked_value, field


class QuickInstrumentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.settings = QSettings(str(Path(self.temp.name) / "settings.ini"), QSettings.Format.IniFormat)
        self.service = InstrumentService(settings=self.settings, offline=True)

    def tearDown(self):
        self.service.close()
        self.service.manager.shutdown()
        self.temp.cleanup()

    def test_saved_legacy_addresses_limits_and_gain_are_loaded(self):
        self.settings.setValue("addr/g1", "GPIB0::11::INSTR")
        self.settings.setValue("rates/amp", 1e7)
        self.settings.setValue("rates/lkn", 100.)
        self.settings.setValue("protection/g1/max_voltage_v", 5.)
        self.service._load()
        self.assertEqual(self.service.connections.gate1, "GPIB0::11::INSTR")
        self.assertEqual(self.service.connections.gate1_max_voltage_v, 5.)
        self.assertEqual(self.service.get_rates(), (1e7, 100.))

    def test_settings_edits_preserve_legacy_keys_and_reject_nonfinite(self):
        self.service.setValue("device_id", "sample 12")
        self.assertEqual(self.settings.value("path/device_id"), "sample 12")
        previous = self.service.connections.gate1_max_voltage_v
        self.service.setValue("gate1_max_voltage_v", "nan")
        self.assertEqual(self.service.connections.gate1_max_voltage_v, previous)
        self.assertIn("finite", self.service.message)

    def test_configuration_edits_rejected_during_acquisition(self):
        self.service.set_active_provider(lambda: True)
        self.service.setValue("gate1", "GPIB0::99::INSTR")
        self.assertNotEqual(self.service.connections.gate1, "GPIB0::99::INSTR")
        self.assertIn("cleanup", self.service.message)

    def test_offline_commands_never_reach_manager(self):
        with patch.object(self.service.manager, "connect_all") as connect:
            self.service.command("connect", {})
        connect.assert_not_called()
        self.assertIn("disabled", self.service.message)

    def configure_connection_test(self):
        from app.quick.instruments import ADDRESS_KEYS
        for attr in ADDRESS_KEYS:
            setattr(self.service.connections, attr, "")
        self.service.connections.lockin = "GPIB0::8::INSTR"
        self.service.offline = False
        self.addCleanup(lambda: self.service.manager.sessions.update(
            {name: None for name in self.service.manager.sessions}))

    def connect_fake_lockin(self):
        manager = self.service.manager
        manager.sessions['lockin'] = object()
        manager.states['lockin'] = 'ok'
        manager._connected_addresses['lockin'] = self.service.connections.lockin

    def test_connection_actions_follow_connected_and_changed_configuration(self):
        self.configure_connection_test()
        self.assertTrue(self.service.connectionActions['connectEnabled'])
        self.assertFalse(self.service.connectionActions['disconnectEnabled'])
        self.connect_fake_lockin()
        self.assertEqual(self.service.connectionActions['connectText'], 'Connected')
        self.assertFalse(self.service.connectionActions['connectEnabled'])
        self.assertTrue(self.service.connectionActions['disconnectEnabled'])
        with patch.object(self.service.manager, 'connect_all') as connect:
            self.service.command('connect', {})
            connect.assert_not_called()
        self.service.connections.lockin = 'GPIB0::9::INSTR'
        self.assertTrue(self.service.connectionActions['connectEnabled'])
        self.assertEqual(self.service.connectionActions['connectText'], 'Apply connection changes')
        self.assertEqual(next(d for d in self.service.devices if d['name'] == 'lockin')['connectionStatus'],
                         'Reconnect required')

    def test_connection_pending_duplicate_and_failure_recovery(self):
        self.configure_connection_test()
        with patch.object(self.service.manager, 'connect_all', return_value=True) as connect:
            self.service.command('connect', {})
            self.assertEqual(self.service.connectionActions['connectText'], 'Connecting…')
            self.assertFalse(self.service.connectionActions['connectEnabled'])
            self.service.command('connect', {})
            connect.assert_called_once()
        self.service._operation(False, 'Connection failed')
        self.assertTrue(self.service.connectionActions['connectEnabled'])
        with patch.object(self.service.manager, 'connect_all', side_effect=RuntimeError('failed to start')):
            self.service.command('connect', {})
        self.assertTrue(self.service.connectionActions['connectEnabled'])
        self.assertIn('failed to start', self.service.message)

    def test_connection_actions_exclude_disabled_and_unconfigured_devices(self):
        self.configure_connection_test()
        self.service.connections.drive_lockin = 'GPIB0::9::INSTR'
        self.service.connections.drag_drive_enabled = False
        self.connect_fake_lockin()
        self.assertEqual(self.service.connectionActions['status'], '1/1 configured instruments connected')
        self.service.connections.drag_drive_enabled = True
        self.assertEqual(self.service.connectionActions['connectText'], 'Connect remaining')
        self.service.connections.drag_drive_enabled = False
        self.service.connections.lockin = ''
        self.assertTrue(self.service.connectionActions['connectEnabled'])
        self.assertIn('changes pending', self.service.connectionActions['status'])
        self.service.manager.sessions['lockin'] = None
        self.assertFalse(self.service.connectionActions['connectEnabled'])
        self.assertFalse(self.service.connectionActions['disconnectEnabled'])

    def test_connection_completion_waits_for_worker_release_and_notifies_ui(self):
        from PySide6.QtTest import QTest
        self.configure_connection_test()
        self.service._connection_operation = 'connect'
        worker_busy = [True]
        changes = []
        self.service.changed.connect(lambda: changes.append(self.service.connectionActions))
        with patch.object(self.service.manager, 'is_busy', side_effect=lambda: worker_busy[0]):
            self.service._operation(False, 'Other operation rejected')
            self.assertEqual(self.service.connectionActions['connectText'], 'Connecting…')
            self.connect_fake_lockin()
            worker_busy[0] = False
            QTest.qWait(60)
        self.assertEqual(changes[-1]['connectText'], 'Connected')
        self.assertTrue(changes[-1]['disconnectEnabled'])

    def test_disconnect_pending_and_measurement_lock(self):
        self.configure_connection_test()
        self.connect_fake_lockin()
        self.service.set_active_provider(lambda: True)
        self.assertFalse(self.service.connectionActions['disconnectEnabled'])
        self.service.set_active_provider(lambda: False)
        with patch.object(self.service.manager, 'disconnect_all', return_value=True) as disconnect:
            self.service.command('disconnect', {})
            self.assertEqual(self.service.connectionActions['disconnectText'], 'Disconnecting…')
            self.service.command('disconnect', {})
            disconnect.assert_called_once()
        self.service.manager.sessions['lockin'] = None
        self.service._operation(False, 'Disconnected')
        self.assertTrue(self.service.connectionActions['connectEnabled'])
        self.assertFalse(self.service.connectionActions['disconnectEnabled'])

    def test_connected_sensitivity_is_verified_and_voltage_input_required(self):
        class Session:
            def read_sensitivity(self):
                return dict(sensitivity=23, sensitivity_v=.1, input_config=0)
        with patch.object(self.service.manager, "is_connected", return_value=True), patch.object(self.service.manager, "get_session", return_value=Session()):
            self.assertEqual(self.service.get_rates(), (1e7, 100.))
            with patch.object(Session, "read_sensitivity", return_value=dict(sensitivity=23, sensitivity_v=.1, input_config=2)):
                with self.assertRaisesRegex(RuntimeError, "voltage-mode"):
                    self.service.get_rates()

    def test_form_rejects_silent_rounding_and_unknown_choices(self):
        with self.assertRaises(ValueError):
            checked_value(field("n", "Points", 10), "10.1")
        with self.assertRaises(ValueError):
            checked_value(field("m", "Mode", "Raw", options=["Raw", "Derived"]), "Other")
        self.assertEqual(checked_value(field("v", "Voltage", 0., min=-1, max=1), "1e-6"), 1e-6)

    def test_manual_commands_require_idle_finite_targets_and_exact_channel(self):
        self.service.offline = False
        self.service.set_active_provider(lambda: True)
        with patch.object(self.service.manager, "ramp_gate") as ramp:
            self.service.command("ramp", {"device": "g1", "target": 1.})
            ramp.assert_not_called()
        self.service.set_active_provider(lambda: False)
        with patch.object(self.service.manager, "ramp_gate") as ramp:
            self.service.command("ramp", {"device": "g1", "target": float("nan")})
            self.service.command("ramp", {"device": "g1", "target": 21.})
            ramp.assert_not_called()
        with patch.object(self.service.manager, "ramp_daq_output") as ramp:
            self.service.command("daq", {"channel": 1.2, "target": 0.})
            ramp.assert_not_called()
        self.service.close()
        with patch.object(self.service.manager, "connect_all") as connect:
            self.service.command("connect", {})
            connect.assert_not_called()

    def test_manual_readback_uses_real_schema_and_daq_preserves_held_vs_measured(self):
        self.service._read({"g1": dict(set_voltage=1.2, measured_voltage=1.19, current=2e-9,
            current_compliance_tripped=True, current_compliance_a=1e-6, max_source_voltage_v=20.)}, "read")
        gate = next(v for v in self.service.devices if v["name"] == "g1")
        self.assertEqual(gate["setVoltage"], "1.2")
        self.assertEqual(gate["measuredVoltage"], "1.19")
        self.assertTrue(gate["complianceTripped"])
        self.service._daq_done(0, True, "read", {"commanded_v": .2, "measured_v": .19})
        self.assertEqual(self.service.daqOutputs[0]["commanded"], "0.2")
        self.assertEqual(self.service.daqOutputs[0]["measured"], "0.19")
        self.service._status("g1", "idle", "disconnected")
        self.assertEqual(next(v for v in self.service.devices if v["name"] == "g1")["measuredVoltage"], "—")

    def test_verified_lockin_sensitivity_updates_calibration_and_current_input_invalidates(self):
        self.service.setVerifiedLockin("lockin", {"settings": {"sensitivity": 23, "input_config": 0, "frequency_hz": 500.}})
        self.assertEqual(self.service.calibration["lockin_sensitivity_v"], .1)
        self.assertEqual(self.service.calibration["frequency_hz"], 500.)
        self.assertIn("Verified", self.service.calibrationStatus["lockin"])
        self.service.setVerifiedLockin("lockin", {"settings": {"sensitivity": 23, "input_config": 2}})
        self.assertIn("Unverified", self.service.calibrationStatus["lockin"])

    def test_telemetry_does_not_invalidate_configuration_forms(self):
        changes = []
        for name in ("connectionFieldsChanged", "sampleFieldsChanged", "signalFieldsChanged"):
            getattr(self.service, name).connect(lambda name=name: changes.append(name))
        self.service._read({"g1": {"current": 2e-9}}, "Gate readback")
        self.service._daq_done(0, True, "AO readback", {"measured_v": .1})
        self.service._report("Protection state changed")
        self.assertEqual(changes, [])
        self.assertEqual(self.service.message, "Protection state changed")
        self.service.setValue("device_id", "fresh sample")
        self.assertEqual(changes, ["sampleFieldsChanged"])
        self.service.setValue("preamp_sensitivity_a", 2e-7)
        self.assertEqual(changes[-1], "signalFieldsChanged")
        self.service.setValue("gate1", "GPIB0::12::INSTR")
        self.assertEqual(changes[-1], "connectionFieldsChanged")
