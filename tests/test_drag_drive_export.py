"""Automatic exports use saved currents and coordinates, never the live plot."""
import csv
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np
from PIL import Image

from app.drag_drive import DUAL_SIGNALS
from app.png_export import export_run


class DragDriveExportTests(unittest.TestCase):
    def setUp(self):
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.root = Path(scratch.name)

    def run_files(self, *, grid=False, partial=False, empty_channel=False):
        path = self.root / "dual.csv"
        # Deliberately different orders of magnitude; raw DAQ values must not be plotted.
        samples = [(0, -1, 1), (1, -1, 2), (2, -1, 3),
                   (2, 1, 6), (1, 1, 5), (0, 1, 4)] if grid else [(0, 0, 1), (1, 0, 2), (1, 0, 3)]
        if partial:
            samples = samples[:-1]
        with path.open("w", newline="", encoding="utf-8") as stream:
            writer = csv.writer(stream)
            writer.writerow(["Vds", "Vtg", "Vbg", "Ids_DC", "raw_drive_X", *DUAL_SIGNALS, "PassIndex", "FastDirection"])
            writer.writerow(["V", "V", "V", "A", "V", *("A",) * 6, "#", ""])
            for index, (x, y, value) in enumerate(samples):
                currents = [value * 1e-12, -value * 2e-12, value * 3e-12,
                            value * 1e-6, -value * 2e-6, value * 3e-6]
                if empty_channel:
                    currents[4] = ""
                writer.writerow([x, x, y, value * 1e-9, value * .2, *currents,
                                 1 if index < (3 if grid else 2) else 2,
                                 "forward" if index < (3 if grid else 2) else "backward"])
        metadata = {"measurement": "map_2d" if grid else "vds_sweep", "status": "stopped" if partial else "finished",
                    "csv_path": str(path), "save_root": {"device_id": "ExportTest"},
                    "signal_chain": {"drag_drive": {"enabled": True}},
                    "params": {"base_name": "dual", "axis_fast": "Vtg", "axis_slow": "Vbg" if grid else "None",
                               "plot_x_resolved": "Step Index" if grid else "Vds"}}
        meta = self.root / "dual_metadata.json"
        meta.write_text(json.dumps(metadata), encoding="utf-8")
        return path, meta

    def test_all_current_channels_and_overview_saved_without_modifying_sources(self):
        path, meta = self.run_files()
        before = path.read_bytes(), meta.read_bytes()
        result = export_run({"metadata_path": str(meta)})
        expected = {f"dual_{name}.png" for name in ("Ids_DC", *DUAL_SIGNALS)} | {"dual_DragDrive_2x2.png"}
        self.assertEqual({Path(p).name for p in result["outputs"]}, expected)
        self.assertEqual(result["warnings"], [])
        for output in result["outputs"]:
            with Image.open(output) as image:
                self.assertEqual(image.size, (2400, 1800) if "2x2" in output else (1200, 900))
                self.assertAlmostEqual(image.info["dpi"][0], 150, delta=.1)
                if "2x2" in output:
                    self.assertIn("Drag / Drive", image.info["Title"])
                    self.assertIn("pA", image.info["Description"])
                    self.assertIn("µA", image.info["Description"])
        self.assertEqual((path.read_bytes(), meta.read_bytes()), before)

    def capture_figures(self, meta):
        captured = {}
        def capture(figure, output, title, description):
            captured[Path(output).name] = (figure, title, description)
            self.addCleanup(figure.clear)
        with patch("app.png_export._write_png", side_effect=capture):
            result = export_run({"metadata_path": str(meta)})
        return captured, result

    def test_overview_position_units_shared_x_and_unjoined_passes(self):
        _path, meta = self.run_files()
        figures, _result = self.capture_figures(meta)
        self.assertIn("dual_DragDrive_2x2.png", figures)
        fig = figures["dual_DragDrive_2x2.png"][0]
        axes = {ax.get_title(): ax for ax in fig.axes}
        self.assertEqual(set(axes), {"Drag X", "Drag Y", "Drive X", "Drive Y"})
        drag_x, drag_y, drive_x, drive_y = (axes[name] for name in ("Drag X", "Drag Y", "Drive X", "Drive Y"))
        self.assertLess(drag_x.get_position().x0, drive_x.get_position().x0)
        self.assertGreater(drag_x.get_position().y0, drag_y.get_position().y0)
        self.assertAlmostEqual(drag_y.get_position().y0, drive_y.get_position().y0)
        for ax in axes.values():
            self.assertTrue(drag_x.get_shared_x_axes().joined(drag_x, ax))
            if ax is not drag_x:
                self.assertFalse(drag_x.get_shared_y_axes().joined(drag_x, ax))
            self.assertEqual(len(ax.lines), 2)
            self.assertEqual([len(line.get_xdata()) for line in ax.lines], [2, 1])
        np.testing.assert_allclose(drag_x.lines[0].get_ydata(), [1, 2])
        np.testing.assert_allclose(drive_x.lines[0].get_ydata(), [1, 2])
        self.assertIn("pA", drag_x.get_ylabel())
        self.assertIn("µA", drive_x.get_ylabel())

    def test_two_dimensional_export_is_heatmaps_even_when_live_x_is_step_index(self):
        path, meta = self.run_files(grid=True, partial=True)
        before = path.read_bytes(), meta.read_bytes()
        figures, result = self.capture_figures(meta)
        expected = {f"dual_{name}_heatmap.png" for name in ("Ids_DC", *DUAL_SIGNALS)} | {"dual_DragDrive_2x2_heatmap.png"}
        self.assertEqual(set(figures), expected)
        self.assertEqual(result["warnings"], [])
        fig, title, description = figures["dual_DragDrive_2x2_heatmap.png"]
        axes = {ax.get_title(): ax for ax in fig.axes if ax.get_title()}
        self.assertEqual(set(axes), {"Drag X", "Drag Y", "Drive X", "Drive Y"})
        for name in ("Drag X", "Drive X"):
            ax = axes[name]
            self.assertEqual(ax.get_xlabel(), "Vtg (V)")
            self.assertEqual(ax.get_ylabel(), "Vbg (V)")
            values = np.ma.asarray(ax.collections[0].get_array()).reshape(2, 3)
            np.testing.assert_allclose(values.filled(np.nan), [[1, 2, 3], [np.nan, 5, 6]], equal_nan=True)
        self.assertIn("Stopped / Partial", title)
        self.assertIn("Vbg", description)
        self.assertEqual((path.read_bytes(), meta.read_bytes()), before)

    def test_heatmap_pngs_are_written_and_missing_channel_does_not_hide_others(self):
        path, meta = self.run_files(grid=True, empty_channel=True)
        result = export_run({"metadata_path": str(meta)})
        self.assertIn(str(self.root / "plots/dual_DragDrive_2x2_heatmap.png"), result["outputs"])
        self.assertEqual(len(result["outputs"]), 7)
        self.assertTrue(any("I_drive_Y" in warning for warning in result["warnings"]))
        for output in result["outputs"]:
            with Image.open(output) as image:
                self.assertEqual(image.size, (2400, 1800) if "2x2" in output else (1200, 900))
        figures, _result = self.capture_figures(meta)
        fig = figures["dual_DragDrive_2x2_heatmap.png"][0]
        axis = next(ax for ax in fig.axes if ax.get_title() == "Drive Y")
        self.assertTrue(any("No finite data" in text.get_text() for text in axis.texts))

    def test_all_condition_csvs_get_their_own_overview(self):
        path, meta = self.run_files()
        other = path.with_name("condition_2.csv")
        other.write_bytes(path.read_bytes())
        data = json.loads(meta.read_text())
        data.update(measurement="photocurrent", csv_paths=[str(path), str(other)])
        meta.write_text(json.dumps(data))
        figures, _result = self.capture_figures(meta)
        self.assertIn("dual_DragDrive_2x2.png", figures)
        self.assertIn("condition_2_DragDrive_2x2.png", figures)

    def test_running_metadata_is_not_exported(self):
        _path, meta = self.run_files()
        data = json.loads(meta.read_text())
        data["status"] = "running"
        meta.write_text(json.dumps(data))
        result = export_run({"metadata_path": str(meta)})
        self.assertEqual(result["outputs"], [])
        self.assertTrue(result["warnings"])

    @unittest.skipUnless(os.environ.get("TRANSPORT_OFFLINE_VERIFICATION") == "1", "Use offline Qt wrapper")
    def test_finished_run_exports_identical_pngs_in_single_and_compare_layouts(self):
        from PySide6 import QtCore
        from app.ui.measurement_png_coordinator import MeasurementPngCoordinator
        from app.ui.widgets.plot_widget import PlotWidget
        from app.ui.widgets.run_panel import RunPanel

        path, meta = self.run_files()
        plot, panel = PlotWidget(), RunPanel("Start")
        self.addCleanup(plot.close)
        self.addCleanup(panel.close)
        plot.set_compare_channels(["I_drag_X", "I_drag_Y", "I_drive_X", "I_drive_Y"], grid=True)
        plot.set_y_axis_options(["Ids_DC", *DUAL_SIGNALS], "Ids_DC")
        tab = SimpleNamespace(plot=plot, run_panel=panel, worker_thread=None,
                              p=SimpleNamespace(output_csv_path=str(path), output_metadata_path=str(meta)))
        class Queue(QtCore.QObject):
            def __init__(self):
                super().__init__()
                self.results = []
            def submit(self, job):
                self.results.append(export_run(job))
        queue = Queue()
        coordinator = MeasurementPngCoordinator([tab], queue, lambda: True)
        self.addCleanup(coordinator.shutdown)
        images = []
        for mode in ("Single Plot", "4-Channel Compare"):
            plot.set_selected_plot_mode(mode)
            tab.worker_thread = object()
            panel.set_running(True)
            # Deliberately unrelated display data, clipped ranges, and selected DC channel.
            plot.get_axes()[0].plot([99, 100], [1, 2])
            plot.get_axes()[0].set_xlim(99, 100)
            axes = tuple(plot.get_axes())
            panel.set_running(False)
            self.assertEqual(len(queue.results), len(images))
            tab.worker_thread = None
            coordinator._flush_ready()
            self.assertEqual(plot.current_plot_mode(), mode)
            self.assertEqual(tuple(plot.get_axes()), axes)
            images.append({Path(output).name: Path(output).read_bytes() for output in queue.results[-1]["outputs"]})
        self.assertEqual(len(images[0]), 8)
        self.assertEqual(images[0], images[1])

    def test_duplicate_map_coordinates_are_reported_without_rewriting_csv(self):
        path, meta = self.run_files(grid=True)
        with path.open("a", encoding="utf-8") as stream:
            stream.write(path.read_text().splitlines()[2] + "\n")
        before = path.read_bytes()
        result = export_run({"metadata_path": str(meta)})
        self.assertEqual(result["outputs"], [])
        self.assertTrue(any("Duplicate coordinates" in warning for warning in result["warnings"]))
        self.assertEqual(path.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
