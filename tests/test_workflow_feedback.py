import os
import unittest
from unittest.mock import patch
if os.environ.get("TRANSPORT_OFFLINE_VERIFICATION") != "1":
    raise unittest.SkipTest("Requires offline wrapper")
from PySide6 import QtCore, QtTest, QtWidgets
from app.ui.main_window import MainWindow


class WorkflowFeedbackTests(unittest.TestCase):
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

    def test_global_stop_is_inset_left_and_resize_never_emits_stop(self):
        w = self.window
        stop = w.conn_dock.btn_stop
        spy = QtTest.QSignalSpy(w.conn_dock.stop_requested)
        positions = []
        for width in (1400, 1100, 1600):
            w.resize(width, 860)
            self.app.processEvents()
            pos = stop.mapTo(w, QtCore.QPoint())
            positions.append((pos.x(), pos.y()))
            self.assertGreaterEqual(pos.x(), 20)
            self.assertLess(pos.x(), 100)
            self.assertGreaterEqual(pos.y(), w.workspace_menu.height() + 8)
            self.assertTrue(stop.isVisible())
        self.assertEqual(len(set(positions)), 1)
        w.showMaximized()
        self.app.processEvents()
        self.assertTrue(stop.isVisible())
        w.showNormal()
        self.app.processEvents()
        self.assertEqual(spy.count(), 0)
        w.resize(1400, 860)

    def test_gate_invalid_ratio_is_inline_and_repair_preserves_other_blocks(self):
        t = self.window.tab_gate_scan
        self.window.tabs.setCurrentWidget(t)
        t.rad_mode_derived.setChecked(True)
        old = t.sp_ratio.value()
        t.run_panel.set_start_blocked("test-safety-hold", True)
        try:
            t.sp_ratio.setValue(0)
            t.workflow.refresh()
            self.assertIn("non-zero ratio", t.workflow.summary.text())
            self.assertTrue(t.sp_ratio.property("invalid"))
            t.parameters_button.setChecked(False)
            t.workflow.review_button.click()
            self.assertFalse(t.control_scroll.isHidden())
            t.sp_ratio.setValue(1)
            t.workflow.refresh()
            self.assertFalse(t.sp_ratio.property("invalid"))
            self.assertIn("test-safety-hold", t.run_panel._external_start_blocks)
            self.assertFalse(t.btn_start.isEnabled())
        finally:
            t.sp_ratio.setValue(old)
            t.rad_mode_raw.setChecked(True)
            t.run_panel.set_start_blocked("test-safety-hold", False)

    def test_cosweep_zero_step_is_caught_before_start_without_dialogs(self):
        t = self.window.tab_cosweep
        old = t.sp_vtg_step.value()
        with patch.object(QtWidgets.QMessageBox, "warning") as warning:
            try:
                t.sp_vtg_step.setValue(0)
                t.workflow.refresh()
                self.assertIn("Step", t.workflow.summary.text())
                self.assertTrue(t.sp_vtg_step.property("invalid"))
                warning.assert_not_called()
            finally:
                t.sp_vtg_step.setValue(old)
                t.workflow.refresh()

    def test_missing_sample_settings_visible_even_with_parameters_hidden(self):
        w = self.window
        t = w.tab_dual
        w.tabs.setCurrentWidget(t)
        old = w.conn_dock.ed_user.text()
        try:
            w.conn_dock.ed_user.clear()
            t.parameters_button.setChecked(False)
            t.workflow.refresh()
            self.assertIn("Operator", t.workflow.summary.text())
            self.app.processEvents()
            self.assertTrue(t.workflow.summary.isVisible())
            self.assertGreaterEqual(t.workflow.summary.height(), t.workflow.summary.heightForWidth(t.workflow.summary.width()))
            self.assertFalse(t.btn_start.isEnabled())
        finally:
            w.conn_dock.ed_user.setText(old)
            t.parameters_button.setChecked(True)

    def test_status_phases_distinguish_stopping_cleanup_and_completion(self):
        p = self.window.tab_dual.run_panel
        for state, expected in (("positioning", "Positioning"), ("thermal_wait", "Waiting"),
                                ("measuring", "Acquiring"), ("stopping", "Stopping"),
                                ("cleanup", "Stopping"), ("finished", "Complete"), ("stopped", "Stopped")):
            p.set_status_text("Detail", state)
            self.assertEqual(p.lbl_phase.text(), expected)
        self.assertEqual(p.btn_stop.text(), "Stop measurement")
        p.set_status_text("Idle", "idle")

    def test_batch_phase_is_visible_in_run_panel(self):
        t = self.window.tab_bfield_gate_scan
        t._on_batch_state_changed("thermal_wait", "Waiting for stable temperature")
        self.assertEqual(t.run_panel.lbl_phase.text(), "Waiting")
        self.assertIn("stable temperature", t.run_panel.lbl_status.text())
        t._on_batch_progress_changed(1, 5, "Settling")
        self.assertIn("2/5", t.run_panel.progress.format())
        t._on_batch_finished()
        self.assertEqual(t.run_panel.lbl_phase.text(), "Complete")

    def test_batch_ignores_unrelated_single_scan_output_collision(self):
        from types import SimpleNamespace
        t = self.window.tab_bfield_gate_scan
        old = t.lbl_output_warning.text()
        try:
            with patch.object(t, "save", SimpleNamespace(user="tester", device_id="sample", base="offline")), \
                 patch("app.ui.measurement_workflow.parameter_issues", return_value=[]):
                t.lbl_output_warning.setText("CSV already exists")
                t.workflow.refresh()
                self.assertNotIn("workflow-inputs", t.run_panel._external_start_blocks)
                self.assertNotIn("CSV already exists", t.workflow.summary.text())
        finally:
            t.lbl_output_warning.setText(old)
            t.workflow.refresh()

    def test_temperature_hold_updates_feedback_without_touching_inputs(self):
        t = self.window.tab_dual
        try:
            t.run_panel.set_start_blocked("sample_temperature", True)
            self.app.processEvents()
            self.assertIn("temperature", t.workflow.summary.text())
        finally:
            t.run_panel.set_start_blocked("sample_temperature", False)
            self.app.processEvents()
        self.assertNotIn("Wait for the sample temperature", t.workflow.summary.text())

    def test_busy_device_operation_is_an_explicit_start_block(self):
        t = self.window.tab_dual
        with patch.object(t.device_manager, "is_busy", return_value=True):
            t.workflow.refresh()
            self.assertIn("device operation", t.workflow.summary.text())
            self.assertIn("workflow-inputs", t.run_panel._external_start_blocks)
            self.assertFalse(t.btn_start.isEnabled())
        t.workflow.refresh()

    def test_source_selectors_are_not_hidden_in_output_settings(self):
        w = self.window
        for t in (w.tab_dual, w.tab_gate_scan, w.tab_photocurrent):
            self.assertFalse(t.exp_output.isAncestorOf(t.cbo_source))
            self.assertTrue(t.control_widget.isAncestorOf(t.cbo_source))

    def test_tab_refinement_preserves_controls_and_removes_duplicate_feedback(self):
        w = self.window
        t = w.tab_dual
        self.assertTrue(t.lbl_connection_hint.isHidden())
        self.assertEqual(t.btn_set_vtg.text(), "Apply now")
        self.assertTrue(t.exp_timing.isAncestorOf(t.sp_vds_ramp))
        self.assertFalse(w.tab_bfield_gate_scan.condition_details_section.is_expanded())
        w.tab_bfield_gate_scan.condition_details_section.set_expanded(True)
        self.assertFalse(w.tab_bfield_gate_scan.condition_details.isHidden())
        w.tab_bfield_gate_scan.condition_details_section.set_expanded(False)
        self.assertTrue(t.workflow.review_button.isHidden())

    def test_log_summary_only_appears_when_useful(self):
        t = self.window.tab_dual
        t.log.clear()
        t.log_button.setChecked(False)
        self.assertTrue(t.log_summary.isHidden())
        t.log.appendPlainText("Offline status")
        self.assertFalse(t.log_summary.isHidden())
        t.log_button.setChecked(True)
        self.assertTrue(t.log_summary.isHidden())
        t.log_button.setChecked(False)
        t.log.clear()

    def test_render_dense_tab_controls(self):
        from pathlib import Path
        w = self.window
        w.resize(1400, 860)
        for name, tab in (("map", w.tab_cosweep), ("photocurrent", w.tab_photocurrent)):
            w.tabs.setCurrentWidget(tab)
            self.app.processEvents()
            w.grab().save(str(Path(__file__).resolve().parents[1] / f"tab_{name}_preview.png"))
            if name == "map":
                tab.control_scroll.ensureWidgetVisible(tab.btn_set_vtg)
                self.app.processEvents()
                w.grab().save(str(Path(__file__).resolve().parents[1] / "tab_map_axes_preview.png"))

    def test_quiet_readback_completion_releases_workflow_busy_block(self):
        from types import SimpleNamespace
        t = self.window.tab_dual
        old = t.lbl_output_warning.text()
        busy = [True]
        try:
            with patch.object(t, "save", SimpleNamespace(user="tester", device_id="sample", base="offline")), \
                 patch.object(t.device_manager, "is_busy", side_effect=lambda: busy[0]):
                t.lbl_output_warning.clear()
                t.workflow.refresh()
                self.assertIn("workflow-inputs", t.run_panel._external_start_blocks)
                QtTest.QTest.qWait(20)
                busy[0] = False
                t.device_manager.gate_currents_read.emit({}, "offline quiet completion")
                self.app.processEvents()
                self.assertNotIn("workflow-inputs", t.run_panel._external_start_blocks)
        finally:
            t.lbl_output_warning.setText(old)
            t.workflow.refresh()

    def test_cleanup_remains_active_after_worker_stops(self):
        w = self.window
        p = w.tab_cosweep.run_panel
        p.set_running(False)
        try:
            p.set_status_text("Returning outputs to zero...", "cleanup")
            self.assertIn("2D Map", w.active_run_button.text())
            self.assertIn("Stopping", w.active_run_button.text())
            p.set_status_text("Stopped by user", "done")
            self.assertNotIn("2D Map", w.active_run_button.text())
        finally:
            p.set_status_text("Idle", "idle")

    def test_render_validation_and_series_layout_offline(self):
        from pathlib import Path
        w = self.window
        w.resize(1400, 860)
        root = Path(__file__).resolve().parents[1]
        t = w.tab_gate_scan
        w.tabs.setCurrentWidget(t)
        old = t.sp_ratio.value()
        try:
            t.rad_mode_derived.setChecked(True)
            t.sp_ratio.setValue(0)
            t.workflow.refresh()
            t.workflow.review_inputs()
            self.app.processEvents()
            self.assertTrue(t.workflow.summary.isVisible())
            self.assertGreaterEqual(t.workflow.summary.height(), t.workflow.summary.heightForWidth(t.workflow.summary.width()))
            w.grab().save(str(root / "workflow_validation_preview.png"))
        finally:
            t.sp_ratio.setValue(old)
            t.rad_mode_raw.setChecked(True)
        w.tabs.setCurrentWidget(w.tab_bfield_gate_scan)
        w.tab_bfield_gate_scan.set_status("Idle", "idle")
        w.tab_bfield_gate_scan.log.clear()
        w.tab_bfield_gate_scan.bfield_status.setText("Configure a series before starting")
        self.app.processEvents()
        self.assertIs(w.tab_bfield_gate_scan.control_layout.itemAt(0).widget(), w.tab_bfield_gate_scan.bfield_section)
        w.grab().save(str(root / "workflow_series_preview.png"))
