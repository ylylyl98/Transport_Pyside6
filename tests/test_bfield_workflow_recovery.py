"""Regression cases from the September 7 bench-log and state-machine audit."""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import json
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace as NS
from unittest.mock import patch

import test_transport_responsiveness as transport_fixture
import test_gate_scan_field_batch as gate_fixture
from app.models import BFieldTransportParams, BFieldTransportCondition, LineSweepParams, SaveRoot, Connections
from app.engine.bfield_transport_sweep import TransportSweepPlan
from app.devices.aps100_attodry1000_adapter import APS100AttoDry1000Adapter, APS100SafetyError
from app.workers.line_sweep import LineSweepWorker


class TransportRecoveryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        transport_fixture.TransportResponsivenessTests.setUpClass()

    def setUp(self):
        self.fixture = transport_fixture.TransportResponsivenessTests("test_bias_ramp_cancellation_prevents_late_sweep")
        self.fixture.setUp()
        self.c = self.fixture.controller
        self.c._log = lambda *a, **k: None
        self.c._checkpoint_runtime = lambda *a: None

    def tearDown(self):
        self.fixture.tearDown()

    def test_thermal_cancel_resumes_positioning_after_ack_and_recovery(self):
        c = self.c
        c._measurement_phase = "positioning"
        c._request_thermal_pause()
        c._on_safe_move(dict(success=False, cancelled=True, cancellation_reason="thermal"))
        with patch.object(c, "_position_at_start") as position:
            c.continue_after_temperature_check()
            position.assert_not_called()
            c._on_magnet_operation("pause")
            c.continue_after_temperature_check()
            position.assert_called_once()
        self.assertEqual(self.fixture.failures, [])

    def test_real_failure_during_thermal_hold_is_not_swallowed(self):
        self.c._thermal_hold = True
        self.c._on_safe_move(dict(success=False, error="hardware fault"))
        self.assertEqual(self.fixture.failures, ["hardware fault"])

    def test_temperature_recovery_never_resumes_without_manual_action(self):
        c = self.c
        c._measurement_phase = "positioning"
        c._request_thermal_pause()
        c._on_magnet_operation("pause")
        with patch.object(c, "_position_at_start") as position:
            c._resume_after_thermal_recovery()
            position.assert_not_called()
            c._cleanup_in_progress = True
            c.continue_after_temperature_check()
            position.assert_not_called()
            c._cleanup_in_progress = False
            c.continue_after_temperature_check()
            position.assert_called_once()

    def test_persistent_endpoint_does_not_wait_for_unrequested_operation(self):
        c = self.c
        c._transition_pending = c._persistent_row_transition = True
        c._thermal_hold = c._thermal_pause_pending = True
        events = []
        self.fixture.magnet.enter_persistent_mode = lambda **kw: events.append(kw)
        with patch.object(c, "_thermal_permission", return_value=(False, NS())):
            c._on_magnet_operation("pause")
            c._begin_row_persistent_transition()
            self.assertEqual(events, [dict(zero_leads=True)])
            self.assertFalse(c._transition_pending)
            c._on_magnet_operation("enter_persistent_mode")
            self.assertTrue(c._thermal_hold)
        c._row_persistent_ready_at = 0
        with patch.object(c, "_cleanup") as cleanup:
            c.continue_after_temperature_check()
            cleanup.assert_called_once_with("finished")

    def test_discarded_endpoint_read_still_advances_reverse_leg(self):
        c = self.c
        c._transition_pending = c._endpoint_ack_waiting = True
        c._transition_next = "return"
        c._sample_pending = 1
        with patch.object(c, "_commit_sample") as commit:
            c._sample_ready(dict(_monitor_generation=-1), None)
            commit.assert_not_called()
        self.assertEqual(c._sample_pending, 0)
        self.assertFalse(c._endpoint_ack_waiting)
        self.assertEqual(self.fixture.magnet.events[-1], ("sweep", c.plan.params.start_field_t))

    def test_continuation_permission_is_not_used_for_recovery(self):
        c = self.c
        c._measurement_phase = "positioning"
        with patch.object(c.thermal_safety, "evaluate_continuation", create=True,
                          return_value=self.fixture.decision) as continuation:
            self.assertTrue(c._thermal_permission()[0])
            continuation.assert_called_once()
            c._thermal_hold = True
            c._thermal_permission()
            c._thermal_permission(for_reentry=True)
            continuation.assert_called_once()

    def test_new_run_resets_thermal_and_transition_state(self):
        c = self.c
        c._thermal_hold = c._thermal_pause_pending = c._persistent_row_transition = True
        c._thermal_resume_action = "bias"
        c._enable_background_work()
        self.assertFalse(c._thermal_hold or c._thermal_pause_pending or c._persistent_row_transition)
        self.assertIsNone(c._thermal_resume_action)

    def test_failed_thermal_pause_cleanup_does_not_poison_next_sweep(self):
        c = self.c
        c._measurement_phase = "positioning"
        c._request_thermal_pause()
        # Failure cleanup takes ownership of the queued pause acknowledgement.
        c._cleanup_in_progress = True
        c._cleanup_waiting = "pause"
        c._cleanup_final_mode = "persistent"
        with patch.object(c, "_begin_persistent_cleanup"):
            c._on_magnet_operation("pause")
        self.assertTrue(c._thermal_hold and c._thermal_pause_pending)
        c._cleanup_in_progress = False
        c._enable_background_work()
        c._telemetry_watchdog.stop()
        with patch.object(c, "_note_heater_activation"), patch.object(c, "_apply_biases"):
            c._on_safe_move(dict(success=True))
            self.fixture.pump(lambda: c._io_lane.pending == 0)
        self.assertFalse(c._thermal_hold or c._thermal_pause_pending)
        self.assertEqual(c._measurement_phase, "starting_sweep")
        self.assertEqual(self.fixture.magnet.events[-1], ("sweep", c.plan.params.stop_field_t))
        self.assertEqual(self.fixture.failures, [])

    def test_roundtrip_persistent_policy_finishes_reverse_first(self):
        c = self.c
        c.plan = TransportSweepPlan.from_params(BFieldTransportParams(
            round_trip=True, cooldown_policy="persistent_each_row",
            conditions=[BFieldTransportCondition(), BFieldTransportCondition()]))
        c._leg_index = 0
        c._begin_endpoint_hold()
        self.assertFalse(c._persistent_row_transition)
        c._on_magnet_operation("pause")
        self.assertEqual(self.fixture.magnet.events[-1], ("sweep", c.plan.params.start_field_t))

    def test_oneway_next_condition_repositions(self):
        c = self.c
        c.plan = TransportSweepPlan.from_params(BFieldTransportParams(round_trip=False,
            conditions=[BFieldTransportCondition(), BFieldTransportCondition()]))
        with patch.object(c, "_position_at_start") as position, patch.object(c, "_start_condition") as bias:
            c._next_condition()
        position.assert_called_once()
        bias.assert_not_called()

    def test_positioning_thermal_recovery_does_not_sweep_none(self):
        c = self.c
        c._measurement_phase = "positioning"
        c._target = None
        c._request_thermal_pause()
        c._on_magnet_operation("pause")
        with patch.object(c, "_position_at_start") as position:
            c.continue_after_temperature_check()
        position.assert_called_once()

    def test_thermal_recovery_waits_for_actual_bias_completion(self):
        c = self.c
        c._enable_background_work()
        c._telemetry_watchdog.stop()
        entered, release = threading.Event(), threading.Event()
        c._apply_biases = lambda _: (entered.set(), release.wait(3))
        try:
            c._start_condition()
            self.fixture.pump(entered.is_set)
            c._request_thermal_pause()
            c._on_magnet_operation("pause")
            c.continue_after_temperature_check()
            self.assertNotIn(("sweep", c.plan.params.stop_field_t), self.fixture.magnet.events)
            release.set()
            self.fixture.pump(lambda: c._io_lane.pending == 0)
            self.assertNotIn(("sweep", c.plan.params.stop_field_t), self.fixture.magnet.events)
            c.continue_after_temperature_check()
            self.assertEqual(self.fixture.magnet.events[-1], ("sweep", c.plan.params.stop_field_t))
        finally:
            release.set()

    def test_bias_settle_is_executed_and_cancelable(self):
        c = self.c
        c.device_manager = NS(get_session=lambda _: NS(voltage=0.0, set_voltage=lambda v: None))
        start = time.monotonic()
        c._apply_biases(BFieldTransportCondition(settle_s=0.03))
        self.assertGreaterEqual(time.monotonic() - start, 0.025)
        c._io_cancel.set()
        with self.assertRaisesRegex(RuntimeError, "cancelled"):
            c._apply_biases(BFieldTransportCondition(settle_s=10))

    def test_missing_endpoint_ack_has_long_bounded_wait(self):
        c = self.c
        c._measurement_phase = "endpoint_hold"
        c._phase_started = time.monotonic() - 10000
        c._on_telemetry_watchdog()
        self.assertIn("endpoint_hold", self.fixture.failures[-1])

    def test_endpoint_ack_during_thermal_hold_preserves_next_leg(self):
        c = self.c
        c._transition_pending = True
        c._transition_next = "return"
        c._thermal_hold = True
        c._thermal_resume_action = "endpoint"
        with patch.object(c, "_thermal_permission", return_value=(False, NS())):
            c._on_magnet_operation("pause")
        self.assertTrue(c._transition_pending)
        self.assertEqual(c._transition_next, "return")
        c.continue_after_temperature_check()
        self.assertEqual(self.fixture.magnet.events[-1], ("sweep", c.plan.params.start_field_t))

    def test_slow_positioning_gets_rate_based_budget(self):
        c = self.c
        c.plan.params.start_field_t = -2.
        c.plan.params.rate_t_per_min = 0.01
        self.fixture.magnet.telemetry_cache.publish(NS(field_t=2., monotonic_s=time.monotonic()))
        self.fixture.magnet.safe_move_to_field = lambda *a, **k: captured.update(k)
        captured = {}
        c._position_at_start()
        self.assertGreater(captured["timeout_s"], 4. / 0.01 * 60.)


class GateRecoveryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        gate_fixture.GateScanFieldBatchTests.setUpClass()

    def setUp(self):
        self.fixture = gate_fixture.GateScanFieldBatchTests(unittest.defaultTestLoader.getTestCaseNames(gate_fixture.GateScanFieldBatchTests)[0])
        self.fixture.setUp()
        self.b = self.fixture.batch
        self.b._state.request = NS(condition_index=0, condition_name="A", fields_t=(1,), batch_id="audit")
        self.b._state.jobs = ((self.b._state.request, 1),)
        self.b._state.phase = "measuring"
        self.b._emit_activity = lambda *a, **k: None
        self.b._emit_state = lambda *a: None

    def tearDown(self):
        self.fixture.tearDown()

    def test_cooldown_stop_finishes_on_pause_ack(self):
        self.b._state.phase = "thermal_wait"
        self.b.stop()
        self.b._on_magnet_operation("pause")
        self.assertFalse(self.b.active)
        self.assertEqual(self.b._state.phase, "stopped")

    def test_same_field_fault_is_rejected_before_measurement(self):
        b = self.b
        b._last_persistent_field_t = 1
        b._snapshot = gate_fixture._snapshot(2, fault=True)
        with patch.object(b, "_start_measurement_after_move") as start:
            b._request_next_move()
        start.assert_not_called()
        self.assertEqual(b._state.phase, "failed")

    def test_disconnect_stops_worker_before_releasing(self):
        self.fixture.tab.worker = object()
        self.b._on_disconnected()
        self.assertIn(("stop",), self.fixture.tab.calls)
        self.assertTrue(self.b.active)
        self.fixture.tab.worker = None
        self.b._on_measurement_terminal("stopped", "partial.csv")
        self.assertFalse(self.b.active)
        self.assertEqual(self.b._state.phase, "failed")

    def test_same_field_stale_reading_requests_refresh(self):
        b = self.b
        b._last_persistent_field_t = 1
        b._snapshot = gate_fixture._snapshot(1)
        b._snapshot.monotonic_s = time.monotonic() - 100
        with patch.object(b, "_start_measurement_after_move") as start:
            b._request_next_move()
        start.assert_not_called()
        self.assertEqual(b._state.phase, "verifying")
        b._verification_timer.stop()

    def test_checkpoint_failure_is_reported(self):
        b = self.b
        b._checkpoint_path = os.path.join(self.fixture.tmp.name, "checkpoint.json")
        b._state.request.params = LineSweepParams()
        b._state.request.calibration = None
        with patch("app.engine.gate_scan_field_batch.os.replace", side_effect=OSError("disk full")):
            b._write_checkpoint("running")
        self.assertIn("disk full", b._checkpoint_write_error)

    def test_worker_zero_failure_is_terminal_error(self):
        with tempfile.TemporaryDirectory() as directory:
            p = LineSweepParams(output_csv_path=os.path.join(directory, "scan.csv"),
                output_metadata_path=os.path.join(directory, "scan_metadata.json"))
            fake = NS(voltage=1., set_voltage=lambda v: None)
            worker = LineSweepWorker(p, SaveRoot(base=directory), Connections(), g1=fake, g2=fake, g3=fake, daq=NS())
            worker._build_trajectory = lambda: [dict(vtg=1., vbg=1., vds=1.)]
            worker._validate_sessions = lambda: None
            worker._validate_trajectory_limits = lambda _: None
            worker._safe_ramp_vds = lambda *a, **k: None
            worker._run_trajectory_pass = lambda f, writer, *a: writer.writerow([0, 1, 1, 1])
            finished, errors = [], []
            worker.finished.connect(finished.append)
            worker.error.connect(errors.append)
            def ramp(fn, current, target, *a, **k):
                if target == 0:
                    raise OSError("GPIB zero failed")
            with patch("app.workers.line_sweep.safe_ramp", side_effect=ramp):
                worker.run()
            self.assertFalse(finished)
            self.assertIn("cleanup failed", errors[0])
            with open(p.output_metadata_path) as handle:
                metadata = json.load(handle)
            self.assertEqual(metadata["status"], "error")
            self.assertFalse(metadata["safe_state"]["ok"])


