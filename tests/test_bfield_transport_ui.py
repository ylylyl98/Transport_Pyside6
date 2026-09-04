from __future__ import annotations

import os
import json
import tempfile
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6 import QtCore, QtWidgets

from app.device_manager import DeviceManager
from app.models import Connections, SaveRoot
from app.settings import get_app_settings
from app.signal_chain import SignalChainSnapshot
from app.ui.tabs.bfield_transport_tab import BFieldTransportTab


class BFieldTransportUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def setUp(self):
        get_app_settings().remove(BFieldTransportTab.SETTINGS_PREFIX)
        self.temporary = tempfile.TemporaryDirectory()
        connections = Connections()
        manager = DeviceManager(connections)
        self.tab = BFieldTransportTab(
            SaveRoot(base=self.temporary.name, user="operator", device_id="sample"),
            connections,
            manager,
            get_signal_chain_callable=lambda: SignalChainSnapshot(1000.0, 0.02, 100e-9),
        )

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
        self.assertIn("C01_Neutral_point.csv", self.tab.lbl_filename_preview.toPlainText())
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
