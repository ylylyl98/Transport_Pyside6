"""Frozen Quick plot snapshots through the independent PNG archive/render API."""
import csv
import io
import json
import os
from pathlib import Path
import tempfile
import time
import shutil
import unittest

if os.environ.get('TRANSPORT_OFFLINE_VERIFICATION') != '1':
    raise unittest.SkipTest('Use verify_offline.py')

import numpy as np
from app.quick.plot import PlotModel
from app.plot_snapshot import load_snapshot, save_snapshot


class QuickPlotExportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='quick-export-')
        self.root = Path(self.temp.name)
        self.model = PlotModel()
        self.model.beginRun({'x_axis': 'Vtg', 'measurement': 'gate_scan',
                             'csv_path': str(self.root / 'partial.csv'),
                             'params': {'vds_source': 'Keithley 2400'}})
        for i in range(12):
            self.model.appendRecord({'vtg': i, 'Ids_DC': i*1e-9, 'Ids_X': i*1e-6,
                                     'Ids_Y': -i*1e-9, 'Ids_Keithley': i*1e-12,
                                     'direction': 'Forward' if i < 6 else 'Backward'})
        self.model.fourChannels = True
        self.model.set_limits('Ids_DC', (2, 5), (-1e-9, 4e-9))
        self.model.set_limits('Ids_X', (2, 5), (-2e-6, 3e-6))

    def tearDown(self):
        self.model._timer.stop()
        self.temp.cleanup()

    def test_archive_and_detached_renderer_schema_preserve_panels_and_limits(self):
        from app.png_export import build_analysis_figure, _current_scale
        original = self.model.exportSnapshot()
        archive = self.root / 'snapshot.npz'
        save_snapshot(original, archive)
        self.model.appendRecord({'vtg': 100, 'Ids_DC': 100})
        frozen = load_snapshot(archive)
        self.assertEqual(frozen['view']['channel_order'], self.model.channels())
        figure = build_analysis_figure(frozen)
        self.assertEqual(len(figure.axes), 4)
        for axis, channel in zip(figure.axes, frozen['view']['channel_order']):
            panel = frozen['panels'][channel]
            factor, _ = _current_scale(panel)
            np.testing.assert_allclose(axis.get_xlim(), panel['view']['xlim'])
            np.testing.assert_allclose(axis.get_ylim(), np.asarray(panel['view']['ylim']) / factor)
            self.assertEqual(sum(len(line.get_xdata()) for line in axis.lines), 12)
            self.assertEqual(axis.lines[1].get_linestyle(), '--')
        figure.clear()

    def test_all_native_palette_assets_match_named_export_palette_samples(self):
        import matplotlib
        from app.quick.palettes import COLOR_TABLES
        from app.quick.plot import color_table
        self.assertEqual(set(COLOR_TABLES), set(matplotlib.colormaps))
        for name in matplotlib.colormaps:
            expected = matplotlib.colormaps[name](np.linspace(0, 1, 256), bytes=True)[:, :3]
            np.testing.assert_array_equal(color_table(name), expected)
        self.model.setColorMap('RdBu_r')
        self.assertEqual(self.model.colorMap, 'RdBu_r')

    def _history_source(self, missing=False):
        columns = ['Vtg', 'Ids_DC', 'Ids_X', 'Ids_Y', 'Ids_Keithley', 'Direction']
        if missing:
            columns.remove('Ids_Y')
        with (self.root / 'partial.csv').open('w', newline='') as stream:
            writer = csv.writer(stream)
            writer.writerow(columns)
            writer.writerow(['V' if c == 'Vtg' else '' if c == 'Direction' else 'A' for c in columns])
            for i in range(12):
                row = {'Vtg': i, 'Ids_DC': i*1e-9, 'Ids_X': i*1e-6, 'Ids_Y': -i*1e-9,
                       'Ids_Keithley': i*1e-12, 'Direction': 'Forward' if i < 6 else 'Backward'}
                writer.writerow([row[c] for c in columns])
        (self.root / 'partial_metadata.json').write_text(json.dumps({'measurement': 'gate_scan'}), encoding='utf-8')

    def _restore_history(self, path):
        from PySide6 import QtCore
        from tests.test_quick_history import FakeExporter
        from app.quick.history import HistoryBackend
        settings = QtCore.QSettings(str(self.root / 'history.ini'), QtCore.QSettings.Format.IniFormat)
        backend = HistoryBackend(self.root, settings=settings, exporter=FakeExporter(), autostart=False)
        self.addCleanup(backend.shutdown)
        backend.restoreView(str(path))
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and backend.busy:
            QtCore.QCoreApplication.processEvents()
            time.sleep(.005)
        self.assertFalse(backend.busy, backend.status)
        return backend

    def test_export_restore_and_reexport_keep_real_channels_layout_limits_and_samples(self):
        from app.png_export import export_analysis, build_analysis_figure
        self._history_source()
        # Selected channel differs from the first overview panel.
        self.model.channel = 'Ids_X'
        self.model.set_limits('Ids_DC', (2, 5), (-1e-9, 4e-9))
        self.model.set_limits('Ids_X', (2, 5), (-2e-6, 3e-6))
        snapshot = self.model.exportSnapshot()
        self.assertEqual(snapshot['signal'], 'Ids_X')
        for output in (self.root / 'roundtrip.png', None):
            exported = export_analysis(snapshot, output)
            backend = self._restore_history(exported['view_path'])
            self.assertEqual(backend.state['signal'], 'Ids_X')
            self.assertTrue(backend.plotModel.fourChannels)
            self.assertEqual(backend.plotModel.channels(), self.model.channels())
            for channel in self.model.channels():
                actual = list(backend.plotModel.traces(channel))
                self.assertEqual(sum(len(y.values) for _, _, y in actual), 12)
                np.testing.assert_allclose(np.concatenate([y.values for _, _, y in actual]),
                                           np.concatenate([y.values for _, _, y in self.model.traces(channel)]))
                np.testing.assert_allclose(backend.plotModel.limits(channel), self.model.limits(channel))
            self.assertEqual(backend.rangeState['y']['title'], 'Ids_X (A)')
            np.testing.assert_allclose(backend.rangeState['y']['bounds'], [-2e-6, 3e-6])
            backend.setRange('y', False, '-4e-6', '8e-6')
            np.testing.assert_allclose(backend.plotModel.limits('Ids_DC')[1], [-1e-9, 4e-9])
            backend.plotModel.set_limits('Ids_Y', (3, 4), (-3e-9, -1e-9))
            repeated = backend.export_snapshot()
            figure = build_analysis_figure(repeated)
            self.assertEqual(len(figure.axes), 4)
            self.assertEqual(sum(len(line.get_xdata()) for ax in figure.axes for line in ax.lines), 48)
            figure.clear()
            again = export_analysis(repeated, self.root / ('again-' + str(output is None) + '.png'))
            clone = self._restore_history(again['view_path'])
            self.assertTrue(clone.plotModel.fourChannels)
            self.assertEqual(clone.plotModel.channels(), self.model.channels())
            np.testing.assert_allclose(clone.plotModel.limits('Ids_X'), ((3, 4), (-4e-6, 8e-6)))
            np.testing.assert_allclose(clone.plotModel.limits('Ids_Y')[1], (-3e-9, -1e-9))
            backend.setOption('signal', 'Ids_X')
            deadline = time.monotonic() + 5
            from PySide6.QtCore import QCoreApplication
            while backend.busy and time.monotonic() < deadline:
                QCoreApplication.processEvents()
                time.sleep(.005)
            self.assertFalse(backend.plotModel.fourChannels)
            self.assertEqual(backend.plotModel.channels(), ['Ids_X'])

    def test_legacy_overview_label_recovers_order_and_missing_channel_keeps_other_panels(self):
        from app.png_export import export_analysis
        self._history_source(missing=True)
        exported = export_analysis(self.model.exportSnapshot(), self.root / 'legacy.png')
        path = Path(exported['view_path'])
        payload = json.loads(path.read_text(encoding='utf-8'))
        payload['view']['signal'] = '4-Channel Compare'
        payload['view']['channel_order'] = ['Ids_X', 'Ids_Y', 'Ids_DC', 'Ids_Keithley']
        path.write_text(json.dumps(payload), encoding='utf-8')
        backend = self._restore_history(path)
        self.assertEqual(backend.state['signal'], 'Ids_X')
        self.assertEqual(backend.plotModel.channels(), payload['view']['channel_order'])
        self.assertIn('Missing column: Ids_Y', backend.status)
        self.assertEqual(list(backend.plotModel.traces('Ids_Y')), [])
        for channel in ('Ids_X', 'Ids_DC', 'Ids_Keithley'):
            self.assertEqual(sum(len(y.values) for _, _, y in backend.plotModel.traces(channel)), 12)

    def test_restored_normalized_panels_keep_dimensionless_labels_and_map_has_single_view(self):
        from PySide6.QtCore import QCoreApplication
        from app.png_export import export_analysis, build_analysis_figure
        from app.analysis_csv_export import csv_bytes
        self._history_source()
        exported = export_analysis(self.model.exportSnapshot(), self.root / 'normalized.png')
        path = Path(exported['view_path'])
        payload = json.loads(path.read_text(encoding='utf-8'))
        payload['view']['normalization'] = 'max_abs'
        for panel in payload['view']['channel_views'].values():
            panel['manual_y'] = [False]
        path.write_text(json.dumps(payload), encoding='utf-8')
        backend = self._restore_history(path)
        snapshot = backend.export_snapshot()
        self.assertEqual(backend.traceCount, 8)
        figure = build_analysis_figure(snapshot)
        for channel, axis in zip(backend.plotModel.channels(), figure.axes):
            self.assertEqual(snapshot['panels'][channel]['y_unit'], '')
            self.assertIn('Normalized ' + channel, axis.get_ylabel())
            self.assertIn('Normalized ' + channel, backend.plotModel.channelLabel(channel))
            for trace in snapshot['panels'][channel]['traces']:
                self.assertLessEqual(np.max(np.abs(trace['y'])), 1.)
        figure.clear()
        rows = list(csv.reader(io.StringIO(csv_bytes(snapshot, 'curve').decode('utf-8-sig'))))
        self.assertTrue(all(unit == '' for unit in rows[1][1::2]))
        # Switching to a newly selected map must discard the overview layout.
        map_path = self.root / 'map.csv'
        map_path.write_text('Vtg,Vbg,Ids_DC\nV,V,A\n0,0,1\n1,0,2\n0,1,3\n1,1,4\n', encoding='utf-8')
        map_path.with_name('map_metadata.json').write_text(json.dumps({'measurement': 'map_2d'}), encoding='utf-8')
        backend.refresh()
        deadline = time.monotonic() + 5
        while backend.busy and time.monotonic() < deadline:
            QCoreApplication.processEvents()
            time.sleep(.005)
        backend.selectType('map_2d')
        backend.setChecked(str(map_path), True)
        deadline = time.monotonic() + 5
        while backend.busy and time.monotonic() < deadline:
            QCoreApplication.processEvents()
            time.sleep(.005)
        self.assertFalse(backend.busy)
        self.assertTrue(backend.hasMap)
        self.assertFalse(backend.plotModel.fourChannels)
        self.assertFalse(backend.capture_view()['four_channels'])

    def test_live_map_overview_restores_full_curves_even_when_source_has_two_map_axes(self):
        from app.png_export import export_analysis
        path = self.root / 'grid.csv'
        path.write_text('Vtg,Vbg,Ids_DC,Ids_X,Ids_Y,Ids_Keithley\nV,V,A,A,A,A\n'
                        '0,0,1,2,3,4\n1,0,2,3,4,5\n0,1,3,4,5,6\n1,1,4,5,6,7\n', encoding='utf-8')
        params = {'vds_source': 'Keithley 2400', 'axis_fast': 'Vtg', 'axis_slow': 'Vbg'}
        path.with_name('grid_metadata.json').write_text(json.dumps({'measurement': 'map_2d', 'params': params}), encoding='utf-8')
        self.model.beginRun({'measurement': 'map_2d', 'x_axis': 'Vtg', 'csv_path': str(path), 'params': params})
        for i, (vtg, vbg) in enumerate(((0, 0), (1, 0), (0, 1), (1, 1))):
            self.model.appendRecord({'vtg': vtg, 'vbg': vbg, 'Ids_DC': i+1, 'Ids_X': i+2,
                                     'Ids_Y': i+3, 'Ids_Keithley': i+4})
        self.model.fourChannels = True
        exported = export_analysis(self.model.exportSnapshot(), self.root / 'map-overview.png')
        backend = self._restore_history(exported['view_path'])
        self.assertTrue(backend.plotModel.fourChannels)
        self.assertFalse(backend.hasMap)
        self.assertEqual(backend.state['map_y'], 'None (1D)')
        self.assertFalse(backend.state['auto_axes'])
        for channel in self.model.channels():
            np.testing.assert_array_equal(np.concatenate([x.values for _, x, _ in backend.plotModel.traces(channel)]), [0, 1, 0, 1])
            self.assertEqual(sum(len(y.values) for _, _, y in backend.plotModel.traces(channel)), 4)

    def test_daq_overview_unassigned_fourth_panel_restores_other_three_channels(self):
        from app.png_export import export_analysis
        self._history_source()
        self.model.beginRun({'measurement': 'gate_scan', 'x_axis': 'Vtg',
                             'csv_path': str(self.root / 'partial.csv'), 'params': {'vds_source': 'NI DAQ AO'}})
        for i in range(12):
            self.model.appendRecord({'vtg': i, 'Ids_DC': i*1e-9, 'Ids_X': i*1e-6, 'Ids_Y': -i*1e-9})
        self.model.fourChannels = True
        self.assertEqual(self.model.channels(), ['Ids_DC', 'Ids_Y', 'Ids_X', ''])
        exported = export_analysis(self.model.exportSnapshot(), self.root / 'daq-overview.png')
        backend = self._restore_history(exported['view_path'])
        self.assertEqual(backend.plotModel.channels(), self.model.channels())
        self.assertIn('No channel assigned', backend.status)
        for channel in ('Ids_DC', 'Ids_X', 'Ids_Y'):
            self.assertEqual(sum(len(y.values) for _, _, y in backend.plotModel.traces(channel)), 12)

    def test_explicit_and_automatic_export_include_all_channel_csv_and_view(self):
        from app.png_export import export_analysis
        from app.analysis_csv_export import csv_bytes
        snapshot = self.model.exportSnapshot()
        rows = list(csv.reader(io.StringIO(csv_bytes(snapshot, 'curve').decode('utf-8-sig'))))
        self.assertEqual(len(rows[0]), 16)  # 4 channels x 2 directions x X/Y
        for channel in self.model.channels():
            self.assertIn(channel, rows[0])
        exported = export_analysis(snapshot, self.root / 'overview.png')
        self.assertEqual(len(exported['outputs']), 1)
        saved = json.loads(Path(exported['view_path']).read_text(encoding='utf-8'))
        self.assertTrue(saved['view']['four_channels'])
        self.assertEqual(saved['image'], {'width': 2400, 'height': 1800, 'dpi': 150})
        self.assertEqual(saved['view']['channel_views']['Ids_DC']['ylim'], [-1e-9, 4e-9])
        self.assertGreater(Path(exported['outputs'][0]).stat().st_size, 10_000)
        automatic = export_analysis(snapshot)
        self.assertEqual(len(automatic['outputs']), 1)
        repeated = export_analysis(snapshot)
        self.assertEqual(repeated['outputs'], automatic['outputs'])
        # A second panel's manual Y range must change PNG identity even though
        # selected first-panel data and all full-resolution CSV bytes match.
        self.model.set_limits('Ids_X', (2, 5), (-3e-6, 7e-6))
        changed = export_analysis(self.model.exportSnapshot())
        self.assertNotEqual(changed['outputs'], automatic['outputs'])
        self.assertEqual(changed['csv_outputs'], automatic['csv_outputs'])

    def test_frozen_four_panel_snapshot_is_rendered_in_separate_hardware_free_process(self):
        from PySide6.QtCore import QTimer
        from PySide6.QtTest import QTest
        from PySide6.QtWidgets import QApplication
        from app.ui.png_export_process import PngExportProcess
        queue = PngExportProcess()
        completed, ready, ticks = [], [], []
        queue.completed.connect(completed.append)
        queue.ready.connect(ready.append)
        timer = QTimer()
        timer.setInterval(10)
        timer.timeout.connect(lambda: ticks.append(1))
        timer.start()
        try:
            queue.submit_snapshot(self.model.exportSnapshot(), self.root / 'detached.png')
            deadline = time.monotonic() + 25
            while time.monotonic() < deadline and not completed:
                QApplication.processEvents()
                QTest.qWait(20)
            self.assertTrue(completed, 'detached four-panel export timed out')
            self.assertNotIn('error', completed[0], completed[0])
            self.assertNotEqual(ready[0]['pid'], os.getpid())
            self.assertEqual(ready[0]['hardware_modules'], [])
            self.assertGreater(len(ticks), 5)
            output = Path('tmp/verification/quick_plot/fourchannel_export.png')
            output.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(completed[0]['outputs'][0], output)
        finally:
            timer.stop()
            queue.shutdown()


if __name__ == '__main__':
    unittest.main()
