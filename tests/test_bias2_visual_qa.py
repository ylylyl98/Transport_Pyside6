import os
import unittest
from pathlib import Path
from PySide6 import QtCore, QtQml, QtWidgets, QtTest
from PySide6.QtQuickControls2 import QQuickStyle
from app.quick.application import ApplicationBackend
from app.quick.plot import register_types
from app.device_manager import DeviceManager
from app.models import Connections, SaveRoot
from app.ui.tabs.photocurrent_tab import PhotocurrentTab
from app.ui.tabs.bfield_transport_tab import BFieldTransportTab

OUT = Path('tmp/verification/bias2_ui')

class Bias2VisualQATests(unittest.TestCase):
    def test_offline_visuals(self):
        OUT.mkdir(parents=True, exist_ok=True)
        app = QtWidgets.QApplication.instance()
        QQuickStyle.setStyle('Fusion')
        register_types()
        backend = ApplicationBackend(offline=True)
        backend._instruments.setValue('signal_chain_mode', 'single_drive')
        backend._measurement.setValue('bias2_mode', 'Follow')
        engine = QtQml.QQmlApplicationEngine()
        warnings = []
        engine.warnings.connect(lambda errors: warnings.extend(map(str, errors)))
        engine.rootContext().setContextProperty('appBackend', backend)
        engine.load(QtCore.QUrl.fromLocalFile(str(Path('app/quick/qml/Main.qml').resolve())))
        self.assertTrue(engine.rootObjects(), warnings)
        window = engine.rootObjects()[0]
        window.setProperty('parameterWidth', 520)
        window.resize(1200, 850)
        scroll = window.findChild(QtCore.QObject, 'parameterScroll')
        for dark in (False, True):
            backend.dark = dark
            QtTest.QTest.qWait(500)
            self.assertTrue(window.grabWindow().save(str(OUT / ('quick_dark.png' if dark else 'quick_light.png'))))
            content = scroll.property('contentItem')
            content.setProperty('contentY', max(0, content.property('contentHeight') - content.property('height')))
            QtTest.QTest.qWait(300)
            self.assertTrue(window.grabWindow().save(str(OUT / ('quick_dark_bottom.png' if dark else 'quick_light_bottom.png'))))
            content.setProperty('contentY', 0)
        window.setProperty('showInstruments', True)
        pane = window.findChild(QtCore.QObject, 'instrumentPane')
        pane.setProperty('section', 6)
        for dark in (False, True):
            backend.dark = dark
            QtTest.QTest.qWait(400)
            self.assertTrue(window.grabWindow().save(str(OUT / ('quick_signal_dark.png' if dark else 'quick_signal_light.png'))))
        self.assertEqual(warnings, [])
        window.hide()
        backend.requestClose()
        for _ in range(30): app.processEvents()
        engine.deleteLater()
        conn = Connections(); manager = DeviceManager(conn); save = SaveRoot(base=str(OUT.resolve()), user='offline', device_id='preview')
        for cls, name in ((PhotocurrentTab, 'photocurrent'), (BFieldTransportTab, 'bfield')):
            tab = cls(save, conn, manager)
            editor = tab.condition_bias2_editor
            table = tab.tbl_bias_conditions if name == 'photocurrent' else tab.condition_table
            table.selectRow(0)
            editor.mode.setCurrentText('Follow')
            self.assertFalse(editor.voltage.isEnabled())
            self.assertTrue(editor.offset.isEnabled())
            self.assertTrue(editor.ramp.isEnabled())
            editor.resize(520, editor.sizeHint().height())
            editor.show()
            QtTest.QTest.qWait(200)
            self.assertTrue(editor.grab().save(str(OUT / (name + '_follow.png'))))
            editor.mode.setCurrentText('Disabled')
            self.assertFalse(editor.voltage.isEnabled())
            self.assertFalse(editor.offset.isEnabled())
            self.assertFalse(editor.ramp.isEnabled())
            editor.grab().save(str(OUT / (name + '_disabled.png')))
            tab.close()
