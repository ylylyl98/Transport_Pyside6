"""Normalization must preserve raw snapshots, signs and missing-data gaps."""
import unittest
from pathlib import Path

import numpy as np

from app.curve_history import CurveTrace
from app.curve_normalization import normalize_traces


def trace(values, x=None):
    values = np.asarray(values, dtype=float)
    return CurveTrace(Path("scan.csv"), "scan", "forward",
                      np.arange(len(values), dtype=float) if x is None else np.asarray(x, dtype=float),
                      values, "V", "A")


class CurveNormalizationTests(unittest.TestCase):
    def test_max_abs_preserves_sign_and_ignores_nonfinite_coordinates(self):
        original = trace([-4., 2., np.nan, np.inf, 1e6], [0., 1., 2., 3., np.nan])
        before = original.y.copy()
        normalized = normalize_traces([original], "max_abs")[0]
        np.testing.assert_allclose(normalized.y, [-1., .5, np.nan, np.nan, np.nan], equal_nan=True)
        np.testing.assert_array_equal(original.y, before)
        self.assertEqual(original.y_unit, "A")
        self.assertEqual(normalized.y_unit, "")

    def test_zero_and_constant_traces_have_defined_results(self):
        for mode, values, expected in (("max_abs", [0., 0.], [0., 0.]),
                                       ("max_abs", [-2., -2.], [-1., -1.]),
                                       ("min_max", [2., 2.], [0., 0.])):
            with self.subTest(mode=mode, values=values), np.errstate(all="raise"):
                np.testing.assert_allclose(normalize_traces([trace(values)], mode)[0].y, expected)

    def test_empty_or_all_missing_traces_stay_empty_or_missing(self):
        for mode in ("max_abs", "min_max"):
            result = normalize_traces([trace([]), trace([np.nan, np.inf])], mode)
            self.assertEqual(result[0].y.size, 0)
            self.assertTrue(np.isnan(result[1].y).all())

    def test_min_max_handles_extreme_finite_ranges_without_overflow(self):
        for values in ([-1e308, 0., 1e308], [-1e-300, 0., 1e-300]):
            with self.subTest(values=values), np.errstate(all="raise"):
                try:
                    result = normalize_traces([trace(values)], "min_max")[0]
                except FloatingPointError as exc:
                    self.fail(f"Finite measurement ranges must normalize without overflow: {exc}")
                np.testing.assert_allclose(result.y, [0., .5, 1.])

    def test_min_max_preserves_equal_increments_in_narrow_ranges(self):
        for low in (.3, 3e-6, -.3):
            middle = np.nextafter(low, np.inf)
            high = np.nextafter(middle, np.inf)
            with self.subTest(low=low), np.errstate(all="raise"):
                result = normalize_traces([trace([low, middle, high])], "min_max")[0]
                np.testing.assert_array_equal(result.y, [0., .5, 1.])

    def test_direction_traces_are_scaled_independently(self):
        first, second = trace([1., 2.]), trace([100., 400.])
        second.direction = "backward"
        result = normalize_traces([first, second], "max_abs")
        np.testing.assert_allclose(result[0].y, [.5, 1.])
        np.testing.assert_allclose(result[1].y, [.25, 1.])
        self.assertEqual(result[1].direction, "backward")

    def test_raw_mode_keeps_measurement_units_and_values(self):
        original = trace([-3e-9, 5e-9])
        result = normalize_traces([original], "raw")[0]
        np.testing.assert_array_equal(result.y, [-3e-9, 5e-9])
        self.assertEqual(result.y_unit, "A")

    def test_unknown_mode_is_rejected(self):
        with self.assertRaises(ValueError):
            normalize_traces([trace([1.])], "unknown")
