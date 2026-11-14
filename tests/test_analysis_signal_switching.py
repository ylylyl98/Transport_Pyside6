"""Regressions for analysis signal changes across plot layouts."""
import json
import os
from pathlib import Path
import tempfile
import time
import unittest

if os.environ.get('TRANSPORT_OFFLINE_VERIFICATION') != '1':
    raise unittest.SkipTest('Run through verify_offline.py')

from PySide6 import QtCore

from app.quick.history import HistoryBackend
from tests.test_quick_history import FakeExporter


class AnalysisSignalSwitchingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        settings = QtCore.QSettings(str(self.root / 'settings.ini'), QtCore.QSettings.IniFormat)
        self.backend = HistoryBackend(self.root, settings=settings, exporter=FakeExporter(), autostart=False)
        self.addCleanup(self.backend.shutdown)

    def drain(self):
        end = time.monotonic() + 5
        while time.monotonic() < end:
            QtCore.QCoreApplication.processEvents()
            if not self.backend.busy:
                return
            time.sleep(.005)
        self.fail('Analysis worker did not settle: ' + self.backend.status)

    def select_data(self, kind='bfield_transport', mode='ordinary'):
        axis = {'vds_sweep': 'Vds', 'gate_scan': 'Doping', 'bfield_gate_scan': 'Doping',
                'bfield_transport': 'B_measured_T', 'photocurrent': 'Wavelength', 'map_2d': 'Vtg'}[kind]
        path = self.root / (kind + '.csv')
        path.write_text(f'{axis},Vbg,Ids_DC,I_drag_X,I_drive_X,Sample_temperature_K\n'
                        '0,0,1,10,100,4\n1,0,2,20,200,5\n'
                        '0,1,3,30,300,6\n1,1,4,40,400,7\n', encoding='utf-8')
        path.with_name(path.stem + '_metadata.json').write_text(json.dumps({
            'measurement': kind, 'signal_chain': {'drag_drive': {'mode': mode}},
            'params': {'axis_fast': 'Vtg', 'axis_slow': 'Vbg'},
        }), encoding='utf-8')
        self.backend.selectType(kind)
        self.backend.refresh(); self.drain()
        self.backend.setChecked(str(path), True); self.drain()
        return path

    def assert_signal(self, signal, expected):
        self.assertEqual(self.backend.state['signal'], signal)
        self.assertEqual(self.backend.plotModel.channel, signal)
        snapshot = self.backend.export_snapshot()
        self.assertEqual(snapshot['signal'], signal)
        self.assertEqual(snapshot['view']['signal'], signal)
        self.assertEqual(snapshot['traces'][0]['y'].tolist(), expected)
        displayed = list(self.backend.plotModel.traces(signal))
        self.assertEqual(displayed[0][2].values.tolist(), expected)

    def test_signal_selector_changes_single_bfield_plot_and_export(self):
        path = self.select_data()
        original = path.read_bytes()
        for signal, values in [('Sample_temperature_K', [4, 5, 6, 7]), ('Ids_DC', [1, 2, 3, 4])]:
            with self.subTest(signal=signal):
                self.backend.setOption('signal', signal); self.drain()
                self.assert_signal(signal, values)
                self.assertEqual(self.backend.plotModel.layout, 'single')
        self.assertEqual(path.read_bytes(), original)

    def test_signal_selector_leaves_overlay_two_and_four_without_stale_panels(self):
        self.select_data()
        plot = self.backend.plotModel
        for layout in ('overlay', 'two', 'four'):
            with self.subTest(layout=layout):
                plot.setYAxes('Ids_DC', 'I_drag_X', 'two' if layout == 'two' else 'overlay')
                if layout == 'four':
                    plot.setPlotCount(4)
                self.drain()
                self.assertEqual(plot.layout, layout)
                self.backend.setOption('signal', 'Sample_temperature_K'); self.drain()
                self.assert_signal('Sample_temperature_K', [4, 5, 6, 7])
                self.assertEqual(plot.layout, 'single')
                self.assertFalse(self.backend.state['four_channels'])
                self.assertNotIn('panels', self.backend.export_snapshot())

    def test_axis_signal_choice_overrides_role_default_and_survives_reload(self):
        self.select_data(mode='single_drag')
        self.assertEqual(self.backend.state['signal'], 'I_drag_X')
        self.backend.plotModel.setPanelChannel('I_drag_X', 'Ids_DC'); self.drain()
        self.assert_signal('Ids_DC', [1, 2, 3, 4])
        self.assertTrue(self.backend.state['signal_explicit'])
        session = self.backend.capture_session()
        self.backend.restore_session(session); self.drain()
        self.assert_signal('Ids_DC', [1, 2, 3, 4])

    def test_four_to_single_plot_drops_four_channel_worker_state(self):
        self.select_data()
        plot = self.backend.plotModel
        plot.setPlotCount(4); self.drain()
        self.assertTrue(self.backend.state['four_channels'])
        plot.setYAxes('Sample_temperature_K', ''); self.drain()
        self.assert_signal('Sample_temperature_K', [4, 5, 6, 7])
        self.assertFalse(self.backend.state['four_channels'])
        self.assertNotIn('panels', self.backend.export_snapshot())

    def test_qml_signal_selector_changes_displayed_bfield_data(self):
        from PySide6 import QtQml, QtQuickControls2, QtTest
        from app.quick.plot import register_types

        self.select_data()
        QtQuickControls2.QQuickStyle.setStyle('Fusion')
        register_types()
        engine = QtQml.QQmlApplicationEngine()
        warnings = []
        engine.warnings.connect(lambda rows: warnings.extend(str(row) for row in rows))
        engine.rootContext().setContextProperty('historyBackend', self.backend)
        engine.rootContext().setContextProperty('historyConnected', False)
        engine.load(QtCore.QUrl.fromLocalFile(str(Path(__file__).resolve().parents[1] /
                                                'app/quick/qml/HistoryWindow.qml')))
        self.assertTrue(engine.rootObjects(), warnings)
        window = engine.rootObjects()[0]
        try:
            QtTest.QTest.qWait(80)
            selector = window.findChild(QtCore.QObject, 'historySignal')
            self.assertTrue(selector.property('enabled'))
            # Exercise the actual QML onActivated handler, not only the slot.
            index = self.backend.signalChoices.index('Sample_temperature_K')
            selector.setProperty('currentIndex', index)
            self.assertTrue(QtCore.QMetaObject.invokeMethod(selector, 'activated', QtCore.Q_ARG(int, index)))
            self.drain()
            self.assert_signal('Sample_temperature_K', [4, 5, 6, 7])
            self.assertIn('Sample_temperature_K', self.backend.plotModel.yLabel)
            self.assertEqual(warnings, [])
        finally:
            window.hide()
            engine.deleteLater()
            QtCore.QCoreApplication.sendPostedEvents(None, QtCore.QEvent.DeferredDelete)

    def test_overlay_first_signal_choice_overrides_role_default(self):
        self.select_data(mode='single_drive')
        self.backend.plotModel.setYAxes('Ids_DC', 'Sample_temperature_K'); self.drain()
        self.assertEqual(self.backend.state['signal'], 'Ids_DC')
        self.assertTrue(self.backend.state['signal_explicit'])
        self.backend.plotModel.setYAxes('Ids_DC', ''); self.drain()
        self.assert_signal('Ids_DC', [1, 2, 3, 4])

    def test_signal_selector_updates_other_curve_analysis_types(self):
        for kind in ('vds_sweep', 'gate_scan', 'bfield_gate_scan', 'photocurrent', 'map_2d'):
            with self.subTest(kind=kind):
                self.select_data(kind)
                if kind == 'map_2d':
                    self.backend.setOption('map_y', 'None (1D)'); self.drain()
                self.backend.setOption('signal', 'I_drag_X'); self.drain()
                self.assert_signal('I_drag_X', [10, 20, 30, 40])

    def test_map_signal_change_resets_color_and_preserves_coordinate_ranges(self):
        self.select_data('map_2d')
        self.backend.setRange('x', False, '-0.25', '0.75')
        self.backend.setRange('y', False, '-0.5', '1.5')
        self.backend.setRange('color', False, '1', '2')
        self.backend.setOption('signal', 'I_drag_X'); self.drain()
        snapshot = self.backend.export_snapshot()
        self.assertEqual(snapshot['signal'], 'I_drag_X')
        self.assertEqual(snapshot['map']['z'].tolist(), [[10, 20], [30, 40]])
        self.assertTrue(snapshot['view']['clim_auto'])
        self.assertEqual(snapshot['view']['map_xlim'], [-0.25, 0.75])
        self.assertEqual(snapshot['view']['map_ylim'], [-0.5, 1.5])


if __name__ == '__main__':
    unittest.main()
