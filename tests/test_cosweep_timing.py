import unittest
from dataclasses import replace

from app.models import CoParams, Connections
from app.workers.cosweep_timing import estimate_cosweep_seconds


class CoSweepTimingTests(unittest.TestCase):
    def params(self, **changes):
        p = CoParams(coordinate_mode="Derived", axis_fast="Doping", axis_slow="Vds",
                     doping_start=0, doping_stop=1, doping_step=0.5,
                     efield_start=0, vds_start=0, vds_stop=1, vds_step=0.5,
                     delay=0, n_sample=1, vds_source="Keithley 2400")
        return replace(p, **changes)

    def test_delay_is_counted_once_for_each_grid_point(self):
        p = self.params()
        self.assertAlmostEqual(estimate_cosweep_seconds(replace(p, delay=2)) - estimate_cosweep_seconds(p), 18)

    def test_averages_add_daq_time_but_do_not_repeat_settling(self):
        p = self.params(delay=2)
        self.assertAlmostEqual(estimate_cosweep_seconds(replace(p, n_sample=4)) - estimate_cosweep_seconds(p), 0.27)

    def test_larger_gate_travel_takes_longer_for_the_same_point_count(self):
        p = self.params()
        self.assertGreater(estimate_cosweep_seconds(replace(p, doping_stop=10, doping_step=5)), estimate_cosweep_seconds(p))

    def test_daq_ramps_include_step_delays_and_zero_return(self):
        p = CoParams(axis_fast="Vds", axis_slow="None", vds_source="NI DAQ",
                     vtg_start=0, vtg_stop=0, vbg_start=0, vbg_stop=0,
                     vds_start=0, vds_stop=0.098, vds_step=0.098,
                     vds_ramp=0.05, delay=0, n_sample=1)
        # Two DAQ ramp steps at 10 ms, two cleanup steps at 50 ms;
        # two 10 ms samples, two file/plot updates and three ramp-start reads.
        self.assertAlmostEqual(estimate_cosweep_seconds(p, io_seconds=0), 0.19)

    def test_invalid_trajectory_is_not_reported_as_zero_duration(self):
        with self.assertRaises(ValueError):
            estimate_cosweep_seconds(self.params(vds_step=0))


class HistoricalCoSweepTimingTests(unittest.TestCase):
    def setUp(self):
        self.p = CoParams(coordinate_mode="Derived", axis_fast="Vds", axis_slow="Doping",
                          doping_start=-1, doping_stop=1, doping_step=0.2, efield_start=0,
                          vds_start=0.9, vds_stop=1.1, vds_step=0.002,
                          vg_ramp=0.1, vds_ramp=0.05, ratio=1, ratio_target="Vtg",
                          delay=0.4, n_sample=1, vds_source="Keithley 2400")
        self.connections = Connections(gate1="GPIB0::2::INSTR", gate2="GPIB0::3::INSTR",
                                       gate3="GPIB0::1::INSTR", lockin="GPIB0::8::INSTR")

    def estimate(self, p=None, **kwargs):
        return estimate_cosweep_seconds(p or self.p, connections=self.connections,
                                        device_id="YZ324", **kwargs)

    def test_complete_historical_run_including_cleanup(self):
        self.assertAlmostEqual(self.estimate(), 1430, places=5)

    def test_delay_and_averages_are_not_multiplied_by_historical_correction(self):
        baseline = self.estimate()
        self.assertAlmostEqual(self.estimate(replace(self.p, delay=1.4)) - baseline, 1111)
        self.assertGreater(self.estimate(replace(self.p, n_sample=2)) - baseline, 500)

    def test_correction_scales_with_points_and_retains_ramp_cost(self):
        p = replace(self.p, doping_step=0.4)
        from app.workers.cosweep import build_cosweep_points
        residual = self.estimate() - estimate_cosweep_seconds(self.p)
        self.assertAlmostEqual(self.estimate(p) - estimate_cosweep_seconds(p),
                               residual * len(build_cosweep_points(p)) / 1111)
        self.assertGreater(self.estimate(replace(self.p, vds_ramp=0.001)), self.estimate())

    def test_different_hardware_device_and_trajectory_use_uncalibrated_model(self):
        baseline = estimate_cosweep_seconds(self.p)
        self.assertEqual(estimate_cosweep_seconds(self.p, connections=Connections(), device_id="YZ324"), baseline)
        self.assertEqual(estimate_cosweep_seconds(self.p, connections=self.connections, device_id="different"), baseline)
        for p in (replace(self.p, vds_source="NI DAQ"),
                  replace(self.p, axis_fast="Doping", axis_slow="Vds"),
                  replace(self.p, coordinate_mode="Raw", axis_slow="None")):
            self.assertEqual(self.estimate(p), estimate_cosweep_seconds(p))

    def test_duration_format_preserves_subsecond_changes_and_carries_minutes(self):
        from app.workers.cosweep_timing import format_cosweep_duration
        self.assertEqual(format_cosweep_duration(1430), "23 min 50.00 s")
        self.assertEqual(format_cosweep_duration(3599.999), "1 h 0 min 0.00 s")
        self.assertNotEqual(format_cosweep_duration(80), format_cosweep_duration(80.02))

    def test_average_three_uses_observed_throughput_not_average_one_extrapolation(self):
        p = replace(self.p, n_sample=3, doping_start=-1.5, doping_stop=1.5,
                    doping_step=0.075, vds_start=0.8, vds_stop=1.3, vds_step=0.001)
        # Completed map: 41 passes x 501 points; metadata includes zero return.
        self.assertAlmostEqual(self.estimate(p), 47944.0, places=5)
        self.assertAlmostEqual(self.estimate(replace(p, delay=1.4)) - self.estimate(p), 20541)
        self.assertGreater(self.estimate(replace(p, n_sample=4)) - self.estimate(p), 10000)
        from app.workers.cosweep_timing import cosweep_calibration_description
        self.assertIn("completed", cosweep_calibration_description(p, self.connections, "YZ324"))

    def test_other_averages_interpolate_and_extrapolate_completed_references(self):
        one = self.estimate()
        three = self.estimate(replace(self.p, n_sample=3))
        increment = (three - one) / 2
        for count in (2, 4, 5, 10):
            with self.subTest(count=count):
                self.assertAlmostEqual(self.estimate(replace(self.p, n_sample=count)),
                                       one + (count - 1) * increment, places=5)
        from app.workers.cosweep_timing import cosweep_calibration_description
        self.assertIn("interpolated", cosweep_calibration_description(replace(self.p, n_sample=2), self.connections, "YZ324"))
        self.assertIn("extrapolated", cosweep_calibration_description(replace(self.p, n_sample=5), self.connections, "YZ324"))
