"""Real Quick History window coverage, filtering and visual regression, offline."""
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

OUT = Path("tmp/verification/analysis_ui")


class FakeExporter(QtCore.QObject):
    completed = QtCore.Signal(object)
    busy = False
    def shutdown(self):
        pass


class HistoryPresentationTests(unittest.TestCase):
    def setUp(self):
        self.app = QtWidgets.QApplication.instance()
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.addCleanup(self.temp.cleanup)
        self.settings = QtCore.QSettings(str(self.root / "settings.ini"), QtCore.QSettings.Format.IniFormat)
        self.backend = HistoryBackend(self.root, settings=self.settings, exporter=FakeExporter(), autostart=False)
        self.addCleanup(self.backend.shutdown)
        OUT.mkdir(parents=True, exist_ok=True)

    def write_data(self, name, fields, row, role, bias2="Disabled"):
        path = self.root / name
        units = ["" if key == "Direction" else "V" if key in ("Vds", "Bias2") else "A" for key in fields]
        second = [(.2 if key == "Vds" else .325 if key == "Bias2" else value * 2
                   if isinstance(value, (float, int)) else value) for key, value in zip(fields, row)]
        path.write_text(",".join(fields) + "\n" + ",".join(units) + "\n"
                        + ",".join(map(str, row)) + "\n" + ",".join(map(str, second)) + "\n",
                        encoding="utf-8")
        metadata = {"measurement": "vds_sweep", "created_at": "2026-11-12T12:30:00", "status": "finished",
                    "params": {"vds_source": "Keithley 2400", "bias2_mode": bias2,
                               "bias2_offset": .125, "vds_start": 0, "vds_stop": 1},
                    "signal_chain": {"drag_drive": {"mode": role, "verified": True,
                                    "series_resistance_ohm": 100000}}}
        path.with_name(path.stem + "_metadata.json").write_text(json.dumps(metadata), encoding="utf-8")
        return path

    def drain(self):
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            self.app.processEvents()
            if not self.backend.busy:
                return
            QtTest.QTest.qWait(10)
        self.fail("History background task did not settle: " + self.backend.status)

    def test_mixed_coverage_hidden_selection_and_details_visuals(self):
        drive = self.write_data("single_drive_follow.csv", ["Vds", "Ids_DC", "I_drive_X", "Bias2", "Ibias2_Keithley", "Direction"],
                                [0.1, 1e-9, 2e-9, .225, 3e-9, "forward"], "single_drive", "Follow")
        drag = self.write_data("single_drag.csv", ["Vds", "Ids_DC", "I_drag_X", "Direction"],
                               [0.1, 1e-9, 4e-9, "forward"], "single_drag")
        bias = self.write_data("fixed_bias.csv", ["Vds", "Ids_DC", "Bias2", "Ibias2_Keithley", "Direction"],
                               [0.1, 1e-9, .2, 3e-9, "forward"], "ordinary", "Fixed")
        self.backend.refresh()
        self.drain()
        self.assertEqual(len(self.backend.files), 3)
        for path in (drive, drag, bias):
            self.backend.setChecked(str(path), True)
        self.drain()
        self.assertIn("I_drive_X", self.backend.signalChoices)
        self.backend.setOption("signal", "I_drive_X")
        self.drain()
        self.assertEqual(self.backend.selectedCount, 3)
        self.assertEqual(self.backend.coverageSummary, "1 / 3 files displayed")
        self.assertIn("single_drag.csv", self.backend.coverageDetails)
        self.assertIn("fixed_bias.csv", self.backend.coverageDetails)
        self.backend.setOption("search", "single_drive")
        self.assertEqual(self.backend.hiddenSelectedCount, 2)
        self.assertEqual(self.backend.selectedCount, 3)

        QQuickStyle.setStyle("Fusion")
        register_types()
        engine = QtQml.QQmlApplicationEngine()
        warnings = []
        previous_handler = None
        def capture_warning(kind, context, message):
            if kind == QtCore.QtMsgType.QtWarningMsg and ("qml" in message.lower() or ".qml" in str(context.file).lower()):
                warnings.append(message)
            if previous_handler is not None:
                previous_handler(kind, context, message)
        previous_handler = QtCore.qInstallMessageHandler(capture_warning)
        self.addCleanup(QtCore.qInstallMessageHandler, previous_handler)
        engine.warnings.connect(lambda errors: warnings.extend(map(str, errors)))
        engine.rootContext().setContextProperty("historyBackend", self.backend)
        engine.rootContext().setContextProperty("historyConnected", False)
        engine.load(QtCore.QUrl.fromLocalFile(str(Path("app/quick/qml/HistoryWindow.qml").resolve())))
        self.assertTrue(engine.rootObjects(), warnings)
        window = engine.rootObjects()[0]
        self.addCleanup(engine.deleteLater)
        self.addCleanup(window.hide)

        def find_visual(name):
            stack = [window.contentItem()]
            while stack:
                item = stack.pop()
                if item.objectName() == name:
                    return item
                if hasattr(item, "childItems"):
                    stack.extend(item.childItems())
            return window.findChild(QtCore.QObject, name)

        for width, height, size in ((1100, 700, "minimum"), (1480, 900, "standard")):
            for dark in (False, True):
                self.backend.setDark(dark)
                QtQml.QQmlProperty(window, "width").write(width)
                QtQml.QQmlProperty(window, "height").write(height)
                QtTest.QTest.qWait(250)
                self.assertEqual((window.width(), window.height()), (width, height))
                for name in ("historyFilePane", "historyMainPlot", "historyCoverageSummary", "historyHiddenSelection"):
                    item = find_visual(name)
                    self.assertIsNotNone(item, name)
                    self.assertTrue(item.property("visible"), name)
                    self.assertGreater(item.property("width"), 20, name)
                    origin = item.mapToScene(QtCore.QPointF(0, 0))
                    self.assertGreaterEqual(origin.x(), -1, name)
                    self.assertLessEqual(origin.x() + item.property("width"), width + 1, name)
                    self.assertGreaterEqual(origin.y(), -1, name)
                    self.assertLessEqual(origin.y() + item.property("height"), height + 1, name)
                hidden = find_visual("historyHiddenSelection")
                self.assertIn("2 selected files hidden", hidden.property("text"))
                coverage = find_visual("historyCoverageSummary")
                self.assertEqual(coverage.property("text"), "1 / 3 files displayed")
                image = window.grabWindow()
                self.assertEqual((image.width(), image.height()), (width, height))
                self.assertTrue(image.save(str(OUT / f"coverage_{size}_{'dark' if dark else 'light'}.png")))
                button = find_visual("historyCoverageButton")
                self.assertTrue(button.property("enabled"))
                button.clicked.emit()
                QtTest.QTest.qWait(180)
                dialog = window.findChild(QtCore.QObject, "historyCoverageDetails")
                self.assertIsNotNone(dialog)
                self.assertTrue(dialog.property("visible"))
                details = window.findChild(QtCore.QObject, "historyCoverageText")
                if details is None:
                    details = find_visual("historyCoverageText")
                self.assertIsNotNone(details)
                self.assertIn("single_drag.csv", details.property("text"))
                self.assertIn("single_drive_follow.csv", details.property("text"))
                self.assertIn("Missing column", details.property("text"))
                self.assertTrue(window.grabWindow().save(str(OUT / f"details_{size}_{'dark' if dark else 'light'}.png")))
                QtCore.QMetaObject.invokeMethod(dialog, "close")
                QtTest.QTest.qWait(80)
        self.assertEqual(warnings, [], "\n".join(warnings))
        window.setProperty("closeAllowed", True)
        window.close()
