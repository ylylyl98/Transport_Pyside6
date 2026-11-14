import csv
import os
from pathlib import Path
import tempfile
import unittest

if os.environ.get('TRANSPORT_OFFLINE_VERIFICATION') != '1':
    raise unittest.SkipTest('Run through verify_offline.py')

import numpy as np
from app.curve_history import load_curve
from app.quick.plot import PlotModel, channel_unit


class ContinuousFieldPlotTests(unittest.TestCase):
    def test_channel_units(self):
        for channel, unit in [('raw_X', 'V'), ('raw_drive_Y', 'V'), ('Vds_measured', 'V'),
                              ('Sample_temperature_K', 'K'), ('B_measured_T', 'T'), ('Ids_DC', 'A')]:
            self.assertEqual(channel_unit(channel), unit)

    def test_live_source_fields_and_other_axes(self):
        plot = PlotModel()
        self.addCleanup(lambda: plot._timer.stop())
        plot.beginRun({'x_axis': 'B-field'})
        plot.appendRecord({'x': 1., 'index': 7, 'Ids_DC': 10., 'Ids_Keithley': 11.,
                           'Ibias2_Keithley': 12., 'B_Keithley_T': 2., 'B_Bias2_T': None})
        def x(channel):
            return next(plot.traces(channel))[1].values
        np.testing.assert_equal(x('Ids_DC'), [1.])
        np.testing.assert_equal(x('Ids_Keithley'), [2.])
        np.testing.assert_equal(x('Ibias2_Keithley'), [np.nan])
        plot._resolved_axis = 'Step Index'
        np.testing.assert_equal(x('Ids_Keithley'), [7.])

    def test_live_legacy_without_source_field(self):
        plot = PlotModel()
        self.addCleanup(lambda: plot._timer.stop())
        plot.beginRun({'x_axis': 'B-field'})
        plot.appendRecord({'x': 1., 'Ids_Keithley': 2.})
        np.testing.assert_equal(next(plot.traces('Ids_Keithley'))[1].values, [1.])

    def test_history_source_field_and_legacy(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'sample.csv'
            def write(headers, row):
                with path.open('w', newline='', encoding='utf-8') as handle:
                    writer = csv.writer(handle)
                    writer.writerow(headers)
                    writer.writerow(row)
            headers = ['B_measured_T', 'B_Keithley_T', 'B_Bias2_T', 'Ids_Keithley', 'Ibias2_Keithley', 'Ids_DC', 'Vtg']
            write(headers, [1, 2, 3, 4, 5, 6, 7])
            for signal, expected in [('Ids_Keithley', 2), ('Ibias2_Keithley', 3), ('Ids_DC', 1)]:
                curve = load_curve(path, 'B_measured_T', signal)
                np.testing.assert_equal(curve.x, [expected])
                self.assertEqual(curve.x_unit, 'T')
            np.testing.assert_equal(load_curve(path, 'Vtg', 'Ids_Keithley').x, [7])
            write(headers, [1, '', 3, 4, 5, 6, 7])
            with self.assertRaisesRegex(ValueError, 'No finite measurements'):
                load_curve(path, 'B_measured_T', 'Ids_Keithley')
            write(['B_measured_T', 'Keithley_current'], [1, 4])
            np.testing.assert_equal(load_curve(path, 'B_measured_T', 'Ids_Keithley').x, [1])
