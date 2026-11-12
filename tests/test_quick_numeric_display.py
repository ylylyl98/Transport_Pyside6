import os
import unittest
from pathlib import Path
from PySide6 import QtCore, QtQml, QtTest, QtWidgets
from PySide6.QtQuickControls2 import QQuickStyle

if os.environ.get('TRANSPORT_OFFLINE_VERIFICATION') != '1':
    raise unittest.SkipTest('Use verify_offline.py')

class NumericDisplayTests(unittest.TestCase):
    def setUp(self):
        self.app = QtWidgets.QApplication.instance()
        QQuickStyle.setStyle("Fusion")
        self.engine = QtQml.QQmlApplicationEngine()
        self.warnings = []
        self.engine.warnings.connect(lambda items: self.warnings.extend(map(str, items)))
        qml = '''import QtQuick
import QtQuick.Controls
import "NumberFormat.js" as N
ApplicationWindow {
  id: win
  width: 520; height: 400; visible: true
  property var value: 0.123456789123
  property int commits: 0
  property string small: N.compact(0.0000000000123456789)
  property string amps: N.quantity(0.00000000123456789, "A")
  property string missing: N.compact(NaN)
  property string infinity: N.compact(Infinity)
  property string minusZero: N.compact(-0)
  property string negativeTiny: N.compact(-1.23456789e-20)
  property string trillion: N.compact(1000000000000)
  property string micro: N.compact(0.000005)
  QtObject {
    id: backend
    property var rangeState: ({x: {auto: false, bounds: [0.123456789123, 0.987654321987], error: "", unit: "V", title: "Range"}})
    property int calls: 0
    property string lastLower: ""
    property string lastUpper: ""
    signal rangeChanged()
    function setRange(axis, autoMode, low, high) { calls++; lastLower=low; lastUpper=high; rangeState = {x: {auto: autoMode, bounds: [Number(low), Number(high)], error: "", unit: "V", title: "Range"}}; rangeChanged() }
  }
  property alias rangeBackend: backend
  Column {
    FieldEditor { objectName: "editor"; formField: ({key: "n", kind: "number", label: "N", value: win.value}); onCommitted: v => { win.commits++; win.value=Number(v) } }
    HistoryRange { axis: "x"; label: "X"; backend: backend }
    Button { objectName: "other"; text: "Other" }
  }
}'''
        self.engine.loadData(qml.encode(), QtCore.QUrl.fromLocalFile(str(Path('app/quick/qml/NumericHarness.qml').resolve())))
        self.assertTrue(self.engine.rootObjects(), self.warnings)
        self.window = self.engine.rootObjects()[0]
        self.window.requestActivate()
        QtTest.QTest.qWait(100)

    def tearDown(self):
        self.window.hide(); self.engine.deleteLater()
        self.app.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)
        self.assertEqual(self.warnings, [])

    def find(self, name):
        obj = self.window.findChild(QtCore.QObject, name)
        self.assertIsNotNone(obj, name)
        return obj

    def type(self, item, value):
        item.forceActiveFocus()
        QtTest.QTest.keyClick(self.window, QtCore.Qt.Key_A, QtCore.Qt.ControlModifier)
        for char in value:
            QtTest.QTest.keyClick(self.window, ord(char.upper()))
        QtTest.QTest.keyClick(self.window, QtCore.Qt.Key_Return)
        self.app.processEvents()

    def test_focus_enter_and_range_preserve_precision(self):
        field = self.find('field_n'); other = self.find('other')
        self.assertEqual(field.property('text'), '0.123457')
        field.forceActiveFocus(); self.app.processEvents()
        self.assertEqual(field.property('text'), '0.123456789123')
        other.forceActiveFocus(); self.app.processEvents()
        self.assertEqual(self.window.property('commits'), 0)
        self.assertEqual(self.window.property('value'), .123456789123)
        self.type(field, '0.25')
        QtTest.QTest.keyClick(self.window, QtCore.Qt.Key_Tab)
        self.assertEqual(self.window.property('commits'), 1)
        lower = self.find('historyRange_x_lower')
        backend = self.window.property('rangeBackend')
        lower.forceActiveFocus(); self.app.processEvents()
        self.assertEqual(lower.property('text'), '0.123456789123')
        other.forceActiveFocus(); self.app.processEvents()
        self.assertEqual(backend.property('calls'), 0)
        self.type(lower, '0.5')
        self.assertEqual(backend.property('lastUpper'), '0.987654321987')
        self.assertEqual(self.window.property('small'), '1.23457e-11')
        self.assertEqual(self.window.property('amps'), '1.23457 nA')
        self.assertEqual(self.window.property('missing'), '—')
        self.assertEqual(self.window.property('infinity'), '—')
        self.assertEqual(self.window.property('minusZero'), '0')
        self.assertEqual(self.window.property('negativeTiny'), '-1.23457e-20')
        self.assertEqual(self.window.property('trillion'), '1e+12')
        self.assertEqual(self.window.property('micro'), '5e-6')
