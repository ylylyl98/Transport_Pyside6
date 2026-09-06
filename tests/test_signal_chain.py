import unittest

from app.signal_chain import (
    current_from_lockin_daq_voltage,
    preamp_gain_v_per_a,
    sr830_xy_output_gain,
    SignalChainSnapshot,
)
from app.run_output import new_run_id
from unittest.mock import patch


class SignalChainTests(unittest.TestCase):
    def test_same_second_runs_have_distinct_ids(self):
        with patch("app.run_output.datetime") as clock:
            clock.datetime.now.return_value.strftime.return_value = "20260905_120000"
            ids = {new_run_id() for _ in range(100)}
        self.assertEqual(len(ids), 100)
        self.assertTrue(all(value.endswith("20260905_120000") for value in ids))

    def test_snapshot_and_conversion_agree_across_ranges(self):
        for sensitivity in (1e-3, 0.01, 0.1, 1):
            for preamp in (1e-9, 1e-7, 1e-5):
                snapshot = SignalChainSnapshot(lockin_sensitivity_v=sensitivity, preamp_sensitivity_a=preamp)
                # A fixed input current produces range-dependent DAQ voltage.
                current = 1e-12
                raw = current / preamp * 10 / sensitivity
                self.assertAlmostEqual(raw / (snapshot.preamp_gain_v_per_a * snapshot.lockin_scale) / current, 1)

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
