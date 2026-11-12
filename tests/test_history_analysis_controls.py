import os
import tempfile
import unittest
from pathlib import Path

if os.environ.get('TRANSPORT_OFFLINE_VERIFICATION') != '1':
    raise unittest.SkipTest('Use verify_offline.py')

import numpy as np
from PySide6 import QtCore
from app.quick.history import HistoryBackend
from tests.test_quick_history import FakeExporter

class HistoryAnalysisControlTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.exporter = FakeExporter()
        self.backend = HistoryBackend(Path(self.folder.name), settings=QtCore.QSettings(str(Path(self.folder.name) / 'prefs.ini'), QtCore.QSettings.IniFormat), exporter=self.exporter, autostart=False)
        self.addCleanup(self.backend.shutdown)

    def test_nan_cut_blocks_cut_and_both_but_not_heatmap(self):
        path = str(Path(self.folder.name) / 'map.csv')
        self.backend._snapshot = {'map': {'path': path, 'x': np.array([0., 1.]), 'y': np.array([1., 2.]), 'x_name': 'Vtg', 'y_name': 'Vbg'},
            'traces': [{'path': path, 'x': np.array([0., 1.]), 'y': np.array([np.nan, np.nan])}],
            'records': [{'path': path}], 'warnings': [], '_coverage_paths': []}
        self.backend._views[self.backend._kind]['fixed_axis'] = 'Vbg'
        self.backend._views[self.backend._kind]['fixed_value'] = 1.
        self.assertTrue(self.backend.exportReady)
        self.assertFalse(self.backend.cutHasData)
        self.assertFalse(self.backend.cutReady)
        self.assertIn('no finite signal', self.backend.cutEmptyText)
        for mode in ('cut', 'both'):
            self.backend.exportView(mode, str(Path(self.folder.name) / mode))
            self.assertIn('No finite measured cut', self.backend.status)
            self.assertEqual(self.exporter.jobs, [])
        self.backend.exportView('heatmap', str(Path(self.folder.name) / 'heatmap'))
        self.assertEqual(len(self.exporter.jobs), 1)

    def test_coordinates_preserve_distinct_exact_values(self):
        a, b = .12345678901, .12345678902
        self.backend._snapshot = {'map': {'x': np.array([0.]), 'y': np.array([a,b]), 'x_name': 'Vtg', 'y_name': 'Vbg'}}
        self.backend._views[self.backend._kind].update(fixed_axis='Vbg', fixed_value=b)
        options = self.backend.measuredCoordinateOptions
        self.assertNotEqual(options[0]['label'], options[1]['label'])
        self.assertEqual([x['value'] for x in options], [a,b])
        self.assertEqual(self.backend.cutCoordinateLabel, options[1]['label'])
        self.assertEqual(self.backend.cutCoordinateExact, repr(b))

    def test_range_labels_follow_selected_channel_units(self):
        plot = self.backend._plot
        plot.setTraces([{'x':[0.,1.], 'y':[1.,2.], 'channel':'Ids_X'},
                        {'x':[0.,1.], 'y':[3.,4.], 'channel':'raw_X'}],
                       yLabel='Ids_X (A)', channelLabels={'Ids_X':'Ids_X (A)', 'raw_X':'raw_X (V)'})
        plot.applyView({'layout':'two', 'overlay_channels':['Ids_X','raw_X']})
        self.backend.setRangeChannel('raw_X')
        self.assertEqual(self.backend.rangeState['y']['title'], 'raw_X (V)')
        self.assertEqual(self.backend.rangeState['y']['unit'], 'V')
        plot.applyView({'layout':'overlay', 'overlay_channels':['Ids_X','raw_X'], 'overlay_axis':'shared'})
        self.assertEqual(self.backend.rangeState['y']['title'], 'Shared Y — mixed units')
        self.assertEqual(self.backend.rangeState['y']['unit'], '')
        self.backend._views[self.backend._kind]['normalization'] = 'max_abs'
        self.assertEqual(self.backend.rangeState['y']['unit'], '')

    def test_per_channel_ranges_and_shared_overlay(self):
        plot = self.backend._plot
        plot.setTraces([{'x':[0.,1.], 'y':[2.,3.], 'channel':'Ids_X'}, {'x':[0.,1.], 'y':[4.,5.], 'channel':'Ids_Y'}])
        plot.applyView({'layout':'two', 'overlay_channels':['Ids_X','Ids_Y']})
        self.assertEqual(self.backend.rangeChannels, ['Ids_X','Ids_Y'])
        self.backend.setRangeChannel('Ids_Y')
        self.backend.setRange('y', False, '10', '20')
        view = plot.captureView()
        self.assertEqual(view['channel_views']['Ids_Y']['ylim'], [10., 20.])
        self.assertFalse(view['channel_views']['Ids_X']['manual_y'][0])
        captured = self.backend.capture_view()
        plot.home()
        plot.applyView(captured)
        self.assertEqual(plot.captureView()['channel_views']['Ids_Y']['ylim'], [10., 20.])
        self.assertEqual(self.backend.rangeChannel, 'Ids_Y')
        self.backend.setRange('y', True, '', '')
        self.assertFalse(plot.captureView()['channel_views']['Ids_Y']['manual_y'][0])
        plot.applyView({'layout':'four', 'four_channels':True, 'channel_order':['Ids_X','Ids_Y','I_drag_X','I_drive_X']})
        self.backend.setRangeChannel('I_drive_X')
        self.backend.setRange('y', False, '-2', '2')
        self.assertEqual(plot.captureView()['channel_views']['I_drive_X']['ylim'], [-2.,2.])
        plot.applyView({'layout':'overlay', 'overlay_channels':['Ids_X','Ids_Y'], 'overlay_axis':'shared'})
        self.assertEqual(self.backend.rangeChannels, ['Shared Y'])
        self.backend.setRange('y', False, '-5', '5')
        view = plot.captureView()
        for channel in ('Ids_X','Ids_Y'):
            self.assertEqual(view['channel_views'][channel]['ylim'], [-5.,5.])
            self.assertTrue(view['channel_views'][channel]['manual_y'][0])
