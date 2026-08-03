from __future__ import annotations

import unittest

from app.gate_transform import (
    RATIO_TARGET_VBG,
    RATIO_TARGET_VTG,
    derived_to_gates,
    gates_to_derived,
)
from app.models import Connections, LineSweepParams, SaveRoot
from app.workers.line_sweep import LineSweepWorker


class GateTransformTests(unittest.TestCase):
    def test_forward_inverse_round_trip_for_both_ratio_targets(self):
        for target in (RATIO_TARGET_VBG, RATIO_TARGET_VTG):
            for ratio in (2.5, -1.25):
                with self.subTest(target=target, ratio=ratio):
                    doping, efield = gates_to_derived(1.2, -0.4, ratio, target)
                    vtg, vbg = derived_to_gates(doping, efield, ratio, target)
                    self.assertAlmostEqual(vtg, 1.2)
                    self.assertAlmostEqual(vbg, -0.4)

    def test_ratio_one_is_equivalent_for_both_targets(self):
        expected = gates_to_derived(0.75, -0.25, 1.0, RATIO_TARGET_VBG)
        actual = gates_to_derived(0.75, -0.25, 1.0, RATIO_TARGET_VTG)
        self.assertEqual(expected, actual)

    def test_inverse_rejects_zero_ratio(self):
        for target in (RATIO_TARGET_VBG, RATIO_TARGET_VTG):
            with self.subTest(target=target):
                with self.assertRaisesRegex(ValueError, "non-zero ratio"):
                    derived_to_gates(1.0, 0.0, 0.0, target)


class LineSweepTrajectoryTests(unittest.TestCase):
    @staticmethod
    def _worker(params: LineSweepParams) -> LineSweepWorker:
        return LineSweepWorker(params, SaveRoot(), Connections())

    def test_doping_and_efield_sweeps_work_for_both_ratio_targets(self):
        for target in (RATIO_TARGET_VBG, RATIO_TARGET_VTG):
            for axis in ("Doping", "E-field"):
                with self.subTest(target=target, axis=axis):
                    params = LineSweepParams(
                        mode="Derived",
                        derived_ratio=2.0,
                        derived_ratio_target=target,
                        derived_axis=axis,
                        derived_start=-1.0,
                        derived_stop=1.0,
                        derived_fixed=0.25,
                        derived_vds_mode="Swept",
                        derived_vds_start=-0.1,
                        derived_vds_stop=0.3,
                        n_points=5,
                    )
                    points = self._worker(params)._build_derived_trajectory()
                    self.assertEqual(len(points), 5)
                    self.assertAlmostEqual(points[0]["vds"], -0.1)
                    self.assertAlmostEqual(points[-1]["vds"], 0.3)

                    for point in points:
                        doping, efield = gates_to_derived(
                            point["vtg"],
                            point["vbg"],
                            params.derived_ratio,
                            target,
                        )
                        self.assertAlmostEqual(doping, point["doping"])
                        self.assertAlmostEqual(efield, point["efield"])

                    swept_values = [point["doping" if axis == "Doping" else "efield"] for point in points]
                    fixed_values = [point["efield" if axis == "Doping" else "doping"] for point in points]
                    self.assertEqual(swept_values, [-1.0, -0.5, 0.0, 0.5, 1.0])
                    self.assertTrue(all(abs(value - 0.25) < 1e-12 for value in fixed_values))

    def test_raw_trajectory_uses_selected_ratio_target_for_saved_columns(self):
        params = LineSweepParams(
            mode="Raw",
            raw_vtg_active=True,
            raw_vtg_start=1.0,
            raw_vtg_stop=1.0,
            raw_vbg_active=False,
            raw_vbg_start=2.0,
            derived_ratio=3.0,
            derived_ratio_target=RATIO_TARGET_VTG,
            n_points=1,
        )
        point = self._worker(params)._build_raw_trajectory()[0]
        self.assertAlmostEqual(point["doping"], 5.0)
        self.assertAlmostEqual(point["efield"], 1.0)

    def test_physical_voltage_limits_are_checked_for_each_ratio_target(self):
        cases = (
            (RATIO_TARGET_VTG, 10.0, 10.0),
            (RATIO_TARGET_VBG, 10.0, -10.0),
        )
        for target, doping, efield in cases:
            with self.subTest(target=target):
                params = LineSweepParams(
                    mode="Derived",
                    derived_ratio=0.1,
                    derived_ratio_target=target,
                    derived_axis="Doping",
                    derived_start=doping,
                    derived_stop=doping,
                    derived_fixed=efield,
                    n_points=1,
                )
                worker = self._worker(params)
                trajectory = worker._build_derived_trajectory()
                with self.assertRaisesRegex(RuntimeError, "exceeds limit"):
                    worker._validate_trajectory_limits(trajectory)


if __name__ == "__main__":
    unittest.main()
