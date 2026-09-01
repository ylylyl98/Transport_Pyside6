import unittest

from app.signal_chain import (
    current_from_lockin_daq_voltage,
    preamp_gain_v_per_a,
    sr830_xy_output_gain,
)


class SignalChainTests(unittest.TestCase):
    def test_sr830_xy_output_gain_uses_ten_volts_full_scale(self):
        cases = (
            (1e-3, 1e4),
            (1e-2, 1e3),
            (1e-1, 1e2),
            (1.0, 10.0),
        )
        for sensitivity_v, expected_gain in cases:
            with self.subTest(sensitivity_v=sensitivity_v):
                self.assertAlmostEqual(sr830_xy_output_gain(sensitivity_v), expected_gain)

    def test_current_from_lockin_daq_voltage(self):
        preamp_sensitivity_a = 1e-7
        lockin_sensitivity_v = 1e-3
        daq_voltage_v = 1.0

        self.assertAlmostEqual(preamp_gain_v_per_a(preamp_sensitivity_a), 1e7)
        self.assertAlmostEqual(sr830_xy_output_gain(lockin_sensitivity_v), 1e4)
        self.assertAlmostEqual(
            current_from_lockin_daq_voltage(
                daq_voltage_v,
                preamp_sensitivity_a,
                lockin_sensitivity_v,
            ),
            1e-11,
        )

    def test_nonpositive_values_are_rejected(self):
        with self.assertRaises(ValueError):
            preamp_gain_v_per_a(0.0)
        with self.assertRaises(ValueError):
            sr830_xy_output_gain(0.0)


if __name__ == "__main__":
    unittest.main()
