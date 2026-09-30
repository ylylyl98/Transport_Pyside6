import unittest
import os
import tempfile
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6 import QtCore, QtWidgets

from app.device_manager import DeviceManager
from app.models import Connections, SaveRoot
from app.ui.tabs.cosweep_tab import CoSweepTab
from app.models import CoParams
from app.workers.cosweep import CoSweepWorker, build_cosweep_points, validate_cosweep_params
from app.run_output import write_run_metadata


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

    def test_doping_bias_grid_in_both_orientations(self):
        for fast, slow, expected in [
            ("Doping", "Vds", [(0, -1), (1, -1), (1, 1), (0, 1)]),
            ("Vds", "Doping", [(0, -1), (0, 1), (1, 1), (1, -1)]),
        ]:
            for target in ("Vbg", "Vtg"):
                with self.subTest(fast=fast, target=target):
                    points = build_cosweep_points(derived_params(
                        axis_fast=fast, axis_slow=slow, doping_step=1,
                        efield_start=0.5, efield_stop=99, ratio=2, ratio_target=target,
                        vds_start=-1, vds_stop=1, vds_step=2))
                    self.assertEqual([(p["doping"], p["vds"]) for p in points], expected)
                    self.assertTrue(all(p["efield"] == 0.5 for p in points))
                    first = points[0]
                    self.assertEqual((first["vtg"], first["vbg"]),
                                     (0.25, -0.125) if target == "Vbg" else (0.125, -0.25))

    def test_bias_sweep_rejects_voltage_limit_and_zero_step(self):
        for overrides, message in [({"vds_stop": 50}, "above"),
                                   ({"vds_stop": 1, "vds_step": 0}, "greater than zero")]:
            with self.subTest(overrides=overrides), self.assertRaisesRegex(ValueError, message):
                validate_cosweep_params(derived_params(axis_slow="Vds", **overrides))

    def test_bias_controls_preview_and_settings(self):
        tab = CoSweepTab(SaveRoot(base=self.settings_dir.name), Connections(), DeviceManager(Connections()))
        tab.cbo_coordinates.setCurrentIndex(1)
        tab.cbo_fast.setCurrentText("Vds")
        tab.cbo_slow.setCurrentText("Doping")
        self.assertEqual(tab.cbo_fast.currentText(), "Vds")
        tab.sp_vds_start.setValue(-1)
        tab.sp_vds_stop.setValue(1)
        tab.sp_vds_step.setValue(2)
        tab.sp_doping_start.setValue(0)
        tab.sp_doping_stop.setValue(1)
        tab.sp_doping_step.setValue(1)
        tab.sp_efield_start.setValue(0.5)
        self.assertTrue(tab.sp_vds_stop.isEnabled())
        self.assertFalse(tab.sp_efield_stop.isEnabled())
        self.assertIn("Vds-1to1V", tab._output_summary_parts())
        self.assertIn("E0.5", tab._output_summary_parts())
        tab.collect_params()
        self.assertEqual(len(build_cosweep_points(tab.p)), 4)
        tab.on_preview()
        self.assertEqual(list(tab.plot.ax.lines[0].get_xdata()), [-1, 1, 1, -1])
        self.assertEqual(list(tab.plot.ax.lines[0].get_ydata()), [0, 0, 1, 1])
        self.assertEqual(tab.plot.ax.get_xlabel(), "Vds (V)")
        tab.save_tab_settings()
        tab.close()
        restored = CoSweepTab(SaveRoot(base=self.settings_dir.name), Connections(), DeviceManager(Connections()))
        self.assertEqual((restored.cbo_fast.currentText(), restored.cbo_slow.currentText()), ("Vds", "Doping"))
        self.assertEqual(restored.sp_vds_stop.value(), 1)
        restored.close()

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
        self.assertIn("Doping-1to2", derived_parts)
        self.assertIn("E0.25to0.75", derived_parts)
        self.assertIn("Vds0.1V", derived_parts)
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
        self.assertIn("Vtg-1to1V", raw_parts)
        self.assertIn("Vbg0to0V", raw_parts)
        self.assertIn("Vds0.1V", raw_parts)
        raw.close()

    def test_compact_filename_keeps_conditions_and_orders_fast_then_slow(self):
        from app.signal_chain import SignalChainSnapshot
        snapshot = SignalChainSnapshot(17, 0.2, 10e-9)
        tab = CoSweepTab(SaveRoot(base=self.settings_dir.name, device_id="YZ324"), Connections(),
                         DeviceManager(Connections()), get_signal_chain_callable=lambda: snapshot)
        self.addCleanup(tab.close)
        tab.ed_base.setText("1.67K0T")
        tab.cbo_sweep_dim.setCurrentText("2D map")
        tab.cbo_coordinates.setCurrentIndex(1)
        tab.cbo_fast.setCurrentText("Doping")
        tab.cbo_slow.setCurrentText("Vds")
        tab.sp_doping_start.setValue(-1)
        tab.sp_doping_stop.setValue(1)
        tab.sp_vds_start.setValue(0.9)
        tab.sp_vds_stop.setValue(1.1)
        tab.sp_efield_start.setValue(0)
        tab.sp_ratio.setValue(1)
        tab.cbo_ratio_target.setCurrentIndex(tab.cbo_ratio_target.findData("Vtg"))
        tab._output_run_id = "20260928_210442"
        tab.refresh_output_preview()
        self.assertEqual(tab._planned_output.csv_name,
                         "YZ324_1.67K0T_Doping-1to1_Vds0.9to1.1V_E0_rVtg1_17Hz_LIA200mV_Pre10nA_20260928_210442.csv")
        tab.cbo_fast.setCurrentText("Vds")
        tab.cbo_slow.setCurrentText("Doping")
        self.assertEqual(tab._output_summary_parts()[:2], ["Vds0.9to1.1V", "Doping-1to1"])
        tab.collect_params()
        self.assertEqual(tab.p.vds_source, "Keithley 2400")
        self.assertEqual(tab.p.coordinate_mode, "Derived")
        self.assertEqual((tab.p.axis_fast, tab.p.axis_slow), ("Vds", "Doping"))

    def test_worker_applies_bias_at_each_sample_and_records_it(self):
        import csv
        import json

        class Gate:
            voltage = 0.0
            def ramp_voltage(self, value, _step): self.voltage = value
            def set_voltage(self, value): self.voltage = value
            def acquire(self): return {"current": 0.0}

        class Daq:
            voltage = 0.0
            def __init__(self, bias): self.bias, self.samples, self.channels = bias, [], []
            def ramp_voltage(self, channel, value, _step, delay=None):
                self.channels.append(channel)
                self.voltage = value
            def acquire(self): self.samples.append(self.bias().voltage)
            def get_ai_value(self, _index): return 0.0
            def get_ao_vs_gnd_value(self, channel): return self.voltage

        for source in ("Keithley 2400", "NI DAQ"):
            for fast, slow, expected in [("Doping", "Vds", [-1, -1, 1, 1]),
                                          ("Vds", "Doping", [-1, 1, 1, -1])]:
                with self.subTest(source=source, fast=fast), tempfile.TemporaryDirectory() as output_dir:
                    params = derived_params(axis_fast=fast, axis_slow=slow, doping_step=1,
                                            efield_start=0.5, vds_start=-1, vds_stop=1, vds_step=2,
                                            vds_source=source, ao_channel=1, n_sample=1, delay=0,
                                            output_csv_path=os.path.join(output_dir, "map.csv"),
                                            output_metadata_path=os.path.join(output_dir, "map_metadata.json"))
                    g1, g2, g3 = Gate(), Gate(), Gate()
                    daq = Daq(lambda: g3 if source == "Keithley 2400" else daq)
                    chain = {"frequency_hz": 17, "lockin_sensitivity_v": 0.2, "preamp_sensitivity_a": 1e-8}
                    worker = CoSweepWorker(params, SaveRoot(), Connections(), g1=g1, g2=g2, g3=g3, daq=daq, signal_chain=chain)
                    errors = []
                    timings = []
                    worker.timing_updated.connect(timings.append)
                    worker.error.connect(errors.append)
                    with patch("app.workers.cosweep.time.sleep"), patch("app.workers.cosweep.safe_ramp"), patch("app.workers.cosweep.write_run_metadata", wraps=write_run_metadata) as metadata_writer:
                        worker.run()
                    self.assertEqual(metadata_writer.call_count, 1)
                    self.assertEqual([item["phase"] for item in timings], ["sampling"] * 4 + ["cleanup", "done"])
                    self.assertEqual([item["completed"] for item in timings[:4]], [1, 2, 3, 4])
                    self.assertGreaterEqual(timings[-1]["elapsed"], timings[-2]["elapsed"])
                    self.assertEqual(errors, [])
                    self.assertEqual(daq.samples, expected)
                    with open(params.output_metadata_path, encoding="utf-8") as stream:
                        metadata = json.load(stream)
                    self.assertEqual(metadata["signal_chain"], chain)
                    self.assertEqual(metadata["params"]["vds_source"], source)
                    self.assertEqual(metadata["params"]["axis_fast"], fast)
                    self.assertEqual(metadata["params"]["axis_slow"], slow)
                    self.assertEqual(metadata["params"]["efield_start"], 0.5)
                    with open(params.output_csv_path, newline="") as stream:
                        reader = csv.DictReader(stream)
                        next(reader)  # CSV units row
                        rows = list(reader)
                    self.assertEqual([float(row["Vds"]) for row in rows], expected)
                    self.assertEqual([float(row["E-field"]) for row in rows], [0.5] * 4)
                    if source == "NI DAQ":
                        self.assertTrue(daq.channels)
                        self.assertEqual(set(daq.channels), {1})

    def test_time_estimate_updates_with_delay_and_averages(self):
        tab = CoSweepTab(SaveRoot(base=self.settings_dir.name), Connections(), DeviceManager(Connections()))
        tab.cbo_coordinates.setCurrentIndex(1)
        tab.cbo_fast.setCurrentText("Doping")
        tab.cbo_slow.setCurrentText("Vds")
        tab.sp_doping_start.setValue(0)
        tab.sp_doping_stop.setValue(1)
        tab.sp_doping_step.setValue(0.5)
        tab.sp_vds_start.setValue(0)
        tab.sp_vds_stop.setValue(1)
        tab.sp_vds_step.setValue(0.5)
        tab.sp_delay.setValue(0)
        tab.sp_nsamp.setValue(1)
        before = tab.lbl_sweep_summary.text()
        self.assertIn("Estimated total:", before)
        self.assertTrue(hasattr(tab, "lbl_eta"), "ETA must have a dedicated visible run-panel label")
        self.assertIs(tab.lbl_eta.parentWidget(), tab.run_panel)
        self.assertIn("ETA (estimated total):", tab.lbl_eta.text())
        first_eta = tab.lbl_eta.text()
        tab.resize(1100, 700)
        tab.show()
        self.app.processEvents()
        self.assertTrue(tab.lbl_eta.isVisible())
        self.assertTrue(tab.run_panel.rect().contains(tab.lbl_eta.geometry()))
        tab.control_scroll.verticalScrollBar().setValue(tab.control_scroll.verticalScrollBar().maximum())
        self.assertTrue(tab.lbl_eta.isVisible())

        tab.sp_delay.setValue(2)
        after = tab.lbl_sweep_summary.text()
        self.assertNotEqual(before, after)
        self.assertNotEqual(first_eta, tab.lbl_eta.text())
        tab.sp_nsamp.setValue(100)
        self.assertNotEqual(after, tab.lbl_sweep_summary.text())
        tab.close()

    def test_eta_uses_matching_history_and_shows_small_average_changes(self):
        conns = Connections(gate1="GPIB0::2::INSTR", gate2="GPIB0::3::INSTR",
                            gate3="GPIB0::1::INSTR", lockin="GPIB0::8::INSTR")
        tab = CoSweepTab(SaveRoot(base=self.settings_dir.name, device_id="YZ324"),
                         conns, DeviceManager(conns))
        self.addCleanup(tab.close)
        tab.cbo_sweep_dim.setCurrentText("2D map")
        tab.cbo_coordinates.setCurrentIndex(1)
        tab.cbo_fast.setCurrentText("Vds")
        tab.cbo_slow.setCurrentText("Doping")
        for widget, value in ((tab.sp_doping_start, -1), (tab.sp_doping_stop, 1),
                              (tab.sp_doping_step, 0.2), (tab.sp_vds_start, 0.9),
                              (tab.sp_vds_stop, 1.1), (tab.sp_vds_step, 0.002),
                              (tab.sp_efield_start, 0), (tab.sp_delay, 0.4),
                              (tab.sp_nsamp, 1), (tab.sp_ratio, 1)):
            widget.setValue(value)
        tab.cbo_source.setCurrentText("Keithley 2400")
        # Calibration is selected without reading the old experiment files.
        with patch("builtins.open", side_effect=AssertionError("ETA must not read files")):
            tab._update_sweep_summary()
            self.assertIn("historical calibration", tab.lbl_eta.text())
            before = tab.lbl_eta.text()
            tab.sp_nsamp.setValue(2)
            self.assertNotEqual(before, tab.lbl_eta.text())
            self.assertIn("interpolated", tab.lbl_eta.toolTip())
            self.assertIn("Not a measured per-read duration", tab.lbl_eta.toolTip())
            tab.save.device_id = "other"
            tab._update_sweep_summary()
            self.assertIn("model estimate", tab.lbl_eta.text())
        from app.ui.dock import ConnDock
        from app.ui.main_window import MainWindow
        with patch.object(ConnDock, "_start_scan"):
            dock = ConnDock()
        self.addCleanup(dock.close)
        for widget, text in ((dock.cbo_g1, conns.gate1), (dock.cbo_g2, conns.gate2),
                             (dock.cbo_g3, conns.gate3), (dock.cbo_lockin, conns.lockin),
                             (dock.cbo_daq, conns.daq_dev)):
            widget.setCurrentText(text)
        dock.ed_device_id.setText("YZ324")
        window_stub = SimpleNamespace(conn_dock=dock, tab_cosweep=tab,
                                      _on_save_settings_edited=lambda: None)
        MainWindow._bind_save_preview_updates(window_stub)
        tab._update_sweep_summary()
        self.assertIn("Ave interpolated", tab.lbl_eta.text())
        dock.cbo_g1.setCurrentText("GPIB0::9::INSTR")
        self.assertIn("model estimate", tab.lbl_eta.text())
        self.assertEqual(conns.gate1, "GPIB0::2::INSTR", "Preview must not change live connections")

    def test_live_eta_survives_setup_edits_pause_and_cleanup(self):
        from app.workers.cosweep_timing import LiveCoSweepTiming
        tab = CoSweepTab(SaveRoot(base=self.settings_dir.name), Connections(), DeviceManager(Connections()))
        self.addCleanup(tab.close)
        tab._live_eta = LiveCoSweepTiming(10, 25, 5)
        tab._on_live_timing({"completed": 1, "elapsed": 2, "phase": "sampling"})
        self.assertIn("Remaining:", tab.lbl_eta.text())
        first = tab.lbl_eta.text()
        tab.sp_delay.setValue(10)
        self.assertEqual(tab.lbl_eta.text(), first)
        tab._on_live_timing({"completed": 1, "elapsed": 2, "phase": "paused"})
        self.assertIn("Paused", tab.lbl_eta.text())
        tab._on_live_timing({"completed": 10, "elapsed": 20, "phase": "sampling"})
        self.assertIn("Remaining: ~5.00 s", tab.lbl_eta.text())
        tab.on_finished("map.csv")
        self.assertNotEqual(tab.run_panel.lbl_status.text(), "Finished")
        tab._on_live_timing({"completed": 10, "elapsed": 20, "phase": "cleanup"})
        self.assertIn("returning outputs to zero", tab.lbl_eta.text())
        self.assertNotIn("Finished", tab.lbl_eta.text())
        tab._on_live_timing({"completed": 10, "elapsed": 27, "phase": "done"})
        self.assertIn("Finished | Active elapsed: 27.00 s", tab.lbl_eta.text())
        self.assertEqual(tab.run_panel.lbl_status.text(), "Finished")
        tab._on_live_timing({"completed": 10, "elapsed": 27, "phase": "cleanup_failed"})
        self.assertIn("Zero return not confirmed", tab.lbl_eta.text())

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
            params = CoParams(axis_fast="Vtg", axis_slow="Vbg", vtg_start=0, vtg_stop=1, vtg_step=1, vbg_start=0, vbg_stop=1, vbg_step=1, vds_source="Keithley 2400", n_sample=1, delay=0, output_csv_path=os.path.join(output_dir, "map.csv"),
                                            output_metadata_path=os.path.join(output_dir, "map_metadata.json"))
            g1, g2, g3 = Gate("g1"), Gate("g2"), Gate("g3")
            worker = CoSweepWorker(params, SaveRoot(), Connections(), g1=g1, g2=g2, g3=g3, daq=Daq())
            with patch("app.workers.cosweep.time.sleep"), patch("app.workers.cosweep.safe_ramp"):
                worker.run()
            self.assertEqual(g2.calls, [0.0, 1.0])
            self.assertEqual(g1.calls, [0.0, 1.0, 1.0, 0.0])


if __name__ == "__main__":
    unittest.main()
