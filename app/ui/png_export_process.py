"""Nonblocking Qt client for a detached serial, low-priority PNG renderer."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from uuid import uuid4

from PySide6 import QtCore
from app.background_process import is_process_running


class _SnapshotTask(QtCore.QRunnable):
    def __init__(self, snapshot, path):
        super().__init__()
        self.snapshot, self.path = snapshot, path

    def run(self):
        from app.png_export import save_snapshot
        temporary = self.path.with_suffix(".tmp")
        try:
            save_snapshot(self.snapshot, temporary)
            os.replace(temporary, self.path)
        except Exception as exc:
            self.path.with_suffix(".error.json").write_text(json.dumps({"error": str(exc)}), encoding="utf-8")
        finally:
            temporary.unlink(missing_ok=True)


class PngExportProcess(QtCore.QObject):
    ready = QtCore.Signal(object)
    completed = QtCore.Signal(object)
    reload_finished = QtCore.Signal(object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.spool = Path(tempfile.mkdtemp(prefix="transport-png-"))
        self._pending = {}
        self._pid = None
        self._announced = False
        self._closed = False
        self._reloading = False
        self._reload_starting = False
        self._sequence = 0
        self._last_activity = time.monotonic()
        self.timer = QtCore.QTimer(self)
        self.timer.setInterval(100)
        self.timer.timeout.connect(self._poll)
        self.pool = QtCore.QThreadPool(self)
        self.pool.setMaxThreadCount(1)

    def new_snapshot_path(self):
        return self.spool / f"{uuid4().hex}.npz"

    @property
    def busy(self):
        return bool(self._pending or self.pool.activeThreadCount())

    def request_reload(self):
        """Drain this generation; durably stage new work for the next one."""
        if self._closed or self._reloading:
            return
        if self._pid is None and not self._pending:
            self.reload_finished.emit({"ok": True})
            return
        self._reloading = True
        self._reload_starting = False
        if self._pid is None:
            self._promote_waiting()
            self._reload_starting = True
            self._launch_worker()
            self.timer.start()
            return
        (self.spool / "stop").touch()
        self.timer.start()

    def _promote_waiting(self):
        for path in self.spool.glob("*.job.waiting"):
            os.replace(path, path.with_suffix(".json"))

    def _worker_exited(self):
        return (self.spool / "exited.json").exists() or (type(self._pid) is int and not is_process_running(self._pid))

    def _finish_reload(self, error=""):
        self._reloading = self._reload_starting = False
        self.reload_finished.emit({"ok": not bool(error), "error": error})

    def submit_snapshot(self, snapshot, output=None, cut_only=False, *, heatmap_only=False):
        path = self.new_snapshot_path()
        number = self.submit({"kind": "analysis", "snapshot_path": str(path), "wait_for_snapshot": True,
                              "output": str(output) if output is not None else None, "cut_only": bool(cut_only), "heatmap_only": bool(heatmap_only)})
        self.pool.start(_SnapshotTask(snapshot, path))
        return number

    def submit(self, job):
        if self._closed:
            raise RuntimeError("PNG exporter has closed")
        self._sequence += 1
        number = f"{self._sequence:08d}_{uuid4().hex}"
        path = self.spool / f"{number}.job.json"
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps({**job, "id": number}, ensure_ascii=False), encoding="utf-8")
        waiting = self._reloading and not self._reload_starting
        os.replace(temporary, path.with_suffix(".waiting") if waiting else path)
        self._pending[number] = path
        self._last_activity = time.monotonic()
        if not waiting and (self._pid is None or (self.spool / "exited.json").exists()):
            self._launch_worker()
        self.timer.start()
        return number

    def _launch_worker(self, after_pid=None):
        self._announced = False
        if after_pid is None:
            for name in ("exited.json", "ready.json", "boot.json", "fatal.json", "stop"):
                (self.spool / name).unlink(missing_ok=True)
        executable = Path(sys.executable)
        if os.name == "nt" and executable.with_name("pythonw.exe").exists():
            executable = executable.with_name("pythonw.exe")
        root = Path(__file__).resolve().parents[2]
        arguments = [str(root / "transport_png_exporter.py"), "--spool", str(self.spool), "--owner-pid", str(os.getpid())]
        if after_pid is not None:
            arguments += ["--after-pid", str(after_pid)]
        # The viewer receives parent commands on stdin. On Windows, inheriting
        # that pipe in the detached pythonw worker makes its reader see EOF and
        # closes the viewer. The worker communicates exclusively through spool
        # files, so none of its standard streams should inherit viewer IPC.
        # Popen also supplies a PID before Python writes boot.json, allowing
        # startup failures to be detected during a renderer reload.
        detached = ({"creationflags": subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP}
                    if os.name == "nt" else {"start_new_session": True})
        try:
            with (self.spool / "worker-stderr.log").open("ab") as stderr:
                process = subprocess.Popen(
                    [str(executable), *arguments], executable=str(executable), cwd=str(root),
                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=stderr,
                    close_fds=True, **detached)
        except OSError as exc:
            self._pid = None
            error = "PNG background process could not start: " + str(exc)
            if self._reloading:
                self._reload_failed(error)
            else:
                self._fail_all(error)
            return
        self._pid = process.pid
        # Reap the child without blocking Qt or tying its lifetime to the GUI.
        threading.Thread(target=process.wait, daemon=True, name="png-worker-reaper").start()

    def _reload_failed(self, error):
        # Updated code may be temporarily broken. Preserve accepted work on
        # disk so Reload can retry it once the code has been repaired.
        self._pid = None
        self.timer.stop()
        self._finish_reload(error)

    def _fail_all(self, error):
        for number, path in list(self._pending.items()):
            path.unlink(missing_ok=True)
            path.with_suffix(".waiting").unlink(missing_ok=True)
            self.completed.emit({"id": number, "outputs": [], "error": error})
        self._pending.clear()
        self.timer.stop()
        if self._reloading:
            self._finish_reload(error)

    def _poll(self):
        if (self.spool / "fatal.json").exists():
            error = "PNG worker could not initialize: " + json.loads((self.spool / "fatal.json").read_text(encoding="utf-8"))["error"]
            if self._reloading:
                self._reload_failed(error)
            else:
                self._fail_all(error)
            self._pid = None
            return
        if (self.spool / "boot.json").exists():
            self._pid = json.loads((self.spool / "boot.json").read_text(encoding="utf-8"))["pid"]
        if not self._announced and (self.spool / "ready.json").exists():
            self._announced = True
            ready = json.loads((self.spool / "ready.json").read_text(encoding="utf-8"))
            self._pid = ready["pid"]
            self.ready.emit(ready)
            if self._reload_starting:
                self._finish_reload()
        for number, path in list(self._pending.items()):
            result = path.with_name(path.name.replace(".job.json", ".result.json"))
            if result.exists():
                message = json.loads(result.read_text(encoding="utf-8"))
                self._pending.pop(number)
                self.completed.emit(message)
                result.unlink(missing_ok=True)
                self._last_activity = time.monotonic()
        if self._reload_starting and not self._announced and self._worker_exited():
            self._reload_failed("PNG replacement process exited before it was ready; accepted jobs remain queued. Repair the code and reload again.")
        elif self._reloading and not self._reload_starting and self._worker_exited():
            self._promote_waiting()
            self._reload_starting = True
            self._launch_worker()
        elif not self._pending and not self._reloading:
            self.timer.stop()
        elif not self._reloading and self._worker_exited():
            # Preserve durable accepted jobs when a worker exits; rendering a
            # large map may take time and is never canceled by a wall timeout.
            self._launch_worker()

    def shutdown(self):
        """Leave accepted jobs to drain in the detached process after GUI close."""
        if self._closed:
            return
        self._closed = True
        self.timer.stop()
        if self._reloading and not self._reload_starting and self._pending and type(self._pid) is int:
            # A stopping worker may already have observed an empty queue but
            # not yet written its exit marker. A detached successor waits for
            # its actual exit, then promotes the staged generation itself.
            old_pid = self._pid
            (self.spool / "stop").touch()
            self._reloading = self._reload_starting = False
            self._launch_worker(after_pid=old_pid)
            return
        self._promote_waiting()
        self._reloading = self._reload_starting = False
        if self._pending and (self._pid is None or self._worker_exited()):
            self._launch_worker()
        (self.spool / "stop").touch()
