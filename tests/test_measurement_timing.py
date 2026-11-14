import unittest
from copy import deepcopy
from app.quick.parameters import defaults, row_default
from app.quick.measurement_timing import estimate_measurement_time


class MeasurementTimingTests(unittest.TestCase):
    def estimate(self, mode, values=None):
        return estimate_measurement_time(mode, values if values is not None else defaults(mode))

    def test_all_modes_have_estimates(self):
        for mode in range(6):
            with self.subTest(mode=mode):
                result = self.estimate(mode)
                self.assertIsNotNone(result['seconds'], result['note'])
                self.assertGreater(result['seconds'], 0)
                self.assertGreater(result['points'], 0)

    def test_line_roundtrip_and_averages(self):
        for mode in (0, 1):
            v = defaults(mode)
            one = self.estimate(mode, v)
            v['sweep_both_ways'] = True
            reverse = self.estimate(mode, v)
            self.assertEqual(reverse['points'], 2*one['points'])
            self.assertGreater(reverse['seconds'], one['seconds'])
            v['n_sample'] += 1
            self.assertGreater(self.estimate(mode, v)['seconds'], reverse['seconds'])

    def test_map_and_photocurrent_scaling(self):
        for mode in (2, 3):
            v = defaults(mode)
            baseline = self.estimate(mode, v)
            v['delay'] += 1
            self.assertAlmostEqual(self.estimate(mode, v)['seconds']-baseline['seconds'], baseline['points'])
        v = defaults(3)
        v['bias_conditions'].append(deepcopy(v['bias_conditions'][0]))
        self.assertEqual(self.estimate(3, v)['points'], 2*self.estimate(3)['points'])
        for row in v['bias_conditions']: row['enabled'] = False
        self.assertIsNone(self.estimate(3, v)['seconds'])

    def test_field_gate_recipes_and_resume(self):
        v = defaults(4)
        a = row_default('conditions', 4)
        b = deepcopy(a); b['n_points'] = 10
        v['fields_t'] = '0, 1'
        v['conditions'] = [a, b]
        self.assertEqual(self.estimate(4, v)['points'], 2*(a['n_points']+10))
        b['enabled'] = False
        self.assertEqual(self.estimate(4, v)['points'], 2*a['n_points'])
        a['enabled'] = False
        self.assertIsNone(self.estimate(4, v)['seconds'])
        v['resume_checkpoint'] = 'saved.json'
        self.assertIsNone(self.estimate(4, v)['seconds'])

    def test_field_roundtrip_hysteresis_and_conditions(self):
        v = defaults(5); v['round_trip'] = False
        one = self.estimate(5, v)
        v['round_trip'] = True; v['field_approach'] = 'linear_no_backward'
        reverse = self.estimate(5, v)
        self.assertEqual(reverse['points'], 2*one['points']-1)
        self.assertGreater(reverse['seconds'], one['seconds'])
        v['conditions'].append(deepcopy(v['conditions'][0]))
        v['conditions'][-1]['name'] = 'Con2'
        self.assertEqual(self.estimate(5, v)['points'], 2*reverse['points'])
        for row in v['conditions']: row['enabled'] = False
        self.assertIsNone(self.estimate(5, v)['seconds'])

    def test_field_stability_windows_and_reversal(self):
        v = defaults(5); v['round_trip'] = False
        v['field_spacing'] = 'points'; v['field_points'] = 3
        v['temperature_window_s'] = 10.; v['field_stability_s'] = 2.
        base = self.estimate(5, v)
        v['field_stability_s'] = 12.
        self.assertAlmostEqual(self.estimate(5, v)['seconds'] - base['seconds'], 3*2)
        v['field_stability_s'] = 2.
        v['sample_reversal_mode'] = 'per_point'; v['sample_reversal_settle_s'] = 3.
        reversal = self.estimate(5, v)
        # Per point: OFF and ON waits plus another complete qualification;
        # one initial switch and one cleanup restoration.
        self.assertAlmostEqual(reversal['seconds'] - base['seconds'], 3*(6+11)+6)
        self.assertEqual(reversal['points'], base['points'])
        self.assertEqual(reversal['cleanup_seconds'], 3.)
        v['sample_reversal_mode'] = 'whole_sweep'
        self.assertAlmostEqual(self.estimate(5, v)['seconds'] - base['seconds'], 6.)

    def test_field_gate_rate_and_cleanup_reserve(self):
        v = defaults(4); v['fields_t'] = '0, 1, -1'
        v['field_rate_t_per_min'] = 1.
        slow = self.estimate(4, v)
        v['field_rate_t_per_min'] = 2.
        fast = self.estimate(4, v)
        self.assertAlmostEqual(slow['seconds'] - fast['seconds'], 90.)
        v = defaults(1); v['raw_vtg_start'] = v['raw_vtg_stop'] = 1.
        self.assertGreater(self.estimate(1, v)['cleanup_seconds'], 0)
        v = defaults(2); v['vtg_stop'] = 1.
        self.assertGreater(self.estimate(2, v)['cleanup_seconds'], 0)

    def test_invalid_and_oversized_are_unavailable(self):
        for mode, key in ((0,'vds_step'),(1,'n_points'),(2,'vtg_step'),(3,'wl_step'),(5,'rate_t_per_min')):
            v = defaults(mode); v[key] = 0
            self.assertIsNone(self.estimate(mode, v)['seconds'], mode)
        v = defaults(0); v['vds_step'] = 1e-10
        self.assertIsNone(self.estimate(0, v)['seconds'])


if __name__ == '__main__': unittest.main()
