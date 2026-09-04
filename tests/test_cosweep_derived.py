import unittest
import os
import tempfile
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PyQt6 import QtCore, QtWidgets

from app.device_manager import DeviceManager
from app.models import Connections, SaveRoot
from app.ui.tabs.cosweep_tab import CoSweepTab
from app.models import CoParams
from app.workers.cosweep import CoSweepWorker, build_cosweep_points, validate_cosweep_params


def derived_params(**overrides):
    values = dict(
        coordinate_mode="Derived",
        axis_fast="Doping",
        axis_slow="E-field",
        doping_start=0.0,
        doping_stop=1.0,
        doping_step=0.5,
        efield_start=0.0,
        efield_stop=1.0,
        efield_step=0.5,
        vds_start=0.1,
        vds_stop=0.1,
        ratio=1.0,
    )
    values.update(overrides)
    return CoParams(**values)


class CoSweepDerivedTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
        cls.settings_dir = tempfile.TemporaryDirectory()
        QtCore.QSettings.setPath(QtCore.QSettings.Format.IniFormat, QtCore.QSettings.Scope.UserScope, cls.settings_dir.name)

    @classmethod
    def tearDownClass(cls):
        cls.settings_dir.cleanup()

    def test_serpentine_derived_grid_and_gate_conversion(self):
        points = build_cosweep_points(derived_params())
        self.assertEqual([(p["doping"], p["efield"]) for p in points], [
            (0.0, 0.0), (0.5, 0.0), (1.0, 0.0),
            (1.0, 0.5), (0.5, 0.5), (0.0, 0.5),
            (0.0, 1.0), (0.5, 1.0), (1.0, 1.0),
        ])
        self.assertEqual((points[1]["vtg"], points[1]["vbg"]), (0.25, 0.25))

    def test_zero_ratio_and_voltage_limit_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "non-zero"):
            validate_cosweep_params(derived_params(ratio=0.0))
        with self.assertRaisesRegex(ValueError, "above"):
            validate_cosweep_params(derived_params(doping_stop=50.0))

    def test_worker_sets_both_gates_for_one_derived_point(self):
        calls = []
        g1 = SimpleNamespace(ramp_voltage=lambda value, step: calls.append(("g1", value, step)))
        g2 = SimpleNamespace(ramp_voltage=lambda value, step: calls.append(("g2", value, step)))
        worker = CoSweepWorker(derived_params(), SimpleNamespace(), SimpleNamespace(), g1=g1, g2=g2)
        worker.set_derived_gates(0.25, -0.25)
        self.assertEqual([call[:2] for call in calls], [("g1", 0.25), ("g2", -0.25)])

    def test_derived_fast_slow_orientation_persists(self):
        connections = Connections()
        save = SaveRoot(base=self.settings_dir.name)
        tab = CoSweepTab(save, connections, DeviceManager(connections))
        tab.cbo_coordinates.setCurrentIndex(1)
        tab.cbo_fast.setCurrentText("E-field")
        tab.cbo_slow.setCurrentText("Doping")
        tab.save_tab_settings()
        tab.close()

        restored = CoSweepTab(save, Connections(), DeviceManager(Connections()))
        self.assertEqual(restored.cbo_coordinates.currentData(), "Derived")
        self.assertEqual(restored.cbo_fast.currentText(), "E-field")
        self.assertEqual(restored.cbo_slow.currentText(), "Doping")
        restored.close()

    def test_output_summary_labels_derived_coordinates_not_gate_setpoints(self):
        connections = Connections()
        save = SaveRoot(base=self.settings_dir.name)
        tab = CoSweepTab(save, connections, DeviceManager(connections))
        tab.cbo_coordinates.setCurrentIndex(1)
        tab.cbo_fast.setCurrentText("Doping")
        tab.cbo_slow.setCurrentText("E-field")
        tab.sp_doping_start.setValue(-1.0)
        tab.sp_doping_stop.setValue(2.0)
        tab.sp_efield_start.setValue(0.25)
        tab.sp_efield_stop.setValue(0.75)
        tab.sp_vds_start.setValue(0.1)
        tab.sp_vds_stop.setValue(0.1)
        derived_parts = tab._output_summary_parts()
        self.assertIn("Doping_-1to2", derived_parts)
        self.assertIn("E-field_0.25to0.75", derived_parts)
        self.assertIn("fixed_Vds_0.1V", derived_parts)
        self.assertFalse(any(part.startswith("fixed_Vtg_") for part in derived_parts))
        self.assertFalse(any(part.startswith("fixed_Vbg_") for part in derived_parts))
        tab.close()

        raw = CoSweepTab(save, Connections(), DeviceManager(Connections()))
        raw.cbo_coordinates.setCurrentIndex(0)
        raw.cbo_fast.setCurrentText("Vtg")
        raw.cbo_slow.setCurrentText("Vbg")
        raw.sp_vtg_start.setValue(-1.0)
        raw.sp_vtg_stop.setValue(1.0)
        raw.sp_vbg_start.setValue(0.0)
        raw.sp_vbg_stop.setValue(0.0)
        raw.sp_vds_start.setValue(0.1)
        raw.sp_vds_stop.setValue(0.1)
        raw_parts = raw._output_summary_parts()
        self.assertIn("Vtg_-1to1V", raw_parts)
        self.assertIn("Vbg_0to0V", raw_parts)
        self.assertIn("fixed_Vds_0.1V", raw_parts)
        raw.close()

    def test_raw_slow_axis_is_set_once_per_row_before_fast_points(self):
        class Gate:
            def __init__(self, name):
                self.name, self.calls, self.voltage = name, [], 0.0
            def ramp_voltage(self, value, _step):
                self.calls.append(float(value))
                self.voltage = float(value)
            def set_voltage(self, value):
                self.voltage = float(value)
            def acquire(self):
                return {"current": 0.0}

        class Daq:
            def acquire(self): pass
            def get_ai_value(self, _index): return 0.0

        with tempfile.TemporaryDirectory() as output_dir:
            params = CoParams(axis_fast="Vtg", axis_slow="Vbg", vtg_start=0, vtg_stop=1, vtg_step=1, vbg_start=0, vbg_stop=1, vbg_step=1, vds_source="Keithley 2400", n_sample=1, delay=0, output_csv_path=os.path.join(output_dir, "map.csv"))
            g1, g2, g3 = Gate("g1"), Gate("g2"), Gate("g3")
            worker = CoSweepWorker(params, SaveRoot(), Connections(), g1=g1, g2=g2, g3=g3, daq=Daq())
            with patch("app.workers.cosweep.time.sleep"), patch("app.workers.cosweep.safe_ramp"):
                worker.run()
            self.assertEqual(g2.calls, [0.0, 1.0])
            self.assertEqual(g1.calls, [0.0, 1.0, 1.0, 0.0])


if __name__ == "__main__":
    unittest.main()
