import os
import unittest
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6 import QtCore, QtWidgets

from app.ui.sample_temperature_bar import SampleTemperatureBar
from app.ui.widgets.run_panel import RunPanel


class _FakeLakeShoreController(QtCore.QObject):
    snapshot_updated = QtCore.pyqtSignal(object)
    control_result = QtCore.pyqtSignal(object)
    error = QtCore.pyqtSignal(str)
    fault = QtCore.pyqtSignal(str)
    disconnected = QtCore.pyqtSignal()

    def __init__(self):
        super().__init__()
        self.setpoints = []
        self.off_requests = 0

    def set_sample_setpoint(self, target):
        self.setpoints.append(float(target))

    def heater_off(self):
        self.off_requests += 1


def _snapshot(now, *, temperature=10.0, status="0", connected=True,
              communication_valid=True):
    return SimpleNamespace(
        sample_temperature_k=temperature,
        sample_sensor_status=status,
        monotonic_s=float(now),
        connected=connected,
        communication_valid=communication_valid,
    )


class SampleTemperatureControlUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def test_external_temperature_block_never_overrides_local_or_running_state(self):
        panel = RunPanel("Start")
        panel.set_start_available(False)
        panel.set_start_blocked("sample_temperature", True)
        panel.set_start_blocked("sample_temperature", False)
        self.assertFalse(panel.btn_start.isEnabled())

        panel.set_start_available(True)
        self.assertTrue(panel.btn_start.isEnabled())
        panel.set_running(True)
        panel.set_start_blocked("sample_temperature", True)
        panel.set_start_blocked("sample_temperature", False)
        self.assertFalse(panel.btn_start.isEnabled())
        panel.deleteLater()

    def test_invalid_sensor_time_does_not_count_toward_stability_dwell(self):
        now = [0.0]
        controller = _FakeLakeShoreController()
        config = SimpleNamespace(
            sample_stability_tolerance_k=0.05,
            sample_stability_dwell_s=5.0,
            maximum_reading_age_s=20.0,
        )
        bar = SampleTemperatureBar(controller, config, clock=lambda: now[0])
        bar._age_timer.stop()
        bar.set_backend("1000")
        bar.wait_check.setChecked(True)
        bar._target = 10.0

        bar.on_snapshot(_snapshot(now[0], status="16"))
        self.assertIsNone(bar._stable_since)
        now[0] = 10.0
        bar.on_snapshot(_snapshot(now[0], status="0"))
        self.assertEqual(bar._stable_since, 10.0)
        self.assertFalse(bar.is_ready())
        now[0] = 15.0
        self.assertTrue(bar.is_ready())
        bar.deleteLater()

    def test_stale_snapshot_invalidates_ready_without_a_new_snapshot(self):
        now = [0.0]
        controller = _FakeLakeShoreController()
        config = SimpleNamespace(
            sample_stability_tolerance_k=0.05,
            sample_stability_dwell_s=1.0,
            maximum_reading_age_s=3.0,
        )
        bar = SampleTemperatureBar(controller, config, clock=lambda: now[0])
        bar._age_timer.stop()
        bar.set_backend("1000")
        bar.wait_check.setChecked(True)
        bar._target = 10.0
        bar.on_snapshot(_snapshot(now[0]))
        now[0] = 1.0
        self.assertTrue(bar.is_ready())

        now[0] = 3.1
        bar._on_age_timer()
        self.assertFalse(bar.is_ready())
        self.assertIsNone(bar._stable_since)
        self.assertIn("stale telemetry", bar.current.text())
        bar.deleteLater()

    def test_target_persists_while_bar_visibility_follows_backend(self):
        controller = _FakeLakeShoreController()
        config = SimpleNamespace(
            sample_stability_tolerance_k=0.05,
            sample_stability_dwell_s=1.0,
            maximum_reading_age_s=3.0,
        )
        bar = SampleTemperatureBar(controller, config)
        bar._age_timer.stop()
        bar.set_backend("1000")
        bar.target.setValue(12.5)
        bar._set_target()
        bar.set_backend("2100")
        self.assertTrue(bar.isHidden())
        bar.set_backend("1000")
        self.assertFalse(bar.isHidden())
        self.assertEqual(bar._target, 12.5)
        self.assertEqual(controller.setpoints, [12.5])
        bar.deleteLater()


if __name__ == "__main__":
    unittest.main()
