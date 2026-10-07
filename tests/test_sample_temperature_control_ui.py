import os
import unittest
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6 import QtCore, QtWidgets

from app.ui.sample_temperature_bar import SampleTemperatureBar
from app.ui.widgets.run_panel import RunPanel
from test_sample_heater_ranges import TEST_BANDS


class _FakeLakeShoreController(QtCore.QObject):
    connected = QtCore.Signal(object)
    snapshot_updated = QtCore.Signal(object)
    control_result = QtCore.Signal(object)
    error = QtCore.Signal(str)
    fault = QtCore.Signal(str)
    disconnected = QtCore.Signal()

    def __init__(self):
        super().__init__()
        self.setpoints = []
        self.off_requests = 0
        self.is_connected = True
        self.control_requests = []

    def set_sample_setpoint(self, target):
        self.setpoints.append(float(target))

    def heater_off(self):
        self.off_requests += 1

    def set_sample_control(self, target, **settings):
        self.setpoints.append(float(target))
        self.control_requests.append({"temperature_k": float(target), **settings})


def _snapshot(now, *, temperature=10.0, status="0", connected=True,
              communication_valid=True):
    return SimpleNamespace(
        sample_temperature_k=temperature,
        sample_sensor_status=status,
        monotonic_s=float(now),
        connected=connected,
        communication_valid=communication_valid,
    )


