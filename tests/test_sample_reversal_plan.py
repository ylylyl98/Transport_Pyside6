import unittest
from app.models import BFieldTransportParams
from app.engine.bfield_transport_sweep import BFieldTransportSafetyError, TransportSweepPlan
from app.quick.parameters import defaults, build_params, convert, measurement_sections

class SampleReversalPlanTests(unittest.TestCase):
    def test_modes_and_backend_validation(self):
        for mode in ('unchanged', 'per_point', 'whole_sweep'):
            p = BFieldTransportParams(measurement_mode='stepped', sample_reversal_mode=mode)
            self.assertEqual(TransportSweepPlan.from_params(p).params.sample_reversal_mode, mode)
        for mode in ('per_point', 'whole_sweep'):
            for backend, trajectory in (('1000', 'continuous'), ('2100', 'stepped')):
                with self.assertRaises(BFieldTransportSafetyError):
                    TransportSweepPlan.from_params(BFieldTransportParams(magnet_backend=backend, measurement_mode=trajectory, sample_reversal_mode=mode))
        for value in (-1, float('nan'), float('inf')):
            with self.assertRaises(BFieldTransportSafetyError):
                TransportSweepPlan.from_params(BFieldTransportParams(sample_reversal_settle_s=value))
        with self.assertRaises(BFieldTransportSafetyError):
            TransportSweepPlan.from_params(BFieldTransportParams(sample_reversal_mode='bad'))
    def test_quick_recipe_round_trip(self):
        values = defaults(5)
        values['measurement_mode'] = 'stepped'
        values['sample_reversal_mode'] = convert('whole_sweep', 'unchanged', 'sample_reversal_mode', 5)
        values['sample_reversal_settle_s'] = 4.5
        params = build_params(values, 5)
        self.assertEqual((params.sample_reversal_mode, params.sample_reversal_settle_s), ('whole_sweep', 4.5))
        self.assertEqual(TransportSweepPlan.from_params(params).params.sample_reversal_mode, 'whole_sweep')
        self.assertTrue(any(s.get('title') == 'Sample B current reversal' for s in measurement_sections(values, 5)))

if __name__ == '__main__': unittest.main()
