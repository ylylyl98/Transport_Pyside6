"""An accepted analysis snapshot must reach disk before Transport closes its viewer."""
import csv
import json
import os
import tempfile
import time
import unittest
from pathlib import Path

if os.environ.get("TRANSPORT_OFFLINE_VERIFICATION") != "1":
    raise unittest.SkipTest("Requires offline wrapper")

from PySide6 import QtCore, QtTest, QtWidgets
from app.ui.history_viewer_launcher import HistoryViewerLauncher


class PngViewerShutdownTests(unittest.TestCase):
    def test_reload_waits_for_accepted_analysis_export_before_reopening(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = Path(__file__).resolve().parents[1]
            target = root / "pending.png"
            helper = root / "pending_viewer.py"
            helper.write_text(f'''import sys, time, threading
sys.path.insert(0, {str(project)!r})
import numpy as np
from PySide6 import QtWidgets
from transport_history_viewer import HistoryViewerWindow, CommandBridge, report
from app.ui.png_export_process import _SnapshotTask
original = _SnapshotTask.run
def slow(task):
    time.sleep(.7)
    original(task)
_SnapshotTask.run = slow
app = QtWidgets.QApplication([])
window = HistoryViewerWindow({str(root)!r})
bridge = CommandBridge()
bridge.received.connect(window.handle_command)
threading.Thread(target=bridge.read_commands, daemon=True).start()
snapshot = {{"mode":"curve", "kind":"vds_sweep", "signal":"Ids_DC", "x_name":"Vds", "x_unit":"V", "y_unit":"A", "view":{{"legend":True}}, "records":[{{"path":"synthetic.csv", "status":"finished", "metadata":{{}}}}], "traces":[{{"path":"synthetic.csv", "direction":"forward", "condition":"", "x":np.array([0.,1.]), "y":np.array([1e-9,2e-9])}}]}}
window.page.png_actions.exporter.submit_snapshot(snapshot, {str(target)!r})
window.show()
report("ready", pid=__import__('os').getpid(), hardware_modules=[])
sys.exit(app.exec())
''', encoding="utf-8")
            launcher = HistoryViewerLauncher(lambda: root)
            self.addCleanup(launcher.shutdown)
            messages = []
            launcher.message_received.connect(messages.append)
            launcher.process.setProgram(__import__("sys").executable)
            launcher.process.setArguments(["-u", str(helper)])
            launcher.process.start()
            end = time.monotonic() + 20
            while not messages and time.monotonic() < end:
                QtWidgets.QApplication.instance().processEvents()
                QtTest.QTest.qWait(20)
            launcher.reload_analysis()
            end = time.monotonic() + 20
            while len([m for m in messages if m.get("event") == "ready"]) < 2 and time.monotonic() < end:
                QtWidgets.QApplication.instance().processEvents()
                QtTest.QTest.qWait(20)
            self.assertEqual(len([m for m in messages if m.get("event") == "ready"]), 2,
                             f"{messages!r}\n{launcher._stderr}")
            payload = json.loads(target.with_name("pending_view.json").read_text(encoding="utf-8"))
            self.assertTrue(target.stat().st_size > 0)
            self.assertTrue(Path(payload["csv_files"][0]["path"]).stat().st_size > 0)
            self.assertTrue(any(m.get("event") == "reload_session" for m in messages), f"{messages!r}\n{launcher._stderr}")

    def test_viewer_shutdown_waits_for_slow_analysis_snapshot_handoff(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = Path(__file__).resolve().parents[1]
            target = root / "analysis.png"
            helper = root / "viewer_fixture.py"
            helper.write_text(f'''import sys, time
sys.path.insert(0, {str(project)!r})
import numpy as np
from PySide6 import QtWidgets
from transport_history_viewer import HistoryViewerWindow, CommandBridge, report
from app.ui.png_export_process import _SnapshotTask
import threading
original = _SnapshotTask.run
def slow(task):
    time.sleep(.8)
    original(task)
_SnapshotTask.run = slow
app = QtWidgets.QApplication([])
window = HistoryViewerWindow({str(root)!r})
bridge = CommandBridge()
bridge.received.connect(window.handle_command)
threading.Thread(target=bridge.read_commands, daemon=True).start()
snapshot = {{"mode":"curve", "kind":"vds_sweep", "signal":"Ids_DC", "x_name":"Vds", "x_unit":"V", "y_unit":"A", "view":{{"legend":True}}, "records":[{{"path":"synthetic.csv", "status":"finished", "metadata":{{}}}}], "traces":[{{"path":"synthetic.csv", "direction":"forward", "condition":"", "x":np.array([0.,1.]), "y":np.array([1e-9,2e-9])}}]}}
window.page.png_actions.exporter.submit_snapshot(snapshot, {str(target)!r})
window.show()
report("ready", pid=__import__('os').getpid(), hardware_modules=[])
sys.exit(app.exec())
''', encoding="utf-8")
            launcher = HistoryViewerLauncher(lambda: root)
            self.addCleanup(launcher.shutdown)
            messages, closed = [], []
            launcher.message_received.connect(messages.append)
            launcher.shutdown_ready.connect(lambda: closed.append(True))
            launcher.process.setProgram(__import__("sys").executable)
            launcher.process.setArguments(["-u", str(helper)])
            launcher.process.start()
            def wait_for(predicate):
                end = time.monotonic() + 15
                while time.monotonic() < end:
                    QtWidgets.QApplication.instance().processEvents()
                    if predicate(): return
                    QtTest.QTest.qWait(20)
                self.fail("Analysis snapshot handoff timed out")
            wait_for(lambda: bool(messages))
            self.assertFalse(launcher.prepare_shutdown())
            wait_for(lambda: bool(closed))
            self.assertTrue(any(message.get("event") == "close_ready" for message in messages))
            # PNG/CSV paths are reserved before rendering. The view sidecar is
            # committed last, after every PNG and CSV has been fully written.
            sidecar = target.with_name("analysis_view.json")
            wait_for(lambda: sidecar.exists() and sidecar.stat().st_size > 0)
            payload = json.loads(sidecar.read_text(encoding="utf-8"))
            self.assertTrue(target.stat().st_size > 0)
            with Path(payload["csv_files"][0]["path"]).open(encoding="utf-8-sig", newline="") as stream:
                rows = list(csv.reader(stream))
            self.assertEqual(rows[3:], [["0.0", "1e-09"], ["1.0", "2e-09"]])
