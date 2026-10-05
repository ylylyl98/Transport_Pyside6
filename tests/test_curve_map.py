import json
import tempfile
import unittest
from pathlib import Path

import numpy as np
from app.curve_history import discover_history
from app.curve_map import load_map, map_comparison


class CurveMapTests(unittest.TestCase):
    def setUp(self):
        self.scratch = tempfile.TemporaryDirectory()
        self.addCleanup(self.scratch.cleanup)
        self.root = Path(self.scratch.name)

    def make_map(self, name="map", duplicate=False):
        path = self.root / (name + ".csv")
        rows = "Vtg,Vbg,Ids_DC,PassIndex,FastDirection\nV,V,A,#,\n0,10,1,0,forward\n1,10,2,0,forward\n1,20,4,0,backward\n0,20,3,0,backward\n"
        if duplicate:
            rows += "0,10,100,1,forward\n1,10,200,1,forward\n"
        path.write_text(rows, encoding="utf-8")
        path.with_name(name + "_metadata.json").write_text(json.dumps({"measurement": "map_2d", "params": {"axis_fast": "Vtg", "axis_slow": "Vbg"}}), encoding="utf-8")
        return path

    def test_snake_map_uses_coordinates_and_keeps_missing_cells(self):
        path = self.make_map()
        result = load_map(path, "Vtg", "Vbg", "Ids_DC")
        self.assertEqual(result.x.tolist(), [0, 1])
        self.assertEqual(result.y.tolist(), [10, 20])
        self.assertEqual(result.z.tolist(), [[1, 2], [3, 4]])
        result = load_map(path, "Vtg", "Vbg", "Ids_DC", "forward")
        self.assertEqual(result.z.tolist(), [[1, 2]])

    def test_production_snake_rows_are_not_mistaken_for_repeated_full_passes(self):
        path = self.root / "map.csv"
        path.write_text("Vtg,Vbg,Ids_DC,PassIndex,FastDirection\nV,V,A,#,\n0,10,1,0,forward\n1,10,2,0,forward\n1,20,4,1,reverse\n0,20,3,1,reverse\n", encoding="utf-8")
        result = load_map(path, "Vtg", "Vbg", "Ids_DC")
        self.assertEqual(result.z.tolist(), [[1, 2], [3, 4]])
        backward = load_map(path, "Vtg", "Vbg", "Ids_DC", "backward")
        self.assertEqual(backward.z.tolist(), [[3, 4]])

    def test_duplicate_passes_are_selected_without_averaging(self):
        path = self.make_map(duplicate=True)
        result = load_map(path, "Vtg", "Vbg", "Ids_DC", pass_index="First pass")
        self.assertEqual(result.pass_index, 0)
        self.assertEqual(result.passes, [0, 1])
        self.assertEqual(result.z[0].tolist(), [1, 2])
        later = load_map(path, "Vtg", "Vbg", "Ids_DC", pass_index="1")
        self.assertEqual(later.z.tolist(), [[100, 200]])

    def test_duplicate_same_pass_is_reported_instead_of_averaged(self):
        path = self.make_map()
        with path.open("a") as stream:
            stream.write("0,10,99,0,forward\n")
        with self.assertRaisesRegex(ValueError, "Duplicate"):
            load_map(path, "Vtg", "Vbg", "Ids_DC")

    def test_comparison_uses_measured_fixed_value_for_each_selected_map(self):
        first = self.make_map("first")
        self.make_map("second")
        records, _ = discover_history(self.root)
        result, traces, warnings = map_comparison(records, "Vtg", "Vbg", "Ids_DC", "All", "First pass", str(first), "Vbg", 20)
        self.assertEqual(result.z.tolist(), [[1, 2], [3, 4]])
        self.assertEqual(len(traces), 2)
        self.assertEqual(traces[0].x.tolist(), [0, 1])
        self.assertEqual(traces[0].y.tolist(), [3, 4])
        self.assertTrue(all("Vbg=20" in trace.label for trace in traces))
        self.assertEqual(warnings, [])

    def test_missing_fixed_value_does_not_snap_to_other_condition(self):
        path = self.make_map()
        records, _ = discover_history(self.root)
        result, traces, warnings = map_comparison(records, "Vtg", "Vbg", "Ids_DC", "All", "First pass", str(path), "Vbg", 15)
        self.assertIsNotNone(result)
        self.assertEqual(traces, [])
        self.assertTrue(warnings)

    def test_fixed_axis_units_must_match_before_comparing_conditions(self):
        first = self.make_map("first")
        second = self.make_map("second")
        second.write_text(second.read_text().replace("V,V,A,#,", "V,mV,A,#,"), encoding="utf-8")
        records, _ = discover_history(self.root)
        result, traces, warnings = map_comparison(records, "Vtg", "Vbg", "Ids_DC", "All", "All rows", str(first), "Vbg", 10)
        self.assertEqual(len(traces), 1)
        self.assertEqual(traces[0].path, first)
        self.assertTrue(warnings)

    def test_cut_units_are_anchored_to_chosen_primary_map(self):
        primary = self.make_map("primary")
        wrong = self.make_map("wrong")
        wrong.write_text(wrong.read_text().replace("V,V,A,#,", "mV,V,A,#,"), encoding="utf-8")
        records, _ = discover_history(self.root)
        records.sort(key=lambda record: record.path == primary)
        result, traces, warnings = map_comparison(records, "Vtg", "Vbg", "Ids_DC", "All", "All rows", str(primary), "Vbg", 10)
        self.assertEqual([trace.path for trace in traces], [primary])
        self.assertEqual(traces[0].x_unit, "V")
        self.assertTrue(warnings)

    def test_switching_signal_reuses_csv_and_grid_but_refresh_loads_new_values(self):
        from unittest.mock import patch
        from app.curve_cache import SnapshotCache
        path = self.make_map()
        path.write_text(path.read_text().replace("Ids_DC,", "Ids_DC,Ids_X,")
                        .replace("V,V,A,#,", "V,V,A,A,#,")
                        .replace(",1,0,forward", ",1,11,0,forward")
                        .replace(",2,0,forward", ",2,12,0,forward")
                        .replace(",4,0,backward", ",4,14,0,backward")
                        .replace(",3,0,backward", ",3,13,0,backward"))
        records, _ = discover_history(self.root)
        cache = SnapshotCache()
        first, _, _ = map_comparison(records, "Vtg", "Vbg", "Ids_DC", cache=cache)
        with patch("app.curve_history.read_csv_snapshot", side_effect=AssertionError("CSV reread for signal change")), \
             patch("app.curve_map._map_grid", side_effect=AssertionError("Grid rebuilt for signal change")):
            second, _, _ = map_comparison(records, "Vtg", "Vbg", "Ids_X", cache=cache)
        self.assertEqual(first.z.tolist(), [[1, 2], [3, 4]])
        self.assertEqual(second.z.tolist(), [[11, 12], [13, 14]])
        path.write_text(path.read_text().replace(",11,", ",111,"))
        records, _ = discover_history(self.root)
        updated, _, _ = map_comparison(records, "Vtg", "Vbg", "Ids_X", cache=cache)
        self.assertEqual(updated.z[0, 0], 111)

    def test_auto_axes_can_infer_fast_and_slow_from_legacy_csv(self):
        path = self.root / "legacy.csv"
        path.write_text("Vtg,Vbg,Doping,Vds,Ids_DC,PassIndex\nV,V,V,V,A,#\n"
                        "0,0,-1,10,1,0\n0,0,1,10,2,0\n"
                        "0,0,1,20,4,1\n0,0,-1,20,3,1\n")
        records, _ = discover_history(self.root)
        primary, traces, warnings = map_comparison(records, "Vtg", "Vbg", "Ids_DC", auto_axes=True)
        self.assertEqual((primary.x_name, primary.y_name), ("Doping", "Vds"))
        self.assertEqual(primary.z.tolist(), [[1, 2], [3, 4]])
        self.assertEqual(warnings, [])

    def test_auto_axes_preserve_selected_cut_for_legacy_efield_column(self):
        path = self.make_map()
        path.write_text(path.read_text().replace("Vtg,Vbg", "Doping,Efield"))
        path.with_name(path.stem + "_metadata.json").write_text(json.dumps({
            "measurement": "map_2d", "params": {"axis_fast": "Doping", "axis_slow": "E-field"}
        }))
        records, _ = discover_history(self.root)
        primary, traces, warnings = map_comparison(records, "Doping", "E-field", "Ids_DC",
                                                   fixed_axis="Efield", fixed_value=20, auto_axes=True)
        self.assertEqual(primary.y_name, "Efield")
        self.assertEqual(traces[0].y.tolist(), [3, 4])
        self.assertIn("Efield=20", traces[0].label)
        self.assertEqual(warnings, [])

    def test_native_schema_retains_shared_data_with_a_bounded_cache(self):
        from unittest.mock import patch
        from app.curve_cache import SnapshotCache
        path = self.root / "map.csv"
        with path.open("w") as stream:
            stream.write("Vtg,Vbg,Vds,Vds_measured,raw_X,raw_Y,raw_DC,Ids_X,Ids_Y,Ids_DC,Ids_Keithley,Doping,E-field,PassIndex,FastDirection\n")
            stream.write("V,V,V,V,V,V,V,A,A,A,A,V,V,#,\n")
            for y in range(50):
                for x in range(100):
                    stream.write(f"{x},{y},0,0,1,2,3,{x+10},{y+20},{x+y},5,0,0,{y},forward\n")
        records, _ = discover_history(self.root)
        cache = SnapshotCache(max_bytes=2 * 1024 * 1024)
        map_comparison(records, "Vtg", "Vbg", "Ids_DC", cache=cache)
        with patch("app.curve_history.read_csv_snapshot", side_effect=AssertionError("Native CSV excluded from cache")):
            result, _, warnings = map_comparison(records, "Vtg", "Vbg", "Ids_X", cache=cache)
        self.assertEqual(result.z[49, 99], 109)
        self.assertEqual(warnings, [])
        self.assertLessEqual(cache.bytes_used, cache.max_bytes)

    def test_nearest_cut_reports_actual_coordinate_for_both_orientations(self):
        self.make_map()
        records, _ = discover_history(self.root)
        result = map_comparison(records, "Vtg", "Vbg", "Ids_DC", fixed_axis="Vbg", fixed_value=18, nearest=True)
        self.assertEqual(result.fixed_axis, "Vbg")
        self.assertEqual(result.fixed_value, 20)
        self.assertEqual(result.traces[0].y.tolist(), [3, 4])
        vertical = map_comparison(records, "Vtg", "Vbg", "Ids_DC", fixed_axis="Vtg", fixed_value=.9, nearest=True)
        self.assertEqual(vertical.fixed_value, 1)
        self.assertEqual(vertical.traces[0].x.tolist(), [10, 20])
        self.assertEqual(vertical.traces[0].y.tolist(), [2, 4])

    def test_nearest_cut_does_not_snap_each_comparison_to_different_conditions(self):
        first = self.make_map("first")
        second = self.make_map("second")
        second.write_text(second.read_text().replace(",20,", ",21,"))
        records, _ = discover_history(self.root)
        result = map_comparison(records, "Vtg", "Vbg", "Ids_DC", primary_path=str(first), fixed_axis="Vbg", fixed_value=19, nearest=True)
        self.assertEqual(result.fixed_value, 20)
        self.assertEqual([trace.path for trace in result.traces], [first])
        self.assertTrue(result.warnings)


if __name__ == "__main__":
    unittest.main()
