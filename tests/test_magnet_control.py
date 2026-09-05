import unittest
from unittest.mock import patch
from types import SimpleNamespace

from PyQt6 import QtCore
from PyQt6 import QtWidgets
from PyQt6.QtWidgets import QApplication

from app.ui.magnet_panel import MagnetPanel
from controllers.attodry2100_controller import AttoDRY2100Controller
from controllers.magnet_controller import MagnetController, _MagnetWorker
from utils.config import AttoDRY2100Config
from utils.config import cfg


class _Fake1000(QtCore.QObject):
    connected = QtCore.pyqtSignal(object)
    disconnected = QtCore.pyqtSignal()
    snapshot_updated = QtCore.pyqtSignal(object)
    transition_progress = QtCore.pyqtSignal(str, float)
    operation_finished = QtCore.pyqtSignal(str)
    error = QtCore.pyqtSignal(str)
    fault = QtCore.pyqtSignal(str)

    def __init__(self):
        super().__init__()
        self.calls = []

    def connect_instrument(self, resource, use_mock=False):
        self.calls.append(("connect", resource, use_mock))

    def disconnect_instrument(self): self.calls.append(("disconnect",))
    def refresh_snapshot(self): self.calls.append(("refresh",))
    def pause(self): self.calls.append(("pause",))
    def enter_driven_mode(self): self.calls.append(("driven",))
    def enter_persistent_mode(self, zero_leads=True): self.calls.append(("persistent", zero_leads))
    def safe_move_to_field(self, target, **kwargs): self.calls.append(("move", target, kwargs))


class _Fake2100(QtCore.QObject):
    connected = QtCore.pyqtSignal(object)
    disconnected = QtCore.pyqtSignal()
    snapshot_updated = QtCore.pyqtSignal(object)
    temperature_updated = QtCore.pyqtSignal(object)
    operation_finished = QtCore.pyqtSignal(str, bool, object)
    error = QtCore.pyqtSignal(str)
    fault = QtCore.pyqtSignal(str)

    def __init__(self):
        super().__init__()
        self.calls = []

    def connect_async(self): self.calls.append(("connect",))
    def disconnect_async(self): self.calls.append(("disconnect",))
    def read_snapshot_async(self): self.calls.append(("refresh",))
    def read_temperature_snapshot_async(self): self.calls.append(("temperature",))
    def set_polling_enabled(self, enabled): self.calls.append(("polling", enabled))
    def request_stop(self): self.calls.append(("stop",))
    def set_h_setpoint_async(self, target): self.calls.append(("setpoint", target))
    def start_field_control_async(self): self.calls.append(("start",))
    def configure_sample_temperature_async(self, target, rate): self.calls.append(("temp_set", target, rate))
    def stop_sample_temperature_control_async(self): self.calls.append(("temp_stop",))


class MagnetPanelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.a = _Fake1000()
        self.b = _Fake2100()
        self.panel = MagnetPanel(self.a, self.b)
        self.panel._on_connected("1000", object())
        self.panel.review.setChecked(True)

    def tearDown(self):
        self.panel.deleteLater()
        self.app.processEvents()

    def test_production_panel_has_no_mock_or_resource_edit_widgets(self):
        self.assertFalse(hasattr(self.panel, "mock_check"))
        self.assertFalse(hasattr(self.panel, "resource_edit"))

    def test_aps_final_mode_defaults_to_persistent(self):
        self.assertEqual(self.panel.mode_combo.currentData(), "persistent")

    def test_connect_uses_configured_resource_and_explicit_real_mode(self):
        panel = MagnetPanel(self.a, self.b)
        panel.review.setChecked(True)
        panel._connect_selected()
        self.assertEqual(self.a.calls[-1], ("connect", cfg.magnet.visa_resource, False))
        panel.deleteLater()


    def test_real_commands_require_review_and_revocation(self):
        self.panel._set_reviewed(True)
        self.panel._reviewed = False
        self.panel.mode_combo.setCurrentIndex(0)
        self.panel._move_to_target()
        self.assertNotIn(("move",), self.a.calls)

    @patch("app.ui.magnet_panel.QtWidgets.QMessageBox.question")
    def test_persistent_modal_cancel_and_accept(self, question):
        self.panel.mode_combo.setCurrentIndex(1)
        self.panel._aps_heater_state = True
        question.return_value = QtWidgets.QMessageBox.StandardButton.No
        self.panel._move_to_target()
        self.assertNotIn(("move",), self.a.calls)
        question.return_value = QtWidgets.QMessageBox.StandardButton.Yes
        self.panel._move_to_target()
        move = [call for call in self.a.calls if call[0] == "move"][-1]
        self.assertTrue(move[2]["zero_leads"])
        self.assertTrue(move[2]["persistent_field_confirmed"])

    def test_driven_safe_move_stop_and_telemetry_are_available(self):
        self.panel.mode_combo.setCurrentIndex(0)
        self.panel._aps_heater_state = True
        self.panel._move_to_target()
        move = [call for call in self.a.calls if call[0] == "move"][-1]
        self.assertEqual(move[2]["final_mode"], "driven")
        self.assertTrue(self.panel.stop_button.isEnabled())
        self.panel._on_snapshot("1000", SimpleNamespace(
            field_t=1.0, output_field_t=0.9, output_current_a=4.0,
            sweep_state="pause", heater_on=True, magnet_voltage_v=0.2,
            output_voltage_v=0.1,
            status=SimpleNamespace(standby=False, quench=False),
            temperature_k=None,
        ))
        self.assertIn("0.900000", self.panel.output_field_label.text())

    def test_activity_log_accumulates_and_progress_uses_units(self):
        self.panel.activity_log.clear()
        self.panel._on_progress("ramping field", 0.25)
        self.panel._on_progress("heater cooling", 4.0)
        text = self.panel.activity_log.toPlainText()
        self.assertIn("ramping field: 0.250 T", text)
        self.assertIn("heater cooling: 4.000 s", text)
        self.assertEqual(self.panel.progress.minimum(), 0)
        self.assertEqual(self.panel.progress.maximum(), 0)
        self.assertIn("s", self.panel.progress.format())

    def test_changed_telemetry_and_progress_are_throttled_below_one_hz(self):
        self.panel.activity_log.clear()
        self.panel._busy_1000 = True
        snapshot = SimpleNamespace(
            field_t=1.0, output_field_t=0.9, output_current_a=4.0,
            sweep_state="ramp", heater_on=True, magnet_voltage_v=0.2,
            output_voltage_v=0.1,
            status=SimpleNamespace(standby=False, quench=False),
            temperature_k=None,
        )
        clock = [10.0]
        with patch("app.ui.magnet_panel.time.monotonic", side_effect=lambda: clock[0]):
            self.panel._on_snapshot("1000", snapshot)
            snapshot.field_t = 1.1
            self.panel._on_snapshot("1000", snapshot)
            self.panel._on_progress("ramping field", 0.1)
            self.panel._on_progress("ramping field", 0.2)
        lines = self.panel.activity_log.toPlainText().splitlines()
        self.assertEqual(len([line for line in lines if "telemetry:" in line]), 1)
        self.assertEqual(len([line for line in lines if "ramping field:" in line]), 1)
        clock[0] = 40.1
        self.panel._on_snapshot("1000", snapshot)
        self.assertEqual(len([line for line in self.panel.activity_log.toPlainText().splitlines() if "telemetry:" in line]), 2)

    def test_heater_countdown_uses_live_updates_and_coarse_deduplicated_milestones(self):
        self.panel.activity_log.clear()
        self.panel._reset_activity_throttles()
        for remaining in (120, 119, 91, 90, 89, 61, 60, 59, 31, 30, 29, 11, 10, 9, 1, 0, 0):
            self.panel._on_progress("heater cooling", remaining)
        lines = self.panel.activity_log.toPlainText().splitlines()
        self.assertEqual(len([line for line in lines if "heater cooling:" in line]), 7)
        self.assertIn("heater cooling: 0 s milestone", lines[-1])
        self.assertEqual(self.panel.progress.format(), "heater cooling: 0.000 s")

    def test_heater_countdown_starts_a_new_log_lifecycle_after_a_new_transition(self):
        self.panel.activity_log.clear()
        self.panel._reset_activity_throttles()
        for remaining in (10, 9, 0):
            self.panel._on_progress("heater cooling", remaining)
        self.panel._on_progress("zeroing leads", 0.5)
        for remaining in (10, 9, 0):
            self.panel._on_progress("heater cooling", remaining)
        lines = self.panel.activity_log.toPlainText().splitlines()
        self.assertEqual(len([line for line in lines if "heater cooling: 10.000 s (started)" in line]), 2)
        self.assertEqual(len([line for line in lines if "heater cooling: 0 s milestone" in line]), 2)

    def test_aps_telemetry_logs_state_transitions_but_not_numeric_changes(self):
        self.panel.activity_log.clear()
        self.panel._busy_1000 = True
        clock = [10.0]
        def snap(field=1.0, sweep="standby", heater=False, standby=True, quench=False, power=False):
            return SimpleNamespace(field_t=field, output_field_t=0.1, output_current_a=1.0,
                sweep_state=sweep, heater_on=heater, magnet_voltage_v=0.0,
                output_voltage_v=0.0, temperature_k=None,
                status=SimpleNamespace(standby=standby, quench=quench,
                                       power_module_failure=power))
        with patch("app.ui.magnet_panel.time.monotonic", side_effect=lambda: clock[0]):
            self.panel._on_snapshot("1000", snap())
            before = len([x for x in self.panel.activity_log.toPlainText().splitlines() if "telemetry:" in x])
            self.panel._on_snapshot("1000", snap(field=2.0))
            self.assertEqual(len([x for x in self.panel.activity_log.toPlainText().splitlines() if "telemetry:" in x]), before)
            for changed in (snap(sweep="ramp"), snap(heater=True), snap(standby=False),
                            snap(quench=True), snap(power=True)):
                self.panel._on_snapshot("1000", changed)
            count = len([x for x in self.panel.activity_log.toPlainText().splitlines() if "telemetry:" in x])
            self.assertEqual(count, before + 5)
            self.panel._on_snapshot("1000", snap(field=3.0, sweep="ramp", heater=True, standby=False, quench=False, power=True))
            clock[0] = 40.0
            self.panel._on_snapshot("1000", snap(field=4.0, sweep="ramp", heater=True, standby=False, quench=True, power=True))
            self.assertEqual(len([x for x in self.panel.activity_log.toPlainText().splitlines() if "telemetry:" in x]), count + 2)

    def test_2100_busy_numeric_changes_keep_one_second_throttle(self):
        self.panel._select_backend(1)
        self.panel._busy_2100 = True
        self.panel.activity_log.clear()
        clock = [10.0]
        def snap(field):
            return SimpleNamespace(field_t=field, output_field_t=0.0, output_current_a=0.0,
                sweep_state="ramp", heater_on=False, magnet_voltage_v=0.0,
                output_voltage_v=0.0, temperature_k=None,
                status=SimpleNamespace(standby=False, quench=False))
        with patch("app.ui.magnet_panel.time.monotonic", side_effect=lambda: clock[0]):
            self.panel._on_snapshot("2100", snap(1.0))
            self.panel._on_snapshot("2100", snap(2.0))
            self.assertEqual(len([x for x in self.panel.activity_log.toPlainText().splitlines() if "2100 telemetry:" in x]), 1)
            clock[0] = 11.0
            self.panel._on_snapshot("2100", snap(3.0))
            self.assertEqual(len([x for x in self.panel.activity_log.toPlainText().splitlines() if "2100 telemetry:" in x]), 2)

    def test_fault_is_always_recorded_in_activity_log(self):
        self.panel.activity_log.clear()
        self.panel._on_fault("Quench reported")
        self.assertIn("[FAULT] Quench reported", self.panel.activity_log.toPlainText())

    def test_busy_guard_blocks_repeated_move_and_stop_remains_available(self):
        self.panel.mode_combo.setCurrentIndex(0)
        self.panel._aps_heater_state = True
        self.panel._move_to_target()
        self.panel._move_to_target()
        self.assertEqual(len([c for c in self.a.calls if c[0] == "move"]), 1)
        self.panel._on_1000_error("simulated operation error")
        self.assertTrue(self.panel.stop_button.isEnabled())

    def test_aps_zero_target_completion_reenables_move_for_next_target(self):
        self.panel.mode_combo.setCurrentIndex(0)
        self.panel._aps_heater_state = True
        self.panel.target_field.setValue(0.0)
        self.panel._move_to_target()

        self.assertTrue(self.panel._busy_1000)
        self.assertFalse(self.panel.move_button.isEnabled())

        self.panel._on_operation("safe_move:driven:+0.000000")

        self.assertFalse(self.panel._busy_1000)
        self.assertTrue(self.panel.move_button.isEnabled())
        self.panel.target_field.setValue(1.0)
        self.panel._move_to_target()
        self.assertEqual(
            [call[1] for call in self.a.calls if call[0] == "move"],
            [0.0, 1.0],
        )

    def test_aps_completion_does_not_override_exclusive_reservation(self):
        self.panel._aps_heater_state = True
        self.panel._aps_exclusive = True
        self.panel._update_buttons()
        self.assertFalse(self.panel.move_button.isEnabled())

        self.panel._on_operation("safe_move:driven:+0.000000")

        self.assertFalse(self.panel._busy_1000)
        self.assertFalse(self.panel.move_button.isEnabled())
        self.panel._on_aps_exclusive_changed(False, "")
        self.assertTrue(self.panel.move_button.isEnabled())

    def test_2100_setpoint_then_start_is_ordered_and_busy(self):
        self.panel.backend_combo.setCurrentIndex(1)
        self.panel._on_connected("2100", object())
        self.panel.review.setChecked(True)
        self.panel._move_to_target()
        self.panel._move_to_target()
        self.assertEqual([c[0] for c in self.b.calls if c[0] in {"setpoint", "start"}], ["setpoint"])
        self.b.operation_finished.emit("setpoint", True, None)
        self.assertEqual([c[0] for c in self.b.calls if c[0] in {"setpoint", "start"}], ["setpoint", "start"])

    def test_unknown_heater_state_blocks_move_until_refresh(self):
        self.panel._move_to_target()
        self.assertFalse(any(c[0] == "move" for c in self.a.calls))
        self.assertIn("heater state is unknown", self.panel.message_label.text())

    @patch("app.ui.magnet_panel.QtWidgets.QMessageBox.question")
    def test_heater_off_driven_cancel_sends_nothing(self, question):
        self.panel._aps_heater_state = False
        self.panel._aps_latest_snapshot = SimpleNamespace(field_t=1.25)
        self.panel.mode_combo.setCurrentIndex(0)
        question.return_value = QtWidgets.QMessageBox.StandardButton.No
        self.panel._move_to_target()
        self.assertFalse(any(c[0] == "move" for c in self.a.calls))

    @patch("app.ui.magnet_panel.QtWidgets.QMessageBox.question")
    def test_heater_off_driven_accept_confirms_field_match(self, question):
        self.panel._aps_heater_state = False
        self.panel._aps_latest_snapshot = SimpleNamespace(field_t=-0.5)
        self.panel.mode_combo.setCurrentIndex(0)
        question.return_value = QtWidgets.QMessageBox.StandardButton.Yes
        self.panel._move_to_target()
        move = [call for call in self.a.calls if call[0] == "move"][-1]
        self.assertTrue(move[2]["persistent_field_confirmed"])
        self.assertTrue(move[2]["zero_leads"])

    def test_heater_on_driven_does_not_need_field_confirmation(self):
        self.panel._aps_heater_state = True
        self.panel.mode_combo.setCurrentIndex(0)
        self.panel._move_to_target()
        move = [call for call in self.a.calls if call[0] == "move"][-1]
        self.assertFalse(move[2]["persistent_field_confirmed"])

    @patch("app.ui.magnet_panel.QtWidgets.QMessageBox.question")
    def test_heater_off_persistent_confirmation_keeps_zero_leads(self, question):
        self.panel._aps_heater_state = False
        self.panel._aps_latest_snapshot = SimpleNamespace(field_t=0.0)
        self.panel.mode_combo.setCurrentIndex(1)
        question.return_value = QtWidgets.QMessageBox.StandardButton.Yes
        self.panel._move_to_target()
        move = [call for call in self.a.calls if call[0] == "move"][-1]
        self.assertTrue(move[2]["zero_leads"])
        self.assertTrue(move[2]["persistent_field_confirmed"])

    def test_aps_connect_pending_prevents_reordered_reconnects(self):
        panel = MagnetPanel(self.a, self.b)
        panel.review.setChecked(True)
        panel._connect_selected()
        panel._connect_selected()
        self.assertEqual([c for c in self.a.calls if c[0] == "connect"],
                         [("connect", cfg.magnet.visa_resource, False)])
        panel._on_1000_error("connection failed")
        panel._connect_selected()
        self.assertEqual(len([c for c in self.a.calls if c[0] == "connect"]), 2)
        panel.deleteLater()


