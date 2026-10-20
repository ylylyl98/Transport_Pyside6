import os
import tempfile
import unittest
from pathlib import Path
from PySide6 import QtCore, QtWidgets

if os.environ.get("TRANSPORT_OFFLINE_VERIFICATION") != "1":
    raise unittest.SkipTest("Requires offline wrapper")

from app.ui.workspace_state import save_workspace, restore_workspace, reset_workspace


class Window(QtWidgets.QMainWindow):
    def __init__(self):
        super().__init__()
        self.tabs = QtWidgets.QTabWidget()
        self.tabs.addTab(QtWidgets.QWidget(), "Vds Sweep")
        self.tabs.addTab(QtWidgets.QWidget(), "Gate Scan")
        self.tabs.addTab(QtWidgets.QWidget(), "Curve Compare")
        self.setCentralWidget(self.tabs)
        self.instrument_dock = QtWidgets.QDockWidget("Instruments", self)
        self.instrument_dock.setObjectName("instrumentWorkspace")
        self.instrument_dock.setWidget(QtWidgets.QLabel("Offline"))
        self.addDockWidget(QtCore.Qt.DockWidgetArea.LeftDockWidgetArea, self.instrument_dock)
        self.command_bar = QtWidgets.QToolBar(self)
        self.command_bar.setObjectName("commands")
        self.addToolBar(self.command_bar)

    def _measurement_tabs(self):
        return (self.tabs.widget(0), self.tabs.widget(1))


class WorkspaceStateTests(unittest.TestCase):
    def test_restore_geometry_docks_and_measurement_without_launching_analysis(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = QtCore.QSettings(str(Path(directory) / "window.ini"), QtCore.QSettings.Format.IniFormat)
            first, second = Window(), Window()
            try:
                # Stay within the offscreen platform's 800x800 display. Qt
                # correctly clamps restored windows after a screen change.
                first.resize(600, 500)
                first.show()
                first.instrument_dock.hide()
                first.command_bar.hide()
                first._last_measurement_page = "Gate Scan"
                first.tabs.setCurrentIndex(2)
                save_workspace(first, settings)
                notifications = []
                second.tabs.currentChanged.connect(notifications.append)
                restore_workspace(second, settings)
                second.show()
                QtWidgets.QApplication.instance().processEvents()
                self.assertEqual(second.size(), first.size())
                self.assertEqual(second.tabs.currentIndex(), 1)
                self.assertEqual(notifications, [])
                self.assertTrue(second.instrument_dock.isHidden())
                self.assertTrue(second.command_bar.isHidden())
                reset_workspace(second, settings)
                self.assertFalse(second.instrument_dock.isHidden())
                self.assertFalse(second.command_bar.isHidden())
            finally:
                first.close()
                second.close()

    def test_invalid_saved_page_and_geometry_leave_default_available(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = QtCore.QSettings(str(Path(directory) / "window.ini"), QtCore.QSettings.Format.IniFormat)
            settings.setValue("workspace/window/measurement_page", "Unknown old page")
            settings.setValue("workspace/window/geometry", "broken")
            window = Window()
            try:
                restore_workspace(window, settings)
                self.assertEqual(window.tabs.currentIndex(), 0)
            finally:
                window.close()