class FastZeroTests(unittest.TestCase):
    def test_worker_reports_intentional_pause_without_generic_fault(self):
        from unittest.mock import MagicMock
        from controllers.magnet_controller import _MagnetWorker
        from app.devices.aps100_attodry1000_adapter import MockAPS100Adapter, APS100OperationCancelled
        for reason in ("thermal", "monitor", "user"):
            with self.subTest(reason=reason):
                worker = _MagnetWorker()
                worker.adapter = MagicMock()
                worker.adapter.read_snapshot.return_value = MockAPS100Adapter().read_snapshot()
                worker.adapter.get_rates.return_value = {}
                worker.adapter.get_voltage_limit.return_value = 3.0
                errors, results, operations = [], [], []
                worker.error.connect(errors.append)
                worker.safe_move_result.connect(results.append)
                worker.operation_finished.connect(operations.append)
                def cancel(*args, **kwargs):
                    worker.request_stop(reason)
                    raise APS100OperationCancelled(worker._stop_event)
                worker.adapter.safe_move_to_field.side_effect = cancel
                worker.safe_move_to_field("test", 0.1, "driven", False, True, 0., 3600., .002, False)
                self.assertEqual(errors, [])
                self.assertEqual(operations, ["cancelled:safe_move"])
                self.assertTrue(results[0]["cancelled"])
                self.assertEqual(results[0]["cancellation_reason"], reason)

    def adapter(self, voltage=0.0, heater=0):
        adapter = APS100AttoDry1000Adapter(sleep_fn=lambda _: None)
        commands = []
        adapter._write = commands.append
        adapter._ensure_no_fault = lambda: None
        adapter.get_heater_state = lambda: heater
        adapter.get_magnet_voltage_v = lambda: voltage
        adapter.get_fast_rate_a_per_s = lambda: 5.
        adapter.get_output_field_t = lambda: 0.0 if "SWEEP ZERO FAST" in commands else 2.0
        adapter.get_sweep_state = lambda: "zeroing"
        adapter.get_status = lambda: NS(standby="SWEEP ZERO FAST" in commands, sweep_active="SWEEP ZERO FAST" not in commands)
        return adapter, commands

    def test_zero_selects_fast_after_voltage_check_without_changing_rate(self):
        adapter, commands = self.adapter()
        self.assertEqual(adapter.zero_output(verify_persistent_switch=True, max_magnet_voltage_v=0.1), 0.)
        self.assertEqual(commands, ["SWEEP ZERO SLOW", "SWEEP ZERO FAST"])

    def test_voltage_or_heater_invalid_never_selects_fast(self):
        for options in ({"voltage": 0.2}, {"voltage": float("nan")}, {"heater": 1}):
            with self.subTest(options=options):
                adapter, commands = self.adapter(**options)
                with self.assertRaises(APS100SafetyError):
                    adapter.zero_output(verify_persistent_switch=True, max_magnet_voltage_v=0.1)
                self.assertNotIn("SWEEP ZERO FAST", commands)

    def test_magnet_zero_never_uses_fast(self):
        adapter, commands = self.adapter(heater=1)
        adapter.get_output_field_t = lambda: 0.0 if commands else 2.0
        adapter.get_status = lambda: NS(standby=True, sweep_active=False)
        adapter.zero_output()
        self.assertEqual(commands, ["SWEEP ZERO SLOW"])

    def test_stalled_zero_eventually_pauses(self):
        adapter, commands = self.adapter()
        adapter.get_output_field_t = lambda: 2.0
        adapter.get_status = lambda: NS(standby=False, sweep_active=True)
        clock = [0.]
        adapter._sleep = lambda seconds: clock.__setitem__(0, clock[0] + 100.)
        with patch("app.devices.aps100_attodry1000_adapter.time.monotonic", side_effect=lambda: clock[0]):
            with self.assertRaisesRegex(Exception, "Timed out"):
                adapter.zero_output(timeout_s=1.)
        self.assertEqual(commands[-1], "SWEEP PAUSE")

    def test_progressing_motion_outlives_short_initial_timeout(self):
        adapter, commands = self.adapter()
        clock = [0.]
        adapter._sleep = lambda seconds: clock.__setitem__(0, clock[0] + 1.)
        adapter.get_field_t = lambda: min(3., clock[0])
        adapter.get_sweep_state = lambda: "sweep up"
        adapter.pause = lambda **k: commands.append("pause")
        with patch("app.devices.aps100_attodry1000_adapter.time.monotonic", side_effect=lambda: clock[0]):
            self.assertEqual(adapter.wait_for_field(3., timeout_s=0.5), 3.)
        self.assertEqual(commands, ["pause"])
