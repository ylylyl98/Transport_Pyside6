import os
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
