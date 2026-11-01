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
    installRequested = QtCore.Signal()
    dialogVisible = False
    downloadState = 'idle'
    downloadMessage = ''
    downloadProgress = 0.0
    downloadedBytes = 0.0
    totalBytes = 0.0
    canDownload = True
    canInstall = False
    releaseUrl = ''
    currentVersion = '1.0.0'
    latestVersion = ''
    available = False
    checking = False
    autoCheck = True
    message = 'Ready'

    def checkNow(self):
        self.checked = True
        self.openDialog()

    def setInstallGuard(self, guard):
        self.guard = guard

    def openDialog(self):
        self.dialogVisible = True
        self.changed.emit()

    def hideDialog(self):
        self.dialogVisible = False
        self.changed.emit()

    def downloadUpdate(self):
        self.downloadState = 'downloading'
        self.changed.emit()

    def cancelDownload(self):
        self.downloadState = 'idle'
        self.changed.emit()

    def installUpdate(self):
        self.installRequested.emit()

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
        self.assertTrue(service.dialogVisible)
        self.assertIsNotNone(self.window._update_dialog)
        self.assertFalse(getattr(service, "opened", False))
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

    def test_dialog_buttons_track_download_and_guard_state(self):
        service = self.window.update_service
        service.available, service.latestVersion = True, '1.1.0'
        service.openDialog()
        dialog = self.window._update_dialog
        self.assertIsNotNone(dialog)
        dialog.download.click()
        self.assertEqual(service.downloadState, 'downloading')
        service.downloadProgress = .5
        service.downloadedBytes = 2 * 1048576
        service.totalBytes = 4 * 1048576
        service.changed.emit()
        self.assertEqual(dialog.progress.value(), 500)
        self.assertIn('2.0 / 4.0', dialog.bytes_label.text())
        dialog.cancel.click()
        self.assertEqual(service.downloadState, 'idle')
        service.downloadState, service.canInstall = 'ready', False
        service.changed.emit()
        self.assertFalse(dialog.install.isEnabled())
        dialog.later.click()
        self.assertFalse(service.dialogVisible)

    def test_install_helper_starts_only_after_history_shutdown_and_failure_stays_open(self):
        service = Mock()
        service.finishShutdown.return_value = False
        shell = SimpleNamespace(update_service=service, setEnabled=Mock(), centralWidget=lambda: None,
                                findChildren=lambda cls: [])
        event = QtGui.QCloseEvent()
        MainWindow._finish_update_shutdown(shell, event)
        self.assertFalse(event.isAccepted())
        service.openDialog.assert_called_once()
        service.close.assert_not_called()
        service.finishShutdown.return_value = True
        MainWindow._finish_update_shutdown(shell, event)
        self.assertTrue(event.isAccepted())
        service.close.assert_called_once()

    def test_pending_install_cancelled_if_hardware_refuses_close(self):
        service = Mock(installPending=True)
        shell = SimpleNamespace(lockin_panel=SimpleNamespace(prepare_shutdown=lambda: False),
                                update_service=service)
        event = QtGui.QCloseEvent()
        MainWindow.closeEvent(shell, event)
        self.assertFalse(event.isAccepted())
        service.cancelInstall.assert_called_once()
        service.finishShutdown.assert_not_called()

    def test_rejected_instrument_shutdown_keeps_update_service_alive(self):
        service = Mock()
        shell = SimpleNamespace(lockin_panel=SimpleNamespace(prepare_shutdown=lambda: False),
                                update_service=service)
        event = QtGui.QCloseEvent()
        MainWindow.closeEvent(shell, event)
        self.assertFalse(event.isAccepted())
        service.close.assert_not_called()
