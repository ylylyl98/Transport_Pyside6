import unittest
import numpy as np

from app.map_view import visible_clim


class MapViewTests(unittest.TestCase):
    def test_visible_bounds_ignore_outliers_outside_view_and_nonfinite_cells(self):
        data = {'x': [-1, 0, 1], 'y': [-2, 0, 2],
                'z': [[-100, 500, 900], [1, np.nan, 300], [2, np.inf, 700]]}
        self.assertEqual(visible_clim(data, [0, -1], [2, 0]), (1, 2))
        self.assertIsNone(visible_clim(data, [.1, .2], [0, 2]))
        self.assertIsNone(visible_clim(data, [0, 0], [0, 2]))

    def test_constant_tiny_values_get_a_finite_nonzero_display_interval(self):
        low, high = visible_clim({'x': [1], 'y': [2], 'z': [[3e-12]]})
        self.assertLess(low, 3e-12)
        self.assertGreater(high, 3e-12)
        self.assertLess(high, 4e-12)


if __name__ == '__main__':
    unittest.main()
