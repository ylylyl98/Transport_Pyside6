"""Widgets update notices are nonmodal and do not duplicate release alerts."""
import os
import unittest
from unittest.mock import patch, Mock
from types import SimpleNamespace

if os.environ.get('TRANSPORT_OFFLINE_VERIFICATION') != '1':
    raise unittest.SkipTest('Run with verify_offline.py')

from PySide6 import QtCore, QtGui, QtWidgets
from app.ui.main_window import MainWindow


class FakeUpdates(QtCore.QObject):
    changed = QtCore.Signal()
    currentVersion = '1.0.0'
    latestVersion = ''
    available = False
    checking = False
    autoCheck = True
    message = 'Ready'

    def checkNow(self):
        self.checked = True

    def setAutoCheck(self, value):
        self.autoCheck = value

    def openRelease(self):
        self.opened = True

    def ignoreLatest(self):
        self.available = False


class UpdateShell(QtWidgets.QMainWindow):
    _setup_updates = MainWindow._setup_updates
    _update_release_status = MainWindow._update_release_status
    _check_updates = MainWindow._check_updates
    _dismiss_update_notice = MainWindow._dismiss_update_notice

    def __init__(self):
        super().__init__()
        self.tools_menu = self.menuBar().addMenu('Tools')
        self._setup_updates()


class WidgetUpdateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.application = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def setUp(self):
        self.factory = patch('app.ui.main_window.UpdateService', FakeUpdates)
        self.factory.start()
        self.window = UpdateShell()

    def tearDown(self):
        self.window.close()
        self.window.deleteLater()
        self.factory.stop()

    def test_update_menu_triggers_manual_check_and_persists_auto_preference(self):
        service = self.window.update_service
        self.assertTrue(self.window.update_notice.isHidden())
        for message in ('Source checkout: use Git', 'Offline verification disables updates', 'Checking...', 'Up to date'):
            service.message = message
            service.changed.emit()
            self.assertTrue(self.window.update_notice.isHidden())
        self.window.check_updates_action.trigger()
        self.assertTrue(self.window.update_service.checked)
        self.assertFalse(self.window.update_notice.isHidden())
        self.window.auto_updates_action.setChecked(False)
        self.assertFalse(self.window.update_service.autoCheck)
        self.window.update_service.checking = True
        self.window.update_service.changed.emit()
        self.assertFalse(self.window.check_updates_action.isEnabled())
        self.assertFalse(self.window.update_notice.isHidden())
        service.checking = False
        for message in ('Up to date', 'Could not contact GitHub', 'Source checkout: use Git'):
            service.message = message
            service.changed.emit()
            self.assertEqual(self.window.update_notice_label.text(), message)
            self.assertFalse(self.window.update_notice.isHidden())
        self.window.update_dismiss_button.click()
        service.changed.emit()
        self.assertTrue(self.window.update_notice.isHidden())

    def test_release_notice_dismisses_one_version_and_new_release_can_reappear(self):
        service = self.window.update_service
        service.available, service.latestVersion = True, '1.1.0'
        service.changed.emit()
        self.assertFalse(self.window.update_notice.isHidden())
        self.assertFalse(self.window.update_download_button.isHidden())
        self.assertIn('1.1.0', self.window.update_notice_label.text())
        self.assertEqual(self.window.findChildren(QtWidgets.QDialog), [])
        self.assertFalse(getattr(service, 'opened', False))
        self.window.update_download_button.click()
        self.assertTrue(service.opened)
        self.window.update_dismiss_button.click()
        self.assertTrue(self.window.update_notice.isHidden())
        service.changed.emit()
        self.assertTrue(self.window.update_notice.isHidden())
        service.latestVersion = '1.2.0'
        service.changed.emit()
        self.assertFalse(self.window.update_notice.isHidden())
        self.assertIn('1.2.0', self.window.update_notice_label.text())
        self.window.update_dismiss_button.click()
        self.window.check_updates_action.trigger()
        self.assertFalse(self.window.update_notice.isHidden())

    def test_rejected_instrument_shutdown_keeps_update_service_alive(self):
        service = Mock()
        shell = SimpleNamespace(lockin_panel=SimpleNamespace(prepare_shutdown=lambda: False),
                                update_service=service)
        event = QtGui.QCloseEvent()
        MainWindow.closeEvent(shell, event)
        self.assertFalse(event.isAccepted())
        service.close.assert_not_called()
