import os
import unittest
if os.environ.get("TRANSPORT_OFFLINE_VERIFICATION") != "1":
    raise unittest.SkipTest("Requires verify_offline.py hardware isolation")
from types import SimpleNamespace
from unittest.mock import patch
from PySide6 import QtCore, QtTest, QtWidgets
from controllers.magnet_controller import MagnetController
from app.engine.bfield_transport_controller import BFieldTransportController


class OfflineShutdownTests(unittest.TestCase):
    def setUp(self):
        self.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
        self.magnet = MagnetController()
        self.controller = BFieldTransportController(self.magnet, SimpleNamespace(), SimpleNamespace())
        self.controller._log = lambda _message: None

    def tearDown(self):
        self.magnet.shutdown()
        self.app.processEvents()

    def test_never_connected_magnet_closes_without_persistent_request(self):
        requests = QtTest.QSignalSpy(self.magnet._persistent_requested)
        self.assertTrue(self.controller.prepare_shutdown())
        self.assertEqual(requests.count(), 0)
        self.assertEqual(self.magnet.exclusive_owner, "")

    def test_queued_connection_does_not_bypass_shutdown_protection(self):
        # Hold the connect request at the queue boundary; no adapter is opened.
        with patch.object(self.magnet, "_connect_requested"):
            self.magnet.connect_instrument(use_mock=True)
        self.assertFalse(self.magnet.is_connected)
        self.assertFalse(self.controller.prepare_shutdown())

    def test_connection_loss_does_not_reset_shutdown_protection(self):
        with patch.object(self.magnet, "_connect_requested"):
            self.magnet.connect_instrument(use_mock=True)
        self.magnet.disconnected.emit()
        self.assertFalse(self.controller.prepare_shutdown())

    def test_active_work_never_uses_offline_shortcut(self):
        self.controller._active = True
        with patch.object(self.controller, "_cleanup"):
            self.assertFalse(self.controller.prepare_shutdown())

    def test_offline_close_check_does_not_cache_clearance_for_later_connection(self):
        self.assertTrue(self.controller.prepare_shutdown())
        with patch.object(self.magnet, "_connect_requested"):
            self.magnet.connect_instrument(use_mock=True)
        self.assertFalse(self.controller.prepare_shutdown())
