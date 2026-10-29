"""Native plot geometry, acquisition buffering and actual Qt Quick rendering."""
import os
import math
from pathlib import Path
import unittest
from unittest.mock import patch

if os.environ.get('TRANSPORT_OFFLINE_VERIFICATION') != '1':
    raise unittest.SkipTest('Run through verify_offline.py for isolated Qt and hardware')

import numpy as np
from PySide6.QtCore import QUrl, QPoint, QPointF, Qt, QObject, QMetaObject
from PySide6.QtGui import QImage, QPainter, QWheelEvent
from PySide6.QtQml import QQmlEngine
from PySide6.QtQuick import QQuickView
from PySide6.QtQuickControls2 import QQuickStyle
from PySide6.QtTest import QSignalSpy, QTest
from PySide6.QtWidgets import QApplication

from app.quick.plot import (PlotModel, QuickPlot, NumericBuffer, envelope_segments,
                            prepare_heatmap, cell_edges, register_types, _finite_runs)


class PlotGeometryTests(unittest.TestCase):
    def test_envelope_retains_extrema_gaps_and_order_without_mutating_data(self):
        x = np.linspace(-4, 7, 1_000_003)
        y = np.sin(x)
        y[12345], y[555555] = -99, 123
        y[500000:500005] = np.nan
        segments = envelope_segments(x, y, (-5, 8), 500)
        self.assertEqual(len(segments), 2)
        self.assertLess(sum(len(a) for a, _ in segments), 2200)
        self.assertEqual(min(min(b) for _, b in segments), -99)
        self.assertEqual(max(max(b) for _, b in segments), 123)
        self.assertTrue(all(np.all(np.diff(a) >= 0) for a, _ in segments))
        self.assertTrue(np.isnan(y[500001]))
        self.assertEqual(len(x), 1_000_003)

    def test_viewport_keeps_crossing_lines_and_separates_outside_excursions(self):
        segments = envelope_segments([-3, 3], [1, 2], (-1, 1), 100)
        self.assertEqual(len(segments[0][0]), 2)
        segments = envelope_segments([0, 1, 10, 11, 0, 1], [0, 1, 2, 3, 4, 5], (-1, 2), 100)
        self.assertTrue(segments)
        self.assertEqual(segments[0][0][0], 0)

    def test_indexed_viewport_preserves_descending_duplicates_neighbors_and_gaps(self):
        x = np.array([8., 6., 6., 4., 2., 0., np.nan, 0., 8.])
        y = np.arange(len(x), dtype=float)
        runs = _finite_runs(x, y)
        parts = envelope_segments(x, y, (3, 6), 100, runs=runs)
        self.assertEqual(len(parts), 2)
        np.testing.assert_equal(parts[0][0], [8, 6, 6, 4, 2])
        np.testing.assert_equal(parts[0][1], [0, 1, 2, 3, 4])
        np.testing.assert_equal(parts[1][0], [0, 8])
        self.assertEqual(envelope_segments(x, y, (9, 10), 100, runs=runs), [])
        self.assertEqual(envelope_segments(x, y, (-3, -1), 100, runs=runs), [])

    def test_buffer_grows_and_keeps_all_samples_and_incremental_bounds(self):
        data = NumericBuffer()
        for i in range(1025):
            data.append(i)
        data.append(None)
        self.assertEqual(data.size, 1026)
        self.assertEqual(data.bounds, (0, 1024))
        np.testing.assert_array_equal(data.values[:-1], np.arange(1025))
        self.assertTrue(math.isnan(data.values[-1]))

    def test_exact_heatmap_rejects_duplicates_and_keeps_missing_cells(self):
        data = prepare_heatmap({'x': [0, 1, 0], 'y': [0, 0, 1], 'z': [2, 4, 6]})
        np.testing.assert_equal(data['z'], [[2, 4], [6, np.nan]])
        with self.assertRaisesRegex(ValueError, 'Duplicate'):
            prepare_heatmap({'x': [0, 0], 'y': [0, 0], 'z': [2, 4]})
        with self.assertRaisesRegex(ValueError, 'Duplicate'):
            prepare_heatmap({'x': [0, 0], 'y': [0], 'z': [[1, 2]]})
        np.testing.assert_equal(cell_edges([0, 2, 8]), [-1, 1, 5, 11])


