"""Exercise setup navigation and full status messages without hardware access."""
import os
from pathlib import Path
import tempfile
import time
import unittest

if os.environ.get("TRANSPORT_OFFLINE_VERIFICATION") != "1":
    raise unittest.SkipTest("Run with verify_offline.py")

from PySide6 import QtCore, QtQml, QtQuickControls2, QtTest
from app.quick.application import ApplicationBackend
from app.quick.plot import register_types


class QuickWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.scratch = tempfile.TemporaryDirectory()
        self.addCleanup(self.scratch.cleanup)
        self.base = Path(self.scratch.name) / "not-created"
        self.backend = ApplicationBackend(offline=True)
        self.backend._instruments.setValue("base", str(self.base))
        self.backend._instruments.setValue("user", "Operator One")
        self.backend._instruments.setValue("device_id", "Sample 024")
        QtQuickControls2.QQuickStyle.setStyle("Fusion")
        register_types()
        self.engine = QtQml.QQmlApplicationEngine()
        self.warnings = []
        self.engine.warnings.connect(lambda errors: self.warnings.extend(str(e) for e in errors))
        self.engine.rootContext().setContextProperty("appBackend", self.backend)
        self.engine.loadData(b"import QtQuick\nMain { width: 1100; height: 760 }",
                             QtCore.QUrl.fromLocalFile(str(Path(__file__).resolve().parents[1]
                                                          / "app/quick/qml/WorkflowHarness.qml")))
        self.assertTrue(self.engine.rootObjects(), "\n".join(self.warnings))
        self.window = self.engine.rootObjects()[0]
        self.window.setProperty("pinInstruments", False)
        self.window.setProperty("showInstruments", False)
        self.window.requestActivate()
        QtTest.QTest.qWait(50)

    def tearDown(self):
        self.window.hide()
        self.backend.requestClose()
        end = time.monotonic() + 5
        while not self.backend.closeAccepted and time.monotonic() < end:
            QtTest.QTest.qWait(10)
        self.assertTrue(self.backend.closeAccepted)
        self.engine.deleteLater()
        QtCore.QCoreApplication.sendPostedEvents(None, QtCore.QEvent.DeferredDelete)
        self.backend.deleteLater()
        QtCore.QCoreApplication.sendPostedEvents(None, QtCore.QEvent.DeferredDelete)

    def item(self, name):
        found = self.window.findChild(QtCore.QObject, name)
        if found is None:
            def descendants(item):
                yield item
                for child in item.childItems():
                    yield from descendants(child)
            found = next((i for i in descendants(self.window.contentItem()) if i.objectName() == name), None)
        self.assertIsNotNone(found, name)
        return found

    def click(self, name):
        item = self.item(name)
        center = item.mapToScene(QtCore.QPointF(item.width() / 2, item.height() / 2)).toPoint()
        QtTest.QTest.mouseClick(self.window, QtCore.Qt.LeftButton, QtCore.Qt.NoModifier, center)
        QtTest.QTest.qWait(50)

    def test_setup_shortcuts_retain_pages_and_show_the_intended_destination(self):
        expected = self.base / "Operator_One" / "Sample_024"
        self.assertEqual(Path(self.backend.dataFolderPath), expected)
        self.assertFalse(self.base.exists())
        self.assertIn(str(expected), self.item("dataDestination").property("text"))
        self.click("openConnections")
        pane = self.item("instrumentPane")
        self.assertEqual(pane.property("section"), 1)
        self.assertTrue(self.window.property("showInstruments"))
        connection_page = self.item("instrumentScroller1")
        self.click("openSampleFiles")
        self.assertEqual(pane.property("section"), 5)
        editor = self.item("field_device_id")
        editor.forceActiveFocus()
        editor.setProperty("text", "Sample 025")
        QtTest.QTest.keyClick(self.window, QtCore.Qt.Key_Tab)
        QtTest.QTest.qWait(40)
        self.assertEqual(Path(self.backend.dataFolderPath), self.base / "Operator_One" / "Sample_025")
        self.assertIn("Sample_025", self.item("dataDestination").property("text"))
        self.click("openConnections")
        self.assertIs(self.item("instrumentScroller1"), connection_page)
        self.assertFalse(self.base.exists(), "Showing the save destination must not create folders")
        self.assertEqual(self.warnings, [])

    def test_status_details_keyboard_escape_and_stop_stay_accessible_on_small_windows(self):
        QtQml.QQmlProperty(self.window, "width").write(800)
        QtQml.QQmlProperty(self.window, "height").write(600)
        long_message = "Review the source and protection settings.\n" * 60 + "END OF STATUS"
        self.backend.report(long_message)
        self.backend._measurement._phase = "error"
        self.backend._measurement.changed.emit()
        QtTest.QTest.qWait(50)
        self.assertEqual(self.item("measurementPhase").property("text"), "error")
        button = self.item("showStatusDetails")
        button.forceActiveFocus()
        QtTest.QTest.keyClick(self.window, QtCore.Qt.Key_Space)
        QtTest.QTest.qWait(50)
        popup = self.item("statusDetailsPopup")
        self.assertTrue(popup.property("visible"))
        self.assertFalse(popup.property("modal"))
        self.assertIn(long_message, self.item("statusDetailsText").property("text"))
        self.assertTrue(self.item("statusDetailsText").property("readOnly"))
        QtTest.QTest.keyClick(self.window, QtCore.Qt.Key_Escape)
        QtTest.QTest.qWait(40)
        self.assertFalse(popup.property("visible"))
        self.click("showStatusDetails")
        for name in ("stopAll", "startMeasurement", "stopMeasurement", "showStatusDetails", "openConnections", "openSampleFiles"):
            item = self.item(name)
            position = item.mapToScene(QtCore.QPointF(0, 0))
            self.assertGreaterEqual(position.x(), 0, name)
            self.assertGreaterEqual(position.y(), 0, name)
            self.assertLessEqual(position.x() + item.width(), self.window.width(), name)
            self.assertLessEqual(position.y() + item.height(), self.window.height(), name)
        self.click("stopAll")
        self.assertIn("hardware commands are disabled", self.backend.message)
        self.assertEqual(self.warnings, [])

    def test_right_panel_docks_at_125_percent_desktop_width_and_retains_editors(self):
        QtQml.QQmlProperty(self.window, "width").write(1536)
        QtQml.QQmlProperty(self.window, "height").write(864)
        plot = self.backend.plotModel
        plot.setTraces([dict(x=[0., 1., 2.], y=[1., 3., 2.], channel="Ids_DC")])
        self.click("openSampleFiles")
        QtTest.QTest.qWait(300)
        self.click("pinInstrumentPanel")
        QtTest.QTest.qWait(300)
        self.assertTrue(self.window.property("dockInstruments"))
        setup, live, dock = (self.item(name) for name in
                             ("measurementSetupRegion", "liveDataRegion", "instrumentDock"))
        setup_right = setup.mapToScene(QtCore.QPointF(setup.width(), 0)).x()
        live_left = live.mapToScene(QtCore.QPointF(0, 0)).x()
        live_right = live.mapToScene(QtCore.QPointF(live.width(), 0)).x()
        dock_left = dock.mapToScene(QtCore.QPointF(0, 0)).x()
        self.assertLess(setup_right, live_left)
        self.assertLess(live_right, dock_left)
        self.assertGreaterEqual(live.width(), 400)
        editor = self.item("field_device_id")
        editor.forceActiveFocus()
        editor.setProperty("text", "Draft sample")
        QtQml.QQmlProperty(self.window, "width").write(1100)
        QtTest.QTest.qWait(350)
        self.assertFalse(self.window.property("dockInstruments"))
        self.assertTrue(self.window.property("pinInstruments"))
        self.assertTrue(self.item("instrumentDrawer").property("visible"))
        self.assertIs(self.item("field_device_id"), editor)
        self.assertEqual(editor.property("text"), "Draft sample")
        drawer_left = self.item("instrumentRegion").mapToScene(QtCore.QPointF(0, 0)).x()
        self.assertGreaterEqual(drawer_left, setup_right)
        QtQml.QQmlProperty(self.window, "width").write(1536)
        QtTest.QTest.qWait(350)
        self.assertTrue(self.window.property("dockInstruments"))
        self.assertTrue(self.window.property("showInstruments"))
        self.assertIs(self.item("field_device_id"), editor)
        self.assertEqual(plot.pointCount, 3)
        self.click("closeInstrumentPanel")
        self.assertFalse(self.window.property("showInstruments"))
        self.click("openSampleFiles")
        self.assertTrue(self.window.property("dockInstruments"))
        self.assertIs(self.item("field_device_id"), editor)
        self.assertEqual(self.warnings, [])

    def test_resizable_setup_and_separate_log_can_be_hidden_without_losing_state(self):
        QtQml.QQmlProperty(self.window, "width").write(1536)
        self.backend._measurement.mode = 1
        QtTest.QTest.qWait(100)
        setup = self.item("measurementSetupRegion")
        width_before = setup.width()
        divider = setup.mapToScene(QtCore.QPointF(setup.width() + 4, setup.height() / 2)).toPoint()
        QtTest.QTest.mousePress(self.window, QtCore.Qt.LeftButton, pos=divider)
        QtTest.QTest.mouseMove(self.window, divider + QtCore.QPoint(55, 0), delay=40)
        QtTest.QTest.mouseRelease(self.window, QtCore.Qt.LeftButton, pos=divider + QtCore.QPoint(55, 0))
        QtTest.QTest.qWait(80)
        resized_width = setup.width()
        self.assertGreater(resized_width, width_before + 20)
        scroller = self.item("parameterScroll").property("contentItem")
        scroller.setProperty("contentY", 100.)
        self.click("hideParameters")
        self.assertFalse(setup.property("visible"))
        self.click("showParameters")
        self.assertTrue(setup.property("visible"))
        self.assertAlmostEqual(setup.width(), resized_width, delta=1)
        self.assertAlmostEqual(scroller.property("contentY"), 100., delta=1)
        QtTest.QTest.keyClick(self.window, QtCore.Qt.Key_L, QtCore.Qt.ControlModifier)
        QtTest.QTest.qWait(50)
        log, live, plot = (self.item(name) for name in
                           ("measurementLogRegion", "liveDataRegion", "livePlotPane"))
        self.assertTrue(log.property("visible"))
        self.assertAlmostEqual(log.width(), live.width(), delta=1)
        self.assertGreaterEqual(plot.height(), 100)
        self.assertLessEqual(log.y() + log.height(), live.height())
        self.click("closeMeasurementLog")
        self.assertFalse(log.property("visible"))
        QtQml.QQmlProperty(self.window, "width").write(800)
        self.window.setProperty("narrowPage", 0)
        QtTest.QTest.keyClick(self.window, QtCore.Qt.Key_L, QtCore.Qt.ControlModifier)
        QtTest.QTest.qWait(50)
        self.assertEqual(self.window.property("narrowPage"), 1)
        self.assertTrue(log.property("visible"), "Opening the log must reveal its region on narrow windows")
        self.assertEqual(self.warnings, [])
