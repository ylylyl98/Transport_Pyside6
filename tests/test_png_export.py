"""Export regressions: lost channels, invented conditions, view changes and file corruption."""
import json
import os
import tempfile
import unittest
from pathlib import Path

import numpy as np
from PIL import Image


class PngExportTests(unittest.TestCase):
    def setUp(self):
        self.scratch = tempfile.TemporaryDirectory()
        self.addCleanup(self.scratch.cleanup)
        self.root = Path(self.scratch.name)

    def run_files(self, status="finished", empty_keithley=False):
        path = self.root / "device_run.csv"
        values = "" if empty_keithley else "4e-9"
        path.write_text("Vds,Vtg,Vbg,Ids_DC,Ids_X,Ids_Y,Keithley_current,Direction\nV,V,V,A,A,A,A,\n"
                        f"0,1,-1,1e-9,2e-9,3e-9,{values},forward\n"
                        f"1,1,-1,2e-9,3e-9,4e-9,{values},forward\n"
                        f"1,1,-1,3e-9,4e-9,5e-9,{values},backward\n", encoding="utf-8")
        meta = path.with_name(path.stem + "_metadata.json")
        meta.write_text(json.dumps({"measurement": "vds_sweep", "created_at": "2026-10-01T14:30:00",
                                   "status": status, "save_root": {"device_id": "SavedDevice"},
                                   "params": {"base_name": "Contact12", "vtg_set": 1, "vbg_set": -1}}), encoding="utf-8")
        return path, meta

    def test_automatic_four_channels_keep_data_and_mark_stopped(self):
        from app.png_export import export_run
        csv, meta = self.run_files("stopped")
        before = (csv.read_bytes(), meta.read_bytes())
        result = export_run({"csv_paths": [str(csv)], "metadata_path": str(meta)})
        self.assertEqual({Path(p).name for p in result["outputs"]},
                         {f"device_run_{name}.png" for name in ("Ids_DC", "Ids_X", "Ids_Y", "Ids_Keithley")})
        for output in result["outputs"]:
            self.assertEqual(Path(output).parent, self.root / "plots")
            with Image.open(output) as image:
                self.assertEqual(image.size, (1200, 900))
                self.assertAlmostEqual(image.info["dpi"][0], 150, delta=.1)
                self.assertIn("SavedDevice", image.info["Title"])
                self.assertIn("Stopped", image.info["Title"])
        self.assertEqual((csv.read_bytes(), meta.read_bytes()), before)

    @unittest.skipUnless(os.name == 'nt', 'Windows extended paths')
    def test_run_channel_and_overview_outputs_use_ordinary_paths(self):
        from app.png_export import export_run
        csv = self.root / 'dual_run.csv'
        csv.write_text('Vds,I_drag_X,I_drag_Y,I_drive_X,I_drive_Y\nV,A,A,A,A\n'
                       '0,1e-9,2e-9,3e-9,4e-9\n1,2e-9,3e-9,4e-9,5e-9\n', encoding='utf-8')
        result = export_run({'csv_paths': ['\\\\?\\' + str(csv)], 'measurement': 'vds_sweep'})
        self.assertEqual(result['warnings'], [])
        self.assertEqual(len(result['outputs']), 5)
        self.assertTrue(any('DragDrive_2x2' in path for path in result['outputs']))
        for path in result['outputs']:
            self.assertFalse(path.startswith('\\\\?\\'))
            self.assertTrue(Path(path).is_file())

    def test_absent_channel_is_skipped_and_no_field_is_invented(self):
        from app.png_export import export_run
        csv, meta = self.run_files(empty_keithley=True)
        result = export_run({"csv_paths": [str(csv)], "metadata_path": str(meta)})
        self.assertEqual(len(result["outputs"]), 3)
        with Image.open(result["outputs"][0]) as image:
            self.assertNotIn("B =", image.info["Description"])

    def test_full_dual_settings_header_keeps_comparison_plot_readable(self):
        from app.png_export import build_analysis_figure
        snapshot = self.snapshot()
        values = {"frequency_hz": 17, "phase_deg": 0, "sine_out_v": .1, "harmonic": 1,
                  "sensitivity_label": "50 mV", "time_constant_label": "100 ms",
                  "filter_slope_label": "24 dB/oct", "ref_source_label": "Internal",
                  "input_config_label": "A-B", "input_ground_label": "Float",
                  "input_coupling_label": "DC", "line_filter_label": "None", "reserve_label": "Normal"}
        for record in snapshot["records"]:
            record["metadata"]["signal_chain"] = {"preamp_gain_v_per_a": 1e7,
                "drag_drive": {"enabled": True, "series_resistance_ohm": 1e6, "drive_preamp_gain": 10,
                               "instruments": {"drag": {"values": values}, "drive": {"values": values}}}}
        figure = build_analysis_figure(snapshot)
        self.addCleanup(figure.clear)
        self.assertGreaterEqual(figure.axes[0].get_position().height, .6)
        figure.canvas.draw()
        bounds = figure.texts[1].get_window_extent(figure.canvas.get_renderer())
        self.assertLessEqual(bounds.x1, figure.bbox.width * .97)

    def test_photocurrent_metadata_resolves_all_condition_files(self):
        from app.png_export import export_run
        csv, meta = self.run_files()
        other = self.root / "condition2.csv"
        other.write_bytes(csv.read_bytes())
        data = json.loads(meta.read_text())
        data.update(measurement="photocurrent", csv_paths=[str(csv), str(other)])
        data["params"]["plot_x_resolved"] = "Vds"
        meta.write_text(json.dumps(data))
        result = export_run({"metadata_path": str(meta)})
        self.assertEqual(len(result["outputs"]), 8)

    def test_two_dimensional_grids_are_not_automatically_flattened(self):
        from app.png_export import export_run
        csv, meta = self.run_files()
        data = json.loads(meta.read_text())
        data.update(measurement="map_2d", csv_path=str(csv))
        data["params"].update(axis_fast="Vtg", axis_slow="Vbg")
        meta.write_text(json.dumps(data))
        self.assertEqual(export_run({"metadata_path": str(meta)})["outputs"], [])

    def snapshot(self):
        return {"mode": "curve", "kind": "gate_scan", "signal": "Ids_DC", "x_name": "Vtg",
                "x_unit": "V", "y_unit": "A", "view": {"xlim": [-.5, .5], "ylim": [0, 4e-9], "legend": True, "direction": "All"},
                "records": [{"path": "first.csv", "created_at": "2026-10-01T14:30:00", "status": "finished", "fingerprint": [1, 20],
                             "metadata": {"save_root": {"device_id": "D1"}, "params": {"base_name": "first", "raw_vds_start": .005, "raw_vds_active": False, "raw_vbg_start": -1, "raw_vbg_active": False}}},
                            {"path": "second.csv", "created_at": "2026-10-01T14:42:00", "status": "finished", "fingerprint": [2, 20],
                             "metadata": {"save_root": {"device_id": "D1"}, "params": {"base_name": "second", "raw_vds_start": .010, "raw_vds_active": False, "raw_vbg_start": -1, "raw_vbg_active": False}}}],
                "traces": [{"path": "first.csv", "x": np.array([-1, 0, 1]), "y": np.array([1e-9, 2e-9, 3e-9]), "direction": "forward", "condition": ""},
                           {"path": "second.csv", "x": np.array([-1, 0, 1]), "y": np.array([2e-9, 3e-9, 4e-9]), "direction": "backward", "condition": ""}]}

    def test_analysis_exports_displayed_values_zoom_and_differing_conditions(self):
        from app.png_export import build_analysis_figure, export_analysis
        snapshot = self.snapshot()
        fig = build_analysis_figure(snapshot)
        self.addCleanup(fig.clear)
        ax = fig.axes[0]
        np.testing.assert_allclose(ax.lines[0].get_ydata(), [1, 2, 3])
        self.assertEqual(tuple(ax.get_xlim()), (-.5, .5))
        self.assertEqual(tuple(ax.get_ylim()), (0, 4))
        self.assertIn("5 mV", ax.lines[0].get_label())
        self.assertIn("10 mV", ax.lines[1].get_label())
        headings = " ".join(t.get_text() for t in fig.texts)
        self.assertIn("Vbg=−1 V", headings)
        self.assertNotIn("Vds =", headings)
        target = self.root / "compare.png"
        result = export_analysis(snapshot, target)
        sidecar = json.loads(Path(result["view_path"]).read_text())
        self.assertEqual(sidecar["view"]["xlim"], [-.5, .5])
        self.assertEqual(len(sidecar["sources"]), 2)
        self.assertEqual(sidecar["view"]["signal"], "Ids_DC")
        with Image.open(result["outputs"][0]) as image:
            self.assertEqual(image.size, (1200, 900))
        second = export_analysis(snapshot, target)
        self.assertNotEqual(second["outputs"], result["outputs"])

    def map_snapshot(self):
        snapshot = self.snapshot()
        snapshot.update(mode="map", kind="map_2d", map={"path": "first.csv", "x_name": "Vtg", "y_name": "Vbg", "x_unit": "V", "y_unit": "V", "signal_unit": "A", "x": np.array([-1, 0, 1]), "y": np.array([-1, 1]), "z": np.array([[1e-9, 2e-9, 3e-9], [2e-9, 3e-9, 4e-9]])})
        snapshot["view"].update(fixed_axis="Vbg", fixed_value=-1, map_xlim=[-.5, .8], map_ylim=[-.7, .6], clim=[1e-9, 4e-9], cmap="RdBu_r")
        return snapshot

    def test_map_and_cut_save_separately_and_cut_only_stays_single(self):
        from app.png_export import export_analysis, save_snapshot, load_snapshot
        snapshot = self.map_snapshot()
        archive = self.root / "snapshot.npz"
        save_snapshot(snapshot, archive)
        frozen = load_snapshot(archive)
        for cut in (False, True):
            result = export_analysis(frozen, self.root / f"map_{cut}.png", cut_only=cut)
            self.assertEqual({Path(output).name for output in result["outputs"]},
                             {"map_True.png"} if cut else {"map_False_heatmap.png", "map_False_cut.png"})
            for output in result["outputs"]:
                with Image.open(output) as image:
                    self.assertEqual(image.size, (1200, 900))
                    if output.endswith("_heatmap.png"):
                        self.assertNotIn("Cut:", image.info["Description"])
                    else:
                        self.assertIn("Vbg=−1 V", image.info["Description"])
                    self.assertIn("first", image.info["Title"])
            saved = json.loads(Path(result["view_path"]).read_text())
            self.assertEqual(saved["view"]["fixed_value"], -1)
            self.assertEqual(saved["view"]["map_xlim"], [-.5, .8])

    def test_standalone_heatmap_keeps_physical_units_without_cut_marker(self):
        from app.png_export import build_analysis_figure
        snapshot = self.map_snapshot()
        snapshot.update(x_name="Vbg", y_unit="")
        snapshot["view"].update(normalization="max_abs", fixed_axis="Vtg", fixed_value=0)
        snapshot["traces"][0]["y"] = np.array([.25, .5, 1.])
        figure = build_analysis_figure(snapshot, map_only=True)
        self.addCleanup(figure.clear)
        self.assertEqual(len(figure.axes), 2)
        ax, bar = figure.axes
        self.assertEqual((ax.get_xlabel(), ax.get_ylabel()), ("Vtg (V)", "Vbg (V)"))
        self.assertIn("nA", bar.get_ylabel())
        np.testing.assert_allclose(ax.collections[0].get_array(), [[1, 2, 3], [2, 3, 4]])
        self.assertEqual(ax.collections[0].get_clim(), (1, 4))
        self.assertEqual(ax.collections[0].get_cmap().name, "RdBu_r")
        self.assertEqual(tuple(ax.get_xlim()), (-.5, .8))
        self.assertEqual(tuple(ax.get_ylim()), (-.7, .6))
        self.assertEqual(len(ax.lines), 0)
        self.assertNotIn("Cut:", " ".join(text.get_text() for text in figure.texts))
        self.assertEqual(snapshot["view"]["normalization"], "max_abs")
        self.assertEqual(snapshot["view"]["fixed_value"], 0)

    def test_map_export_collision_keeps_existing_files_and_numbers_whole_pair(self):
        from app.png_export import export_analysis
        existing = self.root / "map_cut.png"
        existing.write_bytes(b"keep existing cut")
        result = export_analysis(self.map_snapshot(), self.root / "map.png")
        self.assertEqual(existing.read_bytes(), b"keep existing cut")
        self.assertEqual({Path(output).name for output in result["outputs"]}, {"map_02_heatmap.png", "map_02_cut.png"})
        self.assertEqual(Path(result["view_path"]).name, "map_02_view.json")
        self.assertFalse((self.root / "map_heatmap.png").exists())

    def test_failed_second_image_leaves_no_partial_export(self):
        from unittest.mock import patch
        from app.png_export import export_analysis, _write_png
        def write(figure, output, *args):
            if Path(output).stem.endswith("_cut"):
                figure.clear()
                raise OSError("simulated disk error")
            return _write_png(figure, output, *args)
        with patch("app.png_export._write_png", side_effect=write), self.assertRaisesRegex(OSError, "disk error"):
            export_analysis(self.map_snapshot(), self.root / "map.png")
        self.assertEqual(list(self.root.iterdir()), [])

    def test_map_without_cut_saves_only_heatmap(self):
        from app.png_export import export_analysis
        snapshot = self.map_snapshot()
        snapshot["traces"] = []
        snapshot["view"]["fixed_value"] = None
        result = export_analysis(snapshot, self.root / "map.png")
        self.assertEqual([Path(output).name for output in result["outputs"]], ["map_heatmap.png"])

    def test_heatmap_only_export_omits_cut_and_preserves_view(self):
        from app.png_export import export_analysis
        snapshot = self.map_snapshot()
        snapshot["view"]["normalization"] = "max_abs"
        snapshot["y_unit"] = ""
        result = export_analysis(snapshot, self.root / "only_heatmap.png", heatmap_only=True)
        self.assertEqual([Path(output).name for output in result["outputs"]], ["only_heatmap.png"])
        with Image.open(result["outputs"][0]) as image:
            self.assertEqual(image.size, (1200, 900))
            self.assertNotIn("Cut:", image.info["Description"])
            self.assertIn("nA", image.info["Description"])
        saved = json.loads(Path(result["view_path"]).read_text())
        self.assertTrue(saved["view"]["heatmap_only"])
        self.assertEqual(saved["view"]["normalization"], "max_abs")
        self.assertEqual(saved["view"]["fixed_value"], -1)

    def test_bfield_manifest_without_csv_unit_row_has_current_and_field_units(self):
        from app.png_export import export_run
        csv = self.root / "field.csv"
        csv.write_text("B_measured_T,Ids_DC,Keithley_current,Direction\n-1,1e-9,2e-9,forward\n1,2e-9,3e-9,forward\n")
        manifest = self.root / "series_manifest.json"
        manifest.write_text(json.dumps({"schema": "bfield_transport_manifest_v1", "status": "finished", "params": {}}))
        result = export_run({"csv_paths": [str(csv)], "metadata_path": str(manifest), "device_id": "D1"})
        self.assertEqual(len(result["outputs"]), 2)
        with Image.open(result["outputs"][0]) as image:
            self.assertIn("T", image.info["Description"])
            self.assertIn("nA", image.info["Description"])
        from app.curve_history import load_curve
        loaded = load_curve(csv, "B_measured_T", "Ids_DC")
        self.assertEqual((loaded.x_unit, loaded.y_unit), ("T", "A"))

    def test_common_sweep_range_is_shared_without_repeating_in_legend(self):
        from app.png_export import build_analysis_figure
        snapshot = self.snapshot()
        for record in snapshot["records"]:
            record["metadata"]["params"].update(raw_vds_active=True, raw_vds_start=0, raw_vds_stop=.1)
        fig = build_analysis_figure(snapshot)
        self.addCleanup(fig.clear)
        self.assertIn("0 V → 100 mV", " ".join(text.get_text() for text in fig.texts))
        self.assertNotIn("Vds =", fig.axes[0].lines[0].get_label())

    def test_derived_vds_sweep_is_not_labeled_with_unused_fixed_default(self):
        from app.plot_export_labels import conditions
        record = {"path": "gate.csv", "metadata": {"params": {"mode": "Derived", "derived_axis": "Doping", "derived_fixed": -1,
                  "derived_vds_mode": "Sweep", "derived_vds_fixed": 0, "derived_vds_start": .01, "derived_vds_stop": .1}}}
        self.assertEqual(conditions(record)["Vds"], "10 mV → 100 mV")


if __name__ == "__main__":
    unittest.main()