class PlotModelTests(unittest.TestCase):
    def setUp(self):
        self.model = PlotModel()

    def tearDown(self):
        self.model._timer.stop()

    def test_plan_before_first_sample_acquired_policy_readback_and_home(self):
        p = self.model
        p.beginRun({'x_axis': 'Vtg', 'planned_ranges': {'Vtg': (-5, 5)}})
        self.assertEqual(p.limits()[0], (-5.3, 5.3))
        p.appendRecord({'vtg': 1, 'Ids_DC': 2})
        self.assertEqual(p.limits()[0], (-5.3, 5.3))
        p.setXRangeMode('Acquired data')
        self.assertEqual(p.limits()[0], (.95, 1.05))
        p.set_limits('Ids_DC', (-1, 2), (-2, 3))
        p.appendRecord({'vtg': 20, 'Ids_DC': 50})
        self.assertEqual(p.limits(), ((-1, 2), (-2, 3)))
        p.home()
        self.assertGreater(p.limits()[0][1], 20)
        p.setXRangeMode('Full sweep')
        self.assertLess(p.limits()[0][0], -5)
        p.beginRun({'x_axis': 'Vtg', 'planned_ranges': {'Vtg': (-2, 2)}})
        self.assertEqual(p._count, 0)
        self.assertEqual(p.limits()[0], (-2.12, 2.12))

    def test_four_panels_share_x_independent_y_and_coordinate_switch(self):
        p = self.model
        p.beginRun({'x_axis': 'Vtg'})
        p.appendRecord({'vtg': 1, 'vbg': 10, 'Ids_DC': 1, 'Ids_X': 1000,
                        'Ids_Y': -10, 'Ids_Keithley': 100})
        p.appendRecord({'vtg': 2, 'vbg': 20, 'Ids_DC': 2, 'Ids_X': 2000,
                        'Ids_Y': -20, 'Ids_Keithley': 200})
        p.fourChannels = True
        self.assertEqual(len(p.channels()), 4)
        self.assertEqual(len({p.limits(c)[0] for c in p.channels()}), 1)
        self.assertEqual(len({p.limits(c)[1] for c in p.channels()}), 4)
        p.set_limits('Ids_DC', (0, 1), (0, 3))
        p.xAxis = 'Vbg'
        self.assertGreater(p.limits()[0][1], 20)
        self.assertEqual(p.limits()[1], (0, 3))
        p.channel = 'Ids_X'
        self.assertFalse(p._manual_y)
        self.assertIn('Vbg', p.xLabel)

    def test_conditions_direction_passes_segment_and_timer_coalescing(self):
        p = self.model
        frames = QSignalSpy(p.frameReady)
        p.beginRun({'x_axis': 'Vds'})
        for i in range(100):
            p.appendRecord({'vds': i, 'Ids_DC': i, 'direction': 'Forward', 'condition': 1})
        p.appendRecord({'vds': 101, 'Ids_DC': 1, 'direction': 'Backward', 'condition': 1})
        p.appendRecord({'vds': 102, 'Ids_DC': 2, 'direction': 'Forward', 'condition': 2})
        p.appendRecord({'vds': 103, 'Ids_DC': 3, 'direction': 'Forward', 'condition': 2, 'pass_index': 1})
        self.assertEqual(frames.count(), 0)
        p.flush()
        self.assertEqual(frames.count(), 1)
        self.assertEqual(len(p._traces), 4)
        self.assertEqual(p.pointCount, 103)

    def test_heatmap_view_color_limits_and_roundtrip(self):
        p = self.model
        p.setHeatmap({'x': [0, 1], 'y': [0, 1], 'z': [[1, np.nan], [3, 9]]}, 'X (V)', 'Y (V)', 'I (A)')
        self.assertTrue(p.isHeatmap)
        self.assertEqual(p.color_limits(), (1, 9))
        p.applyView({'xlim': [-.5, .4], 'ylim': [[-.5, 1.5]], 'cmap': 'plasma'})
        self.assertEqual(p.color_limits(), (1, 3))
        p.setColorLimits(-2, 8)
        saved = p.captureView()
        p.home()
        p.autoColorLimits()
        p.applyView(saved)
        self.assertEqual(p.captureView(), saved)
        with self.assertRaises(ValueError):
            p.applyView({'xlim': [2, 1]})

    def test_snapshot_is_full_resolution_frozen_and_four_views_match(self):
        p = self.model
        p.beginRun({'x_axis': 'Vtg', 'csv_path': str(Path('tmp/live.csv').absolute()),
                    'measurement': 'gate_scan', 'params': {'vds_source': 'Keithley 2400'}})
        for i in range(10):
            p.appendRecord({'vtg': i, 'Ids_DC': i*1e-9, 'Ids_X': i*1e-6,
                            'Ids_Y': -i*1e-9, 'Ids_Keithley': i*1e-12})
        p.fourChannels = True
        p.set_limits('Ids_DC', (2, 6), (-1e-9, 5e-9))
        p.set_limits('Ids_X', (2, 6), (-1e-6, 5e-6))
        snapshot = p.exportSnapshot()
        self.assertEqual(len(snapshot['panels']), 4)
        for channel, data in snapshot['panels'].items():
            self.assertEqual(data['view']['xlim'], [2, 6])
            self.assertEqual(data['view']['ylim'], list(p.limits(channel)[1]))
            self.assertEqual(len(data['traces'][0]['x']), 10)
            self.assertFalse(data['traces'][0]['x'].flags.writeable)
        p.appendRecord({'vtg': 100, 'Ids_DC': 100e-9, 'Ids_X': 100e-6})
        p.home()
        self.assertEqual(len(snapshot['panels']['Ids_DC']['traces'][0]['x']), 10)
        self.assertEqual(snapshot['panels']['Ids_DC']['view']['xlim'], [2, 6])

    def test_color_changes_notify_and_empty_visible_map_retains_last_limits(self):
        p = self.model
        p.setHeatmap({'x': [0, 1], 'y': [0, 1], 'z': [[1, 2], [3, 4]]})
        self.assertEqual(p.color_limits(), (1, 4))
        changes = QSignalSpy(p.viewChanged)
        p.setColorMap('RdBu_r')
        self.assertEqual(p.captureView()['cmap'], 'RdBu_r')
        p.setColorLimits(-1, 8)
        p.autoColorLimits()
        self.assertEqual(changes.count(), 3)
        p.applyView({'xlim': [10, 11]})
        self.assertEqual(p.color_limits(), (1, 4))

    def test_million_sample_model_retains_data_behind_display_envelope(self):
        x = np.linspace(0, 1, 1_000_000)
        self.model.setTraces([{'x': x, 'y': np.sin(5*x)}], 'Vds (V)', 'Current (A)')
        self.assertEqual(self.model.pointCount, 1_000_000)
        trace = self.model._traces[0]
        self.assertEqual(trace.columns['x'].size, 1_000_000)
        self.assertLess(sum(len(s[0]) for s in envelope_segments(trace.columns['x'].values,
                                                                trace.columns['y'].values, (0, 1), 500)), 1100)

    def test_native_transport_record_conditions_axes_and_export_source_paths(self):
        p = self.model
        paths = [str(Path('tmp/condition1.csv').absolute()), str(Path('tmp/condition2.csv').absolute())]
        p.beginRun({'x_axis': 'B-field', 'csv_paths': paths, 'measurement': 'bfield_transport'})
        for condition in (1, 2):
            p.appendRecord({'x': .5, 'condition_index': condition, 'condition_name': f'C{condition}',
                            'Doping': condition*2., 'E-field': condition*3., 'Vtg': condition,
                            'Ids_DC': condition*1e-9, 'direction': 'forward'})
        self.assertEqual(len(p._traces), 2)
        p.xAxis = 'Doping'
        self.assertGreater(p.limits()[0][1], 4.)
        snapshot = p.exportSnapshot()
        self.assertEqual([t['path'] for t in snapshot['traces']], paths)
        np.testing.assert_array_equal(snapshot['traces'][1]['x'], [4.])
        p.finishRun({'status': 'stopped', 'detail': 'user stop'})
        finished = p.exportSnapshot()
        self.assertEqual(finished['records'][0]['status'], 'stopped')
        self.assertEqual(finished['records'][0]['metadata']['detail'], 'user stop')

    def test_dual_compare_keeps_drag_and_drive_columns(self):
        p = self.model
        p.beginRun({'params': {'vds_source': 'Keithley 2400'},
                    'signal_chain': {'drag_drive': {'enabled': True}}, 'plot_choice': 'I_drag_X'})
        p.fourChannels = True
        self.assertEqual(p.channels(), ['I_drag_X', 'I_drive_X', 'I_drag_Y', 'I_drive_Y'])
        self.assertIn('I_drive_R', p.channelOptions)

    def test_preferences_are_small_per_mode_and_accept_legacy_aliases(self):
        p = self.model
        p.beginRun({'x_axis': 'Vtg'})
        p.appendRecord({'vtg': 1, 'vds': .01, 'Ids_X': 2})
        changes = QSignalSpy(p.preferencesChanged)
        p.applyPreferences({'plot_choice': 'Ids_X', 'plot_x_axis': 'Vds',
                            'plot_mode': '4-Channel Compare', 'x_range_mode': 'Acquired data',
                            'colorMap': 'RdBu_r'})
        self.assertEqual(changes.count(), 1)
        captured = p.capturePreferences()
        self.assertNotIn('traces', captured)
        self.assertLess(len(str(captured)), 250)
        other = PlotModel()
        try:
            other.applyPreferences(captured)
            self.assertEqual(other.capturePreferences(), captured)
            self.assertEqual(other.pointCount, 0)
            self.assertEqual(p.pointCount, 1)
            self.assertTrue(other.fourChannels)
        finally:
            other._timer.stop()


class NativeQuickPlotTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        QQuickStyle.setStyle('Basic')
        register_types()

    def setUp(self):
        self.model = PlotModel()
        self.model.setTraces([{'x': [0, 1, 2, 3, 4], 'y': [0, 1, np.nan, -1, 0], 'label': 'Forward'},
                              {'x': [4, 3, 2, 1, 0], 'y': [1, 2, 1, 0, -1], 'label': 'Reverse', 'direction': 'Backward'}], 'Vtg (V)', 'Current (A)')
        self.view = QQuickView()
        self.view.engine().rootContext().setContextProperty('testPlotModel', self.model)
        self.view.setResizeMode(QQuickView.ResizeMode.SizeRootObjectToView)
        # Loading the real PlotPane catches QML property/type/import failures.
        directory = Path(__file__).resolve().parents[1] / 'app' / 'quick' / 'qml'
        self.view.engine().addImportPath(str(directory))
        component = self.view.engine()
        from PySide6.QtQml import QQmlComponent
        self.component = QQmlComponent(component)
        self.component.setData(b'import QtQuick\nimport "."\nPlotPane { width: 900; height: 620; plotModel: testPlotModel }', QUrl.fromLocalFile(str(directory / '_plot_test.qml')))
        self.assertFalse(self.component.isError(), '\n'.join(e.toString() for e in self.component.errors()))
        root = self.component.create()
        self.assertIsNotNone(root, '\n'.join(e.toString() for e in self.component.errors()))
        self.view.setContent(QUrl(), self.component, root)
        self.view.resize(900, 620)
        self.view.show()
        QTest.qWait(80)
        QApplication.processEvents()
        self.item = root.findChild(QuickPlot, 'nativePlot')
        self.assertIsNotNone(self.item)

    def tearDown(self):
        self.view.close()
        self.view.deleteLater()
        self.model._timer.stop()
        QApplication.processEvents()

    def test_real_qml_load_and_frame_contains_native_curves_and_axis_labels(self):
        self.assertGreater(self.item.width(), 500)
        image = self.view.grabWindow()
        self.assertFalse(image.isNull())
        scratch = Path('tmp/verification/quick_plot')
        scratch.mkdir(parents=True, exist_ok=True)
        self.assertTrue(image.save(str(scratch / 'native_lines.png')))
        # Painted frame must contain blue/orange curve pixels, not just a blank
        # background and QML controls. Qt Quick itself owns this actual item.
        image = image.convertToFormat(QImage.Format.Format_RGBA8888)
        array = np.frombuffer(image.bits(), dtype=np.uint8).reshape(image.height(), image.bytesPerLine())[:, :image.width()*4].reshape(image.height(), image.width(), 4)
        colored = (array[:, :, 2].astype(int) > array[:, :, 0].astype(int) + 30) & (array[:, :, 0] < 90)
        self.assertGreater(int(colored.sum()), 100)
        rect = self.item.panels()[0][1]
        top = self.item.mapToScene(rect.topLeft())
        plot_pixels = array[round(top.y()):round(top.y()+rect.height()),
                            round(top.x()):round(top.x()+rect.width()), :3]
        # Curve painting must not inherit a marker brush and fill the next
        # trace's enclosed polygon.
        self.assertGreater(float(np.all(plot_pixels == 255, axis=2).mean()), .85)

    def test_full_legend_is_created_only_while_plot_options_are_visible(self):
        root = self.view.rootObject()
        popup = root.findChild(QObject, 'plotOptionsPopup')
        legend = root.findChild(QObject, 'plotLegend')
        self.assertIsNotNone(popup)
        self.assertIsNotNone(legend)
        rows = [{'x': [0, 1], 'y': [i, i + 1], 'label': f'Trace {i}'} for i in range(120)]
        self.model.setTraces(rows)
        self.model.home()
        self.assertFalse(popup.property('visible'))
        self.assertEqual(legend.property('count'), 0)
        self.assertTrue(QMetaObject.invokeMethod(popup, 'open'))
        QTest.qWait(30)
        self.assertTrue(popup.property('visible'))
        self.assertEqual(legend.property('count'), len(rows))
        self.model.setTraces(rows[:40])
        self.assertEqual(legend.property('count'), 40)
        self.assertTrue(QMetaObject.invokeMethod(popup, 'close'))
        QTest.qWait(180)
        self.assertFalse(popup.property('visible'))
        self.assertEqual(legend.property('count'), 0)
        self.model.setTraces(rows[:20])
        self.assertEqual(legend.property('count'), 0)
        self.assertTrue(QMetaObject.invokeMethod(popup, 'open'))
        QTest.qWait(30)
        self.assertEqual(legend.property('count'), 20)
        self.assertTrue(QMetaObject.invokeMethod(popup, 'close'))

    def test_dark_theme_native_axes_curve_and_empty_map_cell(self):
        self.item.darkMode = True
        target = QImage(int(self.item.width()), int(self.item.height()), QImage.Format.Format_ARGB32_Premultiplied)
        painter = QPainter(target)
        self.item.paint(painter)
        painter.end()
        self.assertEqual(target.pixelColor(1, 1).name(), '#1d2532')
        self.model.setHeatmap({'x': [0, 1], 'y': [0, 1], 'z': [[1, np.nan], [3, 4]]})
        self.model.home()
        rect = self.item.panels()[0][1]
        image = self.item._heatmap_image(rect, *self.model.limits())
        self.assertEqual(image.pixelColor(int(image.width()*.75), int(image.height()*.75)).name(), '#1d2532')

    def test_pan_reuses_finite_index_and_keeps_only_latest_path_per_trace(self):
        x = np.linspace(-1, 1, 10000)
        self.model.setTraces([{'x': x, 'y': np.sin(x)}])
        target = QImage(int(self.item.width()), int(self.item.height()), QImage.Format.Format_ARGB32_Premultiplied)
        def paint():
            painter = QPainter(target)
            try:
                self.item.paint(painter)
            finally:
                painter.end()
        with patch('app.quick.plot._finite_runs', wraps=_finite_runs) as index:
            paint()
            for i in range(25):
                self.model.set_limits(self.model.channel, (i*.001, i*.001+.01), (-2, 2))
                self.model.flush()
                paint()
            self.assertEqual(index.call_count, 1)
        self.assertEqual(len(self.item._cache), 1)
        self.assertEqual(len(self.item._run_cache), 1)
        self.model.clear()
        paint()
        self.assertEqual(self.item._cache, {})
        self.assertEqual(self.item._run_cache, {})

    def test_live_frame_reuses_completed_path_and_invalidates_appended_trace(self):
        self.model.beginRun({'x_axis': 'Vtg'})
        self.model.appendRecord({'vtg': 0, 'Ids_DC': 1, 'condition': 1})
        self.model.appendRecord({'vtg': 1, 'Ids_DC': 2, 'condition': 2})
        self.model.set_limits(self.model.channel, (-1, 4), (-1, 4))
        target = QImage(int(self.item.width()), int(self.item.height()), QImage.Format.Format_ARGB32_Premultiplied)
        def paint():
            painter = QPainter(target)
            try:
                self.item.paint(painter)
            finally:
                painter.end()
        paint()
        slots = [(id(trace), self.model.channel) for trace in self.model._traces]
        old_paths = [self.item._cache[slot][1] for slot in slots]
        self.model.appendRecord({'vtg': 2, 'Ids_DC': 3, 'condition': 2})
        self.model.flush()
        paint()
        self.assertIs(self.item._cache[slots[0]][1], old_paths[0])
        self.assertIsNot(self.item._cache[slots[1]][1], old_paths[1])

    def test_rectangle_pan_wheel_preserve_view_on_new_records_and_home(self):
        self.model.beginRun({'x_axis': 'Vtg', 'planned_ranges': {'Vtg': [0, 10]}})
        self.model.appendRecord({'vtg': 2, 'Ids_DC': 2})
        self.model.appendRecord({'vtg': 8, 'Ids_DC': 8})
        self.model.flush()
        rect = self.item.panels()[0][1]
        def window_point(point):
            p = self.item.mapToScene(point)
            return QPoint(round(p.x()), round(p.y()))
        a = window_point(QPointF(rect.left() + rect.width() * .2, rect.top() + rect.height() * .2))
        b = window_point(QPointF(rect.left() + rect.width() * .8, rect.top() + rect.height() * .8))
        self.model.interactionMode = 'zoom'
        QTest.mousePress(self.view, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, a)
        QTest.mouseMove(self.view, b, 10)
        QTest.mouseRelease(self.view, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, b)
        zoomed = self.model.limits()
        self.assertLess(zoomed[0][1] - zoomed[0][0], 10)
        self.model.appendRecord({'vtg': 100, 'Ids_DC': 100})
        self.assertEqual(self.model.limits(), zoomed)
        self.model.interactionMode = 'pan'
        center = window_point(rect.center())
        end = center + QPoint(20, 15)
        QTest.mousePress(self.view, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, center)
        QTest.mouseMove(self.view, end, 10)
        QTest.mouseRelease(self.view, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, end)
        self.assertNotEqual(self.model.limits(), zoomed)
        before = self.model.limits()
        event = QWheelEvent(QPointF(center), QPointF(center), QPoint(), QPoint(0, 120), Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier, Qt.ScrollPhase.NoScrollPhase, False)
        QApplication.sendEvent(self.view, event)
        self.assertLess(self.model.limits()[0][1] - self.model.limits()[0][0], before[0][1] - before[0][0])
        self.model.home()
        self.assertGreater(self.model.limits()[0][1], 100)

    def test_native_heatmap_nonuniform_cells_missing_colorbar_and_cut_click(self):
        self.model.setHeatmap({'x': [0, 2, 8], 'y': [0, 1], 'z': [[1, np.nan, 3], [4, 5, 6]]}, 'Vtg (V)', 'Vbg (V)', 'I (A)')
        self.model.home()
        self.model.flush()
        QTest.qWait(50)
        rect = self.item.panels()[0][1]
        image = self.item._heatmap_image(rect, *self.model.limits())
        self.assertEqual(image.pixelColor(int(image.width() * .3), int(image.height() * .75)), image.pixelColor(int(image.width() * .4), int(image.height() * .75)))
        self.assertEqual(image.pixelColor(int(image.width() * .3), int(image.height() * .75)).name(), '#ffffff')
        cuts = QSignalSpy(self.model.cutRequested)
        pos = self.item.mapToScene(rect.center())
        QTest.mouseClick(self.view, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, QPoint(round(pos.x()), round(pos.y())))
        self.assertEqual(cuts.count(), 1)
        self.assertIn(cuts.at(0)[0], [0, 2, 8])
        self.assertIn(cuts.at(0)[1], [0, 1])
        target = Path('tmp/verification/quick_plot/native_heatmap.png')
        target.parent.mkdir(parents=True, exist_ok=True)
        self.assertTrue(self.view.grabWindow().save(str(target)))


if __name__ == '__main__':
    unittest.main()
