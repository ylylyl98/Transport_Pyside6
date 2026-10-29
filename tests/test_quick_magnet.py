"""Magnet presentation tests use only signal-emitting fake controllers."""
import time
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from PySide6.QtCore import QObject, Signal
from app.quick.magnet import MagnetService
from app.devices.lakeshore335_adapter import LakeShore335Snapshot
from app.devices.attodry2100_adapter import AttoDRY2100Snapshot, AttoDRY2100Capabilities
from utils.config import cfg


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
        self.aps, self.sdk, self.ls = Controller(), SDKController(), Controller()
        self.service = MagnetService(magnet1000=self.aps, magnet2100=self.sdk, lakeshore=self.ls)
        self.service._timer.stop()
        self.addCleanup(self.service.close)
        self.service.acknowledgeReview(True)

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
        self.assertEqual(changes, [True, True])
        self.assertNotIn("final_mode", [s["key"] for s in self.service.fields])

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
            self.assertFalse(self.service.review_valid())
            self.service._tick()
            self.assertFalse(self.service.reviewed)

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
        self.service.selectBackend("2100")
        self.service._connected["2100"] = True
        self.service.command("temperature")
        self.sdk.configure_sample_temperature_async.assert_not_called()
        self.service._snapshot("2100", AttoDRY2100Snapshot(0., capabilities=AttoDRY2100Capabilities(sample_temperature_control=True)))
        self.service.command("temperature")
        self.sdk.configure_sample_temperature_async.assert_called_once()
        self.service._pending.clear()
        self.service.command("disconnect_temperature")
        self.ls.disconnect_instrument.assert_not_called()

    def test_submit_failure_clears_pending_and_requires_new_control_readback(self):
        self.service._connected["temperature"] = True
        self.ls.set_sample_control.side_effect = RuntimeError("queue rejected")
        self.service.command("temperature")
        self.assertNotIn("sample_control", self.service.pendingOperations)
        self.assertTrue(self.service._control_failed)
        self.assertIsNone(self.service.measurement_settings()[1])

    def test_sdk_connection_starts_polling_and_capability_temperature_updates(self):
        self.sdk.connected.emit("SDK")
        self.sdk.set_polling_enabled.assert_called_once_with(True)
        self.service._backend = "2100"
        self.service._snapshot("2100", AttoDRY2100Snapshot(0., capabilities=AttoDRY2100Capabilities(sample_temperature_readback=True)))
        self.service._tick()
        self.sdk.read_temperature_snapshot_async.assert_called_once()
        self.service._tick()
        self.sdk.read_temperature_snapshot_async.assert_called_once()

    def test_stop_cancels_unissued_sdk_start_and_does_not_turn_sample_heater_off(self):
        self.service._connected.update({"1000": True, "2100": True, "temperature": True})
        self.service._pending_start_2100 = True
        self.service.request_stop()
        self.service._operation2100("setpoint", True, None)
        self.sdk.start_field_control_async.assert_not_called()
        self.aps.pause.assert_called_once()
        self.sdk.request_stop.assert_called_once()
        self.ls.heater_off.assert_not_called()

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
