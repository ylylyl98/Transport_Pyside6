from __future__ import annotations

import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6 import QtTest, QtWidgets

from app.device_manager import DeviceManager
from app.models import Connections, SaveRoot
from app.signal_chain import SignalChainSnapshot, engineering_value, signal_chain_filename_parts
from app.ui.dock import ConnDock
from app.ui.lockin_panel import LockinPanel
from app.ui.main_window import MainWindow
from app.ui.tabs.cosweep_tab import CoSweepTab
from app.ui.tabs.dual_gate_tab import DualGateTab
from app.ui.tabs.gate_scan_tab import GateScanTab
from app.ui.tabs.photocurrent_tab import PhotocurrentTab


class SignalChainUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def test_engineering_filename_tags_are_compact_and_readable(self):
        snapshot = SignalChainSnapshot(
            frequency_hz=1000.0,
            lockin_sensitivity_v=0.02,
            preamp_sensitivity_a=100e-9,
        )

        self.assertEqual(engineering_value(5e6, "V/A"), "5MV/A")
        self.assertEqual(
            signal_chain_filename_parts(snapshot),
            ["freq_1kHz", "lia_20mV", "preamp_100nA"],
        )
        metadata = snapshot.to_dict()
        self.assertEqual(metadata["preamp_gain_v_per_a"], 1e7)
        self.assertEqual(metadata["preamp_sensitivity_source"], "manual calibration")

    def test_all_measurement_tabs_include_the_same_signal_chain_tags(self):
        snapshot = SignalChainSnapshot(1000.0, 0.02, 100e-9)
        snapshot_getter = lambda: snapshot
        manager = DeviceManager(Connections())
        save = SaveRoot()
        tabs = [
            DualGateTab(save, manager.connections, manager, get_signal_chain_callable=snapshot_getter),
            CoSweepTab(save, manager.connections, manager, get_signal_chain_callable=snapshot_getter),
            GateScanTab(save, manager.connections, manager, get_signal_chain_callable=snapshot_getter),
            PhotocurrentTab(save, manager.connections, manager, get_signal_chain_callable=snapshot_getter),
        ]
        try:
            for tab in tabs:
                parts = tab._output_summary_parts()
                self.assertIn("freq_1kHz", parts)
                self.assertIn("lia_20mV", parts)
                self.assertIn("preamp_100nA", parts)
                tab.refresh_output_preview()
                self.assertIn("freq_1kHz_lia_20mV_preamp_100nA", tab._planned_output.stem)
        finally:
            for tab in tabs:
                tab.close()

    def test_user_filename_stem_replaces_the_generated_measurement_label(self):
        manager = DeviceManager(Connections())
        save = SaveRoot(device_id="YZD323")
        gate_scan = GateScanTab(save, manager.connections, manager)
        cosweep = CoSweepTab(save, manager.connections, manager)
        try:
            gate_scan.ed_base.setText("gate_scan")
            gate_scan.refresh_output_preview()
            self.assertEqual(gate_scan._planned_output.display_stem.count("gate_scan"), 1)
            self.assertIn("gate_scan", gate_scan._planned_output.output_dir)

            cosweep.ed_base.setText("dual_gate_cosweep")
            cosweep.cbo_sweep_dim.setCurrentText("2D map")
            cosweep.refresh_output_preview()
            map_stem = cosweep._planned_output.display_stem
            self.assertIn("dual_gate_cosweep", map_stem)
            self.assertNotIn("map_2d", map_stem)
            self.assertIn("map_2d", cosweep._planned_output.output_dir)

            cosweep.cbo_sweep_dim.setCurrentText("1D sweep")
            cosweep.refresh_output_preview()
            sweep_stem = cosweep._planned_output.display_stem
            self.assertIn("dual_gate_cosweep", sweep_stem)
            self.assertNotIn("sweep_1d", sweep_stem)
            self.assertIn("sweep_1d", cosweep._planned_output.output_dir)

            gate_scan.ed_base.setText("gate_scan_temperature_10K")
            gate_scan.refresh_output_preview()
            self.assertIn("gate_scan_temperature_10K", gate_scan._planned_output.display_stem)
        finally:
            gate_scan.close()
            cosweep.close()

    def test_preamp_edit_reports_active_saved_value_and_gain(self):
        with patch.object(ConnDock, "_start_scan"):
            dock = ConnDock()
        changed = QtTest.QSignalSpy(dock.signal_chain_changed)

        dock.sp_amp.setValue(200e-9)

        self.assertGreaterEqual(len(changed), 1)
        self.assertIn("Active: 200 nA - saved", dock.lbl_amp_status.text())
        self.assertIn("gain 5 MV/A", dock.lbl_amp_status.text())
        self.assertEqual(dock.lbl_amp_status.property("role"), "success-hint")
        dock.close()

    def test_run_start_passes_the_snapshot_and_matching_rates_to_every_worker(self):
        snapshot = SignalChainSnapshot(137.0, 0.02, 500e-9)
        manager = DeviceManager(Connections())
        manager.mark_in_use = lambda _devices: (True, [])
        manager.release = lambda _devices: None
        save = SaveRoot()
        cases = [
            (
                DualGateTab(save, manager.connections, manager, lambda: (2e6, 20.0), get_signal_chain_callable=lambda: snapshot),
                "app.ui.tabs.dual_gate_tab.DualGateWorker",
                ("_validate_required_sessions",),
            ),
            (
                CoSweepTab(save, manager.connections, manager, lambda: (2e6, 20.0), get_signal_chain_callable=lambda: snapshot),
                "app.ui.tabs.cosweep_tab.CoSweepWorker",
                ("_validate_sweep_setup", "_validate_required_sessions"),
            ),
            (
                GateScanTab(save, manager.connections, manager, lambda: (2e6, 20.0), get_signal_chain_callable=lambda: snapshot),
                "app.ui.tabs.gate_scan_tab.LineSweepWorker",
                ("_validate_required_sessions", "_validate_params"),
            ),
            (
                PhotocurrentTab(save, manager.connections, manager, lambda: (2e6, 20.0), get_signal_chain_callable=lambda: snapshot),
                "app.ui.tabs.photocurrent_tab.PhotocurrentWorker",
                ("_validate_required_sessions", "_validate_condition_output_paths"),
            ),
        ]
        try:
            for tab, worker_path, validation_names in cases:
                captured = {}

                def capture_worker(*_args, **kwargs):
                    captured.update(kwargs)
                    raise RuntimeError("stop after argument capture")

                tab.validate_output_ready = lambda _save: True
                tab.collect_params = lambda: None
                tab.begin_run_logging = lambda *_args: None
                tab.end_run_logging = lambda *_args: None
                tab.append_log = lambda *_args: None
                for name in validation_names:
                    setattr(tab, name, lambda: True)

                with patch(worker_path, side_effect=capture_worker):
                    tab.start_run()

                self.assertEqual(captured["amp_rate"], 2e6)
                self.assertEqual(captured["lkn_rate"], 20.0)
                self.assertEqual(captured["signal_chain"]["frequency_hz"], 137.0)
                self.assertEqual(captured["signal_chain"]["lockin_sensitivity_v"], 0.02)
                self.assertEqual(captured["signal_chain"]["preamp_sensitivity_a"], 500e-9)
        finally:
            for tab, _worker_path, _validation_names in cases:
                tab.close()

    def test_measurement_claim_includes_connected_lockin_and_releases_exact_set(self):
        manager = DeviceManager(Connections())
        manager.sessions["lockin"] = object()
        manager.states["lockin"] = "ok"
        tab = GateScanTab(SaveRoot(), manager.connections, manager)
        try:
            claimed, blocked = tab.claim_run_devices(["daq", "g1"])
            self.assertTrue(claimed)
            self.assertEqual(blocked, [])
            self.assertEqual(manager.current_in_use(), {"daq", "g1", "lockin"})
            self.assertEqual(tab._run_claimed_devices, ["daq", "g1", "lockin"])

            tab.release_run_devices()
            self.assertEqual(manager.current_in_use(), set())
            self.assertEqual(tab._run_claimed_devices, [])
        finally:
            tab.close()

    def test_connected_lockin_read_failure_blocks_run_before_devices_are_claimed(self):
        class UnreadableLockin:
            def read_sensitivity(self):
                raise RuntimeError("SENS query failed")

        manager = DeviceManager(Connections())
        manager.sessions["lockin"] = UnreadableLockin()
        manager.states["lockin"] = "ok"
        with patch.object(ConnDock, "_start_scan"):
            dock = ConnDock(manager)
        tab = GateScanTab(
            SaveRoot(),
            manager.connections,
            manager,
            get_global_rates_callable=dock.get_rates,
        )
        try:
            with patch.object(QtWidgets.QMessageBox, "warning") as warning:
                tab.start_run()

            self.assertEqual(manager.current_in_use(), set())
            warning.assert_called_once()
            self.assertIn("could not be verified", warning.call_args.args[2])
        finally:
            tab.close()
            dock.close()

    def test_compact_sections_use_the_requested_default_states(self):
        with patch.object(ConnDock, "_start_scan"):
            dock = ConnDock()
        manager = DeviceManager(Connections())
        save = SaveRoot()
        lockin = LockinPanel(manager)
        tabs = [
            DualGateTab(save, manager.connections, manager),
            CoSweepTab(save, manager.connections, manager),
            GateScanTab(save, manager.connections, manager),
            PhotocurrentTab(save, manager.connections, manager),
        ]
        try:
            self.assertFalse(dock.exp_hw.is_expanded())
            self.assertFalse(dock.exp_protection.is_expanded())
            self.assertFalse(dock.exp_save.is_expanded())
            self.assertFalse(dock.exp_signal_chain.is_expanded())
            self.assertTrue(dock.exp_connections.is_expanded())
            self.assertTrue(dock.exp_manual.is_expanded())
            self.assertFalse(lockin.exp_settings.is_expanded())
            self.assertFalse(lockin.exp_actions.is_expanded())
            for tab in tabs:
                self.assertFalse(tab.exp_timing.is_expanded())
                self.assertFalse(tab.exp_output.is_expanded())
            self.assertFalse(tabs[0].exp_dev.is_expanded())
            self.assertFalse(tabs[3].exp_vds.is_expanded())
        finally:
            dock.close()
            lockin.close()
            for tab in tabs:
                tab.close()

    def test_signal_chain_edits_refresh_every_main_window_filename_preview(self):
        class PreviewTab:
            def __init__(self):
                self.refresh_count = 0

            def refresh_output_preview(self):
                self.refresh_count += 1

        window = SimpleNamespace(
            tab_dual=PreviewTab(),
            tab_gate_scan=PreviewTab(),
            tab_cosweep=PreviewTab(),
            tab_photocurrent=PreviewTab(),
        )

        MainWindow._on_signal_chain_changed(window)

        for tab in (
            window.tab_dual,
            window.tab_gate_scan,
            window.tab_cosweep,
            window.tab_photocurrent,
        ):
            self.assertEqual(tab.refresh_count, 1)

    def test_lockin_safe_stop_request_targets_the_active_measurement(self):
        class RunTab:
            def __init__(self, active=False):
                self.worker = object() if active else None
                self.stop_count = 0

            def stop_run(self):
                self.stop_count += 1

        window = SimpleNamespace(
            tab_dual=RunTab(),
            tab_gate_scan=RunTab(active=True),
            tab_cosweep=RunTab(),
            tab_photocurrent=RunTab(),
        )

        MainWindow._stop_active_sweep_for_lockin_settings(window)

        self.assertEqual(window.tab_gate_scan.stop_count, 1)
        self.assertEqual(window.tab_dual.stop_count, 0)
        self.assertEqual(window.tab_cosweep.stop_count, 0)
        self.assertEqual(window.tab_photocurrent.stop_count, 0)


if __name__ == "__main__":
    unittest.main()
