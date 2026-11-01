"""Hardware-free Analysis selector and stable-file-list integration tests."""
import json
import os
from pathlib import Path
from unittest.mock import patch
import tempfile
import time
import unittest

if os.environ.get('TRANSPORT_OFFLINE_VERIFICATION') != '1':
    raise unittest.SkipTest('Run through verify_offline.py')

from PySide6 import QtCore, QtQml, QtTest, QtWidgets
from PySide6.QtQuickControls2 import QQuickStyle
from app.quick.history import HistoryBackend
from app.quick.plot import register_types
from tests.test_quick_history import FakeExporter


class QuickAnalysisDataTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
        QQuickStyle.setStyle('Fusion')
        register_types()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.settings = QtCore.QSettings(str(self.root / 'settings.ini'), QtCore.QSettings.IniFormat)
        self.backend = HistoryBackend(self.root, settings=self.settings, exporter=FakeExporter(), autostart=False)
        self.engine = QtQml.QQmlApplicationEngine()
        self.warnings = []
        self.engine.warnings.connect(lambda errors: self.warnings.extend(str(e) for e in errors))
        self.engine.rootContext().setContextProperty('historyBackend', self.backend)
        self.engine.rootContext().setContextProperty('historyConnected', False)

    def write(self, name, kind, text, metadata=None):
        path = self.root / name
        path.write_text(text, encoding='utf-8')
        data = {'measurement': kind, 'created_at': '2026-10-26T12:30:00', 'status': 'finished'}
        data.update(metadata or {})
        path.with_name(path.stem + '_metadata.json').write_text(json.dumps(data), encoding='utf-8')
        return path

    def load(self):
        url = QtCore.QUrl.fromLocalFile(str(Path(__file__).resolve().parents[1] / 'app/quick/qml/HistoryWindow.qml'))
        self.engine.load(url)
        self.assertTrue(self.engine.rootObjects(), self.warnings)
        self.window = self.engine.rootObjects()[0]
        self.window.requestActivate()
        QtTest.QTest.qWait(80)

    def drain(self):
        end = time.monotonic() + 6
        while time.monotonic() < end:
            self.app.processEvents()
            if not self.backend.busy and not self.backend._index_active:
                self.app.processEvents()
                return
            time.sleep(.005)
        self.fail('Analysis worker did not settle: ' + self.backend.status)

    def item(self, name):
        item = self.window.findChild(QtCore.QObject, name)
        self.assertIsNotNone(item, name)
        return item

    def tearDown(self):
        if self.engine.rootObjects(): self.engine.rootObjects()[0].hide()
        self.backend.shutdown()
        self.engine.deleteLater()
        self.app.sendPostedEvents(None, QtCore.QEvent.DeferredDelete)
        self.temp.cleanup()
        self.assertEqual(self.warnings, [])

    def test_selection_controls_reflect_actual_signal_and_plot_fallback(self):
        a = self.write('a.csv', 'vds_sweep',
                       'Vds,Ids_DC,Ids_X,I_drag_X,I_drive_X,Direction\n'
                       '0,1,nan,2,0,forward\n1,2,nan,4,0,forward\n')
        b = self.write('b.csv', 'vds_sweep', 'Vds,Ids_X,Direction\n0,5,forward\n1,6,forward\n')
        original = {p: p.read_bytes() for p in (a, b)}
        self.load()
        self.backend.refresh()
        self.drain()
        select = self.item('historySignal')
        self.assertFalse(select.property('enabled'))
        self.assertEqual(self.backend.signalChoices, [])
        self.backend.setChecked(str(a), True)
        self.drain()
        self.assertTrue(select.property('enabled'))
        self.assertIn('Ids_DC', self.backend.signalChoices)
        self.assertNotIn('Ids_X', self.backend.signalChoices, 'all NaN signal is unavailable')
        self.assertNotIn('Drag ratio X', self.backend.signalChoices, 'zero Drive ratio cannot plot')
        self.assertEqual(select.property('currentText'), 'Ids_DC')
        self.assertEqual(self.backend._snapshot['signal'], 'Ids_DC')
        self.backend.setChecked(str(a), False)
        self.backend.setChecked(str(b), True)
        self.drain()
        self.assertEqual(self.backend.signalChoices, ['Ids_X'])
        self.assertEqual(select.property('currentText'), 'Ids_X')
        self.assertEqual(self.backend.state['signal'], 'Ids_X')
        self.assertEqual(self.backend._snapshot['signal'], 'Ids_X')
        self.assertEqual(self.backend.plotModel._traces[0].columns['y'].values.tolist(), [5., 6.])
        self.assertTrue(any('Ids_DC' in warning for warning in self.backend._snapshot['warnings']))
        self.backend.setChecked(str(b), False)
        self.drain()
        self.assertEqual(self.backend.signalChoices, [])
        self.assertFalse(select.property('enabled'))
        self.assertEqual({p: p.read_bytes() for p in (a, b)}, original)

    def test_primary_map_changes_available_signal_and_rendered_data(self):
        metadata = {'params': {'axis_fast': 'Vtg', 'axis_slow': 'Vbg'}}
        a = self.write('a.csv', 'map_2d', 'Vtg,Vbg,Ids_DC\n0,0,1\n1,0,2\n0,1,3\n1,1,4\n', metadata)
        b = self.write('b.csv', 'map_2d', 'Vtg,Vbg,Ids_X\n0,0,5\n1,0,6\n0,1,7\n1,1,8\n', metadata)
        self.load()
        self.backend.refresh()
        self.drain()
        self.backend.selectType('map_2d')
        self.drain()
        self.backend.setChecked(str(a), True)
        self.backend.setChecked(str(b), True)
        self.drain()
        self.backend.setOption('primary_path', str(a))
        self.drain()
        self.assertEqual(self.backend.signalChoices, ['Ids_DC'])
        self.assertEqual(self.backend.state['signal'], 'Ids_DC')
        self.assertEqual(self.backend._snapshot['map']['path'], str(a))
        self.backend.setOption('primary_path', str(b))
        self.drain()
        self.assertEqual(self.backend.signalChoices, ['Ids_X'])
        self.assertEqual(self.backend.state['signal'], 'Ids_X')
        self.assertEqual(self.backend._snapshot['map']['path'], str(b))
        self.assertEqual(self.item('historySignal').property('currentText'), 'Ids_X')

    def test_rapid_selection_dispatches_latest_and_shutdown_cancels_queued_work(self):
        from app.quick.history import _read_display
        a = self.write('a.csv', 'vds_sweep', 'Vds,Ids_DC\n0,1\n1,2\n')
        b = self.write('b.csv', 'vds_sweep', 'Vds,Ids_DC\n0,5\n1,6\n')
        self.load()
        self.backend.refresh()
        self.drain()
        with patch('app.quick.history._read_display', wraps=_read_display) as read:
            self.backend.setChecked(str(a), True)
            self.backend.setChecked(str(b), True)
            self.backend.clearSelection()
            self.backend.setChecked(str(b), True)
            self.assertTrue(self.backend.busy)
            self.assertFalse(self.backend.exportReady)
            self.assertEqual(read.call_count, 0, 'dispatch must yield to UI notifications first')
            self.drain()
            self.assertEqual(read.call_count, 1)
            self.assertEqual([r['path'] for r in self.backend._snapshot['records']], [str(b)])
            self.assertTrue(self.backend.exportReady)
            self.backend.setChecked(str(a), True)
            self.assertTrue(self.backend.busy)
            self.assertFalse(self.backend.exportReady)
            self.assertTrue(self.backend._plot_dispatch.isActive())
            self.backend.shutdown()
            self.app.processEvents()
            self.assertFalse(self.backend._plot_dispatch.isActive())
            self.assertEqual(read.call_count, 1, 'shutdown cancels undispatched selection work')

    def test_four_channel_missing_signal_is_not_replaced_with_available_signal(self):
        from app.quick.history import _defaults, _read_display
        from app.curve_history import discover_history
        from app.curve_cache import SnapshotCache
        self.write('only.csv', 'vds_sweep', 'Vds,Ids_DC\n0,1\n1,2\n')
        records, _ = discover_history(self.root)
        view = _defaults('vds_sweep')
        view.update(four_channels=True, channel_order=['Ids_DC', 'Ids_X', 'Ids_Y', 'Ids_Keithley'],
                    channel_views={})
        snapshot, _, _ = _read_display(records, view, 'vds_sweep', SnapshotCache())
        self.assertEqual(snapshot['panels']['Ids_DC']['signal'], 'Ids_DC')
        self.assertTrue(snapshot['panels']['Ids_DC']['traces'])
        for channel in ('Ids_X', 'Ids_Y', 'Ids_Keithley'):
            self.assertEqual(snapshot['panels'][channel]['signal'], channel)
            self.assertEqual(snapshot['panels'][channel]['traces'], [])
            self.assertTrue(snapshot['panels'][channel]['warnings'])

    def test_check_updates_stable_model_and_keeps_qml_delegate(self):
        a = self.write('a.csv', 'vds_sweep', 'Vds,Ids_DC\n0,1\n1,2\n')
        self.write('b.csv', 'vds_sweep', 'Vds,Ids_DC\n0,2\n1,3\n')
        self.load()
        self.backend.refresh()
        self.drain()
        model = self.backend.fileModel
        reset = QtTest.QSignalSpy(model.modelReset)
        changed = QtTest.QSignalSpy(model.dataChanged)
        view = self.item('historyFileList')
        content = view.property('contentItem')
        def delegates():
            return [item for item in content.childItems() if item.metaObject().className().startswith('QQuickRectangle')
                    and item.width() > 100 and item.height() > 20]
        QtTest.QTest.qWait(50)
        before = delegates()
        self.assertTrue(before)
        self.backend.setChecked(str(a), True)
        self.drain()
        self.assertEqual(reset.count(), 0)
        self.assertEqual(changed.count(), 1)
        self.assertTrue(any(item is before[0] for item in delegates()))
        revision = self.backend._revision
        self.backend.setChecked(str(a), True)
        self.app.processEvents()
        self.assertEqual(self.backend._revision, revision)
        self.assertEqual(changed.count(), 1)
