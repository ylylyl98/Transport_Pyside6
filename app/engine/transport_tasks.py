"""Serialized blocking work with completion delivered on the owning Qt thread."""
from concurrent.futures import ThreadPoolExecutor
from threading import Lock
from collections import deque
from contextlib import contextmanager

from PySide6 import QtCore


class TaskLane(QtCore.QObject):
    completed = QtCore.Signal(object, object, object)
    failed = QtCore.Signal(str)

    def __init__(self, name, parent=None):
        super().__init__(parent)
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix=name)
        self.pending = 0
        self.completed.connect(self._deliver, QtCore.Qt.ConnectionType.QueuedConnection)

    def submit(self, work, done):
        self.pending += 1
        future = self.executor.submit(work)

        def completed(result):
            try:
                value, error = result.result(), None
            except Exception as exc:
                value, error = None, exc
            self.completed.emit(done, value, error)

        future.add_done_callback(completed)

    def _deliver(self, done, value, error):
        self.pending -= 1
        try:
            done(value, error)
        except Exception as exc:
            self.failed.emit(f"Transport worker completion failed: {exc}")

    def close(self):
        self.executor.shutdown(wait=False)


class LatestTelemetry:
    """Worker-side reference cache; no Qt/UI work takes place in publish()."""
    def __init__(self):
        self._lock = Lock()
        self._snapshot = None
        self._recent = deque(maxlen=2)
        self._captures = []

    def publish(self, snapshot):
        with self._lock:
            old = getattr(self._snapshot, "monotonic_s", -1)
            new = getattr(snapshot, "monotonic_s", -1)
            if self._snapshot is None or new >= old:
                self._snapshot = snapshot
                self._recent.append(snapshot)
                for capture in self._captures:
                    if len(capture[0]) >= 100000:
                        capture[1] = "Field telemetry capture exceeded its limit"
                    elif capture[1] is None:
                        capture[0].append(snapshot)

    def clear(self):
        with self._lock:
            self._snapshot = None
            self._recent.clear()
            for capture in self._captures:
                capture[1] = "Field telemetry was cleared during acquisition"

    def get(self):
        with self._lock:
            return self._snapshot

    @contextmanager
    def capture_history(self):
        """Pin telemetry only while acquiring, including the preceding bracket."""
        with self._lock:
            capture = [list(self._recent), None]
            self._captures.append(capture)
        def read():
            with self._lock:
                if capture[1] is not None:
                    raise RuntimeError(capture[1])
                return tuple(capture[0])
        try:
            yield read
        finally:
            with self._lock:
                self._captures = [active for active in self._captures if active is not capture]
