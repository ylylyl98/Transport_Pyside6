import os
import unittest
from unittest.mock import patch
if os.environ.get("TRANSPORT_OFFLINE_VERIFICATION") != "1":
    raise unittest.SkipTest("Requires offline wrapper")
from PySide6 import QtCore, QtTest, QtWidgets
from app.ui.main_window import MainWindow


class WorkspaceLayoutTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance()
        with patch("app.ui.dock.ConnDock._start_scan"):
            cls.window = MainWindow()
        cls.window.show()
        cls.app.processEvents()

    @classmethod
    def tearDownClass(cls):
        cls.window.close()
        cls.app.processEvents()

    def test_instruments_share_left_workspace_without_right_docks(self):
        w = self.window
        docks = w.findChildren(QtWidgets.QDockWidget)
        self.assertEqual(len(docks), 1)
        self.assertEqual(w.dockWidgetArea(w.instrument_dock), QtCore.Qt.DockWidgetArea.LeftDockWidgetArea)
        self.assertEqual(w.instrument_workspace.pages.count(), 3)
        self.assertTrue(w.instrument_workspace.isAncestorOf(w.magnet_panel))
        self.assertTrue(w.instrument_workspace.isAncestorOf(w.lockin_panel))
        self.assertTrue(w.instrument_workspace.isAncestorOf(w.conn_dock.exp_manual))
        self.assertFalse(w.instrument_workspace.isAncestorOf(w.tab_dual.control_scroll))

    def test_setup_moves_existing_widgets_without_duplicate_controls(self):
        w = self.window
        w.instrument_workspace.show_devices()
        self.assertTrue(w.instrument_workspace.devices_dialog.isVisible())
        self.assertTrue(w.instrument_workspace.devices_dialog.isAncestorOf(w.conn_dock.cbo_g1))
        self.assertTrue(w.instrument_workspace.devices_dialog.isAncestorOf(w.conn_dock._protection_controls["g1"][0]))
        w.instrument_workspace.devices_dialog.close()
        w.instrument_workspace.show_experiment()
        self.assertTrue(w.instrument_workspace.experiment_dialog.isAncestorOf(w.conn_dock.ed_base))
        w.instrument_workspace.experiment_dialog.close()

    def test_global_stop_remains_visible_and_keeps_signal_route(self):
        w = self.window
        w.instrument_dock.hide()
        w.command_bar.hide()
        self.app.processEvents()
        self.assertTrue(w.conn_dock.btn_stop.isVisible())
        spy = QtTest.QSignalSpy(w.conn_dock.stop_requested)
        w.conn_dock.stop_requested.disconnect(w.on_emergency_stop)
        try:
            w.conn_dock.btn_stop.click()
            self.assertEqual(spy.count(), 1)
        finally:
            w.conn_dock.stop_requested.connect(w.on_emergency_stop)
            w.instrument_dock.show()
            w.command_bar.show()

    def test_parameter_and_log_toggles_leave_run_controls_available(self):
        w = self.window
        tab = w.tab_dual
        w.tabs.setCurrentWidget(tab)
        tab.parameters_button.setChecked(False)
        self.assertTrue(tab.control_scroll.isHidden())
        self.assertTrue(tab.run_panel.isVisible())
        tab.parameters_button.setChecked(True)
        tab.log_button.setChecked(False)
        self.assertTrue(tab.log.isHidden())
        tab.log.appendPlainText("Offline test message")
        self.assertEqual(tab.log_summary.text(), "Offline test message")
        tab.log_button.setChecked(True)
        self.assertFalse(tab.log.isHidden())
        tab.log_button.setChecked(False)

    def test_running_experiment_is_visible_after_switching_tabs(self):
        w = self.window
        try:
            w.tab_dual.run_panel.set_running(True)
            w.tabs.setCurrentWidget(w.tab_cosweep)
            self.assertIn("Vds Sweep", w.active_run_button.text())
            w.active_run_button.click()
            self.assertIs(w.tabs.currentWidget(), w.tab_dual)
        finally:
            w.tab_dual.run_panel.set_running(False)

    def test_reloading_analysis_keeps_active_measurement_and_instrument_owners(self):
        import time
        w = self.window
        launcher = w.tab_curve_compare
        owners = (w.device_manager, w.magnet1000, w.magnet2100, w.bfield_transport_controller)
        messages = []
        launcher.message_received.connect(messages.append)
        w.tab_dual.run_panel.set_running(True)
        try:
            launcher.open_viewer()
            end = time.monotonic() + 15
            while not any(m.get("event") == "ready" for m in messages) and time.monotonic() < end:
                self.app.processEvents()
                QtTest.QTest.qWait(20)
            self.assertTrue(any(m.get("event") == "ready" for m in messages))
            first_pid = launcher.process.processId()
            launcher.reload_button.click()
            end = time.monotonic() + 15
            while not any(m.get("event") == "session_loaded" for m in messages) and time.monotonic() < end:
                self.app.processEvents()
                QtTest.QTest.qWait(20)
            self.assertTrue(any(m.get("event") == "session_loaded" for m in messages))
            self.assertNotEqual(launcher.process.processId(), first_pid)
            self.assertTrue(w.tab_dual.run_panel.operation_active())
            self.assertIn("Vds Sweep", w.active_run_button.text())
            self.assertEqual(owners, (w.device_manager, w.magnet1000, w.magnet2100, w.bfield_transport_controller))
        finally:
            w.tab_dual.run_panel.set_running(False)
            launcher.message_received.disconnect(messages.append)
            launcher._send("close")
            end = time.monotonic() + 5
            while launcher.process.state() != QtCore.QProcess.ProcessState.NotRunning and time.monotonic() < end:
                self.app.processEvents()
                QtTest.QTest.qWait(20)

    def test_history_entry_does_not_participate_in_measurement_control(self):
        w = self.window
        self.assertNotIn(w.tab_curve_compare, w._measurement_tabs())
        self.assertFalse(hasattr(w.tab_curve_compare, "run_panel"))
        try:
            w.tab_dual.run_panel.set_running(True)
            with patch.object(w.tab_curve_compare, "open_viewer"):
                w.tabs.setCurrentWidget(w.tab_curve_compare)
                self.assertIn("Vds Sweep", w.active_run_button.text())
                w.active_run_button.click()
                self.assertIs(w.tabs.currentWidget(), w.tab_dual)
        finally:
            w.tab_dual.run_panel.set_running(False)

    def test_measurement_completion_automatically_creates_png_with_viewer_closed(self):
        import json
        import tempfile
        import time
        from pathlib import Path
        from PySide6 import QtTest
        w = self.window
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "run.csv"
            path.write_text("Vds,Ids_DC\nV,A\n0,1e-9\n1,2e-9\n")
            metadata = path.with_name("run_metadata.json")
            metadata.write_text(json.dumps({"measurement": "vds_sweep", "status": "finished", "csv_path": str(path)}))
            tab = w.tab_dual
            previous = (tab.p.output_csv_path, tab.p.output_metadata_path)
            tab.p.output_csv_path, tab.p.output_metadata_path = str(path), str(metadata)
            try:
                w.tab_curve_compare.auto_png_check.setChecked(True)
                tab.run_panel.set_running(True)
                tab.run_panel.set_running(False)
                deadline = time.monotonic() + 15
                while time.monotonic() < deadline and not (path.parent / "plots/run_Ids_DC.png").exists():
                    self.app.processEvents()
                    QtTest.QTest.qWait(30)
                self.assertTrue((path.parent / "plots/run_Ids_DC.png").exists())
                self.assertEqual(w.tab_curve_compare.process.state(), QtCore.QProcess.ProcessState.NotRunning)
            finally:
                tab.run_panel.set_running(False)
                tab.p.output_csv_path, tab.p.output_metadata_path = previous

    def test_offline_render_fits_and_global_stop_is_not_in_overflow(self):
        from pathlib import Path
        w = self.window
        w.resize(1400, 860)
        w.tabs.setCurrentWidget(w.tab_dual)
        w.instrument_workspace.pages.setCurrentIndex(0)
        self.app.processEvents()
        stop = w.conn_dock.btn_stop
        self.assertTrue(stop.isVisible())
        corner = stop.mapTo(w, stop.rect().bottomRight())
        self.assertLessEqual(corner.x(), w.width())
        self.assertGreater(w.tab_dual.plot.width(), 400)
        scale = w.devicePixelRatioF()
        filename = "workspace_preview.png" if scale == 1 else f"workspace_preview_{scale:g}x.png"
        w.grab().save(str(Path(__file__).resolve().parents[1] / filename))
        w.resize(1100, 760)
        self.app.processEvents()
        self.assertTrue(stop.isVisible())
        self.assertGreaterEqual(w.stop_strip.height(), stop.height())
        self.assertLessEqual(stop.mapTo(w, stop.rect().bottomRight()).x(), w.width())
        w.resize(1400, 860)

    def test_field_series_remains_active_between_scans(self):
        from unittest.mock import PropertyMock
        w = self.window
        with patch.object(type(w.gate_scan_field_batch), "active", new_callable=PropertyMock, return_value=True):
            w.gate_scan_field_batch.state_changed.emit("settling", "offline")
            self.assertIn("B-field Gate Scan", w.active_run_button.text())
            w.active_run_button.click()
            self.assertIs(w.tabs.currentWidget(), w.tab_bfield_gate_scan)
        w.gate_scan_field_batch.finished.emit()
        self.assertFalse(w.active_run_button.isEnabled())
