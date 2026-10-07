"""Identify configured systems using fake controller connection acknowledgements."""
import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

if os.environ.get("TRANSPORT_OFFLINE_VERIFICATION") != "1":
    raise unittest.SkipTest("Use verify_offline.py to isolate hardware and user settings")

from PySide6 import QtCore, QtTest, QtWidgets
from app.devices.aps100_attodry1000_adapter import APS100Identity
from app.devices.attodry2100_adapter import AttoDRY2100Identity
from app.ui.magnet_panel import MagnetPanel
from app.ui.sample_temperature_bar import SampleTemperatureBar
from app.ui.main_window import MainWindow
from test_magnet_control import _Fake1000, _Fake2100
from test_sample_temperature_control_ui import _FakeLakeShoreController
from utils.config import cfg

APS = APS100Identity("attocube", "APS100", "offline", "1", "0")
SDK = AttoDRY2100Identity(host="offline", details={"device_name": "attoDRY2100"})


class SystemAutoDetectionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance()

    def setUp(self):
        self.a, self.b, self.ls = _Fake1000(), _Fake2100(), _FakeLakeShoreController()
        self.bar = SampleTemperatureBar(self.ls, SimpleNamespace(maximum_reading_age_s=3), compact=True)
        self.bar._age_timer.stop()
        self.panel = MagnetPanel(self.a, self.b, lakeshore335=self.ls,
                                 sample_temperature_control=self.bar)
        self.panel.review.setChecked(True)

    def tearDown(self):
        self.panel.deleteLater()
        self.app.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)
        self.app.processEvents()

    def fail_aps(self, error="not an APS100"):
        self.a.error.emit("APS100 connection failed: " + error)

    def fail_sdk(self, error="SDK unavailable"):
        self.b.error.emit("attoDRY2100 connect failed: " + error)
        self.b.fault.emit(error)
        self.b.operation_finished.emit("connect", False, RuntimeError(error))

    def test_startup_and_lakeshore_alone_do_not_identify_or_connect_a_system(self):
        self.ls.connected.emit(object())
        QtTest.QTest.qWait(550)
        self.assertIn("No system identified", self.panel.system_detection_label.text())
        self.assertEqual(self.panel._connected, {"1000": False, "2100": False})
        self.assertEqual((self.a.calls, self.b.calls, self.ls.control_requests), ([], [], []))

    def test_only_connected_sdk_selects_2100_temperature_area_automatically(self):
        self.b.connected.emit(SDK)
        self.assertEqual(self.panel._backend, "2100")
        self.assertTrue(self.panel.auto_system_check.isChecked())
        self.assertTrue(self.panel.temp1000_group.isHidden())
        self.assertFalse(self.panel.temp_group.isHidden())
        self.assertIn("Identified: attoDRY2100", self.panel.system_detection_label.text())
        self.assertEqual(self.b.calls, [("polling", True)])
        self.assertEqual(self.ls.control_requests, [])

    def test_only_connected_aps_selects_1000_after_enabling_auto(self):
        self.panel.backend_combo.setCurrentIndex(1)
        self.assertFalse(self.panel.auto_system_check.isChecked())
        self.a.connected.emit(APS)
        self.assertEqual(self.panel._backend, "2100")
        self.panel.auto_system_check.setChecked(True)
        self.assertEqual(self.panel._backend, "1000")
        self.assertEqual(self.a.calls, [])

    def test_detection_queues_configured_connections_once_without_control_writes(self):
        self.panel.detect_system_button.click()
        self.assertEqual(self.a.calls, [("connect", cfg.magnet.visa_resource, False)])
        self.assertEqual(self.b.calls, [("connect",)])
        self.assertEqual(self.panel._detect_pending, {"1000", "2100"})
        self.panel._detect_system()
        self.panel._connect_selected()
        self.assertEqual(len(self.a.calls), 1)
        self.assertEqual(len(self.b.calls), 1)
        self.assertFalse(self.panel.detect_system_button.isEnabled())
        self.assertTrue(self.panel.stop_button.isEnabled())
        self.assertEqual(self.ls.control_requests, [])
        self.assertEqual(self.ls.off_requests, 0)

    def test_detection_waits_for_both_results_then_uses_sdk_if_aps_fails(self):
        self.panel._detect_system()
        self.b.connected.emit(SDK)
        self.assertEqual(self.panel._backend, "1000")
        self.assertEqual(self.panel.connection_label.text(), "Disconnected")
        self.assertEqual(self.panel._detect_pending, {"1000"})
        self.fail_aps()
        self.assertEqual(self.panel._backend, "2100")
        self.assertFalse(self.panel._detect_pending)
        self.assertIn("Identified: attoDRY2100", self.panel.system_detection_label.text())
        self.assertEqual(self.panel.fault_label.text(), "None")
        self.assertIn("not an APS100", self.panel.system_detection_label.toolTip())

    def test_sdk_failure_does_not_mark_successful_1000_connection_as_faulted(self):
        self.panel._detect_system()
        self.a.connected.emit(APS)
        self.fail_sdk()
        self.assertEqual(self.panel._backend, "1000")
        self.assertFalse(self.panel._detect_pending)
        self.assertIn("Identified: attoDRY1000", self.panel.system_detection_label.text())
        self.assertEqual(self.panel.fault_label.text(), "None")
        self.assertTrue(self.panel.detect_system_button.isEnabled())

    def test_both_systems_connected_keep_selection_and_request_manual_choice(self):
        self.bar.target.setValue(20)
        self.panel.sample_target.setValue(6)
        self.panel._detect_system()
        self.b.connected.emit(SDK)
        self.a.connected.emit(APS)
        self.assertEqual(self.panel._backend, "1000")
        self.assertIn("Both systems connected", self.panel.system_detection_label.text())
        self.panel.backend_combo.setCurrentIndex(1)
        self.assertFalse(self.panel.auto_system_check.isChecked())
        self.a.disconnected.emit()
        self.assertEqual(self.panel._backend, "2100")
        self.assertEqual(self.bar.target.value(), 20)
        self.assertEqual(self.panel.sample_target.value(), 6)

    def test_two_failed_connections_report_unidentified_and_allow_retry(self):
        self.panel._detect_system()
        self.fail_aps("missing resource")
        self.fail_sdk("missing SDK")
        self.assertFalse(self.panel._detect_pending)
        self.assertIn("No system identified", self.panel.system_detection_label.text())
        self.assertIn("missing resource", self.panel.system_detection_label.toolTip())
        self.assertIn("missing SDK", self.panel.system_detection_label.toolTip())
        self.assertTrue(self.panel.detect_system_button.isEnabled())
        self.panel._detect_system()
        self.assertEqual(len(self.a.calls), 2)
        self.assertEqual(len(self.b.calls), 2)

    def test_detection_preserves_existing_connection_and_only_tries_missing_system(self):
        self.b.connected.emit(SDK)
        self.b.calls.clear()
        self.panel._detect_system()
        self.assertEqual(self.b.calls, [])
        self.assertEqual(self.a.calls, [("connect", cfg.magnet.visa_resource, False)])
        self.fail_aps()
        self.assertEqual(self.panel._backend, "2100")

    def test_detect_requires_existing_configuration_review(self):
        self.panel.review.setChecked(False)
        self.panel._detect_system()
        self.assertFalse(self.panel._detect_pending)
        self.assertEqual((self.a.calls, self.b.calls), ([], []))
        self.assertIn("Commissioning review required", self.panel.fault_label.text())

    def test_busy_or_reserved_operations_block_detection(self):
        for owner, name in ((self.panel, "_busy_1000"), (self.panel, "_busy_2100"),
                            (self.panel, "_aps_exclusive"), (self.panel, "_temp_request_pending"),
                            (self.bar, "_pending_operation"), (self.b, "has_pending_work")):
            with self.subTest(name=name):
                setattr(owner, name, True)
                self.panel._update_buttons()
                self.assertFalse(self.panel.detect_system_button.isEnabled())
                self.panel._detect_system()
                self.assertEqual((self.a.calls, self.b.calls), ([], []))
                setattr(owner, name, False)
        self.panel._update_buttons()
        self.assertTrue(self.panel.detect_system_button.isEnabled())

    def test_auto_switch_waits_for_measurement_completion_without_any_command(self):
        running = [True]
        self.panel.auto_selection_allowed = lambda: not running[0]
        self.b.connected.emit(SDK)
        self.assertEqual(self.panel._backend, "1000")
        self.assertEqual(self.panel._deferred_auto_backend, "2100")
        self.panel._retry_auto_selection()
        self.assertEqual(self.panel._backend, "1000")
        running[0] = False
        self.panel._retry_auto_selection()
        self.assertEqual(self.panel._backend, "2100")
        self.assertIsNone(self.panel._deferred_auto_backend)
        self.assertEqual(self.b.calls, [("polling", True)])

    def test_manual_selection_cancels_a_deferred_switch(self):
        self.panel.auto_selection_allowed = lambda: False
        self.b.connected.emit(SDK)
        self.assertEqual(self.panel._deferred_auto_backend, "2100")
        self.panel.auto_system_check.setChecked(False)
        self.panel.auto_selection_allowed = lambda: True
        self.panel._retry_auto_selection()
        self.assertEqual(self.panel._backend, "1000")
        self.assertIsNone(self.panel._deferred_auto_backend)

    def test_auto_switch_waits_for_sdk_requests_to_drain(self):
        self.b.has_pending_work = True
        self.b.connected.emit(SDK)
        self.assertEqual(self.panel._backend, "1000")
        self.assertEqual(self.panel._deferred_auto_backend, "2100")
        self.panel._retry_auto_selection()
        self.assertEqual(self.panel._backend, "1000")
        self.b.has_pending_work = False
        self.panel._retry_auto_selection()
        self.assertEqual(self.panel._backend, "2100")
        self.assertEqual(self.b.calls, [("polling", True)])

    def test_explicitly_choosing_the_current_system_turns_auto_off(self):
        self.a.connected.emit(APS)
        self.b.connected.emit(SDK)
        self.panel.backend_combo.activated.emit(0)
        self.assertFalse(self.panel.auto_system_check.isChecked())
        self.a.disconnected.emit()
        self.assertEqual(self.panel._backend, "1000")
        self.assertIn("Manual selection: 1000", self.panel.system_detection_label.text())

    def test_initial_aps_disconnect_is_not_a_finished_detection_attempt(self):
        self.panel._detect_system()
        self.a.disconnected.emit()
        self.assertIn("1000", self.panel._detect_pending)
        self.assertFalse(self.panel.detect_system_button.isEnabled())
        self.fail_sdk()
        self.a.connected.emit(APS)
        self.assertFalse(self.panel._detect_pending)
        self.assertIn("Identified: attoDRY1000", self.panel.system_detection_label.text())

    def test_synchronous_connection_exception_finishes_attempt_without_stuck_buttons(self):
        with patch.object(self.a, "connect_instrument", side_effect=RuntimeError("APS rejected")), \
             patch.object(self.b, "connect_async", side_effect=RuntimeError("SDK rejected")):
            self.panel._detect_system()
        self.assertFalse(self.panel._detect_pending)
        self.assertFalse(self.panel._aps_connect_pending)
        self.assertFalse(self.panel._sdk_connect_pending)
        self.assertTrue(self.panel.detect_system_button.isEnabled())

    def test_pending_manual_sdk_connection_cannot_be_duplicated_by_detect(self):
        self.panel.backend_combo.setCurrentIndex(1)
        self.panel._connect_selected()
        self.panel._connect_selected()
        self.panel._detect_system()
        self.assertEqual(self.b.calls, [("connect",)])
        self.assertEqual(self.a.calls, [])
        self.b.operation_finished.emit("connect", False, RuntimeError("unavailable"))
        self.assertFalse(self.panel._sdk_connect_pending)


class SystemDetectionMeasurementTests(unittest.TestCase):
    def test_window_holds_system_selection_until_running_measurement_finishes(self):
        app = QtWidgets.QApplication.instance()
        with patch("app.ui.dock.ConnDock._start_scan"):
            w = MainWindow()
        try:
            w.tab_dual.run_panel.set_running(True)
            self.assertFalse(w.magnet_panel.detect_system_button.isEnabled())
            w.magnet_panel._on_connected("2100", SDK)
            w.magnet_panel._temp_timer.stop()
            self.assertEqual(w.magnet_panel._backend, "1000")
            self.assertEqual(w.magnet_panel._deferred_auto_backend, "2100")
            self.assertFalse(w.magnet_panel.detect_system_button.isEnabled())
            w.tab_dual.run_panel.set_running(False)
            w.magnet_panel._retry_auto_selection()
            self.assertEqual(w.magnet_panel._backend, "2100")
            self.assertIn("attoDRY2100 / SDK", w.temperature_summary_label.text())
        finally:
            w.magnet1000.shutdown()
            w.lakeshore335.shutdown()
            w.magnet2100.shutdown()
            w.device_manager.shutdown()
            w.hide()
            w.deleteLater()
            app.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)
            app.processEvents()
