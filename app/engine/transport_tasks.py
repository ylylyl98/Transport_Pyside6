"""Serialized blocking work with completion delivered on the owning Qt thread."""
from concurrent.futures import ThreadPoolExecutor
from threading import Lock

from PyQt6 import QtCore


class TaskLane(QtCore.QObject):
    completed = QtCore.pyqtSignal(object, object, object)
    failed = QtCore.pyqtSignal(str)

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

    def publish(self, snapshot):
        with self._lock:
            old = getattr(self._snapshot, "monotonic_s", -1)
            new = getattr(snapshot, "monotonic_s", -1)
            if self._snapshot is None or new >= old:
                self._snapshot = snapshot

    def clear(self):
        with self._lock:
            self._snapshot = None

    def get(self):
        with self._lock:
            return self._snapshot
