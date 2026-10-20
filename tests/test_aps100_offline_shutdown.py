import os
import unittest
if os.environ.get("TRANSPORT_OFFLINE_VERIFICATION") != "1":
    raise unittest.SkipTest("Requires verify_offline.py hardware isolation")
from types import SimpleNamespace
from unittest.mock import patch
from PySide6 import QtCore, QtGui, QtTest, QtWidgets
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

    def test_cancelled_close_requires_a_new_persistent_acknowledgement(self):
        with patch.object(self.magnet, "_connect_requested"):
            self.magnet.connect_instrument(use_mock=True)
        with patch.object(self.magnet, "enter_persistent_mode") as persistent:
            self.assertFalse(self.controller.prepare_shutdown())
            self.controller._on_magnet_operation("enter_persistent_mode")
            self.assertTrue(self.controller.prepare_shutdown())
            self.assertEqual(persistent.call_count, 1)

            self.controller.cancel_shutdown_preparation()
            self.assertFalse(self.controller._shutdown_complete)
            self.assertFalse(self.controller.prepare_shutdown())
            self.assertEqual(persistent.call_count, 2)
            self.controller._on_magnet_operation("enter_persistent_mode")
            self.assertTrue(self.controller.prepare_shutdown())

    def test_cancelling_clearance_does_not_interrupt_pending_safety_work(self):
        with patch.object(self.magnet, "_connect_requested"):
            self.magnet.connect_instrument(use_mock=True)
        with patch.object(self.magnet, "enter_persistent_mode") as persistent:
            self.assertFalse(self.controller.prepare_shutdown())
            owner = self.magnet.exclusive_owner
            self.controller.cancel_shutdown_preparation()
            self.assertTrue(self.controller._shutdown_persistent_waiting)
            self.assertTrue(self.controller._shutdown_exclusive_acquired)
            self.assertEqual(self.magnet.exclusive_owner, owner)
            self.assertFalse(self.controller.prepare_shutdown())
            self.assertEqual(persistent.call_count, 1)
            self.controller._on_magnet_operation("enter_persistent_mode")
            self.assertTrue(self.controller.prepare_shutdown())

    def test_new_run_attempt_discards_previous_close_clearance(self):
        self.controller._shutdown_complete = True
        # A failed preflight still makes the old shutdown acknowledgement
        # obsolete; a later close must inspect the current instrument state.
        with patch.object(self.controller, "_cleanup"):
            self.assertFalse(self.controller.start())
        self.assertFalse(self.controller._shutdown_complete)

    def test_lockin_rejected_close_requires_a_fresh_persistent_acknowledgement(self):
        from app.ui.main_window import MainWindow

        with patch.object(self.magnet, "_connect_requested"):
            self.magnet.connect_instrument(use_mock=True)
        with patch.object(self.magnet, "enter_persistent_mode") as persistent:
            for blocked_panel in ("lockin_panel", "drive_lockin_panel"):
                with self.subTest(blocked_panel=blocked_panel):
                    self.controller.cancel_shutdown_preparation()
                    self.assertFalse(self.controller.prepare_shutdown())
                    self.controller._on_magnet_operation("enter_persistent_mode")
                    self.assertTrue(self.controller.prepare_shutdown())
                    count = persistent.call_count
                    window = SimpleNamespace(
                        bfield_transport_controller=self.controller,
                        lockin_panel=SimpleNamespace(prepare_shutdown=lambda: True),
                        drive_lockin_panel=SimpleNamespace(prepare_shutdown=lambda: True),
                    )
                    setattr(window, blocked_panel, SimpleNamespace(prepare_shutdown=lambda: False))
                    event = QtGui.QCloseEvent()
                    MainWindow.closeEvent(window, event)
                    self.assertFalse(event.isAccepted())
                    self.assertFalse(self.controller._shutdown_complete)
                    # User may have changed APS mode while close was rejected.
                    # A second close must get a new owner acknowledgement.
                    self.assertFalse(self.controller.prepare_shutdown())
                    self.assertEqual(persistent.call_count, count + 1)
                    self.controller._on_magnet_operation("enter_persistent_mode")
                    self.assertTrue(self.controller.prepare_shutdown())

    def test_lockin_rejected_close_preserves_pending_persistent_and_reservation(self):
        from app.ui.main_window import MainWindow

        with patch.object(self.magnet, "_connect_requested"):
            self.magnet.connect_instrument(use_mock=True)
        with patch.object(self.magnet, "enter_persistent_mode") as persistent:
            self.assertFalse(self.controller.prepare_shutdown())
            owner = self.magnet.exclusive_owner
            for blocked_panel in ("lockin_panel", "drive_lockin_panel"):
                with self.subTest(blocked_panel=blocked_panel):
                    window = SimpleNamespace(
                        bfield_transport_controller=self.controller,
                        lockin_panel=SimpleNamespace(prepare_shutdown=lambda: True),
                        drive_lockin_panel=SimpleNamespace(prepare_shutdown=lambda: True),
                    )
                    setattr(window, blocked_panel, SimpleNamespace(prepare_shutdown=lambda: False))
                    event = QtGui.QCloseEvent()
                    MainWindow.closeEvent(window, event)
                    self.assertFalse(event.isAccepted())
                    self.assertTrue(self.controller._shutdown_persistent_waiting)
                    self.assertTrue(self.controller._shutdown_exclusive_acquired)
                    self.assertEqual(self.magnet.exclusive_owner, owner)
                    self.assertFalse(self.controller.prepare_shutdown())
                    self.assertEqual(persistent.call_count, 1)
            self.controller._on_magnet_operation("enter_persistent_mode")
            self.assertTrue(self.controller.prepare_shutdown())
