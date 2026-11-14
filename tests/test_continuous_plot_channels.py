import unittest

from app.result_channels import B_FIELD_DIAGNOSTIC_CHANNELS, plot_channel_options


class ContinuousPlotChannelsTests(unittest.TestCase):
    def test_diagnostics_are_limited_to_b_field_and_dual_raw_to_dual_mode(self):
        ordinary = plot_channel_options('Keithley 2400')
        field = plot_channel_options('Keithley 2400', b_field_diagnostics=True)
        self.assertTrue(set(B_FIELD_DIAGNOSTIC_CHANNELS).issubset(field))
        self.assertTrue(set(B_FIELD_DIAGNOSTIC_CHANNELS).isdisjoint(ordinary))
        self.assertIn('Ids_Keithley', field)
        self.assertNotIn('raw_drive_X', field)
        dual = plot_channel_options('None', {'drag_drive': {'mode': 'dual'}},
                                    bias2=True, b_field_diagnostics=True)
        self.assertTrue({'raw_drive_X', 'raw_drive_Y', 'I_drive_X',
                         'I_drag_X', 'Ibias2_Keithley'}.issubset(dual))


if __name__ == '__main__':
    unittest.main()