class APS100TransportWorkerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    @staticmethod
    def _adapter(*, sweep_active=False, fail_rate=False):
        class Adapter:
            connected = True

            def __init__(self):
                self.calls = []

            def get_rates(self):
                self.calls.append("get_rates")
                return {0: (40.0, 0.1)}

            def get_limits_t(self):
                self.calls.append("get_limits")
                return (-1.0, 1.0)

            def get_voltage_limit_v(self):
                return 3.0

            def take_remote(self):
                self.calls.append("remote")

            def set_rate_t_per_min(self, rate, **_kwargs):
                self.calls.append(("rate", rate))
                if fail_rate:
                    raise RuntimeError("injected RATE failure")
                return {0: rate}

            def set_limits_t(self, low, high):
                self.calls.append(("limits", low, high))
                return (low, high)

            def restore_rates(self, rates):
                self.calls.append(("restore_rates", rates))

            def get_status(self):
                self.calls.append("status")
                return SimpleNamespace(sweep_active=sweep_active)

            def pause(self):
                self.calls.append("pause")

            def read_snapshot(self):
                return SimpleNamespace(status=SimpleNamespace(
                    quench=False, power_module_failure=False
                ))

        return Adapter()

    def test_transport_configuration_takes_remote_before_rate(self):
        worker = _MagnetWorker()
        worker.adapter = self._adapter()
        results = []
        worker.transport_config_result.connect(results.append)

        worker.configure_transport(0.05, 6.0)

        self.assertTrue(results[-1]["success"])
        self.assertLess(worker.adapter.calls.index("remote"), worker.adapter.calls.index(("rate", 0.05)))

    def test_transport_configuration_rolls_back_after_mutation_failure(self):
        worker = _MagnetWorker()
        worker.adapter = self._adapter(fail_rate=True)
        results = []
        worker.transport_config_result.connect(results.append)

        worker.configure_transport(0.05, 6.0)

        self.assertFalse(results[-1]["success"])
        self.assertIn(("restore_rates", {0: (40.0, 0.1)}), worker.adapter.calls)
        self.assertIn(("limits", -1.0, 1.0), worker.adapter.calls)

    def test_idle_pause_is_an_acknowledged_noop(self):
        worker = _MagnetWorker()
        worker.adapter = self._adapter(sweep_active=False)
        finished = []
        worker.operation_finished.connect(finished.append)

        worker.pause()

        self.assertNotIn("pause", worker.adapter.calls)
        self.assertNotIn("remote", worker.adapter.calls)
        self.assertEqual(finished[-1], "pause")


class ExplicitMockControllerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_explicit_controller_mock_smoke(self):
        controller = MagnetController()
        identities = []
        controller.connected.connect(identities.append)
        controller.connect_instrument(use_mock=True)
        for _ in range(30):
            self.app.processEvents()
            if identities:
                break
            QtCore.QThread.msleep(10)
        self.assertTrue(identities)
        controller.shutdown()


class ImmediateFailureSignalTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_immediate_failure_is_published(self):
        controller = AttoDRY2100Controller(
            config=AttoDRY2100Config(sdk_directory="missing"),
            request_timeout_s=0.2,
            shutdown_wait_s=0.2,
        )
        failures = []
        controller.operation_finished.connect(lambda name, ok, error: failures.append((name, ok)))
        controller.connect_async()
        # Disconnect is rejected immediately while the first request is pending.
        controller.disconnect_async()
        for _ in range(20):
            self.app.processEvents()
        self.assertIn(("disconnect", False), failures)
        self.assertTrue(controller.shutdown(0.5))


class MainWindowMagnetLifecycleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_main_window_owns_one_controller_of_each_type(self):
        from app.ui.main_window import MainWindow
        window = MainWindow()
        self.assertIs(window.magnet_panel.magnet1000, window.magnet1000)
        self.assertIs(window.magnet_panel.magnet2100, window.magnet2100)
        self.assertIn(window.lockin_dock, window.tabifiedDockWidgets(window.magnet_dock))
        self.assertEqual(
            window.dockWidgetArea(window.magnet_dock),
            window.dockWidgetArea(window.lockin_dock),
        )
        view_actions = {action.text() for action in window.view_menu.actions()}
        self.assertIn(window.magnet_dock.windowTitle(), view_actions)
        self.assertIn(window.lockin_dock.windowTitle(), view_actions)
        window.magnet1000.shutdown()
        self.assertTrue(window.magnet2100.shutdown(0.5))
        window.deleteLater()

    def test_close_event_keeps_window_open_when_2100_shutdown_unconfirmed(self):
        from app.ui.main_window import MainWindow
        window = MainWindow()
        real_shutdown_1000 = window.magnet1000.shutdown
        real_shutdown = window.magnet2100.shutdown
        window.magnet1000.shutdown = lambda: None
        window.magnet2100.shutdown = lambda: False

        class Event:
            accepted = False
            ignored = False
            def accept(self): self.accepted = True
            def ignore(self): self.ignored = True

        event = Event()
        with patch("app.ui.main_window.QtWidgets.QMessageBox.critical"):
            window.closeEvent(event)
        self.assertFalse(event.accepted)
        self.assertTrue(event.ignored)
        window.magnet1000.shutdown = real_shutdown_1000
        real_shutdown_1000()
        window.magnet2100.shutdown = real_shutdown
        real_shutdown(0.5)
        window.deleteLater()


if __name__ == "__main__":
    unittest.main()
