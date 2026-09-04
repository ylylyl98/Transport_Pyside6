import unittest
from unittest.mock import patch

from PyQt6.QtWidgets import QApplication

from app.device_manager import DeviceManager
from app.engine.gate_scan_field_batch import GateScanFieldBatch
from app.models import Connections, SaveRoot
from app.settings import get_app_settings
from app.ui.tabs.bfield_gate_scan_tab import BFieldGateScanTab


class BFieldGateScanTabTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_conditions_are_independent_snapshots_of_full_gate_editor(self):
        connections = Connections()
        manager = DeviceManager(connections)
        manager.is_connected = lambda _name: True
        tab = BFieldGateScanTab(SaveRoot(base="."), connections, manager)
        tab.cbo_x.setCurrentText("Vbg")
        tab.cbo_y.setCurrentText("Ids_Y")
        tab.sp_raw_vtg_stop.setValue(2.0)
        tab.condition_update.click()
        tab.condition_name.setText("E-field condition")
        tab.rad_mode_derived.setChecked(True)
        tab.rad_sweep_efield.setChecked(True)
        tab.sp_derived_start.setValue(-1.0)
        tab.sp_derived_stop.setValue(1.0)
        tab.condition_add.click()
        # Switching away and back must restore the first row's complete
        # snapshot before Update/capture can overwrite it.
        tab.condition_table.selectRow(0)
        tab.condition_table.selectRow(1)
        tab.condition_table.selectRow(0)
        tab.condition_update.click()
        requests = tab.capture_field_batch_requests()
        self.assertEqual(len(requests), 2)
        self.assertEqual(requests[0][0].mode, "Raw")
        self.assertEqual(requests[0][0].raw_vtg_stop, 2.0)
        self.assertEqual(requests[0][0].plot_x_axis, "Vbg")
        self.assertEqual(requests[0][0].plot_choice, "Ids_Y")
        self.assertEqual(requests[1][0].mode, "Derived")
        self.assertEqual(requests[1][0].derived_axis, "E-field")
        self.assertEqual(requests[1][3], "E-field condition")
        tab.deleteLater()

    def test_ordinary_gate_editor_is_not_given_bfield_controls(self):
        # The dedicated tab owns the B-field controls; this assertion is made
        # by MainWindow wiring and the normal GateScanTab's opt-out default.
        from app.ui.tabs.gate_scan_tab import GateScanTab
        connections = Connections()
        tab = GateScanTab(SaveRoot(base="."), connections, DeviceManager(connections))
        self.assertFalse(tab.include_field_batch)
        self.assertIsNone(tab.exp_bfield_batch)
        tab.deleteLater()

    def _fresh_raw_tab(self):
        get_app_settings().remove("tabs/bfield_gate_scan")
        connections = Connections()
        manager = DeviceManager(connections)
        manager.is_connected = lambda _name: True
        tab = BFieldGateScanTab(SaveRoot(base="."), connections, manager)
        tab.rad_mode_raw.setChecked(True)
        tab.collect_params()
        tab.condition_update.click()
        return tab

    def test_start_capture_uses_saved_snapshot_without_implicit_update(self):
        tab = self._fresh_raw_tab()
        saved_stop = tab._conditions[0].params.raw_vtg_stop
        tab.sp_raw_vtg_stop.setValue(saved_stop + 1.0)
        tab.verified_run_calibration = lambda: (1e7, 100.0, {})

        requests = tab.capture_field_batch_requests()

        self.assertEqual(requests[0][0].raw_vtg_stop, saved_stop)
        self.assertEqual(tab._conditions[0].params.raw_vtg_stop, saved_stop)
        self.assertIn("Unsaved editor changes", tab.bfield_status.text())
        tab.deleteLater()

    def test_switching_conditions_clears_modified_marker_from_previous_row(self):
        tab = self._fresh_raw_tab()
        tab.condition_name.setText("Condition 2")
        tab.condition_add.click()
        tab.condition_table.selectRow(0)
        tab.sp_raw_vtg_stop.setValue(2.0)
        self.assertIn("Modified", tab.condition_table.item(0, 1).text())

        tab.condition_table.selectRow(1)

        self.assertNotIn("Modified", tab.condition_table.item(0, 1).text())
        self.assertEqual(tab.condition_table.item(1, 1).text(), "Condition 2")
        self.assertEqual(tab.condition_edit_status.text(), "Editing saved condition: Condition 2")
        tab.deleteLater()

    def test_start_series_is_blocked_until_modified_condition_is_saved(self):
        tab = self._fresh_raw_tab()

        class FakeOrchestrator:
            def __init__(self):
                self.active = False
                self.starts = []

            def start(self, fields):
                self.starts.append(fields)

        orchestrator = FakeOrchestrator()
        tab._bfield_orchestrator = orchestrator
        tab.set_batch_magnet_context("1000")
        tab.bfield_fields.setText("0")
        tab.sp_raw_vtg_stop.setValue(2.0)

        tab.start_series()

        self.assertEqual(orchestrator.starts, [])
        self.assertIn("Cannot start", tab.bfield_status.text())
        self.assertIn("Update selected", tab.bfield_status.text())
        self.assertEqual(tab._conditions[0].params.raw_vtg_stop, 1.0)

        tab.condition_update.click()
        tab.start_series()

        self.assertEqual(orchestrator.starts, ["0"])
        self.assertNotIn("Cannot start", tab.bfield_status.text())
        tab.deleteLater()

    def test_bfield_preview_updates_for_ranges_and_elides_long_series(self):
        tab = self._fresh_raw_tab()
        tab.bfield_fields.setText("1:-1:-1")
        self.assertIn("Preview (2 fields): 1, 0", tab.bfield_preview.text())
        self.assertIn("1, 0", tab.bfield_preview.toolTip())

        tab.bfield_fields.setText("0:8:0.0008")
        preview = tab.bfield_preview.text()
        self.assertIn("Preview (10000 fields):", preview)
        self.assertIn("…", preview)
        self.assertIn("7.9992", tab.bfield_preview.toolTip())
        tab.deleteLater()

    def test_bfield_preview_reports_parser_errors(self):
        tab = self._fresh_raw_tab()
        for expression, message in (("1:-1:0", "step cannot be zero"), ("0:1:-.25", "direction"), ("0:nan:.1", "finite")):
            tab.bfield_fields.setText(expression)
            self.assertIn("Invalid B-field series", tab.bfield_preview.text())
            self.assertIn(message, tab.bfield_preview.text())
        tab.deleteLater()

    def test_settings_restore_refreshes_preview_after_signal_blocked_load(self):
        settings = get_app_settings()
        settings.setValue("tabs/bfield_gate_scan/bfield_fields", "1:-1:-1")
        try:
            connections = Connections()
            manager = DeviceManager(connections)
            manager.is_connected = lambda _name: True
            tab = BFieldGateScanTab(SaveRoot(base="."), connections, manager)
            self.assertEqual(tab.bfield_fields.text(), "1:-1:-1")
            self.assertIn("Preview (2 fields): 1, 0", tab.bfield_preview.text())
            tab.deleteLater()
        finally:
            settings.remove("tabs/bfield_gate_scan")

    def test_preview_and_start_receive_identical_expanded_order(self):
        tab = self._fresh_raw_tab()

        class FakeOrchestrator:
            active = False

            def __init__(self):
                self.starts = []

            def start(self, fields):
                self.starts.append(fields)

        orchestrator = FakeOrchestrator()
        tab._bfield_orchestrator = orchestrator
        tab.set_batch_magnet_context("1000")
        expression = "1:-0.5:-0.5, 0.25"
        tab.bfield_fields.setText(expression)
        expected = GateScanFieldBatch.parse_fields(expression)
        tab.start_series()
        self.assertEqual(len(orchestrator.starts), 1)
        self.assertEqual(GateScanFieldBatch.parse_fields(orchestrator.starts[0]), expected)
        self.assertIn("1, 0.5, 0, 0.25", tab.bfield_preview.text())
        tab.deleteLater()


if __name__ == "__main__":
    unittest.main()
