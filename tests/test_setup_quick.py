"""Configured hardware isolation and complete Quick navigation, without hardware."""
import os
from pathlib import Path
import time
import unittest
from unittest.mock import patch

if os.environ.get("TRANSPORT_OFFLINE_VERIFICATION") != "1":
    raise unittest.SkipTest("Run through verify_offline.py")

from PySide6 import QtCore, QtQml, QtQuickControls2, QtTest
from app.quick.application import ApplicationBackend
from app.quick.magnet import MagnetService
from app.quick.plot import register_types
from app.settings import get_app_settings
from utils.config import cfg, SetupConfig


class SetupQuickTests(unittest.TestCase):
    def find_item(self, window, name):
        def walk(item):
            yield item
            for child in item.childItems():
                yield from walk(child)
        return next((item for item in walk(window.contentItem()) if item.objectName() == name), None)

    def test_none_never_constructs_or_operates_hardware(self):
        with patch.object(cfg, "setup", SetupConfig("none", "external")), \
             patch("app.quick.magnet.MagnetController") as aps, \
             patch("app.quick.magnet.AttoDRY2100Controller") as sdk, \
             patch("app.quick.magnet.LakeShore335Controller") as lake:
            service = MagnetService()
            try:
                for action in ("detect", "connect", "connect_temperature", "move", "temperature", "stop", "heater_off"):
                    service.command(action)
                    self.assertIn("No magnet", service.message)
                service.request_stop()
                self.assertTrue(service.is_ready())
                self.assertEqual(service.measurement_settings(), (False, None))
                self.assertEqual(service.fields, [])
                self.assertFalse(service._timer.isActive())
                context = service.experiment_context_snapshot()
                self.assertEqual(context["temperature_source"], "external")
                self.assertIsNone(context["field_t"])
                self.assertIsNone(context["sample_temperature_k"])
                self.assertFalse(context["magnet1000"]["available"])
                self.assertFalse(context["magnet2100"]["available"])
                service.selectBackend("2100")
                self.assertEqual(service.backend, "none")
            finally:
                self.assertTrue(service.close())
            aps.assert_not_called()
            sdk.assert_not_called()
            lake.assert_not_called()

    def test_full_window_for_each_setup(self):
        register_types()
        QtQuickControls2.QQuickStyle.setStyle("Fusion")
        for backend, source, ids in (("1000", "ls335", [0, 1, 2, 3, 4, 5]),
                                     ("2100", "2100", [0, 1, 2, 3, 5]),
                                     ("none", "external", [0, 1, 2, 3])):
            with self.subTest(backend=backend), patch.object(cfg, "setup", SetupConfig(backend, source)):
                get_app_settings().clear()
                model = ApplicationBackend(offline=True)
                engine = QtQml.QQmlApplicationEngine()
                warnings = []
                engine.warnings.connect(lambda items: warnings.extend(str(item) for item in items))
                engine.rootContext().setContextProperty("appBackend", model)
                window = None
                try:
                    self.assertEqual(model.magnet.magnet1000 is None, backend != "1000")
                    self.assertEqual(model.magnet.magnet2100 is None, backend != "2100")
                    self.assertEqual(model.magnet.lakeshore335 is None, backend != "1000")
                    self.assertEqual([item["id"] for item in model.measurement.availableModes], ids)
                    engine.loadData(b"import QtQuick\nMain { width: 1440; height: 900 }",
                                    QtCore.QUrl.fromLocalFile(str(Path(__file__).resolve().parents[1] /
                                                                 "app/quick/qml/SetupHarness.qml")))
                    self.assertTrue(engine.rootObjects(), "\n".join(warnings))
                    window = engine.rootObjects()[0]
                    window.setProperty("pinInstruments", True)
                    window.setProperty("showInstruments", True)
                    window.requestActivate()
                    QtTest.QTest.qWait(100)
                    for mode in range(6):
                        self.assertEqual(self.find_item(window, "measurementTab" + str(mode)) is not None, mode in ids)
                    last = self.find_item(window, "measurementTab" + str(ids[-1]))
                    center = last.mapToScene(QtCore.QPointF(last.width() / 2, last.height() / 2)).toPoint()
                    QtTest.QTest.mouseClick(window, QtCore.Qt.LeftButton, QtCore.Qt.NoModifier, center)
                    QtTest.QTest.qWait(60)
                    self.assertEqual(model.measurement.mode, ids[-1])
                    if backend == "2100":
                        self.assertEqual(model.measurement._values[5]["measurement_mode"], "stepped")
                        self.assertFalse(any(section["title"] == "Sample B current reversal"
                                             for section in model.measurement.uiSections))
                    pane = window.findChild(QtCore.QObject, "instrumentPane")
                    pane.setProperty("section", 2)
                    QtTest.QTest.qWait(60)
                    self.assertEqual(pane.property("section"), 0 if backend == "none" else 2)
                    if backend == "none":
                        self.assertFalse(self.find_item(window, "instrumentPage2").isVisible())
                        self.assertFalse(self.find_item(window, "stopFieldNotice").isVisible())
                        self.assertEqual([item["key"] for item in model.runtime.fields], ["font_size_pt"])
                    target = Path(__file__).resolve().parents[1] / "tmp" / "setup_validation" / (backend + ".png")
                    target.parent.mkdir(parents=True, exist_ok=True)
                    self.assertTrue(window.grabWindow().save(str(target)))
                    self.assertEqual(warnings, [])
                finally:
                    if window is not None:
                        window.hide()
                    model.requestClose()
                    deadline = time.monotonic() + 5
                    while not model.closeAccepted and time.monotonic() < deadline:
                        QtTest.QTest.qWait(10)
                    self.assertTrue(model.closeAccepted, model.message)
                    engine.deleteLater()
                    model.deleteLater()
                    QtCore.QCoreApplication.sendPostedEvents(None, QtCore.QEvent.DeferredDelete)
                    QtCore.QCoreApplication.processEvents()


if __name__ == "__main__":
    unittest.main()
