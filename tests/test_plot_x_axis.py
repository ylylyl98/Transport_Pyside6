from __future__ import annotations

import unittest

from app.gate_transform import RATIO_TARGET_VBG, RATIO_TARGET_VTG
from app.plot_x_axis import (
    FOLLOW_SWEEP,
    STEP_INDEX,
    normalize_plot_x_selection,
    plot_x_axis_label,
    record_x_value,
    resolve_gate_scan_x_axis,
    resolve_map_x_axis,
)


class PlotXAxisTests(unittest.TestCase):
    def test_legacy_auto_migrates_to_follow_sweep(self):
        self.assertEqual(normalize_plot_x_selection("Auto"), FOLLOW_SWEEP)

    def test_gate_scan_follow_sweep_tracks_derived_axis(self):
        for derived_axis in ("Doping", "E-field"):
            with self.subTest(derived_axis=derived_axis):
                resolved = resolve_gate_scan_x_axis(
                    FOLLOW_SWEEP,
                    "Derived",
                    derived_axis,
                    True,
                    True,
                    False,
                )
                self.assertEqual(resolved, derived_axis)

    def test_raw_follow_uses_single_axis_or_step_index_for_multiple_axes(self):
        self.assertEqual(
            resolve_gate_scan_x_axis(FOLLOW_SWEEP, "Raw", "Doping", True, False, False),
            "Vtg",
        )
        self.assertEqual(
            resolve_gate_scan_x_axis(FOLLOW_SWEEP, "Raw", "Doping", True, True, False),
            STEP_INDEX,
        )

    def test_manual_override_wins_and_map_follows_fast_axis(self):
        self.assertEqual(
            resolve_gate_scan_x_axis("Vbg", "Derived", "Doping", True, False, False),
            "Vbg",
        )
        self.assertEqual(resolve_map_x_axis(FOLLOW_SWEEP, "Vds"), "Vds")
        self.assertEqual(resolve_map_x_axis("E-field", "Vds"), "E-field")

    def test_record_values_and_formula_labels(self):
        record = {
            "index": 7,
            "vtg": 1.0,
            "vbg": 2.0,
            "vds": 3.0,
            "doping": 5.0,
            "efield": 1.0,
        }
        self.assertEqual(record_x_value(record, STEP_INDEX), 7.0)
        self.assertEqual(record_x_value(record, "Doping"), 5.0)
        self.assertEqual(
            plot_x_axis_label("Doping", 2.0, RATIO_TARGET_VTG),
            "Doping (2.00*Vtg + Vbg) (V)",
        )
        self.assertEqual(
            plot_x_axis_label("E-field", 2.0, RATIO_TARGET_VBG),
            "E-field (Vtg - 2.00*Vbg) (V)",
        )


if __name__ == "__main__":
    unittest.main()
