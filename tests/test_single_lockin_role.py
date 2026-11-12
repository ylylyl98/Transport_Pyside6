import math
import unittest
from types import SimpleNamespace
from app.drag_drive import (acquire_current_sample, acquisition_columns, measurement_mode,
                            verify_measurement_calibration)
from app.result_channels import plot_channel_options
from app.signal_chain import sample_ac_voltage_estimate


class Daq:
    ai_channel_indexes = [0, 1, 2]
    def acquire(self):
        return {'ai0': 1., 'ai1': -2., 'ai2': 3.}


class SingleRoleTests(unittest.TestCase):
    def test_single_drive_uses_drive_calibration_and_does_not_fabricate_drag(self):
        chain = {'drag_drive': {'mode': 'single_drive', 'verified': True,
                 'drive_sensitivity_v': .1, 'drive_preamp_gain': 10.,
                 'series_resistance_ohm': 1000., 'dc_enabled': False}}
        values = acquire_current_sample(Daq(), 1, 1e7, 100., chain)
        self.assertAlmostEqual(values['I_drive_X'], 1e-6)
        self.assertAlmostEqual(values['I_drive_Y'], -2e-6)
        self.assertTrue(math.isnan(values['Ids_X']))
        self.assertTrue(math.isnan(values['Ids_DC']))
        self.assertNotIn('I_drag_X', values)
        self.assertEqual(acquisition_columns(chain), ('I_drive_X', 'I_drive_Y', 'I_drive_R'))
        self.assertNotIn('Ids_X', plot_channel_options('', chain))
        self.assertNotIn('Ids_DC', plot_channel_options('', chain))

    def test_sequence_daq_uses_configured_channel_order(self):
        class SequenceDaq:
            ai_channel_indexes = [2, 0, 1]
            def acquire(self):
                return [3., 1., -2.]
        result = acquire_current_sample(SequenceDaq(), 1, 1., 1.)
        self.assertEqual((result['raw_X'], result['raw_Y'], result['raw_DC']), (1., -2., 3.))

    def test_single_drag_dc_has_independent_gain(self):
        chain = {'drag_drive': {'mode': 'single_drag', 'verified': True,
                 'drag_sensitivity_v': .1, 'dc_preamp_gain_v_per_a': 3.}}
        result = acquire_current_sample(Daq(), 1, 1e6, 2., chain)
        self.assertAlmostEqual(result['I_drag_X'], 1e-8)
        self.assertEqual(result['Ids_DC'], 1.)
        self.assertNotIn('I_drive_X', result)

    def test_legacy_modes_and_excitation(self):
        self.assertEqual(measurement_mode({'drag_drive': {'enabled': True}}), 'dual')
        self.assertEqual(measurement_mode({}), 'ordinary')
        meta = {'excitation_source': 'external', 'ac_voltage_ratio': .1,
                'lockin_settings': {'values': {'sine_out_v': 1.}}}
        self.assertIsNone(sample_ac_voltage_estimate(meta))
        meta['excitation_source'] = 'primary'
        self.assertEqual(sample_ac_voltage_estimate(meta), .1)

    def test_single_drive_validates_only_primary_and_no_external_reference_requirement(self):
        lockin = SimpleNamespace(model='SR850', read_analog_output_settings=lambda: {
            'ch1': 'X', 'ch2': 'Y', 'x_offset': 0, 'y_offset': 0, 'x_expand': 1, 'y_expand': 1})
        manager = SimpleNamespace(get_session=lambda name: Daq() if name == 'daq' else lockin)
        settings = {'lockin': {'values': {'sensitivity': 20, 'input_config': 1, 'ref_source': 0}}}
        result = verify_measurement_calibration({'mode': 'single_drive', 'series_resistance_ohm': 1000}, manager, settings)
        self.assertTrue(result['verified'])
        self.assertEqual(result['channel_map']['drive_x'], 'ai0')
