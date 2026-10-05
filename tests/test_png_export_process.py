import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

if os.environ.get("TRANSPORT_OFFLINE_VERIFICATION") != "1":
    raise unittest.SkipTest("Requires offline wrapper")

from PySide6 import QtCore, QtTest, QtWidgets
from app.ui.widgets.run_panel import RunPanel


class PngProcessTests(unittest.TestCase):
    def wait_for(self, predicate):
        end = time.monotonic() + 20
        while time.monotonic() < end:
            QtWidgets.QApplication.instance().processEvents()
            if predicate():
                return
            QtTest.QTest.qWait(20)
        self.fail("PNG background operation timed out")

    def test_replacement_exit_before_boot_is_detected_and_queued_job_can_retry(self):
        from unittest.mock import patch
        from app.ui.png_export_process import PngExportProcess
        with tempfile.TemporaryDirectory() as directory:
            startup = Path(directory)
            # Fail during Python startup, before the worker can write boot.json.
            # Exercise the production launcher rather than overriding its PID.
            (startup / 'sitecustomize.py').write_text("raise SystemExit('test pre-boot failure')\n", encoding='utf-8')
            queue = PngExportProcess()
            self.addCleanup(queue.shutdown)
            results, reloads = [], []
            queue.completed.connect(results.append)
            queue.reload_finished.connect(reloads.append)
            queue.submit({'kind': 'run', 'csv_paths': []})
            self.wait_for(lambda: len(results) == 1)
            with patch.dict(os.environ, {'PYTHONPATH': str(startup)}):
                queue.request_reload()
                queue.submit({'kind': 'run', 'csv_paths': []})
                self.wait_for(lambda: bool(reloads))
                self.assertFalse(reloads[-1]['ok'])
                self.assertFalse((queue.spool / 'boot.json').exists())
                self.assertTrue(list(queue.spool.glob('*.job.json')))
            queue.request_reload()
            self.wait_for(lambda: len(results) == 2 and len(reloads) == 2)
            self.assertTrue(reloads[-1]['ok'])

    def test_exporter_is_separate_survives_job_error_and_gui_keeps_ticking(self):
        from app.ui.png_export_process import PngExportProcess
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            csv = root / "run.csv"
            csv.write_text("Vds,Ids_DC,Direction\nV,A,\n0,1e-9,forward\n1,2e-9,forward\n")
            metadata = root / "run_metadata.json"
            metadata.write_text(json.dumps({"measurement": "vds_sweep", "csv_path": str(csv), "status": "finished"}))
            queue = PngExportProcess()
            self.addCleanup(queue.shutdown)
            completed, ready, ticks = [], [], []
            queue.completed.connect(completed.append)
            queue.ready.connect(ready.append)
            timer = QtCore.QTimer()
            timer.setInterval(10)
            timer.timeout.connect(lambda: ticks.append(1))
            timer.start()
            try:
                queue.submit({"kind": "analysis", "snapshot_path": str(root / "missing.npz"), "output": str(root / "bad.png")})
                queue.submit({"kind": "run", "metadata_path": str(metadata)})
                self.wait_for(lambda: len(completed) == 2)
                self.assertNotEqual(ready[0]["pid"], os.getpid())
                self.assertEqual(ready[0]["hardware_modules"], [])
                self.assertIn("error", completed[0])
                self.assertEqual(len(completed[1]["outputs"]), 1)
                self.assertGreater(len(ticks), 5)
            finally:
                timer.stop()
                queue.shutdown()

    def test_dual_grid_is_exported_by_hardware_free_background_worker(self):
        from app.ui.png_export_process import PngExportProcess
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            csv = root / "dual_grid.csv"
            csv.write_text("Vtg,Vbg,I_drag_X,I_drag_Y,I_drive_X,I_drive_Y\nV,V,A,A,A,A\n"
                           "0,0,1e-12,2e-12,1e-6,2e-6\n1,0,2e-12,3e-12,2e-6,3e-6\n"
                           "1,1,3e-12,4e-12,3e-6,4e-6\n0,1,4e-12,5e-12,4e-6,5e-6\n")
            meta = root / "dual_grid_metadata.json"
            meta.write_text(json.dumps({"measurement": "map_2d", "status": "finished", "csv_path": str(csv),
                                        "signal_chain": {"drag_drive": {"enabled": True}},
                                        "params": {"axis_fast": "Vtg", "axis_slow": "Vbg"}}))
            queue = PngExportProcess()
            self.addCleanup(queue.shutdown)
            completed, ready = [], []
            queue.completed.connect(completed.append)
            queue.ready.connect(ready.append)
            queue.submit({"kind": "run", "metadata_path": str(meta)})
            self.wait_for(lambda: bool(completed))
            self.assertNotEqual(ready[0]["pid"], os.getpid())
            self.assertEqual(ready[0]["hardware_modules"], [])
            self.assertNotIn("error", completed[0])
            self.assertEqual(completed[0]["warnings"], [])
            self.assertEqual({Path(output).name for output in completed[0]["outputs"]},
                             {f"dual_grid_{name}_heatmap.png" for name in
                              ("I_drag_X", "I_drag_Y", "I_drive_X", "I_drive_Y", "DragDrive_2x2")})

    def test_coordinator_freezes_paths_and_waits_for_worker_cleanup(self):
        from app.ui.measurement_png_coordinator import MeasurementPngCoordinator
        class Queue(QtCore.QObject):
            def __init__(self):
                super().__init__()
                self.jobs = []
            def submit(self, job):
                self.jobs.append(job)
        panel = RunPanel("Start")
        self.addCleanup(panel.close)
        tab = SimpleNamespace(run_panel=panel, p=SimpleNamespace(output_csv_path="old.csv", output_metadata_path="old_metadata.json"), worker_thread=object())
        queue = Queue()
        coordinator = MeasurementPngCoordinator([tab], queue, lambda: True)
        self.addCleanup(coordinator.shutdown)
        panel.set_running(True)
        tab.p.output_csv_path = "new.csv"
        tab.p.output_metadata_path = "new_metadata.json"
        panel.set_running(False)
        QtTest.QTest.qWait(300)
        self.assertEqual(queue.jobs, [])
        tab.worker_thread = None
        self.wait_for(lambda: bool(queue.jobs))
        self.assertEqual(queue.jobs[0]["metadata_path"], "old_metadata.json")
        self.assertEqual(queue.jobs[0]["csv_paths"], ["old.csv"])
        panel.status_changed.emit()
        QtTest.QTest.qWait(300)
        self.assertEqual(len(queue.jobs), 1)

    def test_shutdown_leaves_queued_png_job_to_finish(self):
        from app.ui.png_export_process import PngExportProcess
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            csv = root / "run.csv"
            csv.write_text("Vds,Ids_DC\nV,A\n0,1e-9\n1,2e-9\n")
            queue = PngExportProcess()
            queue.submit({"kind": "run", "csv_paths": [str(csv)], "measurement": "vds_sweep"})
            queue.shutdown()
            self.wait_for(lambda: (root / "plots/run_Ids_DC.png").exists())

    def test_elapsed_time_does_not_delete_accepted_queue_files(self):
        from app.ui.png_export_process import PngExportProcess
        queue = PngExportProcess()
        self.addCleanup(queue.shutdown)
        number = queue.submit({"kind": "run", "csv_paths": []})
        path = queue.spool / f"{number}.job.json"
        queue._last_activity -= 181
        queue._poll()
        self.assertTrue(path.exists())

    def test_worker_restart_preserves_newly_accepted_job(self):
        from app.ui.png_export_process import PngExportProcess
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            csv = root / "restart.csv"
            csv.write_text("Vds,Ids_DC\nV,A\n0,1e-9\n1,2e-9\n")
            queue = PngExportProcess()
            self.addCleanup(queue.shutdown)
            completed, ready = [], []
            queue.completed.connect(completed.append)
            queue.ready.connect(ready.append)
            queue.submit({"kind": "run", "csv_paths": []})
            self.wait_for(lambda: len(completed) == 1)
            first_pid = ready[0]["pid"]
            (queue.spool / "stop").touch()
            self.wait_for(lambda: (queue.spool / "exited.json").exists())
            queue.submit({"kind": "run", "csv_paths": [str(csv)], "measurement": "vds_sweep"})
            self.wait_for(lambda: len(completed) == 2)
            self.assertNotEqual(first_pid, ready[-1]["pid"])
            self.assertEqual(len(completed[-1]["outputs"]), 1)
            self.assertTrue((root / "plots/restart_Ids_DC.png").exists())

    def test_reload_drains_old_jobs_and_preserves_jobs_submitted_during_reload(self):
        from app.ui.png_export_process import PngExportProcess
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ("old", "new"):
                (root / f"{name}.csv").write_text("Vds,Ids_DC\nV,A\n0,1e-9\n1,2e-9\n")
            queue = PngExportProcess()
            self.addCleanup(queue.shutdown)
            ready, completed, reloaded = [], [], []
            queue.ready.connect(ready.append)
            queue.completed.connect(completed.append)
            queue.reload_finished.connect(reloaded.append)
            queue.submit({"kind": "run", "csv_paths": [str(root / "old.csv")], "measurement": "vds_sweep"})
            self.wait_for(lambda: bool(ready))
            first_pid = ready[0]["pid"]
            queue.request_reload()
            queue.submit({"kind": "run", "csv_paths": [str(root / "new.csv")], "measurement": "vds_sweep"})
            self.wait_for(lambda: len(completed) == 2 and bool(reloaded))
            self.assertNotEqual(first_pid, ready[-1]["pid"])
            self.assertTrue(reloaded[-1]["ok"])
            self.assertTrue((root / "plots/old_Ids_DC.png").exists())
            self.assertTrue((root / "plots/new_Ids_DC.png").exists())
            self.assertFalse(any(result.get("error") for result in completed))

    def test_shutdown_during_reload_still_finishes_newly_accepted_job(self):
        from app.ui.png_export_process import PngExportProcess
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            csv = root / "closing.csv"
            csv.write_text("Vds,Ids_DC\nV,A\n0,1e-9\n1,2e-9\n")
            queue = PngExportProcess()
            self.addCleanup(queue.shutdown)
            queue.submit({"kind": "run", "csv_paths": []})
            queue.request_reload()
            queue.submit({"kind": "run", "csv_paths": [str(csv)], "measurement": "vds_sweep"})
            queue.shutdown()
            self.wait_for(lambda: (root / "plots/closing_Ids_DC.png").exists())

    def test_shutdown_after_old_worker_decides_to_exit_hands_jobs_to_successor(self):
        import sys
        from app.ui.png_export_process import PngExportProcess
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            helper = root / "gated_worker.py"
            project = Path(__file__).resolve().parents[1]
            helper.write_text(f'''import sys, time
from pathlib import Path
sys.path.insert(0, {str(project)!r})
import transport_png_exporter as worker
original = worker.write_json
def gated(path, payload):
    if path.name == "exited.json":
        Path({str(root / 'exit_decided')!r}).touch()
        while not Path({str(root / 'release')!r}).exists(): time.sleep(.02)
    original(path, payload)
worker.write_json = gated
worker.main()
''', encoding="utf-8")
            class GatedQueue(PngExportProcess):
                first = True
                def _launch_worker(self, *args, **kwargs):
                    if self.first:
                        self.first = False
                        ok, self._pid = QtCore.QProcess.startDetached(sys.executable, [str(helper), "--spool", str(self.spool), "--owner-pid", str(os.getpid())], str(project))
                        if not ok: raise RuntimeError("Fixture worker failed to start")
                    else:
                        super()._launch_worker(*args, **kwargs)
            queue = GatedQueue()
            self.addCleanup(queue.shutdown)
            queue.submit({"kind": "run", "csv_paths": []})
            self.wait_for(lambda: not queue.busy)
            queue.request_reload()
            self.wait_for(lambda: (root / "exit_decided").exists())
            csv = root / "handoff.csv"
            csv.write_text("Vds,Ids_DC\nV,A\n0,1e-9\n1,2e-9\n")
            queue.submit({"kind": "run", "csv_paths": [str(csv)], "measurement": "vds_sweep"})
            try:
                queue.shutdown()
            finally:
                (root / "release").touch()
            self.wait_for(lambda: (root / "plots/handoff_Ids_DC.png").exists())

    def test_replacement_startup_exit_reports_reload_failure_and_can_retry_durable_jobs(self):
        import sys
        from app.ui.png_export_process import PngExportProcess
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            helper = root / "bad_startup.py"
            helper.write_text("import json, os, sys\nfrom pathlib import Path\np=Path(sys.argv[1]); (p/'boot.json').write_text(json.dumps({'pid':os.getpid()}))\nsys.exit(1)\n")
            class InterruptedQueue(PngExportProcess):
                launches = 0
                def _launch_worker(self, *args, **kwargs):
                    self.launches += 1
                    if self.launches == 2:
                        for name in ("exited.json", "ready.json", "boot.json", "fatal.json", "stop"):
                            (self.spool / name).unlink(missing_ok=True)
                        self._announced = False
                        ok, self._pid = QtCore.QProcess.startDetached(sys.executable, [str(helper), str(self.spool)])
                        if not ok: raise RuntimeError("Fixture worker failed to start")
                    else:
                        super()._launch_worker(*args, **kwargs)
            queue = InterruptedQueue()
            self.addCleanup(queue.shutdown)
            results, reloads = [], []
            queue.completed.connect(results.append)
            queue.reload_finished.connect(reloads.append)
            queue.submit({"kind": "run", "csv_paths": []})
            self.wait_for(lambda: len(results) == 1)
            queue.request_reload()
            csv = root / "retry.csv"
            csv.write_text("Vds,Ids_DC\nV,A\n0,1e-9\n1,2e-9\n")
            queue.submit({"kind": "run", "csv_paths": [str(csv)], "measurement": "vds_sweep"})
            self.wait_for(lambda: bool(reloads))
            self.assertFalse(reloads[-1]["ok"])
            self.assertTrue(list(queue.spool.glob("*.job.json")))
            queue.request_reload()
            self.wait_for(lambda: len(results) == 2 and len(reloads) == 2)
            self.assertTrue(reloads[-1]["ok"])
            self.assertTrue((root / "plots/retry_Ids_DC.png").exists())


if __name__ == "__main__":
    unittest.main()
