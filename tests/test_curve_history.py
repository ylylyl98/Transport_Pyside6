import json
import tempfile
import unittest
from pathlib import Path

from app.curve_history import discover_history, load_curve, comparison_traces


class CurveHistoryTests(unittest.TestCase):
    def setUp(self):
        self.scratch = tempfile.TemporaryDirectory()
        self.addCleanup(self.scratch.cleanup)
        self.root = Path(self.scratch.name)

    def write_scan(self, name, text, metadata=None):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        if metadata is not None:
            path.with_name(path.stem + "_metadata.json").write_text(json.dumps(metadata), encoding="utf-8")
        return path

    def test_history_discovers_nested_measurement_types_and_excludes_exports(self):
        old = self.write_scan("2026-09-30/vds_sweep/old.csv", "Vds,Ids_DC,Direction\nV,A,\n0,1,forward\n", {
            "measurement": "vds_sweep", "created_at": "2026-09-30T12:00:00", "status": "finished",
            "params": {"vtg_set": 1.5, "vbg_set": -2, "vds_start": 0, "vds_stop": 0.1}})
        new = self.write_scan("2026-10-01/gate_scan/new.csv", "Vtg,Vbg,Vds,Doping,Efield,Ids_X,Direction\nV,V,V,V,V,A,\n0,0,.01,0,0,1,forward\n", {
            "measurement": "gate_scan", "created_at": "2026-10-01T12:00:00", "status": "stopped"})
        map_path = self.write_scan("2026-10-01/map_2d/map.csv", "Vds,Vtg,Ids_DC\nV,V,A\n0,0,1\n", {"measurement": "map_2d", "created_at": "2026-10-01T11:00:00"})
        self.write_scan("exports/cut.csv", "Vds,Ids_DC\nV,A\n0,1\n")
        records, warnings = discover_history(self.root)
        self.assertEqual([r.path for r in records], [new, map_path, old])
        self.assertEqual(records[0].status, "stopped")
        self.assertIn("Vtg=1.5", records[2].conditions)
        self.assertEqual(records[2].units["Ids_DC"], "A")
        self.assertEqual(warnings, [])

    def test_legacy_flat_scan_survives_broken_metadata_and_numeric_second_row(self):
        path = self.write_scan("device_vds_sweep_20260930_120000.csv", "Vds,Ids_DC,Direction\n0,1,forward\n1,2,backward\n")
        path.with_name(path.stem + "_metadata.json").write_text("{", encoding="utf-8")
        records, warnings = discover_history(self.root)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].status, "unknown")
        self.assertTrue(warnings)
        curve = load_curve(path, "Vds", "Ids_DC")
        self.assertEqual(curve.x.tolist(), [0, 1])
        self.assertEqual(curve.y.tolist(), [1, 2])

    def test_indexing_never_parses_bad_body_and_loading_reports_it(self):
        path = self.write_scan("gate_scan.csv", "Vtg,Ids_DC,Direction\nV,A,\n0,1,forward\nBROKEN\n1,2,backward\n")
        records, _ = discover_history(self.root)
        self.assertEqual(len(records), 1)
        with self.assertRaisesRegex(ValueError, "row"):
            load_curve(path, "Vtg", "Ids_DC")

    def test_trailing_partial_row_is_ignored_and_nan_gaps_are_preserved(self):
        path = self.write_scan("vds_sweep.csv", "Vds,Ids_DC,Direction\nV,A,\n0,1,forward\n1,nan,forward\n2,3,forward\n3,4")
        curve = load_curve(path, "Vds", "Ids_DC")
        self.assertEqual(curve.x.tolist(), [0, 1, 2])
        self.assertTrue(str(curve.y[1]) == "nan")
        self.assertTrue(curve.warnings)

    def test_direction_traces_preserve_acquisition_order_without_joining_passes(self):
        self.write_scan("vds_sweep.csv", "Vds,Ids_DC,Direction\nV,A,\n0,1,forward\n1,2,forward\n1,3,backward\n0,4,backward\n0,5,forward\n1,6,forward\n")
        records, _ = discover_history(self.root)
        traces, warnings = comparison_traces(records, "Vds", "Ids_DC", "All")
        self.assertEqual([t.x.tolist() for t in traces], [[0, 1], [1, 0], [0, 1]])
        self.assertEqual([t.y.tolist() for t in traces], [[1, 2], [3, 4], [5, 6]])
        backward, _ = comparison_traces(records, "Vds", "Ids_DC", "backward")
        self.assertEqual(len(backward), 1)
        self.assertEqual(backward[0].y.tolist(), [3, 4])
        self.assertEqual(warnings, [])

    def test_incompatible_units_and_missing_channels_do_not_hide_valid_curves(self):
        self.write_scan("vds_sweep_good.csv", "Vds,Ids_DC,Direction\nV,A,\n0,1,forward\n", {"measurement": "vds_sweep", "created_at": "2026-10-01T12:00:00"})
        self.write_scan("vds_sweep_wrong.csv", "Vds,Ids_DC,Direction\nV,V,\n0,2,forward\n", {"measurement": "vds_sweep", "created_at": "2026-09-30T12:00:00"})
        self.write_scan("gate_scan_missing.csv", "Vtg,Ids_X,Direction\nV,A,\n0,2,forward\n")
        records, _ = discover_history(self.root)
        traces, warnings = comparison_traces(records, "Vds", "Ids_DC", "All")
        self.assertEqual(len(traces), 1)
        self.assertEqual(traces[0].y.tolist(), [1])
        self.assertEqual(len(warnings), 2)

    def test_missing_directory_is_empty_without_creating_it(self):
        root = self.root / "missing"
        records, warnings = discover_history(root)
        self.assertEqual(records, [])
        self.assertFalse(root.exists())
        self.assertTrue(warnings)

    def test_index_cache_reuses_headers_but_not_changed_metadata(self):
        from unittest.mock import patch
        path = self.write_scan("vds_sweep.csv", "Vds,Ids_DC,Direction\nV,A,\n0,1,forward\n", {"measurement": "vds_sweep", "status": "running"})
        cache = {}
        records, _ = discover_history(self.root, cache=cache)
        self.assertEqual(records[0].status, "running")
        with patch("app.curve_history._header", side_effect=AssertionError("Unchanged header reread")):
            records, _ = discover_history(self.root, cache=cache)
            self.assertEqual(records[0].status, "running")
        path.with_name(path.stem + "_metadata.json").write_text(json.dumps({"measurement": "vds_sweep", "status": "finished"}), encoding="utf-8")
        records, _ = discover_history(self.root, cache=cache)
        self.assertEqual(records[0].status, "finished")

    def test_photocurrent_conditions_are_separate_traces(self):
        self.write_scan("photocurrent.csv", "Condition,Wavelength,Ids_DC\n,nm,A\n1,500,1\n1,600,2\n2,500,10\n2,600,20\n")
        records, _ = discover_history(self.root)
        traces, _ = comparison_traces(records, "Wavelength", "Ids_DC")
        self.assertEqual([trace.y.tolist() for trace in traces], [[1, 2], [10, 20]])
        self.assertIn("1", traces[0].condition)

    def test_bfield_keithley_current_alias_is_selectable(self):
        path = self.write_scan("bfield_transport.csv", "B_measured_T,Ids_DC,Keithley_current,Direction\n0,1,2,forward\n1,3,4,forward\n")
        curve = load_curve(path, "B_measured_T", "Ids_Keithley")
        self.assertEqual(curve.y.tolist(), [2, 4])


if __name__ == "__main__":
    unittest.main()
