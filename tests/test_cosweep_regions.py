import os
import csv
import tempfile
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6 import QtCore, QtWidgets
from app.device_manager import DeviceManager
from app.models import CoParams, Connections, SaveRoot
from app.ui.tabs.cosweep_tab import CoSweepTab
from app.workers.cosweep import CoSweepWorker, build_cosweep_points


def example(**overrides):
    values = dict(axis_fast="Vtg", axis_slow="Vbg", vtg_start=0, vtg_stop=14.95,
                  vtg_step=0.115, vbg_start=-6, vbg_stop=12, vbg_step=0.2,
                  vds_start=-0.1, vds_stop=-0.1,
                  regions=[dict(vtg_start=14.95, vtg_stop=17, vtg_step=0.115,
                                vbg_start=-6, vbg_stop=10, vbg_step=0.2)])
    values.update(overrides)
    return CoParams(**values)


class RegionTests(unittest.TestCase):
    def test_worker_runs_merged_rows_into_one_csv(self):
        class Gate:
            voltage = 0.0
            def __init__(self):
                self.calls = []
            def ramp_voltage(self, value, step):
                self.calls.append(value)
                self.voltage = value
            def set_voltage(self, value):
                self.voltage = value
            def acquire(self):
                return {"current": 0.0}
        class Daq:
            def acquire(self):
                pass
            def get_ai_value(self, index):
                return 0.0
        with tempfile.TemporaryDirectory() as directory:
            p = CoParams(axis_fast="Vtg", axis_slow="Vbg", vtg_stop=1, vtg_step=1,
                         vbg_stop=2, vbg_step=1, n_sample=1, delay=0,
                         output_csv_path=directory + "/map.csv",
                         regions=[dict(vtg_start=1, vtg_stop=2, vtg_step=1,
                                       vbg_start=0, vbg_stop=1, vbg_step=1)])
            g1, g2, g3 = Gate(), Gate(), Gate()
            worker = CoSweepWorker(p, SaveRoot(base=directory), Connections(),
                                   g1=g1, g2=g2, g3=g3, daq=Daq())
            errors, finished = [], []
            worker.error.connect(errors.append)
            worker.finished.connect(finished.append)
            with patch("app.workers.cosweep.time.sleep"), patch("app.workers.cosweep.safe_ramp"):
                worker.run()
            self.assertEqual(errors, [])
            self.assertEqual(len(finished), 1)
            self.assertEqual(g1.calls, [0, 1, 2, 1, 0, 1])
            self.assertEqual(g2.calls, [0, 1, 2])
            with open(finished[0], newline="") as stream:
                reader = csv.DictReader(stream)
                next(reader)  # Existing CSV format includes a units row.
                rows = list(reader)
            self.assertEqual(len(rows), 8)
            self.assertEqual([float(row["Vtg"]) for row in rows], [0, 1, 2, 2, 1, 0, 0, 1])

    def test_corner_moves_stay_inside_union_in_both_orientations(self):
        for overrides in ({}, {"axis_fast": "Vbg", "axis_slow": "Vtg"},
                          {"vtg_start": 14.95, "vtg_stop": 0, "vbg_start": 12, "vbg_stop": -6}):
            p = example(**overrides)
            points = build_cosweep_points(p)
            position = {"Vtg": points[0]["vtg"], "Vbg": points[0]["vbg"]}
            for point in points[1:]:
                for axis, value in point["moves"]:
                    start = position[axis]
                    for i in range(11):
                        sample = dict(position)
                        sample[axis] = start + (value - start) * i / 10
                        self.assertTrue(-1e-10 <= sample["Vtg"] <= 14.95 + 1e-10 and -6 - 1e-10 <= sample["Vbg"] <= 12 + 1e-10
                                        or 14.95 - 1e-10 <= sample["Vtg"] <= 17 + 1e-10 and -6 - 1e-10 <= sample["Vbg"] <= 10 + 1e-10, sample)
                    position[axis] = value
                self.assertEqual(position, {"Vtg": point["vtg"], "Vbg": point["vbg"]})
        corner = next(p for p in build_cosweep_points(example()) if p["vbg"] == 10.2 and p["vtg"] == 14.95)
        self.assertEqual(corner["moves"], [("Vtg", 14.95), ("Vbg", 10.2)])

    def test_disconnected_regions_are_rejected(self):
        p = example()
        p.regions[0].update(vtg_start=16, vbg_start=-5)
        with self.assertRaisesRegex(ValueError, "without leaving"):
            build_cosweep_points(p)

    def test_example_is_one_raster_without_duplicates_or_extra_coverage(self):
        points = build_cosweep_points(example())
        pairs = {(p["vtg"], p["vbg"]) for p in points}
        self.assertEqual(len(points), len(pairs))
        self.assertEqual(len(points), 91 * 131 + 81 * 18)
        rows = {}
        for p in points:
            rows.setdefault(p["pass_index"], []).append(p)
            self.assertLessEqual(p["vtg"], 17 if p["vbg"] <= 10 else 14.95)
        self.assertEqual(len(rows), 91)
        for index, row in rows.items():
            values = [p["vtg"] for p in row]
            self.assertEqual(values, sorted(values, reverse=bool(index % 2)))
            self.assertEqual(max(values), 17 if row[0]["vbg"] <= 10 else 14.95)
        self.assertIn((17, 10), pairs)
        self.assertNotIn((17, 10.2), pairs)

    def test_swapped_and_descending_axes(self):
        base = build_cosweep_points(example())
        swapped = build_cosweep_points(example(axis_fast="Vbg", axis_slow="Vtg"))
        self.assertEqual({(p["vtg"], p["vbg"]) for p in base},
                         {(p["vtg"], p["vbg"]) for p in swapped})
        descending = build_cosweep_points(example(vtg_start=14.95, vtg_stop=0, vbg_start=12, vbg_stop=-6))
        self.assertEqual((descending[0]["vtg"], descending[0]["vbg"]), (14.95, 12))
        self.assertEqual(descending[-1]["vbg"], -6)

    def test_invalid_regions_are_rejected_before_running(self):
        for changes in ({"vtg_step": 0}, {"vtg_stop": float("nan")},
                        {"vtg_stop": 1000}, {"vtg_step": 1e-12}):
            p = example()
            p.regions[0].update(changes)
            with self.assertRaises(ValueError):
                build_cosweep_points(p)
        with self.assertRaises(ValueError):
            build_cosweep_points(example(axis_slow="None"))

    def test_region_editor_preserves_microvolt_bounds_and_submillivolt_steps(self):
        app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
        with tempfile.TemporaryDirectory() as directory:
            settings = QtCore.QSettings(directory + "/precision.ini", QtCore.QSettings.Format.IniFormat)
            with patch("app.ui.tabs.cosweep_tab.get_app_settings", return_value=settings), \
                 patch("app.ui.tabs.base_tab.get_app_settings", return_value=settings):
                conns = Connections()
                tab = CoSweepTab(SaveRoot(base=directory), conns, DeviceManager(conns))
                original = dict(vtg_start=0.123456, vtg_stop=0.124456, vtg_step=0.0001,
                                vbg_start=0.123456, vbg_stop=0.124456, vbg_step=0.0001)
                tab._regions = [dict(original)]
                def accept_dialog(dialog):
                    form = dialog.layout()
                    for index, (key, value) in enumerate(original.items()):
                        spin = form.itemAt(index, QtWidgets.QFormLayout.ItemRole.FieldRole).widget()
                        self.assertEqual(spin.decimals(), 6)
                        self.assertAlmostEqual(spin.value(), value, places=6)
                        if key.endswith("step"):
                            spin.setValue(0.000001)
                            self.assertEqual(spin.value(), 0.000001)
                            spin.setValue(value)
                    return QtWidgets.QDialog.DialogCode.Accepted
                try:
                    with patch.object(QtWidgets.QDialog, "exec", accept_dialog):
                        tab._edit_region(0)
                    self.assertEqual(tab._regions, [original])
                    self.assertEqual(tab._active_regions(), [original])
                finally:
                    tab._preview_timer.stop()
                    tab.close()

    def test_ui_preview_collection_and_persistence(self):
        app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
        with tempfile.TemporaryDirectory() as directory:
            settings = QtCore.QSettings(directory + "/settings.ini", QtCore.QSettings.Format.IniFormat)
            with patch("app.ui.tabs.cosweep_tab.get_app_settings", return_value=settings), \
                 patch("app.ui.tabs.base_tab.get_app_settings", return_value=settings):
                conns = Connections()
                tab = CoSweepTab(SaveRoot(base=directory), conns, DeviceManager(conns))
                p = example()
                for axis in ("vtg", "vbg"):
                    for part in ("start", "stop", "step"):
                        getattr(tab, f"sp_{axis}_{part}").setValue(getattr(p, f"{axis}_{part}"))
                tab._regions = p.regions
                tab._refresh_regions()
                tab._update_sweep_summary()
                tab.collect_params()
                tab.on_preview()
                self.assertEqual(tab._point_count(), len(build_cosweep_points(tab.p)))
                self.assertEqual(len(tab.preview_plot.ax.lines[0].get_xdata()), tab._point_count())
                with patch.object(tab, "on_preview") as preview:
                    tab.sp_vtg_stop.setValue(14.8)
                    self.assertTrue(tab._preview_timer.isActive())
                    loop = QtCore.QEventLoop()
                    QtCore.QTimer.singleShot(450, loop.quit)
                    loop.exec()
                    preview.assert_called_once()
                    tab._schedule_preview()
                    tab.worker_thread = object()
                    tab._refresh_automatic_preview()
                    preview.assert_called_once()
                    tab.worker_thread = None
                    tab.plot_tabs.setCurrentWidget(tab.plot)
                    tab._refresh_automatic_preview()
                    self.assertEqual(preview.call_count, 2)
                    self.assertIs(tab.plot_tabs.currentWidget(), tab.plot)
                    tab._preview_timer.stop()
                self.assertEqual([tab.plot_tabs.tabText(i) for i in range(2)], ["Sweep Preview", "Measurement"])
                tab.sp_vtg_stop.setValue(14.95)
                tab.plot.ax.plot([1, 2], [3, 4])
                measured_line = tab.plot.ax.lines[-1]
                tab.plot_tabs.setCurrentWidget(tab.plot)
                tab.on_preview()
                self.assertIs(tab.plot_tabs.currentWidget(), tab.plot)
                self.assertIs(tab.plot.ax.lines[-1], measured_line)
                tab.worker_thread = object()
                preview_line = tab.preview_plot.ax.lines[0]
                tab._show_preview()
                self.assertIs(tab.plot_tabs.currentWidget(), tab.preview_plot)
                self.assertIs(tab.preview_plot.ax.lines[0], preview_line)
                self.assertIs(tab.plot.ax.lines[-1], measured_line)
                tab.worker_thread = None
                tab.save_tab_settings()
                restored = CoSweepTab(SaveRoot(base=directory), conns, DeviceManager(conns))
                self.assertEqual(restored._regions, p.regions)
                restored.cbo_sweep_dim.setCurrentText("1D sweep")
                self.assertEqual(restored._active_regions(), [])
                restored.close()
                tab.close()


if __name__ == "__main__":
    unittest.main()
