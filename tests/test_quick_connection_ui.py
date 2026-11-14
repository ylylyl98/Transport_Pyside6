"""Real QML connection buttons bound to services and fake hardware."""
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

if os.environ.get("TRANSPORT_OFFLINE_VERIFICATION") != "1":
    raise unittest.SkipTest("Run through verify_offline.py")

from PySide6 import QtCore, QtQml, QtWidgets
from PySide6.QtQuickControls2 import QQuickStyle
from app.quick.instruments import InstrumentService, ADDRESS_KEYS
from app.quick.magnet import MagnetService
from tests.test_quick_magnet import Controller, SDKController
from utils.config import cfg, SetupConfig


class ConnectionUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
        QQuickStyle.setStyle("Fusion")

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.settings = QtCore.QSettings(str(Path(self.temp.name) / "settings.ini"), QtCore.QSettings.Format.IniFormat)
        self.engine = QtQml.QQmlApplicationEngine()
        self.warnings = []
        self.engine.warnings.connect(lambda items: self.warnings.extend(str(i) for i in items))

    def load(self, component, property_name, service):
        self.engine.rootContext().setContextProperty("service", service)
        source = ("import QtQuick\nimport QtQuick.Controls\nApplicationWindow { width: 900; height: 1000; visible: false\n"
                  "property bool darkTheme: false\n"
                  "Component.onCompleted: Theme.dark = darkTheme\n"
                  "onDarkThemeChanged: Theme.dark = darkTheme\n"
                  "readonly property color connectedColor: Theme.progressSuccess\n"
                  "readonly property color pendingColor: Theme.warning\n"
                  "readonly property color errorColor: Theme.danger\n"
                  "readonly property color disconnectedColor: Theme.muted\n"
                  + component + " { width: parent.width; " + property_name + ": service }\n}")
        self.engine.loadData(source.encode(), QtCore.QUrl.fromLocalFile(str(
            Path(__file__).resolve().parents[1] / "app/quick/qml/ConnectionHarness.qml")))
        self.assertTrue(self.engine.rootObjects(), self.warnings)
        self.window = self.engine.rootObjects()[0]
        self.app.processEvents()

    def button(self, name):
        def walk(item):
            yield item
            for child in item.childItems():
                yield from walk(child)
        return next(item for item in walk(self.window.contentItem()) if item.objectName() == name)

    def check(self, name, text, enabled):
        self.app.processEvents()
        button = self.button(name)
        self.assertEqual(button.property("text"), text)
        self.assertEqual(button.property("enabled"), enabled)
        self.assertEqual(button.property("tone"), "neutral")
        if text in ("Connected", "LS335 Connected"):
            expression = QtQml.QQmlExpression(QtQml.QQmlEngine.contextForObject(button), button, "contentItem.color")
            color, undefined = expression.evaluate()
            self.assertFalse(undefined)
            self.assertEqual(color, self.window.property("connectedColor"))

    def check_status(self, name, text, color):
        for dark in (False, True):
            self.window.setProperty("darkTheme", dark)
            self.app.processEvents()
            label = self.button(name)
            self.assertEqual(label.property("text"), text)
            self.assertEqual(label.property("color"), self.window.property(color + "Color"))
        self.window.setProperty("darkTheme", False)

    def test_instrument_connection_buttons_follow_service(self):
        service = InstrumentService(settings=self.settings)
        self.addCleanup(service.manager.shutdown)
        self.addCleanup(service.close)
        for attr in ADDRESS_KEYS:
            setattr(service.connections, attr, "")
        service.connections.lockin = "GPIB0::8::INSTR"
        manager = service.manager
        self.addCleanup(lambda: manager.sessions.update({name: None for name in manager.sessions}))
        self.load("ConnectionInstrumentSection", "instruments", service)
        self.check("instrumentsConnect", "Connect all", True)
        self.check("instrumentsDisconnect", "Disconnect all", False)
        self.check_status("connectionStatus_lockin", "LOCKIN: Disconnected", "disconnected")
        self.check_status("connectionStatus_g4", "G4: Not configured", "disconnected")
        with patch.object(manager, "connect_all", return_value=True):
            service.command("connect", {})
        self.check("instrumentsConnect", "Connecting…", False)
        manager.sessions["lockin"] = object()
        manager.states["lockin"] = "ok"
        manager._connected_addresses["lockin"] = service.connections.lockin
        service._operation(False, "Connected")
        self.check("instrumentsConnect", "Connected", False)
        self.check("instrumentsDisconnect", "Disconnect all", True)
        self.check_status("connectionStatus_lockin", "LOCKIN: Connected", "connected")
        service.connections.lockin = "GPIB0::9::INSTR"
        service.changed.emit()
        self.check_status("connectionStatus_lockin", "LOCKIN: Reconnect required", "pending")
        service.connections.lockin = "GPIB0::8::INSTR"
        service.changed.emit()
        service.set_active_provider(lambda: True)
        service.changed.emit()
        self.check("instrumentsDisconnect", "Disconnect all", False)
        service.set_active_provider(lambda: False)
        service.changed.emit()
        with patch.object(manager, "disconnect_all", return_value=True):
            service.command("disconnect", {})
        self.check("instrumentsDisconnect", "Disconnecting…", False)
        manager.sessions["lockin"] = None
        service._operation(False, "Disconnected")
        self.check("instrumentsConnect", "Connect all", True)
        manager.states["lockin"] = "err"
        service.changed.emit()
        self.window.setWidth(360)
        self.check_status("connectionStatus_lockin", "LOCKIN: Connection error", "error")
        self.assertEqual(self.warnings, [])

    def test_magnet_and_ls335_buttons_follow_service(self):
        for backend in ("1000", "2100"):
            with self.subTest(backend=backend), patch.object(cfg, "setup", SetupConfig(backend, "ls335" if backend == "1000" else "2100")):
                aps, sdk, ls = Controller(), SDKController(), Controller()
                service = MagnetService(magnet1000=aps, magnet2100=sdk, lakeshore=ls, settings=self.settings)
                service._timer.stop()
                service.acknowledgeReview(True)
                self.load("MagnetInstrumentSection", "magnet", service)
                controller = aps if backend == "1000" else sdk
                self.check("magnetConnect", "Connect", True)
                self.check("magnetDisconnect", "Disconnect", False)
                self.check_status("magnetConnectionStatus", "Magnet: Disconnected", "disconnected")
                service.command("connect")
                self.check("magnetConnect", "Connecting…", False)
                self.check_status("magnetConnectionStatus", "Magnet: Connecting", "pending")
                controller.connected.emit("fake")
                self.check("magnetConnect", "Connected", False)
                self.check("magnetDisconnect", "Disconnect", True)
                self.check_status("magnetConnectionStatus", "Magnet: Connected", "connected")
                if backend == "2100":
                    self.check_status("temperatureConnectionStatus", "Temperature (2100 SDK): Connected", "connected")
                service.set_active_provider(lambda: True)
                service.changed.emit()
                self.check("magnetDisconnect", "Disconnect", False)
                service.set_active_provider(lambda: False)
                service.changed.emit()
                service.command("disconnect")
                self.check("magnetDisconnect", "Disconnecting…", False)
                self.check_status("magnetConnectionStatus", "Magnet: Disconnecting", "pending")
                controller.disconnected.emit()
                self.check("magnetConnect", "Connect", True)
                self.check_status("magnetConnectionStatus", "Magnet: Disconnected", "disconnected")
                if backend == "2100":
                    self.check_status("temperatureConnectionStatus", "Temperature (2100 SDK): Disconnected", "disconnected")
                if backend == "1000":
                    self.check("temperatureConnect", "Connect LS335", True)
                    self.check("temperatureDisconnect", "Disconnect LS335", False)
                    service.command("connect_temperature")
                    self.check("temperatureConnect", "Connecting LS335…", False)
                    self.check_status("temperatureConnectionStatus", "LS335: Connecting", "pending")
                    ls.connected.emit("fake")
                    self.check("temperatureConnect", "LS335 Connected", False)
                    self.check("temperatureDisconnect", "Disconnect LS335", True)
                    self.check_status("temperatureConnectionStatus", "LS335: Connected", "connected")
                    service.command("disconnect_temperature")
                    self.check("temperatureDisconnect", "Disconnecting LS335…", False)
                    self.check_status("temperatureConnectionStatus", "LS335: Disconnecting", "pending")
                    ls.disconnected.emit()
                    self.check("temperatureConnect", "Connect LS335", True)
                    self.check_status("temperatureConnectionStatus", "LS335: Disconnected", "disconnected")
                self.assertEqual(self.warnings, [])
                self.window.close()
                service.close()

    def tearDown(self):
        if hasattr(self, "window"):
            self.window.close()
        self.temp.cleanup()
