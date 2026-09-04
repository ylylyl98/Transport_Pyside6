import unittest

from app.gate_scan_summary import format_gate_scan_condition
from app.models import LineSweepParams


class GateScanSummaryTests(unittest.TestCase):
    def test_raw_summary_includes_trajectory_fixed_values_and_equivalents(self):
        params = LineSweepParams(
            mode="Raw",
            raw_vtg_active=True, raw_vtg_start=0.0, raw_vtg_stop=1.0,
            raw_vbg_active=False, raw_vbg_start=0.25,
            raw_vds_active=False, raw_vds_start=-0.5,
            derived_ratio=2.0, n_points=11,
        )
        text = format_gate_scan_condition(params)
        self.assertIn("RAW · sweep Vtg", text)
        self.assertIn("Vtg: 0.000 → 1.000 V (0.100 V/pt)", text)
        self.assertIn("Vbg: 0.250 V (fixed)", text)
        self.assertIn("Vds: -0.500 V (fixed)", text)
        self.assertIn("Doping:", text)
        self.assertIn("E-field:", text)

    def test_derived_summary_includes_gate_equivalents_and_details(self):
        params = LineSweepParams(
            mode="Derived", derived_axis="Doping", derived_start=2.0,
            derived_stop=4.0, derived_fixed=0.5, derived_ratio=2.0,
            derived_vds_mode="Fixed", derived_vds_fixed=-0.2, n_points=5,
        )
        text = format_gate_scan_condition(params, details=True)
        self.assertIn("DERIVED · sweep Doping", text)
        self.assertIn("Doping: 2.000 → 4.000 (0.500/pt)", text)
        self.assertIn("E-field: 0.500 (fixed)", text)
        self.assertIn("Vtg: 1.250 → 2.250 V", text)
        self.assertIn("Vbg: 0.375 → 0.875 V", text)
        self.assertIn("Points: 5", text)

    def test_missing_conversion_is_explicit_and_does_not_mutate_recipe(self):
        params = LineSweepParams(
            mode="Raw", raw_vtg_start=0.0, raw_vtg_stop=1.0,
            raw_vbg_start=0.0, derived_ratio=0.0,
        )
        before = params.derived_ratio
        text = format_gate_scan_condition(params)
        # Raw gate values can still be converted to Doping/E-field when the
        # ratio is zero; only the inverse derived-to-gate conversion needs r.
        self.assertIn("Doping: 0.000 → 1.000", text)
        self.assertIn("E-field: 0.000 → 1.000", text)
        self.assertEqual(params.derived_ratio, before)

    def test_derived_zero_ratio_reports_missing_inverse_configuration(self):
        params = LineSweepParams(mode="Derived", derived_ratio=0.0)
        text = format_gate_scan_condition(params)
        self.assertIn("Not configured", text)

    def test_one_point_and_bidirectional_totals_match_acquisition(self):
        one = LineSweepParams(mode="Raw", n_points=1, raw_vtg_start=0.25, raw_vtg_stop=1.25)
        one_text = format_gate_scan_condition(one, details=True)
        self.assertIn("Points: 1", one_text)
        self.assertIn("Vtg: 0.250 V (1 point)", one_text)
        self.assertNotIn("0.250 → 1.250", one_text)

        both = LineSweepParams(mode="Raw", n_points=3, sweep_both_ways=True)
        both_text = format_gate_scan_condition(both, details=True)
        self.assertIn("Points: 6 total (3 each direction)", both_text)


if __name__ == "__main__":
    unittest.main()
