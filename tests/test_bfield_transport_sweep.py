import csv
import json
import os
import tempfile
import unittest
from PyQt6 import QtCore

from app.engine.bfield_transport_sweep import (
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
)
from app.engine.bfield_transport_controller import BFieldTransportController
from app.models import BFieldTransportCondition, BFieldTransportParams


class BFieldTransportSweepTests(unittest.TestCase):
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
        params = BFieldTransportParams(start_field_t=-1, stop_field_t=1, round_trip=True, conditions=rows)
        plan = TransportSweepPlan.from_params(params)
        applied, acquired = [], []
        runner = BFieldTransportSweep(Adapter(), plan, apply_condition=lambda c: applied.append(c.name), acquire=lambda c, d, s: acquired.append((c.name, d, s.field_t)))
        records = runner.run(persistent_field_confirmed=True)
        self.assertEqual(applied, ["a", "b"])
        self.assertEqual([r["direction"] for r in records], ["start", "forward", "backward"] * 2)
        self.assertEqual(acquired[2][2], -1)
        self.assertEqual(runner.adapter.events[-1], "persistent")

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
            def __init__(self): super().__init__(); self.save = type("Save", (), {"user": "u", "device_id": "d", "base": tempfile.gettempdir()})(); self.locked = []
            def collect_params(self): return BFieldTransportParams(start_field_t=0, stop_field_t=1, conditions=[BFieldTransportCondition()])
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
        self.assertIn(("configure", .1, 6.0), magnet.events)
        self.assertTrue(controller.active)
        controller.stop()
        self.assertIn("persistent", magnet.events)
        self.assertIn(("release", "bfield-transport"), magnet.events)


if __name__ == "__main__":
    unittest.main()
