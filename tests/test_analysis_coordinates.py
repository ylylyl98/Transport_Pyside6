import math
import unittest
from app.analysis_coordinates import coordinate_label, coordinate_options

class AnalysisCoordinateTests(unittest.TestCase):
    def test_close_doubles_are_distinguishable_without_changing_values(self):
        values = [0.12345678912345678, math.nextafter(0.12345678912345678, math.inf), 1e-15, -2e9]
        options = coordinate_options(values)
        self.assertEqual([item['value'] for item in options], values)
        self.assertEqual(len({item['label'] for item in options}), len(values))
        self.assertEqual([item['exact'] for item in options], [repr(v) for v in values])
        self.assertEqual(coordinate_label(values, values[1]), options[1]['label'])

    def test_simple_numbers_stay_compact_and_duplicates_keep_same_label(self):
        options = coordinate_options([1., .000005, 1., -3.25])
        self.assertEqual([entry['label'] for entry in options], ['1', '5e-6', '1', '-3.25'])
        self.assertEqual(coordinate_options([]), [])

    def test_invalid_and_absent_coordinates_are_rejected(self):
        for value in (math.nan, math.inf, -math.inf):
            with self.subTest(value=value), self.assertRaises(ValueError):
                coordinate_options([1., value])
        with self.assertRaises(ValueError):
            coordinate_label([1., 2.], 1.00000001)
