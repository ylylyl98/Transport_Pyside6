import os
import unittest
from unittest.mock import patch
if os.environ.get("TRANSPORT_OFFLINE_VERIFICATION") != "1":
    raise unittest.SkipTest("Run through verify_offline.py")
from PySide6 import QtCore, QtWidgets
from app.ui.main_window import MainWindow
from app.ui.unavailable_controller import UnavailableController
from utils.config import cfg

class SetupWidgetsTests(unittest.TestCase):
    def test_configured_owners_and_teardown(self):
        app = QtWidgets.QApplication.instance()
        for backend, source in (("1000", "ls335"), ("2100", "2100"), ("none", "external")):
            with self.subTest(backend=backend), patch.object(cfg.setup, 'magnet_backend', backend), patch.object(cfg.setup, 'temperature_source', source), patch('app.ui.dock.ConnDock._start_scan'):
                window = MainWindow()
                try:
                    self.assertEqual(isinstance(window.magnet1000, UnavailableController), backend != '1000')
                    self.assertEqual(isinstance(window.magnet2100, UnavailableController), backend != '2100')
                    self.assertEqual(isinstance(window.lakeshore335, UnavailableController), backend != '1000')
                    self.assertEqual(window.magnet_panel._backend, backend)
                    self.assertEqual(window.tabs.indexOf(window.tab_bfield_transport) < 0, backend == 'none')
                    self.assertEqual(window.tabs.indexOf(window.tab_bfield_gate_scan) < 0, backend != '1000')
                    if backend != "none":
                        self.assertEqual(window.magnet_panel.backend_combo.count(), 1)
                    context = window.experiment_context_snapshot()
                    self.assertEqual(context["temperature_source"], source)
                    if backend == 'none':
                        self.assertFalse(window.tab_bfield_transport.start_run())
                        self.assertFalse(context["magnet1000"]["available"])
                        self.assertFalse(context["temperature"]["available"])
                    window._show_runtime_settings()
                    groups = window._runtime_settings_dialog.findChildren(QtWidgets.QGroupBox)
                    field_group = next(g for g in groups if g.title().startswith("B-field Sweep"))
                    self.assertEqual(field_group.isHidden(), backend != "1000")
                    with patch("PySide6.QtWidgets.QMessageBox.critical"):
                        window.on_emergency_stop()
                    window.close()
                    app.processEvents()
                finally:
                    if backend == '1000':
                        window.magnet1000.shutdown(); window.lakeshore335.shutdown()
                    elif backend == '2100':
                        window.magnet2100.shutdown()
                    window.device_manager.shutdown()
                    window.hide(); window.deleteLater()
                    app.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)
                    app.processEvents()
