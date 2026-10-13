import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

if os.environ.get("TRANSPORT_OFFLINE_VERIFICATION") != "1":
    raise RuntimeError("Use verify_offline.py")

class ResolutionTests(unittest.TestCase):
    def test_range_and_model_are_required(self):
        from app.voltage_resolution import source_resolution
        for value, expected in ((.2, 5e-6), (2, 50e-6), (20, .0005), (200, .005)):
            self.assertEqual(source_resolution(SimpleNamespace(identity="KEITHLEY INSTRUMENTS INC.,MODEL 2400,123,A", cached_source_voltage_range_v=value)), expected)
        self.assertIsNone(source_resolution(SimpleNamespace(identity="MODEL 2450", cached_source_voltage_range_v=2)))
        self.assertIsNone(source_resolution(SimpleNamespace(identity="MODEL 2400", cached_source_voltage_range_v=None)))

    def test_2401_ranges_and_identity_whitespace(self):
        from app.voltage_resolution import source_resolution
        for model in ("2400", "2401"):
            for value, expected in ((.2, 5e-6), (2, 50e-6), (20, .0005)):
                for scale in (1, 1.05):
                    with self.subTest(model=model, value=value, scale=scale):
                        source = SimpleNamespace(identity=f"KEITHLEY INSTRUMENTS INC., MODEL {model} ,123,A",
                                                 cached_source_voltage_range_v=value * scale)
                        self.assertEqual(source_resolution(source), expected)
        for value in (200, 210):
            self.assertIsNone(source_resolution(SimpleNamespace(identity="MODEL 2401", cached_source_voltage_range_v=value)))

    def test_unknown_resolution_explains_model_range_and_read_error(self):
        from app.voltage_resolution import source_resolution_status
        for identity, value, expected in (("MODEL 2450", 2, "unsupported identity"),
                                          ("MODEL 24010", 2, "unsupported identity"),
                                          ("MODEL 2401", 200, "unsupported cached range"),
                                          ("MODEL 2400", "bad", "unsupported cached range"),
                                          ("MODEL 2400", float("nan"), "unsupported cached range"),
                                          ("MODEL 2400", float("inf"), "unsupported cached range"),
                                          ("MODEL 2400", -20, "unsupported cached range")):
            with self.subTest(identity=identity, value=value):
                status = source_resolution_status(SimpleNamespace(identity=identity, cached_source_voltage_range_v=value))
                self.assertIsNone(status.quantum)
                self.assertFalse(status.refreshable)
                self.assertIn(expected, status.message)
                self.assertIn(identity, status.message)
        status = source_resolution_status(SimpleNamespace(identity="MODEL 2401", cached_source_voltage_range_v=None,
                                                          cached_source_voltage_range_error="VISA timeout"))
        self.assertTrue(status.refreshable)
        self.assertIn("VISA timeout", status.message)

    def test_raw_step_and_fixed_value_checked_without_rounding(self):
        from app.voltage_resolution import validate_voltage_points
        validate_voltage_points([{"vtg":0,"vbg":0,"vds":.00005}], {"vds":.00005})
        with self.assertRaisesRegex(ValueError, "Vds"):
            validate_voltage_points([{"vtg":0,"vbg":0,"vds":.00001}], {"vds":.00005})

    def test_derived_grid_checked_after_conversion(self):
        from app.voltage_resolution import validate_voltage_points
        from app.workers.cosweep import build_cosweep_points
        from app.models import CoParams
        p=CoParams(coordinate_mode="Derived",axis_fast="Doping",axis_slow="Vds",doping_start=0,doping_stop=.0001,doping_step=.0001,efield_start=0,ratio=2,ratio_target="Vtg",vds_start=0,vds_stop=0,vds_step=.001)
        with self.assertRaisesRegex(ValueError, "Vtg"):
            validate_voltage_points(build_cosweep_points(p), {"vtg":.00005,"vbg":.00005})

    def test_fine_step_does_not_allocate_millions_of_points(self):
        from app.models import CoParams
        from app.workers.cosweep import build_cosweep_points
        with patch("app.workers.cosweep._frange_inc", side_effect=AssertionError("allocated")):
            with self.assertRaisesRegex(ValueError,"250,000"):
                build_cosweep_points(CoParams(axis_fast="Vds",axis_slow="None",vds_start=0,vds_stop=20,vds_step=.000001))

    def test_voltage_entry_preserves_six_decimals(self):
        from app.ui.helpers import configure_volt_spinbox
        from app.ui.widgets.safe_spinbox import SafeDoubleSpinBox
        box=SafeDoubleSpinBox(); configure_volt_spinbox(box,.000005,decimals=6)
        self.assertEqual(box.value(),.000005)

    def test_cached_range_readback_and_failure_invalidate(self):
        from test_keithley_protection import FakeKeithley
        source=FakeKeithley(max_voltage=2)
        source.read_protection_settings()
        self.assertEqual(source.cached_source_voltage_range_v,2)
        with patch.object(source,"_query",side_effect=RuntimeError("offline fake failure")):
            with self.assertRaises(RuntimeError): source.read_protection_settings()
        self.assertIsNone(source.cached_source_voltage_range_v)
        self.assertIn("offline fake failure", source.cached_source_voltage_range_error)
        source.read_protection_settings()
        self.assertEqual(source.cached_source_voltage_range_v, 2)
        self.assertEqual(source.cached_source_voltage_range_error, "")

    def test_read_keeps_previous_range_until_complete_and_rejects_invalid_readback(self):
        from test_keithley_protection import FakeKeithley
        source = FakeKeithley(max_voltage=2)
        source.read_protection_settings()
        original_query = source._query
        observed = []

        def query(command):
            observed.append(source.cached_source_voltage_range_v)
            return original_query(command)

        source.reported_voltage_range = 20
        with patch.object(source, "_query", side_effect=query):
            source.read_protection_settings(include_trip=False)
        self.assertEqual(observed, [2] * 4)
        self.assertEqual(source.cached_source_voltage_range_v, 20)
        for value in (0, -20, float("nan"), float("inf")):
            with self.subTest(value=value):
                source.reported_voltage_range = value
                with self.assertRaisesRegex(ValueError, "Invalid source voltage range"):
                    source.read_protection_settings()
                self.assertIsNone(source.cached_source_voltage_range_v)

    def test_ui_input_preview_and_start_guard(self):
        from app.ui.main_window import MainWindow
        with patch("app.ui.dock.ConnDock._start_scan"):
            window=MainWindow()
        try:
            tab=window.tab_cosweep
            tab.sp_vds_step.setValue(.00005)
            self.assertEqual(tab.sp_vds_step.value(),.00005)
            self.assertIn("0.00005",tab.sp_vds_step.text())
            self.assertIn("unconfirmed",tab.lbl_precision.text())
            fake=SimpleNamespace(identity="MODEL 2400",cached_source_voltage_range_v=2)
            with patch.dict(tab.device_manager.sessions, {"g3":fake}):
                tab.device_manager.gate_currents_read.emit({}, "fake cached readback")
                self.assertIn("5e-05 V",tab.lbl_precision.text())
                tab.cbo_sweep_dim.setCurrentText("1D sweep")
                tab.cbo_fast.setCurrentText("Vds")
                tab.sp_vds_start.setValue(0)
                tab.sp_vds_stop.setValue(.0001)
                tab.sp_vds_step.setValue(.00001)
                tab._update_sweep_summary()
                self.assertIn("not representable",tab.lbl_precision.text())
                from app.ui.measurement_workflow import parameter_issues
                self.assertTrue(any("not representable" in message for _,message in parameter_issues(tab)))
                tab.sp_vds_step.setValue(.00005)
                tab._update_sweep_summary()
                self.assertEqual(tab._precision_error, "")
            tab._update_sweep_summary()
            with patch.object(tab,"verified_run_calibration") as calibration, patch("app.ui.tabs.cosweep_tab.QtWidgets.QMessageBox.warning") as warning:
                tab.start_run()
                calibration.assert_not_called()
                warning.assert_called_once()
        finally:
            window.close()


