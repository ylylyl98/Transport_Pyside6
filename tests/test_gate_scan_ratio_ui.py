from __future__ import annotations

import os
import tempfile
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6 import QtCore, QtWidgets

from app.device_manager import DeviceManager
from app.gate_transform import RATIO_TARGET_VTG
from app.models import Connections, SaveRoot
from app.plot_x_axis import FOLLOW_SWEEP
from app.ui.tabs.cosweep_tab import CoSweepTab
from app.ui.tabs.gate_scan_tab import GateScanTab


class GateScanRatioUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
        cls.settings_dir = tempfile.TemporaryDirectory()
        QtCore.QSettings.setPath(
            QtCore.QSettings.Format.IniFormat,
            QtCore.QSettings.Scope.UserScope,
            cls.settings_dir.name,
        )

    @classmethod
    def tearDownClass(cls):
        cls.settings_dir.cleanup()

    def _make_tab(self) -> GateScanTab:
        connections = Connections()
        save = SaveRoot(base=self.settings_dir.name)
        return GateScanTab(save, connections, DeviceManager(connections))

    def _make_map_tab(self) -> CoSweepTab:
        connections = Connections()
        save = SaveRoot(base=self.settings_dir.name)
        return CoSweepTab(save, connections, DeviceManager(connections))

    def test_clear_mode_selector_and_ratio_target_persist(self):
        tab = self._make_tab()
        self.assertEqual(tab.control_scroll.minimumWidth(), 390)
        self.assertEqual(tab.control_scroll.maximumWidth(), 500)
        self.assertLessEqual(tab.raw_trajectory_widget.minimumSizeHint().width(), 390)
        self.assertLessEqual(tab.chk_raw_vtg_active.width(), 64)
        self.assertLessEqual(tab.sp_raw_vtg_start.maximumWidth(), 72)
        self.assertLessEqual(tab.derived_trajectory_widget.minimumSizeHint().width(), 390)
        self.assertEqual(
            tab.control_scroll.horizontalScrollBarPolicy(),
            QtCore.Qt.ScrollBarPolicy.ScrollBarAlwaysOff,
        )
        self.assertEqual(tab.rad_mode_raw.text(), "Raw Voltages")
        self.assertEqual(tab.rad_mode_derived.text(), "Doping / E-field")
        self.assertTrue(tab.rad_mode_raw.isChecked())
        self.assertTrue(tab.raw_trajectory_widget.isVisibleTo(tab))

        tab.rad_mode_derived.setChecked(True)
        tab._update_mode_ui()
        self.assertIn("coordinated Vtg and Vbg", tab.lbl_mode_description.text())
        self.assertFalse(tab.raw_trajectory_widget.isVisible())

        tab.cbo_ratio_target.setCurrentIndex(tab.cbo_ratio_target.findData(RATIO_TARGET_VTG))
        self.assertIn("r*Vtg", tab.lbl_ratio_formula.text())
        tab.sp_ratio.setValue(2.0)
        tab.sp_derived_start.setValue(0.0)
        tab.sp_derived_stop.setValue(2.0)
        tab.sp_derived_fixed.setValue(0.0)
        self.assertEqual(tab._derived_gate_endpoints(), [(0.0, 0.0), (0.5, 1.0)])
        self.assertIn("ratio_on_Vtg_r_2", tab._output_summary_parts())
        tab.save_tab_settings()
        tab.close()

        restored = self._make_tab()
        self.assertTrue(restored.rad_mode_derived.isChecked())
        self.assertEqual(restored.cbo_ratio_target.currentData(), RATIO_TARGET_VTG)
        restored.collect_params()
        self.assertEqual(restored.p.derived_ratio_target, RATIO_TARGET_VTG)

        restored._set_mode_selector_enabled(False)
        self.assertFalse(restored.rad_mode_raw.isEnabled())
        self.assertFalse(restored.rad_mode_derived.isEnabled())
        restored.close()

    def test_2d_map_uses_and_persists_the_same_ratio_target(self):
        tab = self._make_map_tab()
        tab.cbo_ratio_target.setCurrentIndex(tab.cbo_ratio_target.findData(RATIO_TARGET_VTG))
        tab.cbo_x.setCurrentIndex(tab.cbo_x.findData("Doping"))
        self.assertIn("r*Vtg", tab.lbl_ratio_formula.text())
        tab.collect_params()
        self.assertEqual(tab.p.ratio_target, RATIO_TARGET_VTG)
        self.assertEqual(tab.p.plot_x_axis, "Doping")
        self.assertEqual(tab.p.plot_x_resolved, "Doping")
        tab.save_tab_settings()
        tab.close()

        restored = self._make_map_tab()
        self.assertEqual(restored.cbo_ratio_target.currentData(), RATIO_TARGET_VTG)
        self.assertEqual(restored.cbo_x.currentData(), "Doping")
        restored.close()

    def test_plot_x_axis_follows_sweep_and_manual_changes_replot_existing_data(self):
        tab = self._make_tab()
        tab.rad_mode_derived.setChecked(True)
        tab.rad_sweep_doping.setChecked(True)
        self.assertEqual(tab.cbo_x.currentData(), FOLLOW_SWEEP)

        tab._plot_records = [
            {
                "index": 0.0, "vtg": 0.1, "vbg": 0.2, "vds": 0.0,
                "doping": 0.5, "efield": -0.3, "direction": "forward",
                "plot_ratio": 2.0, "plot_ratio_target": RATIO_TARGET_VTG,
                "Ids_DC": 1.0, "Ids_X": 2.0, "Ids_Y": 3.0,
            },
            {
                "index": 1.0, "vtg": 0.4, "vbg": 0.5, "vds": 0.0,
                "doping": 1.3, "efield": 0.3, "direction": "forward",
                "plot_ratio": 2.0, "plot_ratio_target": RATIO_TARGET_VTG,
                "Ids_DC": 2.0, "Ids_X": 3.0, "Ids_Y": 4.0,
            },
        ]
        tab._redraw_plot()
        self.assertEqual(list(tab.plot.ax.lines[0].get_xdata()), [0.5, 1.3])
        self.assertIn("Following sweep: Doping", tab.lbl_x_resolved.text())
        self.assertEqual(tab.plot.ax.get_xlabel(), "Doping (2.00*Vtg + Vbg) (V)")

        tab.cbo_x.setCurrentIndex(tab.cbo_x.findData("Vtg"))
        self.assertEqual(list(tab.plot.ax.lines[0].get_xdata()), [0.1, 0.4])
        self.assertIn("Manual override: Vtg", tab.lbl_x_resolved.text())

        tab.plot.set_compare_channels(["Ids_DC", "Ids_X", "Ids_Y"])
        tab.plot.set_selected_plot_mode("4-Channel Compare")
        tab._redraw_plot()
        self.assertEqual(list(tab.plot.get_axes()[-1].lines[0].get_xdata()), [0.1, 0.4])
        self.assertEqual(tab.plot.get_axes()[-1].get_xlabel(), "Vtg (V)")
        tab.close()


if __name__ == "__main__":
    unittest.main()
