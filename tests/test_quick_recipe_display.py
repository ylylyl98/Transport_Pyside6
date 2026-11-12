"""Display precision regressions for calculated Quick recipe coordinates."""
import os
import unittest
if os.environ.get("TRANSPORT_OFFLINE_VERIFICATION") != "1":
    raise unittest.SkipTest("Run through verify_offline.py")

from pathlib import Path
import tempfile
from PySide6 import QtCore, QtQml, QtTest, QtWidgets
from PySide6.QtQuickControls2 import QQuickStyle
from app.models import SaveRoot
from app.quick.measurements import MeasurementService
from tests.test_quick_measurements import FakeManager


class RecipeDisplayTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
        QQuickStyle.setStyle("Fusion")

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.service = MeasurementService(FakeManager(), SaveRoot(base=self.directory.name))
        self.service.mode = 5
        self.service.addRow("conditions")
        self.service.setValue("ratio", 2.3)
        self.service.setRowValue("conditions", 0, "doping", 2.0)
        self.service.setRowValue("conditions", 0, "efield", -2.0)
        self.engine = QtQml.QQmlApplicationEngine()
        self.warnings = []
        self.engine.warnings.connect(lambda items: self.warnings.extend(str(i) for i in items))
        self.engine.rootContext().setContextProperty("service", self.service)
        source = ('import QtQuick\nimport QtQuick.Controls\nApplicationWindow { '
                  'width: 700; height: 1000; visible: true; '
                  'MeasurementPane { width: parent.width; measurement: service } }')
        self.engine.loadData(source.encode(), QtCore.QUrl.fromLocalFile(str(
            Path(__file__).resolve().parents[1] / "app/quick/qml/FormsHarness.qml")))
        self.assertTrue(self.engine.rootObjects(), self.warnings)
        self.window = self.engine.rootObjects()[0]
        QtTest.QTest.qWait(40)

    def cell(self, key):
        model = self.service.rowModel("conditions")
        uid = model.data(model.index(0, 0), model.UidRole)
        name = f"recipeCell_conditions_{uid}_{key}"
        def descendants(item):
            yield item
            for child in item.childItems():
                yield from descendants(child)
        return next(item for item in descendants(self.window.contentItem()) if item.objectName() == name)

    def tearDown(self):
        if self.engine.rootObjects():
            self.engine.rootObjects()[0].hide()
        self.engine.deleteLater()
        self.app.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)
        self.service.deleteLater()
        self.app.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)
        self.directory.cleanup()
        self.assertEqual(self.warnings, [])

    def test_calculated_values_round_only_for_display_and_drafts_keep_focus(self):
        from app.gate_transform import derived_to_gates
        vtg, vbg = derived_to_gates(2.0, -2.0, 2.3, "Vbg")
        self.assertEqual(self.cell("vbg").property("text"), "0.869565")
        self.assertEqual(self.cell("vtg").property("text"), "0")
        values = self.service.rowModel("conditions").cells(0)
        self.assertEqual(next(x["value"] for x in values if x["key"] == "vbg"), vbg)
        self.assertNotEqual(vbg, 0.869565)
        editable = self.cell("doping")
        editable.forceActiveFocus()
        QtTest.QTest.keyClick(self.window, QtCore.Qt.Key.Key_A, QtCore.Qt.KeyboardModifier.ControlModifier)
        for char in "2.123456789":
            QtTest.QTest.keyClick(self.window, QtCore.Qt.Key(ord(char)))
        self.service.setValue("ratio", 2.4)
        self.app.processEvents()
        self.assertIs(self.cell("doping"), editable)
        self.assertEqual(editable.property("text"), "2.123456789")
        self.assertTrue(editable.hasActiveFocus())
        QtTest.QTest.keyClick(self.window, QtCore.Qt.Key.Key_Tab)
        self.app.processEvents()
        self.assertFalse(editable.hasActiveFocus())
        self.assertEqual(self.service._values[5]["conditions"][0]["doping"], 2.123456789)
