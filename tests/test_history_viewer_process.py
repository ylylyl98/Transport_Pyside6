import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

if os.environ.get("TRANSPORT_OFFLINE_VERIFICATION") != "1":
    raise unittest.SkipTest("Requires offline wrapper")

from PySide6 import QtCore, QtTest, QtWidgets
from app.ui.history_viewer_launcher import HistoryViewerLauncher


class HistoryViewerProcessTests(unittest.TestCase):
    def test_export_menu_keeps_connected_viewer_and_parent_commands_alive(self):
        helper = Path(__file__).parent / 'helpers' / 'analysis_export_viewer.py'
        for selection, count in (('heatmap', 1), ('cut', 1), ('both', 2)):
            with self.subTest(selection=selection), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                launcher = HistoryViewerLauncher(lambda: root)
                messages = []
                launcher.message_received.connect(messages.append)
                launcher.process.setProgram(sys.executable)
                launcher.process.setArguments(['-u', str(helper), str(root), selection])
                launcher.process.start()
                try:
                    self.wait_for(lambda: any(m.get('event') == 'export_complete' for m in messages)
                                  or launcher.process.state() == QtCore.QProcess.ProcessState.NotRunning)
                    completed = [m for m in messages if m.get('event') == 'export_complete']
                    self.assertTrue(completed, f'Viewer exited during {selection} export: {messages!r}\n{launcher._stderr}')
                    self.assertTrue(completed[0]['visible'])
                    result = completed[0]['result']
                    self.assertNotIn('error', result)
                    self.assertEqual(len(result['outputs']), count)
                    self.assertEqual(len(result['csv_outputs']), count)
                    self.assertTrue(all(Path(p).stat().st_size > 0 for p in result['outputs'] + result['csv_outputs']))
                    launcher._send('refresh')
                    self.wait_for(lambda: any(m.get('event') == 'command_received'
                                             and m['message'].get('command') == 'refresh' for m in messages))
                    self.assertFalse(any(m.get('event') == 'close_ready' for m in messages))
                    launcher._send('close')
                    self.wait_for(lambda: launcher.process.state() == QtCore.QProcess.ProcessState.NotRunning)
                    self.assertEqual(launcher.process.exitCode(), 0)
                finally:
                    launcher.shutdown()
                    # The pre-fix regression exits the viewer while its accepted
                    # detached export still drains. Let it finish before cleanup.
                    started = next((m for m in messages if m.get('event') == 'export_started'), None)
                    if started:
                        spool = Path(started['spool'])
                        self.wait_for(lambda: (spool / 'exited.json').exists())

    def wait_for(self, condition):
        deadline = time.monotonic() + 12
        while time.monotonic() < deadline:
            QtWidgets.QApplication.instance().processEvents()
            if condition():
                return
            QtTest.QTest.qWait(20)
        self.fail("Timed out waiting for independent history process")

    def test_viewer_is_a_separate_process_and_receives_folder_without_hardware(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            folder = root / "first"
            folder.mkdir()
            launcher = HistoryViewerLauncher(lambda: folder)
            self.addCleanup(launcher.shutdown)
            messages = []
            launcher.message_received.connect(messages.append)
            ticks = []
            timer = QtCore.QTimer()
            timer.setInterval(20)
            timer.timeout.connect(lambda: ticks.append(1))
            timer.start()
            try:
                launcher.open_viewer()
                self.wait_for(lambda: any(m.get("event") == "ready" for m in messages))
                ready = next(m for m in messages if m.get("event") == "ready")
                self.assertNotEqual(ready["pid"], os.getpid())
                self.assertEqual(ready["hardware_modules"], [])
                self.assertTrue(ticks)
                old_pid = launcher.process.processId()
                folder = root / "second"
                folder.mkdir()
                launcher.folder_changed()
                self.wait_for(lambda: any(m.get("event") == "folder" and m.get("folder") == str(folder) for m in messages))
                launcher.open_viewer()
                self.assertEqual(launcher.process.processId(), old_pid)
            finally:
                timer.stop()
                launcher.shutdown()
            self.assertEqual(launcher.process.state(), QtCore.QProcess.ProcessState.NotRunning)

    def test_reload_restarts_viewer_and_renderer_and_hands_off_session(self):
        from app.ui.png_export_process import PngExportProcess
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            launcher = HistoryViewerLauncher(lambda: root)
            queue = PngExportProcess()
            self.addCleanup(launcher.shutdown)
            self.addCleanup(queue.shutdown)
            messages, ready = [], []
            launcher.message_received.connect(messages.append)
            queue.ready.connect(ready.append)
            launcher.bind_exporter(queue)
            queue.submit({"kind": "run", "csv_paths": []})
            launcher.open_viewer()
            self.wait_for(lambda: bool(ready) and any(m.get("event") == "ready" for m in messages))
            old_viewer = launcher.process.processId()
            old_renderer = ready[0]["pid"]
            launcher.reload_button.click()
            self.assertFalse(launcher.reload_button.isEnabled())
            launcher.reload_analysis()  # Repeated requests must not start more processes.
            self.wait_for(lambda: launcher.reload_button.isEnabled() and len([m for m in messages if m.get("event") == "ready"]) == 2)
            self.assertNotEqual(launcher.process.processId(), old_viewer)
            self.assertNotEqual(ready[-1]["pid"], old_renderer)
            saved = next(m["session"] for m in messages if m.get("event") == "reload_session")
            restored = next(m["session"] for m in messages if m.get("event") == "session_loaded")
            self.assertEqual(restored["folder"], saved["folder"])
            self.assertEqual(restored["kind"], saved["kind"])
            self.assertTrue(all(m["hardware_modules"] == [] for m in messages if m.get("event") == "ready"))

    def test_reload_with_viewer_closed_keeps_it_closed(self):
        from app.ui.png_export_process import PngExportProcess
        with tempfile.TemporaryDirectory() as directory:
            launcher = HistoryViewerLauncher(lambda: Path(directory))
            queue = PngExportProcess()
            self.addCleanup(launcher.shutdown)
            self.addCleanup(queue.shutdown)
            launcher.bind_exporter(queue)
            launcher.reload_button.click()
            self.wait_for(launcher.reload_button.isEnabled)
            self.assertEqual(launcher.process.state(), QtCore.QProcess.ProcessState.NotRunning)

    def test_renderer_reload_failure_still_restores_viewer_session(self):
        class FailedRenderer(QtCore.QObject):
            reload_finished = QtCore.Signal(object)
            def request_reload(self):
                self.reload_finished.emit({"ok": False, "error": "invalid updated renderer"})
        with tempfile.TemporaryDirectory() as directory:
            launcher = HistoryViewerLauncher(lambda: Path(directory))
            self.addCleanup(launcher.shutdown)
            renderer = FailedRenderer()
            launcher.bind_exporter(renderer)
            messages = []
            launcher.message_received.connect(messages.append)
            launcher.open_viewer()
            self.wait_for(lambda: any(m.get("event") == "ready" for m in messages))
            old_pid = launcher.process.processId()
            launcher.reload_analysis()
            self.wait_for(lambda: any(m.get("event") == "session_loaded" for m in messages))
            self.assertNotEqual(old_pid, launcher.process.processId())
            self.assertTrue(launcher.reload_button.isEnabled())
            self.assertIn("invalid updated renderer", launcher.status_label.text())

    def test_main_shutdown_during_reload_cancels_viewer_reopen(self):
        from app.ui.png_export_process import PngExportProcess
        with tempfile.TemporaryDirectory() as directory:
            launcher = HistoryViewerLauncher(lambda: Path(directory))
            queue = PngExportProcess()
            self.addCleanup(launcher.shutdown)
            self.addCleanup(queue.shutdown)
            launcher.bind_exporter(queue)
            messages = []
            launcher.message_received.connect(messages.append)
            launcher.open_viewer()
            self.wait_for(lambda: any(m.get("event") == "ready" for m in messages))
            launcher.reload_analysis()
            queue.shutdown()
            launcher.prepare_shutdown()
            self.wait_for(lambda: launcher.process.state() == QtCore.QProcess.ProcessState.NotRunning)
            QtTest.QTest.qWait(300)
            self.assertEqual(launcher.process.state(), QtCore.QProcess.ProcessState.NotRunning)
            self.assertEqual(len([m for m in messages if m.get("event") == "ready"]), 1)


if __name__ == "__main__":
    unittest.main()
