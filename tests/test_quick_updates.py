"""Exercise the real in-app update dialog without network or installation."""
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

if os.environ.get('TRANSPORT_OFFLINE_VERIFICATION') != '1':
    raise unittest.SkipTest('Run with verify_offline.py')

from PySide6 import QtCore, QtGui, QtQml, QtQuickControls2, QtTest
from app.update_check import UpdateService
from tests.test_update_check import verified_release


class QuickUpdateDialogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtGui.QGuiApplication.instance() or QtGui.QGuiApplication([])
        QtQuickControls2.QQuickStyle.setStyle('Fusion')

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.settings = QtCore.QSettings(str(Path(self.temp.name) / 'settings.ini'), QtCore.QSettings.IniFormat)
        self.service = UpdateService(settings=self.settings)
        self.service._current = 'v1.0'
        self.service._receive(verified_release(), '')
        self.engine = QtQml.QQmlApplicationEngine()
        self.warnings = []
        self.engine.warnings.connect(lambda errors: self.warnings.extend(str(e) for e in errors))
        self.engine.rootContext().setContextProperty('updatesBackend', self.service)
        self.engine.loadData(b'''import QtQuick
import QtQuick.Controls
ApplicationWindow {
    id: root; width: 800; height: 600; visible: true; font.pointSize: 10
    property bool dark: false
    Binding { target: Theme; property: "dark"; value: root.dark }
    UpdateDialog { updateService: updatesBackend }
}''',
            QtCore.QUrl.fromLocalFile(str(Path(__file__).resolve().parents[1] / 'app/quick/qml/UpdateHarness.qml')))
        self.assertTrue(self.engine.rootObjects(), self.warnings)
        self.window = self.engine.rootObjects()[0]
        self.window.requestActivate()

    def tearDown(self):
        self.service.close()
        self.window.hide()
        self.engine.deleteLater()
        QtCore.QCoreApplication.sendPostedEvents(None, QtCore.QEvent.DeferredDelete)
        self.temp.cleanup()

    def item(self, name):
        found = self.window.findChild(QtCore.QObject, name)
        self.assertIsNotNone(found, name)
        return found

    def click(self, name):
        item = self.item(name)
        point = item.mapToScene(QtCore.QPointF(item.width() / 2, item.height() / 2)).toPoint()
        QtTest.QTest.mouseClick(self.window, QtCore.Qt.LeftButton, pos=point)
        QtTest.QTest.qWait(30)

    def wait(self, predicate):
        deadline = time.monotonic() + 2
        while not predicate() and time.monotonic() < deadline:
            QtTest.QTest.qWait(10)
        self.assertTrue(predicate())

    def test_download_progress_and_install_guard_use_internal_dialog(self):
        finish, entered = threading.Event(), threading.Event()
        installer = Path(self.temp.name) / 'Transport-Setup-windows-x64.exe'
        installer.write_bytes(b'not executable')
        def fake_download(**args):
            args['events'].put(dict(type='progress', received=512, total=1024))
            entered.set()
            if finish.wait(3):
                args['events'].put(dict(type='complete', path=str(installer), version=args['version']))
        installed = []
        self.service.installRequested.connect(lambda: installed.append(True))
        with patch.object(self.service, '_eligible', return_value=True), \
                patch('app.update_check.download_installer', side_effect=fake_download), \
                patch('app.update_check.launch_after_exit') as launch, \
                patch('app.update_check.QtGui.QDesktopServices.openUrl') as browser:
            self.service.openDialog()
            QtTest.QTest.qWait(50)
            self.click('updateDownloadInstaller')
            self.assertTrue(entered.wait(1))
            self.wait(lambda: self.item('updateProgress').property('value') == .5)
            self.assertTrue(self.item('updateCancelDownload').property('visible'))
            finish.set()
            self.wait(lambda: self.service.downloadState == 'ready')
            self.assertFalse(self.item('updateInstall').property('enabled'))
            self.click('updateInstall')
            self.assertFalse(installed)
            self.service.setInstallGuard(lambda: True)
            self.service.refreshInstallState()
            QtTest.QTest.qWait(30)
            self.click('updateInstall')
            self.assertEqual(installed, [True])
            launch.assert_not_called()
            browser.assert_not_called()
            self.service.cancelInstall()
        self.assertEqual(self.warnings, [])

    def test_popup_actions_fit_small_window_with_large_font_and_both_themes(self):
        self.service._download_state = 'ready'
        self.service._download_message = 'Download verified. Exit Transport and open the installer when idle.'
        self.service._installer_path = 'never-run.exe'
        with patch.object(self.service, '_eligible', return_value=True):
            self.service.openDialog()
            for dark in (False, True):
                self.window.setProperty('dark', dark)
                self.window.setProperty('font', QtGui.QFont('Segoe UI', 18))
                QtTest.QTest.qWait(50)
                dialog = self.item('updateDialog')
                self.assertFalse(dialog.property('modal'))
                self.assertLessEqual(dialog.property('height'), self.window.height() - 16)
                action = self.item('updateInstall')
                origin = action.mapToScene(QtCore.QPointF())
                self.assertGreaterEqual(origin.x(), 0)
                self.assertGreaterEqual(origin.y(), 0)
                self.assertLessEqual(origin.x() + action.width(), self.window.width())
                self.assertLessEqual(origin.y() + action.height(), self.window.height())
        self.assertEqual(self.warnings, [])
