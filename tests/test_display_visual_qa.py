"""Offline screenshots of every Quick measurement page; no instruments are opened."""
from pathlib import Path
import math
import time
import unittest

from PySide6 import QtCore, QtQml, QtTest, QtWidgets
from PySide6.QtQuickControls2 import QQuickStyle

from app.quick.application import ApplicationBackend
from app.quick.plot import register_types

OUT = Path("tmp/verification/display_ui")


class DisplayVisualQATests(unittest.TestCase):
    def test_six_measurements_in_light_dark_and_narrow_windows(self):
        app = QtWidgets.QApplication.instance()
        self.assertIsNotNone(app, "Run this test with verify_offline.py")
        OUT.mkdir(parents=True, exist_ok=True)
        QQuickStyle.setStyle("Fusion")
        register_types()
        backend = ApplicationBackend(offline=True)
        measurement = backend._measurement
        engine = QtQml.QQmlApplicationEngine()
        warnings = []
        previous_handler = None
        def capture_qml_warning(kind, context, message):
            if kind == QtCore.QtMsgType.QtWarningMsg and ("qml" in message.lower() or ".qml" in str(context.file).lower()):
                warnings.append(message)
            if previous_handler is not None:
                previous_handler(kind, context, message)
        previous_handler = QtCore.qInstallMessageHandler(capture_qml_warning)
        self.addCleanup(QtCore.qInstallMessageHandler, previous_handler)
        engine.warnings.connect(lambda errors: warnings.extend(map(str, errors)))
        engine.rootContext().setContextProperty("appBackend", backend)
        engine.load(QtCore.QUrl.fromLocalFile(str(Path("app/quick/qml/Main.qml").resolve())))
        self.assertTrue(engine.rootObjects(), warnings)
        window = engine.rootObjects()[0]
        window.setProperty("showInstruments", False)
        window.setProperty("showParameters", True)
        window.setProperty("parameterWidth", 520)
        self.addCleanup(engine.deleteLater)
        self.addCleanup(backend.requestClose)
        self.addCleanup(window.hide)

        # Values remain exact in the model while their rendered labels/fields
        # must not silently turn a small nonzero value into zero.
        examples = [
            ("vds_sweep", "vds_start", "0.123456789012", "vds_step", "0.000005"),
            ("gate_scan", "raw_vtg_start", "0.123456789012", "raw_vds_start", "0.000005"),
            ("map", "vtg_start", "0.123456789012", "vtg_step", "0.000005"),
            ("photocurrent", "wl_start", "550.123456789", "wl_step", "0.000005"),
            ("bfield_gate_scan", "fields_t", "0.000005, 0.123456789012", None, None),
            ("bfield_transport", "start_field_t", "0.123456789012", "field_step_t", "0.005"),
        ]
        self.assertEqual(len(measurement.modes), len(examples))
        for mode, (name, large_key, large, small_key, small) in enumerate(examples):
            measurement.mode = mode
            measurement.setValue(large_key, large)
            if small_key:
                measurement.setValue(small_key, small)
            QtTest.QTest.qWait(120)
            self.assertEqual(measurement.mode, mode)
            actual = measurement._values[mode][large_key]
            if isinstance(actual, (int, float)):
                self.assertTrue(math.isclose(actual, float(large), rel_tol=1e-12, abs_tol=1e-15), (name, actual))
            else:
                self.assertIn("0.000005", str(actual))
            if small_key:
                self.assertNotEqual(measurement._values[mode][small_key], 0.0, (name, small_key))
            tabs = window.findChild(QtCore.QObject, "measurementMode")
            self.assertIsNotNone(tabs)
            self.assertEqual(tabs.property("currentIndex"), mode)
            scroll = window.findChild(QtCore.QObject, "parameterScroll")
            self.assertIsNotNone(scroll)
            content = scroll.property("contentItem")
            for width, height, size in ((1200, 850, "wide"), (900, 850, "narrow")):
                window.resize(width, height)
                window.setProperty("narrowPage", 0)
                for dark in (False, True):
                    backend.dark = dark
                    content.setProperty("contentY", 0)
                    QtTest.QTest.qWait(110)
                    image = window.grabWindow()
                    self.assertFalse(image.isNull(), (name, size, dark))
                    self.assertGreaterEqual(image.width(), width)
                    self.assertGreaterEqual(image.height(), height)
                    self.assertTrue(image.save(str(OUT / f"{name}_{size}_{'dark' if dark else 'light'}.png")))
                    # Bottom of long forms contains advanced and output controls.
                    content.setProperty("contentY", max(0, content.property("contentHeight") - content.property("height")))
                    QtTest.QTest.qWait(60)
                    self.assertTrue(window.grabWindow().save(str(OUT / f"{name}_{size}_{'dark' if dark else 'light'}_bottom.png")))
        # Expanded File output must remain usable in a narrow parameter pane.
        measurement.mode = 0
        long_name = "sample_long_condition_1234567890_" * 3
        measurement.setValue("base_name", long_name)
        window.resize(1200, 850)
        window.setProperty("narrowPage", 0)
        window.setProperty("showInstruments", False)
        QtTest.QTest.qWait(120)
        file_scroll = window.findChild(QtCore.QObject, "parameterScroll").property("contentItem")
        file_scroll.setProperty("contentY", 0)
        QtTest.QTest.qWait(80)
        def visual_items():
            pending = [window.contentItem()]
            while pending:
                item = pending.pop()
                yield item
                if hasattr(item, "childItems"):
                    pending.extend(item.childItems())
        header = next((item for item in visual_items()
                       if item.objectName() == "measurement_section_File output" and item.property("visible")), None)
        self.assertIsNotNone(header)
        header.clicked.emit()
        QtTest.QTest.qWait(150)
        for panel_width in (400, 520):
            window.setProperty("parameterWidth", panel_width)
            for dark in (False, True):
                backend.dark = dark
                file_scroll.setProperty("contentY", 0)
                QtTest.QTest.qWait(120)
                inputs = [item for item in visual_items()
                          if item.objectName() == "field_base_name" and item.property("visible")]
                self.assertTrue(inputs, "Expanded filename editor is missing")
                editor = inputs[0]
                self.assertEqual(editor.property("text"), long_name)
                self.assertGreater(editor.property("width"), panel_width * 0.4)
                self.assertTrue(window.grabWindow().save(str(
                    OUT / f"file_output_{panel_width}_{'dark' if dark else 'light'}.png")))

        # Inject presentation-only readbacks; no session is connected or queried.
        backend._instruments._readbacks["g1"] = {
            "set_voltage": 0.123456789012, "measured_voltage": 0.000005,
            "current": 2e-9, "current_compliance_a": 1e-6,
            "error": "SIMULATED READBACK ERROR",
        }
        backend._instruments.changed.emit()
        backend._magnet._snapshots["1000"] = {
            "output_field_t": 2e-6, "output_current_a": 2e-9,
            "magnet_voltage_v": 5e-6, "heater_on": False,
            "status": {"quench": True},
        }
        backend._magnet.changed.emit()
        window.setProperty("showInstruments", True)
        pane = window.findChild(QtCore.QObject, "instrumentPane")
        self.assertIsNotNone(pane)
        drawer = window.findChild(QtCore.QObject, "instrumentDrawer")
        self.assertIsNotNone(drawer)

        def wait_for_drawer():
            deadline = time.monotonic() + 1.5
            stable = 0
            previous = None
            while time.monotonic() < deadline:
                app.processEvents()
                QtTest.QTest.qWait(20)
                geometry = (window.width(), window.height(), float(drawer.property("position")),
                            float(drawer.property("x")), float(drawer.property("width")),
                            float(drawer.property("height")))
                if geometry == previous and geometry[2] >= 0.999:
                    stable += 1
                    if stable >= 3:
                        break
                else:
                    stable = 0
                previous = geometry
            self.assertGreaterEqual(float(drawer.property("position")), 0.999, "Drawer did not fully open")
            self.assertGreaterEqual(stable, 3, "Drawer layout did not settle")
            left = float(drawer.property("x"))
            right = left + float(drawer.property("width"))
            self.assertGreaterEqual(left, -1.0)
            self.assertLessEqual(right, window.width() + 1.0)


        def visible_texts():
            pending = [window.contentItem()]
            result = []
            while pending:
                item = pending.pop()
                if item.property("visible"):
                    value = item.property("text")
                    if isinstance(value, str):
                        result.append(value)
                    if hasattr(item, "childItems"):
                        pending.extend(item.childItems())
            return result

        for section, name in ((0, "manual"), (2, "magnet")):
            pane.setProperty("section", section)
            for width, height, size in ((1200, 850, "wide"), (900, 850, "narrow")):
                window.resize(width, height)
                for dark in (False, True):
                    backend.dark = dark
                    wait_for_drawer()
                    texts = visible_texts()
                    if name == "manual":
                        self.assertTrue(any("2 nA" in text for text in texts), texts)
                        self.assertTrue(any("SIMULATED READBACK ERROR" in text for text in texts), texts)
                    else:
                        self.assertTrue(any("2 nA" in text and "quench: ON" in text for text in texts), texts)
                    image = window.grabWindow()
                    self.assertFalse(image.isNull())
                    self.assertTrue(image.save(str(OUT / f"instrument_{name}_{size}_{'dark' if dark else 'light'}.png")))
        self.assertEqual(warnings, [], "\n".join(warnings))
        window.hide()
        backend.requestClose()
        for _ in range(20):
            app.processEvents()
