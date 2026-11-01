"""Overlay export and Analysis recovery preserve both measured channels."""
import csv
import io
import json
import os
import tempfile
import time
import unittest
from pathlib import Path

if os.environ.get('TRANSPORT_OFFLINE_VERIFICATION') != '1':
    raise unittest.SkipTest('Use verify_offline.py')

import numpy as np
from PySide6 import QtCore, QtWidgets
from app.analysis_csv_export import csv_bytes
from app.analysis_export import export_automatic
from app.curve_cache import SnapshotCache
from app.curve_history import discover_history
from app.png_export import build_analysis_figure
from app.quick.history import HistoryBackend, _defaults, _read_display
from tests.test_quick_history import FakeExporter


class OverlayExportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / 'vds.csv'
        self.path.write_text('Vds,Ids_DC,Ids_X,Direction\nV,A,A,\n0,1e-9,3e-9,forward\n1,2e-9,4e-9,backward\n', encoding='utf-8')
        self.path.with_name('vds_metadata.json').write_text(json.dumps({'measurement':'vds_sweep','created_at':'2026-10-26T12:30:00','status':'finished'}), encoding='utf-8')
        self.view = {**_defaults('vds_sweep'), 'layout':'overlay', 'overlay_channels':['Ids_DC','Ids_X'],
                     'overlay_axis':'dual', 'kind':'vds_sweep', 'channel_order':['Ids_DC','Ids_X'], 'four_channels':False,
                     'signal':'Ids_DC', 'x':'Vds', 'auto_axes':False,
                     'channel_views':{'Ids_DC':{'xlim':[-1,2], 'ylim':[0,3e-9], 'manual_y':[True]},
                                      'Ids_X':{'xlim':[-1,2], 'ylim':[0,5e-9], 'manual_y':[True]}}}

    def snapshot(self):
        records, warnings = discover_history(self.root)
        self.assertFalse(warnings)
        return _read_display(records, self.view, 'vds_sweep', SnapshotCache())[0]

    def test_dual_and_shared_figure_and_full_resolution_csv(self):
        snapshot = self.snapshot()
        self.assertEqual(list(snapshot['panels']), ['Ids_DC', 'Ids_X'])
        for mode, axes_count in [('dual',2),('shared',1)]:
            snapshot['view']['overlay_axis'] = mode
            fig = build_analysis_figure(snapshot)
            self.assertEqual(len(fig.axes), axes_count)
            self.assertIn('Ids_DC + Ids_X', fig.texts[0].get_text())
            lines = [line for axis in fig.axes for line in axis.lines]
            self.assertEqual({line.get_color() for line in lines}, {'#2978d5', '#cf650b'})
            self.assertEqual({line.get_linestyle() for line in lines}, {'-', '--'})
            self.assertEqual(tuple(fig.axes[0].get_xlim()), (-1,2))
            self.assertEqual(tuple(fig.axes[0].get_ylim()), (0,3))
            if mode == 'dual': self.assertEqual(tuple(fig.axes[1].get_ylim()), (0,5))
        rows = list(csv.reader(io.StringIO(csv_bytes(snapshot, 'curve').decode('utf-8-sig'))))
        self.assertEqual(rows[0].count('Ids_DC'), 2)
        self.assertEqual(rows[0].count('Ids_X'), 2)
        self.assertTrue(all(unit in ('V','A') for unit in rows[1]))
        self.assertEqual(len(rows)-3, 1)  # Forward/backward are separate full traces.
        samples = [float(value) for row in rows[3:] for value in row if value]
        for expected in (1e-9,2e-9,3e-9,4e-9): self.assertIn(expected, samples)

    def test_missing_channel_stays_empty_on_restore_and_signal_exits_overlay(self):
        self.path.write_text('Vds,Ids_DC,Direction\nV,A,\n0,1e-9,forward\n1,2e-9,forward\n',encoding='utf-8')
        snapshot = self.snapshot()
        self.assertEqual(snapshot['signal'], 'Ids_DC')
        self.assertEqual(snapshot['panels']['Ids_X']['traces'], [])
        self.assertTrue(snapshot['warnings'])
        settings = QtCore.QSettings(str(self.root / 'settings.ini'), QtCore.QSettings.IniFormat)
        backend = HistoryBackend(self.root, settings=settings, exporter=FakeExporter(), autostart=False)
        self.addCleanup(backend.shutdown)
        sidecar = self.root / 'view.json'
        sidecar.write_text(json.dumps({'schema':'transport_plot_view_v1','view':self.view,
                                      'sources':[{'path':str(self.path)}]}),encoding='utf-8')
        backend.restoreView(str(sidecar))
        end = time.monotonic()+5
        while time.monotonic()<end:
            self.app.processEvents()
            if not backend.busy and not backend._index_active and backend._snapshot: break
            time.sleep(.005)
        self.assertIsNotNone(backend._snapshot, backend.status)
        self.assertEqual(backend._snapshot['view']['layout'], 'overlay')
        self.assertEqual(backend._snapshot['panels']['Ids_X']['traces'], [])
        self.assertEqual(backend.capture_view()['overlay_channels'], ['Ids_DC','Ids_X'])
        backend.setOption('signal','Ids_DC')
        self.assertEqual(backend._views['vds_sweep']['layout'], 'single')

    def test_first_channel_missing_and_automatic_export_restore_metadata(self):
        self.path.write_text('Vds,Ids_X,Direction\nV,A,\n0,3e-9,forward\n1,4e-9,forward\n', encoding='utf-8')
        snapshot = self.snapshot()
        self.assertEqual(snapshot['signal'], 'Ids_DC')
        self.assertFalse(snapshot['panels']['Ids_DC']['traces'])
        self.assertTrue(snapshot['panels']['Ids_X']['traces'])
        self.assertFalse(any('using Ids_X' in w for w in snapshot['warnings']))
        result = export_automatic(snapshot)
        payload = json.loads(Path(result['view_path']).read_text(encoding='utf-8'))
        self.assertEqual(payload['view']['overlay_channels'], ['Ids_DC','Ids_X'])
        self.assertEqual(payload['view']['overlay_axis'], 'dual')
        self.assertEqual(payload['image']['width'], 1200)
        self.assertEqual(payload['image']['height'], 900)
        rows = list(csv.reader(io.StringIO(Path(result['csv_outputs'][0]).read_text(encoding='utf-8-sig'))))
        self.assertIn('Ids_X', rows[0])
        self.assertNotIn('Ids_DC', rows[0])

    def test_live_archive_export_restore_and_reexport_for_both_axis_modes(self):
        from app.quick.plot import PlotModel
        from app.plot_snapshot import save_snapshot, load_snapshot
        from app.png_export import export_analysis, _current_scale

        original_source = self.path.read_bytes()
        for axis_mode in ('shared', 'dual'):
            with self.subTest(axis_mode=axis_mode):
                model = PlotModel()
                self.addCleanup(lambda m=model: m._timer.stop())
                model.beginRun({'x_axis':'Vds', 'measurement':'vds_sweep', 'csv_path':str(self.path)})
                model.appendRecord({'vds':0, 'Ids_DC':1e-9, 'Ids_X':3e-9, 'direction':'forward'})
                model.appendRecord({'vds':1, 'Ids_DC':2e-9, 'Ids_X':4e-9, 'direction':'backward'})
                model.channel = 'Ids_X'
                model.layout = 'overlay'
                model.overlayAxis = axis_mode
                model.set_limits('Ids_X', (-1,2), (0,5e-9))
                if axis_mode == 'dual': model.set_limits('Ids_DC', (-1,2), (-1e-9,3e-9))
                original = model.exportSnapshot()
                self.assertEqual(original['signal'], 'Ids_X')
                self.assertEqual(original['view']['overlay_channels'], ['Ids_X','Ids_DC'])
                archive = self.root / (axis_mode + '.npz')
                save_snapshot(original, archive)
                frozen = load_snapshot(archive)
                exported = export_analysis(frozen, self.root / (axis_mode + '.png'))
                backend = HistoryBackend(self.root, settings=QtCore.QSettings(str(self.root / (axis_mode + '.ini')), QtCore.QSettings.IniFormat), exporter=FakeExporter(), autostart=False)
                self.addCleanup(backend.shutdown)
                backend.restoreView(exported['view_path'])
                end = time.monotonic()+5
                while time.monotonic()<end:
                    self.app.processEvents()
                    if not backend.busy and not backend._index_active and backend._snapshot: break
                    time.sleep(.005)
                self.assertIsNotNone(backend._snapshot, backend.status)
                self.assertEqual(backend.plotModel.layout, 'overlay')
                self.assertEqual(backend.plotModel.overlayAxis, axis_mode)
                self.assertEqual(backend.plotModel.channels(), ['Ids_X','Ids_DC'])
                restored = backend.export_snapshot()
                self.assertEqual(restored['signal'], 'Ids_X')
                for channel in ('Ids_X','Ids_DC'):
                    old = frozen['panels'][channel]['traces']
                    new = restored['panels'][channel]['traces']
                    np.testing.assert_allclose(np.concatenate([t['x'] for t in new]), np.concatenate([t['x'] for t in old]))
                    np.testing.assert_allclose(np.concatenate([t['y'] for t in new]), np.concatenate([t['y'] for t in old]))
                    np.testing.assert_allclose(backend.plotModel.limits(channel), model.limits(channel))
                figure = build_analysis_figure(restored)
                self.assertEqual(len(figure.axes), 1 if axis_mode == 'shared' else 2)
                for i, channel in enumerate(('Ids_X','Ids_DC') if axis_mode == 'dual' else ('Ids_X',)):
                    factor, _ = _current_scale(restored['panels'][channel])
                    np.testing.assert_allclose(figure.axes[i].get_ylim(), np.asarray(model.limits(channel)[1]) / factor)
                repeated = export_analysis(restored, self.root / (axis_mode + '-again.png'))
                first_csv = list(csv.reader(io.StringIO(Path(exported['csv_outputs'][0]).read_text(encoding='utf-8-sig'))))
                second_csv = list(csv.reader(io.StringIO(Path(repeated['csv_outputs'][0]).read_text(encoding='utf-8-sig'))))
                self.assertEqual(first_csv[:2], second_csv[:2])
                self.assertEqual(first_csv[3:], second_csv[3:])
                self.assertEqual(self.path.read_bytes(), original_source)

    def test_two_plot_export_has_two_axes_independent_limits_and_csv(self):
        from app.png_export import export_analysis, _current_scale
        self.view.update(layout='two', overlay_channels=['Ids_X','Ids_DC'],
                         channel_order=['Ids_X','Ids_DC'], signal='Ids_X')
        snapshot = self.snapshot()
        self.assertEqual(snapshot['signal'], 'Ids_X')
        self.assertEqual(list(snapshot['panels']), ['Ids_X','Ids_DC'])
        fig = build_analysis_figure(snapshot)
        self.assertEqual(len(fig.axes), 2)
        for axis, channel in zip(fig.axes, ['Ids_X','Ids_DC']):
            panel = snapshot['panels'][channel]
            factor, _ = _current_scale(panel)
            np.testing.assert_allclose(axis.get_xlim(), [-1,2])
            np.testing.assert_allclose(axis.get_ylim(), np.asarray(self.view['channel_views'][channel]['ylim']) / factor)
            self.assertEqual(sum(len(line.get_xdata()) for line in axis.lines), 2)
        out = export_analysis(snapshot, self.root / 'two.png')
        payload = json.loads(Path(out['view_path']).read_text(encoding='utf-8'))
        self.assertEqual(payload['view']['layout'], 'two')
        self.assertEqual((payload['image']['width'], payload['image']['height']), (1200,900))
        rows = list(csv.reader(io.StringIO(Path(out['csv_outputs'][0]).read_text(encoding='utf-8-sig'))))
        self.assertEqual(rows[0], ['Vds','Ids_X','Vds','Ids_X','Vds','Ids_DC','Vds','Ids_DC'])
        self.assertEqual(rows[1], ['V','A','V','A','V','A','V','A'])

    def test_two_live_archive_restore_and_reexport(self):
        from app.quick.plot import PlotModel
        from app.plot_snapshot import save_snapshot, load_snapshot
        from app.png_export import export_analysis
        source = self.path.read_bytes()
        model = PlotModel()
        self.addCleanup(lambda m=model: m._timer.stop())
        model.beginRun({'x_axis':'Vds', 'measurement':'vds_sweep', 'csv_path':str(self.path)})
        model.appendRecord({'vds':0, 'Ids_DC':1e-9, 'Ids_X':3e-9, 'direction':'forward'})
        model.appendRecord({'vds':1, 'Ids_DC':2e-9, 'Ids_X':4e-9, 'direction':'backward'})
        model.channel = 'Ids_X'
        model.layout = 'two'
        model.set_limits('Ids_X', (-1,2), (0,5e-9))
        model.set_limits('Ids_DC', (-1,2), (-1e-9,3e-9))
        original = model.exportSnapshot()
        self.assertEqual(original['signal'], 'Ids_X')
        self.assertEqual(original['view']['layout'], 'two')
        self.assertEqual(original['view']['channel_order'], ['Ids_X','Ids_DC'])
        archive = self.root / 'two.npz'
        save_snapshot(original, archive)
        frozen = load_snapshot(archive)
        exported = export_analysis(frozen, self.root / 'live-two.png')
        backend = HistoryBackend(self.root, settings=QtCore.QSettings(str(self.root / 'two.ini'), QtCore.QSettings.IniFormat), exporter=FakeExporter(), autostart=False)
        self.addCleanup(backend.shutdown)
        backend.restoreView(exported['view_path'])
        end = time.monotonic() + 5
        while time.monotonic() < end:
            self.app.processEvents()
            if not backend.busy and not backend._index_active and backend._snapshot: break
            time.sleep(.005)
        self.assertIsNotNone(backend._snapshot, backend.status)
        self.assertEqual(backend.plotModel.layout, 'two')
        self.assertEqual(backend.plotModel.channels(), ['Ids_X','Ids_DC'])
        restored = backend.export_snapshot()
        self.assertEqual(restored['signal'], 'Ids_X')
        for channel in ('Ids_X','Ids_DC'):
            old, new = frozen['panels'][channel]['traces'], restored['panels'][channel]['traces']
            np.testing.assert_allclose(np.concatenate([t['x'] for t in new]), np.concatenate([t['x'] for t in old]))
            np.testing.assert_allclose(np.concatenate([t['y'] for t in new]), np.concatenate([t['y'] for t in old]))
            np.testing.assert_allclose(backend.plotModel.limits(channel), model.limits(channel))
        self.assertEqual(len(build_analysis_figure(restored).axes), 2)
        again = export_analysis(restored, self.root / 'two-again.png')
        old = list(csv.reader(io.StringIO(Path(exported['csv_outputs'][0]).read_text(encoding='utf-8-sig'))))
        new = list(csv.reader(io.StringIO(Path(again['csv_outputs'][0]).read_text(encoding='utf-8-sig'))))
        self.assertEqual(old[:2], new[:2])
        self.assertEqual(old[3:], new[3:])
        self.assertEqual(self.path.read_bytes(), source)
        backend.setOption('signal','Ids_X')
        self.assertEqual(backend._views['vds_sweep']['layout'], 'single')

    def test_two_first_channel_missing_still_exports_second_and_warns(self):
        from app.png_export import export_analysis
        self.path.write_text('Vds,Ids_X,Direction\nV,A,\n0,3e-9,forward\n1,4e-9,forward\n', encoding='utf-8')
        self.view.update(layout='two')
        snapshot = self.snapshot()
        self.assertFalse(snapshot['traces'])
        self.assertTrue(snapshot['panels']['Ids_X']['traces'])
        self.assertTrue(snapshot['warnings'])
        output = export_analysis(snapshot, self.root / 'two-missing.png')
        rows = list(csv.reader(io.StringIO(Path(output['csv_outputs'][0]).read_text(encoding='utf-8-sig'))))
        self.assertIn('Ids_X', rows[0])
        self.assertNotIn('Ids_DC', rows[0])

    def test_two_snapshot_renders_in_separate_hardware_free_process(self):
        from PySide6.QtTest import QTest
        from app.ui.png_export_process import PngExportProcess
        self.view.update(layout='two')
        queue = PngExportProcess()
        completed, ready = [], []
        queue.completed.connect(completed.append)
        queue.ready.connect(ready.append)
        try:
            queue.submit_snapshot(self.snapshot(), self.root / 'detached-two.png')
            deadline = time.monotonic() + 25
            while time.monotonic() < deadline and not completed:
                self.app.processEvents()
                QTest.qWait(20)
            self.assertTrue(completed, 'detached two-plot export timed out')
            self.assertNotIn('error', completed[0], completed[0])
            self.assertNotEqual(ready[0]['pid'], os.getpid())
            self.assertEqual(ready[0]['hardware_modules'], [])
            view = json.loads(Path(completed[0]['view_path']).read_text(encoding='utf-8'))
            self.assertEqual(view['view']['layout'], 'two')
            self.assertEqual(view['view']['overlay_channels'], ['Ids_DC', 'Ids_X'])
        finally:
            queue.shutdown()
