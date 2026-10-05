"""Derived Drag/Drive comparisons use saved currents and preserve gaps."""
import tempfile
import unittest
from pathlib import Path

import numpy as np

from app.curve_history import load_curve
from app.curve_map import load_map


class AnalysisSignalTests(unittest.TestCase):
    def setUp(self):
        self.scratch = tempfile.TemporaryDirectory()
        self.addCleanup(self.scratch.cleanup)
        self.path = Path(self.scratch.name) / "scan.csv"

    def test_component_ratios_preserve_signs_and_leave_invalid_denominators_as_gaps(self):
        self.path.write_text("Vds,I_drag_X,I_drive_X,I_drag_Y,I_drive_Y,Direction\nV,A,A,A,A,\n"
                             "0,-2,4,3,6,forward\n1,2,0,4,-2,forward\n2,nan,2,5,0,backward\n")
        before = self.path.read_bytes()
        for signal, expected in (("Drag ratio X", [-.5, np.nan, np.nan]),
                                  ("Drag ratio Y", [.5, -2, np.nan])):
            with self.subTest(signal=signal):
                curve = load_curve(self.path, "Vds", signal)
                np.testing.assert_allclose(curve.y, expected, equal_nan=True)
                self.assertEqual(curve.y_unit, "")
                self.assertEqual(curve.directions, ("forward", "forward", "backward"))
                self.assertTrue(curve.warnings)
        self.assertEqual(self.path.read_bytes(), before)

    def test_magnitude_ratio_uses_saved_r_or_falls_back_to_xy(self):
        for columns, units, values in (("I_drag_R,I_drive_R", "A,A", "5,10"),
                                       ("I_drag_X,I_drag_Y,I_drive_X,I_drive_Y", "A,A,A,A", "3,4,6,8")):
            self.path.write_text(f"Vds,{columns}\nV,{units}\n0,{values}\n")
            np.testing.assert_allclose(load_curve(self.path, "Vds", "Drag ratio R").y, [.5])

    def test_ratios_reconcile_current_units_and_reject_incompatible_quantities(self):
        self.path.write_text("Vds,I_drag_X,I_drive_X\nV,nA,A\n0,2,4e-9\n")
        np.testing.assert_allclose(load_curve(self.path, "Vds", "Drag ratio X").y, [.5])
        self.path.write_text("Vds,I_drag_X,I_drive_X\nV,V,A\n0,2,4\n")
        with self.assertRaisesRegex(ValueError, "current units"):
            load_curve(self.path, "Vds", "Drag ratio X")

    def test_map_and_cuts_can_use_ratio_signal(self):
        self.path.write_text("Vtg,Vbg,I_drag_X,I_drive_X,PassIndex\nV,V,A,A,#\n"
                             "0,10,1,2,0\n1,10,2,8,0\n0,20,3,0,1\n1,20,4,2,1\n")
        result = load_map(self.path, "Vtg", "Vbg", "Drag ratio X")
        np.testing.assert_allclose(result.z, [[.5, .25], [np.nan, 2.]], equal_nan=True)
        self.assertEqual(result.signal_unit, "")


if __name__ == "__main__":
    unittest.main()
