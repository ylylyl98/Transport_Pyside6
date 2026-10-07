"""Offline command-contract and worker failure tests for 1000 sample control."""
import math
import time
import unittest
from unittest.mock import patch

from PySide6 import QtTest, QtWidgets

from app.devices.lakeshore335_adapter import (
    LakeShore335Adapter, LakeShore335Error, MockLakeShore335Adapter,
)
from controllers.lakeshore335_controller import _LakeShoreWorker, LakeShore335Controller
from app.ui.sample_temperature_bar import SampleTemperatureBar
from utils.config import cfg
from test_sample_heater_ranges import TEST_BANDS


class ControlResource:
    def __init__(self, *, output=1, voltage=False):
        self.writes = []
        self.queries = []
        self.values = {
            "*IDN?": "LSCI,MODEL335,SN,2.2",
            "OUTMODE? 1": "1,2,0" if output == 1 else "0,1,0",
            "OUTMODE? 2": "1,2,0" if output == 2 else "0,1,0",
            "INTYPE? B": "3,1,0,1,1",
            "HTRSET? 1": "0,2,1,0,1",
            "HTRSET? 2": f"{int(voltage)},2,1,0,1",
            "SETP? 1": "4.2", "SETP? 2": "4.2",
            "RAMP? 1": "0,1", "RAMP? 2": "0,1",
            "RANGE? 1": "0", "RANGE? 2": "0",
            "HTR? 1": "23.5", "HTR? 2": "23.5",
            "MOUT? 1": "99", "MOUT? 2": "99",
            "KRDG? A": "3.0", "KRDG? B": "4.0",
            "RDGST? A": "0", "RDGST? B": "0",
        }
        self.ignore_writes = set()
        self.break_after_enable = False
        self.enabled = False

    def query(self, command):
        self.queries.append(command)
        if self.break_after_enable and self.enabled and command.startswith("HTR?"):
            return "nan"
        return self.values[command]

    def write(self, command):
        if "?" in command:
            raise RuntimeError("Query failed")
        self.writes.append(command)
        if command in self.ignore_writes:
            return
        prefix, arguments = command.split(" ", 1)
        output, value = arguments.split(",", 1)
        self.values[f"{prefix}? {output}"] = value
        if prefix == "RANGE":
            self.enabled = value != "0"

    def close(self):
        pass


