"""Offline regressions for planned ranges, acquired ranges and live navigation."""
import os
import unittest
from contextlib import ExitStack
from unittest.mock import patch

if os.environ.get("TRANSPORT_OFFLINE_VERIFICATION") != "1":
    raise unittest.SkipTest("Use verify_offline.py to isolate hardware and settings")

import numpy as np
from PySide6 import QtCore, QtWidgets

from app.models import CoParams, Connections, LineSweepParams, SaveRoot
from app.plot_ranges import (ACQUIRED_DATA, FULL_SWEEP, coordinate_ranges,
                             finite_bounds, padded_limits, stepped_sweep_bounds)
from app.ui.widgets.plot_widget import PlotWidget
from app.utils import _frange_inc
from app.workers.cosweep import build_cosweep_points
from app.workers.line_sweep import LineSweepWorker


class TrajectoryRangeTests(unittest.TestCase):
    def test_nonfinite_values_do_not_set_bounds(self):
        self.assertIsNone(finite_bounds([np.nan, np.inf, -np.inf]))
        self.assertIsNone(finite_bounds([]))
        self.assertEqual(finite_bounds([np.nan, -2, 3, np.inf]), (-2, 3))

    def test_padding_handles_reverse_and_constant_coordinates(self):
        self.assertEqual(padded_limits((10, 0)), (-0.3, 10.3))
        for value in (0, -2, 500):
            low, high = padded_limits((value, value))
            self.assertLess(low, value)
            self.assertGreater(high, value)
        with self.assertRaises(ValueError):
            padded_limits((np.nan, 2))

    def test_voltage_bounds_cover_the_worker_sequence(self):
        for start, stop, step in ((0, 1, .3), (1, 0, -.3), (0, 1, .6),
                                  (1, 0, -.6), (-1, 1, .25), (2, 2, .1), (1, 2, 0)):
            with self.subTest(start=start, stop=stop, step=step):
                points = _frange_inc(start, stop, step)
                low, high = stepped_sweep_bounds(start, stop, step)
                self.assertAlmostEqual(low, min(points))
                self.assertAlmostEqual(high, max(points))

    def test_empty_trajectory_has_no_invented_extent(self):
        self.assertEqual(coordinate_ranges([]), {})

    def test_raw_reverse_and_round_trip_step_indices(self):
        params = LineSweepParams(raw_vtg_start=2, raw_vtg_stop=-1,
                                 raw_vbg_active=True, raw_vbg_start=-3,
                                 raw_vbg_stop=1, n_points=5, sweep_both_ways=True)
        worker = LineSweepWorker(params, SaveRoot(), Connections())
        self.addCleanup(worker.deleteLater)
        ranges = coordinate_ranges(worker._build_trajectory(), total_count=10)
        self.assertEqual(ranges["Vtg"], (-1, 2))
        self.assertEqual(ranges["Vbg"], (-3, 1))
        self.assertEqual(ranges["Step Index"], (0, 9))

    def test_derived_range_uses_transformed_execution_coordinates(self):
        for target in ("Vtg", "Vbg"):
            with self.subTest(target=target):
                params = LineSweepParams(mode="Derived", derived_axis="Doping",
                                         derived_ratio=-2, derived_ratio_target=target,
                                         derived_start=4, derived_stop=-2,
                                         derived_fixed=1, n_points=4)
                worker = LineSweepWorker(params, SaveRoot(), Connections())
                self.addCleanup(worker.deleteLater)
                points = worker._build_trajectory()
                ranges = coordinate_ranges(points)
                self.assertEqual(ranges["Doping"], (-2, 4))
                self.assertEqual(ranges["E-field"], (1, 1))
                for axis, key in (("Vtg", "vtg"), ("Vbg", "vbg")):
                    self.assertEqual(ranges[axis], finite_bounds([p[key] for p in points]))

    def test_map_bounds_include_additional_regions_and_snake_rows(self):
        params = CoParams(axis_fast="Vtg", axis_slow="Vbg", vtg_start=1,
                          vtg_stop=0, vtg_step=.5, vbg_start=-1, vbg_stop=1,
                          vbg_step=1, regions=[dict(vtg_start=1, vtg_stop=2,
                          vtg_step=.5, vbg_start=0, vbg_stop=1, vbg_step=1)])
        points = build_cosweep_points(params)
        ranges = coordinate_ranges(points)
        self.assertEqual(ranges["Vtg"], (0, 2))
        self.assertEqual(ranges["Vbg"], (-1, 1))
        self.assertEqual(ranges["Step Index"], (0, len(points) - 1))


