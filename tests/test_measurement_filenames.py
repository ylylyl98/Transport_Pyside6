import os
import tempfile
import unittest
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6 import QtWidgets
from app.device_manager import DeviceManager
from app.models import Connections, SaveRoot, PhotocurrentBiasCondition
from app.signal_chain import SignalChainSnapshot
from app.ui.main_window import MainWindow
from app.ui.tabs.cosweep_tab import CoSweepTab
from app.ui.tabs.bfield_transport_tab import BFieldTransportTab
from app.ui.tabs.dual_gate_tab import DualGateTab
from app.ui.tabs.gate_scan_tab import GateScanTab
from app.ui.tabs.photocurrent_tab import PhotocurrentTab
from app.workers.photocurrent import PhotocurrentWorker


class MeasurementFilenameTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def tab(self, cls):
        root = tempfile.TemporaryDirectory()
        self.addCleanup(root.cleanup)
        conns = Connections()
        tab = cls(SaveRoot(base=root.name), conns, DeviceManager(conns),
                  get_signal_chain_callable=lambda: SignalChainSnapshot(17, 0.2, 1e-8))
        self.addCleanup(tab.close)
        return tab

    def test_vds_sweep_names_range_fixed_gates_and_round_trip(self):
        tab = self.tab(DualGateTab)
        tab.sp_vds_start.setValue(-1)
        tab.sp_vds_stop.setValue(1)
        tab.sp_vtg.setValue(0.5)
        tab.sp_vbg.setValue(-0.5)
        tab.chk_sweep_bidirectional.setChecked(True)
        self.assertEqual(tab._output_summary_parts(),
                         ["Vds-1to1V", "Vtg0.5V", "Vbg-0.5V", "RT", "17Hz", "LIA200mV", "Pre10nA"])

    def test_gate_scan_keeps_derived_bias_range_and_ratio(self):
        tab = self.tab(GateScanTab)
        tab.rad_mode_derived.setChecked(True)
        tab.rad_sweep_doping.setChecked(True)
        tab.sp_derived_start.setValue(-1)
        tab.sp_derived_stop.setValue(1)
        tab.sp_derived_fixed.setValue(0.2)
        tab.btn_derived_vbias_fixed.setChecked(True)
        tab.sp_derived_vds_fixed.setValue(0.1)
        tab.sp_ratio.setValue(2)
        tab.cbo_ratio_target.setCurrentIndex(tab.cbo_ratio_target.findData("Vtg"))
        tab.chk_sweep_bidirectional.setChecked(False)
        parts = tab._output_summary_parts()
        self.assertEqual(parts, ["Doping-1to1", "E0.2", "Vds0.1V", "rVtg2", "Fwd", "17Hz", "LIA200mV", "Pre10nA"])
        tab.btn_derived_vbias_swept.setChecked(True)
        tab.sp_derived_vds_start.setValue(-0.5)
        tab.sp_derived_vds_stop.setValue(0.5)
        self.assertIn("Vds-0.5to0.5V", tab._output_summary_parts())


    def test_photocurrent_range_and_duplicate_conditions_keep_timestamp_last(self):
        tab = self.tab(PhotocurrentTab)
        tab.sp_wls.setValue(550)
        tab.sp_wle.setValue(740)
        self.assertEqual(tab._output_summary_parts(), ["WL550to740nm", "17Hz", "LIA200mV", "Pre10nA"])
        base = "sample_PC_WL550to740nm_17Hz_LIA200mV_Pre10nA_20260928_210442_02.csv"
        condition = PhotocurrentBiasCondition(vtg=0.0001, vbg=-1, vds=0.1)
        paths = PhotocurrentWorker.condition_csv_paths(base, [condition, condition], True)
        self.assertEqual(len(set(paths)), 2)
        for index, path in enumerate(paths, 1):
            self.assertTrue(path.endswith(f"_C{index:02d}_Vtg0.0001V_Vbg-1V_Vds0.1V_20260928_210442_02.csv"))
        off = PhotocurrentWorker.condition_csv_paths(base, [condition], False)[0]
        self.assertIn("VdsOff", off)

    def test_all_previews_follow_current_signal_chain_settings(self):
        current = [SignalChainSnapshot(17, 0.2, 1e-8)]
        names = [("tab_dual", DualGateTab), ("tab_gate_scan", GateScanTab),
                 ("tab_cosweep", CoSweepTab), ("tab_photocurrent", PhotocurrentTab),
                 ("tab_bfield_transport", BFieldTransportTab)]
        tabs = {}
        for name, cls in names:
            tab = self.tab(cls)
            tab.get_signal_chain = lambda: current[0]
            tabs[name] = tab
        window = SimpleNamespace(**tabs)
        MainWindow._on_signal_chain_changed(window)
        for tab in tabs.values():
            self.assertIn("17Hz_LIA200mV_Pre10nA", tab._planned_output.stem)
        current[0] = SignalChainSnapshot(137, 0.02, 500e-9)
        MainWindow._on_signal_chain_changed(window)
        for tab in tabs.values():
            self.assertIn("137Hz_LIA20mV_Pre500nA", tab._planned_output.stem)
            self.assertNotIn("17Hz_LIA200mV_Pre10nA", tab._planned_output.stem)
