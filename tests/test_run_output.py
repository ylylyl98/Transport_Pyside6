import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.models import SaveRoot, LineSweepParams
from app.run_output import (build_planned_output, gate_scan_filename_parts,
                            compose_output_stem, unique_planned_output)
from app.signal_chain import SignalChainSnapshot


class RunOutputTests(unittest.TestCase):
    def test_gate_filename_preserves_main_recipe_and_field_tags(self):
        params = LineSweepParams(mode="Derived", derived_start=20, derived_stop=27,
                                 derived_fixed=7, derived_ratio=1.15, derived_vds_fixed=-0.1)
        parts = gate_scan_filename_parts(params, SignalChainSnapshot(433, 0.1, 200e-9), 2)
        self.assertEqual(compose_output_stem("YZ289", "gate_scan", "3.6K_e1_-0.1V2mV433Hz", parts,
                                             "20260906_143025"),
                         "YZ289_3.6K_e1_-0.1V2mV433Hz_Doping20to27_E7_Vds-0.1V_rVbg1.15_Fwd_"
                         "433Hz_LIA100mV_Pre200nA_B_2T_20260906_143025")

    def test_collision_suffix_and_frozen_preview(self):
        with tempfile.TemporaryDirectory() as folder, patch("app.run_output.new_run_id", return_value="20260906_143025"):
            save = SaveRoot(base=folder, device_id="sample")
            first = build_planned_output(save, "gate_scan", "test", create_dir=True)
            self.assertTrue(first.csv_path.endswith("20260906_143025.csv"))
            Path(first.csv_path).write_text("original", encoding="utf-8")
            second = build_planned_output(save, "gate_scan", "test")
            self.assertTrue(second.csv_path.endswith("20260906_143025_02.csv"))
            Path(second.metadata_path).write_text("{}", encoding="utf-8")
            third = build_planned_output(save, "gate_scan", "test")
            self.assertTrue(third.csv_path.endswith("20260906_143025_03.csv"))
            frozen = build_planned_output(save, "gate_scan", "test", run_id=second.run_id, freeze=True)
            self.assertEqual(frozen, second)
            self.assertEqual(Path(first.csv_path).read_text(encoding="utf-8"), "original")

    def test_related_condition_file_and_reserved_recipe_collision(self):
        with tempfile.TemporaryDirectory() as folder:
            planned = build_planned_output(SaveRoot(base=folder), "photocurrent", "test",
                                           run_id="20260906_143025", create_dir=True)
            # Photocurrent bias CSVs put their tags before the timestamp.
            path = os.path.join(planned.output_dir, planned.display_stem + "_Vtg_1V_" + planned.run_id + ".csv")
            Path(path).touch()
            second = unique_planned_output(planned)
            self.assertTrue(second.run_id.endswith("_02"))
            third = unique_planned_output(planned, [second.csv_path])
            self.assertTrue(third.run_id.endswith("_03"))
