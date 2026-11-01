"""Sampling summaries must match actual offline worker trajectories."""
import os
import threading
import time
import unittest
from unittest.mock import patch

from PySide6.QtCore import QCoreApplication
from PySide6.QtTest import QSignalSpy

if os.environ.get("TRANSPORT_OFFLINE_VERIFICATION") != "1":
    raise unittest.SkipTest("Run through verify_offline.py")

from app.models import CoParams, Connections, LineSweepParams, PhotocurrentParams, SaveRoot
from app.quick.parameters import defaults
from app.quick.sampling import gate_sampling, point_sampling, step_sampling
from app.utils import _frange_inc
from app.workers.cosweep import _merged_region_rows
from app.workers.line_sweep import LineSweepWorker
from app.workers.photocurrent import PhotocurrentWorker


class QuickSamplingTests(unittest.TestCase):
    def assert_summary_count(self, text, values):
        self.assertIn(f"Points / pass: {len(values):,}", text)

    def test_voltage_sampling_follows_worker_including_overshoot(self):
        for start, stop, step in ((0., 1., .6), (1., 0., .6), (0., 1., .3), (2., 2., .1), (0., 1., .25)):
            with self.subTest(start=start, stop=stop, step=step):
                values = _frange_inc(start, stop, step if stop >= start else -step)
                text, warning = step_sampling(start, stop, step)
                self.assert_summary_count(text, values)
                self.assertEqual(warning, abs(values[-1] - stop) > 1e-10)
                if warning:
                    self.assertIn(f"Actual last point: {values[-1]:g} V", text)
        text, warning = step_sampling(0., 1., .6)
        self.assertTrue(warning)
        self.assertIn("Actual last point: 1.2 V (Stop: 1 V)", text)

    def test_photo_clamps_to_stop_and_reports_short_final_interval(self):
        for start, stop, step in ((0., 1., .6), (1., 0., .6), (3., 3., .4), (0., 1., .25)):
            with self.subTest(start=start, stop=stop, step=step):
                params = PhotocurrentParams(wl_start=start, wl_stop=stop, wl_step=step)
                worker = PhotocurrentWorker(params, SaveRoot(), Connections())
                points = worker._wavelengths()
                text, warning = step_sampling(start, stop, step, "nm", "photo")
                self.assert_summary_count(text, points)
                self.assertEqual(points[-1], stop)
                self.assertFalse(warning)
                if len(points) > 1 and abs(abs(points[-1] - points[-2]) - step) > 1e-9:
                    self.assertIn(f"Last interval: {abs(points[-1] - points[-2]):g} nm", text)

    def test_region_policy_matches_merged_map_endpoints(self):
        for start, stop, step in ((0., 1., .6), (1., 0., .6), (1., 1., .4), (0., 1., .25)):
            with self.subTest(start=start, stop=stop, step=step):
                params = CoParams(axis_fast="Vtg", axis_slow="Vbg", coordinate_mode="Raw",
                    vtg_start=start, vtg_stop=stop, vtg_step=step,
                    vbg_start=0., vbg_stop=0., vbg_step=1.)
                points = _merged_region_rows(params)[0][1]
                text, warning = step_sampling(start, stop, step, "V", "region")
                self.assert_summary_count(text, points)
                self.assertEqual(points[-1], stop)
                self.assertFalse(warning)

    def test_gate_point_counts_and_common_axis_sampling_match_worker(self):
        for count in (1, 51):
            values = defaults(1)
            values.update(n_points=count, raw_vtg_active=True, raw_vtg_start=0., raw_vtg_stop=1.,
                          raw_vbg_active=True, raw_vbg_start=2., raw_vbg_stop=4.)
            params = LineSweepParams(**{k: v for k, v in values.items() if k in LineSweepParams.__dataclass_fields__})
            points = LineSweepWorker(params, SaveRoot(), Connections())._build_raw_trajectory()
            self.assertEqual(len(points), count)
            text = gate_sampling(values)
            for axis, key in (("Vtg", "vtg"), ("Vbg", "vbg")):
                if count == 1:
                    self.assertIn(f"{axis}: 1 point · Start only ({points[0][key]:g} V)", text)
                else:
                    self.assertIn(f"{axis}: {count} points / pass · Step: {abs(points[1][key] - points[0][key]):g} V", text)
            values.update(mode="Derived", derived_axis="Doping", derived_start=0., derived_stop=1.,
                          derived_vds_mode="Swept", derived_vds_start=0., derived_vds_stop=.5)
            text = gate_sampling(values)
            self.assertIn(point_sampling("Doping", 0., 1., count), text)
            self.assertIn(point_sampling("Vds", 0., .5, count), text)

    def test_large_summaries_are_bounded_async_and_results_stay_keyed(self):
        from app.quick.sampling import SamplingPreview
        app = QCoreApplication.instance() or QCoreApplication([])
        preview = SamplingPreview()
        started, release = threading.Event(), threading.Event()
        worker_threads = []
        main_thread = threading.get_ident()

        def delayed(start, stop, step, unit, policy):
            worker_threads.append(threading.get_ident())
            started.set()
            if not release.wait(5):
                raise RuntimeError("sampling test did not release worker")
            return f"result for {stop:g} {unit}", False

        def wait_until(condition):
            deadline = time.monotonic() + 5
            while not condition() and time.monotonic() < deadline:
                app.processEvents()
                time.sleep(.002)
            self.assertTrue(condition())

        try:
            with patch("app.quick.sampling.step_sampling", side_effect=delayed):
                updates = QSignalSpy(preview.updated)
                first = preview.step(0., 5000., 1.)
                self.assertIn("calculating", first[0])
                self.assertTrue(started.wait(2), "worker did not start")
                self.assertNotEqual(worker_threads[0], main_thread)
                # The blocked worker cannot delay another UI call; duplicate keys
                # are deduplicated and no more than eight tasks are queued.
                self.assertIn("calculating", preview.step(0., 5000., 1.)[0])
                for stop in range(5001, 5011):
                    self.assertIn("calculating", preview.step(0., float(stop), 1.)[0])
                self.assertEqual(len(preview._pending), 8)
                self.assertEqual(len(worker_threads), 1)
                release.set()
                wait_until(lambda: len(preview._pending) == 0)
                self.assertEqual(updates.count(), 8)
                self.assertEqual(preview.step(0., 5000., 1.), ("result for 5000 V", False))
                self.assertEqual(preview.step(0., 5001., 1.), ("result for 5001 V", False))
                # A request rejected only by the full queue can be retried after
                # updates without receiving a result from a different key.
                self.assertIn("calculating", preview.step(0., 5010., 1.)[0])
                wait_until(lambda: (0., 5010., 1., "V", "voltage") in preview._results)
                self.assertEqual(preview.step(0., 5010., 1.), ("result for 5010 V", False))
        finally:
            release.set()
            preview._pool.waitForDone(5000)
            app.processEvents()

    def test_service_and_recipe_sampling_track_commits_and_invalid_inputs(self):
        from app.quick.measurements import MeasurementService
        from tests.test_quick_measurements import FakeManager
        app = QCoreApplication.instance() or QCoreApplication([])
        service = MeasurementService(FakeManager(), SaveRoot())

        def section(title):
            return next(item for item in service.uiSections if item["title"] == title)

        try:
            service.mode = 0
            for key, value in (("vds_start", 0.), ("vds_stop", 1.), ("vds_step", .6)):
                service.setValue(key, value)
            trajectory = section("Vds trajectory")
            self.assertIn("3", trajectory["samplingText"])
            self.assertIn("Actual last point: 1.2 V", trajectory["samplingText"])
            self.assertTrue(trajectory["samplingWarning"])
            service.setValue("vds_step", "invalid")
            trajectory = section("Vds trajectory")
            self.assertIn("unavailable", trajectory["samplingText"].lower())
            self.assertNotIn("1.2 V", trajectory["samplingText"])
            self.assertTrue(trajectory["samplingWarning"])
            service.setValue("vds_step", .25)
            self.assertIn("5", section("Vds trajectory")["samplingText"])
            self.assertFalse(section("Vds trajectory")["samplingWarning"])

            service.mode = 2
            for key, value in (("vtg_start", 0.), ("vtg_stop", 1.), ("vtg_step", .6)):
                service.setValue(key, value)
            self.assertIn("Actual last point: 1.2 V", section("Vtg range")["samplingText"])
            self.assertTrue(section("Vtg range")["samplingWarning"])
            service.addRow("regions")
            model = service.rowModel("regions")
            uid = model.data(model.index(0, 0), model.UidRole)
            self.assertTrue(model.commit(uid, "vtg_step", .6))
            footer = model.cells(0)[0]
            self.assertIn("Vtg · Points / pass: 3", footer["samplingText"])
            self.assertIn("Last interval: 0.4 V", footer["samplingText"])
            self.assertFalse(footer["samplingWarning"])
            service.setRowValue("regions", 0, "vtg_step", "bad")
            self.assertIn("unavailable", model.cells(0)[0]["samplingText"].lower())
            self.assertTrue(model.cells(0)[0]["samplingWarning"])

            service.mode = 3
            for key, value in (("wl_start", 500.), ("wl_stop", 501.), ("wl_step", .6)):
                service.setValue(key, value)
            wavelength = section("Wavelength trajectory")
            self.assertIn("Points / pass: 3", wavelength["samplingText"])
            self.assertIn("Last interval: 0.4 nm", wavelength["samplingText"])
            self.assertFalse(wavelength["samplingWarning"])
            service.setValue("wl_step", "bad")
            self.assertIn("unavailable", section("Wavelength trajectory")["samplingText"].lower())

            service.mode = 1
            for key, value in (("n_points", 3), ("raw_vtg_start", 0.), ("raw_vtg_stop", 1.),
                               ("raw_vbg_active", True), ("raw_vbg_start", 0.), ("raw_vbg_stop", 4.)):
                service.setValue(key, value)
            text = section("Gate trajectory")["samplingText"]
            self.assertIn("Vtg: 3 points / pass · Step: 0.5 V", text)
            self.assertIn("Vbg: 3 points / pass · Step: 2 V", text)
            service.mode = 4
            service.addRow("conditions")
            model = service.rowModel("conditions")
            uid = model.data(model.index(0, 0), model.UidRole)
            self.assertTrue(model.commit(uid, "n_points", 3))
            self.assertIn("Vtg: 3 points / pass", model.cells(0)[0]["samplingText"])
            service.setRowValue("conditions", 0, "n_points", "bad")
            self.assertIn("unavailable", model.cells(0)[0]["samplingText"].lower())
            self.assertTrue(model.cells(0)[0]["samplingWarning"])

            service.mode = 5
            magnetic = section("Magnetic field trajectory")
            self.assertIn("Continuous", magnetic["description"])
            self.assertNotIn("samplingText", magnetic)
        finally:
            service.deleteLater()
            app.sendPostedEvents(None, 0)

    def test_point_limit_is_enforced_for_all_policies(self):
        for policy in ("voltage", "photo", "region"):
            with self.subTest(policy=policy):
                text, warning = step_sampling(0., 249999.4, 1., "V", policy)
                self.assertTrue(warning)
                self.assertIn("250,000", text)
        for policy in ("voltage", "photo", "region"):
            with self.subTest(policy=policy):
                text, warning = step_sampling(0., 249999., 1., "V", policy)
                self.assertIn("250,000", text)
                self.assertFalse(warning)