class SampleControlAdapterTests(unittest.TestCase):
    def make_adapter(self, **kwargs):
        resource = ControlResource(**kwargs)
        adapter = LakeShore335Adapter(visa_resource=resource)
        adapter.connect()
        return adapter, resource

    def test_preserve_settings_only_writes_target_and_reads_actual_heater_output(self):
        adapter, resource = self.make_adapter()
        state = adapter.set_sample_control(10.0)
        self.assertEqual(resource.writes, ["SETP 1,10"])
        self.assertEqual(state["setpoint"], 10)
        self.assertEqual(state["heater_output"], 23.5)
        self.assertEqual(state["range"], 0)
        self.assertNotIn("MOUT? 1", resource.queries)

    def test_explicit_ramp_and_heat_activation_are_ordered_and_verified(self):
        adapter, resource = self.make_adapter()
        state = adapter.set_sample_control(12.5, heater_range=1,
            ramp_enabled=True, ramp_rate_k_per_min=0.5)
        self.assertEqual(resource.writes, ["RAMP 1,1,0.5", "SETP 1,12.5", "RANGE 1,1"])
        self.assertTrue(state["ramp_enabled"])
        self.assertEqual(state["ramp_rate_k_per_min"], 0.5)
        self.assertEqual(state["range"], 1)

    def test_off_is_verified_before_changing_target(self):
        adapter, resource = self.make_adapter()
        resource.values["RANGE? 1"] = "3"
        adapter.set_sample_control(4.2, heater_range=0)
        self.assertEqual(resource.writes, ["RANGE 1,0", "SETP 1,4.2"])

    def test_invalid_targets_and_rates_write_nothing(self):
        for target in (0, -1, 300.001, math.nan, math.inf):
            with self.subTest(target=target):
                adapter, resource = self.make_adapter()
                with self.assertRaises(LakeShore335Error):
                    adapter.set_sample_control(target)
                self.assertEqual(resource.writes, [])
        for rate in (0, 0.099, 100.1, math.nan):
            adapter, resource = self.make_adapter()
            with self.assertRaises(LakeShore335Error):
                adapter.set_sample_control(5, ramp_enabled=True, ramp_rate_k_per_min=rate)
            self.assertEqual(resource.writes, [])

    def test_configured_bounds_and_range_ceiling_are_enforced_before_writes(self):
        adapter, resource = self.make_adapter()
        for settings in ({"maximum_temperature_k": 7},
                         {"minimum_temperature_k": 20},
                         {"heater_range": 3, "maximum_heater_range": 1},
                         {"heater_range": 1.5}):
            with self.assertRaises(LakeShore335Error):
                adapter.set_sample_control(10, **settings)
        self.assertEqual(resource.writes, [])

    def test_celsius_or_disabled_input_and_ambiguous_mapping_write_nothing(self):
        for key, value in (("INTYPE? B", "3,1,0,1,2"),
                           ("INTYPE? B", "0,1,0,1,1"),
                           ("OUTMODE? 2", "1,2,0")):
            adapter, resource = self.make_adapter()
            resource.values[key] = value
            with self.assertRaises(LakeShore335Error):
                adapter.set_sample_control(10, heater_range=1)
            self.assertEqual(resource.writes, [])

    def test_faulty_sample_sensor_cannot_enable_heating(self):
        for key, value in (("RDGST? B", "16"), ("KRDG? B", "nan")):
            adapter, resource = self.make_adapter()
            resource.values[key] = value
            with self.assertRaises(LakeShore335Error):
                adapter.set_sample_control(10, heater_range=1)
            self.assertEqual(resource.writes, [])

    def test_sensor_fault_appearing_during_setpoint_write_prevents_activation(self):
        adapter, resource = self.make_adapter()
        original_write = resource.write
        def write(command):
            original_write(command)
            if command.startswith("SETP"):
                resource.values["RDGST? B"] = "16"
        resource.write = write
        with self.assertRaises(LakeShore335Error):
            adapter.set_sample_control(10, heater_range=1)
        self.assertNotIn("RANGE 1,1", resource.writes)

    def test_failed_setpoint_or_ramp_confirmation_never_enables_heater(self):
        for ignored in ("SETP 1,10", "RAMP 1,1,0.5"):
            adapter, resource = self.make_adapter()
            resource.ignore_writes.add(ignored)
            with self.assertRaises(LakeShore335Error):
                adapter.set_sample_control(10, heater_range=1,
                    ramp_enabled=True, ramp_rate_k_per_min=0.5)
            self.assertFalse(any(command.startswith("RANGE") for command in resource.writes))

    def test_failed_activation_readback_turns_that_output_off(self):
        adapter, resource = self.make_adapter()
        resource.ignore_writes.add("RANGE 1,1")
        with self.assertRaisesRegex(LakeShore335Error, "heater OFF confirmed"):
            adapter.set_sample_control(10, heater_range=1)
        self.assertEqual(resource.writes[-2:], ["RANGE 1,1", "RANGE 1,0"])

    def test_keep_active_range_and_ramp_only_writes_verified_target(self):
        adapter, resource = self.make_adapter()
        resource.values["RANGE? 1"] = "2"
        resource.values["RAMP? 1"] = "1,0.5"
        state = adapter.set_sample_control(10)
        self.assertEqual(resource.writes, ["SETP 1,10"])
        self.assertEqual((state["range"], state["ramp_enabled"], state["ramp_rate_k_per_min"]), (2, True, 0.5))
        self.assertIn("RDGST? B", resource.queries)

    def test_keep_active_output_requires_valid_sensor_before_writes(self):
        adapter, resource = self.make_adapter()
        resource.values["RANGE? 1"] = "1"
        resource.values["RDGST? B"] = "16"
        with self.assertRaises(LakeShore335Error):
            adapter.set_sample_control(10)
        self.assertEqual(resource.writes, [])

    def test_unconfirmed_setpoint_or_ramp_shuts_down_already_active_output(self):
        for output in (1, 2):
            for ignored, settings in ((f"SETP {output},10", {}),
                    (f"RAMP {output},1,0.5", {"ramp_enabled": True, "ramp_rate_k_per_min": 0.5})):
                with self.subTest(output=output, ignored=ignored):
                    adapter, resource = self.make_adapter(output=output)
                    resource.values[f"RANGE? {output}"] = "1"
                    resource.ignore_writes.add(ignored)
                    with self.assertRaisesRegex(LakeShore335Error, "heater OFF confirmed"):
                        adapter.set_sample_control(10, **settings)
                    self.assertEqual(resource.writes, [ignored, f"RANGE {output},0"])
                    self.assertEqual(resource.values[f"RANGE? {output}"], "0")

    def test_late_sensor_fault_shuts_down_active_keep_output(self):
        adapter, resource = self.make_adapter()
        resource.values["RANGE? 1"] = "1"
        original_write = resource.write
        def write(command):
            original_write(command)
            if command.startswith("SETP"):
                resource.values["RDGST? B"] = "16"
        resource.write = write
        with self.assertRaisesRegex(LakeShore335Error, "heater OFF confirmed"):
            adapter.set_sample_control(10)
        self.assertEqual(resource.writes, ["SETP 1,10", "RANGE 1,0"])

    def test_keep_shutdown_failure_remains_unconfirmed(self):
        adapter, resource = self.make_adapter()
        resource.values["RANGE? 1"] = "1"
        resource.ignore_writes.update({"SETP 1,10", "RANGE 1,0"})
        with self.assertRaisesRegex(LakeShore335Error, "heater OFF unconfirmed"):
            adapter.set_sample_control(10)
        self.assertEqual(resource.values["RANGE? 1"], "1")

    def test_keep_cannot_bypass_configured_range_ceiling(self):
        adapter, resource = self.make_adapter()
        resource.values["RANGE? 1"] = "3"
        for settings in ({}, {"heater_range": 1}):
            with self.assertRaisesRegex(LakeShore335Error, "Existing heater range"):
                adapter.set_sample_control(10, maximum_heater_range=1, **settings)
        self.assertEqual(resource.writes, [])
        adapter.heater_off()
        adapter.set_sample_control(10, maximum_heater_range=1, heater_range=1)
        self.assertEqual(resource.writes, ["RANGE 1,0", "SETP 1,10", "RANGE 1,1"])

    def test_telemetry_failure_after_activation_also_turns_output_off(self):
        adapter, resource = self.make_adapter()
        resource.break_after_enable = True
        with self.assertRaisesRegex(LakeShore335Error, "heater OFF confirmed"):
            adapter.set_sample_control(10, heater_range=1)
        self.assertEqual(resource.values["RANGE? 1"], "0")

    def test_failed_compensating_shutdown_is_reported_as_unconfirmed(self):
        adapter, resource = self.make_adapter()
        resource.break_after_enable = True
        resource.ignore_writes.add("RANGE 1,0")
        with self.assertRaisesRegex(LakeShore335Error, "heater OFF unconfirmed"):
            adapter.set_sample_control(10, heater_range=1)
        self.assertEqual(resource.values["RANGE? 1"], "1")

    def test_voltage_output_only_accepts_off_or_on(self):
        adapter, resource = self.make_adapter(output=2, voltage=True)
        with self.assertRaises(LakeShore335Error):
            adapter.set_sample_control(10, heater_range=2)
        self.assertEqual(resource.writes, [])
        state = adapter.set_sample_control(10, heater_range=1)
        self.assertEqual(state["output_type"], "voltage")
        self.assertEqual(resource.writes, ["SETP 2,10", "RANGE 2,1"])

    def test_shutdown_does_not_require_kelvin_units_or_valid_sensor(self):
        adapter, resource = self.make_adapter()
        resource.values["INTYPE? B"] = "3,1,0,1,2"
        resource.values["RDGST? B"] = "128"
        state = adapter.heater_off()
        self.assertTrue(state["confirmed"])
        self.assertEqual(resource.writes, ["RANGE 1,0"])

    def test_command_injection_and_setup_writes_are_rejected(self):
        adapter, resource = self.make_adapter()
        for command in ("PID 1,1,1,1", "OUTMODE 1,1,2,1", "RANGE 1,1\nSETP 1,300",
                        "RAMP 1,2,1", "RANGE 1,4", "SETP 1,-10"):
            with self.assertRaises(LakeShore335Error):
                adapter._write(command)
        self.assertEqual(resource.writes, [])

    def test_mock_can_control_without_changing_measured_temperatures(self):
        adapter = MockLakeShore335Adapter(sample_temperature_k=4, reservoir_temperature_k=3)
        state = adapter.set_sample_control(10, heater_range=1)
        self.assertEqual(state["setpoint"], 10)
        self.assertEqual(adapter.read_snapshot().sample_temperature_k, 4)

    @staticmethod
    def configure_zones(resource, *, output=1):
        for zone in range(1, 11):
            if zone <= len(TEST_BANDS):
                band = TEST_BANDS[zone-1]
                value = f"{band['upper_temperature_k']},10,10,0,0,{band['heater_range']},2,1"
            else:
                value = "0,10,10,0,0,0,0,1"
            resource.values[f"ZONE? {output},{zone}"] = value

    def test_custom_auto_range_is_chosen_and_verified_for_the_target(self):
        adapter, resource = self.make_adapter()
        state = adapter.set_sample_control(10.001, heater_range="auto", auto_heater_ranges=TEST_BANDS)
        self.assertEqual(resource.writes, ["SETP 1,10.001", "RANGE 1,2"])
        self.assertEqual(state["auto_target_range"], 2)
        self.assertEqual(state["auto_range_source"], "configured")
        self.assertFalse(any(query.startswith("ZONE?") for query in resource.queries))

    def test_empty_custom_table_uses_commissioned_instrument_zones(self):
        adapter, resource = self.make_adapter()
        self.configure_zones(resource)
        state = adapter.set_sample_control(100, heater_range="auto", auto_heater_ranges=[])
        self.assertEqual(resource.writes, ["SETP 1,100", "RANGE 1,2"])
        self.assertEqual(state["auto_upper_temperature_k"], 100)
        self.assertEqual(state["auto_range_source"], "ls335_zone")

    def test_auto_with_invalid_custom_table_or_missing_coverage_writes_nothing(self):
        for bands, target in (([TEST_BANDS[0], TEST_BANDS[0]], 4.2), (TEST_BANDS[:1], 20)):
            adapter, resource = self.make_adapter()
            with self.assertRaises(LakeShore335Error):
                adapter.set_sample_control(target, heater_range="auto", auto_heater_ranges=bands)
            self.assertEqual(resource.writes, [])

    def test_auto_does_not_silently_clip_range_or_guess_an_unconfigured_zone_table(self):
        adapter, resource = self.make_adapter()
        for settings in ({"auto_heater_ranges": TEST_BANDS, "maximum_heater_range": 1}, {}):
            with self.assertRaises(LakeShore335Error):
                adapter.set_sample_control(20, heater_range="auto", **settings)
        self.assertEqual(resource.writes, [])

    def test_invalid_instrument_zones_or_different_input_block_all_writes(self):
        for key, value in (("ZONE? 1,1", "10,10,10,0,0,1,1,1"),
                           ("ZONE? 1,2", "5,10,10,0,0,2,2,1"),
                           ("ZONE? 1,2", "malformed"),
                           ("ZONE? 1,2", "0,10,10,0,0,0,0,1")):
            adapter, resource = self.make_adapter()
            self.configure_zones(resource)
            resource.values[key] = value
            with self.assertRaises(LakeShore335Error):
                adapter.set_sample_control(4.2, heater_range="auto")
            self.assertEqual(resource.writes, [])

    def test_native_zone_mode_keeps_range_and_ramp_under_instrument_control(self):
        adapter, resource = self.make_adapter()
        self.configure_zones(resource)
        resource.values["OUTMODE? 1"] = "2,2,0"
        resource.values["RAMP? 1"] = "1,1"
        state = adapter.set_sample_control(20, heater_range="auto")
        self.assertEqual(resource.writes, ["SETP 1,20"])
        self.assertTrue(state["auto_native_zone"])
        self.assertEqual(state["auto_target_range"], 2)

    def test_keep_in_native_zone_mode_validates_profiles_and_preserves_ramp_and_range(self):
        adapter, resource = self.make_adapter()
        self.configure_zones(resource)
        resource.values["OUTMODE? 1"] = "2,2,0"
        resource.values["RAMP? 1"] = "1,1"
        adapter.set_sample_control(20)
        self.assertEqual(resource.writes, ["SETP 1,20"])
        self.assertIn("ZONE? 1,10", resource.queries)

    def test_keep_cannot_bypass_native_zone_sensor_or_range_limits(self):
        for changes, settings in (({"ZONE? 1,2": "100,10,10,0,0,2,1,1"}, {}),
                ({}, {"maximum_heater_range": 1}), ({}, {"maximum_heater_range": 2})):
            with self.subTest(changes=changes, settings=settings):
                adapter, resource = self.make_adapter()
                self.configure_zones(resource)
                resource.values["OUTMODE? 1"] = "2,2,0"
                resource.values.update(changes)
                with self.assertRaises(LakeShore335Error):
                    adapter.set_sample_control(20, **settings)
                self.assertEqual(resource.writes, [])

    def test_manual_overrides_cannot_compete_with_native_zone_profiles(self):
        for settings in ({"heater_range": 1}, {"ramp_enabled": False, "ramp_rate_k_per_min": 1}):
            adapter, resource = self.make_adapter()
            resource.values["OUTMODE? 1"] = "2,2,0"
            with self.assertRaisesRegex(LakeShore335Error, "Keep heater range and Keep ramp"):
                adapter.set_sample_control(20, **settings)
            self.assertEqual(resource.writes, [])

    def test_auto_resumes_stopped_native_output_when_setpoint_is_stationary(self):
        adapter, resource = self.make_adapter()
        self.configure_zones(resource)
        resource.values["OUTMODE? 1"] = "2,2,0"
        state = adapter.set_sample_control(20, heater_range="auto")
        self.assertEqual(resource.writes, ["SETP 1,20", "RANGE 1,2"])
        self.assertTrue(state["auto_native_zone"])
        self.assertEqual(state["range"], 2)

    def test_sensor_fault_after_native_setpoint_write_requests_shutdown(self):
        adapter, resource = self.make_adapter()
        self.configure_zones(resource)
        resource.values["OUTMODE? 1"] = "2,2,0"
        resource.values["RAMP? 1"] = "1,0.5"
        original_write = resource.write
        def write(command):
            original_write(command)
            if command.startswith("SETP"):
                resource.values["RDGST? B"] = "16"
        resource.write = write
        with self.assertRaisesRegex(LakeShore335Error, "heater OFF confirmed"):
            adapter.set_sample_control(20, heater_range="auto")
        self.assertEqual(resource.writes, ["SETP 1,20", "RAMP 1,0,0.5", "RANGE 1,0"])

    def test_native_zone_mode_rejects_custom_overrides_and_unsafe_zone_limits(self):
        for settings in ({"auto_heater_ranges": TEST_BANDS},
                         {"ramp_enabled": True, "ramp_rate_k_per_min": 1},
                         {"maximum_heater_range": 1}):
            adapter, resource = self.make_adapter()
            self.configure_zones(resource)
            resource.values["OUTMODE? 1"] = "2,2,0"
            with self.assertRaises(LakeShore335Error):
                adapter.set_sample_control(4.2, heater_range="auto", **settings)
            self.assertEqual(resource.writes, [])

    def test_zone_off_stops_ramp_so_crossing_a_zone_cannot_restart_heating(self):
        adapter, resource = self.make_adapter()
        resource.values["OUTMODE? 1"] = "2,2,0"
        resource.values["RAMP? 1"] = "1,0.5"
        state = adapter.heater_off()
        self.assertEqual(resource.writes, ["RAMP 1,0,0.5", "RANGE 1,0"])
        self.assertTrue(state["zone_ramp_stopped"])
        self.assertEqual(resource.values["RAMP? 1"], "0,0.5")

    def test_failed_zone_ramp_stop_still_attempts_off_and_reports_unconfirmed(self):
        adapter, resource = self.make_adapter()
        resource.values["OUTMODE? 1"] = "2,2,0"
        resource.values["RAMP? 1"] = "1,0.5"
        resource.ignore_writes.add("RAMP 1,0,0.5")
        with self.assertRaisesRegex(LakeShore335Error, "Zone ramp stop unconfirmed"):
            adapter.heater_off()
        self.assertEqual(resource.writes[-1], "RANGE 1,0")

    def test_setpoint_readback_failure_in_zone_mode_also_stops_ramp_and_heater(self):
        adapter, resource = self.make_adapter()
        self.configure_zones(resource)
        resource.values["OUTMODE? 1"] = "2,2,0"
        resource.values["RAMP? 1"] = "1,0.5"
        resource.ignore_writes.add("SETP 1,20")
        with self.assertRaisesRegex(LakeShore335Error, "heater OFF confirmed"):
            adapter.set_sample_control(20, heater_range="auto")
        self.assertEqual(resource.writes, ["SETP 1,20", "RAMP 1,0,0.5", "RANGE 1,0"])

    def test_voltage_auto_table_cannot_select_medium(self):
        adapter, resource = self.make_adapter(output=2, voltage=True)
        with self.assertRaises(LakeShore335Error):
            adapter.set_sample_control(20, heater_range="auto", auto_heater_ranges=TEST_BANDS)
        self.assertEqual(resource.writes, [])

    def test_off_with_a_new_zone_setpoint_is_rejected_before_possible_activation(self):
        adapter, resource = self.make_adapter()
        resource.values["OUTMODE? 1"] = "2,2,0"
        with self.assertRaisesRegex(LakeShore335Error, "Use Heater Off"):
            adapter.set_sample_control(20, heater_range=0)
        self.assertEqual(resource.writes, [])


class SampleControlWorkerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def setUp(self):
        self.worker = _LakeShoreWorker()
        self.results = []
        self.worker.control_result.connect(self.results.append)
        self.addCleanup(lambda: self.worker._timer.stop())

    def test_disconnected_requests_all_report_failure(self):
        self.worker.set_sample_control({"temperature_k": 10})
        self.worker.set_sample_setpoint(10)
        self.worker.heater_off()
        self.assertEqual([item["operation"] for item in self.results],
                         ["sample_control", "setpoint", "heater_off"])
        self.assertTrue(all(not item["ok"] for item in self.results))

    def test_control_reads_do_not_turn_mapping_fault_into_thermal_fault(self):
        adapter, resource = SampleControlAdapterTests().make_adapter()
        self.worker.adapter = adapter
        resource.values["INTYPE? B"] = "3,1,0,1,2"
        snapshots, faults = [], []
        self.worker.snapshot_updated.connect(snapshots.append)
        self.worker.fault.connect(faults.append)
        self.worker.refresh_snapshot()
        self.assertEqual(len(snapshots), 1)
        self.assertTrue(snapshots[0].communication_valid)
        self.assertEqual(faults, [])
        self.assertFalse(self.results[0]["ok"])

    def test_unverified_channel_mapping_prevents_control_writes(self):
        self.worker.adapter, resource = SampleControlAdapterTests().make_adapter()
        with patch.object(cfg.lakeshore335, "verified_channel_mapping", False):
            self.worker.set_sample_control({"temperature_k": 10, "heater_range": 1})
        self.assertFalse(self.results[0]["ok"])
        self.assertEqual(resource.writes, [])

    def test_worker_uses_configured_temperature_limits(self):
        self.worker.adapter, resource = SampleControlAdapterTests().make_adapter()
        with patch.object(cfg.lakeshore335, "sample_maximum_temperature_k", 7):
            self.worker.set_sample_control({"temperature_k": 10})
        self.assertFalse(self.results[0]["ok"])
        self.assertEqual(resource.writes, [])

    def test_compatibility_setpoint_api_uses_the_same_configured_bounds(self):
        self.worker.adapter, resource = SampleControlAdapterTests().make_adapter()
        with patch.object(cfg.lakeshore335, "sample_maximum_temperature_k", 7):
            self.worker.set_sample_setpoint(10)
        self.assertFalse(self.results[0]["ok"])
        self.assertEqual(resource.writes, [])

    def test_compatibility_setpoint_api_obeys_configured_range_ceiling(self):
        self.worker.adapter, resource = SampleControlAdapterTests().make_adapter()
        resource.values["RANGE? 1"] = "3"
        with patch.object(cfg.lakeshore335, "sample_maximum_heater_range", 1):
            self.worker.set_sample_setpoint(10)
        self.assertFalse(self.results[0]["ok"])
        self.assertEqual(resource.writes, [])

    def test_worker_passes_the_configured_auto_table_and_replies_with_resolved_range(self):
        self.worker.adapter, resource = SampleControlAdapterTests().make_adapter()
        with patch.object(cfg.lakeshore335, "sample_auto_heater_ranges", TEST_BANDS):
            self.worker.set_sample_control({"temperature_k": 20, "heater_range": "auto"})
        self.assertTrue(self.results[0]["ok"])
        self.assertEqual(self.results[0]["auto_target_range"], 2)
        self.assertEqual(resource.writes[-1], "RANGE 1,2")


class SampleControlThreadTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def wait_until(self, predicate):
        deadline = time.monotonic() + 2
        while not predicate() and time.monotonic() < deadline:
            QtTest.QTest.qWait(10)
        self.assertTrue(predicate(), "Queued temperature control did not complete")

    def test_ui_queued_apply_stability_and_heater_off_through_real_worker_thread(self):
        adapter = MockLakeShore335Adapter(sample_temperature_k=4.2, reservoir_temperature_k=3)
        controller = LakeShore335Controller()
        bar = SampleTemperatureBar(controller, cfg.lakeshore335)
        try:
            bar.set_backend("1000")
            bar.wait_check.setChecked(True)
            with patch("controllers.lakeshore335_controller.MockLakeShore335Adapter", return_value=adapter):
                controller.connect_instrument(use_mock=True)
                self.wait_until(lambda: bar._control_state is not None)
            self.assertNotEqual(controller._worker.thread(), self.app.thread())
            bar.target.setValue(10)
            bar.heater_range.setCurrentIndex(2)
            bar.ramp_mode.setCurrentIndex(2)
            bar.ramp_rate.setValue(0.5)
            bar._set_target()
            self.assertFalse(bar.is_ready())
            self.wait_until(lambda: bar._pending_operation is None)
            self.assertEqual(bar._target, 10)
            self.assertEqual(adapter.control_commands, ["RAMP 1,1,0.5", "SETP 1,10", "RANGE 1,1"])
            with patch.object(cfg.lakeshore335, "sample_stability_dwell_s", 0):
                adapter.set_readings(10, 3)
                controller.refresh_snapshot()
                self.wait_until(bar.is_ready)
            bar._heater_off()
            self.wait_until(lambda: bar._pending_operation is None)
            self.assertIn("OFF — confirmed", bar.control_status.text())
            self.assertEqual(adapter.control_commands[-1], "RANGE 1,0")
        finally:
            bar._age_timer.stop()
            controller.shutdown()
            bar.deleteLater()

    def test_queued_auto_apply_resolves_configuration_and_off_stays_off(self):
        adapter = MockLakeShore335Adapter(sample_temperature_k=4.2, reservoir_temperature_k=3)
        controller = LakeShore335Controller()
        bar = SampleTemperatureBar(controller, cfg.lakeshore335)
        try:
            bar.set_backend("1000")
            with patch("controllers.lakeshore335_controller.MockLakeShore335Adapter", return_value=adapter):
                controller.connect_instrument(use_mock=True)
                self.wait_until(lambda: bar._control_state is not None)
            with patch.object(cfg.lakeshore335, "sample_auto_heater_ranges", TEST_BANDS):
                bar.target.setValue(20)
                bar.heater_range.setCurrentIndex(bar.heater_range.findData("auto"))
                bar._set_target()
                self.wait_until(lambda: bar._pending_operation is None)
                self.assertEqual(adapter.control_commands, ["SETP 1,20", "RANGE 1,2"])
                self.assertIn("Medium", bar.auto_preview.text())
                bar._heater_off()
                self.wait_until(lambda: bar._pending_operation is None)
                controller.refresh_snapshot()
                QtTest.QTest.qWait(30)
                self.assertEqual(adapter.control_commands, ["SETP 1,20", "RANGE 1,2", "RANGE 1,0"])
        finally:
            bar._age_timer.stop()
            controller.shutdown()
            bar.deleteLater()


if __name__ == "__main__":
    unittest.main()
