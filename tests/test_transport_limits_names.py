import unittest
from unittest.mock import patch
from app.condition_names import unique_condition_name
from app.engine.bfield_transport_sweep import aps100_transport_field_limit, validate_field_bounds, BFieldTransportSafetyError
from utils.config import cfg

class TransportLimitsNamesTests(unittest.TestCase):
    def test_limits_and_configured_cap(self):
        with patch.object(cfg.magnet, 'safe_control_max_field_t', 8.0), patch.object(cfg.magnet, 'maximum_field_t', 9.0), patch.object(cfg.magnet, 'maximum_current_a', 44.27), patch.object(cfg.magnet, 'coil_constant_t_per_a', .20328):
            self.assertEqual(validate_field_bounds(-8, 8), (-8, 8))
            with self.assertRaises(BFieldTransportSafetyError): validate_field_bounds(0, 8.001)
            with patch.object(cfg.magnet, 'safe_control_max_field_t', 5.0):
                self.assertEqual(aps100_transport_field_limit(), 5.0)
                with self.assertRaises(BFieldTransportSafetyError): validate_field_bounds(0, 6)
            with patch.object(cfg.magnet, 'maximum_current_a', 20.0):
                self.assertAlmostEqual(aps100_transport_field_limit(), 20 * .20328)
    def test_unique_names(self):
        self.assertEqual(unique_condition_name(['Con1', 'Con3'], 'Con1 copy copy'), 'Con2')
        self.assertEqual(unique_condition_name(['Device', 'Device 2'], 'Device copy copy'), 'Device 3')
        self.assertEqual(unique_condition_name(['Con1', 'Con2']), 'Con3')
        self.assertEqual(unique_condition_name(['con1', 'Con3'], 'Con1'), 'Con2')
        self.assertEqual(unique_condition_name(['Device', 'Device 2'], 'Device 2'), 'Device 3')

    def test_invalid_configuration_and_explicit_caps_fail_closed(self):
        for value in (float('nan'), float('inf'), 0, -1):
            with self.subTest(value=value):
                with self.assertRaises(BFieldTransportSafetyError):
                    validate_field_bounds(0, 1, max_field_t=value)
        with patch.object(cfg.magnet, 'maximum_current_a', -40), patch.object(cfg.magnet, 'coil_constant_t_per_a', -.2):
            with self.assertRaises(BFieldTransportSafetyError): aps100_transport_field_limit()

    def test_quick_form_commits_eight_tesla_and_enforces_lower_commissioned_limit(self):
        from app.quick.parameters import defaults, build_params
        from app.engine.bfield_transport_sweep import TransportSweepPlan
        values = defaults(5)
        values.update(start_field_t=-8., stop_field_t=8.)
        plan = TransportSweepPlan.from_params(build_params(values, 5))
        self.assertEqual((plan.params.start_field_t, plan.params.stop_field_t), (-8., 8.))
        with patch.object(cfg.magnet, 'safe_control_max_field_t', 7.):
            with self.assertRaises(BFieldTransportSafetyError):
                TransportSweepPlan.from_params(build_params(values, 5))

if __name__ == '__main__': unittest.main()