class SampleTemperatureControlUiTests(unittest.TestCase):
    def test_primary_button_continuation_keeps_external_gates_and_stop(self):
        panel = RunPanel("Start scan")
        panel.set_running(True)
        panel.set_start_available(False)  # Recipe editing is locked during run.
        panel.set_continuation(True, False, "Temperature still high")
        self.assertEqual(panel.btn_start.text(), "Continue scan")
        self.assertFalse(panel.btn_start.isEnabled())
        self.assertTrue(panel.btn_stop.isEnabled())
        panel.set_continuation(True, True, "Ready")
        self.assertTrue(panel.btn_start.isEnabled())
        panel.set_start_blocked("sample_temperature", True)
        self.assertFalse(panel.btn_start.isEnabled())
        panel.set_start_blocked("sample_temperature", False)
        panel.set_continuation(False, False)
        self.assertEqual(panel.btn_start.text(), "Start scan")
        self.assertFalse(panel.btn_start.isEnabled())
        panel.set_running(False)
        panel.set_start_available(True)
        self.assertTrue(panel.btn_start.isEnabled())
        panel.deleteLater()

    def test_initial_unchecked_wait_and_first_toggle_update_all_start_gates(self):
        controller = _FakeLakeShoreController()
        bar = SampleTemperatureBar(controller, SimpleNamespace(maximum_reading_age_s=3))
        bar._age_timer.stop()
        bar.set_backend("1000")
        panels = [RunPanel("Start") for _ in range(5)]
        def update(ready):
            for panel in panels:
                panel.set_start_blocked("sample_temperature", bar.wait_check.isChecked() and not ready)
        bar.readiness_changed.connect(update)
        update(bar.is_ready())
        self.assertTrue(all(panel.btn_start.isEnabled() for panel in panels))
        bar.wait_check.setChecked(True)
        self.assertTrue(all(not panel.btn_start.isEnabled() for panel in panels))
        bar.wait_check.setChecked(False)
        self.assertTrue(all(panel.btn_start.isEnabled() for panel in panels))
        bar.deleteLater()
        for panel in panels:
            panel.deleteLater()

    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def test_external_temperature_block_never_overrides_local_or_running_state(self):
        panel = RunPanel("Start")
        panel.set_start_available(False)
        panel.set_start_blocked("sample_temperature", True)
        panel.set_start_blocked("sample_temperature", False)
        self.assertFalse(panel.btn_start.isEnabled())

        panel.set_start_available(True)
        self.assertTrue(panel.btn_start.isEnabled())
        panel.set_running(True)
        panel.set_start_blocked("sample_temperature", True)
        panel.set_start_blocked("sample_temperature", False)
        self.assertFalse(panel.btn_start.isEnabled())
        panel.deleteLater()

    def test_invalid_sensor_time_does_not_count_toward_stability_dwell(self):
        now = [0.0]
        controller = _FakeLakeShoreController()
        config = SimpleNamespace(
            sample_stability_tolerance_k=0.05,
            sample_stability_dwell_s=5.0,
            maximum_reading_age_s=20.0,
        )
        bar = SampleTemperatureBar(controller, config, clock=lambda: now[0])
        bar._age_timer.stop()
        bar.set_backend("1000")
        bar.wait_check.setChecked(True)
        bar._target = 10.0

        bar.on_snapshot(_snapshot(now[0], status="16"))
        self.assertIsNone(bar._stable_since)
        now[0] = 10.0
        bar.on_snapshot(_snapshot(now[0], status="0"))
        self.assertEqual(bar._stable_since, 10.0)
        self.assertFalse(bar.is_ready())
        now[0] = 15.0
        self.assertTrue(bar.is_ready())
        bar.deleteLater()

    def test_stale_snapshot_invalidates_ready_without_a_new_snapshot(self):
        now = [0.0]
        controller = _FakeLakeShoreController()
        config = SimpleNamespace(
            sample_stability_tolerance_k=0.05,
            sample_stability_dwell_s=1.0,
            maximum_reading_age_s=3.0,
        )
        bar = SampleTemperatureBar(controller, config, clock=lambda: now[0])
        bar._age_timer.stop()
        bar.set_backend("1000")
        bar.wait_check.setChecked(True)
        bar._target = 10.0
        bar.on_snapshot(_snapshot(now[0]))
        now[0] = 1.0
        self.assertTrue(bar.is_ready())

        now[0] = 3.1
        bar._on_age_timer()
        self.assertFalse(bar.is_ready())
        self.assertIsNone(bar._stable_since)
        self.assertIn("stale telemetry", bar.current.text())
        bar.deleteLater()

    def test_target_persists_while_bar_visibility_follows_backend(self):
        controller = _FakeLakeShoreController()
        config = SimpleNamespace(
            sample_stability_tolerance_k=0.05,
            sample_stability_dwell_s=1.0,
            maximum_reading_age_s=3.0,
        )
        bar = SampleTemperatureBar(controller, config)
        bar._age_timer.stop()
        bar.set_backend("1000")
        bar.target.setValue(12.5)
        bar._set_target()
        controller.control_result.emit({"operation": "sample_control", "ok": True,
                                        "setpoint": 12.5, "output": 1, "range": 0})
        bar.set_backend("2100")
        self.assertTrue(bar.isHidden())
        bar.set_backend("1000")
        self.assertFalse(bar.isHidden())
        self.assertEqual(bar._target, 12.5)
        self.assertEqual(controller.setpoints, [12.5])
        bar.deleteLater()

    def make_control_bar(self):
        now = [0.0]
        controller = _FakeLakeShoreController()
        config = SimpleNamespace(maximum_reading_age_s=3.0,
            sample_stability_tolerance_k=0.05, sample_stability_dwell_s=1.0,
            sample_warning_temperature_k=4.6)
        bar = SampleTemperatureBar(controller, config, clock=lambda: now[0])
        bar._age_timer.stop()
        bar.set_backend("1000")
        bar.wait_check.setChecked(True)
        self.addCleanup(lambda: bar.deleteLater())
        return bar, controller, now

    @staticmethod
    def control_state(target=10, **extra):
        return {"operation": "read_control", "ok": True, "output": 1,
                "setpoint": target, "range": 0, "ramp_enabled": False,
                "ramp_rate_k_per_min": 1, "heater_output": 0,
                "output_type": "current", **extra}

    def test_request_requires_readback_before_target_and_stability_are_accepted(self):
        bar, controller, now = self.make_control_bar()
        bar.target.setValue(10)
        bar._set_target()
        self.assertIsNone(bar._target)
        self.assertEqual(bar.measurement_settings(), (True, None))
        self.assertFalse(bar.set_button.isEnabled())
        self.assertTrue(bar.off_button.isEnabled())
        bar.on_snapshot(_snapshot(now[0]))
        now[0] = 1
        self.assertFalse(bar.is_ready())
        controller.control_result.emit(self.control_state(operation="sample_control"))
        self.assertEqual(bar._target, 10)
        self.assertTrue(bar.set_button.isEnabled())
        bar.on_snapshot(_snapshot(now[0]))
        now[0] = 2
        self.assertTrue(bar.is_ready())
        self.assertIn("blocks magnet", bar.message.text())

    def test_failed_write_blocks_readiness_until_a_new_apply_succeeds(self):
        bar, controller, now = self.make_control_bar()
        controller.control_result.emit(self.control_state())
        bar.target.setValue(10)
        bar._set_target()
        controller.control_result.emit({"operation": "sample_control", "ok": False, "error": "mismatch"})
        controller.control_result.emit(self.control_state())
        bar.on_snapshot(_snapshot(now[0]))
        now[0] = 1
        self.assertFalse(bar.is_ready())
        self.assertIn("mismatch", bar.message.text())
        bar._set_target()
        controller.control_result.emit(self.control_state(operation="sample_control"))
        bar.on_snapshot(_snapshot(now[0]))
        now[0] = 2
        self.assertTrue(bar.is_ready())

    def test_external_setpoint_change_resets_the_stability_dwell(self):
        bar, controller, now = self.make_control_bar()
        controller.control_result.emit(self.control_state())
        bar.on_snapshot(_snapshot(now[0]))
        now[0] = 1
        self.assertTrue(bar.is_ready())
        controller.control_result.emit(self.control_state(target=12))
        self.assertEqual(bar._target, 12)
        self.assertFalse(bar.is_ready())

    def test_range_and_ramp_are_explicit_and_default_to_preserving_settings(self):
        bar, controller, _ = self.make_control_bar()
        bar._set_target()
        self.assertIsNone(controller.control_requests[0]["heater_range"])
        self.assertIsNone(controller.control_requests[0]["ramp_enabled"])
        controller.control_result.emit(self.control_state(operation="sample_control", target=4.2))
        bar.heater_range.setCurrentIndex(2)
        bar.ramp_mode.setCurrentIndex(2)
        bar.ramp_rate.setValue(0.5)
        bar._set_target()
        self.assertEqual(controller.control_requests[-1]["heater_range"], 1)
        self.assertTrue(controller.control_requests[-1]["ramp_enabled"])
        self.assertEqual(controller.control_requests[-1]["ramp_rate_k_per_min"], 0.5)

    def test_off_during_apply_waits_for_off_readback(self):
        bar, controller, _ = self.make_control_bar()
        bar._set_target()
        bar._heater_off()
        self.assertEqual(controller.off_requests, 1)
        controller.control_result.emit(self.control_state(operation="sample_control"))
        self.assertEqual(bar._pending_operation, "heater_off")
        self.assertFalse(bar.is_ready())
        controller.control_result.emit({"operation": "heater_off", "ok": True, "output": 1, "range": 0})
        self.assertIsNone(bar._pending_operation)
        self.assertIn("OFF — confirmed", bar.control_status.text())

    def test_disconnect_disables_apply_and_unknown_heater_state_blocks_wait(self):
        bar, controller, _ = self.make_control_bar()
        controller.disconnected.emit()
        self.assertFalse(bar.set_button.isEnabled())
        self.assertFalse(bar.off_button.isEnabled())
        bar._set_target()
        self.assertEqual(controller.control_requests, [])
        self.assertIn("unknown", bar.control_status.text())

    def test_voltage_output_disables_medium_and_high_without_overwriting_drafts(self):
        bar, controller, _ = self.make_control_bar()
        bar.target.setValue(12.5)
        controller.control_result.emit(self.control_state(output=2, output_type="voltage"))
        self.assertEqual(bar.target.value(), 12.5)
        self.assertFalse(bar.heater_range.model().item(3).isEnabled())
        self.assertFalse(bar.heater_range.model().item(4).isEnabled())
        self.assertEqual(bar.heater_range.itemText(2), "Heater ON")

    def test_sensor_refresh_does_not_overwrite_persistent_heater_status(self):
        bar, controller, now = self.make_control_bar()
        controller.control_result.emit(self.control_state(range=1, heater_output=23.5))
        before = bar.control_status.text()
        bar.on_snapshot(_snapshot(now[0]))
        self.assertEqual(bar.control_status.text(), before)
        self.assertIn("23.5%", before)

    def test_nonpositive_sensor_readings_cannot_count_as_a_stable_low_target(self):
        for temperature in (0, -0.001):
            bar, controller, now = self.make_control_bar()
            controller.control_result.emit(self.control_state(target=0.001))
            bar.on_snapshot(_snapshot(now[0], temperature=temperature))
            now[0] = 1
            self.assertIsNone(bar._stable_since)
            self.assertFalse(bar.is_ready())

    def test_auto_selection_sends_a_policy_request_and_previews_target_band(self):
        bar, controller, _ = self.make_control_bar()
        bar._save_auto_ranges(TEST_BANDS)
        bar.heater_range.setCurrentIndex(bar.heater_range.findData("auto"))
        bar.target.setValue(20)
        self.assertIn("Medium", bar.auto_preview.text())
        self.assertEqual(controller.control_requests, [])
        bar._set_target()
        self.assertEqual(controller.control_requests[0]["heater_range"], "auto")

    def test_editing_target_preview_does_not_apply_or_override_manual_range(self):
        bar, controller, _ = self.make_control_bar()
        bar._save_auto_ranges(TEST_BANDS)
        bar.heater_range.setCurrentIndex(bar.heater_range.findData("auto"))
        bar.target.setValue(10)
        self.assertIn("Low", bar.auto_preview.text())
        bar.target.setValue(10.001)
        self.assertIn("Medium", bar.auto_preview.text())
        self.assertEqual(controller.control_requests, [])
        bar.heater_range.setCurrentIndex(2)
        bar._set_target()
        self.assertEqual(controller.control_requests[0]["heater_range"], 1)

    def test_missing_or_uncovered_auto_table_has_no_high_range_fallback(self):
        bar, controller, _ = self.make_control_bar()
        bar.heater_range.setCurrentIndex(bar.heater_range.findData("auto"))
        self.assertIn("LS335 Zone table", bar.auto_preview.text())
        bar._save_auto_ranges(TEST_BANDS[:1])
        bar.target.setValue(20)
        self.assertIn("does not cover", bar.auto_preview.text())
        self.assertEqual(controller.control_requests, [])

    def test_saving_auto_table_preserves_previous_config_if_persistence_fails(self):
        bar, _, _ = self.make_control_bar()
        bar._save_auto_ranges(TEST_BANDS)
        def fail():
            raise OSError("write failed")
        bar._save_config = fail
        with self.assertRaises(OSError):
            bar._save_auto_ranges([])
        self.assertEqual(bar.config.sample_auto_heater_ranges, TEST_BANDS)

    def test_auto_command_confirmation_displays_resolved_instrument_band(self):
        bar, controller, _ = self.make_control_bar()
        bar.heater_range.setCurrentIndex(bar.heater_range.findData("auto"))
        bar.target.setValue(20)
        controller.control_result.emit(self.control_state(target=20, operation="sample_control",
            range=2, auto_range_source="ls335_zone", auto_native_zone=False,
            auto_target_range=2, auto_upper_temperature_k=100))
        self.assertIn("Medium", bar.auto_preview.text())
        self.assertIn("100 K", bar.auto_preview.text())
        controller.disconnected.emit()
        self.assertIsNone(bar._last_auto_info)

    def test_off_does_not_get_reenabled_by_auto_preview_or_periodic_readback(self):
        bar, controller, _ = self.make_control_bar()
        bar._save_auto_ranges(TEST_BANDS)
        bar.heater_range.setCurrentIndex(bar.heater_range.findData("auto"))
        bar._heater_off()
        controller.control_result.emit({"operation": "heater_off", "ok": True, "output": 1, "range": 0})
        controller.control_result.emit(self.control_state(range=0))
        bar._update_auto_preview()
        self.assertEqual(controller.control_requests, [])
        self.assertEqual(controller.off_requests, 1)


if __name__ == "__main__":
    unittest.main()
