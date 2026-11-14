"""Magnet presentation tests use only signal-emitting fake controllers."""
import time
import unittest
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from PySide6.QtCore import QObject, Signal, QSettings
from app.quick.magnet import MagnetService
from app.devices.lakeshore335_adapter import LakeShore335Snapshot
from app.devices.attodry2100_adapter import AttoDRY2100Snapshot, AttoDRY2100Capabilities
from utils.config import cfg, SetupConfig


class Worker(QObject):
    thermal_field_updated = Signal(object)


class Controller(QObject):
    connected = Signal(object)
    disconnected = Signal()
    error = Signal(str)
    fault = Signal(str)
    snapshot_updated = Signal(object)
    temperature_updated = Signal(object)
    control_result = Signal(object)
    operation_finished = Signal(str)
    safe_move_result = Signal(object)
    transition_progress = Signal(str, float)
    exclusive_changed = Signal(bool, str)

    def __init__(self):
        super().__init__()
        self._worker = Worker()
        self.exclusive_owner = ""
        self.has_pending_work = False
        for name in ("connect_instrument", "disconnect_instrument", "refresh_snapshot", "refresh_control_configuration",
                     "safe_move_to_field", "pause", "set_sample_control", "heater_off", "connect_async",
                     "disconnect_async", "set_polling_enabled", "read_snapshot_async", "read_temperature_snapshot_async",
                     "configure_sample_temperature_async", "stop_sample_temperature_control_async", "set_h_setpoint_async",
                     "start_field_control_async", "request_stop", "shutdown"):
            setattr(self, name, Mock(name=name, return_value=True))


class SDKController(Controller):
    operation_finished = Signal(str, bool, object)


class QuickMagnetTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.settings = QSettings(str(Path(directory.name) / "settings.ini"), QSettings.Format.IniFormat)
        setup_patch = patch.object(cfg, "setup", SetupConfig("1000", "ls335"))
        setup_patch.start()
        self.addCleanup(setup_patch.stop)
        self.aps, self.sdk, self.ls = Controller(), SDKController(), Controller()
        self.service = MagnetService(magnet1000=self.aps, magnet2100=self.sdk, lakeshore=self.ls, settings=self.settings)
        self.service._timer.stop()
        self.addCleanup(self.service.close)
        self.service.acknowledgeReview(True)

    def sdk_service(self):
        setup_patch = patch.object(cfg, "setup", SetupConfig("2100", "2100"))
        setup_patch.start()
        self.addCleanup(setup_patch.stop)
        service = MagnetService(magnet2100=self.sdk, settings=self.settings)
        service._timer.stop()
        self.addCleanup(service.close)
        service.acknowledgeReview(True)
        return service

    def sample(self, status=0, age=0., temperature=4.):
        return LakeShore335Snapshot(temperature, 3.5, status, 0, time.time(), time.monotonic() - age)

    def test_telemetry_keeps_target_and_heater_forms_stable(self):
        changes, bands = [], []
        self.service.fieldsChanged.connect(lambda: changes.append(True))
        self.service.heaterBandFieldsChanged.connect(lambda: bands.append(True))
        self.service._snapshot("1000", SimpleNamespace(field_t=.1, monotonic_s=time.monotonic()))
        self.service._temperature_snapshot(self.sample())
        self.service._tick()
        self.service._report("Thermal interlock state changed")
        self.assertEqual(changes, [])
        self.assertEqual(bands, [])
        self.assertEqual(self.service.telemetry["magnet"]["field_t"], .1)
        self.service.setValue("target_field", .2)
        self.assertEqual(changes, [True])
        self.service.selectBackend("2100")
        self.assertEqual(changes, [True])
        self.assertIn("restart", self.service.message)
        self.assertIn("final_mode", [s["key"] for s in self.service.fields])
        self.assertIsNone(self.service.magnet2100)

    def test_fault_clears_temperature_cache_thermal_permission_and_ready(self):
        self.service._connected["temperature"] = True
        self.service._values["wait_temperature"] = True
        self.service._control_result({"operation": "sample_control", "ok": True, "setpoint": 4.})
        self.service._temperature_snapshot(self.sample())
        self.service._stable_since = time.monotonic() - 20
        self.assertTrue(self.service.is_ready())
        self.ls.fault.emit("sensor disconnected")
        self.assertFalse(self.service.is_ready())
        self.assertNotIn("temperature", self.service._snapshots)
        self.assertIsNone(self.service.thermal_safety.latest_snapshot)
        self.assertFalse(self.service.thermal_safety.magnet_permission)
        self.assertEqual(self.service.measurement_settings(), (True, None))

    def test_temperature_sensor_status_and_age_are_fail_closed(self):
        for snapshot in (self.sample(status="1e-999"), self.sample(status="NaN"), self.sample(age=20), self.sample(temperature=float("nan"))):
            self.service._temperature_snapshot(snapshot)
            self.assertFalse(self.service._temperature_valid())
        self.service._temperature_snapshot(self.sample())
        with patch.object(cfg.lakeshore335, "maximum_reading_age_s", float("inf")):
            self.assertFalse(self.service._temperature_valid())

    def test_configuration_review_invalidates_when_limits_or_mapping_change(self):
        self.assertTrue(self.service.review_valid())
        with patch.object(cfg.lakeshore335, "sample_channel", "A"):
            changes = []
            self.service.changed.connect(lambda: changes.append(True))
            self.assertFalse(self.service.review_valid())
        self.assertFalse(self.service.review_valid())
        self.service._tick()
        self.assertFalse(self.service.reviewed)
        self.assertTrue(changes)

    def test_review_persists_only_for_unchanged_setup_and_can_be_revoked(self):
        def new_service(*, offline=False):
            service = MagnetService(offline=offline, magnet1000=Controller(), magnet2100=SDKController(),
                                    lakeshore=Controller(), settings=self.settings)
            service._timer.stop()
            self.addCleanup(service.close)
            return service

        self.service.acknowledgeReview(False)
        self.assertFalse(new_service().reviewed)
        self.service.acknowledgeReview(True)
        self.assertTrue(new_service().reviewed)
        reopened_settings = QSettings(self.settings.fileName(), QSettings.Format.IniFormat)
        reopened = MagnetService(magnet1000=Controller(), lakeshore=Controller(), settings=reopened_settings)
        reopened._timer.stop()
        self.addCleanup(reopened.close)
        self.assertTrue(reopened.reviewed)
        with patch.object(cfg.lakeshore335, "sample_channel", "A"):
            changed = new_service()
            self.assertFalse(changed.reviewed)
            changed.command("connect")
            self.assertIn("Review", changed.message)
        self.assertFalse(new_service().reviewed)

        self.service.acknowledgeReview(True)
        with patch.object(cfg, "setup", SetupConfig("2100", "2100")):
            other = new_service()
            self.assertFalse(other.reviewed)
            other.acknowledgeReview(True)
        self.assertTrue(new_service().reviewed)
        new_service().acknowledgeReview(False)
        self.assertFalse(new_service().reviewed)
        with patch.object(cfg, "setup", SetupConfig("2100", "2100")):
            self.assertTrue(new_service().reviewed)

    def test_offline_review_does_not_restore_or_modify_saved_approval(self):
        offline = MagnetService(offline=True, magnet1000=Controller(), lakeshore=Controller(), settings=self.settings)
        offline._timer.stop()
        self.addCleanup(offline.close)
        self.assertFalse(offline.reviewed)
        offline.acknowledgeReview(False)
        reopened = MagnetService(magnet1000=Controller(), lakeshore=Controller(), settings=self.settings)
        reopened._timer.stop()
        self.addCleanup(reopened.close)
        self.assertTrue(reopened.reviewed)
        reopened.acknowledgeReview(False)
        offline.acknowledgeReview(True)
        unreviewed = MagnetService(magnet1000=Controller(), lakeshore=Controller(), settings=self.settings)
        unreviewed._timer.stop()
        self.addCleanup(unreviewed.close)
        self.assertFalse(unreviewed.reviewed)

    def test_passive_control_readbacks_do_not_log_setpoint_commands(self):
        self.service.clearLog()
        readback = {"operation": "read_control", "ok": True, "setpoint": 4.}
        with patch.object(self.service, "_notify_readiness", wraps=self.service._notify_readiness) as notify:
            self.service._control_result(readback)
            self.service._control_result(readback)
            self.assertEqual(notify.call_count, 2)
        self.assertEqual(self.service.logText, "")
        self.assertEqual(self.service._target, 4.)
        self.assertEqual(self.service._control_state, readback)

        self.service._control_result({"operation": "sample_control", "ok": True, "setpoint": 4.})
        self.assertIn("Sample setpoint verified: 4 K", self.service.logText)
        self.service.clearLog()
        self.service._control_result({"operation": "setpoint", "ok": True, "setpoint": 5.})
        self.assertIn("Sample setpoint verified: 5 K", self.service.logText)
        self.service.clearLog()
        self.service._control_result({"operation": "read_control", "ok": False, "error": "read failed"})
        self.assertIn("read failed", self.service.logText)

    def test_persistent_confirmation_is_bound_to_displayed_field_and_configuration(self):
        self.service._connected["1000"] = True
        snapshot = SimpleNamespace(field_t=.1, heater_on=False, monotonic_s=time.monotonic())
        self.service._snapshot("1000", snapshot)
        prompts = []
        self.service.confirmationRequested.connect(lambda token, title, body: prompts.append(token))
        self.service.command("move")
        self.aps.safe_move_to_field.assert_not_called()
        snapshot.field_t = .2
        self.service.confirm(prompts[-1], True)
        self.aps.safe_move_to_field.assert_not_called()
        self.assertIn("field or heater state changed", self.service.message)
        self.service.command("move")
        self.service.confirm(prompts[-1], True)
        self.aps.safe_move_to_field.assert_called_once()
        self.assertTrue(self.aps.safe_move_to_field.call_args.kwargs["persistent_field_confirmed"])

    def test_confirmation_displays_target_mode_and_corresponding_heater_action(self):
        self.service._connected["1000"] = True
        self.service._snapshot("1000", SimpleNamespace(field_t=-.1, heater_on=False, monotonic_s=time.monotonic()))
        prompts = []
        self.service.confirmationRequested.connect(lambda _token, _title, body: prompts.append(body))
        self.service.setValue("target_field", .25)
        self.service.command("move")
        self.assertIn("Current / stored field: -0.1 T", prompts[-1])
        self.assertIn("Target field: 0.25 T", prompts[-1])
        self.assertIn("Final mode: persistent", prompts[-1])
        self.assertIn("heater OFF", prompts[-1])
        self.service.setValue("final_mode", "driven")
        self.service.command("move")
        self.assertIn("Final mode: driven", prompts[-1])
        self.assertIn("leaves the heater ON", prompts[-1])
        self.aps.safe_move_to_field.assert_not_called()

    def test_1000_and_2100_temperature_commands_are_separate(self):
        self.service._connected["temperature"] = True
        self.service.command("temperature")
        self.ls.set_sample_control.assert_called_once()
        self.service._pending.clear()
        sdk_service = self.sdk_service()
        self.assertIsNone(sdk_service.magnet1000)
        self.assertIsNone(sdk_service.lakeshore335)
        sdk_service._connected["2100"] = True
        sdk_service.command("temperature")
        self.sdk.configure_sample_temperature_async.assert_not_called()
        sdk_service._snapshot("2100", AttoDRY2100Snapshot(0., capabilities=AttoDRY2100Capabilities(sample_temperature_control=True)))
        sdk_service.command("temperature")
        self.sdk.configure_sample_temperature_async.assert_called_once()
        sdk_service._pending.clear()
        sdk_service.command("disconnect_temperature")
        self.ls.disconnect_instrument.assert_not_called()

    def test_submit_failure_clears_pending_and_requires_new_control_readback(self):
        self.service._connected["temperature"] = True
        self.ls.set_sample_control.side_effect = RuntimeError("queue rejected")
        self.service.command("temperature")
        self.assertNotIn("sample_control", self.service.pendingOperations)
        self.assertTrue(self.service._control_failed)
        self.assertIsNone(self.service.measurement_settings()[1])

    def test_connection_actions_track_pending_failure_and_disconnect(self):
        for name, controller, connect, disconnect, signal_name in (
                ("magnet", self.aps, "connect", "disconnect", "1000"),
                ("temperature", self.ls, "connect_temperature", "disconnect_temperature", "temperature")):
            with self.subTest(name=name):
                state = self.service.connectionActions[name]
                self.assertEqual(state["state"], "disconnected")
                self.assertTrue(state["connectEnabled"])
                self.assertFalse(state["disconnectEnabled"])
                self.service.command(connect)
                self.assertEqual(self.service.connectionActions[name]["state"], "connecting")
                self.service.command(connect)
                controller.connect_instrument.assert_called_once()
                controller.error.emit("connection failed")
                self.assertEqual(self.service.connectionActions[name]["state"], "disconnected")
                self.assertTrue(self.service.connectionActions[name]["connectEnabled"])
                self.service.command(connect)
                controller.connected.emit("fake")
                self.assertEqual(self.service.connectionActions[name]["state"], "connected")
                self.assertFalse(self.service.connectionActions[name]["connectEnabled"])
                self.assertTrue(self.service.connectionActions[name]["disconnectEnabled"])
                self.service.command(connect)
                self.assertEqual(controller.connect_instrument.call_count, 2)
                self.service.command(disconnect)
                self.assertEqual(self.service.connectionActions[name]["state"], "disconnecting")
                self.service.command(disconnect)
                controller.disconnect_instrument.assert_called_once()
                controller.disconnected.emit()
                self.assertEqual(self.service.connectionActions[name]["state"], "disconnected")

    def test_sdk_connection_actions_and_failure_recovery(self):
        service = self.sdk_service()
        service.command("connect")
        self.assertEqual(service.connectionActions["magnet"]["state"], "connecting")
        self.sdk.operation_finished.emit("connect", False, "failed")
        self.assertTrue(service.connectionActions["magnet"]["connectEnabled"])
        service.command("connect")
        self.sdk.connected.emit("SDK")
        self.assertEqual(service.connectionActions["magnet"]["state"], "connected")
        self.assertTrue(service.connectionActions["magnet"]["disconnectEnabled"])
        service.command("disconnect")
        self.assertEqual(service.connectionActions["magnet"]["state"], "disconnecting")
        self.sdk.operation_finished.emit("disconnect", False, "failed")
        self.assertTrue(service.connectionActions["magnet"]["disconnectEnabled"])

    def test_sdk_connection_starts_polling_and_capability_temperature_updates(self):
        service = self.sdk_service()
        self.sdk.connected.emit("SDK")
        self.sdk.set_polling_enabled.assert_called_once_with(True)
        service._snapshot("2100", AttoDRY2100Snapshot(0., capabilities=AttoDRY2100Capabilities(sample_temperature_readback=True)))
        service._tick()
        self.sdk.read_temperature_snapshot_async.assert_called_once()
        service._tick()
        self.sdk.read_temperature_snapshot_async.assert_called_once()

    def test_stop_cancels_unissued_sdk_start_and_does_not_turn_sample_heater_off(self):
        self.service._connected.update({"1000": True, "temperature": True})
        self.service.request_stop()
        self.aps.pause.assert_called_once()
        self.ls.heater_off.assert_not_called()
        service = self.sdk_service()
        service._connected["2100"] = True
        service._pending_start_2100 = True
        service.request_stop()
        service._operation2100("setpoint", True, None)
        self.sdk.start_field_control_async.assert_not_called()
        self.sdk.request_stop.assert_called_once()

    def test_periodic_old_readback_cannot_hide_failed_temperature_apply(self):
        self.service._values["wait_temperature"] = True
        self.service._control_result({"operation": "sample_control", "ok": False, "error": "mismatch"})
        self.service._control_result({"operation": "read_control", "ok": True, "setpoint": 4.})
        self.service._temperature_snapshot(self.sample())
        self.service._stable_since = time.monotonic() - 20
        self.assertFalse(self.service.is_ready())
        self.assertIsNone(self.service.measurement_settings()[1])


if __name__ == "__main__":
    unittest.main()
