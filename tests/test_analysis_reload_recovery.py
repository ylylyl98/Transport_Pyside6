"""Reload recovery state checks without launching renderer/viewer processes."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

if os.environ.get("TRANSPORT_OFFLINE_VERIFICATION") != "1":
    raise unittest.SkipTest("Requires offline wrapper")

from PySide6 import QtCore
from app.ui.history_viewer_launcher import HistoryViewerLauncher
from app.ui.png_export_process import PngExportProcess


class PngReloadRecoveryTests(unittest.TestCase):
    def setUp(self):
        scratch = tempfile.TemporaryDirectory(prefix="transport-png-")
        self.addCleanup(scratch.cleanup)
        with patch("app.ui.png_export_process.tempfile.mkdtemp", return_value=scratch.name):
            self.queue = PngExportProcess()
        self.launches = 0
        self.fail_startup = False
        launcher = patch.object(self.queue, "_launch_worker", side_effect=self.launch_worker)
        launcher.start()
        self.addCleanup(launcher.stop)
        self.addCleanup(self.queue.shutdown)
        self.results, self.reloads = [], []
        self.queue.completed.connect(self.results.append)
        self.queue.reload_finished.connect(self.reloads.append)

    def launch_worker(self, **_):
        self.launches += 1
        for name in ("exited.json", "ready.json", "boot.json", "fatal.json", "stop"):
            (self.queue.spool / name).unlink(missing_ok=True)
        self.queue._announced = False
        self.queue._pid = os.getpid()  # Live PID, with no actual child process.
        marker = "fatal.json" if self.fail_startup else "ready.json"
        payload = {"error": "updated renderer import failed"} if self.fail_startup else {"pid": os.getpid()}
        (self.queue.spool / marker).write_text(json.dumps(payload), encoding="utf-8")

    def complete_job(self, number):
        path = self.queue._pending[number]
        result = path.with_name(path.name.replace(".job.json", ".result.json"))
        result.write_text(json.dumps({"id": number, "outputs": []}), encoding="utf-8")
        path.unlink(missing_ok=True)
        self.queue._poll()

    def fail_replacement(self, *, accept_job=True):
        seed = self.queue.submit({"kind": "run", "csv_paths": []})
        self.queue._poll()
        self.complete_job(seed)
        self.results.clear()
        self.queue.request_reload()
        accepted = self.queue.submit({"kind": "run", "csv_paths": ["accepted.csv"]}) if accept_job else None
        self.fail_startup = True
        (self.queue.spool / "exited.json").write_text("{}", encoding="utf-8")
        self.queue._poll()  # Drain completed; replacement starts.
        self.queue._poll()  # Replacement reports its failed import.
        self.assertFalse(self.reloads[-1]["ok"])
        self.assertIn(str(self.queue.spool), self.reloads[-1]["error"])
        self.assertIn("Reload analysis", self.reloads[-1]["error"])
        return accepted

    def test_new_jobs_remain_durable_until_explicit_retry_succeeds(self):
        accepted = self.fail_replacement()
        later = self.queue.submit({"kind": "run", "csv_paths": ["later.csv"]})
        self.queue._poll()  # Even a previously queued timer event is harmless.
        self.assertEqual(self.launches, 2)
        self.assertFalse(self.queue.timer.isActive())
        self.assertEqual(set(self.queue._pending), {accepted, later})
        self.assertTrue(self.queue._pending[accepted].exists())
        self.assertTrue(self.queue._pending[later].with_suffix(".waiting").exists())
        self.assertFalse(self.results)

        self.queue.request_reload()  # Another failed attempt must preserve both.
        self.queue._poll()
        self.assertFalse(self.reloads[-1]["ok"])
        self.assertEqual(set(self.queue._pending), {accepted, later})
        self.assertFalse(self.results)

        self.fail_startup = False
        self.queue.request_reload()
        self.queue._poll()
        self.assertTrue(self.reloads[-1]["ok"])
        self.assertFalse(self.queue._reload_paused)
        self.assertTrue(all(path.exists() for path in self.queue._pending.values()))
        self.complete_job(accepted)
        self.complete_job(later)
        self.assertEqual({result["id"] for result in self.results}, {accepted, later})
        self.assertFalse(self.queue.busy)

    def test_shutdown_of_failed_reload_preserves_jobs_without_retrying(self):
        accepted = self.fail_replacement()
        later = self.queue.submit({"kind": "run", "csv_paths": ["later.csv"]})
        self.queue.shutdown()
        self.assertEqual(self.launches, 2)
        self.assertTrue(self.queue._pending[accepted].exists())
        waiting = self.queue._pending[later].with_suffix(".waiting")
        self.assertEqual(json.loads(waiting.read_text(encoding="utf-8"))["id"], later)
        self.assertFalse(self.results)

    def test_failed_reload_without_jobs_still_validates_explicit_retry(self):
        self.fail_replacement(accept_job=False)
        self.fail_startup = False
        self.queue.request_reload()
        self.assertEqual(self.launches, 3)
        self.assertFalse(self.reloads[-1]["ok"])
        self.queue._poll()
        self.assertTrue(self.reloads[-1]["ok"])

    def test_temporarily_locked_result_is_retried_without_losing_completion(self):
        number = self.queue.submit({"kind": "run", "csv_paths": []})
        path = self.queue._pending[number]
        result = path.with_name(path.name.replace(".job.json", ".result.json"))
        result.write_text(json.dumps({"id": number, "outputs": []}), encoding="utf-8")
        path.unlink()
        read_text = Path.read_text

        def locked_read(candidate, *args, **kwargs):
            if candidate == result:
                raise PermissionError("temporary Windows sharing lock")
            return read_text(candidate, *args, **kwargs)

        with patch.object(Path, "read_text", locked_read):
            self.queue._poll()
        self.assertIn(number, self.queue._pending)
        self.assertFalse(self.results)
        self.queue._poll()
        self.queue._poll()
        self.assertEqual([item["id"] for item in self.results], [number])
        self.assertFalse(self.queue.busy)


class _ViewerProcess:
    def __init__(self):
        self.running = True
        self.incoming = b""
        self.commands = []

    def state(self):
        return QtCore.QProcess.ProcessState.Running if self.running else QtCore.QProcess.ProcessState.NotRunning

    def write(self, value):
        self.commands.append(json.loads(bytes(value)))

    def readAllStandardOutput(self):
        value, self.incoming = self.incoming, b""
        return value


class ViewerReloadRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.launcher = HistoryViewerLauncher(lambda: Path(tempfile.gettempdir()) / "current-device")
        self.process = _ViewerProcess()
        self.launcher.process = self.process
        self.starts = 0
        replacement = patch.object(self.launcher, "_start_viewer", side_effect=self.start_viewer)
        replacement.start()
        self.addCleanup(replacement.stop)
        self.addCleanup(self.cleanup_launcher)
        self.session = {"schema": "transport_analysis_session_v1", "folder": "custom-history",
                        "custom_folder": "custom-history", "limits": {"xlim": [0.2, 0.8]},
                        "geometry": "saved-window-geometry"}

    def cleanup_launcher(self):
        self.process.running = False
        self.launcher.shutdown()
        self.launcher.close()

    def start_viewer(self):
        self.starts += 1
        self.process.running = True

    def report(self, event, **values):
        self.process.incoming += (json.dumps({"event": event, **values}) + "\n").encode("utf-8")
        self.launcher._read_stdout()

    def exit_viewer(self, code=0, exit_status=QtCore.QProcess.ExitStatus.NormalExit):
        self.process.running = False
        self.launcher._finished(code, exit_status)

    def begin_replacement(self):
        self.launcher.reload_analysis()
        self.report("reload_session", session=self.session)
        self.exit_viewer()
        self.assertEqual(self.starts, 1)

    def test_failed_replacement_retries_original_session_then_clears_handoff(self):
        self.begin_replacement()
        self.exit_viewer(1)
        self.assertTrue(self.launcher.reload_button.isEnabled())
        self.launcher.reload_analysis()
        self.assertEqual(self.starts, 2)
        self.report("ready")
        self.assertEqual(self.process.commands[-1], {"command": "restore_session", "session": self.session})
        self.report("session_loaded")
        self.assertIsNone(self.launcher._reload_session)
        self.assertFalse(self.launcher._reload_reopen)
        self.exit_viewer()
        self.launcher.reload_analysis()
        self.assertEqual(self.starts, 2)  # A subsequently closed viewer stays closed.

    def test_open_after_failed_replacement_also_restores_saved_session(self):
        self.begin_replacement()
        self.exit_viewer(1)
        self.launcher.open_viewer()
        self.assertEqual(self.starts, 2)
        self.report("ready")
        self.assertEqual(self.process.commands[-1]["session"], self.session)

    def test_failed_restore_does_not_replace_original_handoff_with_default_view(self):
        self.begin_replacement()
        self.report("ready")
        self.report("session_error", error="updated viewer could not restore")
        self.launcher.reload_analysis()
        self.report("reload_session", session={"folder": "default-device", "limits": None})
        self.exit_viewer()
        self.report("ready")
        self.assertEqual(self.process.commands[-1]["session"], self.session)
        self.report("session_loaded")
        self.assertIsNone(self.launcher._reload_session)

    def test_renderer_failure_does_not_retain_successfully_restored_viewer(self):
        self.begin_replacement()
        self.launcher._reload_error = "PNG renderer failed"
        self.report("ready")
        self.report("session_loaded")
        self.assertIn("PNG renderer failed", self.launcher.status_label.text())
        self.assertIsNone(self.launcher._reload_session)
        self.assertFalse(self.launcher._reload_reopen)

    def test_user_close_after_failed_restore_stays_closed_until_explicit_open(self):
        self.begin_replacement()
        self.report("ready")
        self.report("session_error", error="updated viewer could not restore")
        self.exit_viewer()
        self.assertFalse(self.launcher._reload_reopen)
        self.assertEqual(self.launcher._reload_session, self.session)

        self.launcher.reload_analysis()
        self.assertEqual(self.starts, 1)
        self.assertFalse(self.process.running)
        self.assertFalse(self.launcher._reloading)
        self.assertEqual(self.launcher._reload_session, self.session)

        self.launcher.open_viewer()
        self.assertEqual(self.starts, 2)
        self.report("ready")
        self.assertEqual(self.process.commands[-1], {"command": "restore_session", "session": self.session})
        self.report("session_loaded")
        self.assertIsNone(self.launcher._reload_session)

    def test_crash_after_failed_restore_retains_automatic_retry_intent(self):
        self.begin_replacement()
        self.report("ready")
        self.report("session_error", error="updated viewer could not restore")
        self.exit_viewer(1, QtCore.QProcess.ExitStatus.CrashExit)
        self.assertTrue(self.launcher._reload_reopen)
        self.launcher.reload_analysis()
        self.assertEqual(self.starts, 2)
        self.report("ready")
        self.assertEqual(self.process.commands[-1]["session"], self.session)

    def test_crash_status_does_not_cancel_retry_even_with_zero_exit_code(self):
        self.begin_replacement()
        self.report("ready")
        self.report("session_error", error="updated viewer could not restore")
        self.exit_viewer(0, QtCore.QProcess.ExitStatus.CrashExit)
        self.assertTrue(self.launcher._reload_reopen)
        self.assertEqual(self.launcher._reload_session, self.session)


if __name__ == "__main__":
    unittest.main()
