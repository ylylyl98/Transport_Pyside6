import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import threading
import time
import unittest
from types import SimpleNamespace as NS
from unittest.mock import patch

from PyQt6 import QtCore, QtWidgets
from app.engine.bfield_transport_controller import BFieldTransportController
from app.engine.bfield_transport_sweep import TransportSweepPlan
from app.engine.transport_tasks import TaskLane, LatestTelemetry
from app.models import BFieldTransportParams
from utils.config import cfg


class Magnet(QtCore.QObject):
    transport_config_result = QtCore.pyqtSignal(object)
    safe_move_result = QtCore.pyqtSignal(object)
    transport_sweep_result = QtCore.pyqtSignal(object)
    snapshot_updated = QtCore.pyqtSignal(object)
    fault = QtCore.pyqtSignal(str)
    operation_finished = QtCore.pyqtSignal(str)

    def __init__(self):
        super().__init__()
        self.telemetry_cache = LatestTelemetry()
        self.events = []

    def pause(self): self.events.append("pause")
    def set_polling_enabled(self, value): pass
    def start_transport_sweep(self, target): self.events.append(("sweep", target))
    def refresh_snapshot(self): pass


class TransportResponsivenessTests(unittest.TestCase):
    def test_transition_logs_ten_seconds_and_phase_immediate(self):
        messages = []
        self.controller._log = messages.append
        with patch("app.engine.bfield_transport_controller.time.monotonic", side_effect=[10, 11, 20, 20]):
            self.controller._on_transition_progress("matching leads", 0.1)
            self.controller._on_transition_progress("matching leads", 0.2)
            self.controller._on_transition_progress("matching leads", 0.3)
            self.controller._on_transition_progress("heater warming", 60)
        self.assertEqual(len(messages), 3)
        self.assertIn("60 s", messages[-1])

    def test_diagnostic_tail_is_bounded_and_dumped(self):
        import json
        import tempfile
        from pathlib import Path
        for i in range(100):
            self.controller._capture_telemetry("APS100", NS(field_t=i))
        self.assertEqual(len(self.controller._telemetry_diagnostics), 60)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "tail.jsonl"
            self.controller._diagnostics_path = str(path)
            self.controller._dump_telemetry_episode("test hold", kind="hold")
            payload = json.loads(path.read_text())
        self.assertEqual(payload["telemetry"][0]["snapshot"]["field_t"], 40)
        self.assertEqual(payload["kind"], "hold")

    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def setUp(self):
        self.magnet = Magnet()
        self.lake = LatestTelemetry()
        self.decision = NS(magnet_permission=True, state=NS(value="SAFE"), reason="safe")
        self.thermal = NS(is_armed=True, latest_snapshot=None, evaluate=lambda _: self.decision)
        self.controller = BFieldTransportController(self.magnet, NS(), NS(), self.thermal)
        self.controller.lakeshore_controller = NS(telemetry_cache=self.lake)
        self.controller.plan = TransportSweepPlan.from_params(BFieldTransportParams(start_field_t=-0.1, stop_field_t=0.1))
        self.controller._active = True
        self.controller._measurement_phase = "sweeping"
        self.controller._target = -0.1
        self.controller._leg_index = 1
        self.failures = []
        self.controller._fail = self.failures.append

    def tearDown(self):
        c = self.controller
        c._telemetry_watchdog.stop()
        for lane in (c._io_lane, c._storage_lane):
            if lane:
                self.pump(lambda: lane.pending == 0)
                lane.close()

    def pump(self, predicate, timeout=3):
        end = time.monotonic() + timeout
        while not predicate() and time.monotonic() < end:
            self.app.processEvents()
            time.sleep(0.002)
        self.assertTrue(predicate())

    def publish(self, stamp=None, **status):
        stamp = time.monotonic() if stamp is None else stamp
        self.magnet.telemetry_cache.publish(NS(monotonic_s=stamp, field_t=0.02,
            magnet_voltage_v=0.0, voltage_limit_v=3.0,
            status=NS(quench=False, power_module_failure=False, faulted=False, **status)))
        self.lake.publish(NS(monotonic_s=stamp, connected=True, communication_valid=True))

    def test_recovery_requires_pause_and_three_fresh_pairs_then_resumes_backward(self):
        c = self.controller
        c._begin_monitor_hold("stale Lake Shore")
        self.publish()
        c._check_monitor_recovery()
        self.assertEqual(self.magnet.events, ["pause"])
        self.magnet.operation_finished.emit("pause")
        c._check_monitor_recovery()  # identical cached readings cannot count again
        self.assertEqual(self.magnet.events, ["pause"])
        for _ in range(3):
            self.publish()
            c._check_monitor_recovery()
        self.assertEqual(self.magnet.events[-1], ("sweep", -0.1))
        self.assertEqual(c._leg_index, 1)
        self.assertIsNone(c._monitor_hold)
        self.assertEqual(self.failures, [])

    def test_missing_pause_ack_escalates(self):
        c = self.controller
        c._begin_monitor_hold("lost telemetry")
        c._monitor_hold["start"] -= 6
        c._check_monitor_recovery()
        self.assertIn("not acknowledged", self.failures[0])

    def test_recovery_timeout_and_stop_do_not_resume(self):
        c = self.controller
        c._begin_monitor_hold("lost telemetry")
        c._monitor_hold["acknowledged"] = True
        c._monitor_hold["start"] -= 31
        c._check_monitor_recovery()
        self.assertIn("30 s", self.failures[0])
        self.assertEqual(self.magnet.events, ["pause"])

    def test_real_temperature_trip_does_not_wait_for_missing_magnet(self):
        c = self.controller
        c._begin_monitor_hold("lost magnet telemetry")
        self.lake.publish(NS(monotonic_s=time.monotonic()))
        self.decision = NS(magnet_permission=False, state=NS(value="TRIPPED"), reason="temperature trip")
        c._check_monitor_recovery()
        self.assertIn("temperature trip", self.failures[0])

    def test_fresh_monitoring_can_wait_for_separate_thermal_dwell(self):
        c = self.controller
        c._begin_monitor_hold("stale Lake Shore")
        self.decision = NS(magnet_permission=False, state=NS(value="COOLDOWN_HOLD"), reason="recovery dwell")
        self.magnet.operation_finished.emit("pause")
        for _ in range(3):
            self.publish()
            c._check_monitor_recovery()
        self.assertIsNone(c._monitor_hold)
        self.assertTrue(c._thermal_hold)
        self.assertEqual(self.magnet.events, ["pause"])
        self.decision = NS(magnet_permission=True, state=NS(value="SAFE"), reason="safe")
        c._resume_after_thermal_recovery()
        self.assertEqual(self.magnet.events[-1], ("sweep", -0.1))

    def test_stop_clears_hold_and_late_reads_cannot_resume(self):
        c = self.controller
        c._begin_monitor_hold("delay")
        c._claimed = []
        c.device_manager = NS(get_session=lambda _: None)
        c.stop()
        self.publish()
        c.on_lakeshore_snapshot(self.lake.get())
        self.assertIsNone(c._monitor_hold)
        self.assertTrue(c._cleanup_in_progress)
        self.assertNotIn(("sweep", -0.1), self.magnet.events)

    def test_success_ignores_legacy_persistent_but_stop_still_forces_it(self):
        c = self.controller
        c.plan.params.final_mode = "persistent"
        c.device_manager = NS(get_session=lambda _: None)
        c._cleanup("finished")
        self.assertEqual(c._cleanup_final_mode, "driven")
        c.stop()
        self.assertEqual(c._cleanup_final_mode, "persistent")

    def test_actual_acquisition_does_not_block_telemetry_delivery(self):
        c = self.controller
        c._enable_background_work()
        c._telemetry_watchdog.stop()
        release, entered = threading.Event(), threading.Event()
        def acquire():
            entered.set()
            release.wait(1)
            return [1.0, 2.0, 3.0]
        c.device_manager = NS(get_session=lambda name: NS(acquire=acquire) if name == "daq" else None)
        self.publish()
        snap = self.magnet.telemetry_cache.get()
        c._record_snapshot(snap)
        self.pump(entered.is_set)
        self.publish()
        c.on_lakeshore_snapshot(self.lake.get())
        self.assertIs(self.thermal.latest_snapshot, self.lake.get())
        self.assertEqual(c._results, [])
        release.set()
        self.pump(lambda: len(c._results) == 1)
        self.assertEqual(c._results[0]["raw_DC"], 3.0)

    def test_fault_during_hold_is_fatal_even_without_lake(self):
        c = self.controller
        c._begin_monitor_hold("lost Lake Shore")
        self.magnet.telemetry_cache.publish(NS(monotonic_s=time.monotonic(), status=NS(quench=True)))
        c._check_monitor_recovery()
        self.assertIn("APS100 fault", self.failures[0])

    def test_superseded_snapshot_cannot_replace_newer_worker_reading(self):
        self.publish()
        newest = self.lake.get()
        old = NS(monotonic_s=newest.monotonic_s - 10)
        self.lake.publish(old)
        self.controller.on_lakeshore_snapshot(old)
        self.assertIs(self.thermal.latest_snapshot, newest)
        self.assertEqual(self.failures, [])

    def test_slow_io_and_storage_keep_qt_timer_running_and_preserve_order(self):
        c = self.controller
        c._enable_background_work()
        c._telemetry_watchdog.stop()
        events, ticks = [], []
        timer = QtCore.QTimer()
        timer.setInterval(5)
        timer.timeout.connect(lambda: ticks.append(1))
        timer.start()
        release = threading.Event()
        c._io_lane.submit(lambda: release.wait(1), lambda *_: events.append("read"))
        c._io_lane.submit(lambda: "zero", lambda *_: events.append("zero"))
        c._storage_lane.submit(lambda: release.wait(1), lambda *_: events.append("write"))
        self.pump(lambda: len(ticks) >= 5)
        self.assertEqual(events, [])
        release.set()
        self.pump(lambda: len(events) == 3)
        timer.stop()
        self.assertLess(events.index("read"), events.index("zero"))

    def test_acquisition_spanning_recovery_is_discarded(self):
        c = self.controller
        c._sample_pending = 1
        row = {"_monitor_generation": c._monitor_generation}
        c._begin_monitor_hold("delay")
        c._monitor_hold = None  # recovered before a slow read completed
        c._sample_ready(row, None)
        self.assertEqual(c._results, [])

    def test_endpoint_ack_waits_for_inflight_acquisition_before_reverse(self):
        c = self.controller
        c._transition_pending = True
        c._transition_next = "return"
        c._sample_pending = 1
        c._leg_index = 0
        c._target = 0.1
        self.magnet.operation_finished.emit("pause")
        self.assertTrue(c._endpoint_ack_waiting)
        self.assertEqual(self.magnet.events, [])
        c._commit_sample = lambda row: None
        c._sample_ready({"_monitor_generation": c._monitor_generation}, None)
        self.assertEqual(self.magnet.events, [("sweep", -0.1)])

    def test_cleanup_does_not_release_before_zeroing_and_storage_barrier(self):
        c = self.controller
        c._enable_background_work()
        c._telemetry_watchdog.stop()
        c._cleanup_in_progress = True
        c._bias_cleanup_done = False
        c._cleanup_waiting = "restore"
        released = []
        c._release_after_cleanup = lambda: released.append(True)
        release = threading.Event()
        c._finalize_files = lambda: release.wait(1)
        c._finish_cleanup()
        self.assertFalse(released)
        c._zero_biases_ready(None, None)
        self.app.processEvents()
        self.assertFalse(released)
        release.set()
        self.pump(lambda: bool(released))

    def test_bias_ramp_cancellation_prevents_late_sweep(self):
        c = self.controller
        c._enable_background_work()
        c._telemetry_watchdog.stop()
        entered = threading.Event()
        def ramp(_):
            entered.set()
            while True:
                c._check_io_cancel()
                time.sleep(0.002)
        c._apply_biases = ramp
        c._start_condition()
        self.pump(entered.is_set)
        c._cleanup_in_progress = True
        c._io_cancel.set()
        self.pump(lambda: c._io_lane.pending == 0)
        self.assertEqual(self.magnet.events, [])