class LivePlotRangeTests(unittest.TestCase):
    def setUp(self):
        self.plot = PlotWidget()
        self.addCleanup(self.close_plot)

    def close_plot(self):
        self.plot.close()
        self.plot.deleteLater()
        app = QtWidgets.QApplication.instance()
        app.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)

    def draw_points(self, xs, ys):
        with self.plot.redraw():
            self.plot.ax.clear()
            self.plot.ax.plot(xs, ys, "o-")
        self.plot.canvas.draw()

    def assert_limits(self, bounds):
        np.testing.assert_allclose(self.plot.ax.get_xlim(), padded_limits(bounds))

    def test_full_range_is_present_before_the_first_sample_and_stays_fixed(self):
        self.plot.set_planned_ranges({"x": (0, 10)})
        self.assert_limits((0, 10))
        self.assertEqual(len(self.plot.ax.lines), 0)
        for xs in ([0], [0, 1], [0, 1, 3]):
            self.draw_points(xs, np.ones(len(xs)))
            self.assert_limits((0, 10))
            self.assertEqual(list(self.plot.ax.lines[0].get_xdata()), xs)

    def test_y_autoscales_only_to_measured_values(self):
        self.plot.set_planned_ranges({"x": (0, 1000)})
        self.draw_points([0, 1], [20, 21])
        low, high = self.plot.ax.get_ylim()
        self.assertGreater(low, 19)
        self.assertLess(high, 22)
        self.draw_points([0, 1, 2], [20, 21, 100])
        self.assertGreater(self.plot.ax.get_ylim()[1], 100)

    def test_acquired_range_follows_actual_points_and_can_return_to_full(self):
        self.plot.set_planned_ranges({"x": (-10, 10)})
        self.draw_points([0, 1], [1, 2])
        self.plot.set_x_range_mode(ACQUIRED_DATA)
        self.assert_limits((0, 1))
        self.draw_points([0, 1, 3], [1, 2, 3])
        self.assert_limits((0, 3))
        self.plot.set_x_range_mode(FULL_SWEEP)
        self.assert_limits((-10, 10))
        self.assertEqual(self.plot.btn_x_range.text(), "X range: Full sweep")

    def test_unplanned_data_uses_dynamic_finite_bounds_even_for_missing_y(self):
        self.draw_points([1, 3, np.nan], [np.nan, np.nan, np.nan])
        self.assert_limits((1, 3))

    def test_actual_readback_outside_the_plan_remains_visible(self):
        self.plot.set_planned_ranges({"x": (-1, 1)})
        self.draw_points([-1.2, 1.3], [1, 2])
        self.assert_limits((-1.2, 1.3))

    def test_small_readback_deviations_use_the_existing_margin(self):
        self.plot.set_planned_ranges({"x": (-1, 1)})
        self.draw_points([-1.02, 1.03], [1, 2])
        self.assert_limits((-1, 1))

    def test_live_redraw_keeps_manual_x_and_y_zoom_until_home(self):
        self.plot.set_planned_ranges({"x": (0, 10)})
        self.draw_points([0, 1], [20, 21])
        self.plot.ax.set_xlim(.5, 2)
        self.plot.ax.set_ylim(19, 22)
        self.draw_points([0, 1, 4], [20, 21, 100])
        self.assertEqual(self.plot.ax.get_xlim(), (.5, 2))
        self.assertEqual(self.plot.ax.get_ylim(), (19, 22))
        self.plot.toolbar.home()
        self.assert_limits((0, 10))
        self.assertGreater(self.plot.ax.get_ylim()[1], 100)

    def test_toolbar_rectangle_zoom_and_history_survive_new_samples(self):
        from matplotlib.backend_bases import MouseEvent
        self.plot.set_planned_ranges({"x": (0, 10)})
        self.draw_points([0, 10], [0, 10])
        toolbar, canvas = self.plot.toolbar, self.plot.canvas
        toolbar.zoom()
        start = self.plot.ax.transData.transform((2, 2))
        stop = self.plot.ax.transData.transform((6, 6))
        toolbar.press_zoom(MouseEvent("button_press_event", canvas, *start, button=1))
        toolbar.release_zoom(MouseEvent("button_release_event", canvas, *stop, button=1))
        toolbar.zoom()
        manual = self.plot.ax.get_xlim(), self.plot.ax.get_ylim()
        self.assertGreater(manual[0][0], 1)
        self.assertLess(manual[0][1], 7)
        self.draw_points([0, 10, 5], [0, 10, 100])
        self.assertEqual((self.plot.ax.get_xlim(), self.plot.ax.get_ylim()), manual)
        toolbar.back()
        self.assert_limits((0, 10))
        toolbar.forward()
        self.assertEqual((self.plot.ax.get_xlim(), self.plot.ax.get_ylim()), manual)
        toolbar.home()
        self.assert_limits((0, 10))
        self.assertGreater(self.plot.ax.get_ylim()[1], 100)

    def test_toolbar_pan_survives_new_samples(self):
        from matplotlib.backend_bases import MouseEvent
        self.plot.set_planned_ranges({"x": (0, 10)})
        self.draw_points([0, 10], [0, 10])
        toolbar, canvas = self.plot.toolbar, self.plot.canvas
        original = self.plot.ax.get_xlim()
        toolbar.pan()
        start = self.plot.ax.transData.transform((4, 4))
        stop = self.plot.ax.transData.transform((6, 6))
        toolbar.press_pan(MouseEvent("button_press_event", canvas, *start, button=1))
        toolbar.drag_pan(MouseEvent("motion_notify_event", canvas, *stop, buttons={1}))
        toolbar.release_pan(None)
        toolbar.pan()
        manual = self.plot.ax.get_xlim(), self.plot.ax.get_ylim()
        self.assertNotEqual(manual[0], original)
        self.draw_points([0, 10, 5], [0, 10, 100])
        self.assertEqual((self.plot.ax.get_xlim(), self.plot.ax.get_ylim()), manual)

    def test_callbacks_are_rebound_after_each_axes_clear(self):
        self.draw_points([0, 1], [0, 1])
        self.draw_points([0, 1, 2], [0, 1, 2])
        self.plot.ax.set_xlim(2, 1)
        self.draw_points([0, 1, 2, 3], [0, 1, 2, 3])
        self.assertEqual(self.plot.ax.get_xlim(), (2, 1))

    def test_four_channels_share_x_but_have_independent_y_zoom(self):
        self.plot.set_compare_channels(["I_drag_X", "I_drag_Y", "I_drive_X", "I_drive_Y"], grid=True)
        self.plot.set_selected_plot_mode("4-Channel Compare")
        self.plot.set_planned_ranges({"x": (-2, 2)})
        with self.plot.redraw():
            for index, axis in enumerate(self.plot.axes):
                axis.clear()
                axis.plot([0, 1], [np.nan, np.nan] if index == 1 else [index, index + 1])
        for axis in self.plot.axes:
            np.testing.assert_allclose(axis.get_xlim(), padded_limits((-2, 2)))
        self.plot.axes[2].set_xlim(.1, .9)
        self.plot.axes[0].set_ylim(-10, 10)
        with self.plot.redraw():
            for index, axis in enumerate(self.plot.axes):
                axis.clear()
                axis.plot([0, 1, 2], [index, index + 1, index + 2])
        for axis in self.plot.axes:
            self.assertEqual(axis.get_xlim(), (.1, .9))
        self.assertEqual(self.plot.axes[0].get_ylim(), (-10, 10))
        self.assertLess(self.plot.axes[3].get_ylim()[1], 6)

    def test_changing_x_coordinate_discards_zoom_and_uses_its_plan(self):
        self.plot.set_planned_ranges({"Doping": (-4, 4), "Vtg": (-1, 1)}, axis="Doping")
        self.plot.ax.set_xlim(-.2, .2)
        with self.plot.redraw():
            self.plot.ax.clear()
            self.plot.set_x_axis_key("Vtg")
        self.assert_limits((-1, 1))

    def test_channel_change_resets_y_zoom_but_keeps_x_zoom(self):
        self.draw_points([0, 1], [1, 2])
        self.plot.ax.set_xlim(.1, .9)
        self.plot.ax.set_ylim(-10, 10)
        self.plot.set_selected_y_axis("I_drive_X")
        self.draw_points([0, 1], [100, 200])
        self.assertEqual(self.plot.ax.get_xlim(), (.1, .9))
        self.assertGreater(self.plot.ax.get_ylim()[0], 90)

    def test_new_run_resets_old_plan_and_zoom_but_preserves_range_choice(self):
        self.plot.set_x_range_mode(ACQUIRED_DATA)
        self.plot.set_planned_ranges({"x": (-10, 10)})
        self.draw_points([0, 1], [2, 3])
        self.plot.ax.set_xlim(.3, .5)
        self.plot.clear(reset_plan=True)
        self.plot.set_planned_ranges({"x": (400, 800)})
        self.draw_points([500, 600], [1, 2])
        self.assert_limits((500, 600))
        self.plot.set_x_range_mode(FULL_SWEEP)
        self.assert_limits((400, 800))

    def test_worker_clear_preserves_current_execution_plan(self):
        self.plot.set_planned_ranges({"x": (400, 800)})
        self.plot.clear()
        self.assert_limits((400, 800))

    def test_redraw_depth_recovers_after_an_exception(self):
        with self.assertRaisesRegex(RuntimeError, "render failed"):
            with self.plot.redraw():
                raise RuntimeError("render failed")
        self.assertEqual(self.plot._view_update_depth, 0)
        with patch.object(self.plot, "_apply_view_limits", side_effect=RuntimeError("limits failed")):
            with self.assertRaises(RuntimeError):
                with self.plot.redraw():
                    pass
        self.assertEqual(self.plot._view_update_depth, 0)


