"""Read-only output examples follow acquisition naming without reserving files."""
from copy import deepcopy
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

if os.environ.get("TRANSPORT_OFFLINE_VERIFICATION") != "1":
    raise unittest.SkipTest("Run with verify_offline.py")

from app.models import SaveRoot
from app.quick.output_preview import PREVIEW_RUN_ID, output_spec, preview_output
from app.quick.parameters import build_params, defaults, row_default
from app.run_output import build_planned_output
from app.signal_chain import SignalChainSnapshot


class QuickOutputPreviewTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "missing"
        self.save = SaveRoot(user="Operator One", device_id="Device A", base=str(self.root))
        self.chain = SignalChainSnapshot()

    def preview(self, values, mode):
        return preview_output(values, mode, self.save, self.chain)

    def test_all_modes_are_read_only_and_leave_input_untouched(self):
        with patch("app.run_output.new_run_id", side_effect=AssertionError("reserved run ID")), \
             patch("os.listdir", side_effect=AssertionError("listed directory")), \
             patch("os.makedirs", side_effect=AssertionError("created directory")):
            for mode in range(6):
                with self.subTest(mode=mode):
                    values = defaults(mode)
                    original = deepcopy(values)
                    result = self.preview(values, mode)
                    self.assertEqual(values, original)
                    self.assertFalse(self.root.exists())
                    self.assertGreaterEqual(result["csvCount"], 1)
                    self.assertIn("{timestamp}", result["csvNames"][0])
                    self.assertNotIn(PREVIEW_RUN_ID, result["csvNames"][0])
                    self.assertIn("Preview only", result["note"])
                    self.assertEqual(result["error"], "")

    def test_regular_plans_use_same_naming_and_sanitize_and_deduplicate(self):
        for mode in (0, 1, 2, 3):
            with self.subTest(mode=mode):
                values = defaults(mode)
                values["base_name"] = "test / unsafe_1kHz"
                params = build_params(values, mode)
                kind, parts = output_spec(params, mode, self.chain)
                expected = build_planned_output(self.save, kind, params.base_name, parts,
                    run_id=PREVIEW_RUN_ID, freeze=True)
                result = self.preview(values, mode)
                if mode != 3:
                    self.assertEqual(result["csvNames"][0], os.path.basename(expected.csv_path).replace(PREVIEW_RUN_ID, "{timestamp}"))
                self.assertEqual(result["folder"], expected.output_dir)
                self.assertEqual(result["csvNames"][0].count("1kHz"), 1)
                self.assertNotIn("/", result["csvNames"][0])
                self.assertNotIn("\\\\", result["csvNames"][0])

    def test_map_switches_output_directory_between_1d_and_2d(self):
        values = defaults(2)
        values["axis_slow"] = "None"
        self.assertEqual(Path(self.preview(values, 2)["folder"]).name, "sweep_1d")
        values["axis_slow"] = "Vbg"
        self.assertEqual(Path(self.preview(values, 2)["folder"]).name, "map_2d")

    def test_photocurrent_enabled_conditions_and_timestamp_placement(self):
        values = defaults(3)
        row = row_default("bias_conditions", 3)
        values["bias_conditions"] = [dict(row, vtg=1), dict(row, enabled=False, vtg=99),
                                      dict(row, vtg=2), dict(row, vtg=3), dict(row, vtg=4)]
        values["use_vds"] = False
        result = self.preview(values, 3)
        self.assertEqual(result["csvCount"], 4)
        self.assertEqual(len(result["csvNames"]), 3)
        for index, name in enumerate(result["csvNames"], 1):
            self.assertIn(f"_C{index:02d}_Vtg{index}V", name)
            self.assertIn("_VdsOff_{timestamp}.csv", name)
            self.assertNotIn("Vtg99", name)
        values["bias_conditions"] = [dict(row, enabled=False)]
        with self.assertRaisesRegex(ValueError, "condition"):
            self.preview(values, 3)

    def test_field_gate_batch_condition_field_product_and_fallback(self):
        values = defaults(4)
        row = row_default("conditions", 4)
        values["fields_t"] = "0, 1"
        values["conditions"] = [dict(row, name="A", base_name="recipe_A"),
                                dict(row, name="disabled", enabled=False, base_name="skip"),
                                dict(row, name="B", base_name="recipe_B")]
        result = self.preview(values, 4)
        self.assertEqual(result["csvCount"], 4)
        self.assertEqual(len(result["csvNames"]), 3)
        self.assertIn("recipe_A", result["csvNames"][0])
        self.assertIn("B_0T", result["csvNames"][0])
        self.assertIn("B_1T", result["csvNames"][1])
        self.assertIn("recipe_B", result["csvNames"][2])
        self.assertTrue(all("skip" not in name for name in result["csvNames"]))
        values["conditions"] = []
        self.assertEqual(self.preview(values, 4)["csvCount"], 2)
        values["conditions"] = [dict(row, enabled=False)]
        with self.assertRaisesRegex(ValueError, "condition"):
            self.preview(values, 4)

    def test_transport_only_enabled_conditions_use_shared_ratio(self):
        values = defaults(5)
        row = row_default("conditions", 5)
        values.update(ratio=2.5, ratio_target="Vtg")
        values["conditions"] = [dict(row, name="first", ratio=7),
                                dict(row, name="skip", enabled=False),
                                dict(row, name="last", ratio=9)]
        result = self.preview(values, 5)
        self.assertEqual(result["csvCount"], 2)
        self.assertIn("C01_first", result["csvNames"][0])
        self.assertIn("C02_last", result["csvNames"][1])
        self.assertTrue(all("rVtg2.5" in name for name in result["csvNames"]))
        self.assertIn("series_manifest.json", result["metadata"])
        self.assertIn("checkpoint", result["note"])
        values["conditions"] = [dict(row, enabled=False)]
        with self.assertRaises(ValueError):
            self.preview(values, 5)
