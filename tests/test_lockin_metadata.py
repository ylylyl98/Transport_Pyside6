import json
import os
import tempfile
import unittest
from dataclasses import dataclass
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6 import QtWidgets
from app.device_manager import DeviceManager
from app.models import Connections, SaveRoot, BFieldTransportParams
from app.signal_chain import SignalChainSnapshot, signal_chain_metadata
from app.ui.tabs.cosweep_tab import CoSweepTab
from app.ui.lockin_panel import LockinPanel
from app.ui.main_window import MainWindow
from app.ui.tabs.gate_scan_tab import GateScanTab
from app.models import LineSweepParams
from app.run_output import write_run_metadata
from app.engine.bfield_transport_sweep import write_series_manifest


class LockinMetadataTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def test_connected_run_captures_settings_and_preserves_values_in_json(self):
        class Lockin:
            model = "SR850"
            identity = "SRS,SR850,123,1.0"
            def read_settings(self):
                return {"frequency_hz": 137, "sensitivity": 20, "time_constant": 10,
                        "time_constant_label": "1 s", "phase_deg": 12.5, "filter_slope": 2,
                        "sine_out_v": 0.01, "ref_source": 0, "harmonic": 1,
                        "reserve": 2, "input_config": 0, "input_ground": 1,
                        "input_coupling": 0, "line_filter": 3, "current_gain": 1}
        manager = DeviceManager(Connections())
        manager.sessions["lockin"] = Lockin()
        manager.states["lockin"] = "ok"
        tab = CoSweepTab(SaveRoot(), manager.connections, manager,
                         get_global_rates_callable=lambda: (1e7, 100),
                         get_signal_chain_callable=lambda: SignalChainSnapshot(137, 0.1, 1e-7))
        self.addCleanup(tab.close)
        captured = signal_chain_metadata(tab.verified_run_calibration()[2])
        self.assertIn("lockin_settings", captured)
        snapshot = captured["lockin_settings"]
        self.assertEqual(snapshot["source"], "instrument")
        self.assertEqual(snapshot["model"], "SR850")
        self.assertEqual(snapshot["address"], manager.connections.lockin)
        self.assertIn("captured_at", snapshot)
        self.assertEqual(snapshot["values"]["phase_deg"], 12.5)
        self.assertEqual(snapshot["values"]["current_gain"], 1)
        self.assertEqual(manager.current_in_use(), set())
        with tempfile.TemporaryDirectory() as root:
            path = os.path.join(root, "metadata.json")
            write_run_metadata(path, {"signal_chain": captured})
            with open(path) as stream:
                saved = json.load(stream)
                self.assertEqual(saved["lockin_settings"], snapshot)
                nested_chain = saved.get("signal_chain", saved.get("validation", {}).get("calibration", {}).get("signal_chain", {}))
                self.assertNotIn("lockin_settings", nested_chain)
            path = os.path.join(root, "manifest.json")
            write_series_manifest(path, params=BFieldTransportParams(),
                                  validation={"calibration": {"signal_chain": captured}})
            with open(path) as stream:
                saved = json.load(stream)
                self.assertEqual(saved["lockin_settings"], snapshot)
                nested_chain = saved.get("signal_chain", saved.get("validation", {}).get("calibration", {}).get("signal_chain", {}))
                self.assertNotIn("lockin_settings", nested_chain)

    def test_disconnected_panel_snapshot_includes_settings_and_saved_source(self):
        panel = LockinPanel(DeviceManager(Connections()))
        self.addCleanup(panel.close)
        panel.sp_phase.setValue(23)
        panel.sp_sine_out.setValue(0.02)
        snapshot = panel.settings_snapshot()
        self.assertEqual(snapshot["source"], "saved/manual")
        self.assertEqual(snapshot["values"]["phase_deg"], 23)
        self.assertEqual(snapshot["values"]["sine_out_v"], 0.02)
        self.assertIn("time_constant_label", snapshot["values"])
        self.assertIn("frequency_hz", snapshot["values"])

    def test_failed_connected_read_blocks_start_instead_of_using_saved_values(self):
        class Lockin:
            def read_settings(self): raise RuntimeError("GPIB read failed")
        manager = DeviceManager(Connections())
        manager.sessions["lockin"] = Lockin()
        manager.states["lockin"] = "ok"
        tab = CoSweepTab(SaveRoot(), manager.connections, manager,
                         get_global_rates_callable=lambda: (1e7, 100))
        self.addCleanup(tab.close)
        with patch.object(QtWidgets.QMessageBox, "warning") as warning:
            self.assertIsNone(tab.verified_run_calibration())
        self.assertIn("GPIB read failed", warning.call_args.args[2])
        self.assertEqual(manager.current_in_use(), set())

    def test_cached_experiment_context_is_detached_without_polling_hardware(self):
        @dataclass
        class Reading:
            monotonic_s: float = 10.0
            field_t: float = 0.3
            sample_temperature_k: float = 1.67
        reading = Reading()
        window = SimpleNamespace(
            magnet_panel=SimpleNamespace(_backend="1000"),
            device_manager=SimpleNamespace(states={"lockin": "ok"}),
            magnet1000=SimpleNamespace(latest_snapshot=reading),
            thermal_safety=SimpleNamespace(latest_snapshot=reading),
            _experiment_2100_field=None, _experiment_2100_temperature=None,
        )
        with patch("time.monotonic", return_value=12.0):
            context = MainWindow.experiment_context_snapshot(window)
        self.assertEqual(context["magnet1000"]["age_s"], 2)
        self.assertEqual(context["temperature"]["values"]["sample_temperature_k"], 1.67)
        reading.sample_temperature_k = 9
        self.assertEqual(context["temperature"]["values"]["sample_temperature_k"], 1.67)
        self.assertFalse(context["magnet2100"]["available"])
        json.dumps(context)

    def test_batch_queries_lockin_once_and_reuses_snapshot_for_subsequent_files(self):
        class Lockin:
            calls = 0
            model = "SR830"
            def read_settings(self):
                self.calls += 1
                return {"phase_deg": 45, "frequency_hz": 17}
        manager = DeviceManager(Connections())
        session = Lockin()
        manager.sessions["lockin"] = session
        manager.states["lockin"] = "ok"
        tab = GateScanTab(SaveRoot(), manager.connections, manager, include_field_batch=True,
                          get_global_rates_callable=lambda: (1e7, 100))
        self.addCleanup(tab.close)
        self.addCleanup(tab.finish_field_batch)
        calibration = tab.verified_run_calibration(capture_settings=False)
        self.assertEqual(session.calls, 0)
        self.assertTrue(tab.begin_field_batch(LineSweepParams(), [], calibration))
        self.assertEqual(session.calls, 1)
        tab.validate_output_ready = lambda _save: True
        tab._validate_required_sessions = lambda: True
        tab._validate_params = lambda: True
        tab.begin_run_logging = lambda *_args: None
        tab.end_run_logging = lambda *_args: None
        captured = []
        def worker(*_args, **kwargs):
            captured.append(kwargs["signal_chain"]["lockin_settings"])
            raise RuntimeError("stop before launching test worker")
        tab._batch_starting = True
        with patch("app.ui.tabs.gate_scan_tab.LineSweepWorker", side_effect=worker):
            tab.start_run()
            tab.start_run()
        self.assertEqual(session.calls, 1)
        self.assertEqual(len(captured), 2)
        self.assertEqual(captured[0], captured[1])
