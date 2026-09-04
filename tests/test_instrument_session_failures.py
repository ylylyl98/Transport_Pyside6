import unittest
from types import SimpleNamespace

from app.devices.aps100_attodry1000_adapter import APS100AttoDry1000Adapter
from app.devices.lakeshore335_adapter import LakeShore335Adapter
from app.thermal_safety import ThermalSafetyEvaluator
from utils.config import LakeShore335Config
from PyQt6 import QtCore, QtWidgets
from PyQt6.QtCore import QCoreApplication
from app.ui.magnet_panel import MagnetPanel
from controllers.magnet_controller import _MagnetWorker
from unittest.mock import patch
from utils.config import cfg


class _Resource:
    def __init__(self):
        self.closed = False
    def close(self):
        self.closed = True


class _Manager:
    def __init__(self, resource):
        self.resource = resource
        self.closed = False
    def open_resource(self, _name):
        return self.resource
    def close(self):
        self.closed = True


class _PanelAPS(QtCore.QObject):
    connected = QtCore.pyqtSignal(object)
    disconnected = QtCore.pyqtSignal()
    snapshot_updated = QtCore.pyqtSignal(object)
    transition_progress = QtCore.pyqtSignal(str, float)
    operation_finished = QtCore.pyqtSignal(str)
    error = QtCore.pyqtSignal(str)
    fault = QtCore.pyqtSignal(str)

    def __init__(self):
        super().__init__()
        self.refresh_calls = 0

    def refresh_snapshot(self):
        self.refresh_calls += 1


class _Panel2100(QtCore.QObject):
    connected = QtCore.pyqtSignal(object)
    disconnected = QtCore.pyqtSignal()
    snapshot_updated = QtCore.pyqtSignal(object)
    temperature_updated = QtCore.pyqtSignal(object)
    operation_finished = QtCore.pyqtSignal(str, bool, object)
    error = QtCore.pyqtSignal(str)
    fault = QtCore.pyqtSignal(str)


