"""Field/signal time association with simulated telemetry only."""
import math
import csv
import threading
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import patch

from app.engine.field_sampling import FieldAssociation, field_points, interpolate_fields
from app.engine.transport_tasks import LatestTelemetry
from app.engine.bfield_transport_controller import BFieldTransportController
from app.models import BFieldTransportCondition, BFieldTransportParams
from app.engine.bfield_transport_sweep import TransportCsvWriter
from app.engine.continuous_transport import CONTINUOUS_COLUMNS
from app.engine.field_sampling import FIELD_SAMPLE_COLUMNS
from app.devices.aps100_attodry1000_adapter import APS100AttoDry1000Adapter


def snapshots(points):
    return [NS(monotonic_s=t, field_t=b) for t, b in points]


class FieldSamplingTests(unittest.TestCase):
    def test_csv_persists_field_association_metadata(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'data.csv'
            values = {name: (index + 0.25) for index, name in enumerate(FIELD_SAMPLE_COLUMNS)}
            values['B_assignment'] = 'read_midpoints_interpolated'
            writer = TransportCsvWriter(str(path), BFieldTransportCondition(), extra_columns=CONTINUOUS_COLUMNS)
            try:
                writer.write(values)
            finally:
                writer.close()
            with path.open(newline='', encoding='utf-8') as handle:
                reader = csv.DictReader(handle)
                row = next(reader)
                self.assertTrue(set(FIELD_SAMPLE_COLUMNS).issubset(reader.fieldnames))
            for key, expected in values.items():
                self.assertEqual(row[key], str(expected))

    def test_adapter_field_timestamp_is_query_midpoint(self):
        fake = NS(_lock=threading.RLock(), coil_constant_t_per_a=2.,
                  get_units=lambda: 'kG', get_operating_mode=lambda: 'Manual',
                  get_field_t=lambda: .2, get_output_field_t=lambda: .4,
                  get_limits_t=lambda: (-1., 1.), get_heater_state=lambda: 1,
                  get_sweep_state=lambda: 'holding', get_status=lambda: None,
                  get_voltage_limit_v=lambda: 1., get_magnet_voltage_v=lambda: 0.,
                  get_output_voltage_v=lambda: 0.)
        with patch('app.devices.aps100_attodry1000_adapter.time.monotonic', side_effect=[10., 14., 18.]):
            snapshot = APS100AttoDry1000Adapter.read_snapshot(fake)
        self.assertEqual(snapshot.field_monotonic_s, 12.)
        self.assertEqual(snapshot.monotonic_s, 18.)
        self.assertEqual(snapshot.field_t, .2)

    def finish(self, reads, points, **sources):
        timing = FieldAssociation()
        timing.daq = reads
        for key, value in sources.items():
            setattr(timing, key, value)
        return timing.finish({}, lambda: snapshots(points), threading.Event(), lambda: None)

    def test_linear_ramp_uses_actual_uneven_read_times_not_trigger_or_endpoint_mean(self):
        row = self.finish([(1., 1.2), (1.4, 1.8), (3., 3.4)], [(0., 0.), (2., .02), (4., .04)])
        self.assertAlmostEqual(row['B_measured_T'], (.011+.016+.032)/3.)
        self.assertEqual(row['B_DAQ_read_count'], 3)
        self.assertAlmostEqual(row['B_acquisition_span_T'], .024)

    def test_reverse_ramp_and_one_read(self):
        row = self.finish([(1., 2.)], [(0., .04), (3., .01)])
        self.assertAlmostEqual(row['B_measured_T'], .025)
        self.assertAlmostEqual(row['B_acquisition_start_T'], .03)
        self.assertAlmostEqual(row['B_acquisition_end_T'], .02)

    def test_nonlinear_ramp_span_keeps_intermediate_extremum(self):
        row = self.finish([(0., 0.), (1., 1.), (2., 2.)], [(0., 0.), (1., .1), (2., 0.)])
        self.assertAlmostEqual(row['B_measured_T'], .1/3.)
        self.assertAlmostEqual(row['B_acquisition_span_T'], .1)

    def test_sequential_keithley_reads_get_their_own_field(self):
        row = self.finish([(1., 2.)], [(0., 0.), (3., .03), (6., .06)],
                          keithley=(3., 4.), bias2=(4., 5.))
        self.assertAlmostEqual(row['B_measured_T'], .015)
        self.assertAlmostEqual(row['B_Keithley_T'], .035)
        self.assertAlmostEqual(row['B_Bias2_T'], .045)

    def test_no_daq_uses_actual_smu_read(self):
        row = self.finish([], [(0., 0.), (2., .2)], keithley=(1., 2.))
        self.assertAlmostEqual(row['B_measured_T'], .15)
        self.assertEqual(row['B_DAQ_read_count'], 0)

    def test_missing_leading_bracket_and_gaps_are_rejected(self):
        for points in ([(2., .02), (3., .03)], [(0., 0.), (4., .04)]):
            with self.subTest(points=points), self.assertRaises(RuntimeError):
                self.finish([(1., 3.)], points)
        # A long gap between reads must not be hidden by exact endpoint matches.
        with self.assertRaisesRegex(RuntimeError, 'gap'):
            self.finish([(0., 0.), (5., 5.)], [(0., 0.), (5., .05)])

    def test_invalid_conflicting_or_unordered_telemetry_is_rejected(self):
        for points in ([(0., 0.), (1., math.nan)], [(1., 0.), (0., 0.)], [(1., 0.), (1., 1.)]):
            with self.subTest(points=points), self.assertRaises(RuntimeError):
                field_points(snapshots(points))
        self.assertEqual(field_points(snapshots([(1., .1), (1., .1)])), [(1., .1)])

    def test_field_query_timestamp_takes_precedence(self):
        self.assertEqual(field_points([NS(monotonic_s=5., field_monotonic_s=4., field_t=.1)]), [(4., .1)])

    def test_no_extrapolation(self):
        for stamp in (-1., 3.):
            with self.assertRaisesRegex(RuntimeError, 'bracket'):
                interpolate_fields([(0., 0.), (2., .2)], [stamp])

    def test_capture_pins_history_and_clear_invalidates_it(self):
        cache = LatestTelemetry()
        cache.publish(snapshots([(0., 0.)])[0])
        with cache.capture_history() as history:
            for i in range(1, 100):
                cache.publish(snapshots([(i, i/100.)])[0])
            self.assertEqual(len(history()), 100)
            self.assertEqual(history()[0].monotonic_s, 0.)
            cache.clear()
            with self.assertRaisesRegex(RuntimeError, 'cleared'):
                history()
        self.assertEqual(cache._captures, [])

    def test_capture_does_not_suppress_acquisition_errors(self):
        cache = LatestTelemetry()
        with self.assertRaisesRegex(RuntimeError, 'DAQ failed'):
            with cache.capture_history():
                raise RuntimeError('DAQ failed')
        self.assertEqual(cache._captures, [])

    def test_nested_capture_keeps_outer_registered_after_inner_closes(self):
        cache = LatestTelemetry()
        cache.publish(snapshots([(0., 0.)])[0])
        with self.assertRaisesRegex(RuntimeError, 'outer error'):
            with cache.capture_history() as outer:
                with cache.capture_history() as inner:
                    self.assertEqual(len(inner()), 1)
                cache.publish(snapshots([(1., .1)])[0])
                self.assertEqual([point.monotonic_s for point in outer()], [0., 1.])
                raise RuntimeError('outer error')
        self.assertEqual(cache._captures, [])

    def test_cancel_interrupts_trailing_bracket_wait(self):
        cancel = threading.Event()
        timing = FieldAssociation()
        timing.daq = [(1., 2.)]
        def wait(_):
            cancel.set()
        cancel.wait = wait
        def check():
            if cancel.is_set():
                raise RuntimeError('cancelled')
        with self.assertRaisesRegex(RuntimeError, 'cancelled'):
            timing.finish({}, lambda: snapshots([(0., 0.)]), cancel, check)

    def test_missing_trailing_bracket_has_bounded_wait(self):
        timing = FieldAssociation()
        timing.daq = [(1., 2.)]
        with patch('app.engine.field_sampling.time.monotonic', side_effect=[10., 14.]):
            with self.assertRaisesRegex(RuntimeError, 'Timed out'):
                timing.finish({}, lambda: snapshots([(0., 0.)]), threading.Event(), lambda: None)

    def test_controller_matches_actual_reads_and_waits_only_for_trailing_telemetry(self):
        cache, now = LatestTelemetry(), [0.]
        def advance(duration):
            now[0] += duration
            cache.publish(snapshots([(now[0], now[0]*.01)])[0])
        cache.publish(snapshots([(0., 0.)])[0])
        reads = []
        def acquire():
            reads.append(now[0])
            advance(.2)
            return [1., 2., 3.]
        params = BFieldTransportParams(averages=3, read_delay_s=.4)
        cancel = NS(wait=advance)
        controller = NS(magnet=NS(telemetry_cache=cache), plan=NS(params=params),
                        _io_cancel=cancel, _check_io_cancel=lambda: None,
                        _calibration=(1., 1.), _signal_chain=None,
                        device_manager=NS(get_session=lambda name: NS(acquire=acquire) if name == 'daq' else None))
        with patch('time.monotonic', side_effect=lambda: now[0]):
            row = BFieldTransportController._read_sample(controller,
                {'_match_field': True, 'B_measured_T': -.2, 'B_trigger_T': -.2},
                BFieldTransportCondition(vds_source='None'))
        self.assertEqual(len(reads), 3)
        self.assertAlmostEqual(row['B_measured_T'], .007)
        self.assertEqual(row['B_trigger_T'], -.2)
        self.assertNotIn('_match_field', row)
        self.assertEqual(cache._captures, [])


if __name__ == '__main__':
    unittest.main()
