"""Offline interaction and visual checks for History numeric controls."""
import json
import os
from pathlib import Path
import tempfile
import time
import unittest

if os.environ.get("TRANSPORT_OFFLINE_VERIFICATION") != "1":
    raise unittest.SkipTest("Use verify_offline.py")

from PySide6 import QtCore, QtQml, QtTest, QtWidgets
from PySide6.QtQuickControls2 import QQuickStyle
from app.quick.history import HistoryBackend
from app.quick.plot import register_types

OUT = Path("tmp/verification/analysis_controls")


class Exporter(QtCore.QObject):
    completed = QtCore.Signal(object)
    def __init__(self):
        super().__init__()
        self.jobs = []
    @property
    def busy(self): return bool(self.jobs)
    def submit_snapshot(self, snapshot, output=None, cut_only=False, **kwargs):
        self.jobs.append((snapshot, output, cut_only, kwargs))
        return str(len(self.jobs))
    def shutdown(self): pass


class HistoryControlsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        QQuickStyle.setStyle("Fusion")
        register_types()

    def setUp(self):
        self.app = QtWidgets.QApplication.instance()
        self.scratch = tempfile.TemporaryDirectory()
        self.addCleanup(self.scratch.cleanup)
        self.root = Path(self.scratch.name)
        self.settings = QtCore.QSettings(str(self.root / "settings.ini"), QtCore.QSettings.Format.IniFormat)
        self.exporter = Exporter()
        self.backend = HistoryBackend(self.root, settings=self.settings, exporter=self.exporter, autostart=False)
        self.addCleanup(self.backend.shutdown)
        self.warnings = []
        previous = None
        def warn(kind, context, message):
            if kind == QtCore.QtMsgType.QtWarningMsg and ("qml" in message.lower() or ".qml" in str(context.file).lower()):
                self.warnings.append(message)
            if previous is not None: previous(kind, context, message)
        previous = QtCore.qInstallMessageHandler(warn)
        self.addCleanup(QtCore.qInstallMessageHandler, previous)
        self.engine = QtQml.QQmlApplicationEngine()
        self.engine.warnings.connect(lambda errors: self.warnings.extend(map(str, errors)))
        self.engine.rootContext().setContextProperty("historyBackend", self.backend)
        self.engine.rootContext().setContextProperty("historyConnected", False)
        self.engine.load(QtCore.QUrl.fromLocalFile(str(Path("app/quick/qml/HistoryWindow.qml").resolve())))
        self.assertTrue(self.engine.rootObjects(), self.warnings)
        self.window = self.engine.rootObjects()[0]
        QtQml.QQmlProperty(self.window, "width").write(1100)
        QtQml.QQmlProperty(self.window, "height").write(700)
        self.addCleanup(self.engine.deleteLater)
        self.addCleanup(self.window.hide)
        OUT.mkdir(parents=True, exist_ok=True)

    def write(self, name, kind, data, params=None):
        path = self.root / name
        path.write_text(data, encoding="utf-8")
        path.with_name(path.stem + "_metadata.json").write_text(json.dumps({
            "measurement": kind, "created_at": "2026-11-12T12:30:00", "status": "finished", "params": params or {}}), encoding="utf-8")
        return path

    def drain(self):
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            self.app.processEvents()
            if not self.backend.busy:
                QtTest.QTest.qWait(40)
                return
            QtTest.QTest.qWait(10)
        self.fail("History task did not settle: " + self.backend.status)

    def find(self, name):
        stack = [self.window.contentItem()]
        while stack:
            item = stack.pop()
            if item.objectName() == name:
                return item
            if hasattr(item, "childItems"): stack.extend(item.childItems())
        return self.window.findChild(QtCore.QObject, name)

    def screenshot(self, name):
        QtTest.QTest.qWait(150)
        image = self.window.grabWindow()
        self.assertEqual((image.width(), image.height()), (1100, 700))
        self.assertTrue(image.save(str(OUT / name)))

    def replace_text(self, item, text):
        item.forceActiveFocus()
        QtTest.QTest.qWait(50)
        QtTest.QTest.keyClick(self.window, QtCore.Qt.Key.Key_A, QtCore.Qt.KeyboardModifier.ControlModifier)
        for character in text:
            key = {"-": QtCore.Qt.Key.Key_Minus, ".": QtCore.Qt.Key.Key_Period, "+": QtCore.Qt.Key.Key_Plus}.get(character)
            if key is None: key = QtCore.Qt.Key(ord(character.upper()))
            QtTest.QTest.keyClick(self.window, key)
        QtTest.QTest.qWait(30)

    def close_window(self):
        self.window.setProperty("closeAllowed", True)
        self.window.close()
        self.assertEqual(self.warnings, [], "\n".join(self.warnings))

    def test_one_dimensional_range_dialog_and_saved_values(self):
        path = self.write("line.csv", "vds_sweep", "Vds,Ids_DC,Direction\nV,A,\n0,1e-9,forward\n1,3e-9,forward\n")
        self.backend.refresh(); self.drain()
        self.backend.setChecked(str(path), True); self.drain()
        self.assertFalse(self.backend.hasMap)
        button = self.find("historyRanges")
        self.assertIsNotNone(button)
        self.assertTrue(button.property("visible"))
        button.clicked.emit(); QtTest.QTest.qWait(150)
        dialog = self.find("historyRangeDialog")
        self.assertTrue(dialog.property("visible"))
        color = self.find("historyRange_color_mode")
        self.assertTrue(color is None or not color.property("visible"))
        for axis, low, high in (("x", "-0.123456789012", "0.987654321098"),
                                ("y", "-0.000000004123", "0.000000008456")):
            mode = self.find(f"historyRange_{axis}_mode")
            self.assertIsNotNone(mode)
            mode.setProperty("currentIndex", 1); mode.activated.emit(1)
            QtTest.QTest.qWait(80)
            lower, upper = self.find(f"historyRange_{axis}_lower"), self.find(f"historyRange_{axis}_upper")
            self.assertTrue(lower.property("visible")); self.assertTrue(upper.property("visible"))
            self.replace_text(lower, low); QtTest.QTest.keyClick(self.window, QtCore.Qt.Key.Key_Return)
            self.replace_text(upper, high); QtTest.QTest.keyClick(self.window, QtCore.Qt.Key.Key_Return)
            QtTest.QTest.qWait(80)
            self.assertEqual(self.backend.rangeState[axis]["bounds"], [float(low), float(high)])
            self.assertFalse(self.backend.rangeState[axis]["auto"])
        view = self.backend.capture_view()
        session = self.backend.capture_session()
        self.assertTrue(view["manual_x"])
        self.assertTrue(view["manual_y"][0])
        self.screenshot("range_1d_light.png")
        self.backend.setDark(True); self.screenshot("range_1d_dark.png")
        saved = self.root / "saved_session.json"
        saved.write_text(json.dumps(session), encoding="utf-8")
        clone = HistoryBackend(self.root, settings=self.settings, exporter=Exporter(), autostart=False)
        self.addCleanup(clone.shutdown)
        clone.restore_session(json.loads(saved.read_text(encoding="utf-8")))
        deadline = time.monotonic() + 8
        while clone.busy and time.monotonic() < deadline: QtTest.QTest.qWait(10)
        self.assertFalse(clone.busy)
        self.assertEqual(clone.capture_view()["xlim"], view["xlim"])
        self.assertEqual(clone.capture_view()["ylim"], view["ylim"])
        lower = self.find("historyRange_x_lower")
        self.replace_text(lower, "3"); QtTest.QTest.keyClick(self.window, QtCore.Qt.Key.Key_Return)
        QtTest.QTest.qWait(80)
        self.assertTrue(self.backend.rangeState["x"]["error"])
        self.assertEqual(self.backend.capture_view()["xlim"], view["xlim"])
        mode = self.find("historyRange_x_mode")
        mode.setProperty("currentIndex", 0); mode.activated.emit(0)
        QtTest.QTest.qWait(80)
        self.assertTrue(self.backend.rangeState["x"]["auto"])
        self.assertFalse(self.backend.rangeState["x"]["error"])
        self.close_window()

    def test_exact_measured_coordinate_and_empty_nan_cut(self):
        near1, near2, nan_row = 1.000000001, 1.000000002, 2.0
        data = ("Vtg,Vbg,Ids_DC\nV,V,A\n"
                f"0,{near1!r},1e-9\n1,{near1!r},2e-9\n"
                f"0,{near2!r},3e-9\n1,{near2!r},4e-9\n"
                f"0,{nan_row!r},nan\n1,{nan_row!r},nan\n")
        path = self.write("map.csv", "map_2d", data, {"axis_fast": "Vtg", "axis_slow": "Vbg"})
        self.backend.refresh(); self.drain()
        self.backend.selectType("map_2d"); self.backend.setChecked(str(path), True); self.drain()
        self.assertTrue(self.backend.hasMap)
        self.backend.setOption("fixed_axis", "Vbg"); self.drain()
        options = self.backend.measuredCoordinateOptions
        near = [item for item in options if item["value"] in (near1, near2)]
        self.assertEqual(len(near), 2, options)
        self.assertNotEqual(near[0]["label"], near[1]["label"])
        self.assertEqual({item["value"] for item in near}, {near1, near2})
        self.assertTrue(all(float(item["exact"]) == item["value"] for item in near))
        coordinate = self.find("historyCutCoordinate")
        self.assertIsNotNone(coordinate)
        self.assertTrue(coordinate.property("visible"))
        index = next(i for i, item in enumerate(options) if item["value"] == near2)
        coordinate.setProperty("currentIndex", index); coordinate.activated.emit(index)
        self.drain()
        self.assertEqual(self.backend.state["fixed_value"], near2)
        for dark in (False, True):
            self.backend.setDark(dark)
            arrow = coordinate.mapToScene(QtCore.QPointF(coordinate.width() - 8, coordinate.height() / 2)).toPoint()
            QtTest.QTest.mouseClick(self.window, QtCore.Qt.MouseButton.LeftButton, QtCore.Qt.KeyboardModifier.NoModifier, arrow)
            QtTest.QTest.qWait(100)
            self.assertEqual(float(coordinate.property("editText")), near2)
            self.screenshot("coordinate_options_" + ("dark" if dark else "light") + ".png")
            QtTest.QTest.keyClick(self.window, QtCore.Qt.Key.Key_Escape)
            QtTest.QTest.qWait(60)
        editor = coordinate.property("contentItem")
        editor.forceActiveFocus(); QtTest.QTest.qWait(100)
        self.assertEqual(float(coordinate.property("editText")), near2)
        self.find("historyExportView").forceActiveFocus(); QtTest.QTest.qWait(100)
        self.assertEqual(self.backend.state["fixed_value"], near2)
        self.replace_text(editor, "1.123456789123")
        draft = coordinate.property("editText")
        self.assertEqual(draft, "1.123456789123")
        self.backend.changed.emit(); QtTest.QTest.qWait(100)
        self.assertEqual(coordinate.property("editText"), draft)
        self.assertEqual(self.backend.state["fixed_value"], near2)
        QtTest.QTest.keyClick(self.window, QtCore.Qt.Key.Key_Escape)
        QtTest.QTest.qWait(80)
        self.assertFalse(coordinate.property("dirty"))
        self.assertEqual(self.backend.state["fixed_value"], near2)
        self.replace_text(editor, str(near1))
        QtTest.QTest.keyClick(self.window, QtCore.Qt.Key.Key_Return); self.drain()
        self.assertEqual(self.backend.state["fixed_value"], near1)
        self.assertFalse(coordinate.property("dirty"))
        self.replace_text(editor, "7.123456789")
        self.assertTrue(coordinate.property("dirty"))
        self.backend.setOption("fixed_axis", "Vtg"); self.drain()
        self.assertFalse(coordinate.property("dirty"))
        self.assertNotEqual(coordinate.property("editText"), "7.123456789")
        self.assertNotEqual(self.backend.state["fixed_value"], 7.123456789)
        self.backend.setOption("fixed_axis", "Vbg"); self.drain()
        self.backend.setOption("fixed_value", near1); self.drain()
        self.replace_text(editor, "1e")
        QtTest.QTest.keyClick(self.window, QtCore.Qt.Key.Key_Return); QtTest.QTest.qWait(80)
        self.assertTrue(coordinate.property("invalid"))
        self.assertEqual(self.backend.state["fixed_value"], near1)
        QtTest.QTest.keyClick(self.window, QtCore.Qt.Key.Key_Escape); QtTest.QTest.qWait(80)
        self.assertFalse(coordinate.property("dirty"))
        self.assertEqual(self.backend.state["fixed_value"], near1)
        self.backend.setOption("fixed_value", nan_row); self.drain()
        self.assertTrue(self.backend.hasMap)
        self.assertTrue(self.backend.exportReady)
        self.assertFalse(self.backend.cutHasData)
        self.assertFalse(self.backend.cutReady)
        self.find("historyPlotTabs").setProperty("currentIndex", 1); QtTest.QTest.qWait(100)
        cut = self.find("historyCutPlot")
        self.assertIsNotNone(cut)
        empty = cut.findChild(QtCore.QObject, "plotEmptyText")
        self.assertIsNotNone(empty)
        self.assertTrue(empty.property("visible"))
        self.assertTrue(empty.property("text"))
        for dark in (False, True):
            self.backend.setDark(dark)
            self.screenshot("nan_cut_" + ("dark" if dark else "light") + ".png")
        button = self.find("historyExportView")
        self.assertTrue(button.property("enabled")); button.clicked.emit(); QtTest.QTest.qWait(150)
        heat = self.find("historyExportHeatmapItem")
        cut_item = self.find("historyExportCutItem")
        both_item = self.find("historyExportBothItem")
        for item in (heat, cut_item, both_item): self.assertIsNotNone(item)
        self.assertTrue(heat.property("enabled"))
        self.assertFalse(cut_item.property("enabled"))
        self.assertFalse(both_item.property("enabled"))
        self.backend.exportView("cut", ""); self.assertEqual(self.exporter.jobs, [])
        self.backend.exportView("both", ""); self.assertEqual(self.exporter.jobs, [])
        self.backend.exportView("heatmap", "")
        self.assertEqual(len(self.exporter.jobs), 1)
        self.assertTrue(self.exporter.jobs[0][3]["heatmap_only"])
        self.close_window()

    def test_two_channel_independent_y_ranges_and_draft_isolation(self):
        path = self.write("two.csv", "vds_sweep",
                          "Vds,Ids_DC,Ids_X,Direction\nV,A,A,\n0,1e-9,2e-9,forward\n1,3e-9,6e-9,forward\n")
        self.backend.refresh(); self.drain()
        self.backend.setChecked(str(path), True); self.drain()
        self.backend.plotModel.setYAxes("Ids_DC", "Ids_X", "two")
        self.backend._schedule_plot(); self.drain()
        self.assertEqual(len(self.backend.rangeChannels), 2)
        button = self.find("historyRanges"); button.clicked.emit(); QtTest.QTest.qWait(120)
        selector = self.find("historyRangeChannel")
        self.assertTrue(selector.property("visible"))
        channels = self.backend.rangeChannels
        def choose(index):
            selector.setProperty("currentIndex", index); selector.activated.emit(index)
            QtTest.QTest.qWait(80)
            self.assertEqual(self.backend.rangeChannel, channels[index])
        def fixed_y(low, high):
            mode = self.find("historyRange_y_mode")
            mode.setProperty("currentIndex", 1); mode.activated.emit(1); QtTest.QTest.qWait(70)
            lower, upper = self.find("historyRange_y_lower"), self.find("historyRange_y_upper")
            self.replace_text(lower, low); QtTest.QTest.keyClick(self.window, QtCore.Qt.Key.Key_Return)
            self.replace_text(upper, high); QtTest.QTest.keyClick(self.window, QtCore.Qt.Key.Key_Return)
            QtTest.QTest.qWait(60)
            self.assertEqual(self.backend.rangeState["y"]["bounds"], [float(low), float(high)])
        choose(0); fixed_y("-0.000000004", "0.000000004")
        choose(1); fixed_y("-0.000000008", "0.000000008")
        choose(0)
        self.assertEqual(self.backend.rangeState["y"]["bounds"], [-4e-9, 4e-9])
        lower = self.find("historyRange_y_lower")
        self.replace_text(lower, "-0.000000003")
        self.assertTrue(lower.property("dirty"))
        choose(1)
        self.assertFalse(lower.property("dirty"))
        self.assertEqual(self.backend.rangeState["y"]["bounds"], [-8e-9, 8e-9])
        self.assertNotEqual(lower.property("text"), "-0.000000003")
        self.backend.setDark(False); self.screenshot("range_two_channels_light.png")
        self.backend.setDark(True); self.screenshot("range_two_channels_dark.png")
        self.close_window()
