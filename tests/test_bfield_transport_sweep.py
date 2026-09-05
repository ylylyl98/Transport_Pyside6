import csv
import json
import os
import tempfile
import unittest
import time
from unittest.mock import patch
from types import SimpleNamespace
from PyQt6 import QtCore

from app.engine.bfield_transport_sweep import (
    build_transport_output_paths,
    BFieldTransportSafetyError,
    BFieldTransportSweep,
    COIL_CONSTANT_T_PER_A,
    MAX_RATE_T_PER_MIN,
    TransportCsvWriter,
    build_trajectory,
    enabled_conditions,
    rate_t_per_min_to_a_per_s,
    validate_setup,
    validate_voltage_margin,
    write_series_manifest,
    TransportSweepPlan,
    estimate_transport_times,
    normalize_cooldown_policy,
    transport_output_summary_parts,
    validate_field_bounds,
)
from app.engine.bfield_transport_controller import BFieldTransportController
from app.models import BFieldTransportCondition, BFieldTransportParams, SaveRoot
from app.run_output import build_planned_output
from app.signal_chain import SignalChainSnapshot
from utils.config import cfg


class BFieldTransportSweepTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6 import QtWidgets
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def test_transport_polling_is_restarted_only_after_sweep_acceptance(self):
        class Magnet(QtCore.QObject):
            transport_config_result = QtCore.pyqtSignal(object)
            safe_move_result = QtCore.pyqtSignal(object)
            transport_sweep_result = QtCore.pyqtSignal(object)
            snapshot_updated = QtCore.pyqtSignal(object)
            fault = QtCore.pyqtSignal(str)

            def __init__(self):
                super().__init__()
                self.polling = []
                self.sweep_targets = []
                self.refreshes = 0

            def set_polling_enabled(self, enabled):
                self.polling.append(bool(enabled))

            def start_transport_sweep(self, target):
                self.sweep_targets.append(float(target))

            def refresh_snapshot(self):
                self.refreshes += 1

        magnet = Magnet()
        controller = BFieldTransportController(
            magnet, SimpleNamespace(), SimpleNamespace(), thermal_safety=None
        )
        controller.plan = TransportSweepPlan.from_params(BFieldTransportParams())
        controller._active = True
        controller._condition_index = 0
        controller._begin_leg(0, controller.plan.params.stop_field_t)
        self.assertEqual(magnet.polling, [])
        controller._on_sweep_started({"success": True})
        self.assertEqual(magnet.polling, [True])
        self.assertEqual(magnet.refreshes, 1)
        self.assertEqual(controller._measurement_phase, "sweeping")
        controller._cleanup_in_progress = True
        controller._set_transport_polling(False)
        self.assertEqual(magnet.polling, [True, False])

    def test_endpoint_snapshot_is_recorded_once_before_transition(self):
        class Magnet(QtCore.QObject):
            transport_config_result = QtCore.pyqtSignal(object)
            safe_move_result = QtCore.pyqtSignal(object)
            transport_sweep_result = QtCore.pyqtSignal(object)
            snapshot_updated = QtCore.pyqtSignal(object)
            fault = QtCore.pyqtSignal(str)
            operation_finished = QtCore.pyqtSignal(str)

            def pause(self):
                pass

            def start_transport_sweep(self, _target):
                pass

        thermal = SimpleNamespace(
            is_armed=True,
            latest_snapshot=object(),
            evaluate=lambda _snapshot: SimpleNamespace(magnet_permission=True),
        )
        controller = BFieldTransportController(
            Magnet(), SimpleNamespace(), SimpleNamespace(), thermal_safety=thermal
        )
        controller.plan = TransportSweepPlan.from_params(
            BFieldTransportParams(start_field_t=-0.1, stop_field_t=0.1)
        )
        controller._active = True
        controller._measurement_phase = "sweeping"
        controller._target = 0.1
        controller._leg_index = 0
        recorded = []
        controller._record_snapshot = recorded.append
        snapshot = SimpleNamespace(
            reading_age_s=0.0,
            field_t=0.1,
            heater_on=True,
            magnet_voltage_v=0.0,
            voltage_limit_v=3.0,
            status=SimpleNamespace(
                quench=False, power_module_failure=False, faulted=False,
                sweep_active=True,
            ),
        )
        controller._on_snapshot(snapshot)
        snapshot.status.sweep_active = False
        controller._on_snapshot(snapshot)
        controller._on_snapshot(snapshot)
        self.assertEqual(len(recorded), 1)
        self.assertTrue(controller._transition_pending)

    @patch.object(cfg.mcd, "transport_endpoint_settling_enabled", True)
    def test_active_at_limit_requires_stable_reads_before_pause(self):
        class Magnet(QtCore.QObject):
            transport_config_result = QtCore.pyqtSignal(object)
            safe_move_result = QtCore.pyqtSignal(object)
            transport_sweep_result = QtCore.pyqtSignal(object)
            snapshot_updated = QtCore.pyqtSignal(object)
            fault = QtCore.pyqtSignal(str)
            operation_finished = QtCore.pyqtSignal(str)

            def __init__(self):
                super().__init__()
                self.pause_calls = 0

            def pause(self):
                self.pause_calls += 1

        thermal = SimpleNamespace(
            is_armed=True,
            latest_snapshot=object(),
            evaluate=lambda _snapshot: SimpleNamespace(magnet_permission=True),
        )
        magnet = Magnet()
        controller = BFieldTransportController(
            magnet, SimpleNamespace(), SimpleNamespace(), thermal_safety=thermal
        )
        controller.plan = TransportSweepPlan.from_params(
            BFieldTransportParams(start_field_t=-0.01, stop_field_t=0.01)
        )
        controller._active = True
        controller._measurement_phase = "sweeping"
        controller._target = 0.01
        controller._leg_index = 0
        controller._endpoint_tolerance = controller._endpoint_tolerance_t()
        recorded = []
        controller._record_snapshot = lambda snapshot: recorded.append(float(snapshot.field_t))
        def snapshot(field, active=True):
            return SimpleNamespace(
                reading_age_s=0.0, field_t=field, heater_on=True,
                magnet_voltage_v=0.0, voltage_limit_v=3.0,
                status=SimpleNamespace(
                    quench=False, power_module_failure=False, faulted=False,
                    sweep_active=active,
                ),
            )
        for _ in range(2):
            controller._on_snapshot(snapshot(0.01))
            self.assertEqual(magnet.pause_calls, 0)
        controller._on_snapshot(snapshot(0.01))
        self.assertEqual(magnet.pause_calls, 1)
        self.assertEqual(controller._measurement_phase, "endpoint_hold")
        self.assertEqual(controller._leg_index, 0)

    def test_short_span_endpoint_tolerance_rejects_early_and_accepts_overshoot(self):
        class Magnet(QtCore.QObject):
            transport_config_result = QtCore.pyqtSignal(object)
            safe_move_result = QtCore.pyqtSignal(object)
            transport_sweep_result = QtCore.pyqtSignal(object)
            snapshot_updated = QtCore.pyqtSignal(object)
            fault = QtCore.pyqtSignal(str)
            operation_finished = QtCore.pyqtSignal(str)

            def __init__(self):
                super().__init__()
                self.pause_calls = 0

            def pause(self):
                self.pause_calls += 1

        thermal = SimpleNamespace(
            is_armed=True,
            latest_snapshot=object(),
            evaluate=lambda _snapshot: SimpleNamespace(magnet_permission=True),
        )
        magnet = Magnet()
        controller = BFieldTransportController(
            magnet, SimpleNamespace(), SimpleNamespace(), thermal_safety=thermal
        )
        controller.plan = TransportSweepPlan.from_params(
            BFieldTransportParams(start_field_t=-0.01, stop_field_t=0.01)
        )
        controller._active = True
        controller._measurement_phase = "sweeping"
        controller._target = 0.01
        controller._leg_index = 0
        controller._endpoint_tolerance = controller._endpoint_tolerance_t()
        recorded = []
        controller._record_snapshot = lambda snapshot: recorded.append(float(snapshot.field_t))
        def snapshot(field):
            return SimpleNamespace(
                reading_age_s=0.0, field_t=field, heater_on=True,
                magnet_voltage_v=0.0, voltage_limit_v=3.0,
                status=SimpleNamespace(
                    quench=False, power_module_failure=False, faulted=False,
                    sweep_active=True,
                ),
            )
        for _ in range(4):
            controller._on_snapshot(snapshot(0.0092))
        self.assertEqual(magnet.pause_calls, 0)
        self.assertEqual(controller._measurement_phase, "sweeping")
        controller._on_snapshot(snapshot(0.0102))
        for _ in range(2):
            controller._on_snapshot(snapshot(0.0100))
        self.assertEqual(magnet.pause_calls, 1)
        self.assertEqual(controller._measurement_phase, "endpoint_hold")
        self.assertAlmostEqual(controller._position_tolerance_t(), 0.0002)
        endpoint_rows = [field for field in recorded if abs(field - 0.0100) <= controller._endpoint_tolerance + 1e-9]
        self.assertEqual(len(endpoint_rows), 1)
        self.assertAlmostEqual(endpoint_rows[0], 0.0102)

    def test_endpoint_pause_acknowledgement_starts_reverse_leg(self):
        class Magnet(QtCore.QObject):
            transport_config_result = QtCore.pyqtSignal(object)
            safe_move_result = QtCore.pyqtSignal(object)
            transport_sweep_result = QtCore.pyqtSignal(object)
            snapshot_updated = QtCore.pyqtSignal(object)
            fault = QtCore.pyqtSignal(str)
            operation_finished = QtCore.pyqtSignal(str)

            def __init__(self):
                super().__init__()
                self.pause_calls = 0
                self.sweep_targets = []

            def pause(self):
                self.pause_calls += 1

            def start_transport_sweep(self, target):
                self.sweep_targets.append(float(target))

        thermal = SimpleNamespace(
            is_armed=True,
            latest_snapshot=object(),
            evaluate=lambda _snapshot: SimpleNamespace(magnet_permission=True),
        )
        magnet = Magnet()
        controller = BFieldTransportController(
            magnet, SimpleNamespace(), SimpleNamespace(), thermal_safety=thermal
        )
        controller.plan = TransportSweepPlan.from_params(
            BFieldTransportParams(start_field_t=-0.01, stop_field_t=0.01)
        )
        controller._active = True
        controller._measurement_phase = "sweeping"
        controller._target = 0.01
        controller._leg_index = 0
        controller._endpoint_tolerance = controller._endpoint_tolerance_t()
        controller._record_snapshot = lambda _snapshot: None
        snapshot = SimpleNamespace(
            reading_age_s=0.0, field_t=0.01, heater_on=True,
            magnet_voltage_v=0.0, voltage_limit_v=3.0,
            status=SimpleNamespace(
                quench=False, power_module_failure=False, faulted=False,
                sweep_active=False,
            ),
        )
        controller._on_snapshot(snapshot)
        self.assertEqual(magnet.pause_calls, 1)
        self.assertEqual(magnet.sweep_targets, [])
        magnet.operation_finished.emit("pause")
        self.assertEqual(magnet.sweep_targets, [-0.01])
        self.assertEqual(controller._leg_index, 1)
        self.assertEqual(controller._measurement_phase, "starting_sweep")

    def test_one_way_endpoint_keeps_forward_runtime_target_until_cleanup(self):
        class Magnet(QtCore.QObject):
            transport_config_result = QtCore.pyqtSignal(object)
            safe_move_result = QtCore.pyqtSignal(object)
            transport_sweep_result = QtCore.pyqtSignal(object)
            snapshot_updated = QtCore.pyqtSignal(object)
            fault = QtCore.pyqtSignal(str)
            operation_finished = QtCore.pyqtSignal(str)

            def pause(self):
                pass

        thermal = SimpleNamespace(
            is_armed=True,
            latest_snapshot=object(),
            evaluate=lambda _snapshot: SimpleNamespace(magnet_permission=True),
        )
        controller = BFieldTransportController(
            Magnet(), SimpleNamespace(), SimpleNamespace(), thermal_safety=thermal
        )
        with tempfile.TemporaryDirectory() as directory:
            controller.plan = TransportSweepPlan.from_params(
                BFieldTransportParams(
                    start_field_t=-0.1, stop_field_t=0.1, round_trip=False,
                )
            )
            controller._active = True
            controller._measurement_phase = "sweeping"
            controller._target = 0.1
            controller._leg_index = 0
            controller._record_snapshot = lambda _snapshot: None
            controller._checkpoint = os.path.join(directory, "checkpoint.json")
            snapshot = SimpleNamespace(
                reading_age_s=0.0,
                field_t=0.1,
                heater_on=True,
                magnet_voltage_v=0.0,
                voltage_limit_v=3.0,
                status=SimpleNamespace(
                    quench=False, power_module_failure=False, faulted=False,
                    sweep_active=False,
                ),
            )
            for _ in range(3):
                controller._on_snapshot(snapshot)
            self.assertEqual(controller._leg_index, 0)
            self.assertEqual(controller._target, 0.1)
            self.assertEqual(controller._transition_next, "next")
            with open(controller._checkpoint, encoding="utf-8") as handle:
                runtime = json.load(handle)["runtime"]
            self.assertEqual(runtime["leg_index"], 0)
            self.assertAlmostEqual(runtime["target_field_t"], 0.1)

    def test_telemetry_watchdog_holds_active_leg(self):
        class Magnet(QtCore.QObject):
            transport_config_result = QtCore.pyqtSignal(object)
            safe_move_result = QtCore.pyqtSignal(object)
            transport_sweep_result = QtCore.pyqtSignal(object)
            snapshot_updated = QtCore.pyqtSignal(object)
            fault = QtCore.pyqtSignal(str)

        controller = BFieldTransportController(
            Magnet(), SimpleNamespace(), SimpleNamespace(), thermal_safety=None
        )
        controller._active = True
        controller._cleanup_in_progress = False
        controller._measurement_phase = "sweeping"
        controller._leg_started_monotonic = 0.0
        controller._last_telemetry_monotonic = 0.0
        controller._last_progress_monotonic = 0.0
        controller._failures = []
        controller._fail = lambda message: controller._failures.append(message)
        holds = []
        controller._begin_monitor_hold = holds.append
        with patch("app.engine.bfield_transport_controller.time.monotonic", return_value=200.0):
            with patch.object(cfg.mcd, "sweep_progress_timeout_s", 1.0):
                controller._on_telemetry_watchdog()
        self.assertEqual(controller._failures, [])
        self.assertIn("telemetry watchdog timeout", holds[0])

    def test_endpoint_oscillation_has_bounded_stability_grace(self):
        controller = BFieldTransportController.__new__(BFieldTransportController)
        controller._active = True
        controller._cleanup_in_progress = False
        controller._measurement_phase = "sweeping"
        controller._leg_started_monotonic = 0.0
        controller._last_telemetry_monotonic = 199.5
        # Ordinary progress may still be refreshed by readings outside the
        # endpoint window; the endpoint grace must remain independently bound.
        controller._last_progress_monotonic = 199.9
        controller._endpoint_candidate_started = 0.0
        controller._progress_timeout_s = 20.0
        controller._leg_timeout_s = 3600.0
        controller._leg_expected_duration_s = 0.0
        controller._failures = []
        controller._fail = lambda message: controller._failures.append(message)
        # Fresh telemetry may take longer than the old two-second allowance.
        for now in (2.1, 10.0, 60.0, 120.0):
            controller._last_telemetry_monotonic = now
            with patch("app.engine.bfield_transport_controller.time.monotonic", return_value=now):
                controller._on_telemetry_watchdog()
            self.assertEqual(controller._failures, [])
        controller._last_telemetry_monotonic = 199.5
        with patch("app.engine.bfield_transport_controller.time.monotonic", return_value=200.0):
            controller._on_telemetry_watchdog()
        self.assertEqual(len(controller._failures), 1)
        self.assertIn("endpoint did not stabilize", controller._failures[0])

    def test_endpoint_arrival_requires_commanded_approach_direction(self):
        controller = BFieldTransportController.__new__(BFieldTransportController)
        controller.plan = TransportSweepPlan.from_params(
            BFieldTransportParams(start_field_t=-0.1, stop_field_t=0.1)
        )
        for leg, target, previous, measured, expected in (
            (0, 0.1, 0.097, 0.098, True),
            (0, 0.1, 0.102, 0.099, False),
            (1, -0.1, -0.097, -0.098, True),
            (1, -0.1, -0.102, -0.099, False),
        ):
            with self.subTest(leg=leg, previous=previous):
                controller._leg_index = leg
                controller._target = target
                controller._last_progress_field = previous
                self.assertEqual(controller._endpoint_approach_ready(measured), expected)

    def test_failed_endpoint_pause_does_not_start_return_leg(self):
        controller = BFieldTransportController.__new__(BFieldTransportController)
        controller._cleanup_in_progress = False
        controller._active = True
        controller._thermal_pause_pending = False
        controller._persistent_row_transition = False
        controller._transition_pending = True
        controller._transition_next = "return"
        failures, legs = [], []
        controller._fail = failures.append
        controller._begin_leg = lambda *args: legs.append(args)
        controller._on_magnet_operation("failed:pause")
        self.assertEqual(failures, ["APS100 operation failed: pause"])
        self.assertEqual(legs, [])

    def test_optional_settling_restarts_reads_but_not_timeout_after_excursion(self):
        controller = BFieldTransportController.__new__(BFieldTransportController)
        controller._target = 0.1
        controller._endpoint_tolerance = 0.002
        controller._endpoint_stable_reads = 0
        controller._endpoint_last_field = None
        controller._endpoint_candidate_started = None
        controller._endpoint_hold_started = None
        for field, stamp, expected in (
            (0.099, 0.0, False), (0.103, 1.1, False),
            (0.100, 2.2, False), (0.100, 3.3, False), (0.100, 4.4, True),
        ):
            self.assertEqual(controller._endpoint_stability_ready(field, True, stamp), expected)
        self.assertEqual(controller._endpoint_candidate_started, 0.0)

    def test_watchdog_uses_derived_duration_for_slow_long_valid_leg(self):
        class Magnet(QtCore.QObject):
            transport_config_result = QtCore.pyqtSignal(object)
            safe_move_result = QtCore.pyqtSignal(object)
            transport_sweep_result = QtCore.pyqtSignal(object)
            snapshot_updated = QtCore.pyqtSignal(object)
            fault = QtCore.pyqtSignal(str)

            def set_polling_enabled(self, _enabled):
                pass

            def refresh_snapshot(self):
                pass

            def start_transport_sweep(self, _target):
                pass

        controller = BFieldTransportController(
            Magnet(), SimpleNamespace(), SimpleNamespace(), thermal_safety=None
        )
        controller.plan = TransportSweepPlan.from_params(BFieldTransportParams(
            start_field_t=-6.0, stop_field_t=6.0, rate_t_per_min=1e-6,
        ))
        controller._active = True
        controller._begin_leg(0, 6.0)
        controller._on_sweep_started({"success": True})
        expected = 12.0 / 1e-6 * 60.0
        self.assertAlmostEqual(controller._leg_expected_duration_s, expected)
        self.assertGreater(controller._progress_timeout_s, expected)

    def test_cleanup_error_before_failed_pause_keeps_persistent_transition_owned_by_ack(self):
        controller = BFieldTransportController.__new__(BFieldTransportController)
        controller._cleanup_in_progress = True
        controller._cleanup_waiting = "pause"
        controller._cleanup_failures = []
        controller._log_path = None
        transitions = []
        controller._begin_persistent_cleanup = lambda: transitions.append("persistent")

        controller._on_magnet_error("APS100 pause rejected")
        self.assertEqual(transitions, [])
        self.assertEqual(len(controller._cleanup_failures), 1)

        controller._on_magnet_operation("failed:pause")
        self.assertEqual(transitions, ["persistent"])

    def test_sync_persistent_cleanup_failure_retains_ownership_and_skips_restore(self):
        class Magnet(QtCore.QObject):
            transport_config_result = QtCore.pyqtSignal(object)
            safe_move_result = QtCore.pyqtSignal(object)
            transport_sweep_result = QtCore.pyqtSignal(object)
            snapshot_updated = QtCore.pyqtSignal(object)
            fault = QtCore.pyqtSignal(str)

            def __init__(self):
                super().__init__()
                self.events = []

            def pause(self):
                self.events.append("pause")

            def enter_persistent_mode(self, **_kwargs):
                self.events.append("persistent")
                raise RuntimeError("PSHTR OFF confirmation timed out")

        class Devices:
            def __init__(self):
                self.released = False

            def get_session(self, _name):
                return None

            def release(self, _claimed):
                self.released = True

        class Tab:
            def set_sweep_locked(self, _locked):
                pass

        controller = BFieldTransportController(
            Magnet(), Tab(), Devices(), thermal_safety=None
        )
        controller._active = True
        controller._claimed = ["aps100"]
        controller._exclusive_acquired = True
        controller._cleanup_in_progress = True
        controller._cleanup_waiting = "persistent"
        controller._cleanup_failures = []
        states = []
        controller.state_changed.connect(lambda phase, detail: states.append((phase, detail)))
        restored = []
        controller._request_restore = lambda: restored.append(True)
        self.assertFalse(hasattr(controller.magnet, "operation_finished"))

        controller._begin_persistent_cleanup()

        self.assertEqual(controller.magnet.events, ["persistent"])
        self.assertTrue(controller._cleanup_in_progress)
        self.assertEqual(controller._cleanup_waiting, "persistent")
        self.assertTrue(controller._exclusive_acquired)
        self.assertFalse(controller.device_manager.released)
        self.assertFalse(restored)
        self.assertTrue(any("OFF confirmation timed out" in item for item in controller._cleanup_failures))
        self.assertTrue(any(phase == "cleanup_overdue" and "ownership retained" in detail for phase, detail in states))

    def test_positioning_snapshot_is_not_recorded_before_sweep_target_exists(self):
        class Magnet(QtCore.QObject):
            transport_config_result = QtCore.pyqtSignal(object)
            safe_move_result = QtCore.pyqtSignal(object)
            transport_sweep_result = QtCore.pyqtSignal(object)
            snapshot_updated = QtCore.pyqtSignal(object)
            fault = QtCore.pyqtSignal(str)

        thermal = SimpleNamespace(
            is_armed=True,
            latest_snapshot=object(),
            evaluate=lambda _snapshot: SimpleNamespace(magnet_permission=True),
        )
        controller = BFieldTransportController(
            Magnet(), SimpleNamespace(), SimpleNamespace(), thermal_safety=thermal
        )
        controller.plan = TransportSweepPlan.from_params(BFieldTransportParams())
        controller._active = True
        controller._target = None
        recorded = []
        controller._record_snapshot = recorded.append

        controller._on_snapshot(SimpleNamespace(
            reading_age_s=0.0,
            field_t=0.0,
            heater_on=True,
            magnet_voltage_v=0.0,
            voltage_limit_v=3.0,
            status=SimpleNamespace(
                quench=False,
                power_module_failure=False,
                faulted=False,
                sweep_active=False,
            ),
        ))

        self.assertEqual(recorded, [])

    def test_transport_output_names_include_sweep_signal_chain_and_condition_names(self):
        params = BFieldTransportParams(
            start_field_t=-0.5,
            stop_field_t=0.5,
            rate_t_per_min=0.1,
            round_trip=True,
            conditions=[
                BFieldTransportCondition(name="Neutral point"),
                BFieldTransportCondition(name="High / doping"),
            ],
        )
        signal_chain = SignalChainSnapshot(1000.0, 0.02, 100e-9)
        parts = transport_output_summary_parts(params, signal_chain)
        self.assertEqual(parts[:4], ["B_-0.5to0.5T", "round_trip", "rate_0.1Tpermin", "2conditions"])
        self.assertEqual(parts[-3:], ["freq_1kHz", "lia_20mV", "preamp_100nA"])

        planned = build_planned_output(
            SaveRoot(base=".", user="operator", device_id="sample"),
            "bfield_transport",
            "transport",
            parts,
            run_id="run",
        )
        paths = build_transport_output_paths(planned, params.conditions)
        self.assertIn("B_-0.5to0.5T_round_trip_rate_0.1Tpermin_2conditions", planned.stem)
        self.assertTrue(paths.condition_csv_paths[0].endswith("_C01_Neutral_point.csv"))
        self.assertTrue(paths.condition_csv_paths[1].endswith("_C02_High_doping.csv"))
        self.assertIn(paths.manifest_path, paths.all_paths)

    def test_adaptive_is_default_and_policy_estimates_preserve_trajectory(self):
        self.assertEqual(BFieldTransportParams().cooldown_policy, "adaptive")
        self.assertEqual(normalize_cooldown_policy("Stay driven"), "stay_driven")
        round_trip = estimate_transport_times(-1, 1, 0.1, 2, round_trip=True)
        one_way = estimate_transport_times(-1, 1, 0.1, 2, round_trip=False)
        self.assertAlmostEqual(round_trip["sweep_s_per_row"], 2400.0)
        self.assertAlmostEqual(round_trip["batch_s"], 4801.0)
        self.assertLess(one_way["batch_s"], round_trip["batch_s"])
        persistent_rt = estimate_transport_times(
            -0.1, 0.1, 0.1, 2, round_trip=True,
            cooldown_policy="persistent_each_row", heater_cool_s=120,
            recovery_dwell_s=30, heater_warm_s=60,
        )
        self.assertAlmostEqual(persistent_rt["field_positioning_extra_s"], 120.0)

    def test_persistent_policy_reports_fixed_and_variable_recovery_time(self):
        estimate = estimate_transport_times(
            -1, 1, 0.1, 3, round_trip=False,
            cooldown_policy="persistent_each_row", heater_cool_s=120,
            recovery_dwell_s=30, heater_warm_s=60, lead_zero_rematch_s=None,
        )
        self.assertEqual(estimate["fixed_thermal_extra_s"], 420.0)
        self.assertEqual(estimate["field_positioning_extra_s"], 2400.0 * 2)
        self.assertTrue(estimate["thermal_recovery_variable"])

    def test_active_permission_fails_closed_when_lakeshore_is_lost(self):
        controller = BFieldTransportController.__new__(BFieldTransportController)
        controller.thermal_safety = None
        permitted, reason = controller._thermal_permission()
        self.assertFalse(permitted)
        self.assertIn("unavailable", reason)
        controller.thermal_safety = type("Thermal", (), {"is_armed": False})()
        permitted, reason = controller._thermal_permission()
        self.assertFalse(permitted)
        self.assertIn("commissioned", reason)
    def test_rate_cap_converts_to_commissioned_aps_rate(self):
        self.assertAlmostEqual(rate_t_per_min_to_a_per_s(MAX_RATE_T_PER_MIN), 0.03430, places=7)
        with self.assertRaises(BFieldTransportSafetyError):
            validate_setup(-1, 1, MAX_RATE_T_PER_MIN + 1e-4)

    def test_only_sweep_endpoints_are_limited_to_six_t(self):
        self.assertEqual(validate_setup(-6, 5, 0.1)["stop_field_t"], 5)
        with self.assertRaises(BFieldTransportSafetyError):
            validate_setup(-6.01, 1, 0.1)

    def test_sub_resolution_transport_span_is_rejected(self):
        with self.assertRaisesRegex(BFieldTransportSafetyError, "APS100 transport resolution"):
            validate_field_bounds(0.0, 50e-6)

    def test_round_trip_and_one_way_legs(self):
        self.assertEqual(build_trajectory(0.2, -0.4, True), ((0.2, "start"), (-0.4, "forward"), (0.2, "backward")))
        self.assertEqual(build_trajectory(0.2, -0.4, False), ((0.2, "start"), (-0.4, "forward")))

    def test_fixed_doping_efield_rows_resolve_gates(self):
        row = BFieldTransportCondition(name="n", doping=2, efield=0.4, ratio=2, ratio_target="Vbg")
        rows = enabled_conditions([row])
        self.assertEqual(len(rows), 1)
        self.assertAlmostEqual(row.vtg, 1.2)
        self.assertAlmostEqual(row.vbg, 0.4)

    def test_uncommissioned_voltage_is_not_guessed(self):
        self.assertIsNone(validate_voltage_margin(0.1, inductance_h=None, voltage_limit_v=None))
        self.assertAlmostEqual(validate_voltage_margin(0.1, inductance_h=10, voltage_limit_v=3), 0.0819887183, places=7)
        with self.assertRaises(BFieldTransportSafetyError):
            validate_voltage_margin(MAX_RATE_T_PER_MIN, inductance_h=100, voltage_limit_v=3)

    def test_csv_is_incremental_and_manifest_records_conditions(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "c.csv")
            condition = BFieldTransportCondition(name="fixed")
            with TransportCsvWriter(path, condition) as writer:
                writer.write({"Index": 0, "Direction": "forward", "B_measured_T": 0.25})
            with open(path, newline="", encoding="utf-8") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(rows[0]["Direction"], "forward")
            manifest = os.path.join(directory, "series.json")
            params = BFieldTransportParams(conditions=[condition])
            write_series_manifest(manifest, params=params, validation={"rate_a_per_s": 0.01})
            with open(manifest, encoding="utf-8") as handle:
                payload = json.load(handle)
            self.assertEqual(payload["schema"], "bfield_transport_manifest_v1")
            self.assertEqual(payload["conditions"][0]["name"], "fixed")

    def test_multi_condition_runner_holds_each_row_and_cleans_up(self):
        class Adapter:
            def __init__(self):
                self.events = []
                self.field = 0.0
            def safe_move_to_field(self, target, **_kwargs):
                self.events.append(("move", target))
                self.field = target
            def read_snapshot(self):
                return type("Snapshot", (), {"field_t": self.field})()
            def start_sweep_to(self, target):
                self.events.append(("sweep", target))
                self.field = target
            def wait_for_field(self, target, **_kwargs):
                self.field = target
                return self.field
            def pause(self, **_kwargs):
                self.events.append("pause")
            def enter_persistent_mode(self, **_kwargs):
                self.events.append("persistent")
        rows = [BFieldTransportCondition(name="a", doping=1), BFieldTransportCondition(name="b", efield=1)]
        params = BFieldTransportParams(start_field_t=-1, stop_field_t=1, round_trip=True, conditions=rows, final_mode="persistent")
        plan = TransportSweepPlan.from_params(params)
        applied, acquired = [], []
        runner = BFieldTransportSweep(Adapter(), plan, apply_condition=lambda c: applied.append(c.name), acquire=lambda c, d, s: acquired.append((c.name, d, s.field_t)))
        records = runner.run(persistent_field_confirmed=True)
        self.assertEqual(applied, ["a", "b"])
        self.assertEqual([r["direction"] for r in records], ["start", "forward", "backward"] * 2)
        self.assertEqual(acquired[2][2], -1)
        self.assertNotIn("persistent", runner.adapter.events)
        self.assertEqual(plan.params.final_mode, "driven")

    def test_runner_writes_rates_limits_restores_and_acquires_progress(self):
        class Adapter:
            def __init__(self):
                self.events, self.field = [], 0.0
                self.rates, self.limits = {0: 0.01}, (-2.0, 2.0)
            def get_rates(self): return dict(self.rates)
            def get_limits_t(self): return self.limits
            def set_rate_t_per_min(self, rate, **_kwargs):
                self.events.append(("rate", rate)); self.rates = {0: rate}; return self.rates
            def set_limits_t(self, low, high):
                self.events.append(("limits", low, high)); self.limits = (low, high); return self.limits
            def restore_rates(self, rates): self.events.append(("restore_rates", rates)); self.rates = rates
            def safe_move_to_field(self, target, **_kwargs): self.field = target
            def read_snapshot(self): return type("Snapshot", (), {"field_t": self.field, "status": type("Status", (), {})()})()
            def start_sweep_to(self, target): self.field = target
            def wait_for_field(self, target, **kwargs):
                for value in (self.field + (target - self.field) * 0.5, target):
                    self.field = value
                    kwargs["progress"](value)
                return self.field
            def pause(self, **_kwargs): pass
            def enter_persistent_mode(self, **_kwargs): pass
        params = BFieldTransportParams(start_field_t=0, stop_field_t=1, round_trip=False, rate_t_per_min=0.1)
        plan = TransportSweepPlan.from_params(params)
        samples = []
        adapter = Adapter()
        runner = BFieldTransportSweep(adapter, plan, acquire=lambda _c, direction, snapshot: samples.append((direction, snapshot.field_t)))
        runner.run(persistent_field_confirmed=True)
        self.assertTrue(any(e[0] == "rate" for e in adapter.events))
        self.assertIn(("limits", -6.0, 6.0), adapter.events)
        self.assertTrue(any(e[0] == "restore_rates" for e in adapter.events))
        self.assertGreaterEqual(len(samples), 3)  # start plus mid/end progress
        self.assertEqual(adapter.rates, {0: 0.01})

    def test_successful_driven_final_mode_leaves_heater_on(self):
        class Adapter:
            def __init__(self):
                self.events = []
                self.field = 0.0

            def safe_move_to_field(self, target, **kwargs):
                self.events.append(("move", target, kwargs))
                self.field = target

            def read_snapshot(self):
                return type("Snapshot", (), {"field_t": self.field})()

            def start_sweep_to(self, target):
                self.events.append(("sweep", target))

            def wait_for_field(self, target, **kwargs):
                self.field = target
                kwargs["progress"](target)
                return self.field

            def pause(self, **_kwargs):
                self.events.append("pause")

            def enter_persistent_mode(self, **_kwargs):
                self.events.append("persistent")

        params = BFieldTransportParams(
            start_field_t=0.0,
            stop_field_t=0.01,
            round_trip=True,
            conditions=[BFieldTransportCondition()],
        )
        adapter = Adapter()
        runner = BFieldTransportSweep(adapter, TransportSweepPlan.from_params(params))
        runner.run(persistent_field_confirmed=True)
        self.assertTrue(any(event[0] == "move" for event in adapter.events if isinstance(event, tuple)))
        self.assertNotIn("persistent", adapter.events)
        self.assertEqual(params.final_mode, "driven")
        self.assertEqual(adapter.field, params.start_field_t)
        self.assertEqual([event for event in adapter.events if isinstance(event, tuple) and event[0] == "sweep"], [("sweep", 0.01), ("sweep", 0.0)])

    def test_shutdown_after_driven_completion_waits_for_persistent_ack(self):
        class Magnet(QtCore.QObject):
            transport_config_result = QtCore.pyqtSignal(object)
            safe_move_result = QtCore.pyqtSignal(object)
            transport_sweep_result = QtCore.pyqtSignal(object)
            snapshot_updated = QtCore.pyqtSignal(object)
            fault = QtCore.pyqtSignal(str)
            operation_finished = QtCore.pyqtSignal(str)

            def __init__(self):
                super().__init__()
                self.events = []
                self.latest_snapshot = SimpleNamespace(
                    reading_age_s=0.0, heater_on=True, operating_mode="driven"
                )

            def acquire_exclusive(self, owner):
                self.events.append(("claim", owner))
                return True

            def release_exclusive(self, owner):
                self.events.append(("release", owner))

            def enter_persistent_mode(self, **_kwargs):
                self.events.append("persistent")

        magnet = Magnet()
        controller = BFieldTransportController(
            magnet, SimpleNamespace(), SimpleNamespace(), thermal_safety=None
        )
        controller._completed_final_mode = "driven"

        self.assertFalse(controller.prepare_shutdown())
        self.assertEqual(magnet.events, [("claim", "bfield-transport"), "persistent"])
        self.assertTrue(controller._shutdown_persistent_waiting)
        magnet.operation_finished.emit("enter_persistent_mode")
        self.assertFalse(controller._shutdown_persistent_waiting)
        self.assertEqual(controller._completed_final_mode, "persistent")
        self.assertEqual(
            magnet.events,
            [("claim", "bfield-transport"), "persistent", ("release", "bfield-transport")],
        )

    def test_shutdown_detects_external_driven_state_with_default_bookkeeping(self):
        class Magnet(QtCore.QObject):
            transport_config_result = QtCore.pyqtSignal(object)
            safe_move_result = QtCore.pyqtSignal(object)
            transport_sweep_result = QtCore.pyqtSignal(object)
            snapshot_updated = QtCore.pyqtSignal(object)
            fault = QtCore.pyqtSignal(str)
            operation_finished = QtCore.pyqtSignal(str)

            def __init__(self):
                super().__init__()
                self.latest_snapshot = SimpleNamespace(
                    reading_age_s=0.0, heater_on=False, operating_mode="persistent"
                )
                # This command was queued before the close request.  The
                # shutdown Persistent request must run after it even though
                # the cached telemetry already looks safe.
                self.events = []
                self.pending = ["manual-driven"]

            def acquire_exclusive(self, owner):
                self.events.append(("claim", owner))
                return True

            def release_exclusive(self, owner):
                self.events.append(("release", owner))

            def enter_persistent_mode(self, **_kwargs):
                self.events.append("persistent-queued")
                self.pending.append("persistent")

            def flush_worker_queue(self):
                while self.pending:
                    operation = self.pending.pop(0)
                    self.events.append(f"{operation}-complete")
                    if operation == "persistent":
                        self.operation_finished.emit("enter_persistent_mode")

        magnet = Magnet()
        controller = BFieldTransportController(
            magnet, SimpleNamespace(), SimpleNamespace(), thermal_safety=None
        )
        # This is the app-start/manual-panel case: transport never ran, so
        # completion bookkeeping alone says nothing about actual heater state.
        self.assertEqual(controller._completed_final_mode, "persistent")
        self.assertFalse(controller.prepare_shutdown())
        self.assertEqual(
            magnet.events,
            [("claim", "bfield-transport"), "persistent-queued"],
        )
        magnet.flush_worker_queue()
        self.assertEqual(magnet.events[-1], ("release", "bfield-transport"))
        # MainWindow retries close after shutdown_ready; the confirmed
        # transition must make that retry a no-op rather than queueing another
        # Persistent command.
        self.assertTrue(controller.prepare_shutdown())
        self.assertEqual(
            magnet.events,
            [
                ("claim", "bfield-transport"),
                "persistent-queued",
                "manual-driven-complete",
                "persistent-complete",
                ("release", "bfield-transport"),
            ],
        )

    def test_shutdown_already_persistent_queues_idempotent_quick_cleanup(self):
        class Magnet(QtCore.QObject):
            transport_config_result = QtCore.pyqtSignal(object)
            safe_move_result = QtCore.pyqtSignal(object)
            transport_sweep_result = QtCore.pyqtSignal(object)
            snapshot_updated = QtCore.pyqtSignal(object)
            fault = QtCore.pyqtSignal(str)

            def __init__(self):
                super().__init__()
                self.events = []

            def acquire_exclusive(self, owner):
                self.events.append(("claim", owner))
                return True

            def release_exclusive(self, owner):
                self.events.append(("release", owner))

            def enter_persistent_mode(self, **_kwargs):
                # The real adapter returns immediately when PSHTR?=0; no
                # redundant cool dwell is introduced by the controller.
                self.events.append("persistent-quick")

        magnet = Magnet()
        controller = BFieldTransportController(
            magnet, SimpleNamespace(), SimpleNamespace(), thermal_safety=None
        )
        self.assertTrue(controller.prepare_shutdown())
        self.assertEqual(
            magnet.events,
            [("claim", "bfield-transport"), "persistent-quick", ("release", "bfield-transport")],
        )

    def test_stop_racing_driven_cleanup_upgrades_restore_to_persistent(self):
        controller = BFieldTransportController.__new__(BFieldTransportController)
        controller._cleanup_in_progress = True
        controller._cleanup_final_mode = "driven"
        controller._cleanup_waiting = "restore"
        transitions = []
        controller._begin_persistent_cleanup = lambda: transitions.append("persistent")
        controller._log = lambda _message: None

        controller.stop()

        self.assertEqual(controller._cleanup_final_mode, "persistent")
        self.assertEqual(transitions, ["persistent"])

    def test_failed_transport_always_uses_persistent_cleanup_even_if_driven_selected(self):
        class Adapter:
            def __init__(self):
                self.events = []

            def safe_move_to_field(self, *_args, **_kwargs):
                self.events.append("move")
                raise RuntimeError("simulated move failure")

            def pause(self, **_kwargs):
                self.events.append("pause")

            def enter_persistent_mode(self, **_kwargs):
                self.events.append("persistent")

        params = BFieldTransportParams(
            start_field_t=0.0,
            stop_field_t=0.01,
            round_trip=False,
            final_mode="driven",
            conditions=[BFieldTransportCondition()],
        )
        adapter = Adapter()
        runner = BFieldTransportSweep(adapter, TransportSweepPlan.from_params(params))
        with self.assertRaises(RuntimeError):
            runner.run(persistent_field_confirmed=True)
        self.assertIn("persistent", adapter.events)

    def test_qt_controller_start_reserves_configures_and_stops_safely(self):
        class Magnet(QtCore.QObject):
            transport_config_result = QtCore.pyqtSignal(object)
            safe_move_result = QtCore.pyqtSignal(object)
            transport_sweep_result = QtCore.pyqtSignal(object)
            snapshot_updated = QtCore.pyqtSignal(object)
            fault = QtCore.pyqtSignal(str)
            def __init__(self):
                super().__init__(); self.events = []; self.is_connected = True; self.latest_snapshot = type("Snapshot", (), {"heater_on": True, "field_t": 0.0})()
            def acquire_exclusive(self, owner): self.events.append(("claim", owner)); return True
            def release_exclusive(self, owner): self.events.append(("release", owner))
            def configure_transport(self, rate, limit):
                self.events.append(("configure", rate, limit))
                self.transport_config_result.emit({"success": True, "stored_rates": {0: .01}, "stored_limits": (-1, 1)})
            def safe_move_to_field(self, target, **_kwargs):
                self.events.append(("move", target)); self.safe_move_result.emit({"success": True})
            def start_transport_sweep(self, target):
                self.events.append(("sweep", target)); self.transport_sweep_result.emit({"success": True})
            def pause(self, **_kwargs): self.events.append("pause")
            def enter_persistent_mode(self, **_kwargs): self.events.append("persistent")
            def restore_transport(self, rates, limits): self.events.append(("restore", rates, limits))
        class Manager:
            def __init__(self):
                self.events = []
                self.sessions = {name: type("Session", (), {"voltage": 0.0, "set_voltage": lambda self, value: setattr(self, "voltage", value)})() for name in ("g1", "g2", "g3")}
            def mark_in_use(self, names): self.events.append(("mark", names)); return True, []
            def release(self, names): self.events.append(("release", names))
            def get_session(self, name): return self.sessions.get(name)
            def is_voltage_source_mode(self, name): return name in self.sessions
            def applied_gate_voltage_limit(self, name): return 20.0
        class Tab(QtCore.QObject):
            def __init__(self):
                super().__init__()
                self.temporary = tempfile.TemporaryDirectory()
                self.save = SaveRoot(user="u", device_id="d", base=self.temporary.name)
                self.locked = []
                self.frozen_paths = None
            def collect_params(self): return BFieldTransportParams(start_field_t=0, stop_field_t=1, conditions=[BFieldTransportCondition()])
            def freeze_output_plan(self, params):
                planned = build_planned_output(
                    self.save, "bfield_transport", params.base_name,
                    transport_output_summary_parts(params), run_id="controller_exact",
                )
                self.frozen_paths = build_transport_output_paths(planned, params.conditions)
                return self.frozen_paths
            def validate_transport_output_ready(self, paths): os.makedirs(paths.planned.output_dir, exist_ok=True)
            def set_sweep_locked(self, value): self.locked.append(value)
            def window(self): return type("Window", (), {"magnet_panel": type("Panel", (), {"_review_valid": lambda self: True})()})()
        magnet, manager, tab = Magnet(), Manager(), Tab()
        thermal = type("Thermal", (), {
            "is_armed": True,
            "latest_snapshot": None,
            "evaluate": lambda self, _snapshot: type("Decision", (), {"magnet_permission": True, "reason": "safe"})(),
        })()
        controller = BFieldTransportController(magnet, tab, manager, thermal_safety=thermal)
        self.assertTrue(controller.start())
        self.assertEqual(controller._manifest, tab.frozen_paths.manifest_path)
        self.assertEqual(tuple(writer.path for writer in controller._writers.values()), tab.frozen_paths.condition_csv_paths)
        self.assertIn(("configure", .1, 6.0), magnet.events)
        self.assertTrue(controller.active)
        controller.stop()
        self.assertIn("persistent", magnet.events)
        deadline = time.monotonic() + 5.0
        while controller.active and time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(0.005)
        self.assertIn(("release", "bfield-transport"), magnet.events)
        tab.temporary.cleanup()


if __name__ == "__main__":
    unittest.main()