class SessionLifecycleTests(unittest.TestCase):
    def test_adapter_close_does_not_close_shared_resource_manager(self):
        aps_resource, ls_resource = _Resource(), _Resource()
        manager = _Manager(aps_resource)
        manager.open_resource = lambda name: aps_resource if "APS" in name else ls_resource
        adapter = APS100AttoDry1000Adapter(resource_manager=manager, resource_name="APS")
        lake = LakeShore335Adapter(resource_manager=manager, resource_name="LS")
        adapter._resource = aps_resource
        lake._resource = ls_resource
        adapter.close()
        self.assertFalse(ls_resource.closed)
        lake.close()
        self.assertTrue(aps_resource.closed)
        self.assertTrue(ls_resource.closed)
        self.assertFalse(manager.closed)

    def test_lakeshore_failed_connect_closes_partial_resource(self):
        resource = _Resource()
        manager = _Manager(resource)
        adapter = LakeShore335Adapter(resource_manager=manager)
        with self.assertRaises(Exception):
            adapter.connect()
        self.assertTrue(resource.closed)
        self.assertFalse(manager.closed)

    def test_repeated_communication_faults_are_deduplicated(self):
        config = SimpleNamespace(
            enabled=True, verified_channel_mapping=True,
            sample_warning_temperature_k=5, sample_trip_temperature_k=6,
            sample_recovery_temperature_k=4, reservoir_warning_temperature_k=5,
            reservoir_trip_temperature_k=6, reservoir_recovery_temperature_k=4,
            maximum_reading_age_s=3, sample_channel="A", reservoir_channel="B",
        )
        evaluator = ThermalSafetyEvaluator(config, clock=lambda: 1.0)
        bad = SimpleNamespace(monotonic_s=1.0, connected=False,
                              communication_valid=False, diagnostic_error="closed")
        for _ in range(100):
            evaluator.evaluate(bad, now=1.0)
        self.assertEqual(len(evaluator.events["communication_faults"]), 1)
        self.assertEqual(evaluator.snapshot_dict()["communication_fault_count"], 1)
        self.assertEqual(evaluator.snapshot_dict()["diagnostic_error"], "closed")

    def test_terminal_aps_session_detaches_once_and_does_not_restart_polling(self):
        QCoreApplication.instance() or QCoreApplication([])
        class FakeAdapter:
            connected = True
            def connect(self): return "APS100"
            def read_snapshot(self): raise RuntimeError("Invalid session handle")
            def get_rates(self): return {}
            def get_voltage_limit_v(self): return 1.0
            def close(self, **kwargs): self.closed = True
        worker = _MagnetWorker()
        errors, disconnected = [], []
        worker.error.connect(errors.append)
        worker.disconnected.connect(lambda: disconnected.append(True))
        with patch("controllers.magnet_controller.MockAPS100Adapter", return_value=FakeAdapter()):
            worker.connect_instrument("MOCK", True)
        self.assertIsNone(worker.adapter)
        self.assertFalse(worker._timer.isActive())
        self.assertEqual(len(errors), 1)
        self.assertEqual(len(disconnected), 2)  # explicit pre-connect disconnect + terminal loss
        worker.refresh_snapshot()
        self.assertEqual(len(errors), 1)

    def test_aps_poll_interval_is_fast_until_state_is_clearly_idle(self):
        QCoreApplication.instance() or QCoreApplication([])
        worker = _MagnetWorker()
        fast = int(float(cfg.magnet.poll_interval_s) * 1000)
        slow = int(float(cfg.magnet.stationary_poll_interval_s) * 1000)
        worker._update_poll_interval(SimpleNamespace(
            heater_on=False, sweep_state="standby",
            status=SimpleNamespace(standby=True, faulted=False),
        ))
        self.assertEqual(worker._timer.interval(), slow)
        worker._update_poll_interval(SimpleNamespace(
            heater_on=False, sweep_state="unknown",
            status=SimpleNamespace(standby=True, faulted=False),
        ))
        self.assertEqual(worker._timer.interval(), fast)
        worker._update_poll_interval(SimpleNamespace(
            heater_on=False, sweep_state="standby",
            status=SimpleNamespace(standby=True, faulted=True),
        ))
        self.assertEqual(worker._timer.interval(), fast)
        for snapshot in (
            SimpleNamespace(heater_on=True, sweep_state="standby", status=SimpleNamespace(standby=True, faulted=False)),
            SimpleNamespace(heater_on=False, sweep_state="ramp", status=SimpleNamespace(standby=False, faulted=False)),
            SimpleNamespace(heater_on=None, sweep_state=None, status=None),
        ):
            worker._update_poll_interval(snapshot)
            self.assertEqual(worker._timer.interval(), fast)

    def test_disconnect_resets_idle_poll_interval_to_fast(self):
        QCoreApplication.instance() or QCoreApplication([])
        worker = _MagnetWorker()
        slow = int(float(cfg.magnet.stationary_poll_interval_s) * 1000)
        fast = int(float(cfg.magnet.poll_interval_s) * 1000)
        worker._update_poll_interval(SimpleNamespace(
            heater_on=False, sweep_state="standby",
            status=SimpleNamespace(standby=True, faulted=False),
        ))
        self.assertEqual(worker._timer.interval(), slow)
        worker.disconnect_instrument()
        self.assertEqual(worker._timer.interval(), fast)
        self.assertFalse(worker._timer.isActive())

    def test_identical_aps_faults_are_emitted_once_until_clear(self):
        QCoreApplication.instance() or QCoreApplication([])
        class FakeAdapter:
            connected = True
            def __init__(self):
                self.snapshots = [
                    SimpleNamespace(status=SimpleNamespace(quench=True, power_module_failure=False, faulted=True)),
                    SimpleNamespace(status=SimpleNamespace(quench=True, power_module_failure=False, faulted=True)),
                    SimpleNamespace(status=SimpleNamespace(quench=False, power_module_failure=False, faulted=False)),
                    SimpleNamespace(status=SimpleNamespace(quench=True, power_module_failure=False, faulted=True)),
                ]
            def read_snapshot(self): return self.snapshots.pop(0)
        worker = _MagnetWorker()
        worker.adapter = FakeAdapter()
        faults = []
        worker.fault.connect(faults.append)
        for _ in range(4):
            worker.refresh_snapshot()
        self.assertEqual(faults, ["APS100 fault: quench", "APS100 fault: quench"])

    def test_manual_refresh_reports_success_and_pc_local_update_time(self):
        app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
        aps = _PanelAPS()
        panel = MagnetPanel(aps, _Panel2100())
        panel._on_connected("1000", SimpleNamespace(display_name="APS100"))

        panel._refresh_selected()
        self.assertEqual(aps.refresh_calls, 1)
        self.assertEqual(panel._refresh_pending_backend, "1000")
        self.assertFalse(panel.refresh_button.isEnabled())

        panel._on_snapshot("1000", SimpleNamespace(
            field_t=0.0, output_field_t=0.0, output_current_a=0.0,
            sweep_state="standby", heater_on=False, magnet_voltage_v=0.0,
            output_voltage_v=0.0, temperature_k=None,
            status=SimpleNamespace(standby=True, quench=False),
        ))

        self.assertIsNone(panel._refresh_pending_backend)
        self.assertTrue(panel.refresh_button.isEnabled())
        self.assertNotEqual(panel.last_update_label.text(), "Never")
        self.assertRegex(panel.last_update_label.text(), r"[+-]\d{4}$")
        self.assertIn("telemetry refreshed successfully", panel.activity_log.toPlainText())
        self.assertRegex(panel.activity_log.toPlainText().splitlines()[-1], r"^\d{4}-\d{2}-\d{2} .* [+-]\d{4} ")
        panel.deleteLater()
        app.processEvents()


if __name__ == "__main__":
    unittest.main()
