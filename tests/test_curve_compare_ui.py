import json
import os
import tempfile
import time
import unittest
from pathlib import Path

if os.environ.get("TRANSPORT_OFFLINE_VERIFICATION") != "1":
    raise unittest.SkipTest("Requires offline wrapper")

from PySide6 import QtCore, QtGui, QtTest, QtWidgets
from app.ui.curve_compare import CurveComparePage


class CurveCompareUITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def setUp(self):
        self.scratch = tempfile.TemporaryDirectory()
        self.root = Path(self.scratch.name)
        self.folder = self.root / "device"
        self.folder.mkdir()
        self.settings = QtCore.QSettings(str(self.root / "settings.ini"), QtCore.QSettings.Format.IniFormat)
        self.page = CurveComparePage(lambda: self.folder, settings=self.settings)
        self.host = QtWidgets.QMainWindow()
        self.host.setCentralWidget(self.page)
        self.host.resize(1200, 700)
        self.host.show()
        self.addCleanup(self.clean_up)

    def clean_up(self):
        self.host.close()
        self.page.shutdown()
        self.page.pool.waitForDone(5000)
        self.app.processEvents()
        self.scratch.cleanup()

    def wait_for(self, condition, timeout=6):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.app.processEvents()
            if condition():
                return
            QtTest.QTest.qWait(20)
        self.fail("Timed out waiting for comparison workspace")

    def scan(self, name, date, kind="vds_sweep"):
        path = self.folder / date / kind / (name + ".csv")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("Vds,Vtg,Ids_DC,Direction\nV,V,A,\n0,1,1,forward\n1,1,2,forward\n1,1,3,backward\n0,1,4,backward\n", encoding="utf-8")
        path.with_name(name + "_metadata.json").write_text(json.dumps({"measurement": kind, "created_at": date + "T12:00:00", "status": "finished", "params": {"vtg_set": 1}}), encoding="utf-8")
        return path

    def test_open_page_lists_all_dates_newest_first_and_separates_types(self):
        today = QtCore.QDate.currentDate().toString("yyyy-MM-dd")
        self.scan("new", today)
        self.scan("old", "2020-01-01")
        self.scan("gate", today, "gate_scan")
        self.scan("map", today, "map_2d")
        self.page.refresh()
        self.wait_for(lambda: len(self.page.model.records) == 4)
        self.assertEqual(self.page.date_combo.currentText(), "All dates")
        self.assertEqual(self.page.proxy.rowCount(), 2)
        newest = self.page.proxy.mapToSource(self.page.proxy.index(0, 0)).row()
        self.assertEqual(self.page.model.records[newest].path.stem, "new")
        self.page.measurement_tabs.setCurrentIndex(1)
        self.assertEqual(self.page.proxy.rowCount(), 1)
        self.assertEqual(self.page.model.records[self.page.proxy.mapToSource(self.page.proxy.index(0, 0)).row()].measurement, "gate_scan")
        self.page.measurement_tabs.setCurrentIndex(0)
        self.page.search_edit.setText("old")
        self.assertEqual(self.page.proxy.rowCount(), 1)

    def derived_map(self):
        path = self.scan("derived_map", "2026-10-02", "map_2d")
        path.write_text("Vtg,Vbg,Doping,Vds,Ids_DC,Ids_X,PassIndex,FastDirection\n"
                        "V,V,V,V,A,A,#,\n0,0,-1,10,1,11,0,forward\n"
                        "0,0,1,10,2,12,0,forward\n0,0,1,20,4,14,1,reverse\n"
                        "0,0,-1,20,3,13,1,reverse\n", encoding="utf-8")
        path.with_name(path.stem + "_metadata.json").write_text(json.dumps({
            "measurement": "map_2d", "created_at": "2026-10-02T12:00:00",
            "params": {"axis_fast": "Doping", "axis_slow": "Vds", "coordinate_mode": "Derived"}
        }), encoding="utf-8")
        self.page.refresh()
        self.wait_for(lambda: len(self.page.model.records) == 1 and not self.page.busy)
        self.page.measurement_tabs.setCurrentIndex(2)
        self.page.model.setData(self.page.model.index(0, 0), QtCore.Qt.CheckState.Checked, QtCore.Qt.ItemDataRole.CheckStateRole)
        self.wait_for(lambda: not self.page.busy)
        return path

    def test_map_axes_follow_metadata_and_manual_axes_survive_signal_change(self):
        self.derived_map()
        self.assertEqual((self.page.x_combo.currentText(), self.page.map_y_combo.currentText()), ("Doping", "Vds"))
        self.assertEqual(self.page._displayed_primary.z.tolist(), [[1, 2], [3, 4]])
        self.page.x_combo.setCurrentText("Vds")
        self.page.map_y_combo.setCurrentText("Doping")
        self.wait_for(lambda: not self.page.busy)
        self.page.signal_combo.setCurrentText("Ids_X")
        self.wait_for(lambda: not self.page.busy)
        self.assertEqual((self.page.x_combo.currentText(), self.page.map_y_combo.currentText()), ("Vds", "Doping"))
        self.assertEqual(self.page._displayed_primary.z.tolist(), [[11, 13], [12, 14]])
        self.page.auto_axes_check.setChecked(True)
        self.wait_for(lambda: not self.page.busy)
        self.assertEqual((self.page.x_combo.currentText(), self.page.map_y_combo.currentText()), ("Doping", "Vds"))

    def test_colormap_updates_existing_mesh_and_survives_signal_and_session(self):
        from app.ui.analysis_session import capture_view
        self.derived_map()
        self.assertIsNotNone(self.page._displayed_primary)
        mesh = self.page._colorbar.mappable
        self.assertEqual(mesh.get_cmap().name, "RdBu_r")
        self.page.map_ax.set_xlim(-.5, .5)
        self.page.colormap_combo.setCurrentText("viridis")
        self.app.processEvents()
        self.assertIs(self.page._colorbar.mappable, mesh)
        self.assertFalse(self.page.busy)
        self.assertEqual(mesh.get_cmap().name, "viridis")
        self.assertEqual(tuple(self.page.map_ax.get_xlim()), (-.5, .5))
        self.assertEqual(capture_view(self.page)["cmap"], "viridis")
        self.page.signal_combo.setCurrentText("Ids_X")
        self.wait_for(lambda: not self.page.busy)
        self.assertEqual(self.page._colorbar.mappable.get_cmap().name, "viridis")
        self.page.save_state()
        reopened = CurveComparePage(lambda: self.folder, settings=self.settings)
        try:
            self.wait_for(lambda: not reopened.busy)
            self.assertEqual(reopened._colorbar.mappable.get_cmap().name, "viridis")
            self.assertEqual(reopened.colormap_combo.currentText(), "viridis")
        finally:
            reopened.shutdown()
            reopened.pool.waitForDone(5000)
            reopened.deleteLater()
            self.app.processEvents()

    def test_figure_heading_exposes_scientific_conditions_and_refreshes_metadata(self):
        path = self.derived_map()
        sidecar = path.with_name(path.stem + "_metadata.json")
        meta = json.loads(sidecar.read_text())
        meta.update(save_root={"device_id": "YZ324"},
                    signal_chain={"preamp_gain_v_per_a": 1e7, "frequency_hz": 17,
                                  "lockin_settings": {"values": {"sine_out_v": .02, "phase_deg": 30}}},
                    experiment_context={"temperature": {"available": True, "values": {"sample_temperature_k": 1.67}}})
        meta["params"]["base_name"] = "ACDCMoTe2E2_DCMoTe2E1"
        sidecar.write_text(json.dumps(meta))
        self.page.refresh()
        self.wait_for(lambda: not self.page.busy)
        heading = " ".join(" ".join(text.get_text().split()) for text in self.page.figure.texts)
        for value in ("YZ324", "ACDCMoTe2E2", "1.67 K", "17 Hz", "20 mV", "φ=30"):
            self.assertIn(value, heading)
        self.assertIn("10 V", self.page.ax.get_title(loc="left"))
        self.assertEqual(self.page._displayed_primary.z.tolist(), [[1, 2], [3, 4]])

    def test_heatmap_auto_color_uses_visible_samples_without_reload(self):
        self.derived_map()
        mesh = self.page._colorbar.mappable
        self.page.map_ax.set_xlim(-1.2, -.8)
        self.page.map_ax.set_ylim(9, 21)
        self.app.processEvents()
        self.assertEqual(mesh.get_clim(), (1, 3))
        self.assertIs(self.page._colorbar.mappable, mesh)
        self.assertFalse(self.page.busy)
        self.page.map_ax.set_ylim(9, 11)
        self.app.processEvents()
        low, high = mesh.get_clim()
        self.assertLess(low, 1)
        self.assertGreater(high, 1)
        self.assertLess(high, 2)
        self.page.map_ax.set_xlim(100, 200)
        self.app.processEvents()
        self.assertEqual(mesh.get_clim(), (low, high))
        self.page.signal_combo.setCurrentText('Ids_X')
        self.wait_for(lambda: not self.page.busy)
        self.assertEqual(self.page._colorbar.mappable.get_clim(), (low, high))
        self.page.colormap_combo.setCurrentText('viridis')
        self.assertTrue(self.page.map_ranges.state()['clim_auto'])

    def test_heatmap_ranges_fixed_auto_and_session_restore(self):
        from app.ui.analysis_session import capture_view
        self.derived_map()
        self.assertTrue(hasattr(self.page, 'map_ranges'), 'Heatmap needs Auto / Fixed range controls')
        ranges = self.page.map_ranges
        ranges.apply({'map_x_auto': False, 'map_y_auto': False, 'clim_auto': False,
                      'map_xlim': [-1.2, -.8], 'map_ylim': [9, 21], 'clim': [0, 5]})
        self.page.map_ax.set_ylim(9, 11)
        self.assertEqual(self.page._colorbar.mappable.get_clim(), (0, 5))
        state = capture_view(self.page)
        self.assertFalse(state['clim_auto'])
        self.assertEqual(self.page.map_ax.get_xlim(), (-1.2, -.8))
        self.assertEqual(self.page._colorbar.mappable.get_clim(), (0, 5))
        ranges.apply({'map_x_auto': True, 'map_y_auto': True, 'clim_auto': True})
        self.page.signal_combo.setCurrentText('Ids_X')
        self.wait_for(lambda: not self.page.busy)
        self.assertEqual(self.page._colorbar.mappable.get_clim(), (11, 14))
        self.page.map_ax.set_xlim(-1.2, -.8)
        state = capture_view(self.page)
        self.assertTrue(state['clim_auto'])
        self.assertEqual(state['clim'], [11, 13])
        self.page.save_state()
        reopened = CurveComparePage(lambda: self.folder, settings=self.settings)
        try:
            self.wait_for(lambda: not reopened.busy)
            self.assertEqual(reopened.map_ax.get_xlim(), (-1.2, -.8))
            self.assertEqual(reopened._colorbar.mappable.get_clim(), (11, 13))
        finally:
            reopened.shutdown()
            reopened.pool.waitForDone(5000)
            reopened.deleteLater()
            self.app.processEvents()

    def test_signal_change_resets_fixed_color_to_visible_new_signal_values(self):
        self.derived_map()
        self.page.map_ranges.apply({'map_x_auto': False, 'map_y_auto': False, 'clim_auto': False,
                                    'map_xlim': [-1.2, -.8], 'map_ylim': [9, 21], 'clim': [1, 4]})
        self.assertEqual(self.page._colorbar.mappable.get_clim(), (1, 4))
        self.page.signal_combo.setCurrentText('Ids_X')
        self.wait_for(lambda: not self.page.busy)
        self.assertEqual(self.page.map_ax.get_xlim(), (-1.2, -.8))
        self.assertEqual(self.page.map_ax.get_ylim(), (9, 21))
        self.assertEqual(self.page._colorbar.mappable.get_clim(), (11, 13))
        self.assertTrue(self.page.map_ranges.state()['clim_auto'])
        self.assertEqual(self.page.png_actions._snapshot()['view']['clim'], [11, 13])
        self.page.signal_combo.setCurrentText('Ids_DC')
        self.wait_for(lambda: not self.page.busy)
        self.assertEqual(self.page._colorbar.mappable.get_clim(), (1, 3))

    def test_pending_signal_switch_keeps_color_owner_when_restoring_tab(self):
        self.derived_map()
        self.page.map_ranges.apply({'map_x_auto': False, 'map_y_auto': False, 'clim_auto': False,
                                    'map_xlim': [-1.2, -.8], 'map_ylim': [9, 21], 'clim': [1, 4]})
        self.page.signal_combo.setCurrentText('Ids_X')
        # Change tabs before the debounced load: controls already name Ids_X,
        # but the displayed map and its fixed color bounds still belong to Ids_DC.
        self.page.measurement_tabs.setCurrentIndex(0)
        self.page.measurement_tabs.setCurrentIndex(2)
        self.wait_for(lambda: not self.page.busy)
        self.assertEqual(self.page._displayed_primary.signal, 'Ids_X')
        self.assertEqual(self.page.map_ax.get_xlim(), (-1.2, -.8))
        self.assertEqual(self.page._colorbar.mappable.get_clim(), (11, 13))
        self.assertTrue(self.page.map_ranges.state()['clim_auto'])

    def test_inline_ranges_validate_bounds_and_accept_scientific_units(self):
        self.derived_map()
        self.assertTrue(hasattr(self.page.map_ranges, 'rows'), 'Range controls must be available directly in Heatmap')
        fields = {widget.accessibleName(): widget for widget in self.page.map_controls.findChildren(QtWidgets.QLineEdit)}
        modes = {widget.accessibleName(): widget for widget in self.page.map_controls.findChildren(QtWidgets.QComboBox)}
        modes['Ids_DC (A) range mode'].setCurrentText('Fixed')
        self.assertFalse(fields['Ids_DC (A) minimum'].isReadOnly())
        fields['Ids_DC (A) minimum'].setText('5e-12')
        fields['Ids_DC (A) maximum'].setText('1e-12')
        QtTest.QTest.keyClick(fields['Ids_DC (A) maximum'], QtCore.Qt.Key.Key_Return)
        self.assertTrue(self.page.map_ranges.error_label.isVisible())
        self.assertEqual(self.page._colorbar.mappable.get_clim(), (1, 4))
        modes['Doping (V) range mode'].setCurrentText('Fixed')
        self.assertTrue(self.page.map_ranges.error_label.isVisible(), 'Changing X must not hide an invalid color range')
        fields['Ids_DC (A) minimum'].setText('1e-12')
        fields['Ids_DC (A) maximum'].setText('5e-12')
        QtTest.QTest.keyClick(fields['Ids_DC (A) maximum'], QtCore.Qt.Key.Key_Return)
        self.assertEqual(self.page._colorbar.mappable.get_clim(), (1e-12, 5e-12))
        self.assertFalse(self.page.map_ranges.state()['clim_auto'])
        self.assertFalse(self.page.map_ranges.error_label.isVisible())
        modes['Ids_DC (A) range mode'].setCurrentText('Auto')
        self.assertTrue(fields['Ids_DC (A) minimum'].isReadOnly())
        self.assertEqual(self.page._colorbar.mappable.get_clim(), (1, 4))
        modes['Ids_DC (A) range mode'].setCurrentText('Fixed')
        fields['Ids_DC (A) minimum'].setText('bad')
        QtTest.QTest.keyClick(fields['Ids_DC (A) minimum'], QtCore.Qt.Key.Key_Return)
        self.assertTrue(self.page.map_ranges.error_label.isVisible())
        self.page.signal_combo.setCurrentText('Ids_X')
        self.wait_for(lambda: not self.page.busy)
        self.assertFalse(self.page.map_ranges.error_label.isVisible(), 'A new signal must clear the discarded color draft error')

    def test_inline_auto_ranges_follow_zoom_and_signal_and_keep_uncommitted_edits(self):
        path = self.derived_map()
        ranges = self.page.map_ranges
        self.assertTrue(hasattr(ranges, 'rows'), 'Auto limits must be visible without opening a dialog')
        minimum, maximum = ranges.bound_edits['clim']
        self.assertTrue(minimum.isVisible())
        self.assertEqual((minimum.text(), maximum.text()), ('1', '4'))
        self.page.map_ax.set_xlim(-1.2, -.8)
        self.assertEqual((minimum.text(), maximum.text()), ('1', '3'))
        ranges.mode_controls['clim'].setCurrentText('Fixed')
        minimum.setFocus()
        minimum.selectAll()
        QtTest.QTest.keyClicks(minimum, '2e-12')
        self.page.map_ax.set_ylim(9, 21)
        self.assertEqual(minimum.text(), '2e-12', 'Viewport updates must preserve a range being typed')
        path.write_text(path.read_text(encoding='utf-8').replace('20,3,13,1,reverse', '20,3.1,13,1,reverse'), encoding='utf-8')
        self.page.refresh()
        self.wait_for(lambda: not self.page.busy)
        self.assertIsNotNone(self.page._displayed_primary, self.page.warning_label.text())
        self.assertEqual(self.page._colorbar.mappable.get_clim(), (1, 3), 'Live redraw must not commit an unfinished range')
        self.assertEqual(minimum.text(), '2e-12')
        self.assertTrue(minimum.hasFocus(), 'Live redraw must retain editor focus')
        QtTest.QTest.keyClick(minimum, QtCore.Qt.Key.Key_Return)
        self.assertEqual(self.page._colorbar.mappable.get_clim(), (2e-12, 3))
        self.page.signal_combo.setCurrentText('Ids_X')
        self.wait_for(lambda: not self.page.busy)
        self.assertEqual(ranges.mode_controls['clim'].currentText(), 'Auto')
        self.assertEqual((minimum.text(), maximum.text()), ('11', '13'))

    def test_typed_cut_reuses_heatmap_and_resolves_in_one_job(self):
        from unittest.mock import patch
        self.derived_map()
        self.assertTrue(self.page.fixed_value_combo.isEditable())
        mesh, bar = self.page._colorbar.mappable, self.page._colorbar
        self.page.map_ax.set_xlim(-.5, .5)
        mesh.set_clim(1.5, 3.5)
        with patch.object(self.page, "_submit", wraps=self.page._submit) as submit:
            self.page.fixed_value_combo.setEditText("18")
            QtTest.QTest.keyClick(self.page.fixed_value_combo.lineEdit(), QtCore.Qt.Key.Key_Return)
            self.wait_for(lambda: not self.page.busy)
            self.assertEqual(sum(call.args[0] == "plot" for call in submit.call_args_list), 1)
        self.assertEqual(self.page.fixed_value_combo.currentData(), 20)
        self.assertEqual(self.page.ax.lines[0].get_ydata().tolist(), [3, 4])
        self.assertIn("18", self.page.cut_value_label.text())
        self.assertIn("20", self.page.cut_value_label.text())
        self.assertIs(self.page._colorbar, bar)
        self.assertIs(self.page._colorbar.mappable, mesh)
        self.assertEqual(tuple(self.page.map_ax.get_xlim()), (-.5, .5))
        self.assertEqual(mesh.get_clim(), (1.5, 3.5))
        self.assertFalse(self.page.map_ax.lines)
        self.assertEqual(self.page.png_actions._snapshot()["view"]["fixed_value"], 20)

    def test_fixed_x_control_extracts_vertical_cut_and_keeps_mesh(self):
        self.derived_map()
        mesh = self.page._colorbar.mappable
        self.page.fixed_axis_combo.setCurrentIndex(self.page.fixed_axis_combo.findData("Doping"))
        self.wait_for(lambda: not self.page.busy)
        self.assertIn("Fix X", self.page.fixed_axis_combo.currentText())
        self.page.fixed_value_combo.setEditText("0.9")
        QtTest.QTest.keyClick(self.page.fixed_value_combo.lineEdit(), QtCore.Qt.Key.Key_Return)
        self.wait_for(lambda: not self.page.busy)
        self.assertEqual(self.page.ax.lines[0].get_xdata().tolist(), [10, 20])
        self.assertEqual(self.page.ax.lines[0].get_ydata().tolist(), [2, 4])
        self.assertEqual(self.page.png_actions._snapshot()["view"]["fixed_axis"], "Doping")
        self.assertFalse(self.page.map_ax.lines)
        self.assertIs(self.page._colorbar.mappable, mesh)

    def test_uncommitted_cut_draft_survives_signal_result(self):
        import threading
        from unittest.mock import patch
        from app.curve_map import map_comparison
        self.derived_map()
        started, release = threading.Event(), threading.Event()
        def slow_map(*args):
            started.set()
            release.wait(3)
            return map_comparison(*args)
        with patch("app.ui.curve_compare.map_comparison", slow_map):
            self.page.signal_combo.setCurrentText("Ids_X")
            self.wait_for(started.is_set)
            editor = self.page.fixed_value_combo.lineEdit()
            editor.setFocus()
            editor.selectAll()
            QtTest.QTest.keyClicks(editor, "18")
            release.set()
            self.wait_for(lambda: not self.page.busy)
        self.assertEqual(editor.text(), "18")
        self.assertFalse(self.page.png_actions._is_ready())
        QtTest.QTest.keyClick(editor, QtCore.Qt.Key.Key_Return)
        self.wait_for(lambda: not self.page.busy)
        self.assertEqual(self.page.fixed_value_combo.currentData(), 20)
        self.assertTrue(self.page.png_actions._is_ready())

    def test_invalid_cut_draft_does_not_disable_other_curve_view_exports(self):
        self.derived_map()
        self.page.fixed_value_combo.setEditText("bad")
        self.page.apply_cut_button.click()
        self.assertFalse(self.page.png_actions._is_ready())
        self.scan("curve", "2026-10-02")
        self.page.refresh()
        self.wait_for(lambda: not self.page.busy)
        self.page.measurement_tabs.setCurrentIndex(0)
        row = next(i for i, record in enumerate(self.page.model.records) if record.measurement == "vds_sweep")
        self.page.model.setData(self.page.model.index(row, 0), QtCore.Qt.CheckState.Checked, QtCore.Qt.ItemDataRole.CheckStateRole)
        self.wait_for(lambda: not self.page.busy)
        self.assertEqual(len(self.page.ax.lines), 2)
        self.assertTrue(self.page.png_actions._is_ready())
    def test_file_selection_has_full_height_left_sidebar(self):
        today = QtCore.QDate.currentDate().toString("yyyy-MM-dd")
        self.scan("long_measurement_filename_for_comparing_gate_conditions", today)
        self.page.refresh()
        self.wait_for(lambda: len(self.page.model.records) == 1)
        self.host.resize(1280, 850)
        self.app.processEvents()
        table_pos = self.page.table.mapTo(self.page, QtCore.QPoint(0, 0))
        canvas_pos = self.page.canvas.mapTo(self.page, QtCore.QPoint(0, 0))
        self.assertLess(table_pos.x(), canvas_pos.x())
        self.assertGreater(self.page.table.height(), 450)
        self.assertGreater(self.page.table.width(), 300)
        self.assertIn("long_measurement_filename", self.page.model.data(self.page.model.index(0, 0)))

    def test_expanding_file_list_keeps_curves_and_selection_when_returning(self):
        self.scan("scan", "2020-01-01")
        self.page.refresh()
        self.wait_for(lambda: len(self.page.model.records) == 1)
        self.page.model.setData(self.page.model.index(0, 0), QtCore.Qt.CheckState.Checked, QtCore.Qt.ItemDataRole.CheckStateRole)
        self.wait_for(lambda: len(self.page.ax.lines) == 2)
        before = self.page.table.width()
        self.page.expand_files_button.click()
        self.app.processEvents()
        self.assertTrue(self.page.chart_panel.isHidden())
        self.assertGreater(self.page.table.width(), before + 300)
        self.page.expand_files_button.click()
        self.app.processEvents()
        self.assertFalse(self.page.chart_panel.isHidden())
        self.assertEqual(len(self.page.ax.lines), 2)
        self.assertEqual(len(self.page.model.checked), 1)

    def test_long_filename_wraps_without_loss_and_height_follows_sidebar_width(self):
        name = "YZ324_1.67K0T_REFlmgon_ACDCMoTe2E2_DCMoTe2E1_AmpMoS2E1_Vds0.9to1.05V_Vtg0V_Vbg0V_" + "W" * 70
        path = self.scan(name, "2026-10-02")
        self.page.refresh()
        self.wait_for(lambda: not self.page.busy)
        index = self.page.proxy.index(0, 0)
        self.page.splitter.setSizes([320, 850])
        self.app.processEvents()
        option = QtWidgets.QStyleOptionViewItem()
        self.page.table.initViewItemOption(option)
        option.rect = self.page.table.visualRect(index)
        self.page.table.itemDelegate().initStyleOption(option, index)
        lines = option.text.splitlines()
        self.assertGreater(len(lines), 4, "The filename needs extra physical lines")
        self.assertEqual("".join(lines[1:-2]), path.name)
        self.assertEqual(option.textElideMode, QtCore.Qt.TextElideMode.ElideNone)
        metrics = QtGui.QFontMetricsF(option.font)
        self.assertLessEqual(max(metrics.horizontalAdvance(line) for line in lines), self.page.table.viewport().width() - 30)
        small_height = self.page.table.visualRect(index).height()
        self.assertGreaterEqual(small_height, (len(lines) - 1) * metrics.lineSpacing())
        self.assertLessEqual(self.page.table.visualRect(index).width(), self.page.table.viewport().width())
        self.page.expand_files_button.click()
        self.app.processEvents()
        self.assertLess(self.page.table.visualRect(index).height(), small_height)
        self.assertEqual(self.page.model.data(self.page.model.index(0, 5)), path.name)

    def test_live_file_refresh_preserves_current_index_and_scroll_position(self):
        paths = [self.scan(f"scan_{i:03}", "2026-10-02") for i in range(40)]
        self.page.refresh()
        # Indexing the bulk fixture can exceed the ordinary single-file budget.
        self.wait_for(lambda: not self.page.busy, timeout=20)
        source_row = next(i for i, record in enumerate(self.page.model.records) if record.path == paths[0])
        self.page.model.setData(self.page.model.index(source_row, 0), QtCore.Qt.CheckState.Checked, QtCore.Qt.ItemDataRole.CheckStateRole)
        self.wait_for(lambda: not self.page.busy)
        index = self.page.proxy.index(20, 0)
        self.page.table.setCurrentIndex(index)
        self.page.table.scrollTo(index)
        self.app.processEvents()
        persistent = QtCore.QPersistentModelIndex(index)
        scroll = self.page.table.verticalScrollBar().value()
        with paths[0].open("a", encoding="utf-8") as stream:
            stream.write("2,1,5,forward\n")
        self.page.refresh()
        self.wait_for(lambda: not self.page.busy)
        self.assertTrue(persistent.isValid(), "A growing CSV must not reset the whole list")
        self.assertEqual(self.page.table.currentIndex(), persistent)
        self.assertEqual(self.page.table.verticalScrollBar().value(), scroll)
        self.assertTrue(any(list(line.get_xdata())[-1] == 2 and list(line.get_ydata())[-1] == 5 for line in self.page.ax.lines),
                        "Keeping row geometry must not suppress live curve updates")

    def test_unchanged_refresh_does_not_flush_settings_from_gui_thread(self):
        from unittest.mock import patch
        self.scan("scan", "2026-10-02")
        self.page.refresh()
        self.wait_for(lambda: not self.page.busy)
        self.page.save_state()
        with patch.object(self.settings, "sync", wraps=self.settings.sync) as flush:
            self.page.refresh()
            self.wait_for(lambda: not self.page.busy)
            self.assertEqual(flush.call_count, 0, "Background polling must not rewrite UI preferences")

    def test_wrapped_rows_keep_keyboard_and_checkbox_interaction(self):
        self.scan("设备_🧪_" + "long_filename_component_" * 5, "2026-10-02")
        self.page.refresh()
        self.wait_for(lambda: not self.page.busy)
        # This checks Qt's Unicode row text, not Matplotlib's font coverage.
        self.page.legend_check.setChecked(False)
        index = self.page.proxy.index(0, 0)
        self.page.table.setCurrentIndex(index)
        self.page.table.setFocus()
        QtTest.QTest.keyClick(self.page.table, QtCore.Qt.Key.Key_Space)
        self.assertEqual(len(self.page.model.checked), 1)
        self.wait_for(lambda: not self.page.busy)
        option = QtWidgets.QStyleOptionViewItem()
        self.page.table.initViewItemOption(option)
        option.rect = self.page.table.visualRect(index)
        self.page.table.itemDelegate().initStyleOption(option, index)
        self.assertIn("🧪", option.text)
        check_rect = self.page.table.style().subElementRect(QtWidgets.QStyle.SubElement.SE_ItemViewItemCheckIndicator, option, self.page.table)
        QtTest.QTest.mouseClick(self.page.table.viewport(), QtCore.Qt.MouseButton.LeftButton, pos=check_rect.center())
        self.assertEqual(len(self.page.model.checked), 0)

    def test_live_status_changes_update_search_without_losing_source_index(self):
        path = self.scan("scan", "2026-10-02")
        self.page.refresh()
        self.wait_for(lambda: not self.page.busy)
        source_index = QtCore.QPersistentModelIndex(self.page.model.index(0, 0))
        self.page.search_edit.setText("running")
        self.assertEqual(self.page.proxy.rowCount(), 0)
        sidecar = path.with_name(path.stem + "_metadata.json")
        metadata = json.loads(sidecar.read_text())
        metadata["status"] = "running"
        sidecar.write_text(json.dumps(metadata), encoding="utf-8")
        self.page.refresh()
        self.wait_for(lambda: not self.page.busy)
        self.assertTrue(source_index.isValid())
        self.assertEqual(self.page.proxy.rowCount(), 1)
        self.assertIn("Status: running", self.page.proxy.index(0, 0).data())

    def test_checking_rows_overlays_directions_and_refresh_preserves_checks(self):
        today = QtCore.QDate.currentDate().toString("yyyy-MM-dd")
        first = self.scan("first", today)
        self.scan("second", today)
        self.page.refresh()
        self.wait_for(lambda: len(self.page.model.records) == 2)
        for row in range(2):
            self.page.model.setData(self.page.model.index(row, 0), QtCore.Qt.CheckState.Checked, QtCore.Qt.ItemDataRole.CheckStateRole)
        self.wait_for(lambda: len(self.page.ax.lines) == 4)
        self.assertEqual([list(line.get_xdata()) for line in self.page.ax.lines], [[0, 1], [1, 0], [0, 1], [1, 0]])
        self.page.direction_combo.setCurrentText("backward")
        self.wait_for(lambda: len(self.page.ax.lines) == 2)
        self.page.refresh()
        self.wait_for(lambda: not self.page.busy)
        self.assertIn(str(first), self.page.model.checked)

    def test_reopened_viewer_restores_checked_curves_and_display_state(self):
        today = QtCore.QDate.currentDate().toString("yyyy-MM-dd")
        path = self.scan("scan", today)
        self.page.refresh()
        self.wait_for(lambda: len(self.page.model.records) == 1)
        self.page.model.setData(self.page.model.index(0, 0), QtCore.Qt.CheckState.Checked, QtCore.Qt.ItemDataRole.CheckStateRole)
        self.wait_for(lambda: len(self.page.ax.lines) == 2)
        self.page.save_state()
        reopened = CurveComparePage(lambda: self.folder, settings=self.settings)
        try:
            self.wait_for(lambda: len(reopened.ax.lines) == 2)
            self.assertIn(str(path), reopened.model.checked)
            self.assertEqual(reopened.date_combo.currentText(), "All dates")
        finally:
            reopened.shutdown()
            reopened.pool.waitForDone(5000)
            reopened.deleteLater()
            self.app.processEvents()

    def test_folder_change_isolated_and_display_state_restored(self):
        self.scan("old", "2020-01-01")
        self.page.date_combo.setCurrentText("All dates")
        self.page.refresh()
        self.wait_for(lambda: len(self.page.model.records) == 1)
        self.page.model.setData(self.page.model.index(0, 0), QtCore.Qt.CheckState.Checked, QtCore.Qt.ItemDataRole.CheckStateRole)
        self.wait_for(lambda: len(self.page.ax.lines) == 2)
        original = self.folder
        self.folder = self.root / "other-device"
        self.folder.mkdir()
        self.page.follow_current_folder()
        self.wait_for(lambda: not self.page.busy)
        self.assertEqual(self.page.proxy.rowCount(), 0)
        self.assertEqual(len(self.page.ax.lines), 0)
        self.folder = original
        self.page.follow_current_folder()
        self.wait_for(lambda: len(self.page.ax.lines) == 2)
        self.assertEqual(self.page.date_combo.currentText(), "All dates")

    def test_new_completed_scan_appears_after_refresh(self):
        self.page.refresh()
        self.wait_for(lambda: not self.page.busy)
        self.scan("new", QtCore.QDate.currentDate().toString("yyyy-MM-dd"))
        self.page.refresh()
        self.wait_for(lambda: self.page.proxy.rowCount() == 1)
        self.assertEqual(self.page.model.records[0].status, "finished")

    def test_refresh_during_curve_read_does_not_discard_requested_plot(self):
        import threading
        from unittest.mock import patch
        from app.curve_history import comparison_traces as real_compare
        started, release = threading.Event(), threading.Event()
        self.scan("scan", "2020-01-01")
        self.page.refresh()
        self.wait_for(lambda: len(self.page.model.records) == 1)
        def slow_compare(*args):
            started.set()
            release.wait(3)
            return real_compare(*args)
        with patch("app.ui.curve_compare.comparison_traces", slow_compare):
            self.page.model.setData(self.page.model.index(0, 0), QtCore.Qt.CheckState.Checked, QtCore.Qt.ItemDataRole.CheckStateRole)
            self.wait_for(started.is_set)
            self.page.refresh()
            self.wait_for(lambda: not self.page._scan_active)
            release.set()
            self.wait_for(lambda: not self.page.busy)
        self.assertEqual(len(self.page.ax.lines), 2)

    def test_map_view_then_fixed_coordinate_cut_and_type_selection_isolation(self):
        today = QtCore.QDate.currentDate().toString("yyyy-MM-dd")
        path = self.scan("map", today, "map_2d")
        path.write_text("Vtg,Vbg,Doping,Ids_DC,PassIndex,FastDirection\nV,V,V,A,#,\n0,10,10,1,0,forward\n1,10,10,2,0,forward\n1,20,20,4,1,reverse\n0,20,20,3,1,reverse\n", encoding="utf-8")
        path.with_name(path.stem + "_metadata.json").write_text(json.dumps({"measurement": "map_2d", "created_at": today + "T12:00:00", "params": {"axis_fast": "Vtg", "axis_slow": "Vbg"}}), encoding="utf-8")
        self.page.refresh()
        self.wait_for(lambda: len(self.page.model.records) == 1)
        self.page.measurement_tabs.setCurrentIndex(2)
        self.page.model.setData(self.page.model.index(0, 0), QtCore.Qt.CheckState.Checked, QtCore.Qt.ItemDataRole.CheckStateRole)
        self.wait_for(lambda: self.page.map_ax is not None and len(self.page.map_ax.collections) == 1 and len(self.page.ax.lines) == 1)
        self.assertEqual(list(self.page.ax.lines[0].get_ydata()), [1, 2])
        self.page.fixed_value_combo.setCurrentIndex(self.page.fixed_value_combo.findData(20.))
        self.wait_for(lambda: len(self.page.ax.lines) == 1 and list(self.page.ax.lines[0].get_ydata()) == [3, 4])
        self.page.measurement_tabs.setCurrentIndex(0)
        self.wait_for(lambda: not self.page.busy)
        self.assertEqual(self.page.proxy.rowCount(), 0)
        self.assertEqual(len(self.page.ax.lines), 0)
        self.page.measurement_tabs.setCurrentIndex(2)
        self.wait_for(lambda: len(self.page.ax.lines) == 1)
        self.assertEqual(list(self.page.ax.lines[0].get_ydata()), [3, 4])
        self.page.fixed_axis_combo.setCurrentIndex(self.page.fixed_axis_combo.findData("Vtg"))
        self.wait_for(lambda: len(self.page.ax.lines) == 1 and list(self.page.ax.lines[0].get_ydata()) == [1, 3])
        self.assertEqual(list(self.page.ax.lines[0].get_xdata()), [10, 20])
        self.page.fixed_axis_combo.setCurrentIndex(self.page.fixed_axis_combo.findData("Vbg"))
        self.wait_for(lambda: len(self.page.ax.lines) == 1 and list(self.page.ax.lines[0].get_ydata()) == [1, 2])
        self.page.map_y_combo.setCurrentText("Doping")
        self.wait_for(lambda: self.page.fixed_axis_combo.currentData() == "Doping" and len(self.page.ax.lines) == 1)
        self.assertEqual(list(self.page.ax.lines[0].get_ydata()), [1, 2])

    def test_slow_curve_reads_are_not_starved_by_unchanged_refreshes(self):
        from unittest.mock import patch
        from app.curve_history import comparison_traces as real_compare
        self.scan("scan", "2020-01-01")
        self.page.refresh()
        self.wait_for(lambda: len(self.page.model.records) == 1)
        def slow_compare(*args):
            time.sleep(.45)
            return real_compare(*args)
        self.page.refresh_timer.setInterval(150)
        with patch("app.ui.curve_compare.comparison_traces", slow_compare):
            self.page.model.setData(self.page.model.index(0, 0), QtCore.Qt.CheckState.Checked, QtCore.Qt.ItemDataRole.CheckStateRole)
            self.wait_for(lambda: len(self.page.ax.lines) == 2)
        self.page.refresh_timer.stop()

    def test_export_uses_displayed_snapshot_and_restores_saved_zoom(self):
        path = self.scan("export", "2026-10-01")
        self.page.refresh()
        self.wait_for(lambda: len(self.page.model.records) == 1)
        self.page.model.setData(self.page.model.index(0, 0), QtCore.Qt.CheckState.Checked, QtCore.Qt.ItemDataRole.CheckStateRole)
        self.wait_for(lambda: len(self.page.ax.lines) == 2 and not self.page.busy)
        self.page.ax.set_xlim(.2, .8)
        self.page.ax.set_ylim(1, 3)
        original = path.read_bytes()
        # Export must use arrays already displayed, not changed CSV values.
        path.write_text("Vds,Ids_DC\nV,A\n0,999\n1,999\n")
        target = self.root / "analysis.png"
        self.page.export_to(target)
        self.wait_for(lambda: self.page.last_export_result is not None)
        self.assertNotIn("error", self.page.last_export_result)
        self.assertTrue(target.exists())
        data = json.loads((self.root / "analysis_view.json").read_text())
        self.assertEqual(data["view"]["xlim"], [.2, .8])
        self.assertEqual(data["view"]["ylim"], [1., 3.])
        path.write_bytes(original)
        self.page.ax.set_xlim(-1, 2)
        self.page.restore_export_view(self.root / "analysis_view.json")
        self.wait_for(lambda: len(self.page.ax.lines) == 2 and tuple(self.page.ax.get_xlim()) == (.2, .8))
        self.assertEqual(tuple(self.page.ax.get_ylim()), (1., 3.))
        self.page.ax.set_xlim(-1, 2)
        self.page.restore_export_view(self.root / "analysis_view.json")
        self.wait_for(lambda: tuple(self.page.ax.get_xlim()) == (.2, .8))

    def test_export_is_disabled_while_selection_is_changing(self):
        self.scan("changing", "2026-10-01")
        self.page.refresh()
        self.wait_for(lambda: len(self.page.model.records) == 1)
        self.page.model.setData(self.page.model.index(0, 0), QtCore.Qt.CheckState.Checked, QtCore.Qt.ItemDataRole.CheckStateRole)
        self.assertFalse(self.page.export_button.isEnabled())
        with self.assertRaises(ValueError):
            self.page.export_to(self.root / "stale.png")
        self.wait_for(lambda: len(self.page.ax.lines) == 2 and not self.page.busy)
        self.assertTrue(self.page.export_button.isEnabled())

    def test_reload_session_restores_custom_folder_filters_selection_and_zoom(self):
        path = self.scan("session", "2026-10-01")
        self.page.refresh()
        self.wait_for(lambda: len(self.page.model.records) == 1 and not self.page.busy and self.page.date_combo.findText("2026-10-01") >= 0)
        self.page._custom_folder = self.folder
        self.page.follow_button.setEnabled(True)
        self.page.model.setData(self.page.model.index(0, 0), QtCore.Qt.CheckState.Checked, QtCore.Qt.ItemDataRole.CheckStateRole)
        self.page.direction_combo.setCurrentText("backward")
        self.page.search_edit.setText("session")
        self.page.date_combo.setCurrentText("2026-10-01")
        self.page.legend_check.setChecked(False)
        self.wait_for(lambda: not self.page.busy and len(self.page.ax.lines) == 1)
        self.page.ax.set_xlim(.2, .8)
        self.page.ax.set_ylim(2.5, 4.5)
        state = json.loads(json.dumps(self.page.capture_session()))
        self.assertEqual(state["date"], "2026-10-01")
        other_folder = self.root / "other"
        other_folder.mkdir()
        fresh = CurveComparePage(lambda: other_folder, settings=self.settings)
        self.addCleanup(lambda: (fresh.shutdown(), fresh.pool.waitForDone(5000), fresh.close()))
        fresh.restore_session(state)
        self.wait_for(lambda: not fresh.busy and len(fresh.ax.lines) == 1)
        self.assertEqual(fresh.folder, self.folder)
        self.assertEqual(fresh._custom_folder, self.folder)
        self.assertEqual(fresh.model.checked, {str(path)})
        self.assertEqual(fresh.search_edit.text(), "session")
        self.assertEqual(fresh.date_combo.currentText(), "2026-10-01")
        self.assertEqual(list(fresh.ax.lines[0].get_xdata()), [1, 0])
        self.assertFalse(fresh.legend_check.isChecked())
        self.assertEqual(tuple(fresh.ax.get_xlim()), (.2, .8))
        self.assertEqual(tuple(fresh.ax.get_ylim()), (2.5, 4.5))

    def test_reload_session_restores_primary_map_cut_colors_and_expanded_list(self):
        first = self.scan("primary_session", "2026-10-01", "map_2d")
        other = self.scan("other_session", "2026-10-01", "map_2d")
        for path in (first, other):
            path.write_text("Vtg,Vbg,Ids_DC,PassIndex,FastDirection\nV,V,A,#,\n0,10,1e-9,0,forward\n1,10,2e-9,0,forward\n0,20,3e-9,1,reverse\n1,20,4e-9,1,reverse\n")
        self.page.refresh()
        self.wait_for(lambda: len(self.page.model.records) == 2)
        self.page.measurement_tabs.setCurrentIndex(2)
        for row in range(2):
            self.page.model.setData(self.page.model.index(row, 0), QtCore.Qt.CheckState.Checked, QtCore.Qt.ItemDataRole.CheckStateRole)
        self.wait_for(lambda: not self.page.busy and self.page._displayed_primary is not None)
        self.page.map_file_combo.setCurrentIndex(self.page.map_file_combo.findData(str(first)))
        self.page.fixed_value_combo.setCurrentIndex(self.page.fixed_value_combo.findData(20.))
        self.wait_for(lambda: not self.page.busy)
        self.page.map_ax.set_xlim(.1, .9)
        self.page.ax.set_ylim(2e-9, 5e-9)
        self.page._colorbar.mappable.set_clim(1.5e-9, 3.5e-9)
        self.page._colorbar.mappable.set_cmap("plasma")
        self.page.expand_files_button.setChecked(True)
        state = self.page.capture_session()
        fresh = CurveComparePage(lambda: self.folder, settings=self.settings)
        self.addCleanup(lambda: (fresh.shutdown(), fresh.pool.waitForDone(5000), fresh.close()))
        fresh.restore_session(state)
        self.wait_for(lambda: not fresh.busy and fresh._displayed_primary is not None)
        self.assertEqual(fresh._displayed_primary.path, first)
        self.assertEqual(fresh.fixed_value_combo.currentData(), 20)
        self.assertEqual(tuple(fresh.map_ax.get_xlim()), (.1, .9))
        self.assertEqual(tuple(fresh.ax.get_ylim()), (2e-9, 5e-9))
        self.assertEqual(fresh._colorbar.mappable.get_clim(), (1.5e-9, 3.5e-9))
        self.assertEqual(fresh._colorbar.mappable.get_cmap().name, "plasma")
        self.assertEqual(fresh.colormap_combo.currentText(), "plasma")
        self.assertTrue(fresh.expand_files_button.isChecked())
        self.assertTrue(fresh.chart_panel.isHidden())

    def test_reload_session_follow_mode_uses_latest_transport_folder(self):
        self.scan("old_device", "2026-10-01")
        self.page.refresh()
        self.wait_for(lambda: len(self.page.model.records) == 1)
        self.page.model.setData(self.page.model.index(0, 0), QtCore.Qt.CheckState.Checked, QtCore.Qt.ItemDataRole.CheckStateRole)
        self.wait_for(lambda: not self.page.busy and len(self.page.ax.lines) == 2)
        self.page.expand_files_button.setChecked(True)
        state = self.page.capture_session()
        new_folder = self.root / "new_device"
        new_folder.mkdir()
        fresh = CurveComparePage(lambda: new_folder, settings=self.settings)
        self.addCleanup(lambda: (fresh.shutdown(), fresh.pool.waitForDone(5000), fresh.close()))
        fresh.restore_session(state)
        self.wait_for(lambda: not fresh.busy)
        self.assertEqual(fresh.folder, new_folder)
        self.assertIsNone(fresh._custom_folder)
        self.assertFalse(fresh.model.checked)
        self.assertFalse(fresh.ax.lines)
        self.assertTrue(fresh.expand_files_button.isChecked())
        self.assertTrue(fresh.chart_panel.isHidden())

    def test_map_export_keeps_primary_coordinate_and_can_restore_it(self):
        from PIL import Image
        path = self.scan("map_export", "2026-10-01", "map_2d")
        path.write_text("Vtg,Vbg,Vds,Ids_DC,PassIndex,FastDirection\nV,V,V,A,#,\n0,10,.05,1e-9,0,forward\n1,10,.05,2e-9,0,forward\n1,20,.05,4e-9,1,reverse\n0,20,.05,3e-9,1,reverse\n")
        meta = path.with_name(path.stem + "_metadata.json")
        payload = json.loads(meta.read_text())
        payload["params"].update(vds_set=0, vds_start=.05, vds_stop=.05)
        meta.write_text(json.dumps(payload))
        other = self.scan("other_map_export", "2026-10-01", "map_2d")
        other.write_bytes(path.read_bytes())
        self.page.refresh()
        self.wait_for(lambda: len(self.page.model.records) == 2)
        self.page.measurement_tabs.setCurrentIndex(2)
        for row in range(2):
            self.page.model.setData(self.page.model.index(row, 0), QtCore.Qt.CheckState.Checked, QtCore.Qt.ItemDataRole.CheckStateRole)
        self.wait_for(lambda: self.page._displayed_primary is not None and not self.page.busy)
        self.page.map_file_combo.setCurrentIndex(self.page.map_file_combo.findData(str(path)))
        self.wait_for(lambda: not self.page.busy)
        self.page.fixed_value_combo.setCurrentIndex(self.page.fixed_value_combo.findData(20.))
        self.wait_for(lambda: not self.page.busy and list(self.page.ax.lines[0].get_ydata()) == [3e-9, 4e-9])
        target = self.root / "map_pair.png"
        self.page.export_to(target)
        self.wait_for(lambda: self.page.last_export_result is not None)
        self.assertNotIn("error", self.page.last_export_result)
        outputs = self.page.last_export_result["outputs"]
        self.assertEqual({Path(output).name for output in outputs}, {"map_pair_heatmap.png", "map_pair_cut.png"})
        for output in outputs:
            with Image.open(output) as image:
                self.assertEqual(image.size, (1200, 900))
                self.assertIn("Vds=50 mV", image.info["Description"])
        view = self.root / "map_pair_view.json"
        saved = json.loads(view.read_text())
        self.assertEqual(saved["view"]["fixed_value"], 20)
        self.assertEqual(saved["view"]["primary_path"], str(path))
        self.page.map_file_combo.setCurrentIndex(self.page.map_file_combo.findData(str(other)))
        self.page.fixed_value_combo.setCurrentIndex(self.page.fixed_value_combo.findData(10.))
        self.wait_for(lambda: not self.page.busy)
        self.page.restore_export_view(view)
        self.wait_for(lambda: not self.page.busy and self.page.fixed_value_combo.currentData() == 20)
        self.assertEqual(list(self.page.ax.lines[0].get_ydata()), [3e-9, 4e-9])
        self.assertEqual(self.page._displayed_primary.path, path)

    def test_toolbar_map_save_exports_both_images(self):
        from unittest.mock import patch
        self.derived_map()
        source = self.page._displayed_primary.path
        with patch("PySide6.QtWidgets.QFileDialog.getSaveFileName", side_effect=AssertionError("Automatic export opened a dialog")):
            self.page.toolbar.save_figure()
        self.wait_for(lambda: self.page.last_export_result is not None)
        self.assertNotIn("error", self.page.last_export_result)
        result = self.page.last_export_result
        self.assertEqual(len(result["outputs"]), 2)
        self.assertEqual(len(result["csv_outputs"]), 2)
        self.assertEqual({Path(output).parent for output in result["outputs"] + result["csv_outputs"]}, {source.parent / source.stem})

    def test_export_menu_can_save_only_heatmap(self):
        from unittest.mock import patch
        self.derived_map()
        menu = self.page.export_button.menu()
        self.assertIsNotNone(menu)
        actions = {action.text(): action for action in menu.actions() if not action.isSeparator()}
        self.assertEqual(set(actions), {"Heatmap only", "Cut only", "Heatmap + cut", "Save as…"})
        with patch("PySide6.QtWidgets.QFileDialog.getSaveFileName", side_effect=AssertionError("Automatic export opened a dialog")):
            actions["Heatmap only"].trigger()
        self.wait_for(lambda: self.page.last_export_result is not None)
        self.assertNotIn("error", self.page.last_export_result)
        result = self.page.last_export_result
        self.assertEqual(len(result["outputs"]), 1)
        self.assertEqual(len(result["csv_outputs"]), 1)
        import csv
        with Path(result["csv_outputs"][0]).open(encoding="utf-8-sig", newline="") as stream:
            rows = list(csv.reader(stream))
        self.assertIn("SourceFile", rows[2][0])
        self.assertNotIn("#", rows[0][0])
        self.page.measurement_tabs.setCurrentIndex(0)
        self.wait_for(lambda: not self.page.busy)
        self.assertIsNone(self.page.export_button.menu())

    def test_automatic_export_restore_reuses_pngs_and_csvs(self):
        self.derived_map()
        self.page.export_to()
        self.wait_for(lambda: self.page.last_export_result is not None)
        first = self.page.last_export_result
        self.assertNotIn('error', first)
        self.page.colormap_combo.setCurrentText('viridis')
        self.page.restore_export_view(first['view_path'])
        self.wait_for(lambda: not self.page.busy and self.page.png_actions.restore_view is None)
        self.page.export_to()
        self.wait_for(lambda: self.page.last_export_result is not None)
        second = self.page.last_export_result
        self.assertNotIn('error', second)
        self.assertEqual(first['outputs'], second['outputs'])
        self.assertEqual(first['csv_outputs'], second['csv_outputs'])
        self.assertEqual(len(second['reused_outputs']), 4)

    def test_save_as_keeps_chosen_location_and_writes_companion_csv(self):
        from unittest.mock import patch
        self.derived_map()
        target = self.root / 'chosen.png'
        with patch('PySide6.QtWidgets.QFileDialog.getSaveFileName', return_value=(str(target), 'PNG image (*.png)')):
            self.page.png_actions.save_as_action.trigger()
        self.wait_for(lambda: self.page.last_export_result is not None)
        result = self.page.last_export_result
        self.assertNotIn('error', result)
        self.assertEqual({Path(p).name for p in result['outputs']}, {'chosen_heatmap.png', 'chosen_cut.png'})
        self.assertEqual({Path(p).name for p in result['csv_outputs']}, {'chosen_heatmap.csv', 'chosen_cut.csv'})


    def test_legend_shows_only_differing_settings_with_markers_and_refreshes_metadata(self):
        from app.run_output import write_run_metadata
        for name, phase, amp in (("first", 0, .1), ("second", 90, .2)):
            path = self.scan(name, "2026-10-02")
            sidecar = path.with_name(name + "_metadata.json")
            meta = json.loads(sidecar.read_text())
            meta["signal_chain"] = {"lockin_settings": {"values": {"phase_deg": phase, "sine_out_v": amp}}}
            write_run_metadata(str(sidecar), meta)
            meta = json.loads(sidecar.read_text())
        self.page.refresh()
        self.wait_for(lambda: not self.page.busy)
        for row in range(2):
            self.page.model.setData(self.page.model.index(row, 0), QtCore.Qt.CheckState.Checked, QtCore.Qt.ItemDataRole.CheckStateRole)
        self.wait_for(lambda: not self.page.busy)
        labels = " ".join(line.get_label() for line in self.page.ax.lines)
        self.assertIn("Phase = 90", labels)
        self.assertIn("Amplitude = 100 mV", labels)
        self.assertNotIn("Vtg", labels)
        self.assertNotIn("2026", labels)
        for line in self.page.ax.lines:
            self.assertEqual(line.get_marker(), ".")
            self.assertIn(line.get_linestyle(), ("-", "--"))
        from app.png_export import build_analysis_figure
        figure = build_analysis_figure(self.page.png_actions._snapshot())
        self.addCleanup(figure.clear)
        self.assertEqual([" ".join(line.get_label().split()) for line in figure.axes[0].lines],
                         [" ".join(line.get_label().split()) for line in self.page.ax.lines])
        self.assertTrue(all(line.get_marker() == "." for line in figure.axes[0].lines))
        meta["lockin_settings"]["values"]["phase_deg"] = 45
        sidecar.write_text(json.dumps(meta))
        self.page.refresh()
        self.wait_for(lambda: not self.page.busy)
        self.assertIn("Phase = 45", " ".join(line.get_label() for line in self.page.ax.lines))
        meta["params"].update(base_name="Changed_contact", n_sample=9)
        sidecar.write_text(json.dumps(meta))
        self.page.refresh()
        self.wait_for(lambda: not self.page.busy)
        labels = " ".join(" ".join(line.get_label().split()) for line in self.page.ax.lines)
        self.assertIn("Name = Changed_contact", labels)
        self.assertIn("Samples = 9", labels)

    def test_drag_ratio_is_selectable_and_normalizes_without_current_units(self):
        import numpy as np
        path = self.scan("dual", "2026-10-02")
        path.write_text("Vds,I_drag_X,I_drive_X,Direction\nV,A,A,\n0,1,4,forward\n1,2,4,forward\n")
        self.page.refresh()
        self.wait_for(lambda: not self.page.busy)
        self.assertGreaterEqual(self.page.signal_combo.findText("Drag ratio X"), 0)
        self.page.signal_combo.setCurrentText("Drag ratio X")
        self.page.model.setData(self.page.model.index(0, 0), QtCore.Qt.CheckState.Checked, QtCore.Qt.ItemDataRole.CheckStateRole)
        self.wait_for(lambda: not self.page.busy)
        np.testing.assert_allclose(self.page.ax.lines[0].get_ydata(), [.25, .5])
        self.assertNotIn("(A)", self.page.ax.get_ylabel())
        self.page.normalization_combo.setCurrentIndex(self.page.normalization_combo.findData("max_abs"))
        self.wait_for(lambda: not self.page.busy)
        np.testing.assert_allclose(self.page.ax.lines[0].get_ydata(), [.5, 1.])

    def test_normalization_compares_different_magnitudes_and_restores_raw_values(self):
        import numpy as np
        self.assertTrue(hasattr(self.page, "normalization_combo"), "Comparison needs a normalization selector")
        originals = {}
        for name, values in (("large", [-4., -2., 0., 2.]), ("small", [-4e-9, -2e-9, 0., 2e-9])):
            path = self.scan(name, "2026-10-02")
            path.write_text("Vds,Ids_DC,Direction\nV,A,\n" + "".join(f"{i},{value},forward\n" for i, value in enumerate(values)))
            originals[path] = path.read_bytes()
        self.page.refresh()
        self.wait_for(lambda: not self.page.busy)
        for row in range(2):
            self.page.model.setData(self.page.model.index(row, 0), QtCore.Qt.CheckState.Checked, QtCore.Qt.ItemDataRole.CheckStateRole)
        self.wait_for(lambda: not self.page.busy)
        raw = {trace.path: trace.y.copy() for trace in self.page._displayed_traces}
        self.assertEqual(self.page.normalization_combo.currentData(), "raw")
        for mode, expected in (("max_abs", [-1., -.5, 0., .5]), ("min_max", [0., 1/3, 2/3, 1.])):
            self.page.normalization_combo.setCurrentIndex(self.page.normalization_combo.findData(mode))
            self.assertFalse(self.page.export_button.isEnabled())
            self.wait_for(lambda: not self.page.busy)
            self.assertEqual(len(self.page.ax.lines), 2)
            for line in self.page.ax.lines:
                np.testing.assert_allclose(line.get_ydata(), expected)
            self.assertIn("Normalized", self.page.ax.get_ylabel())
            self.assertNotIn("(A)", self.page.ax.get_ylabel())
        self.page.normalization_combo.setCurrentIndex(0)
        self.wait_for(lambda: not self.page.busy)
        for trace in self.page._displayed_traces:
            np.testing.assert_array_equal(trace.y, raw[trace.path])
        for path, contents in originals.items():
            self.assertEqual(path.read_bytes(), contents)

    def test_normalization_survives_reload_and_png_view_restore(self):
        import numpy as np
        from PIL import Image
        self.assertTrue(hasattr(self.page, "normalization_combo"), "Comparison needs a normalization selector")
        self.scan("normalized", "2026-10-02")
        self.page.refresh()
        self.wait_for(lambda: not self.page.busy)
        self.page.model.setData(self.page.model.index(0, 0), QtCore.Qt.CheckState.Checked, QtCore.Qt.ItemDataRole.CheckStateRole)
        self.page.normalization_combo.setCurrentIndex(self.page.normalization_combo.findData("min_max"))
        self.wait_for(lambda: not self.page.busy)
        expected = [line.get_ydata().copy() for line in self.page.ax.lines]
        state = self.page.capture_session()
        fresh = CurveComparePage(lambda: self.folder, settings=self.settings)
        self.addCleanup(lambda: (fresh.shutdown(), fresh.pool.waitForDone(5000), fresh.close()))
        fresh.restore_session(state)
        self.wait_for(lambda: not fresh.busy)
        self.assertEqual(fresh.normalization_combo.currentData(), "min_max")
        for line, wanted in zip(fresh.ax.lines, expected):
            np.testing.assert_allclose(line.get_ydata(), wanted)
        target = self.root / "normalized.png"
        self.page.export_to(target)
        self.wait_for(lambda: self.page.last_export_result is not None)
        self.assertNotIn("error", self.page.last_export_result)
        view = self.root / "normalized_view.json"
        self.assertEqual(json.loads(view.read_text())["view"]["normalization"], "min_max")
        with Image.open(target) as image:
            self.assertIn("Normalized", image.info["Description"])
        self.page.normalization_combo.setCurrentIndex(0)
        self.wait_for(lambda: not self.page.busy)
        self.page.restore_export_view(view)
        self.wait_for(lambda: not self.page.busy)
        self.assertEqual(self.page.normalization_combo.currentData(), "min_max")
        for line, wanted in zip(self.page.ax.lines, expected):
            np.testing.assert_allclose(line.get_ydata(), wanted)

    def test_normalized_map_cuts_keep_heatmap_physical_units_in_export(self):
        import numpy as np
        from app.png_export import build_analysis_figure
        self.assertTrue(hasattr(self.page, "normalization_combo"), "Comparison needs a normalization selector")
        path = self.scan("map_normalized", "2026-10-02", "map_2d")
        path.write_text("Vtg,Vbg,Ids_DC,PassIndex,FastDirection\nV,V,A,#,\n0,10,1e-9,0,forward\n1,10,2e-9,0,forward\n0,20,3e-9,1,reverse\n1,20,4e-9,1,reverse\n")
        self.page.refresh()
        self.wait_for(lambda: not self.page.busy)
        self.page.measurement_tabs.setCurrentIndex(2)
        self.page.model.setData(self.page.model.index(0, 0), QtCore.Qt.CheckState.Checked, QtCore.Qt.ItemDataRole.CheckStateRole)
        self.page.normalization_combo.setCurrentIndex(self.page.normalization_combo.findData("max_abs"))
        self.wait_for(lambda: not self.page.busy)
        np.testing.assert_allclose(self.page.ax.lines[0].get_ydata(), [.5, 1.])
        np.testing.assert_allclose(self.page._displayed_primary.z, [[1e-9, 2e-9], [3e-9, 4e-9]])
        figure = build_analysis_figure(self.page.png_actions._snapshot())
        self.addCleanup(figure.clear)
        np.testing.assert_allclose(figure.axes[0].lines[0].get_ydata(), [.5, 1.])
        self.assertIn("Normalized", figure.axes[0].get_ylabel())
        np.testing.assert_allclose(figure.axes[1].collections[0].get_array(), [[1., 2.], [3., 4.]])
        self.assertEqual(figure.axes[2].get_ylabel(), "Ids_DC (nA)")


if __name__ == "__main__":
    unittest.main()