class ResolutionStartupTests(unittest.TestCase):
    def setUp(self):
        from app.device_manager import DeviceManager
        from app.models import Connections, SaveRoot
        from app.settings import get_app_settings
        from app.ui.tabs.cosweep_tab import CoSweepTab
        from test_keithley_protection import FakeKeithley
        # verify_offline routes these settings to a temporary directory.
        get_app_settings().remove("tabs/map_2d")
        self.directory = tempfile.TemporaryDirectory()
        self.manager = DeviceManager(Connections())
        for name in ("g1", "g2", "g3"):
            limit, compliance = self.manager._protection_for(name)
            source = FakeKeithley(current_compliance=compliance, max_voltage=limit)
            source._identity = f"KEITHLEY INSTRUMENTS INC.,MODEL {'2401' if name == 'g3' else '2400'},123,A"
            source.acquire = lambda: {"voltage": 0, "current": 0}
            source.read_protection_settings()
            self.manager.sessions[name] = source
            self.manager.states[name] = "ok"
            self.manager._connected_protections[name] = (limit, compliance)
        self.manager.sessions["daq"] = SimpleNamespace()
        self.manager.states["daq"] = "ok"
        self.tab = CoSweepTab(SaveRoot(base=self.directory.name), self.manager.connections, self.manager)
        self.tab.sp_vtg_stop.setValue(.001)
        self.tab.sp_vtg_step.setValue(.001)
        self.tab.sp_vbg_stop.setValue(.001)
        self.tab.sp_vbg_step.setValue(.001)
        self.tab.sp_vds_start.setValue(-.2)
        self.warning_patch = patch("app.ui.tabs.cosweep_tab.QtWidgets.QMessageBox.warning")
        self.warning = self.warning_patch.start()
        self.calibration_patch = patch.object(self.tab, "verified_run_calibration", return_value=None)
        self.calibration = self.calibration_patch.start()

    def tearDown(self):
        from PySide6 import QtCore, QtWidgets
        if self.manager._gate_current_worker is not None:
            self.manager._gate_current_worker.wait(5000)
        QtWidgets.QApplication.processEvents()
        if self.tab.worker_thread is not None:
            self.tab._cleanup_thread()
        self.tab._preview_timer.stop()
        self.tab.close()
        self.tab.deleteLater()
        QtWidgets.QApplication.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)
        self.calibration_patch.stop()
        self.warning_patch.stop()
        self.directory.cleanup()

    def wait_for_read(self):
        from PySide6 import QtTest
        for _ in range(500):
            if not self.manager.is_busy():
                break
            QtTest.QTest.qWait(10)
        self.assertFalse(self.manager.is_busy())

    def test_connected_2401_map_starts_with_fixed_vds(self):
        from PySide6 import QtCore
        from app.signal_chain import SignalChainSnapshot
        from app.workers.cosweep import CoSweepWorker
        self.calibration.return_value = (1, 1, SignalChainSnapshot())
        with patch.object(self.tab, "begin_run_logging"), \
             patch.object(CoSweepWorker, "moveToThread"), patch.object(QtCore.QThread, "start") as start:
            self.tab.start_run()
        self.warning.assert_not_called()
        start.assert_called_once()
        self.assertEqual(self.tab.worker.p.vds_start, -.2)
        self.assertEqual(self.tab.worker.p.vds_stop, -.2)
        self.assertIn("g3", self.manager.current_in_use())

    def test_busy_read_is_reported_before_unknown_resolution(self):
        self.manager.sessions["g3"].cached_source_voltage_range_v = None
        with patch.object(self.manager, "is_busy", return_value=True), \
             patch.object(self.manager, "read_gate_currents") as read:
            self.tab.start_run()
        self.assertEqual(self.warning.call_args.args[1], "Busy")
        read.assert_not_called()
        self.calibration.assert_not_called()

    def test_missing_source_and_wrong_mode_have_specific_messages(self):
        self.manager.states["g3"] = "idle"
        self.tab.start_run()
        self.assertEqual(self.warning.call_args.args[1], "Missing Device")
        self.manager.states["g3"] = "ok"
        self.manager._connected_modes["g3"] = "ohm_4w"
        self.tab.start_run()
        self.assertEqual(self.warning.call_args.args[1], "Keithley Mode")
        self.calibration.assert_not_called()

    def test_missing_range_refreshes_off_thread_and_start_can_be_retried(self):
        from threading import get_ident
        source = self.manager.sessions["g3"]
        source.cached_source_voltage_range_v = None
        source.cached_source_voltage_range_error = "previous timeout"
        original_read = source.read_protection_settings
        thread_ids = []

        def read(*args, **kwargs):
            thread_ids.append(get_ident())
            return original_read(*args, **kwargs)

        with patch.object(source, "read_protection_settings", side_effect=read):
            self.tab.start_run()
            self.assertIsNone(self.tab.worker_thread)
            self.assertFalse(self.tab.btn_start.isEnabled())
            self.tab.start_run()  # A second click cannot queue a second read/run.
            self.wait_for_read()
        self.assertEqual(len(thread_ids), 1)
        self.assertNotEqual(thread_ids[0], get_ident())
        self.assertIn("Press Start", self.tab.run_panel.lbl_status.text())
        self.assertTrue(self.tab.btn_start.isEnabled())
        self.assertFalse(self.tab._precision_refresh_sessions)
        self.calibration.assert_not_called()
        self.warning.assert_not_called()
        self.assertEqual(source.commands, [])
        self.tab.start_run()
        self.calibration.assert_called_once()

    def test_interrupted_refresh_clears_pending_start_without_launching(self):
        self.manager.sessions["g3"].cached_source_voltage_range_v = None
        with patch.object(self.manager, "read_gate_currents", return_value=True):
            self.tab.start_run()
        with patch.object(self.manager, "is_busy", return_value=True):
            self.manager.gate_currents_read.emit({}, "Interrupted for emergency zero")
        self.assertFalse(self.tab._precision_refresh_sessions)
        self.assertIn("interrupted", self.tab.run_panel.lbl_status.text())
        self.assertIsNone(self.tab.worker_thread)
        self.calibration.assert_not_called()
        self.warning.assert_not_called()

    def test_refused_refresh_does_not_leave_start_blocked(self):
        self.manager.sessions["g3"].cached_source_voltage_range_v = None
        with patch.object(self.manager, "read_gate_currents", return_value=False):
            self.tab.start_run()
        self.assertFalse(self.tab._precision_refresh_sessions)
        self.assertTrue(self.tab.btn_start.isEnabled())
        self.assertIn("cached range unavailable", self.warning.call_args.args[2])
        self.calibration.assert_not_called()

    def test_failed_range_refresh_reports_failure_and_does_not_start(self):
        source = self.manager.sessions["g3"]
        source.cached_source_voltage_range_v = None
        with patch.object(source, "_query", side_effect=RuntimeError("VISA timeout")):
            self.tab.start_run()
            self.wait_for_read()
        self.assertIn("G3: VISA timeout", self.warning.call_args.args[2])
        self.assertFalse(self.tab._precision_refresh_sessions)
        self.assertIsNone(self.tab.worker_thread)
        self.calibration.assert_not_called()

    def test_unsupported_model_and_invalid_target_still_block_start(self):
        source = self.manager.sessions["g3"]
        source._identity = "MODEL 2450"
        with patch.object(self.manager, "read_gate_currents") as read:
            self.tab.start_run()
        self.assertIn("G3 / Vds", self.warning.call_args.args[2])
        self.assertIn("MODEL 2450", self.warning.call_args.args[2])
        self.assertIn("20.0 V", self.warning.call_args.args[2])
        read.assert_not_called()
        source._identity = "MODEL 2401"
        self.tab.sp_vds_start.setValue(.000001)
        self.tab.start_run()
        self.assertIn("not representable", self.warning.call_args.args[2])
        self.calibration.assert_not_called()

    def test_daq_source_does_not_require_g3_resolution(self):
        self.manager.sessions["g3"] = None
        self.tab.cbo_source.setCurrentText("NI DAQ ao0")
        self.tab.start_run()
        self.warning.assert_not_called()
        self.calibration.assert_called_once()

    def test_cache_invalidated_during_calibration_blocks_worker_and_releases_devices(self):
        from app.signal_chain import SignalChainSnapshot

        def calibrate():
            self.manager.sessions["g3"].cached_source_voltage_range_v = None
            return (1, 1, SignalChainSnapshot())

        self.calibration.side_effect = calibrate
        self.tab.start_run()
        self.assertIn("cached range unavailable", self.warning.call_args.args[2])
        self.assertIsNone(self.tab.worker_thread)
        self.assertEqual(self.manager.current_in_use(), set())
