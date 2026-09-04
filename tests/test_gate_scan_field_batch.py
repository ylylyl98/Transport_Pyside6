import os
import json
import tempfile
import time
import unittest
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

from PyQt6 import QtCore, QtWidgets

from app.engine.gate_scan_field_batch import GateScanFieldBatch
from app.models import LineSweepParams, SaveRoot
from app.run_output import PlannedOutput
from app.devices.lakeshore335_adapter import MockLakeShore335Adapter
from app.thermal_safety import ThermalSafetyEvaluator
from utils.config import LakeShore335Config


def _snapshot(field, *, heater=False, output=0.0, standby=True, fault=False):
    return SimpleNamespace(
        field_t=float(field),
        output_field_t=float(output),
        output_current_a=float(output),
        heater_on=bool(heater),
        sweep_state="idle",
        status=SimpleNamespace(
            standby=bool(standby), sweep_active=False, quench=bool(fault),
            power_module_failure=False, raw=4,
        ),
    )


class _FakeMagnet(QtCore.QObject):
    snapshot_updated = QtCore.pyqtSignal(object)
    safe_move_result = QtCore.pyqtSignal(object)
    fault = QtCore.pyqtSignal(str)

    def __init__(self):
        super().__init__()
        self.is_connected = True
        self.calls = []
        self.owner = ""
        self.next_id = 0

    def acquire_exclusive(self, owner):
        if self.owner:
            return False
        self.owner = owner
        self.calls.append(("acquire", owner))
        return True

    def release_exclusive(self, owner):
        self.calls.append(("release", owner))
        if self.owner == owner:
            self.owner = ""

    def safe_move_to_field(self, field, **kwargs):
        self.next_id += 1
        request_id = f"move-{self.next_id}"
        self.calls.append(("move", float(field), kwargs, request_id))
        return request_id

    def pause(self):
        self.calls.append(("pause",))


class _FakeTab(QtCore.QObject):
    batch_run_terminal = QtCore.pyqtSignal(str, str)

    def __init__(self, directory):
        super().__init__()
        self.save = SaveRoot(base=directory)
        self._planned_output = PlannedOutput(
            run_id="ordinary-run",
            output_dir=directory,
            stem="device_gate_scan_raw_keithley_g3_forward",
            csv_path=os.path.join(directory, "ordinary.csv"),
            metadata_path=os.path.join(directory, "ordinary_metadata.json"),
            log_path=os.path.join(directory, "ordinary_run_log.txt"),
        )
        self.worker = None
        self.calls = []
        self.params = LineSweepParams()

    def capture_field_batch_params(self):
        self.calls.append(("capture",))
        return self.params, ("daq",)

    def begin_field_batch(self, params, devices):
        self.calls.append(("begin", params, tuple(devices)))
        return True

    def set_batch_locked(self, value):
        self.calls.append(("locked", bool(value)))

    def start_field_batch_measurement(self, output, metadata):
        self.calls.append(("start", output, metadata))
        self._planned_output = output
        self.params.output_csv_path = output.csv_path
        self.params.output_metadata_path = output.metadata_path
        self.params.output_log_path = output.log_path
        self.worker = object()
        return True

    def stop_run(self):
        self.calls.append(("stop",))

    def finish_field_batch(self):
        self.calls.append(("finish",))
        self.worker = None


class _MultiConditionTab(_FakeTab):
    def capture_field_batch_requests(self):
        first = deepcopy(self.params)
        second = deepcopy(self.params)
        second.mode = "Derived"
        second.derived_axis = "E-field"
        return [
            (first, ("daq",), None, "Raw condition"),
            (second, ("daq",), None, "E-field condition"),
        ]

    def set_field_batch_condition(self, params):
        self.calls.append(("condition", params.mode, params.derived_axis))

    @staticmethod
    def validate_field_batch_request(params):
        if params.mode == "Raw" and not any((params.raw_vtg_active, params.raw_vbg_active, params.raw_vds_active)):
            raise ValueError("Raw trajectory needs at least one active variable")


class GateScanFieldBatchTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.magnet = _FakeMagnet()
        self.tab = _FakeTab(self.tmp.name)
        self.batch = GateScanFieldBatch(self.magnet, self.tab)
        self.batch.set_review_validator(lambda: True)
        self.batch._on_snapshot(_snapshot(0.0))

    def tearDown(self):
        self.tmp.cleanup()

    def test_parse_and_format_targets(self):
        self.assertEqual(self.batch.parse_fields("-2\n-0.5\n0\n0.125"), (-2.0, -0.5, 0.0, 0.125))
        self.assertEqual(self.batch.parse_fields("1:-1:-1"), (1.0, 0.0))
        self.assertEqual(self.batch.parse_fields("0:1:0.25"), (0.0, 0.25, 0.5, 0.75))
        self.assertEqual(
            self.batch.parse_fields("1, 2:3:0.5\n4"),
            (1.0, 2.0, 2.5, 4.0),
        )
        self.assertEqual(self.batch.format_field_tag(-2), "B_-2T")
        self.assertEqual(self.batch.format_field_tag(-0.5), "B_-0.5T")
        self.assertEqual(self.batch.format_field_tag(0), "B_0T")
        self.assertEqual(self.batch.format_field_tag(0.125), "B_0.125T")
        with self.assertRaises(ValueError):
            self.batch.parse_fields("0\n-0.0")

    def test_invalid_cooldown_snapshot_does_not_queue_recursive_refresh(self):
        class _LakeShore:
            def __init__(self):
                self.refresh_calls = 0
            def refresh_snapshot(self):
                self.refresh_calls += 1

        controller = _LakeShore()
        config = LakeShore335Config(
            enabled=True, verified_channel_mapping=True,
            sample_channel="A", reservoir_channel="B",
            sample_warning_temperature_k=5.0, sample_trip_temperature_k=6.0,
            sample_recovery_temperature_k=4.0,
            reservoir_warning_temperature_k=5.0, reservoir_trip_temperature_k=6.0,
            reservoir_recovery_temperature_k=4.0, maximum_reading_age_s=3.0,
            required_stable_recovery_dwell_s=0.0,
            minimum_interval_between_heater_activations_s=0.0,
        )
        self.batch.thermal_safety = ThermalSafetyEvaluator(config)
        self.batch.lakeshore_controller = controller
        self.batch._state.request = object()
        self.batch._state.phase = "thermal_wait"
        self.batch._cooldown_started_at = time.monotonic()
        # Model the permitted one-shot refresh on entry, then clear it.
        controller.refresh_snapshot()
        controller.refresh_calls = 0
        invalid = SimpleNamespace(
            monotonic_s=time.monotonic(), connected=False,
            communication_valid=False, diagnostic_error="Invalid session handle",
            sample_temperature_k=None, reservoir_temperature_k=None,
            sample_sensor_status="COMMUNICATION_FAULT",
            reservoir_sensor_status="COMMUNICATION_FAULT",
        )
        self.batch.on_lakeshore_snapshot(invalid)
        self.assertEqual(controller.refresh_calls, 0)
        self.assertEqual(self.batch._state.phase, "thermal_wait")
        self.assertEqual([c for c in self.magnet.calls if c[0] == "move"], [])

    def test_parse_ranges_reject_invalid_direction_and_zero_step(self):
        with self.assertRaisesRegex(ValueError, "step cannot be zero"):
            self.batch.parse_fields("1:-1:0")
        with self.assertRaisesRegex(ValueError, "direction"):
            self.batch.parse_fields("0:1:-0.25")
        with self.assertRaisesRegex(ValueError, "direction"):
            self.batch.parse_fields("1:0:0.25")

    def test_parse_ranges_support_negative_fractional_steps_and_validation(self):
        self.assertEqual(
            self.batch.parse_fields("1:-0.5:-0.5"),
            (1.0, 0.5, 0.0),
        )
        with self.assertRaisesRegex(ValueError, "start:stop:step"):
            self.batch.parse_fields("0:1")
        with self.assertRaisesRegex(ValueError, "finite"):
            self.batch.parse_fields("0:nan:0.1")
        with self.assertRaisesRegex(ValueError, "finite"):
            self.batch.parse_fields("0:1:inf")

    def test_parse_ranges_keep_stop_exclusive_and_enforce_expansion_cap(self):
        self.assertEqual(self.batch.parse_fields("0:0.3:0.1"), (0.0, 0.1, 0.2))
        with self.assertRaisesRegex(ValueError, "more than|cannot exceed"):
            self.batch.parse_fields("0:10001:1")
        with self.assertRaisesRegex(ValueError, "Duplicate"):
            self.batch.parse_fields("0:1:0.5, 0.5")

    def test_parse_ranges_enforce_field_limit_after_expansion(self):
        with patch.object(GateScanFieldBatch, "MAX_EXPANDED_FIELDS", 10):
            with patch("app.engine.gate_scan_field_batch.cfg.magnet.safe_control_max_field_t", 0.5):
                with self.assertRaisesRegex(ValueError, "within"):
                    self.batch.parse_fields("0:1:0.25")

    def test_parse_ranges_allow_exact_cap_but_reject_combined_over_cap(self):
        fields = self.batch.parse_fields("0:8:0.0008")
        self.assertEqual(len(fields), self.batch.MAX_EXPANDED_FIELDS)
        with self.assertRaisesRegex(ValueError, "cannot exceed"):
            self.batch.parse_fields("0:8:0.0008, 0")

    def test_preflight_and_collision_happen_before_movement(self):
        with patch("app.engine.gate_scan_field_batch.QtWidgets.QMessageBox.question", return_value=QtWidgets.QMessageBox.StandardButton.Yes):
            self.assertTrue(self.batch.start("0.5\n1.0"))
        self.assertEqual(len([call for call in self.magnet.calls if call[0] == "move"]), 1)
        self.batch.stop()
        self.magnet.safe_move_result.emit({"request_id": "move-1", "success": False, "error": "stopped"})
        self.assertEqual(self.magnet.owner, "")

        # A fresh batch with an existing first-field CSV must not acquire or move.
        self.magnet = _FakeMagnet()
        self.tab = _FakeTab(self.tmp.name)
        self.batch = GateScanFieldBatch(self.magnet, self.tab)
        self.batch.set_review_validator(lambda: True)
        self.batch._on_snapshot(_snapshot(0.0))
        first = os.path.join(self.tmp.name, "device_gate_scan_raw_keithley_g3_forward_B_0.5T.csv")
        open(first, "w", encoding="utf-8").close()
        with patch("app.engine.gate_scan_field_batch.QtWidgets.QMessageBox.question", return_value=QtWidgets.QMessageBox.StandardButton.Yes):
            self.assertFalse(self.batch.start("0.5"))
        self.assertFalse(any(call[0] == "move" for call in self.magnet.calls))

    def test_non_aps100_backend_is_rejected_before_magnet_use(self):
        self.magnet = _FakeMagnet()
        self.tab = _FakeTab(self.tmp.name)
        self.batch = GateScanFieldBatch(
            self.magnet,
            self.tab,
            selected_backend_callable=lambda: "2100",
        )
        self.batch.set_review_validator(lambda: True)
        self.batch._on_snapshot(_snapshot(0.0))
        errors = []
        self.batch.error.connect(errors.append)
        with patch("app.engine.gate_scan_field_batch.QtWidgets.QMessageBox.question", return_value=QtWidgets.QMessageBox.StandardButton.Yes):
            self.assertFalse(self.batch.start("0.25"))
        self.assertFalse(any(call[0] == "move" for call in self.magnet.calls))
        self.assertFalse(any(call[0] == "acquire" for call in self.magnet.calls))
        self.assertTrue(errors)
        self.assertIn("attoDRY1000 (APS100)", errors[0])

    def test_persistent_verification_is_required_before_scan(self):
        with patch("app.engine.gate_scan_field_batch.QtWidgets.QMessageBox.question", return_value=QtWidgets.QMessageBox.StandardButton.Yes):
            self.assertTrue(self.batch.start("0.25"))
        move = [call for call in self.magnet.calls if call[0] == "move"][0]
        self.assertEqual(move[2]["final_mode"], "persistent")
        self.assertTrue(move[2]["zero_leads"])
        self.magnet.safe_move_result.emit({
            "request_id": move[3], "success": True,
            "snapshot": _snapshot(0.2), "audit": {"id": "audit-1"},
        })
        self.assertFalse(any(call[0] == "start" for call in self.tab.calls))
        self.assertEqual(self.batch._state.phase, "failed")

    def test_review_and_request_id_gate_movement_result(self):
        self.batch.set_review_validator(lambda: False)
        with patch("app.engine.gate_scan_field_batch.QtWidgets.QMessageBox.question", return_value=QtWidgets.QMessageBox.StandardButton.Yes):
            self.assertFalse(self.batch.start("0.25"))
        self.assertFalse(any(call[0] == "move" for call in self.magnet.calls))

        self.batch.set_review_validator(lambda: True)
        with patch("app.engine.gate_scan_field_batch.QtWidgets.QMessageBox.question", return_value=QtWidgets.QMessageBox.StandardButton.Yes):
            self.assertTrue(self.batch.start("0.25"))
        move = [call for call in self.magnet.calls if call[0] == "move"][0]
        self.magnet.safe_move_result.emit({"request_id": "stale", "success": True, "snapshot": _snapshot(0.25), "audit": {}})
        self.assertFalse(any(call[0] == "start" for call in self.tab.calls))
        self.magnet.safe_move_result.emit({"request_id": move[3], "success": True, "snapshot": _snapshot(0.25), "audit": {}})
        self.assertTrue(any(call[0] == "start" for call in self.tab.calls))
        starts_before = len([call for call in self.tab.calls if call[0] == "start"])
        self.magnet.safe_move_result.emit({"request_id": move[3], "success": True, "snapshot": _snapshot(0.25), "audit": {}})
        self.assertEqual(len([call for call in self.tab.calls if call[0] == "start"]), starts_before)

    def test_sequence_starts_next_field_only_after_scan_terminal(self):
        with patch("app.engine.gate_scan_field_batch.QtWidgets.QMessageBox.question", return_value=QtWidgets.QMessageBox.StandardButton.Yes):
            self.assertTrue(self.batch.start("0.25\n-0.5"))
        first = [call for call in self.magnet.calls if call[0] == "move"][0]
        self.magnet.safe_move_result.emit({"request_id": first[3], "success": True, "snapshot": _snapshot(0.25), "audit": {}})
        starts = [call for call in self.tab.calls if call[0] == "start"]
        self.assertEqual(len(starts), 1)
        self.assertIn("requested_field_t", starts[0][2]["gate_scan_bfield_batch"])
        self.tab.batch_run_terminal.emit("finished", "field0.csv")
        moves = [call for call in self.magnet.calls if call[0] == "move"]
        self.assertEqual([call[1] for call in moves], [0.25, -0.5])
        self.assertEqual([call[2]["final_mode"] for call in moves], ["persistent", "persistent"])
        self.magnet.safe_move_result.emit({"request_id": moves[-1][3], "success": True, "snapshot": _snapshot(-0.5), "audit": {}})
        starts = [call for call in self.tab.calls if call[0] == "start"]
        self.assertEqual(len(starts), 2)
        output = starts[0][1]
        self.assertTrue(output.csv_path.endswith("_B_0.25T.csv"))
        self.assertNotIn("202", os.path.basename(output.csv_path))
        second_output = starts[1][1]
        self.assertTrue(second_output.csv_path.endswith("_B_-0.5T.csv"))
        self.assertNotIn("_B_0.25T_B_", os.path.basename(second_output.csv_path))
        self.assertEqual(self.tab.params.output_csv_path, second_output.csv_path)
        with open(self.batch._checkpoint_path, encoding="utf-8") as handle:
            checkpoint = json.load(handle)
        self.assertEqual(checkpoint["fields_t"], [0.25, -0.5])
        self.assertEqual(checkpoint["results"][0]["status"], "job_complete")

    def test_fault_and_nonpersistent_result_never_start_scan(self):
        cases = (
            _snapshot(0.25, heater=True),
            _snapshot(0.25, output=0.1),
            _snapshot(0.25, standby=False),
            _snapshot(0.25, fault=True),
        )
        for final_snapshot in cases:
            with self.subTest(snapshot=final_snapshot):
                self.magnet = _FakeMagnet()
                self.tab = _FakeTab(self.tmp.name)
                self.batch = GateScanFieldBatch(self.magnet, self.tab)
                self.batch.set_review_validator(lambda: True)
                self.batch._on_snapshot(_snapshot(0.0))
                with patch("app.engine.gate_scan_field_batch.QtWidgets.QMessageBox.question", return_value=QtWidgets.QMessageBox.StandardButton.Yes):
                    self.assertTrue(self.batch.start("0.25"))
                move = [call for call in self.magnet.calls if call[0] == "move"][0]
                self.magnet.safe_move_result.emit({"request_id": move[3], "success": True, "snapshot": final_snapshot, "audit": {}})
                self.assertFalse(any(call[0] == "start" for call in self.tab.calls))
                self.assertEqual(self.magnet.owner, "")

    def test_stop_during_measurement_releases_only_after_terminal(self):
        with patch("app.engine.gate_scan_field_batch.QtWidgets.QMessageBox.question", return_value=QtWidgets.QMessageBox.StandardButton.Yes):
            self.batch.start("0.25")
        move = [call for call in self.magnet.calls if call[0] == "move"][0]
        self.magnet.safe_move_result.emit({"request_id": move[3], "success": True, "snapshot": _snapshot(0.25), "audit": {}})
        self.batch.stop()
        self.assertIn(("pause",), self.magnet.calls)
        self.assertIn(("stop",), self.tab.calls)
        self.assertNotEqual(self.magnet.owner, "")
        self.tab.batch_run_terminal.emit("stopped", "user stop")
        self.assertEqual(self.magnet.owner, "")
        self.assertFalse(any(call[0] == "move" for call in self.magnet.calls[2:]))
        with open(self.batch._checkpoint_path, encoding="utf-8") as handle:
            checkpoint = json.load(handle)
        self.assertEqual(checkpoint["status"], "stopped")
        self.assertEqual(checkpoint["next_job_index"], 0)

    def test_move_failure_writes_failed_checkpoint_before_cleanup(self):
        with patch("app.engine.gate_scan_field_batch.QtWidgets.QMessageBox.question", return_value=QtWidgets.QMessageBox.StandardButton.Yes):
            self.assertTrue(self.batch.start("0.25"))
        move = [call for call in self.magnet.calls if call[0] == "move"][0]
        self.magnet.safe_move_result.emit({"request_id": move[3], "success": False, "error": "over-temperature"})
        with open(self.batch._checkpoint_path, encoding="utf-8") as handle:
            checkpoint = json.load(handle)
        self.assertEqual(checkpoint["status"], "failed")
        self.assertIn("over-temperature", checkpoint["error"])
        self.assertEqual(checkpoint["next_job_index"], 0)

    def test_multiple_conditions_run_field_major_in_declared_field_order(self):
        self.tab = _MultiConditionTab(self.tmp.name)
        self.batch = GateScanFieldBatch(self.magnet, self.tab)
        self.batch.set_review_validator(lambda: True)
        self.batch._on_snapshot(_snapshot(0.0))
        with patch("app.engine.gate_scan_field_batch.QtWidgets.QMessageBox.question", return_value=QtWidgets.QMessageBox.StandardButton.Yes):
            self.assertTrue(self.batch.start("0.25,-0.5"))
        move = [call for call in self.magnet.calls if call[0] == "move"][-1]
        self.magnet.safe_move_result.emit({"request_id": move[3], "success": True, "snapshot": _snapshot(move[1]), "audit": {}})
        self.tab.batch_run_terminal.emit("finished", "raw-first.csv")
        move = [call for call in self.magnet.calls if call[0] == "move"][-1]
        self.magnet.safe_move_result.emit({"request_id": move[3], "success": True, "snapshot": _snapshot(move[1]), "audit": {}})
        self.tab.batch_run_terminal.emit("finished", "raw-second.csv")
        moves = [call[1] for call in self.magnet.calls if call[0] == "move"]
        self.assertEqual(moves, [0.25, -0.5])
        starts = [call for call in self.tab.calls if call[0] == "start"]
        self.assertEqual(len(starts), 2)
        self.assertTrue(starts[0][1].csv_path.endswith("_C1_B_0.25T.csv"))
        self.assertTrue(starts[1][1].csv_path.endswith("_C2_B_0.25T.csv"))
        self.assertTrue(any(call[0] == "condition" for call in self.tab.calls))

    def test_stale_telemetry_is_rejected_and_refresh_requested(self):
        self.batch._snapshot_received_at = datetime.now(timezone.utc) - timedelta(seconds=30)
        refresh_calls = []
        self.magnet.refresh_snapshot = lambda: refresh_calls.append(True)
        errors = []
        self.batch.error.connect(errors.append)
        with patch("app.engine.gate_scan_field_batch.QtWidgets.QMessageBox.question", return_value=QtWidgets.QMessageBox.StandardButton.Yes):
            self.assertFalse(self.batch.start("0.25"))
        self.assertEqual(refresh_calls, [True])
        self.assertFalse(any(call[0] == "move" for call in self.magnet.calls))
        self.assertIn("stale", errors[0])

    def test_armed_thermal_preflight_rejects_without_aps_move(self):
        config = LakeShore335Config(
            enabled=True, verified_channel_mapping=True,
            sample_warning_temperature_k=5, sample_trip_temperature_k=6,
            sample_recovery_temperature_k=4, reservoir_warning_temperature_k=5,
            reservoir_trip_temperature_k=6, reservoir_recovery_temperature_k=4,
            required_stable_recovery_dwell_s=0,
            minimum_interval_between_heater_activations_s=0,
        )
        evaluator = ThermalSafetyEvaluator(config)
        evaluator.evaluate(MockLakeShore335Adapter(sample_temperature_k=7, reservoir_temperature_k=2).read_snapshot())
        batch = GateScanFieldBatch(self.magnet, self.tab, thermal_safety=evaluator)
        batch._on_snapshot(_snapshot(0.0))
        with patch("app.engine.gate_scan_field_batch.QtWidgets.QMessageBox.question", return_value=QtWidgets.QMessageBox.StandardButton.Yes):
            self.assertFalse(batch.start("0.25"))
        self.assertFalse(any(call[0] == "move" for call in self.magnet.calls))

    def test_stable_dwell_preflight_waits_then_starts_automatically(self):
        config = LakeShore335Config(
            enabled=True, verified_channel_mapping=True,
            sample_warning_temperature_k=5, sample_trip_temperature_k=6,
            sample_recovery_temperature_k=4, reservoir_warning_temperature_k=5,
            reservoir_trip_temperature_k=6, reservoir_recovery_temperature_k=4,
            required_stable_recovery_dwell_s=30,
            minimum_interval_between_heater_activations_s=0,
        )
        evaluator = ThermalSafetyEvaluator(config)
        ls = MockLakeShore335Adapter(sample_temperature_k=3, reservoir_temperature_k=3)
        decision = evaluator.evaluate(ls.read_snapshot())
        self.assertEqual(decision.block_code, "stable_recovery_dwell")
        batch = GateScanFieldBatch(self.magnet, self.tab, thermal_safety=evaluator)
        batch._on_snapshot(_snapshot(0.0))

        with patch("app.engine.gate_scan_field_batch.QtWidgets.QMessageBox.question", return_value=QtWidgets.QMessageBox.StandardButton.Yes):
            self.assertTrue(batch.start("0.25"))

        self.assertEqual(batch._state.phase, "thermal_wait")
        self.assertEqual(batch._thermal_wait_context, "preflight")
        self.assertFalse(any(call[0] == "move" for call in self.magnet.calls))
        evaluator._stable_since = time.monotonic() - 31
        batch.on_lakeshore_snapshot(ls.read_snapshot())
        self.assertEqual([call[1] for call in self.magnet.calls if call[0] == "move"], [0.25])

    def test_minimum_heater_interval_preflight_waits_then_starts_automatically(self):
        config = LakeShore335Config(
            enabled=True, verified_channel_mapping=True,
            sample_warning_temperature_k=5, sample_trip_temperature_k=6,
            sample_recovery_temperature_k=4, reservoir_warning_temperature_k=5,
            reservoir_trip_temperature_k=6, reservoir_recovery_temperature_k=4,
            required_stable_recovery_dwell_s=0,
            minimum_interval_between_heater_activations_s=60,
        )
        evaluator = ThermalSafetyEvaluator(config)
        ls = MockLakeShore335Adapter(sample_temperature_k=3, reservoir_temperature_k=3)
        evaluator.note_heater_activation()
        decision = evaluator.evaluate(ls.read_snapshot())
        self.assertEqual(decision.block_code, "minimum_heater_interval")
        batch = GateScanFieldBatch(self.magnet, self.tab, thermal_safety=evaluator)
        batch._on_snapshot(_snapshot(0.0))

        with patch("app.engine.gate_scan_field_batch.QtWidgets.QMessageBox.question", return_value=QtWidgets.QMessageBox.StandardButton.Yes):
            self.assertTrue(batch.start("0.25"))

        self.assertEqual(batch._state.phase, "thermal_wait")
        self.assertFalse(any(call[0] == "move" for call in self.magnet.calls))
        evaluator._last_heater_activation = time.monotonic() - 61
        batch.on_lakeshore_snapshot(ls.read_snapshot())
        self.assertEqual([call[1] for call in self.magnet.calls if call[0] == "move"], [0.25])

    def test_preflight_wait_fails_if_sensor_status_becomes_invalid(self):
        config = LakeShore335Config(
            enabled=True, verified_channel_mapping=True,
            sample_warning_temperature_k=5, sample_trip_temperature_k=6,
            sample_recovery_temperature_k=4, reservoir_warning_temperature_k=5,
            reservoir_trip_temperature_k=6, reservoir_recovery_temperature_k=4,
            required_stable_recovery_dwell_s=30,
            minimum_interval_between_heater_activations_s=0,
        )
        evaluator = ThermalSafetyEvaluator(config)
        ls = MockLakeShore335Adapter(sample_temperature_k=3, reservoir_temperature_k=3)
        evaluator.evaluate(ls.read_snapshot())
        batch = GateScanFieldBatch(self.magnet, self.tab, thermal_safety=evaluator)
        batch._on_snapshot(_snapshot(0.0))
        errors = []
        batch.error.connect(errors.append)

        with patch("app.engine.gate_scan_field_batch.QtWidgets.QMessageBox.question", return_value=QtWidgets.QMessageBox.StandardButton.Yes):
            self.assertTrue(batch.start("0.25"))

        ls.set_readings(3.0, 3.0, sample_sensor_status="001")
        batch.on_lakeshore_snapshot(ls.read_snapshot())
        self.assertEqual(batch._state.phase, "failed")
        self.assertFalse(any(call[0] == "move" for call in self.magnet.calls))
        self.assertIn("sensor status is invalid", errors[-1])

    def test_uncommissioned_thermal_config_is_rejected_at_preflight(self):
        evaluator = ThermalSafetyEvaluator(LakeShore335Config(enabled=False))
        batch = GateScanFieldBatch(self.magnet, self.tab, thermal_safety=evaluator)
        batch._on_snapshot(_snapshot(0.0))
        errors = []
        batch.error.connect(errors.append)

        with patch("app.engine.gate_scan_field_batch.QtWidgets.QMessageBox.question", return_value=QtWidgets.QMessageBox.StandardButton.Yes):
            self.assertFalse(batch.start("0.25"))

        self.assertFalse(any(call[0] == "move" for call in self.magnet.calls))
        self.assertIn("not commissioned", errors[-1])

    def test_non_time_cooldown_hold_is_rejected_at_preflight(self):
        config = LakeShore335Config(
            enabled=True, verified_channel_mapping=True,
            sample_warning_temperature_k=5, sample_trip_temperature_k=6,
            sample_recovery_temperature_k=4, reservoir_warning_temperature_k=5,
            reservoir_trip_temperature_k=6, reservoir_recovery_temperature_k=4,
            required_stable_recovery_dwell_s=30,
            minimum_interval_between_heater_activations_s=0,
        )
        evaluator = ThermalSafetyEvaluator(config)
        ls = MockLakeShore335Adapter(sample_temperature_k=4.5, reservoir_temperature_k=3)
        decision = evaluator.evaluate(ls.read_snapshot())
        self.assertIsNone(decision.block_code)
        batch = GateScanFieldBatch(self.magnet, self.tab, thermal_safety=evaluator)
        batch._on_snapshot(_snapshot(0.0))
        errors = []
        batch.error.connect(errors.append)

        with patch("app.engine.gate_scan_field_batch.QtWidgets.QMessageBox.question", return_value=QtWidgets.QMessageBox.StandardButton.Yes):
            self.assertFalse(batch.start("0.25"))

        self.assertFalse(any(call[0] == "move" for call in self.magnet.calls))
        self.assertIn("not reached recovery thresholds", errors[-1])

    def test_successful_move_enters_thermal_wait_before_scan(self):
        config = LakeShore335Config(
            enabled=True, verified_channel_mapping=True,
            sample_warning_temperature_k=5, sample_trip_temperature_k=6,
            sample_recovery_temperature_k=4, reservoir_warning_temperature_k=5,
            reservoir_trip_temperature_k=6, reservoir_recovery_temperature_k=4,
            required_stable_recovery_dwell_s=0,
            minimum_interval_between_heater_activations_s=999,
        )
        evaluator = ThermalSafetyEvaluator(config)
        ls = MockLakeShore335Adapter(sample_temperature_k=3, reservoir_temperature_k=3)
        evaluator.evaluate(ls.read_snapshot())
        batch = GateScanFieldBatch(self.magnet, self.tab, thermal_safety=evaluator)
        batch._on_snapshot(_snapshot(0.0))
        with patch("app.engine.gate_scan_field_batch.QtWidgets.QMessageBox.question", return_value=QtWidgets.QMessageBox.StandardButton.Yes):
            self.assertTrue(batch.start("0.25,-0.5"))
        move = [call for call in self.magnet.calls if call[0] == "move"][0]
        batch._on_move_result({"request_id": move[3], "success": True, "snapshot": _snapshot(0.25), "audit": {}})
        self.assertEqual(batch._state.phase, "thermal_wait")
        self.assertEqual(len([call for call in self.magnet.calls if call[0] == "move"]), 1)
        self.assertFalse(any(call[0] == "start" for call in self.tab.calls))

    def test_temperature_gate_remains_fail_closed_before_next_field_move(self):
        config = LakeShore335Config(
            enabled=True, verified_channel_mapping=True,
            sample_warning_temperature_k=5, sample_trip_temperature_k=6,
            sample_recovery_temperature_k=4, reservoir_warning_temperature_k=5,
            reservoir_trip_temperature_k=6, reservoir_recovery_temperature_k=4,
            required_stable_recovery_dwell_s=0,
            minimum_interval_between_heater_activations_s=0,
        )
        evaluator = ThermalSafetyEvaluator(config)
        ls = MockLakeShore335Adapter(sample_temperature_k=3, reservoir_temperature_k=3)
        evaluator.evaluate(ls.read_snapshot())
        batch = GateScanFieldBatch(self.magnet, self.tab, thermal_safety=evaluator)
        batch._on_snapshot(_snapshot(0.0))
        with patch("app.engine.gate_scan_field_batch.QtWidgets.QMessageBox.question", return_value=QtWidgets.QMessageBox.StandardButton.Yes):
            self.assertTrue(batch.start("0.25,-0.5"))
        first = [call for call in self.magnet.calls if call[0] == "move"][0]
        batch._on_move_result({"request_id": first[3], "success": True, "snapshot": _snapshot(0.25), "audit": {}})
        self.assertEqual(batch._state.phase, "measuring")
        ls.set_readings(4.5, 3.0)
        batch.on_lakeshore_snapshot(ls.read_snapshot())
        self.tab.batch_run_terminal.emit("finished", "field.csv")
        self.assertEqual(batch._state.phase, "thermal_wait")
        self.assertEqual([call[1] for call in self.magnet.calls if call[0] == "move"], [0.25])
        ls.set_readings(3.0, 3.0)
        batch.on_lakeshore_snapshot(ls.read_snapshot())
        self.assertEqual([call[1] for call in self.magnet.calls if call[0] == "move"], [0.25, -0.5])

    def test_recovered_thermal_state_permits_next_field(self):
        config = LakeShore335Config(
            enabled=True, verified_channel_mapping=True,
            sample_warning_temperature_k=5, sample_trip_temperature_k=6,
            sample_recovery_temperature_k=4, reservoir_warning_temperature_k=5,
            reservoir_trip_temperature_k=6, reservoir_recovery_temperature_k=4,
            required_stable_recovery_dwell_s=0,
            minimum_interval_between_heater_activations_s=0,
        )
        evaluator = ThermalSafetyEvaluator(config)
        ls = MockLakeShore335Adapter(sample_temperature_k=3, reservoir_temperature_k=3)
        evaluator.evaluate(ls.read_snapshot())
        batch = GateScanFieldBatch(self.magnet, self.tab, thermal_safety=evaluator)
        batch._on_snapshot(_snapshot(0.0))
        with patch("app.engine.gate_scan_field_batch.QtWidgets.QMessageBox.question", return_value=QtWidgets.QMessageBox.StandardButton.Yes):
            self.assertTrue(batch.start("0.25,-0.5"))
        first = [call for call in self.magnet.calls if call[0] == "move"][0]
        ls.set_readings(4.5, 3.0)
        batch.on_lakeshore_snapshot(ls.read_snapshot())
        batch._on_move_result({"request_id": first[3], "success": True, "snapshot": _snapshot(0.25), "audit": {}})
        self.assertEqual(batch._state.phase, "thermal_wait")
        self.assertFalse(any(call[0] == "start" for call in self.tab.calls))
        ls.set_readings(3.0, 3.0)
        batch.on_lakeshore_snapshot(ls.read_snapshot())
        self.assertEqual(batch._state.phase, "measuring")
        self.assertEqual(len([call for call in self.tab.calls if call[0] == "start"]), 1)
        self.tab.batch_run_terminal.emit("finished", "field.csv")
        self.assertEqual([call[1] for call in self.magnet.calls if call[0] == "move"], [0.25, -0.5])

    def test_cooldown_timeout_fails_batch(self):
        config = LakeShore335Config(
            enabled=True, verified_channel_mapping=True,
            sample_warning_temperature_k=5, sample_trip_temperature_k=6,
            sample_recovery_temperature_k=4, reservoir_warning_temperature_k=5,
            reservoir_trip_temperature_k=6, reservoir_recovery_temperature_k=4,
            required_stable_recovery_dwell_s=0,
            minimum_interval_between_heater_activations_s=999,
            maximum_cooldown_wait_timeout_s=0.1,
        )
        evaluator = ThermalSafetyEvaluator(config)
        ls = MockLakeShore335Adapter(sample_temperature_k=3, reservoir_temperature_k=3)
        evaluator.evaluate(ls.read_snapshot())
        batch = GateScanFieldBatch(self.magnet, self.tab, thermal_safety=evaluator)
        batch._on_snapshot(_snapshot(0.0))
        with patch("app.engine.gate_scan_field_batch.QtWidgets.QMessageBox.question", return_value=QtWidgets.QMessageBox.StandardButton.Yes):
            self.assertTrue(batch.start("0.25,-0.5"))
        first = [call for call in self.magnet.calls if call[0] == "move"][0]
        batch._on_move_result({"request_id": first[3], "success": True, "snapshot": _snapshot(0.25), "audit": {}})
        self.assertEqual(batch._state.phase, "thermal_wait")
        self.assertFalse(any(call[0] == "start" for call in self.tab.calls))
        batch._cooldown_started_at = time.monotonic() - 1
        batch._poll_cooldown()
        self.assertEqual(batch._state.phase, "failed")

    def test_invalid_later_condition_fails_before_any_magnet_use(self):
        self.tab = _MultiConditionTab(self.tmp.name)
        # Mark the later frozen request invalid; the validator must inspect all
        # conditions before APS100 reservation or movement.
        def requests():
            first = deepcopy(self.tab.params)
            second = deepcopy(self.tab.params)
            second.raw_vtg_active = second.raw_vbg_active = second.raw_vds_active = False
            return [(first, ("daq",), None, "valid"), (second, ("daq",), None, "invalid")]
        self.tab.capture_field_batch_requests = requests
        self.batch = GateScanFieldBatch(self.magnet, self.tab)
        self.batch.set_review_validator(lambda: True)
        self.batch._on_snapshot(_snapshot(0.0))
        errors = []
        self.batch.error.connect(errors.append)
        self.assertFalse(self.batch.start("0.25"))
        self.assertTrue(errors)
        self.assertFalse(any(call[0] in {"acquire", "move"} for call in self.magnet.calls))

    def test_terminal_checkpoint_records_series_identity_and_status(self):
        self.tab = _MultiConditionTab(self.tmp.name)
        self.batch = GateScanFieldBatch(self.magnet, self.tab)
        self.batch.set_review_validator(lambda: True)
        self.batch._on_snapshot(_snapshot(0.0))
        with patch("app.engine.gate_scan_field_batch.QtWidgets.QMessageBox.question", return_value=QtWidgets.QMessageBox.StandardButton.Yes):
            self.assertTrue(self.batch.start("0.25,-0.5"))
        series_id = self.batch._series_id
        for _ in range(4):
            move = [call for call in self.magnet.calls if call[0] == "move"][-1]
            self.magnet.safe_move_result.emit({"request_id": move[3], "success": True, "snapshot": _snapshot(move[1]), "audit": {}})
            self.tab.batch_run_terminal.emit("finished", f"{len(self.tab.calls)}.csv")
        with open(self.batch._checkpoint_path, encoding="utf-8") as handle:
            checkpoint = json.load(handle)
        self.assertEqual(checkpoint["status"], "complete")
        self.assertEqual(checkpoint["next_job_index"], 4)
        self.assertEqual(checkpoint["batch_id"], series_id)
        self.assertEqual(checkpoint["condition_count"], 2)

    def test_activity_context_and_persistent_series_log(self):
        events = []
        self.batch.activity.connect(events.append)
        with patch("app.engine.gate_scan_field_batch.QtWidgets.QMessageBox.question", return_value=QtWidgets.QMessageBox.StandardButton.Yes):
            self.assertTrue(self.batch.start("0.25"))
        path = self.batch._batch_log_path
        contextual = [event for event in events if event["context"].get("target_t") is not None]
        self.assertTrue(contextual)
        context = contextual[0]["context"]
        self.assertEqual(context["condition_index"], 1)
        self.assertEqual(context["target_t"], 0.25)
        self.assertEqual(context["job_index"], 1)
        move = [call for call in self.magnet.calls if call[0] == "move"][0]
        self.magnet.safe_move_result.emit({
            "request_id": move[3], "success": True,
            "snapshot": _snapshot(0.25), "audit": {},
        })
        self.tab.batch_run_terminal.emit("finished", "field.csv")
        with open(path, encoding="utf-8") as handle:
            log = handle.read()
        self.assertIn("B-field Gate Scan series", log)
        self.assertIn("target +0.250000 T", log)
        self.assertIn("B-field Gate Scan series complete", log)

    def test_cleanup_keeps_completed_log_immutable_after_late_fault(self):
        with patch("app.engine.gate_scan_field_batch.QtWidgets.QMessageBox.question", return_value=QtWidgets.QMessageBox.StandardButton.Yes):
            self.assertTrue(self.batch.start("0.25"))
        move = [call for call in self.magnet.calls if call[0] == "move"][0]
        self.magnet.safe_move_result.emit({
            "request_id": move[3], "success": True,
            "snapshot": _snapshot(0.25), "audit": {},
        })
        self.tab.batch_run_terminal.emit("finished", "field.csv")
        path = os.path.join(
            self.tmp.name,
            "device_gate_scan_raw_keithley_g3_forward_bfield_batch_log.txt",
        )
        with open(path, encoding="utf-8") as handle:
            before = handle.read()
        self.assertTrue(os.path.exists(path))
        self.assertIsNone(self.batch._batch_log_path)
        self.magnet.fault.emit("late fault")
        with open(path, encoding="utf-8") as handle:
            self.assertEqual(handle.read(), before)


if __name__ == "__main__":
    unittest.main()
