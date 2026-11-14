import json
import tempfile
import time
import unittest
from pathlib import Path

import numpy as np
from PySide6 import QtCore, QtQml, QtQuickControls2, QtTest

from app.quick.history import HistoryBackend
from app.quick.plot import register_types
from tests.test_quick_history import FakeExporter


class BFieldAnalysisAxesTests(unittest.TestCase):
    def test_saved_channels_independent_axes_and_export(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            path = root / 'field.csv'
            path.write_text('B_measured_T,Ids_DC,raw_X,Sample_temperature_K,Reservoir_temperature_K,Direction\n'
                            '0,1,0.1,4,,forward\n1,2,0.2,5,,forward\n', encoding='utf-8')
            path.with_name('field_metadata.json').write_text(json.dumps({'measurement': 'bfield_transport'}), encoding='utf-8')
            settings = QtCore.QSettings(str(root / 'settings.ini'), QtCore.QSettings.Format.IniFormat)
            backend = HistoryBackend(root, settings=settings, exporter=FakeExporter(), autostart=False)
            self.addCleanup(backend.shutdown)

            def drain():
                end = time.monotonic() + 5
                while time.monotonic() < end:
                    QtCore.QCoreApplication.processEvents()
                    if not backend.busy:
                        return
                    time.sleep(.005)
                self.fail('History worker did not settle')

            backend.selectType('bfield_transport')
            backend.refresh(); drain()
            backend.setChecked(str(path), True); drain()
            self.assertIn('Sample_temperature_K', backend.signalChoices)
            self.assertNotIn('Reservoir_temperature_K', backend.signalChoices)
            self.assertNotIn('Vds_measured', backend.signalChoices)
            plot = backend.plotModel
            plot.setYAxes('Ids_DC', '')
            QtQuickControls2.QQuickStyle.setStyle('Fusion')
            register_types()
            engine = QtQml.QQmlApplicationEngine()
            warnings = []
            engine.warnings.connect(lambda rows: warnings.extend(str(row) for row in rows))
            engine.rootContext().setContextProperty('historyBackend', backend)
            engine.rootContext().setContextProperty('historyConnected', False)
            engine.load(QtCore.QUrl.fromLocalFile(str(Path(__file__).resolve().parents[1] / 'app/quick/qml/HistoryWindow.qml')))
            self.assertTrue(engine.rootObjects(), warnings)
            window = engine.rootObjects()[0]
            QtQml.QQmlProperty(window, 'width').write(1100)
            QtQml.QQmlProperty(window, 'height').write(760)
            QtTest.QTest.qWait(150)
            button = window.findChild(QtCore.QObject, 'plotSecondYButton')
            self.assertIsNotNone(button)
            self.assertTrue(button.property('visible'))
            def click(item):
                self.assertIsNotNone(item)
                position = item.mapToScene(QtCore.QPointF(item.width()/2, item.height()/2)).toPoint()
                QtTest.QTest.mouseClick(window, QtCore.Qt.LeftButton, QtCore.Qt.NoModifier, position)
                QtTest.QTest.qWait(50)
            click(button)
            # Popup delegates may be visually parented to the overlay rather
            # than in the window's QObject ownership tree.
            def descendants(item):
                yield item
                for child in item.childItems():
                    yield from descendants(child)
            choice = next((item for item in descendants(window.contentItem())
                           if item.objectName() == 'axisChoice_Sample_temperature_K'), None)
            click(choice)
            drain()
            self.assertEqual(plot.channel, 'Ids_DC')
            snapshot = backend.export_snapshot()
            self.assertEqual(snapshot['view']['overlay_axis'], 'dual')
            self.assertEqual(snapshot['view']['overlay_channels'], ['Ids_DC', 'Sample_temperature_K'])
            self.assertEqual(snapshot['panels']['Ids_DC']['y_unit'], 'A')
            self.assertEqual(snapshot['panels']['Sample_temperature_K']['y_unit'], 'K')
            np.testing.assert_equal(snapshot['panels']['Sample_temperature_K']['traces'][0]['y'], [4, 5])
            from app.png_export import build_analysis_figure
            figure = build_analysis_figure(snapshot)
            self.assertEqual(len(figure.axes), 2)
            self.assertIn('(A)', figure.axes[0].get_ylabel())
            self.assertIn('(K)', figure.axes[1].get_ylabel())
            np.testing.assert_equal(figure.axes[1].lines[0].get_ydata(), [4, 5])
            figure.clear()
            self.assertTrue(window.grabWindow().save(str(Path('tmp/verification/bfield_analysis_axes.png'))))
            self.assertEqual(warnings, [])
            window.setProperty('visible', False)
            backend.shutdown()
