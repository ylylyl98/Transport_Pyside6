"""Mouse/keyboard checks for the file-list and comparison workflow."""
import json
import os
from pathlib import Path
import tempfile
import time
import unittest

if os.environ.get("TRANSPORT_OFFLINE_VERIFICATION") != "1":
    raise unittest.SkipTest("Use verify_offline.py")

from PySide6 import QtCore, QtGui, QtQml, QtQuickControls2, QtTest
from app.quick.history import HistoryBackend
from app.quick.plot import register_types


class HistoryWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.scratch = tempfile.TemporaryDirectory()
        self.addCleanup(self.scratch.cleanup)
        self.folder = Path(self.scratch.name)
        self.path = self.folder / "map.csv"
        self.path.write_text("Vtg,Vbg,Ids_DC\nV,V,A\n0,1,2\n1,1,4\n0,2,3\n1,2,8\n")
        self.path.with_name("map_metadata.json").write_text(json.dumps({"measurement": "map_2d"}))
        self.backend = HistoryBackend(self.folder, settings=QtCore.QSettings(
            str(self.folder / "settings.ini"), QtCore.QSettings.IniFormat), autostart=False)
        self.addCleanup(self.backend.shutdown)
        QtQuickControls2.QQuickStyle.setStyle("Fusion")
        register_types()
        self.engine = QtQml.QQmlApplicationEngine()
        self.warnings = []
        self.engine.warnings.connect(lambda errors: self.warnings.extend(str(e) for e in errors))
        self.engine.rootContext().setContextProperty("historyBackend", self.backend)
        self.engine.rootContext().setContextProperty("historyConnected", False)
        self.engine.load(QtCore.QUrl.fromLocalFile(str(Path(__file__).resolve().parents[1]
                                                     / "app/quick/qml/HistoryWindow.qml")))
        self.assertTrue(self.engine.rootObjects(), "\n".join(self.warnings))
        self.window = self.engine.rootObjects()[0]
        # The offscreen platform reports an 800px screen. Remove the initial
        # screen-size bindings before exercising a production-size window.
        QtQml.QQmlProperty(self.window, "width").write(1100)
        QtQml.QQmlProperty(self.window, "height").write(760)
        self.window.requestActivate()
        self.backend.refresh()
        self.drain()
        self.backend.selectType("map_2d")
        self.backend.setChecked(str(self.path), True)
        self.drain()

    def tearDown(self):
        self.window.setProperty("visible", False)
        del self.window
        del self.engine

    def drain(self):
        end = time.monotonic() + 5
        while time.monotonic() < end:
            QtCore.QCoreApplication.processEvents()
            if not self.backend.busy:
                QtTest.QTest.qWait(30)
                return
            QtTest.QTest.qWait(5)
        self.fail("History worker did not finish")

    def item(self, name):
        item = self.window.findChild(QtCore.QObject, name)
        self.assertIsNotNone(item, name)
        return item

    def click(self, name):
        item = self.item(name)
        center = item.mapToScene(QtCore.QPointF(item.property("width") / 2,
                                               item.property("height") / 2)).toPoint()
        QtTest.QTest.mouseClick(self.window, QtCore.Qt.LeftButton, QtCore.Qt.NoModifier, center)
        QtTest.QTest.qWait(30)
        return item

    def type_text(self, text):
        QtTest.QTest.keyClick(self.window, QtCore.Qt.Key_A, QtCore.Qt.ControlModifier)
        for character in text:
            QtTest.QTest.keyClick(self.window, QtCore.Qt.Key(ord(character.upper())))
        QtTest.QTest.qWait(30)

    def test_search_and_expand_preserve_selection_and_export_readiness(self):
        QtTest.QTest.keyClick(self.window, QtCore.Qt.Key_F, QtCore.Qt.ControlModifier)
        QtTest.QTest.qWait(30)
        self.assertTrue(self.item("historySearch").property("activeFocus"))
        self.type_text("absent")
        deadline = time.monotonic() + 2
        while self.backend.files and time.monotonic() < deadline:
            QtTest.QTest.qWait(10)
        self.assertEqual(self.backend.files, [])
        self.assertEqual(self.item("historySearch").property("text"), "absent")
        self.assertTrue(self.item("historySearch").property("activeFocus"))
        self.assertEqual(self.backend.selectedCount, 1)
        self.assertTrue(self.item("historyExportView").property("enabled"))
        self.click("historyClearFilters")
        self.assertEqual(len(self.backend.files), 1)
        self.assertTrue(self.backend.files[0]["checked"])
        self.click("historyExpandFiles")
        self.assertTrue(self.backend.expanded)
        self.assertFalse(self.item("historyMainPlot").property("visible"))
        self.click("historyExpandFiles")
        self.assertFalse(self.backend.expanded)
        self.assertTrue(self.backend.exportReady)
        self.assertEqual(self.warnings, [])

    def test_range_dialog_mouse_keyboard_validation_and_escape(self):
        self.click("historyRanges")
        self.assertTrue(self.item("historyRangeDialog").property("visible"))
        mode = self.item("historyRange_x_mode")
        QtCore.QMetaObject.invokeMethod(mode, "forceActiveFocus")
        QtTest.QTest.keyClick(self.window, QtCore.Qt.Key_End)
        QtTest.QTest.qWait(30)
        self.assertEqual(mode.property("currentIndex"), 1)
        self.assertFalse(self.backend.rangeState["x"]["auto"])
        self.click("historyRange_x_lower")
        self.type_text("3")
        QtTest.QTest.keyClick(self.window, QtCore.Qt.Key_Return)
        self.assertIn("increasing", self.backend.rangeState["x"]["error"])
        self.type_text("-1")
        QtTest.QTest.keyClick(self.window, QtCore.Qt.Key_Return)
        self.assertEqual(self.backend.rangeState["x"]["error"], "")
        self.assertEqual(self.backend.capture_view()["map_xlim"][0], -1)
        QtTest.QTest.keyClick(self.window, QtCore.Qt.Key_Escape)
        QtTest.QTest.qWait(30)
        self.assertFalse(self.item("historyRangeDialog").property("visible"))
        self.assertEqual(self.warnings, [])

    def test_dark_theme_supports_the_same_file_and_range_actions(self):
        self.backend.setDark(True)
        self.drain()
        self.test_search_and_expand_preserve_selection_and_export_readiness()
        self.test_range_dialog_mouse_keyboard_validation_and_escape()

    def test_partitions_fit_minimum_and_desktop_sizes_in_both_themes(self):
        for dark in (False, True):
            self.backend.setDark(dark)
            for width, height in ((1900, 1000), (1520, 800), (1100, 700)):
                with self.subTest(dark=dark, width=width):
                    QtQml.QQmlProperty(self.window, "width").write(width)
                    QtQml.QQmlProperty(self.window, "height").write(height)
                    self.drain()
                    settings = self.item("historySettings")
                    for group in settings.childItems():
                        if group.isVisible():
                            self.assertGreaterEqual(group.x(), 0)
                            self.assertLessEqual(group.x() + group.width(), settings.width() + 1)
                    main = self.item("historyMainPlot")
                    self.assertGreater(main.property("height"), 180)
                    origin = main.mapToScene(QtCore.QPointF())
                    self.assertGreaterEqual(origin.x(), 0)
                    self.assertLessEqual(origin.x() + main.property("width"), width)
                    tabs = self.item("historyPlotTabs")
                    self.assertEqual(tabs.property("visible"), width < 1900)
                    self.assertIn(self.backend.state["signal"], self.item("historyHeatmapTab").property("text"))
                    cut_text = self.item("historyCutTab").property("text")
                    self.assertIn(self.backend.state["fixed_axis"], cut_text)
                    self.assertEqual(float(cut_text.rsplit("=", 1)[1]), self.backend.state["fixed_value"])
                    if not tabs.property("visible"):
                        for caption_name, plot_name in (("historyHeatmapTitle", "historyMainPlot"),
                                                        ("historyCutTitle", "historyCutPlot")):
                            caption, plot = self.item(caption_name), self.item(plot_name)
                            self.assertAlmostEqual(caption.mapToScene(QtCore.QPointF()).x(),
                                                   plot.mapToScene(QtCore.QPointF()).x(), delta=1)
                            self.assertAlmostEqual(caption.property("width"), plot.property("width"), delta=1)
        self.assertEqual(self.warnings, [])

    def test_file_splitter_drag_persists_the_new_partition_width(self):
        QtQml.QQmlProperty(self.window, "width").write(1520)
        self.drain()
        pane = self.item("historyFilePane")
        previous = pane.property("width")
        right = pane.mapToScene(QtCore.QPointF(previous, pane.property("height") / 2)).toPoint()
        start = right + QtCore.QPoint(5, 0)
        end = start + QtCore.QPoint(60, 0)
        QtTest.QTest.mousePress(self.window, QtCore.Qt.LeftButton, QtCore.Qt.NoModifier, start)
        QtTest.QTest.mouseMove(self.window, end, 30)
        QtTest.QTest.mouseRelease(self.window, QtCore.Qt.LeftButton, QtCore.Qt.NoModifier, end)
        QtTest.QTest.qWait(30)
        self.assertGreater(pane.property("width"), previous + 30)
        self.assertAlmostEqual(self.backend.layout["fileWidth"], pane.property("width"), delta=1)
        self.assertTrue(self.backend.exportReady)
        self.assertEqual(self.warnings, [])
