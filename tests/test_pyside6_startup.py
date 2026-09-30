import unittest
import os
if os.environ.get("TRANSPORT_OFFLINE_VERIFICATION") != "1":
    raise unittest.SkipTest("Run through verify_offline.py to isolate hardware and user settings")
from unittest.mock import patch
from PySide6 import QtCore, QtTest, QtWidgets
from app.ui.main_window import MainWindow
from app.app_identity import configure_qapp


class PySide6StartupTests(unittest.TestCase):
    def test_window_draws_plots_and_closes_without_connections(self):
        app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
        configure_qapp(app)
        with patch("app.ui.dock.ConnDock._start_scan"):
            window = MainWindow()
            try:
                window.show()
                app.processEvents()
                self.assertTrue(window.isVisible())
                self.assertIsInstance(window, QtWidgets.QMainWindow)
                plot = window.tab_cosweep.plot
                self.assertIsInstance(plot.canvas, QtWidgets.QWidget)
                plot.canvas.draw()
                changes = QtTest.QSignalSpy(plot.plot_mode_changed)
                plot._emit_plot_mode_changed("4-Channel Compare")
                self.assertEqual(changes.count(), 1)
                self.assertEqual(plot.current_plot_mode(), "4-Channel Compare")
                self.assertTrue(all(session is None for session in window.device_manager.sessions.values()))
                window.close()
                app.processEvents()
                self.assertFalse(window.isVisible())
            finally:
                # Offline cleanup: production close requires APS100 persistent-mode
                # acknowledgement, which an unconnected instrument cannot provide.
                # Exercise real thread teardown without bypassing that safety gate.
                window.magnet1000.shutdown()
                window.lakeshore335.shutdown()
                self.assertTrue(window.magnet2100.shutdown())
                window.device_manager.shutdown()
                self.assertFalse(window.magnet1000._thread.isRunning())
                self.assertFalse(window.lakeshore335._thread.isRunning())
                window.hide()
                window.deleteLater()
                app.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)
                app.processEvents()
