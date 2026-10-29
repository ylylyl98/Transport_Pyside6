"""Offline QML regression checks for instrument drafts and lazy panel creation."""
import os
import unittest
from pathlib import Path
from types import SimpleNamespace

if os.environ.get("TRANSPORT_OFFLINE_VERIFICATION") != "1":
    raise unittest.SkipTest("Run with verify_offline.py")

from PySide6 import QtCore, QtQml, QtTest, QtWidgets
from PySide6.QtQuickControls2 import QQuickStyle
from app.quick.application import ApplicationBackend


class QuickInstrumentResponsivenessTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        QQuickStyle.setStyle("Fusion")

    def setUp(self):
        self.app = QtWidgets.QApplication.instance()
        self.backend = ApplicationBackend(offline=True)
        self.engine = QtQml.QQmlApplicationEngine()
        self.warnings = []
        self.engine.warnings.connect(lambda items: self.warnings.extend(str(i) for i in items))
        self.engine.rootContext().setContextProperty("backendForTest", self.backend)
        self.engine.loadData(b"""
            import QtQuick
            import QtQuick.Controls
            ApplicationWindow {
                width: 600; height: 800; visible: true
                InstrumentPane { anchors.fill: parent; appBackend: backendForTest }
            }
        """, QtCore.QUrl.fromLocalFile(str(Path(__file__).resolve().parents[1] / "app/quick/qml/InstrumentTest.qml")))
        self.assertTrue(self.engine.rootObjects(), self.warnings)
        self.window = self.engine.rootObjects()[0]
        self.pane = self.window.findChild(QtCore.QObject, "instrumentPane")
        self.app.processEvents()

    def tearDown(self):
        self.window.hide()
        self.engine.deleteLater()
        self.app.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)
        self.backend.requestClose()
        for _ in range(20):
            self.app.processEvents()
        self.backend.deleteLater()
        self.app.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)

    def visual(self, name):
        def descendants(item):
            yield item
            for child in item.childItems():
                yield from descendants(child)
        return next((item for item in descendants(self.window.contentItem()) if item.objectName() == name), None)

    def test_panels_load_once_and_preserve_manual_targets_and_scroll(self):
        for index in range(8):
            loader = self.pane.findChild(QtCore.QObject, "instrumentLoader" + str(index))
            self.assertEqual(loader.property("active"), index == 0)
        target = self.visual("manualGateTarget_g1")
        target.setProperty("text", "0.123456")
        for _ in range(5):
            self.backend._instruments._read({"g1": {"set_voltage": .2, "current": 2e-9}}, "readback")
            self.app.processEvents()
        self.assertIs(self.visual("manualGateTarget_g1"), target)
        self.assertEqual(target.property("text"), "0.123456")
        self.pane.setProperty("section", 2)
        self.app.processEvents()
        scroller = self.pane.findChild(QtCore.QObject, "instrumentScroller2")
        flickable = scroller.property("contentItem")
        flickable.setProperty("contentY", 70.)
        old_y = flickable.property("contentY")
        for index in (3, 4, 5, 6, 7, 1, 2):
            self.pane.setProperty("section", index)
            self.app.processEvents()
        self.assertIs(self.pane.findChild(QtCore.QObject, "instrumentScroller2"), scroller)
        self.assertEqual(flickable.property("contentY"), old_y)
        self.assertEqual(self.warnings, [])

    def test_magnet_telemetry_preserves_edit_identity_focus_and_draft(self):
        self.pane.setProperty("section", 2)
        self.app.processEvents()
        target = self.visual("field_target_field")
        self.assertIsNotNone(target)
        target.forceActiveFocus()
        target.setProperty("text", "0.123456")
        for i in range(5):
            self.backend._magnet._snapshot("1000", SimpleNamespace(field_t=i * .01))
            self.backend._magnet._report("Synthetic thermal status")
            self.app.processEvents()
        self.assertIs(self.visual("field_target_field"), target)
        self.assertTrue(target.property("activeFocus"))
        self.assertEqual(target.property("text"), "0.123456")
        self.assertEqual(self.backend._magnet._values["target_field"], 0.)
        self.assertEqual(self.warnings, [])

    def type_letters(self, text):
        QtTest.QTest.keyClick(self.window, QtCore.Qt.Key.Key_A, QtCore.Qt.KeyboardModifier.ControlModifier)
        for letter in text:
            QtTest.QTest.keyClick(self.window, QtCore.Qt.Key(ord(letter.upper())))
        self.app.processEvents()

    def test_sample_tab_commits_keep_the_next_editor_focused_and_typing(self):
        self.backend._instruments.setValue("user", "before")
        self.backend._instruments.setValue("device_id", "beforedevice")
        self.pane.setProperty("section", 5)
        self.app.processEvents()
        editors = {key: self.visual("field_" + key) for key in ("user", "device_id", "base")}
        editors["user"].forceActiveFocus()
        self.type_letters("operator")
        QtTest.QTest.keyClick(self.window, QtCore.Qt.Key.Key_Tab)
        self.app.processEvents()
        self.assertEqual(self.backend._instruments.save_root.user, "operator")
        self.assertIs(self.window.activeFocusItem(), editors["device_id"])
        self.type_letters("sample")
        QtTest.QTest.keyClick(self.window, QtCore.Qt.Key.Key_Tab)
        self.app.processEvents()
        self.assertEqual(self.backend._instruments.save_root.device_id, "sample")
        self.assertEqual(self.backend._instruments.settings.value("path/device_id"), "sample")
        self.assertIs(self.window.activeFocusItem(), editors["base"])
        for key, editor in editors.items():
            self.assertIs(self.visual("field_" + key), editor)
        self.assertEqual(self.warnings, [])

    def test_sample_descriptor_refresh_keeps_drafts_and_updates_folder_and_lock(self):
        self.pane.setProperty("section", 5)
        self.app.processEvents()
        editors = {key: self.visual("field_" + key) for key in ("user", "device_id", "base")}
        editors["device_id"].forceActiveFocus()
        self.type_letters("draftsample")
        self.backend._instruments.setValue("user", "updatedoperator")
        folder = str(Path(os.environ["LOCALAPPDATA"]) / "selected")
        self.backend._instruments.setValue("base", folder)
        self.app.processEvents()
        for key, editor in editors.items():
            self.assertIs(self.visual("field_" + key), editor)
        self.assertIs(self.window.activeFocusItem(), editors["device_id"])
        self.assertEqual(editors["device_id"].property("text"), "draftsample")
        self.assertEqual(editors["user"].property("text"), "updatedoperator")
        self.assertEqual(editors["base"].property("text"), folder)
        self.assertNotEqual(self.backend._instruments.save_root.device_id, "draftsample")

        requested = QtTest.QSignalSpy(self.backend.chooseFolderRequested)
        editors["base"].forceActiveFocus()
        QtTest.QTest.keyClick(self.window, QtCore.Qt.Key.Key_B, QtCore.Qt.KeyboardModifier.AltModifier)
        self.app.processEvents()
        self.assertEqual(requested.count(), 1)
        self.backend._instruments._scan_busy = True
        try:
            self.backend._instruments.changed.emit()
            self.app.processEvents()
            for editor in editors.values():
                self.assertTrue(editor.property("readOnly"))
            QtTest.QTest.keyClick(self.window, QtCore.Qt.Key.Key_B, QtCore.Qt.KeyboardModifier.AltModifier)
            self.app.processEvents()
            self.assertEqual(requested.count(), 1)
        finally:
            self.backend._instruments._scan_busy = False
            self.backend._instruments.changed.emit()
        self.assertEqual(self.warnings, [])


if __name__ == "__main__":
    unittest.main()
