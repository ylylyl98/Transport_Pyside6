import json
import time
import unittest
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import patch

from tests.test_gate_scan_field_batch import GateScanFieldBatchTests, _snapshot


class FieldSettleTests(unittest.TestCase):
    setUpClass = classmethod(GateScanFieldBatchTests.setUpClass.__func__)
    setUp = GateScanFieldBatchTests.setUp
    tearDown = GateScanFieldBatchTests.tearDown
    def _waiting(self):
        request = replace(self.batch._capture_request((0.25,)), field_settle_s=10.0)
        self.batch._state.request = request
        self.batch._state.jobs = ((request, 0.25),)
        self.batch._state.phase = "moving"
        snap = _snapshot(0.25)
        snap.monotonic_s = time.monotonic()
        self.batch._snapshot = snap
        self.batch._begin_field_settle(snap, 0.25)
        return request

    def test_delay_blocks_start_and_rechecks_at_completion(self):
        self._waiting()
        with patch.object(self.batch, "_start_measurement_after_move") as start:
            self.batch._poll_field_settle()
            start.assert_not_called()
            self.batch._settle_deadline = time.monotonic() - 1
            self.batch._poll_field_settle()
            start.assert_called_once()
        self.assertFalse(self.batch._settle_timer.isActive())

    def test_stop_and_fault_cancel_without_late_start(self):
        for action in (lambda: self.batch.stop(), lambda: self.batch._on_magnet_fault("fault")):
            self._waiting()
            with patch.object(self.batch, "_start_measurement_after_move") as start:
                action()
                self.batch._poll_field_settle()
                start.assert_not_called()
                self.assertFalse(self.batch._settle_timer.isActive())

    def test_stale_readback_and_thermal_denial_fail_closed(self):
        self._waiting()
        self.batch._snapshot.monotonic_s = time.monotonic() - 10000
        self.batch._settle_deadline = time.monotonic() - 1
        with patch.object(self.batch, "_start_measurement_after_move") as start:
            self.batch._poll_field_settle()
            start.assert_not_called()
            self.batch._settle_invalid_since = time.monotonic() - 6
            self.batch._poll_field_settle()
            self.assertEqual(self.batch._state.phase, "failed")
        self._waiting()
        with patch.object(self.batch, "_thermal_armed", return_value=True), patch.object(
            self.batch, "_evaluate_thermal", return_value=SimpleNamespace(magnet_permission=False, reason="hot")
        ), patch.object(self.batch, "_start_measurement_after_move") as start:
            self.batch._poll_field_settle()
            start.assert_not_called()
            self.assertEqual(self.batch._state.phase, "failed")

    def test_stale_recovery_restarts_full_delay(self):
        self._waiting()
        self.batch._snapshot.monotonic_s = time.monotonic() - 10000
        self.batch._settle_deadline = time.monotonic() - 1
        with patch.object(self.batch, "_start_measurement_after_move") as start:
            self.batch._poll_field_settle()
            self.batch._snapshot.monotonic_s = time.monotonic()
            before = time.monotonic()
            self.batch._poll_field_settle()
            start.assert_not_called()
            self.assertGreaterEqual(self.batch._settle_deadline, before + 10)
            self.batch._settle_deadline = time.monotonic() - 1
            self.batch._poll_field_settle()
            start.assert_called_once()

    def test_fresh_out_of_tolerance_field_fails(self):
        self._waiting()
        self.batch._snapshot = _snapshot(0.5)
        self.batch._snapshot.monotonic_s = time.monotonic()
        with patch.object(self.batch, "_start_measurement_after_move") as start:
            self.batch._poll_field_settle()
            start.assert_not_called()
            self.assertEqual(self.batch._state.phase, "failed")

    def test_verified_same_field_conditions_wait_only_once(self):
        request = replace(self.batch._capture_request((0.25,)), field_settle_s=10.0)
        second = replace(request, condition_index=1)
        self.batch._state.request = request
        self.batch._state.jobs = ((request, 0.25), (second, 0.25))
        self.batch._state.phase = "moving"
        snap = _snapshot(0.25)
        snap.monotonic_s = time.monotonic()
        self.batch._snapshot = snap
        with patch.object(self.batch, "_start_measurement_after_move") as start:
            self.batch._complete_move_verification(snap)
            self.assertEqual(self.batch._state.phase, "field_settle")
            start.assert_not_called()
            self.batch._settle_deadline = time.monotonic() - 1
            self.batch._poll_field_settle()
            self.assertEqual(start.call_count, 1)
            self.batch._state.index = 1
            self.batch._request_next_move()
            self.assertEqual(start.call_count, 2)
            self.assertFalse(self.batch._settle_timer.isActive())

    def test_live_thermal_warning_aborts_even_with_stale_field(self):
        self._waiting()
        self.batch._snapshot.monotonic_s = time.monotonic() - 10000
        decision = SimpleNamespace(state=SimpleNamespace(value="WARNING"), reason="hot")
        self.batch.thermal_safety = SimpleNamespace(evaluate=lambda snapshot: decision,
                                                     evaluate_continuation=lambda snapshot: decision)
        with patch.object(self.batch, "_start_measurement_after_move") as start:
            self.batch._check_live_thermal()
            self.assertEqual(self.batch._state.phase, "failed")
            self.batch._poll_field_settle()
            start.assert_not_called()

    def test_checkpoint_resume_checks_delay_and_legacy_default(self):
        from app.run_output import to_jsonable
        import os
        request = replace(self.batch._capture_request((0.25,)), field_approach="linear", field_rate_t_per_min=0.1, field_settle_s=2.5)
        self.batch._state.request = request
        self.batch._state.jobs = ((request, 0.25),)
        self.batch._state.phase = "moving"
        self.batch._checkpoint_path = os.path.join(self.tmp.name, "checkpoint.json")
        self.batch._write_checkpoint("stopped")
        with open(self.batch._checkpoint_path, encoding="utf-8") as handle:
            saved = json.load(handle)
        self.assertEqual(saved["field_control"]["field_settle_s"], 2.5)
        self.batch._validate_resume(self.batch._checkpoint_path, (request,))
        with self.assertRaisesRegex(ValueError, "settle"):
            self.batch._validate_resume(self.batch._checkpoint_path, (replace(request, field_settle_s=0),))
        del saved["field_control"]["field_settle_s"]
        with open(self.batch._checkpoint_path, "w", encoding="utf-8") as handle:
            json.dump(saved, handle)
        self.batch._validate_resume(self.batch._checkpoint_path, (replace(request, field_settle_s=0),))

    def test_capture_validation_and_checkpoint_metadata(self):
        self.tab.get_field_control_settings = lambda: {"field_settle_s": 2.5}
        request = self.batch._capture_requests((0.25,))[0]
        self.assertEqual(request.field_settle_s, 2.5)
        self._waiting()
        self.batch._state.request = request
        self.assertEqual(self.batch._metadata(0.25, self.batch._snapshot)["gate_scan_bfield_batch"]["field_settle_s"], 2.5)
        self.tab.get_field_control_settings = lambda: {"field_settle_s": float("nan")}
        with self.assertRaises(ValueError):
            self.batch._capture_requests((0.25,))


# Avoid collecting the imported base test class a second time.
del GateScanFieldBatchTests
