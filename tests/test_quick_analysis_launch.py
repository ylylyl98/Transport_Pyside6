"""Analysis launch state, readiness and user feedback without instrument access."""
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

if os.environ.get('TRANSPORT_OFFLINE_VERIFICATION') != '1':
    raise unittest.SkipTest('Run through verify_offline.py')

from PySide6 import QtCore, QtQml, QtTest, QtWidgets
from PySide6.QtQuickControls2 import QQuickStyle
from app.quick.history_launcher import HistoryLauncher


class AnalysisLaunchTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
        QQuickStyle.setStyle('Fusion')

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.launcher = HistoryLauncher(lambda: self.temp.name, offline=True)
        self.addCleanup(self.launcher.shutdown)

    @staticmethod
    def message(launcher, line):
        with patch.object(launcher.process, 'readAllStandardOutput', return_value=QtCore.QByteArray(line)):
            launcher._read_stdout()

    def test_open_stays_busy_until_ready_and_suppresses_duplicate_launch(self):
        with patch.object(self.launcher.process, 'start') as start:
            self.launcher.open()
            self.assertTrue(self.launcher.opening)
            self.assertTrue(self.launcher.busy)
            self.launcher._started()
            self.assertTrue(self.launcher.busy, 'QProcess started is earlier than viewer ready')
            self.launcher.open()
            start.assert_called_once()
            self.message(self.launcher, b'{"event":"ready","hardware_modules":[]}\n')
            self.assertFalse(self.launcher.opening)
            self.assertFalse(self.launcher.busy)
            self.assertFalse(self.launcher.launchFailed)
            self.assertIn('separate window', self.launcher.message)

    def test_slow_open_does_not_kill_and_failed_launch_can_retry(self):
        with patch.object(self.launcher.process, 'start') as start, \
             patch.object(self.launcher.process, 'kill') as kill:
            self.launcher.open()
            self.launcher._opening_slow()
            self.assertTrue(self.launcher.opening)
            self.assertIn('still opening', self.launcher.message)
            kill.assert_not_called()
            self.launcher._error()
            self.assertFalse(self.launcher.opening)
            self.assertTrue(self.launcher.launchFailed)
            self.assertFalse(self.launcher.busy)
            self.launcher.open()
            self.assertEqual(start.call_count, 2)
            self.assertTrue(self.launcher.opening)
            self.assertFalse(self.launcher.launchFailed)

    def test_isolation_failure_remains_visible_after_viewer_exit(self):
        with patch.object(self.launcher.process, 'start'), patch.object(self.launcher, '_send') as send:
            self.launcher.open()
            self.message(self.launcher, b'{"event":"ready","hardware_modules":["instruments.example"]}\n')
            self.assertFalse(self.launcher.opening)
            self.assertTrue(self.launcher.launchFailed)
            self.assertIn('isolation', self.launcher.message.lower())
            send.assert_called_with('close')
            self.launcher._finished(0)
            self.assertTrue(self.launcher.launchFailed)
            self.assertIn('isolation', self.launcher.message.lower())

    def test_exit_before_ready_reports_failure(self):
        with patch.object(self.launcher.process, 'start'):
            self.launcher.open()
            self.launcher._finished(0)
            self.assertFalse(self.launcher.opening)
            self.assertTrue(self.launcher.launchFailed)
            self.assertIn('before it was ready', self.launcher.message)

    def test_main_window_displays_analysis_opening_and_retry_without_hiding_stop(self):
        from app.quick.application import ApplicationBackend
        from app.quick.plot import register_types
        backend = ApplicationBackend(offline=True)
        register_types()
        engine = QtQml.QQmlApplicationEngine()
        warnings = []
        engine.warnings.connect(lambda errors: warnings.extend(str(e) for e in errors))
        engine.rootContext().setContextProperty('appBackend', backend)
        engine.loadData(b'import QtQuick\nMain { width: 800; height: 600 }', QtCore.QUrl.fromLocalFile(str(
            Path(__file__).resolve().parents[1] / 'app/quick/qml/AnalysisLaunchHarness.qml')))
        self.assertTrue(engine.rootObjects(), warnings)
        window = engine.rootObjects()[0]
        try:
            window.requestActivate()
            QtTest.QTest.qWait(50)
            button = window.findChild(QtCore.QObject, 'openAnalysis')
            status = window.findChild(QtCore.QObject, 'analysisLaunchStatus')
            stop = window.findChild(QtCore.QObject, 'stopAll')
            self.assertEqual(button.property('text'), 'Analysis')
            self.assertFalse(status.property('visible'))
            with patch.object(backend.history.process, 'start'):
                backend.history.open()
                self.app.processEvents()
                self.assertEqual(button.property('text'), 'Opening…')
                self.assertFalse(button.property('enabled'))
                self.assertTrue(status.property('visible'))
                self.assertIn('Opening Analysis', status.property('text'))
                top = stop.mapToScene(QtCore.QPointF(0, 0))
                self.assertGreaterEqual(top.x(), 0)
                self.assertLessEqual(top.x() + stop.width(), window.width())
                backend.history._error()
                self.app.processEvents()
                self.assertEqual(button.property('text'), 'Retry Analysis')
                self.assertTrue(button.property('enabled'))
                self.assertTrue(status.property('visible'))
                self.assertIn('could not start', status.property('text'))
            self.assertEqual(warnings, [])
        finally:
            window.hide()
            backend.requestClose()
            for _ in range(500):
                if backend.closeAccepted:
                    break
                QtTest.QTest.qWait(10)
            self.assertTrue(backend.closeAccepted)
            engine.deleteLater()
            self.app.sendPostedEvents(None, QtCore.QEvent.DeferredDelete)
            backend.deleteLater()
            self.app.sendPostedEvents(None, QtCore.QEvent.DeferredDelete)
