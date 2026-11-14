import os
import unittest
from pathlib import Path

if os.environ.get("TRANSPORT_OFFLINE_VERIFICATION") != "1":
    raise unittest.SkipTest("Run with verify_offline.py")

from PySide6 import QtCore, QtGui, QtQml, QtTest, QtWidgets
from PySide6.QtQuickControls2 import QQuickStyle

ROOT = Path(__file__).resolve().parents[1]

class ResponsiveFormTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        QQuickStyle.setStyle("Fusion")

    def test_width_editing_and_focus(self):
        engine = QtQml.QQmlEngine()
        warnings = []
        engine.warnings.connect(lambda items: warnings.extend(str(item) for item in items))
        component = QtQml.QQmlComponent(engine)
        qml = '''import QtQuick
import QtQuick.Controls
import "." as UI
ApplicationWindow {
    width: 520; height: 500; visible: true
    QtObject { id: target; property string lastKey: ""; property var lastValue
        function setValue(key, value) { lastKey = key; lastValue = value }
    }
    UI.FieldForm {
        objectName: "form"; width: parent.width - 32; anchors.horizontalCenter: parent.horizontalCenter
        anchors.top: parent.top; anchors.topMargin: 16; target: target
        function refreshDescriptors() { fields = fields.map(field => Object.assign({}, field)) }
        fields: [
          {key:"a", label:"Current source protection limit during a controlled ramp (A)", kind:"number", value:1, group:"Settings"},
          {key:"b", label:"Delay (s)", kind:"integer", value:2, group:"Settings"},
          {key:"path", label:"File path", kind:"text", value:"C:/data/measurement", group:"Settings"},
          {key:"c", label:"Mode", kind:"choice", value:"A", options:["A", "B"], group:"Other"},
          {key:"d", label:"Enabled", kind:"bool", value:true, group:"Other"}
        ]
    }
    property alias lastKey: target.lastKey
    property alias lastValue: target.lastValue
}'''
        component.setData(qml.encode(), QtCore.QUrl.fromLocalFile(str(ROOT / 'app/quick/qml/ResponsiveTest.qml')))
        while component.isLoading():
            QtTest.QTest.qWait(10)
        window = component.create()
        self.assertIsNotNone(window, [e.toString() for e in component.errors()])
        QtTest.QTest.qWait(100)
        def find_visual(node, name):
            if node.objectName() == name:
                return node
            if hasattr(node, "childItems"):
                for child in node.childItems():
                    found = find_visual(child, name)
                    if found: return found
            return None
        def item(key):
            return find_visual(window.contentItem(), 'field_' + key)
        a, b, path = item('a'), item('b'), item('path')
        self.assertTrue(all((a, b, path)), warnings)
        def xy(obj):
            return obj.mapToScene(QtCore.QPointF(0, 0))
        # A wrapped label stays attached to its own editor; the following row
        # begins only after both editors are complete.
        self.assertGreater(xy(a).y(), xy(b).y() + 5)
        self.assertGreater(xy(b).x(), xy(a).x() + 100)
        self.assertGreater(path.width(), a.width())
        self.assertGreater(xy(path).y(), max(xy(a).y() + a.height(), xy(b).y() + b.height()))
        output = ROOT / 'tmp/verification'
        output.mkdir(parents=True, exist_ok=True)
        self.assertTrue(window.grabWindow().save(str(output / 'responsive_form_wide.png')))
        a.forceActiveFocus()
        QtTest.QTest.keyClick(window, QtCore.Qt.Key.Key_A, QtCore.Qt.KeyboardModifier.ControlModifier)
        for key in (QtCore.Qt.Key.Key_3, QtCore.Qt.Key.Key_Period, QtCore.Qt.Key.Key_5):
            QtTest.QTest.keyClick(window, key)
        form = window.findChild(QtCore.QObject, 'form')
        QtCore.QMetaObject.invokeMethod(form, 'refreshDescriptors')
        self.assertEqual(a.property('text'), '3.5')
        self.assertTrue(a.hasActiveFocus())
        QtTest.QTest.keyClick(window, QtCore.Qt.Key.Key_Tab)
        QtTest.QTest.qWait(50)
        self.assertEqual(window.property('lastKey'), 'a')
        self.assertEqual(window.property('lastValue'), '3.5')
        self.assertTrue(b.hasActiveFocus())
        window.setWidth(330)
        QtTest.QTest.qWait(100)
        self.assertGreater(xy(b).y(), xy(a).y() + 10)
        self.assertAlmostEqual(xy(b).x(), xy(a).x(), delta=2)
        self.assertTrue(window.grabWindow().save(str(output / 'responsive_form_narrow.png')))
        self.assertEqual(warnings, [])
        window.close()
        window.deleteLater()



    def test_main_measurement_resizes_with_instruments(self):
        from app.quick.application import ApplicationBackend
        from app.quick.plot import register_types
        register_types()
        backend = ApplicationBackend(offline=True)
        engine = QtQml.QQmlApplicationEngine()
        warnings = []
        engine.warnings.connect(lambda items: warnings.extend(str(item) for item in items))
        engine.rootContext().setContextProperty("appBackend", backend)
        engine.load(QtCore.QUrl.fromLocalFile(str(ROOT / "app/quick/qml/Main.qml")))
        self.assertTrue(engine.rootObjects(), warnings)
        window = engine.rootObjects()[0]
        window.setWidth(1200)
        window.setHeight(750)
        window.setProperty("showInstruments", False)
        QtTest.QTest.qWait(200)
        output = ROOT / "tmp/verification"
        self.assertTrue(window.grabWindow().save(str(output / "measurement_wide.png")))
        window.setWidth(800)
        window.setProperty("showInstruments", True)
        QtTest.QTest.qWait(200)
        self.assertTrue(window.grabWindow().save(str(output / "measurement_narrow_instruments.png")))
        drawer = window.findChild(QtCore.QObject, "instrumentDrawer")
        self.assertAlmostEqual(drawer.property("width"), window.width(), delta=1)
        self.assertGreater(drawer.property("y"), 0)
        self.assertLess(drawer.property("y") + drawer.property("height"), window.height())
        pane = window.findChild(QtCore.QObject, "measurementPane")
        self.assertIsNotNone(pane)
        def visible_inputs(node):
            result = []
            if node.objectName().startswith("field_") and node.isVisible(): result.append(node)
            for child in node.childItems(): result.extend(visible_inputs(child))
            return result
        for control in visible_inputs(pane):
            point = control.mapToItem(pane, QtCore.QPointF(0, 0))
            self.assertLessEqual(point.x() + control.width(), pane.width() + 1, control.objectName())
        self.assertEqual(warnings, [])
        window.hide()
        backend.requestClose()
        QtWidgets.QApplication.processEvents()
        engine.deleteLater()
