"""Live overlay state, actual QML interaction and native two-axis geometry."""
import os
from pathlib import Path
import unittest

if os.environ.get('TRANSPORT_OFFLINE_VERIFICATION') != '1':
    raise unittest.SkipTest('Use verify_offline.py')

import numpy as np
from PySide6 import QtCore, QtGui, QtQml, QtQuickControls2, QtTest, QtWidgets
from app.quick.plot import PlotModel, QuickPlot, COLORS, register_types


def seed(model, count=11):
    model.beginRun({'x_axis': 'Vtg', 'params': {'vds_source': 'Keithley 2400'},
                    'channels': ['Ids_DC', 'Ids_X', 'Ids_Y', 'Ids_Keithley']})
    for i in range(count):
        model.appendRecord({'vtg': i, 'Ids_DC': float(i), 'Ids_X': 1000. * (count-i),
                            'Ids_Y': -float(i), 'Ids_Keithley': i / 100.,
                            'condition': 1, 'direction': 'forward' if i < count // 2 else 'backward'})


class OverlayModelTests(unittest.TestCase):
    def setUp(self):
        self.model = PlotModel()
        seed(self.model)

    def tearDown(self):
        self.model._timer.stop()

    def test_pair_changes_reuse_all_acquired_samples_and_shared_bounds(self):
        p = self.model
        original = p._traces.copy()
        p.layout = 'overlay'
        self.assertEqual(p.channels(), ['Ids_DC', 'Ids_X'])
        self.assertFalse(p.fourChannels)
        np.testing.assert_equal(p.limits('Ids_DC'), p.limits('Ids_X'))
        self.assertLess(p.limits()[1][0], 0)
        self.assertGreater(p.limits()[1][1], 11000.)
        p.setOverlayChannel(1, 'Ids_Y')
        self.assertEqual(p.channels(), ['Ids_DC', 'Ids_Y'])
        self.assertEqual(p._traces, original)
        self.assertEqual(sum(y.size for _, _, y in p.traces('Ids_Y')), 11)
        p.appendRecord({'vtg': 11, 'Ids_DC': 11, 'Ids_Y': -11, 'direction': 'backward', 'condition': 1})
        self.assertEqual(p.pointCount, 12)
        self.assertEqual(sum(y.size for _, _, y in p.traces('Ids_Y')), 12)
        p.setOverlayChannel(1, 'Ids_DC')
        p.setOverlayChannel(1, 'not_a_signal')
        self.assertEqual(p.channels(), ['Ids_DC', 'Ids_Y'])

    def test_return_to_four_restores_individual_zoom_and_overlay_pair(self):
        p = self.model
        p.fourChannels = True
        p.set_limits('Ids_DC', (2, 5), (1, 6))
        p.set_limits('Ids_X', (2, 5), (100, 400))
        four = {c: p.limits(c) for c in p.channels()}
        p.layout = 'overlay'
        p.setOverlayChannel(1, 'Ids_Y')
        p.set_limits('Ids_DC', (0, 8), (-5, 5))
        p.layout = 'four'
        self.assertEqual({c: p.limits(c) for c in p.channels()}, four)
        p.layout = 'overlay'
        self.assertEqual(p.channels(), ['Ids_DC', 'Ids_Y'])
        self.assertEqual(p.limits(), ((0., 8.), (-5., 5.)))
        p.overlayAxis = 'dual'
        self.assertNotEqual(p.limits('Ids_DC')[1], p.limits('Ids_Y')[1])
        p.set_limits('Ids_DC', (1, 3), (-1, 4))
        p.set_limits('Ids_Y', (1, 3), (-10, 0))
        self.assertEqual(p.limits('Ids_DC')[1], (-1., 4.))
        self.assertEqual(p.limits('Ids_Y')[1], (-10., 0.))
        p.overlayAxis = 'shared'
        self.assertEqual(p.limits('Ids_DC'), p.limits('Ids_Y'))

    def test_waiting_data_stays_missing_and_channel_options_follow_source(self):
        p = self.model
        p.beginRun({'channels': ['Ids_DC', 'Ids_X', 'Ids_Y'], 'x_axis': 'Vtg'})
        p.layout = 'overlay'
        p.appendRecord({'vtg': 0, 'Ids_DC': 1, 'Ids_X': np.nan})
        self.assertIn('Ids_X', p.overlayStatus)
        self.assertNotIn('Ids_Keithley', p.channelOptions)
        self.assertIsNone(list(p.traces('Ids_X'))[0][2].bounds)
        p.appendRecord({'vtg': 1, 'Ids_DC': 2, 'Ids_X': 3})
        self.assertNotIn('Waiting', p.overlayStatus)
        self.assertTrue(np.isnan(list(p.traces('Ids_X'))[0][2].values[0]))

    def test_preferences_and_view_roundtrip_with_legacy_defaults(self):
        p = self.model
        p.applyPreferences({'fourChannels': True})
        self.assertEqual(p.layout, 'four')
        p.layout = 'overlay'
        p.setOverlayChannel(0, 'Ids_Y')
        p.overlayAxis = 'dual'
        p.set_limits('Ids_Y', (2, 5), (-8, 2))
        p.set_limits('Ids_X', (2, 5), (100, 200))
        clone = PlotModel()
        self.addCleanup(lambda: clone._timer.stop())
        clone.applyPreferences(p.capturePreferences())
        self.assertEqual(clone.capturePreferences(), p.capturePreferences())
        clone.applyView(p.captureView())
        self.assertEqual(clone.channels(), p.channels())
        for channel in p.channels():
            self.assertEqual(clone.limits(channel), p.limits(channel))
        p.applyPreferences({'xAxis': 'Vbg'})
        self.assertEqual(p.layout, 'overlay', 'partial axis preference must retain overlay')
        p.beginRun({'plot_choice': 'Ids_DC', 'x_axis': 'Vtg'})
        self.assertEqual(p.channel, 'Ids_Y', 'new run must retain chosen first overlay signal')

    def test_snapshot_identity_and_missing_first_channel_are_never_substituted(self):
        p = self.model
        p.setTraces([{'x': [0., 1.], 'y': [2., 3.], 'channel': 'Ids_X'}], 'Vds (V)', 'Ids_DC (A)')
        p.layout = 'overlay'
        p.setOverlayChannel(0, 'Ids_Y')
        snapshot = p.exportSnapshot()
        self.assertEqual(snapshot['signal'], 'Ids_Y')
        self.assertEqual(snapshot['traces'], [])
        self.assertEqual(snapshot['panels']['Ids_Y']['traces'], [])
        self.assertEqual(snapshot['panels']['Ids_X']['traces'][0]['y'].tolist(), [2., 3.])
        self.assertFalse(snapshot['panels']['Ids_X']['traces'][0]['y'].flags.writeable)

    def test_direct_y_axes_publish_once_and_preserve_four_plot_zoom(self):
        p = self.model
        p.layout = 'four'
        p.set_limits('Ids_DC', (2, 5), (1, 6))
        four = {c: p.limits(c) for c in p.channels()}
        preferences = QtTest.QSignalSpy(p.preferencesChanged)
        changed = QtTest.QSignalSpy(p.changed)
        views = QtTest.QSignalSpy(p.viewChanged)
        records = p._traces.copy()
        p.setYAxes('Ids_Y', 'Ids_Keithley')
        self.assertEqual((preferences.count(), changed.count(), views.count()), (1, 1, 1))
        self.assertEqual(p.layout, 'overlay')
        self.assertEqual(p.channel, 'Ids_Y')
        self.assertEqual(p.channels(), ['Ids_Y', 'Ids_Keithley'])
        self.assertEqual(p.exportSnapshot()['signal'], 'Ids_Y')
        p.setYAxes('Ids_DC', 'Ids_DC')
        self.assertEqual(preferences.count(), 1, 'invalid pair must not publish a view')
        p.setYAxes('Ids_Y', '')
        self.assertEqual(p.layout, 'single')
        self.assertEqual(p.channel, 'Ids_Y')
        self.assertEqual(p.overlayChannels, ['Ids_Y', 'Ids_Keithley'])
        p.setPlotCount(4)
        self.assertEqual({c: p.limits(c) for c in p.channels()}, four)
        self.assertEqual(p._traces, records)

    def test_two_plots_keep_independent_ranges_and_roundtrip_preferences(self):
        p = self.model
        p.setYAxes('Ids_DC', 'Ids_X')
        p.setPlotCount(2)
        self.assertEqual(p.layout, 'two')
        self.assertEqual(p.plotCount, 2)
        p.set_limits('Ids_DC', (1, 3), (-1, 5))
        p.set_limits('Ids_X', (1, 3), (100, 200))
        p.setYAxes('Ids_Y', 'Ids_X')
        self.assertEqual(p.layout, 'two')
        self.assertEqual(p.limits('Ids_X')[1], (100., 200.))
        p.set_limits('Ids_Y', (1, 3), (-6, 0))
        two = {c: p.limits(c) for c in p.channels()}
        p.setPlotCount(4)
        p.setPlotCount(2)
        self.assertEqual({c: p.limits(c) for c in p.channels()}, two)
        clone = PlotModel()
        self.addCleanup(lambda: clone._timer.stop())
        clone.applyPreferences(p.capturePreferences())
        clone.applyView(p.captureView())
        self.assertEqual(clone.layout, 'two')
        self.assertEqual(clone.channels(), p.channels())
        self.assertEqual({c: clone.limits(c) for c in clone.channels()}, two)
        clone.setPlotCount(1)
        self.assertEqual(clone.layout, 'overlay')
        p.applyPreferences({'xAxis': 'Vtg'})
        self.assertEqual(p.layout, 'two')
        p.setPlotCount(1)
        self.assertEqual(p.layout, 'overlay', 'return to the previous one-plot arrangement')


    def test_panel_channel_replaces_only_target_and_persists_four_order(self):
        p = self.model
        p.layout = 'four'
        old = p.channels()
        p._channel_order = old.copy()
        p._channels.append('I_drag_X')
        for i, channel in enumerate(old):
            p.set_limits(channel, (1, 4), (i, i + 2))
        bounds = {channel: p.limits(channel) for channel in old}
        traces = p._traces.copy()
        notices = []
        p.preferencesChanged.connect(lambda: notices.append('preferences'))
        p.viewChanged.connect(lambda: notices.append('view'))
        p.changed.connect(lambda: notices.append('changed'))
        p.setPanelChannel(old[1], 'I_drag_X')
        self.assertEqual(notices, ['preferences', 'view', 'changed'])
        order = old.copy()
        order[1] = 'I_drag_X'
        self.assertEqual(p.channels(), order)
        for channel in (old[0], old[2], old[3]):
            self.assertEqual(p.limits(channel), bounds[channel])
        self.assertEqual(p._traces, traces)
        p.setPanelChannel(old[0], old[2])
        p.setPanelChannel(old[0], 'invalid')
        self.assertEqual(p.channels(), order)
        self.assertEqual(len(notices), 3)
        clone = PlotModel()
        self.addCleanup(lambda: clone._timer.stop())
        clone.applyPreferences(p.capturePreferences())
        self.assertEqual(clone.channels(), order)
        clone.beginRun({'channels': p.channelOptions, 'x_axis': 'Vtg'})
        self.assertEqual(clone.channels(), order)
        p.layout = 'two'
        p.layout = 'four'
        self.assertEqual(p.channels(), order)

    def test_panel_channel_single_and_pair_are_atomic_and_preserve_other_y(self):
        p = self.model
        p.setPanelChannel('Ids_DC', 'Ids_Y')
        self.assertEqual(p.channels(), ['Ids_Y'])
        p.setYAxes('Ids_Y', 'Ids_X', 'two')
        p.set_limits('Ids_Y', (2, 5), (-2, 2))
        old_limit = p.limits('Ids_Y')
        notices = []
        p.preferencesChanged.connect(lambda: notices.append('preferences'))
        p.viewChanged.connect(lambda: notices.append('view'))
        p.changed.connect(lambda: notices.append('changed'))
        p.setPanelChannel('Ids_X', 'Ids_DC')
        self.assertEqual(p.channels(), ['Ids_Y', 'Ids_DC'])
        self.assertEqual(p.limits('Ids_Y'), old_limit)
        self.assertEqual(notices, ['preferences', 'view', 'changed'])
        p.setPanelChannel('Ids_Y', 'Ids_DC')
        self.assertEqual(len(notices), 3)
        p.layout = 'overlay'
        p.setPanelChannel('Ids_Y', 'Ids_X')
        self.assertEqual(p.channels(), ['Ids_X', 'Ids_DC'])
        self.assertEqual(p.exportSnapshot()['signal'], 'Ids_X')


class OverlayQmlTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        QtQuickControls2.QQuickStyle.setStyle('Fusion')
        register_types()

    def setUp(self):
        from app.quick.application import ApplicationBackend
        self.backend = ApplicationBackend(offline=True)
        self.model = self.backend.plotModel
        self.model.applyPreferences(dict(layout='single', channel='Ids_DC', channelOrder=None,
                                         overlayChannels=['Ids_DC', 'Ids_X'], overlayAxis='shared',
                                         onePlotLayout='single', xAxis='Follow Sweep'))
        seed(self.model)
        self.model.layout = 'single'
        self.model.layout = 'four'
        self.engine = QtQml.QQmlApplicationEngine()
        self.warnings = []
        self.engine.warnings.connect(lambda rows: self.warnings.extend(str(r) for r in rows))
        self.engine.rootContext().setContextProperty('appBackend', self.backend)
        self.engine.load(QtCore.QUrl.fromLocalFile(str(Path(__file__).resolve().parents[1] / 'app/quick/qml/Main.qml')))
        self.assertTrue(self.engine.rootObjects(), self.warnings)
        self.window = self.engine.rootObjects()[0]
        self.window.setProperty('showInstruments', False)
        self.window.setProperty('pinInstruments', False)
        self.window.setWidth(1536)
        self.window.setHeight(864)
        self.window.requestActivate()
        QtTest.QTest.qWait(80)

    def item(self, name):
        def walk(item):
            yield item
            for child in item.childItems(): yield from walk(child)
        found = self.window.findChild(QtCore.QObject, name)
        if found is None:
            found = next((item for item in walk(self.window.contentItem()) if item.objectName() == name), None)
        self.assertIsNotNone(found, name)
        return found

    def click(self, item):
        position = item.mapToScene(QtCore.QPointF(item.width()/2, item.height()/2)).toPoint()
        QtTest.QTest.mouseClick(self.window, QtCore.Qt.LeftButton, QtCore.Qt.NoModifier, position)
        QtTest.QTest.qWait(50)

    def tearDown(self):
        self.backend.measurement._batch_claimed = False
        self.backend.measurement._phase = 'idle'
        self.window.hide()
        self.backend.requestClose()
        for _ in range(30): QtWidgets.QApplication.processEvents()
        self.assertTrue(self.backend.closeAccepted)
        self.engine.deleteLater()
        QtCore.QCoreApplication.sendPostedEvents(None, QtCore.QEvent.DeferredDelete)
        self.backend.deleteLater()
        QtCore.QCoreApplication.sendPostedEvents(None, QtCore.QEvent.DeferredDelete)
        self.assertEqual(self.warnings, [])

    def test_live_buttons_keyboard_channel_choice_and_narrow_layout(self):
        self.backend.measurement._phase = 'running'
        self.backend.measurement._batch_claimed = True
        self.backend.measurement.changed.emit()
        original = self.model._traces.copy()
        self.click(self.item('plotCount1'))
        self.assertEqual(self.model.layout, 'single')
        self.click(self.item('axisTitle_y_0'))
        self.assertTrue(self.item('plotAxisPopup').property('visible'))
        self.click(self.item('axisChoice_Ids_X'))
        self.assertEqual(self.model.channel, 'Ids_X')
        self.click(self.item('axisTitle_y_0'))
        second = self.item('axisTitleSecondY')
        second.forceActiveFocus()
        QtTest.QTest.keyClick(self.window, QtCore.Qt.Key_Down)
        self.assertEqual(self.model.layout, 'overlay')
        self.assertNotIn(self.model.overlayChannels[0], second.property('model'))
        QtTest.QTest.keyClick(self.window, QtCore.Qt.Key_Down)
        QtTest.QTest.keyClick(self.window, QtCore.Qt.Key_Down)
        QtTest.QTest.qWait(30)
        self.assertEqual(self.model.channels()[1], 'Ids_Keithley')
        axes = self.item('axisTitleYMode')
        axes.forceActiveFocus()
        QtTest.QTest.keyClick(self.window, QtCore.Qt.Key_Down)
        self.assertEqual(self.model.overlayAxis, 'dual')
        QtTest.QTest.keyClick(self.window, QtCore.Qt.Key_Escape)
        self.assertFalse(self.item('plotAxisPopup').property('visible'))
        self.click(self.item('axisTitle_x_0'))
        self.click(self.item('axisChoice_Vtg'))
        self.assertEqual(self.model.xAxis, 'Vtg')
        self.assertEqual(self.model._traces, original)
        for width, height in ((800, 600), (1536, 864)):
            self.window.setWidth(width)
            self.window.setHeight(height)
            self.window.setProperty('narrowPage', 1)
            QtTest.QTest.qWait(40)
            pane = self.item('livePlotPane')
            for name in ('plotCount1', 'plotCount2', 'plotCount4', 'axisTitle_x_0', 'axisTitle_y_0', 'axisTitle_y2_0'):
                item = self.item(name)
                self.assertTrue(item.isVisible(), name)
                for corner in (QtCore.QPointF(0,0), QtCore.QPointF(item.width(),0), QtCore.QPointF(0,item.height()), QtCore.QPointF(item.width(),item.height())):
                    point = item.mapToItem(pane, corner)
                    self.assertGreaterEqual(point.x(), -1)
                    self.assertGreaterEqual(point.y(), -1)
                    self.assertLessEqual(point.x(), pane.width()+1)
                    self.assertLessEqual(point.y(), pane.height()+1)
            self.assertGreater(self.item('nativePlot').height(), 80)
            stop = self.item('stopMeasurement')
            self.assertTrue(stop.isVisible())
            self.assertLessEqual(stop.mapToScene(QtCore.QPointF(0, stop.height())).y(), height)
        self.click(self.item('plotCount2'))
        self.assertEqual(self.model.layout, 'two')
        self.assertEqual(len(self.item('nativePlot').panels()), 2)
        self.click(self.item('plotCount4'))
        self.assertTrue(self.model.fourChannels)
        self.click(self.item('plotCount2'))
        self.assertEqual(self.model.channels(), ['Ids_X', 'Ids_Keithley'])
        self.click(self.item('plotCount1'))
        self.assertEqual(self.model.layout, 'overlay')
        self.click(self.item('axisTitle_y_0'))
        second = self.item('axisTitleSecondY')
        second.forceActiveFocus()
        for _ in range(second.property('currentIndex')):
            QtTest.QTest.keyClick(self.window, QtCore.Qt.Key_Up)
        self.assertEqual(self.model.layout, 'single')
        self.assertEqual(self.model.channel, 'Ids_X')
        QtTest.QTest.keyClick(self.window, QtCore.Qt.Key_Escape)

    def test_native_two_curves_dual_axes_zoom_pan_and_bounded_cache(self):
        self.model.layout = 'overlay'
        self.model.overlayAxis = 'dual'
        self.model.flush()
        QtTest.QTest.qWait(50)
        item = self.item('nativePlot')
        self.assertEqual(len(item.panels()), 1)
        self.assertEqual(len(item._interaction_views(self.model.channels()[0])), 2)
        image = QtGui.QImage(int(item.width()), int(item.height()), QtGui.QImage.Format_ARGB32_Premultiplied)
        painter = QtGui.QPainter(image)
        try: item.paint(painter)
        finally: painter.end()
        array = np.frombuffer(image.bits(), dtype=np.uint8).reshape(image.height(), image.bytesPerLine())[:, :image.width()*4].reshape(image.height(), image.width(), 4)
        for color in COLORS[:2]:
            rgb = QtGui.QColor(color)
            match = (abs(array[:,:,2].astype(int)-rgb.red()) < 4) & (abs(array[:,:,1].astype(int)-rgb.green()) < 4) & (abs(array[:,:,0].astype(int)-rgb.blue()) < 4)
            self.assertGreater(int(match.sum()), 50, color)
        rect = item.panels()[0][1]
        position = item.mapToScene(rect.center())
        before = {c: self.model.limits(c) for c in self.model.channels()}
        event = QtGui.QWheelEvent(rect.center(), rect.center(), QtCore.QPoint(), QtCore.QPoint(0,120), QtCore.Qt.NoButton, QtCore.Qt.NoModifier, QtCore.Qt.NoScrollPhase, False)
        item.wheelEvent(event)
        for channel in self.model.channels():
            self.assertLess(np.ptp(self.model.limits(channel)[1]), np.ptp(before[channel][1]))
        start = position.toPoint()
        before_pan = {c: self.model.limits(c) for c in self.model.channels()}
        QtTest.QTest.mousePress(self.window, QtCore.Qt.MiddleButton, QtCore.Qt.NoModifier, start)
        QtTest.QTest.mouseMove(self.window, start + QtCore.QPoint(20,20), 20)
        QtTest.QTest.mouseRelease(self.window, QtCore.Qt.MiddleButton, QtCore.Qt.NoModifier, start + QtCore.QPoint(20,20))
        self.assertEqual(self.model.limits(self.model.channels()[0])[0], self.model.limits(self.model.channels()[1])[0])
        for channel in self.model.channels():
            self.assertNotEqual(self.model.limits(channel)[1], before_pan[channel][1])
        self.model.interactionMode = 'zoom'
        before_zoom = {c: self.model.limits(c) for c in self.model.channels()}
        a = item.mapToScene(QtCore.QPointF(rect.left()+rect.width()*.2, rect.top()+rect.height()*.2)).toPoint()
        b = item.mapToScene(QtCore.QPointF(rect.left()+rect.width()*.8, rect.top()+rect.height()*.8)).toPoint()
        QtTest.QTest.mousePress(self.window, QtCore.Qt.LeftButton, QtCore.Qt.NoModifier, a)
        QtTest.QTest.mouseMove(self.window, b, 20)
        QtTest.QTest.mouseRelease(self.window, QtCore.Qt.LeftButton, QtCore.Qt.NoModifier, b)
        for channel in self.model.channels():
            self.assertAlmostEqual(np.ptp(self.model.limits(channel)[1])/np.ptp(before_zoom[channel][1]), .6, delta=.015)
        self.assertLessEqual(len(item._cache), len(self.model._traces)*2)

    def test_each_measurement_remembers_overlay_pair_in_existing_preferences(self):
        import json
        p = self.model
        p.layout = 'overlay'
        p.setOverlayChannel(1, 'Ids_Y')
        p.overlayAxis = 'dual'
        key = self.backend._plot_key(0)
        flavor = self.backend._plot_flavors[0]
        saved = json.loads(self.backend.settings.value(f'quick/plots/{key}/{flavor}'))
        self.assertEqual(saved['layout'], 'overlay')
        self.assertEqual(saved['overlayChannels'], ['Ids_DC', 'Ids_Y'])
        self.backend.measurement.mode = 1
        self.backend.plotModel.layout = 'single'
        self.backend.measurement.mode = 0
        self.assertIs(self.backend.plotModel, p)
        self.assertEqual(p.layout, 'overlay')
        self.assertEqual(p.overlayChannels, ['Ids_DC', 'Ids_Y'])
        self.assertEqual(p.overlayAxis, 'dual')
        clone = PlotModel()
        clone.applyPreferences(saved)
        self.assertEqual(clone.layout, 'overlay')
        self.assertEqual(clone.overlayChannels, p.overlayChannels)
        clone._timer.stop()

    def test_two_panels_fit_both_orientations_and_zoom_target_y_only(self):
        p = self.model
        p.setYAxes('Ids_DC', 'Ids_X')
        p.setPlotCount(2)
        item = QuickPlot()
        item.model = p
        item.setHeight(600)
        for width in (1000, 500):
            item.setWidth(width)
            panels = item.panels()
            self.assertEqual([channel for channel, _ in panels], ['Ids_DC', 'Ids_X'])
            self.assertFalse(panels[0][1].intersects(panels[1][1]))
            for _, rect in panels:
                self.assertTrue(item.boundingRect().contains(rect))
            self.assertEqual(panels[0][1].top() == panels[1][1].top(), width >= 680)
            image = QtGui.QImage(width, 600, QtGui.QImage.Format_ARGB32_Premultiplied)
            painter = QtGui.QPainter(image)
            try: item.paint(painter)
            finally: painter.end()
            for index, (_, rect) in enumerate(panels):
                rgb = QtGui.QColor(COLORS[index])
                count = sum(abs(image.pixelColor(x, y).red()-rgb.red()) < 5
                            and abs(image.pixelColor(x, y).green()-rgb.green()) < 5
                            and abs(image.pixelColor(x, y).blue()-rgb.blue()) < 5
                            for x in range(int(rect.left()), int(rect.right()), 2)
                            for y in range(int(rect.top()), int(rect.bottom()), 2))
                self.assertGreater(count, 10, 'each panel must paint its own signal/color')
            previous_y = p.limits('Ids_DC')[1]
            second_y = p.limits('Ids_X')[1]
            center = panels[1][1].center()
            item.wheelEvent(QtGui.QWheelEvent(center, center, QtCore.QPoint(), QtCore.QPoint(0,120), QtCore.Qt.NoButton,
                                            QtCore.Qt.NoModifier, QtCore.Qt.NoScrollPhase, False))
            self.assertEqual(p.limits('Ids_DC')[1], previous_y)
            self.assertLess(np.ptp(p.limits('Ids_X')[1]), np.ptp(second_y))
            self.assertEqual(p.limits('Ids_DC')[0], p.limits('Ids_X')[0])

    def test_titles_edit_target_panel_with_keyboard_and_popup_stays_in_window(self):
        p = self.model
        p.applyPreferences({'channelOrder': ['Ids_DC', 'Ids_Keithley', 'Ids_Y', 'Ids_X']})
        p._channels += ['I_drag_X', 'I_drag_Y', 'I_drag_R', 'I_drive_X', 'I_drive_Y', 'I_drive_R']
        p.set_limits('Ids_DC', (2, 5), (1, 6))
        p.set_limits('Ids_X', (2, 5), (100, 200))
        p._queue(True)
        before = {c: p.limits(c) for c in p.channels()}
        old_order = p.channels()
        self.click(self.item('axisTitle_y_2'))
        self.assertNotIn('Ids_DC', self.item('plotAxisPopup').property('choices').toVariant())
        self.click(self.item('axisChoice_I_drag_X'))
        self.assertEqual(p.layout, 'four')
        self.assertEqual(p.channels(), old_order[:2] + ['I_drag_X'] + old_order[3:])
        self.assertEqual(p.limits('Ids_DC'), before['Ids_DC'])
        self.assertEqual(p.limits('Ids_X'), before['Ids_X'])
        self.assertEqual(p.pointCount, 11)
        self.assertEqual(self.item('axisTitle_y_2').property('modelData')['channel'], 'I_drag_X')
        title = self.item('axisTitle_x_0')
        title.forceActiveFocus()
        QtTest.QTest.keyClick(self.window, QtCore.Qt.Key_Space)
        self.assertTrue(self.item('plotAxisPopup').property('visible'))
        QtTest.QTest.qWait(30)
        choice = self.item('axisChoice_Step Index')
        choice.forceActiveFocus()
        QtTest.QTest.keyClick(self.window, QtCore.Qt.Key_Return)
        self.assertEqual(p.xAxis, 'Step Index')
        self.assertFalse(self.item('plotAxisPopup').property('visible'))
        p.setPlotCount(1)
        self.click(self.item('axisTitle_y_0'))
        option = self.item('axisChoice_Ids_Y')
        option.forceActiveFocus()
        p.appendRecord({'vtg': 12, 'Ids_DC': 12, 'Ids_X': 24, 'condition': 99})
        QtTest.QTest.qWait(40)
        self.assertIs(self.item('axisChoice_Ids_Y'), option, 'acquisition updates must keep the open menu stable')
        self.assertTrue(option.property('activeFocus'))
        QtTest.QTest.keyClick(self.window, QtCore.Qt.Key_Escape)
        for width, height in ((800,600),(1536,864)):
            self.window.setWidth(width)
            self.window.setHeight(height)
            self.window.setProperty('narrowPage', 1)
            QtTest.QTest.qWait(40)
            self.click(self.item('axisTitle_y_0'))
            popup = self.item('plotAxisPopup')
            for _ in range(2):
                QtTest.QTest.qWait(30)
                self.assertGreaterEqual(popup.property('x'), 0)
                self.assertGreaterEqual(popup.property('y'), 0)
                self.assertLessEqual(popup.property('x') + popup.property('width'), width)
                self.assertLessEqual(popup.property('y') + popup.property('height'), height)
                second = self.item('axisTitleSecondY')
                second.forceActiveFocus()
                QtTest.QTest.keyClick(self.window, QtCore.Qt.Key_Down)
            QtTest.QTest.keyClick(self.window, QtCore.Qt.Key_Escape)
        self.click(self.item('axisTitle_y_0'))
        self.window.setWidth(800)
        self.window.setHeight(600)
        QtTest.QTest.qWait(40)
        popup = self.item('plotAxisPopup')
        self.assertLessEqual(popup.property('x') + popup.property('width'), 800)
        self.assertLessEqual(popup.property('y') + popup.property('height'), 600)
        self.backend.report('Axis popup test')
        self.click(self.item('stopAll'))
        self.assertIn('hardware commands are disabled', self.backend.message)
