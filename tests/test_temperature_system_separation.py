"""Offline regression checks for independent 1000/2100 temperature control."""
import os
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

if os.environ.get("TRANSPORT_OFFLINE_VERIFICATION") != "1":
    raise unittest.SkipTest("Run through verify_offline.py to isolate hardware and settings")

from PySide6 import QtCore, QtWidgets
from app.devices.attodry2100_adapter import (
    AttoDRY2100Capabilities, AttoDRY2100Snapshot, AttoDRY2100TemperatureSnapshot,
)
from app.ui.magnet_panel import MagnetPanel
from app.ui.main_window import MainWindow
from app.ui.sample_temperature_bar import SampleTemperatureBar
from test_magnet_control import _Fake1000, _Fake2100
from test_sample_temperature_control_ui import _FakeLakeShoreController, _snapshot


def control_state(target=4.2, **extra):
    return {"operation": "read_control", "ok": True, "output": 1,
            "setpoint": target, "range": 0, "ramp_enabled": False,
            "ramp_rate_k_per_min": 1, "heater_output": 0,
            "output_type": "current", **extra}


class TemperatureSystemPanelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance()

    def setUp(self):
        self.a, self.b, self.ls = _Fake1000(), _Fake2100(), _FakeLakeShoreController()
        self.now = [100.0]
        config = SimpleNamespace(maximum_reading_age_s=3, sample_stability_dwell_s=1)
        self.bar = SampleTemperatureBar(self.ls, config, compact=True, clock=lambda: self.now[0])
        self.bar._age_timer.stop()
        self.panel = MagnetPanel(self.a, self.b, lakeshore335=self.ls,
                                 sample_temperature_control=self.bar)
        self.panel.auto_system_check.setChecked(False)
        self.panel._on_connected("2100", object())
        self.panel._temp_timer.stop()
        self.panel.review.setChecked(True)
        self.capabilities = AttoDRY2100Capabilities(sample_temperature_readback=True,
                                                   sample_temperature_control=True)
        self.b.snapshot_updated.emit(AttoDRY2100Snapshot(0, temperature_k=3.1,
                                                       capabilities=self.capabilities))
        self.b.calls.clear()

    def tearDown(self):
        self.panel.deleteLater()
        self.app.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)
        self.app.processEvents()

    def test_switch_keeps_separate_drafts_readbacks_and_wait_setting_without_commands(self):
        self.bar.target.setValue(12)
        self.bar.heater_range.setCurrentIndex(self.bar.heater_range.findData("auto"))
        self.bar.wait_check.setChecked(True)
        self.ls.control_result.emit(control_state(10))
        self.panel.sample_target.setValue(6)
        self.b.temperature_updated.emit(AttoDRY2100TemperatureSnapshot(sample_temperature_k=5.5))
        self.panel.backend_combo.setCurrentIndex(1)
        self.assertTrue(self.panel.temp1000_group.isHidden())
        self.assertFalse(self.panel.temp_group.isHidden())
        self.assertTrue(self.bar.is_ready())  # 1000 wait does not gate the 2100.
        self.panel.backend_combo.setCurrentIndex(0)
        self.assertFalse(self.panel.temp1000_group.isHidden())
        self.assertTrue(self.panel.temp_group.isHidden())
        self.assertEqual(self.bar.target.value(), 12)
        self.assertEqual(self.bar._target, 10)
        self.assertEqual(self.bar.heater_range.currentData(), "auto")
        self.assertTrue(self.bar.wait_check.isChecked())
        self.assertFalse(self.bar.is_ready())
        self.assertEqual(self.panel.sample_target.value(), 6)
        self.assertEqual(self.panel.sample_temperature.text(), "5.500 K")
        self.assertEqual((self.a.calls, self.b.calls, self.ls.control_requests), ([], [], []))

    def test_selected_temperature_apply_and_stop_route_to_their_own_controller(self):
        self.bar.target.setValue(20)
        self.bar.heater_range.setCurrentIndex(self.bar.heater_range.findData("auto"))
        self.bar.set_button.click()
        self.assertEqual(self.ls.control_requests[0]["temperature_k"], 20)
        self.assertEqual(self.ls.control_requests[0]["heater_range"], "auto")
        self.panel.backend_combo.setCurrentIndex(1)
        self.panel.sample_target.setValue(6)
        self.panel.sample_rate.setValue(0.5)
        self.panel.temp_control_button.click()
        self.panel.temp_stop_button.click()
        self.assertEqual(self.b.calls, [("temp_set", 6, 0.5), ("temp_stop",)])
        self.assertEqual(self.a.calls, [])
        self.assertEqual(self.ls.off_requests, 0)

    def test_handlers_for_the_inactive_system_send_no_commands(self):
        self.panel._set_temperature()
        self.panel._stop_temperature()
        self.assertEqual(self.b.calls, [])
        self.panel.backend_combo.setCurrentIndex(1)
        self.bar._set_target()
        self.bar._heater_off()
        self.assertEqual(self.ls.control_requests, [])
        self.assertEqual(self.ls.off_requests, 0)
        self.assertIsNone(self.bar._pending_operation)

    def test_apply_stays_blocked_until_both_temperature_apply_and_stop_finish(self):
        self.panel.backend_combo.setCurrentIndex(1)
        self.panel._set_temperature()
        self.panel._stop_temperature()
        self.panel._stop_temperature()
        self.assertEqual([call[0] for call in self.b.calls], ["temp_set", "temp_stop"])
        self.b.operation_finished.emit("configure_temperature", True, None)
        self.assertTrue(self.panel._temp_request_pending)
        self.assertFalse(self.panel.temp_control_button.isEnabled())
        self.assertFalse(self.panel.temp_stop_button.isEnabled())
        self.b.error.emit("attoDRY2100 read_temperature failed: telemetry unavailable")
        self.assertTrue(self.panel._temp_request_pending)
        self.panel._set_temperature()
        self.assertEqual(len(self.b.calls), 2)
        self.b.operation_finished.emit("stop_temperature", True, None)
        self.assertFalse(self.panel._temp_request_pending)
        self.assertTrue(self.panel.temp_control_button.isEnabled())
        self.assertTrue(self.panel.temp_stop_button.isEnabled())

    def test_failed_stop_does_not_release_an_unfinished_apply(self):
        self.panel.backend_combo.setCurrentIndex(1)
        self.panel._set_temperature()
        self.panel._stop_temperature()
        self.b.operation_finished.emit("stop_temperature", False, RuntimeError("rejected"))
        self.assertTrue(self.panel._temp_request_pending)
        self.b.operation_finished.emit("configure_temperature", True, None)
        self.assertFalse(self.panel._temp_request_pending)

    def test_synchronous_temperature_request_failure_releases_only_its_guard(self):
        self.panel.backend_combo.setCurrentIndex(1)
        self.panel._set_temperature()
        with patch.object(self.b, "stop_sample_temperature_control_async", side_effect=RuntimeError("rejected")):
            self.panel._stop_temperature()
        self.assertTrue(self.panel._temp_request_pending)
        self.assertTrue(self.panel.temp_stop_button.isEnabled())
        self.b.operation_finished.emit("configure_temperature", True, None)
        self.assertFalse(self.panel._temp_request_pending)
        with patch.object(self.b, "configure_sample_temperature_async", side_effect=RuntimeError("rejected")):
            self.panel._set_temperature()
        self.assertFalse(self.panel._temp_request_pending)
        self.assertTrue(self.panel.temp_control_button.isEnabled())

    def test_1000_snapshot_cannot_overwrite_2100_temperature_or_capabilities(self):
        self.a.snapshot_updated.emit(SimpleNamespace(field_t=0, output_field_t=0,
            output_current_a=0, sweep_state="pause", heater_on=False,
            magnet_voltage_v=0, output_voltage_v=0, temperature_k=42,
            status=SimpleNamespace(standby=True, quench=False)))
        self.assertEqual(self.panel.magnet_temperature.text(), "3.100 K")
        self.assertIs(self.panel._last_capabilities, self.capabilities)
        self.panel.backend_combo.setCurrentIndex(1)
        self.assertTrue(self.panel.temp_control_button.isEnabled())

    def test_late_1000_result_updates_only_1000_state(self):
        self.bar.target.setValue(20)
        self.bar._set_target()
        self.panel.sample_target.setValue(6)
        self.panel.backend_combo.setCurrentIndex(1)
        self.b.temperature_updated.emit(AttoDRY2100TemperatureSnapshot(sample_temperature_k=5.5))
        self.ls.control_result.emit(control_state(20, operation="sample_control"))
        self.assertEqual(self.bar._target, 20)
        self.assertEqual(self.panel.sample_target.value(), 6)
        self.assertEqual(self.panel.sample_temperature.text(), "5.500 K")
        self.assertFalse(self.bar.set_button.isEnabled())
        self.panel.backend_combo.setCurrentIndex(0)
        self.assertTrue(self.bar.set_button.isEnabled())

    def test_reconnected_2100_waits_for_fresh_control_capabilities(self):
        self.panel.backend_combo.setCurrentIndex(1)
        self.assertTrue(self.panel.temp_control_button.isEnabled())
        self.b.disconnected.emit()
        self.assertIsNone(self.panel._last_capabilities)
        self.panel._stop_temperature()
        self.assertEqual(self.b.calls, [])
        self.panel._on_connected("2100", object())
        self.assertFalse(self.panel.temp_control_button.isEnabled())
        self.b.snapshot_updated.emit(AttoDRY2100Snapshot(0, capabilities=self.capabilities))
        self.assertTrue(self.panel.temp_control_button.isEnabled())

    def test_backend_notification_observes_already_updated_temperature_controls(self):
        states = []
        self.panel.backend_changed.connect(lambda backend: states.append(
            (backend, self.bar._backend, self.panel.temp1000_group.isHidden(), self.panel.temp_group.isHidden())))
        self.panel.backend_combo.setCurrentIndex(1)
        self.panel.backend_combo.setCurrentIndex(0)
        self.assertEqual(states, [("2100", "2100", True, False), ("1000", "1000", False, True)])

    def test_summary_uses_confirmed_target_and_rejects_stale_faulty_readings(self):
        self.ls.control_result.emit(control_state(10))
        self.bar.target.setValue(25)
        self.bar.on_snapshot(_snapshot(self.now[0]))
        self.assertIn("target: 10.000 K", self.bar.summary_text())
        self.assertNotIn("25.000", self.bar.summary_text())
        self.now[0] += 4
        self.assertIn("Unavailable (stale telemetry)", self.bar.summary_text())
        self.bar.on_snapshot(_snapshot(self.now[0], status="32"))
        self.assertIn("Unavailable (sensor fault)", self.bar.summary_text())
        self.bar._set_target()
        self.assertIn("target: unconfirmed", self.bar.summary_text())
        self.assertIn("Applying settings", self.bar.summary_text())


class TemperatureSystemWindowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance()
        with patch("app.ui.dock.ConnDock._start_scan"):
            cls.window = MainWindow()
        cls.window.show()
        cls.app.processEvents()

    @classmethod
    def tearDownClass(cls):
        w = cls.window
        w.magnet1000.shutdown()
        w.lakeshore335.shutdown()
        w.magnet2100.shutdown()
        w.device_manager.shutdown()
        w.hide()
        w.deleteLater()
        cls.app.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)
        cls.app.processEvents()

    def setUp(self):
        w = self.window
        w.magnet_panel.backend_combo.setCurrentIndex(0)
        w.sample_temperature_bar._on_disconnected()
        w.sample_temperature_bar.wait_check.setChecked(False)
        w.magnet_panel._on_disconnected("2100")
        w._experiment_2100_temperature = None
        w._update_temperature_summary()

    def test_main_summary_follows_selected_system_without_copying_targets(self):
        w = self.window
        bar = w.sample_temperature_bar
        bar._on_connected()
        bar._on_control_result(control_state(10))
        bar.on_snapshot(_snapshot(time.monotonic()))
        w._experiment_2100_temperature = AttoDRY2100TemperatureSnapshot(
            sample_temperature_k=5.5, sample_setpoint_k=6, vti_temperature_k=4.5,
            sample_control_active=True)
        w.magnet_panel._connected["2100"] = True
        w._update_temperature_summary()
        self.assertIn("attoDRY1000 / LS335", w.temperature_summary_label.text())
        self.assertIn("10.000 K", w.temperature_summary_label.text())
        w.magnet_panel.backend_combo.setCurrentIndex(1)
        text = w.temperature_summary_label.text()
        self.assertIn("attoDRY2100 / SDK", text)
        self.assertIn("Sample: 5.500 K", text)
        self.assertIn("target: 6.000 K", text)
        self.assertIn("VTI: 4.500 K", text)
        self.assertNotIn("10.000 K", text)
        w.magnet_panel.backend_combo.setCurrentIndex(0)
        self.assertIn("target: 10.000 K", w.temperature_summary_label.text())

    def test_stale_and_disconnected_2100_summary_does_not_show_old_readings(self):
        w = self.window
        w.magnet_panel._connected["2100"] = True
        w._experiment_2100_temperature = AttoDRY2100TemperatureSnapshot(
            monotonic_s=time.monotonic() - 20, sample_temperature_k=5.5)
        w.magnet_panel.backend_combo.setCurrentIndex(1)
        self.assertIn("readback stale", w.temperature_summary_label.text())
        self.assertNotIn("5.500", w.temperature_summary_label.text())
        w.magnet2100.disconnected.emit()
        w._update_temperature_summary()
        self.assertIsNone(w._experiment_2100_temperature)
        self.assertIn("Disconnected", w.temperature_summary_label.text())

    def test_temperature_shortcut_opens_correct_panel_and_moves_existing_control(self):
        w = self.window
        self.assertTrue(w.magnet_panel.temp1000_group.isAncestorOf(w.sample_temperature_bar))
        self.assertFalse(w.centralWidget().isAncestorOf(w.sample_temperature_bar))
        self.assertFalse(w.temperature_summary.findChildren(QtWidgets.QDoubleSpinBox))
        for index, group in ((0, w.magnet_panel.temp1000_group), (1, w.magnet_panel.temp_group)):
            w.magnet_panel.backend_combo.setCurrentIndex(index)
            w.instrument_workspace.pages.setCurrentIndex(0)
            w.instrument_dock.hide()
            w.temperature_controls_button.click()
            self.app.processEvents()
            self.assertTrue(w.instrument_dock.isVisible())
            self.assertEqual(w.instrument_workspace.pages.currentIndex(), 1)
            scroll = w.instrument_workspace.pages.widget(1)
            visible = group.mapTo(scroll.viewport(), group.rect().center())
            self.assertTrue(scroll.viewport().rect().contains(visible))

    def test_measurement_wait_blocks_only_1000_and_survives_switching(self):
        w = self.window
        w.sample_temperature_bar.wait_check.setChecked(True)
        for tab in w._measurement_tabs():
            self.assertIn("sample_temperature", tab.run_panel._external_start_blocks)
        w.magnet_panel.backend_combo.setCurrentIndex(1)
        for tab in w._measurement_tabs():
            self.assertNotIn("sample_temperature", tab.run_panel._external_start_blocks)
        w.magnet_panel.backend_combo.setCurrentIndex(0)
        for tab in w._measurement_tabs():
            self.assertIn("sample_temperature", tab.run_panel._external_start_blocks)


if __name__ == "__main__":
    unittest.main()