class MeasurementLiveRangeTests(unittest.TestCase):
    def test_scan_start_installs_the_execution_range_before_worker_start(self):
        from app.signal_chain import SignalChainSnapshot
        from app.ui.main_window import MainWindow
        from app.ui.tabs.gate_scan_tab import GateScanTab
        from app.workers.cosweep import CoSweepWorker
        from app.workers.dual_gate import DualGateWorker
        from app.workers.photocurrent import PhotocurrentWorker

        with patch("app.ui.dock.ConnDock._start_scan"):
            window = MainWindow()
        try:
            window.tab_dual.sp_vds_start.setValue(1)
            window.tab_dual.sp_vds_stop.setValue(-1)
            window.tab_dual.sp_vds_step.setValue(.6)
            window.tab_gate_scan.sp_raw_vtg_start.setValue(2)
            window.tab_gate_scan.sp_raw_vtg_stop.setValue(-1)
            window.tab_cosweep.sp_vtg_start.setValue(-2)
            window.tab_cosweep.sp_vtg_stop.setValue(2)
            window.tab_photocurrent.sp_wls.setValue(800)
            window.tab_photocurrent.sp_wle.setValue(400)
            cases = [(window.tab_dual, DualGateWorker),
                     (window.tab_gate_scan, LineSweepWorker),
                     (window.tab_cosweep, CoSweepWorker),
                     (window.tab_photocurrent, PhotocurrentWorker)]
            for tab, worker_class in cases:
                with self.subTest(tab=type(tab).__name__), ExitStack() as stack:
                    for method, result in (("verified_run_calibration", (1, 1, SignalChainSnapshot())),
                                           ("validate_output_ready", True),
                                           ("_validate_required_sessions", True),
                                           ("claim_run_devices", (True, [])),
                                           ("begin_run_logging", None)):
                        stack.enter_context(patch.object(tab, method, return_value=result))
                    if tab is window.tab_cosweep:
                        stack.enter_context(patch.object(tab, "_precision_check", return_value="offline"))
                    stack.enter_context(patch.object(window, "refresh_models_from_ui"))
                    stack.enter_context(patch.object(worker_class, "moveToThread", return_value=None))
                    start = stack.enter_context(patch.object(QtCore.QThread, "start"))
                    warning = stack.enter_context(patch.object(QtWidgets.QMessageBox, "warning"))
                    tab.start_run()
                    try:
                        warning.assert_not_called()
                        start.assert_called_once()
                        if isinstance(tab, GateScanTab):
                            ranges = coordinate_ranges(tab.worker._build_trajectory(),
                                total_count=tab.p.n_points * (2 if tab.p.sweep_both_ways else 1))
                        elif tab is window.tab_cosweep:
                            ranges = coordinate_ranges(build_cosweep_points(tab.p))
                        elif tab is window.tab_dual:
                            ranges = {"x": finite_bounds(_frange_inc(tab.p.vds_start, tab.p.vds_stop,
                                tab.p.vds_step if tab.p.vds_stop >= tab.p.vds_start else -abs(tab.p.vds_step)))}
                        else:
                            ranges = {"x": (400, 800)}
                        expected = ranges[tab.plot._x_axis_key]
                        np.testing.assert_allclose(tab.plot.ax.get_xlim(), padded_limits(expected))
                        self.assertFalse(tab._plot_records)
                        self.assertFalse(any(axis.lines for axis in tab.plot.axes))
                    finally:
                        thread = tab.worker_thread
                        tab._cleanup_thread()
                        if thread is not None:
                            thread.deleteLater()
        finally:
            window.close()
            window.deleteLater()
            app = QtWidgets.QApplication.instance()
            app.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)
            app.processEvents()

    def test_all_measurement_views_preserve_zoom_when_a_point_arrives(self):
        from app.ui.main_window import MainWindow
        from test_drag_drive_plots import record

        with patch("app.ui.dock.ConnDock._start_scan"):
            window = MainWindow()
        try:
            for name, tab in window._plot_mode_tabs().items():
                with self.subTest(tab=name):
                    tab.plot.set_planned_ranges({key: (0, 10) for key in
                        ("x", "Vtg", "Vbg", "Vds", "Doping", "E-field", "Step Index", "B-field")})
                    tab.plot.ax.set_xlim(.1, .9)
                    tab.plot.ax.set_ylim(-1, 1)
                    if tab is window.tab_bfield_transport:
                        window.bfield_transport_controller._results.append(record(1))
                        window.bfield_transport_controller.refresh_plot(force=True)
                    else:
                        tab.on_point_data(record(1))
                    self.assertEqual(tab.plot.ax.get_xlim(), (.1, .9))
                    self.assertEqual(tab.plot.ax.get_ylim(), (-1, 1))
                    tab.plot.toolbar.home()
                    np.testing.assert_allclose(tab.plot.ax.get_xlim(), padded_limits((0, 10)))
        finally:
            window.close()
            window.deleteLater()
            app = QtWidgets.QApplication.instance()
            app.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)
            app.processEvents()
