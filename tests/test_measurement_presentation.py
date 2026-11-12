import copy
import unittest

from app.quick.parameters import defaults, measurement_sections


class MeasurementPresentationTests(unittest.TestCase):
    def test_six_pages_keep_trajectory_and_bias_visible_without_mutating_recipe(self):
        for mode, title in enumerate(("Vds trajectory", "Gate trajectory", "Map trajectory",
                                      "Wavelength trajectory", "Magnetic field targets", "Magnetic field trajectory")):
            with self.subTest(mode=mode):
                values = defaults(mode)
                original = copy.deepcopy(values)
                sections = measurement_sections(values, mode)
                by_title = {section['title']: section for section in sections}
                self.assertFalse(by_title[title].get('collapsedByDefault', False))
                self.assertTrue(by_title['Acquisition']['collapsedByDefault'])
                self.assertTrue(by_title['Acquisition']['summary'])
                self.assertEqual(values, original)
                if mode < 4:
                    bias = by_title['Bias 2']
                    self.assertFalse(bias.get('collapsedByDefault', False))
                    self.assertIn('bias2_mode', [field['key'] for field in bias['fields']])

    def test_stepped_field_exposes_target_and_folds_stability(self):
        values = defaults(5)
        values['measurement_mode'] = 'stepped'
        sections = {s['title']: s for s in measurement_sections(values, 5)}
        primary = sections['Stepped field and temperature']
        self.assertFalse(primary.get('collapsedByDefault', False))
        self.assertIn('temperature_target_k', [f['key'] for f in primary['fields']])
        stability = sections['Temperature stability and timeout']
        self.assertTrue(stability['collapsedByDefault'])
        self.assertIn('temperature_timeout_s', [f['key'] for f in stability['fields']])
        self.assertTrue(stability['summary'])
        other = {s['title']: s for s in measurement_sections(defaults(0), 0)}
        self.assertTrue(other['Advanced ramps and conversion']['summary'])

    def test_collapsed_invalid_parameter_retains_error_and_full_value(self):
        values = defaults(0)
        values['delay'] = 0.123456789123
        errors = {('', -1, 'delay'): 'Invalid delay'}
        section = next(s for s in measurement_sections(values, 0, errors=errors) if s['title'] == 'Acquisition')
        delay = next(f for f in section['fields'] if f['key'] == 'delay')
        self.assertEqual(delay['error'], 'Invalid delay')
        self.assertEqual(delay['value'], 0.123456789123)
