"""Offline sweep previews reuse acquisition trajectories without touching hardware."""
from copy import deepcopy
import os
import threading
import time
import unittest
from unittest.mock import patch

if os.environ.get("TRANSPORT_OFFLINE_VERIFICATION") != "1":
    raise unittest.SkipTest("Run through verify_offline.py")

import numpy as np
from PySide6.QtCore import QCoreApplication
from app.models import CoParams, Connections, DualGateParams, LineSweepParams, PhotocurrentParams
from app.quick.plot import PlotModel
from app.quick.sweep_preview import SweepPreview, _compress_path, _map_path, build_sweep_preview
from app.workers.cosweep import build_cosweep_points
from app.workers.line_sweep import LineSweepWorker
from app.models import SaveRoot


class QuickSweepPreviewTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QCoreApplication.instance() or QCoreApplication([])

    def test_map_preview_preserves_planner_order_and_region_moves(self):
        params = CoParams(axis_fast='Vtg', axis_slow='Vbg', vtg_start=0, vtg_stop=1, vtg_step=.5,
                          vbg_start=0, vbg_stop=1, vbg_step=.5,
                          regions=[dict(vtg_start=.5, vtg_stop=1.5, vtg_step=.5,
                                        vbg_start=.5, vbg_stop=1.5, vbg_step=.5)])
        planned = build_cosweep_points(params)
        before = deepcopy(params)
        result = build_sweep_preview(2, params, Connections())
        self.assertEqual(params, before)
        self.assertEqual(result['pointCount'], len(planned))
        expected = _compress_path(_map_path(planned, params))
        np.testing.assert_array_equal(result['traces'][0]['x'], expected[:, 0])
        np.testing.assert_array_equal(result['traces'][0]['y'], expected[:, 1])
        self.assertEqual([t['label'] for t in result['traces'] if t['label'].startswith('Region ')], ['Region 1', 'Region 2'])
        self.assertEqual([t['label'] for t in result['traces'] if t.get('marker') in ('start', 'end')], ['Start', 'End'])
        # Every planned region movement is axis aligned; no diagonal shortcuts.
        for a, b in zip(expected, expected[1:]):
            self.assertTrue(a[0] == b[0] or a[1] == b[1])

    def test_compression_preserves_turns_reversals_and_nan_gaps(self):
        path = [(0, 0), (1, 0), (2, 0), (1, 0), (1, 1), (float('nan'), float('nan')),
                (10, 2), (11, 2), (12, 2)]
        actual = _compress_path(path)
        expected = np.asarray([(0, 0), (2, 0), (1, 0), (1, 1), (float('nan'), float('nan')),
                               (10, 2), (12, 2)])
        np.testing.assert_array_equal(actual, expected)

    def test_small_map_prefers_actual_sample_coordinates_as_ticks(self):
        params = CoParams(axis_fast='Vtg', axis_slow='Vbg', vtg_start=0., vtg_stop=1., vtg_step=.25,
                          vbg_start=0., vbg_stop=1., vbg_step=.25)
        points = build_cosweep_points(params)
        result = build_sweep_preview(2, params, Connections())
        self.assertEqual(result['tickValues']['x'], sorted({p['fast_value'] for p in points}))
        self.assertEqual(result['tickValues']['y'], sorted({p['slow_value'] for p in points}))
        self.assertEqual(result['tickValues']['x'], [0., .25, .5, .75, 1.])
        self.assertEqual(result['tickValues']['y'], [0., .25, .5, .75, 1.])
        scatter = next(trace for trace in result['traces'] if trace.get('style') == 'scatter')
        self.assertEqual(result['tickValues']['x'], sorted(set(scatter['x'])))
        self.assertEqual(result['tickValues']['y'], sorted(set(scatter['y'])))

    def test_scatter_is_actual_samples_for_200_and_201_vds_points(self):
        from app.utils import _frange_inc
        for count in (200, 201):
            with self.subTest(count=count):
                params = DualGateParams(vds_start=0., vds_stop=(count-1)/100., vds_step=.01)
                values = _frange_inc(params.vds_start, params.vds_stop, params.vds_step)
                result = build_sweep_preview(0, params, Connections())
                scatter = [trace for trace in result['traces'] if trace.get('style') == 'scatter']
                paths = [trace for trace in result['traces'] if trace.get('style') == 'line']
                self.assertEqual(len(scatter), 1)
                self.assertTrue(paths)
                self.assertEqual(len(values), count)
                np.testing.assert_allclose(scatter[0]['x'], values)
                np.testing.assert_array_equal(scatter[0]['y'], np.arange(count))
                self.assertEqual(result['pointCount'], count)

    def test_scatter_retains_reverse_samples_and_region_moves_are_not_samples(self):
        from app.utils import _frange_inc
        params = DualGateParams(vds_start=0., vds_stop=1., vds_step=.25, sweep_both_ways=True)
        points = _frange_inc(0., 1., .25)
        points += list(reversed(points))
        result = build_sweep_preview(0, params, Connections())
        scatter = next(trace for trace in result['traces'] if trace.get('style') == 'scatter')
        np.testing.assert_allclose(scatter['x'], points)
        np.testing.assert_array_equal(scatter['y'], np.arange(len(points)))

        params = CoParams(axis_fast='Vtg', axis_slow='Vbg', vtg_start=0, vtg_stop=1, vtg_step=.5,
                          vbg_start=0, vbg_stop=1, vbg_step=.5,
                          regions=[dict(vtg_start=.5, vtg_stop=1.5, vtg_step=.5,
                                        vbg_start=.5, vbg_stop=1.5, vbg_step=.5)])
        points = build_cosweep_points(params)
        result = build_sweep_preview(2, params, Connections())
        scatter = next(trace for trace in result['traces'] if trace.get('style') == 'scatter')
        np.testing.assert_array_equal(scatter['x'], [p['fast_value'] for p in points])
        np.testing.assert_array_equal(scatter['y'], [p['slow_value'] for p in points])
        self.assertEqual(len(scatter['x']), len(points))
        self.assertTrue(all(trace.get('style') == 'line' for trace in result['traces']
                            if trace['label'].startswith('Region ')))

        gate = LineSweepParams(n_points=3, raw_vtg_start=-1., raw_vtg_stop=1., sweep_both_ways=True)
        forward = LineSweepWorker(gate, SaveRoot(), Connections())._build_trajectory()
        samples = forward + list(reversed(forward))
        result = build_sweep_preview(1, gate, Connections())
        scatter = [trace for trace in result['traces'] if trace.get('style') == 'scatter']
        self.assertEqual(len(scatter), 3)
        for trace, key in zip(scatter, ('vtg', 'vbg', 'vds')):
            np.testing.assert_allclose(trace['x'], [p['vtg'] for p in samples])
            np.testing.assert_allclose(trace['y'], [p[key] for p in samples])

    def test_large_sweeps_hide_scatter_without_changing_point_count(self):
        params = DualGateParams(vds_start=0., vds_stop=2.001, vds_step=.001)
        result = build_sweep_preview(0, params, Connections())
        self.assertEqual(result['pointCount'], 2002)
        self.assertFalse(any(trace.get('style') == 'scatter' for trace in result['traces']))
        self.assertTrue(any(trace.get('style') == 'line' for trace in result['traces']))
        self.assertIn('sample markers', result['summary'].lower())
        self.assertTrue(any(word in result['summary'].lower() for word in ('hidden', 'not shown', 'omitted')))

    def test_derived_map_and_single_point_gate_use_actual_planners(self):
        params = CoParams(coordinate_mode='Derived', axis_fast='Doping', axis_slow='E-field',
                          doping_start=0., doping_stop=1., doping_step=.5,
                          efield_start=0., efield_stop=1., efield_step=.5)
        points = build_cosweep_points(params)
        result = build_sweep_preview(2, params, Connections())
        self.assertEqual(result['pointCount'], len(points))
        self.assertIn('Doping', result['xLabel'])
        self.assertIn('Physical Vtg', result['summary'])
        gate = LineSweepParams(n_points=1, raw_vtg_start=2., raw_vtg_stop=8.)
        result = build_sweep_preview(1, gate, Connections())
        points = LineSweepWorker(gate, SaveRoot(), Connections())._build_trajectory()
        self.assertEqual(result['pointCount'], 1)
        self.assertEqual(result['traces'][0]['y'][0], points[0]['vtg'])
        self.assertEqual(result['traces'][0]['x'][0], points[0]['vtg'])
        self.assertIn('Vtg', result['xLabel'])
        self.assertEqual(points[0]['vtg'], 2.)

    def test_vds_reverse_and_photo_ranges_are_labeled(self):
        result = build_sweep_preview(0, DualGateParams(vds_start=0, vds_stop=1, vds_step=.6,
                                                      sweep_both_ways=True), Connections())
        self.assertEqual(result['pointCount'], 6)
        self.assertIn('Vds', result['xLabel'])
        self.assertIn('Point order', result['yLabel'])
        self.assertIn(1.2, result['traces'][0]['x'])
        self.assertEqual(result['traces'][0]['x'][0], 0.)
        self.assertEqual(result['traces'][0]['x'][-1], 0.)
        photo = PhotocurrentParams(wl_start=500, wl_stop=501, wl_step=.6)
        result = build_sweep_preview(3, photo, Connections())
        self.assertEqual(result['pointCount'], 3)
        self.assertIn('Wavelength', result['xLabel'])
        self.assertIn('Point order', result['yLabel'])
        self.assertEqual(result['traces'][0]['x'][-1], 501)

    def test_round_trip_duplicate_turn_survives_path_compression(self):
        for mode in ("Raw", "Derived"):
            with self.subTest(mode=mode):
                params = LineSweepParams(mode=mode, n_points=5, sweep_both_ways=True,
                                         raw_vtg_active=True, raw_vtg_start=-1., raw_vtg_stop=1.,
                                         derived_axis="Doping", derived_start=-1., derived_stop=1.,
                                         derived_fixed=0.)
                worker = LineSweepWorker(params, SaveRoot(), Connections())
                forward = worker._build_trajectory()
                result = build_sweep_preview(1, params, Connections())
                trace = result["traces"][0]
                planned_x = [p["vtg"] if mode == "Raw" else p["doping"] for p in forward]
                planned_y = [p["vtg"] for p in forward]
                self.assertAlmostEqual(min(trace["x"]), min(planned_x))
                self.assertAlmostEqual(max(trace["x"]), max(planned_x))
                self.assertAlmostEqual(min(trace["y"]), min(planned_y))
                self.assertAlmostEqual(max(trace["y"]), max(planned_y))
                self.assertEqual((trace["x"][0], trace["y"][0]), (planned_x[0], planned_y[0]))
                self.assertEqual((trace["x"][-1], trace["y"][-1]), (planned_x[0], planned_y[0]))
                self.assertTrue(any(x == planned_x[-1] and y == planned_y[-1]
                                    for x, y in zip(trace["x"], trace["y"])),
                                "The path itself must retain the forward Stop before returning")

    def test_duplicate_turn_and_nan_breaks_are_preserved(self):
        path = [(0., 0.), (1., 1.), (1., 1.), (0., 0.),
                (float("nan"), float("nan")), (2., 2.), (3., 3.), (3., 3.), (2., 2.)]
        expected = np.asarray([(0., 0.), (1., 1.), (0., 0.),
                               (float("nan"), float("nan")), (2., 2.), (3., 3.), (2., 2.)])
        np.testing.assert_array_equal(_compress_path(path), expected)

    def test_start_end_markers_remain_distinct_and_turn_is_forward_stop(self):
        gate = LineSweepParams(n_points=3, raw_vtg_start=0., raw_vtg_stop=2., sweep_both_ways=True)
        result = build_sweep_preview(1, gate, Connections())
        markers = {trace["label"]: trace for trace in result["traces"] if trace.get("marker")}
        start, end, turn = markers["Start"], markers["End"], markers["Stop / turn"]
        self.assertEqual((start["marker"], end["marker"], turn["marker"]), ("start", "end", "turn"))
        self.assertEqual((start["x"][0], start["y"][0]), (end["x"][0], end["y"][0]))
        self.assertNotEqual((turn["x"][0], turn["y"][0]), (end["x"][0], end["y"][0]))
        self.assertEqual((turn["x"][0], turn["y"][0]), (2., 2.))
        self.assertIn("same position", result["summary"])

    def test_axes_follow_real_1d_map_and_gate_coordinates(self):
        for axis in ("Vtg", "Vds"):
            params = CoParams(axis_fast=axis, axis_slow="None")
            if axis == "Vtg":
                params.vtg_start, params.vtg_stop, params.vtg_step = 1., -1., 1.
            else:
                params.vds_start, params.vds_stop, params.vds_step = 1., -1., 1.
            points = build_cosweep_points(params)
            result = build_sweep_preview(2, params, Connections())
            self.assertIn(axis, result["xLabel"])
            self.assertIn("Point order", result["yLabel"])
            self.assertEqual(result["traces"][0]["x"][0], points[0]["fast_value"])
            self.assertEqual(result["traces"][0]["x"][-1], points[-1]["fast_value"])
            self.assertEqual(result["traces"][0]["y"][0], 0)
            self.assertEqual(result["traces"][0]["y"][-1], len(points) - 1)

        for axis in ("Doping", "E-field"):
            params = LineSweepParams(mode="Derived", derived_axis=axis, derived_start=1., derived_stop=-1.,
                                     derived_fixed=.5, n_points=3)
            planned = LineSweepWorker(params, SaveRoot(), Connections())._build_trajectory()
            result = build_sweep_preview(1, params, Connections())
            self.assertIn(axis, result["xLabel"])
            key = "doping" if axis == "Doping" else "efield"
            self.assertEqual(result["traces"][0]["x"][0], planned[0][key])
            self.assertEqual(result["traces"][0]["x"][-1], planned[-1][key])
            self.assertEqual(result["traces"][0]["y"][0], planned[0]["vtg"])
            self.assertEqual(result["traces"][0]["y"][-1], planned[-1]["vtg"])
        params = LineSweepParams(raw_vtg_active=True, raw_vbg_active=True, n_points=3)
        result = build_sweep_preview(1, params, Connections())
        self.assertIn("Step Index", result["xLabel"])
        self.assertEqual(result["traces"][0]["x"][0], 0.)
        self.assertEqual(result["traces"][0]["x"][-1], 2.)

    def test_visible_preview_auto_follows_latest_and_ignores_hidden_or_invalid(self):
        from app.quick.measurements import MeasurementService
        from tests.test_quick_measurements import FakeManager
        service = MeasurementService(FakeManager(), SaveRoot())
        preview = service.sweepPreview
        started, release = threading.Event(), threading.Event()
        seen = []

        def build(mode, params, connections):
            seen.append((mode, getattr(params, "vds_start", None), getattr(params, "raw_vtg_start", None)))
            if len(seen) == 1:
                started.set()
                if not release.wait(5):
                    raise RuntimeError("test worker was not released")
            return dict(traces=[dict(x=[0., 1.], y=[0., 1.])], xLabel="x", yLabel="y",
                        summary=str(seen[-1]), title="test", pointCount=2)

        def wait(predicate, timeout=4.):
            deadline = time.monotonic() + timeout
            while not predicate() and time.monotonic() < deadline:
                self.app.processEvents()
                time.sleep(.005)
            self.assertTrue(predicate())

        def drain(seconds):
            deadline = time.monotonic() + seconds
            while time.monotonic() < deadline:
                self.app.processEvents()
                time.sleep(.005)

        try:
            with patch("app.quick.sweep_preview.build_sweep_preview", side_effect=build):
                service.setValue("vds_start", .1)
                drain(.45)
                self.assertEqual(seen, [], "hidden preview must not calculate")
                service.setSweepPreviewVisible(True)
                wait(started.is_set)
                service.setValue("vds_start", .2)
                service.setValue("vds_start", .3)
                release.set()
                wait(lambda: len(seen) >= 2 and not preview.busy and not preview.stale)
                self.assertEqual(seen[-1][:2], (0, .3))
                self.assertIn("0.3", preview.summary)
                self.assertEqual(len(seen), 2, "rapid committed edits should be coalesced")
                service.mode = 4
                service.addRow("conditions")
                service.duplicateRow("conditions", 0)
                service.setRowValue("conditions", 1, "raw_vtg_start", 4.)
                service.selectRow("conditions", 1)
                wait(lambda: len(seen) >= 3 and seen[-1][0] == 4 and not preview.busy and not preview.stale)
                self.assertEqual(seen[-1][2], 4.)
                service.setRowValue("conditions", 1, "raw_vtg_start", "invalid")
                drain(.8)
                self.assertTrue(preview.error)
                count = len(seen)
                drain(.8)
                self.assertEqual(len(seen), count, "invalid input must not start a retry loop")
                service.setSweepPreviewVisible(False)
                service.setRowValue("conditions", 1, "raw_vtg_start", 3.)
                drain(.5)
                self.assertEqual(len(seen), count)
                service._batch_claimed = True
                service.setSweepPreviewVisible(True)
                drain(.5)
                self.assertEqual(len(seen), count, "active acquisition must not rebuild preview")
                service._batch_claimed = False
        finally:
            release.set()
            preview._pool.waitForDone(5000)
            self.app.processEvents()
    def test_service_preview_modes_dirty_validation_and_active_lock(self):
        from app.quick.measurements import MeasurementService
        from tests.test_quick_measurements import FakeManager
        manager = FakeManager()
        service = MeasurementService(manager, SaveRoot())
        preview = service.sweepPreview
        deadline_seconds = 5

        def wait():
            deadline = time.monotonic() + deadline_seconds
            while preview.busy and time.monotonic() < deadline:
                self.app.processEvents()
                time.sleep(.002)
            self.assertFalse(preview.busy)

        try:
            with patch.object(manager, "mark_in_use", side_effect=AssertionError("preview claimed hardware")), \
                 patch.object(manager, "get_session", side_effect=AssertionError("preview queried hardware")):
                for mode in range(6):
                    with self.subTest(mode=mode):
                        service.mode = mode
                        service.requestSweepPreview()
                        wait()
                        self.assertEqual(preview.error, "")
                        self.assertFalse(preview.stale)
                        self.assertTrue(preview.plotModel.title)
                        self.assertTrue(preview.summary)
                service.mode = 4
                service.addRow("conditions")
                service.duplicateRow("conditions", 0)
                service.selectRow("conditions", 1)
                model = service.rowModel("conditions")
                uid = model.data(model.index(1, 0), model.UidRole)
                model.rememberDraft(uid, "raw_vtg_start", "2")
                service.requestSweepPreview()
                wait()
                self.assertEqual(service._values[4]["conditions"][1]["raw_vtg_start"], 2.)
                self.assertEqual(service._values[4]["conditions"][0]["raw_vtg_start"], 0.)
                self.assertEqual(preview.plotModel._traces[0].columns["y"].values[0], 2.)
                self.assertEqual(preview.plotModel.title, "Gate scan preview")
                service.setRowValue("conditions", 1, "raw_vtg_start", "bad")
                self.assertTrue(preview.stale)
                service.requestSweepPreview()
                self.assertIn("Correct", preview.error)
                service.setRowValue("conditions", 1, "raw_vtg_start", 2.)
                service.requestSweepPreview()
                wait()
                self.assertFalse(preview.stale)
                service._batch_claimed = True
                service.requestSweepPreview()
                self.assertFalse(preview.busy)
                self.assertIn("cleanup", service.message.lower())
                service._batch_claimed = False
        finally:
            service._batch_claimed = False
            preview._pool.waitForDone(5000)
            self.app.processEvents()

    def test_use_gate_regions_preserves_rows_and_rejects_invalid_draft(self):
        from app.quick.measurements import MeasurementService
        from tests.test_quick_measurements import FakeManager
        service = MeasurementService(FakeManager(), SaveRoot())
        service.mode = 2
        service.addRow("regions")
        model = service.rowModel("regions")
        uid = model.data(model.index(0, 0), model.UidRole)
        self.assertTrue(model.commit(uid, "vtg_start", .25))
        service.setValue("coordinate_mode", "Derived")
        before = deepcopy(service._values[2]["regions"])
        model.rememberDraft(uid, "vtg_start", "invalid")
        service.useGateRegions()
        self.assertEqual(service._values[2]["coordinate_mode"], "Derived")
        self.assertEqual(service._values[2]["regions"], before)
        model.rememberDraft(uid, "vtg_start", ".25")
        service.useGateRegions()
        values = service._values[2]
        self.assertEqual((values["coordinate_mode"], values["axis_fast"], values["axis_slow"]), ("Raw", "Vtg", "Vbg"))
        self.assertEqual(values["regions"], before)

    def test_maximum_vds_point_count_includes_both_passes(self):
        params = DualGateParams(vds_start=0., vds_stop=1., vds_step=1. / 249999., sweep_both_ways=True)
        result = build_sweep_preview(0, params, Connections())
        self.assertEqual(result["pointCount"], 500000)
        self.assertIn("500,000", result["summary"])
        self.assertIn("return pass", result["summary"])
        self.assertLessEqual(len(result["traces"][0]["x"]), 20000)

    def test_large_preview_shows_actual_end_when_path_display_is_limited(self):
        params = CoParams(axis_fast="Vtg", axis_slow="Vbg", vtg_start=0., vtg_stop=1., vtg_step=1.,
                          vbg_start=0., vbg_stop=10., vbg_step=.0005)
        planned = build_cosweep_points(params)
        result = build_sweep_preview(2, params, Connections())
        self.assertEqual(result["pointCount"], len(planned))
        self.assertIn("limited", result["summary"].lower())
        self.assertLessEqual(len(result["traces"][0]["x"]), 20000)
        end = next(t for t in result["traces"] if t["label"] == "End")
        self.assertEqual((end["x"][0], end["y"][0]), (planned[-1]["fast_value"], planned[-1]["slow_value"]))

    def test_async_snapshot_invalidation_and_live_plot_independence(self):
        preview = SweepPreview()
        live = PlotModel()
        live.setTraces([dict(x=[0., 1.], y=[2., 3.])])
        self.assertIsNot(preview.plotModel, live)
        started, release = threading.Event(), threading.Event()
        seen = []

        def blocked(mode, params, connections):
            seen.append((threading.get_ident(), params.vds_start))
            started.set()
            if not release.wait(5):
                raise RuntimeError('worker test timed out')
            return dict(traces=[dict(x=[0., 1.], y=[0., 1.])], xLabel='x', yLabel='y',
                        summary='new snapshot', title='test', pointCount=2)

        def wait(predicate):
            deadline = time.monotonic() + 5
            while not predicate() and time.monotonic() < deadline:
                self.app.processEvents()
                time.sleep(.002)
            self.assertTrue(predicate())

        params = DualGateParams(vds_start=.5)
        try:
            with patch('app.quick.sweep_preview.build_sweep_preview', side_effect=blocked):
                self.assertTrue(preview.request(0, params, Connections()))
                self.assertTrue(started.wait(2))
                self.assertNotEqual(seen[0][0], threading.get_ident())
                params.vds_start = 9.
                self.assertEqual(seen[0][1], .5)
                self.assertFalse(preview.request(0, params, Connections()))
                preview.invalidate()
                release.set()
                wait(lambda: not preview.busy)
                self.assertTrue(preview.stale)
                self.assertEqual(preview.summary, '')
                self.assertEqual(live._traces[0].columns['y'].values.tolist(), [2., 3.])
                self.assertTrue(preview.request(0, params, Connections()))
                wait(lambda: not preview.busy)
                self.assertFalse(preview.stale)
                self.assertEqual(preview.summary, 'new snapshot')
                preview.reject('Correct input')
                self.assertTrue(preview.stale)
                self.assertEqual(preview.error, 'Correct input')
        finally:
            release.set()
            preview._pool.waitForDone(5000)
            self.app.processEvents()
