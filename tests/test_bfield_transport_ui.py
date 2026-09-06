from __future__ import annotations

import os
import json
import tempfile
import unittest
import time
from unittest.mock import patch
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6 import QtCore, QtWidgets

from app.device_manager import DeviceManager
from app.models import Connections, SaveRoot
from app.settings import get_app_settings
from app.signal_chain import SignalChainSnapshot
from app.ui.tabs.bfield_transport_tab import BFieldTransportTab
from utils.config import cfg


class BFieldTransportUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def setUp(self):
        get_app_settings().remove(BFieldTransportTab.SETTINGS_PREFIX)
        self.temporary = tempfile.TemporaryDirectory()
        connections = Connections()
        manager = DeviceManager(connections)
        self.manager = manager
        self.tab = BFieldTransportTab(
            SaveRoot(base=self.temporary.name, user="operator", device_id="sample"),
            connections,
            manager,
            get_signal_chain_callable=lambda: SignalChainSnapshot(1000.0, 0.02, 100e-9),
        )

    def test_device_status_panel_tracks_existing_and_future_manager_state(self):
        for name in ("g1", "g2", "g3", "daq"):
            self.manager.sessions[name] = object()
            self.manager._emit_status(name, "ok", "connected")
        self.app.processEvents()

        for name in ("g1", "g2", "g3", "daq"):
            self.assertEqual(self.tab.status_panel.label(name).lbl_state.text(), "OK")

        self.manager.sessions["g2"] = None
        self.manager._emit_status("g2", "idle", "")
        self.app.processEvents()
        self.assertEqual(self.tab.status_panel.label("g2").lbl_state.text(), "Disconnected")

    def test_aps100_badge_and_start_readiness_follow_controller(self):
        class Magnet(QtCore.QObject):
            connected = QtCore.pyqtSignal(object)
            disconnected = QtCore.pyqtSignal()
            snapshot_updated = QtCore.pyqtSignal(object)
            fault = QtCore.pyqtSignal(str)

            def __init__(self):
                super().__init__()
                self.is_connected = True
                self.latest_snapshot = SimpleNamespace(
                    monotonic_s=time.monotonic(),
                    status=SimpleNamespace(quench=False, power_module_failure=False),
                )

        class Controller(QtCore.QObject):
            error = QtCore.pyqtSignal(str)
            state_changed = QtCore.pyqtSignal(str, str)
            finished = QtCore.pyqtSignal()
            stopped = QtCore.pyqtSignal(str)

            def __init__(self):
                super().__init__()
                self.magnet = Magnet()
                self.active = False
                self.thermal_safety = SimpleNamespace(
                    is_armed=True,
                    latest_snapshot=object(),
                    evaluate=lambda _snapshot: SimpleNamespace(magnet_permission=True),
                )

        for name in ("g1", "g2", "g3", "daq"):
            self.manager.sessions[name] = object()
            self.manager._emit_status(name, "ok", "connected")
        controller = Controller()
        self.tab.set_execution_controller(controller)
        self.app.processEvents()

        self.assertEqual(self.tab.status_panel.label("aps100").lbl_state.text(), "OK")
        self.assertTrue(self.tab.btn_start.isEnabled())

        with patch.object(cfg.magnet, "stationary_poll_interval_s", 3.0), patch.object(cfg.magnet, "poll_interval_s", 0.5), patch.object(cfg.magnet, "timeout_ms", 1500):
            # Lake Shore can recheck readiness just before the next idle APS
            # read completes. Ordinary latency must not toggle the button.
            for age in (0.0, 2.5, 3.1, 3.8, 0.0, 3.1):
                controller.magnet.latest_snapshot.reading_age_s = age
                self.tab._sync_aps100_status()
                self.assertTrue(self.tab.btn_start.isEnabled(), age)
                self.assertEqual(self.tab.status_panel.label("aps100").lbl_state.text(), "OK")
            for age in (5.1, float("nan"), -1.0):
                controller.magnet.latest_snapshot.reading_age_s = age
                self.tab.refresh_hardware_readiness()
                self.assertFalse(self.tab.btn_start.isEnabled(), age)
            controller.magnet.latest_snapshot.reading_age_s = 3.1
            controller.active = True
            self.assertEqual(self.tab._aps100_readiness_max_age_s(), 3.0)
            self.tab.refresh_hardware_readiness()
            self.assertFalse(self.tab.btn_start.isEnabled())
            controller.active = False
            controller.magnet.latest_snapshot.reading_age_s = 0.0
            self.tab.refresh_hardware_readiness()
            self.assertTrue(self.tab.btn_start.isEnabled())

        controller.magnet.is_connected = False
        controller.magnet.disconnected.emit()
        self.app.processEvents()
        self.assertEqual(self.tab.status_panel.label("aps100").lbl_state.text(), "Disconnected")
        self.assertFalse(self.tab.btn_start.isEnabled())

    def test_transport_final_mode_is_always_driven_and_persisted(self):
        self.assertEqual(self.tab.cbo_final_mode.currentData(), "driven")
        self.assertFalse(self.tab.cbo_final_mode.isEnabled())
        self.assertEqual(self.tab.cbo_final_mode.findData("persistent"), -1)
        params = self.tab.collect_params()
        self.assertEqual(params.final_mode, "driven")
        self.tab.close()
        self.tab.deleteLater()
        self.app.processEvents()
        connections = Connections()
        self.tab = BFieldTransportTab(
            SaveRoot(base=self.temporary.name, user="operator", device_id="sample"),
            connections, DeviceManager(connections),
        )
        self.assertEqual(self.tab.cbo_final_mode.currentData(), "driven")

    def test_legacy_persistent_default_migrates_to_driven(self):
        settings = get_app_settings()
        settings.beginGroup(self.tab.SETTINGS_PREFIX)
        settings.setValue("final_mode", "persistent")
        settings.setValue("driven_default_applied", True)
        settings.endGroup()
        self.tab._load_settings()
        self.assertEqual(self.tab.collect_params().final_mode, "driven")

    def tearDown(self):
        self.app.processEvents()
        self.tab.close()
        self.tab.deleteLater()
        self.app.processEvents()
        self.temporary.cleanup()
        get_app_settings().remove(BFieldTransportTab.SETTINGS_PREFIX)

    def test_bulk_builder_previews_range_and_broadcasts_scalar_before_add(self):
        self.tab.ed_condition_add_first.setText("0:4:1")
        self.tab.ed_condition_add_second.setText("0.5")

        self.assertIn("Preview (4 conditions)", self.tab.lbl_condition_add_preview.text())
        self.assertIn("Doping 3, E-field 0.5", self.tab.lbl_condition_add_preview.toolTip())
        self.tab.btn_condition_add_preview.click()

        self.assertEqual(self.tab.condition_table.rowCount(), 5)
        self.assertEqual([condition.name for condition in self.tab.conditions], [
            "Con1", "Con2", "Con3", "Con4", "Con5",
        ])
        self.assertEqual([condition.doping for condition in self.tab.conditions[1:]], [0, 1, 2, 3])
        self.assertEqual([condition.efield for condition in self.tab.conditions[1:]], [0.5] * 4)
        self.assertEqual(self.tab.condition_table.currentRow(), 1)

    def test_builder_accepts_bracketed_gate_arrays_and_converts_preview(self):
        self.tab.cbo_condition_add_mode.setCurrentIndex(1)
        self.tab.ed_condition_add_first.setText("[1, 2]")
        self.tab.ed_condition_add_second.setText("[0.5, 1]")

        self.assertEqual(self.tab.lbl_condition_add_first.text(), "Vtg:")
        self.assertEqual(self.tab.lbl_condition_add_second.text(), "Vbg:")
        self.assertIn("Preview (2 conditions)", self.tab.lbl_condition_add_preview.text())
        pending = self.tab._previewed_add_conditions()
        self.assertEqual([(row.vtg, row.vbg) for row in pending], [(1.0, 0.5), (2.0, 1.0)])
        self.assertEqual([(row.doping, row.efield) for row in pending], [(1.5, 0.5), (3.0, 1.0)])

    def test_builder_rejects_unmatched_array_lengths_without_adding(self):
        self.tab.ed_condition_add_first.setText("0, 1")
        self.tab.ed_condition_add_second.setText("0, 1, 2")

        self.assertFalse(self.tab.btn_condition_add_preview.isEnabled())
        self.assertIn("equal lengths", self.tab.lbl_condition_add_preview.text())
        self.assertEqual(self.tab.condition_table.rowCount(), 1)

    def test_quick_add_vds_and_range_preview_match_saved_conditions(self):
        self.tab.ed_condition_add_first.setText("-1:1.5:0.5")
        self.tab.ed_condition_add_second.setText("0")
        self.tab.ed_condition_add_vds.setText("-1")
        preview = self.tab.condition_add_preview_table
        self.assertEqual(preview.rowCount(), 5)
        self.assertEqual([float(preview.item(i, 1).text()) for i in range(5)], [-1, -0.5, 0, 0.5, 1])
        self.assertEqual([float(preview.item(i, 3).text()) for i in range(5)], [-1] * 5)
        self.tab.btn_condition_add_preview.click()
        params = self.tab.collect_params()
        self.assertEqual([(c.doping, c.efield, c.vds) for c in params.conditions[1:]], [(d, 0, -1) for d in [-1, -0.5, 0, 0.5, 1]])
        self.tab._load_settings()
        self.assertEqual([c.vds for c in self.tab.collect_params().conditions[1:]], [-1] * 5)

    def test_quick_add_lists_pair_vds_by_row_and_broadcast_gates(self):
        self.tab.ed_condition_add_first.setText("-1,1,5")
        self.tab.ed_condition_add_second.setText("0")
        self.tab.ed_condition_add_vds.setText("0.1,0.2,0.3")
        rows = self.tab._previewed_add_conditions()
        self.assertEqual([(c.doping, c.vds) for c in rows], [(-1, 0.1), (1, 0.2), (5, 0.3)])
        self.tab.cbo_condition_add_mode.setCurrentIndex(1)
        self.tab.ed_condition_add_first.setText("1")
        self.tab.ed_condition_add_second.setText("0.5")
        rows = self.tab._previewed_add_conditions()
        self.assertEqual([(c.vtg, c.vbg, c.vds) for c in rows], [(1, 0.5, v) for v in [0.1, 0.2, 0.3]])

    def test_quick_add_invalid_series_clear_preview(self):
        for invalid in ("0:1:0", "0:1:-1", "0:101:1", "nan", "np.linspace(-1,1,5)"):
            self.tab.ed_condition_add_vds.setText(invalid)
            self.assertFalse(self.tab.btn_condition_add_preview.isEnabled(), invalid)
            self.assertEqual(self.tab.condition_add_preview_table.rowCount(), 0)
        self.tab.ed_condition_add_first.setText("0,1,2")
        self.tab.ed_condition_add_vds.setText("0,1")
        self.assertFalse(self.tab.btn_condition_add_preview.isEnabled())

    def test_quick_add_checks_connected_voltage_protection(self):
        with patch.object(self.manager, "is_connected", return_value=True), patch.object(self.manager, "applied_gate_voltage_limit", return_value=0.5):
            self.tab.ed_condition_add_vds.setText("1")
            self.assertFalse(self.tab.btn_condition_add_preview.isEnabled())
            self.assertIn("Vds", self.tab.lbl_condition_add_preview.text())

    def test_quick_add_descending_vds_and_lock(self):
        self.tab.ed_condition_add_vds.setText("1:-1.5:-0.5")
        self.assertEqual([c.vds for c in self.tab._previewed_add_conditions()], [1, 0.5, 0, -0.5, -1])
        self.tab.set_sweep_locked(True)
        self.assertFalse(self.tab.ed_condition_add_vds.isEnabled())
        self.tab._refresh_condition_add_preview()
        self.assertFalse(self.tab.btn_condition_add_preview.isEnabled())

    def test_legacy_generated_condition_names_are_shortened_on_load(self):
        self.tab.close()
        self.tab.deleteLater()
        self.app.processEvents()
        settings = get_app_settings()
        settings.setValue(
            f"{BFieldTransportTab.SETTINGS_PREFIX}/conditions",
            json.dumps([{"name": "Condition 12", "doping": 1.0}]),
        )
        connections = Connections()
        self.tab = BFieldTransportTab(
            SaveRoot(base=self.temporary.name, user="operator", device_id="sample"),
            connections,
            DeviceManager(connections),
        )

        self.assertEqual(self.tab.conditions[0].name, "Con12")
        self.assertEqual(self.tab.condition_table.item(0, 1).text(), "Con12")

    def test_table_fits_without_a_horizontal_scrollbar(self):
        table = self.tab.condition_table
        table.setParent(None)
        table.resize(328, 180)
        table.show()
        self.app.processEvents()
        policy = table.horizontalScrollBarPolicy()
        self.assertEqual(policy, QtCore.Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        fixed_widths = {
            column: table.columnWidth(column)
            for column in (0, 1, 6)
        }
        self.assertEqual(fixed_widths, {0: 22, 1: 48, 6: 24})
        self.assertFalse(table.horizontalScrollBar().isVisible())
        last_cell = table.visualRect(table.model().index(0, 8))
        self.assertLess(last_cell.right(), table.viewport().width())
        self.assertGreaterEqual(table.horizontalHeader().height(), 38)
        self.assertEqual(table.horizontalHeaderItem(2).text().replace("\n", ""), "Doping")
        self.assertEqual(table.horizontalHeaderItem(3).text().replace("\n", ""), "E-field")
        self.assertEqual(table.horizontalHeaderItem(5).text().replace("\n", ""), "Source")
        table.close()
        self.app.processEvents()

    def test_compact_source_and_selected_details_keep_full_information_visible(self):
        self.assertEqual(self.tab.condition_table.item(0, 5).text(), "K2400")
        self.tab.condition_table.selectRow(0)
        details = self.tab.lbl_condition_details.text()
        self.assertIn("Con1", details)
        self.assertIn("Keithley 2400", details)
        self.assertIn("Doping 0", details)
        self.assertIn("E-field 0", details)
        self.assertIn("Vtg 0 V", details)
        self.assertIn("Vbg 0 V", details)

        self.tab.condition_table.item(0, 5).setText("DAQ")
        self.assertIn("NI DAQ AO", self.tab.lbl_condition_details.text())
        self.assertEqual(self.tab._condition_from_row(0).vds_source, "NI DAQ AO")

    def test_condition_edits_and_ratio_changes_refresh_selected_details(self):
        self.tab.condition_table.selectRow(0)
        self.tab.condition_table.item(0, 1).setText("Operating point")
        self.tab.condition_table.item(0, 2).setText("2")
        self.tab.btn_condition_update.click()
        self.tab.sp_ratio.setValue(2)

        details = self.tab.lbl_condition_details.text()
        self.assertIn("Operating point", details)
        self.assertIn("Doping 2", details)
        self.assertIn("Vtg 1 V", details)
        self.assertIn("Vbg 0.5 V", details)

    def test_preview_contains_rich_series_and_condition_information(self):
        self.tab.condition_table.item(0, 1).setText("Neutral point")
        self.tab.sp_start.setValue(-1.0)
        self.tab.sp_stop.setValue(2.0)
        self.tab.sp_rate.setValue(0.2)
        self.tab.refresh_output_preview()

        stem = self.tab._planned_output.display_stem
        self.assertIn("B_-1to2T_round_trip_rate_0.2Tpermin_1conditions", stem)
        self.assertIn("freq_1kHz_lia_20mV_preamp_100nA", stem)
        self.assertIn("C01_Neutral_point_Doping_0_Efield_0_r_1xVbg.csv", self.tab.lbl_filename_preview.toPlainText())
        self.assertIn("series_manifest.json", self.tab.lbl_metadata_preview.toPlainText())
        self.assertIn("series_checkpoint.json", self.tab.lbl_metadata_preview.toPlainText())

    def test_frozen_plan_is_collision_checked_against_actual_series_files(self):
        paths = self.tab.freeze_output_plan(self.tab.collect_params())
        self.tab.validate_transport_output_ready(paths)
        with open(paths.condition_csv_paths[0], "w", encoding="utf-8") as handle:
            handle.write("existing")

        with self.assertRaisesRegex(ValueError, "Output file already exists"):
            self.tab.validate_transport_output_ready(paths)
        self.tab.refresh_output_preview()
        self.assertIn("output already exists", self.tab.lbl_output_warning.text())


if __name__ == "__main__":
    unittest.main()
