"""Actual page controls align across shared control types and font sizes."""
import os
from pathlib import Path
import unittest

if os.environ.get('TRANSPORT_OFFLINE_VERIFICATION') != '1':
    raise unittest.SkipTest('Use verify_offline.py')

from PySide6 import QtCore, QtQml, QtTest, QtWidgets
from PySide6.QtQuickControls2 import QQuickStyle
from tests import test_quick_recipe_ui as recipe_tests
from tests import test_quick_workflow as workflow_tests


class ControlAlignmentTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
        QQuickStyle.setStyle('Fusion')

    def assertAligned(self, items):
        coordinates = [(item.mapToScene(QtCore.QPointF(0, 0)).y(), item.height()) for item in items]
        for y, h in coordinates[1:]:
            self.assertAlmostEqual(y, coordinates[0][0], delta=1.1, msg=str(coordinates))
            self.assertAlmostEqual(h, coordinates[0][1], delta=1.1, msg=str(coordinates))

    def test_field_editor_input_expand_and_action_same_height(self):
        engine = QtQml.QQmlApplicationEngine()
        warnings = []
        engine.warnings.connect(lambda errors: warnings.extend(str(e) for e in errors))
        source = '''import QtQuick
import QtQuick.Controls
ApplicationWindow {
  width: 650; height: 200; visible: true
  FieldEditor { objectName: "editor"; width: 600; showLabel: false
    formField: ({key: "sample_name", label: "Name", value: "hello", kind: "text", expandable: true})
    trailingActionText: "Browse"
  }
}'''
        engine.loadData(source.encode(), QtCore.QUrl.fromLocalFile(str(Path(__file__).resolve().parents[1] / 'app/quick/qml/AlignmentHarness.qml')))
        self.assertTrue(engine.rootObjects(), warnings)
        window = engine.rootObjects()[0]
        self.addCleanup(lambda: (window.hide(), engine.deleteLater(), self.app.sendPostedEvents(None, QtCore.QEvent.DeferredDelete)))
        QtTest.QTest.qWait(50)
        editor = window.findChild(QtCore.QObject, 'editor')
        def walk(item):
            yield item
            for child in item.childItems(): yield from walk(child)
        controls = list(walk(window.contentItem()))
        field = next(i for i in controls if i.objectName() == 'field_sample_name')
        expand = next(i for i in controls if i.objectName() == 'expandFieldButton' and i.isVisible())
        action = next(i for i in controls if i.property('text') == 'Browse' and i.isVisible())
        self.assertAligned([field, expand, action])
        self.assertEqual(warnings, [])

    def test_recipe_gate_checkbox_and_input_row(self):
        fixture = recipe_tests.QuickRecipeUiTests('test_inline_checkbox_numeric_and_disabled_vds_retention')
        fixture.app = self.app
        fixture.setUp()
        try:
            fixture.service.mode = 4
            fixture.service.addRow("conditions")
            fixture.load(mode=4, key='conditions', width=520)
            uid = fixture.uid()
            checkbox = fixture.cell(uid, 'raw_vtg_active')
            start = fixture.cell(uid, 'raw_vtg_start')
            stop = fixture.cell(uid, 'raw_vtg_stop')
            self.assertAligned([checkbox, start, stop])
        finally:
            fixture.tearDown()

    def test_main_navigation_and_footer_controls(self):
        fixture = workflow_tests.QuickWorkflowTests('test_sweep_preview_commits_current_input_and_preserves_live_data')
        fixture.setUp()
        try:
            fixture.window.setWidth(1536)
            fixture.window.setHeight(864)
            fixture.window.setProperty('showParameters', False)
            fixture.window.setProperty('showSweepPreview', True)
            QtTest.QTest.qWait(80)
            for size in (10, 14, 18):
                fixture.backend._set_font_size(size)
                QtTest.QTest.qWait(60)
                self.assertAligned([fixture.item('stopAll'), fixture.item('openInstruments'),
                                    fixture.item('openAnalysis'), fixture.item('openViewMenu'),
                                    fixture.item('measurementPhase')])
                self.assertAligned([fixture.item('liveDataTab'), fixture.item('sweepPreviewTab'),
                                    fixture.item('refreshSweepPreview'), fixture.item('showParameters')])
                self.assertAligned([fixture.item('startMeasurement'), fixture.item('stopMeasurement'),
                                    fixture.item('showStatusDetails')])
            self.assertEqual(fixture.warnings, [])
        finally:
            fixture.tearDown()
