"""Thread entry point: completion is emitted after run() and its finally return."""
from PySide6.QtCore import QObject, QThread, Signal, Slot, Qt
import threading


class PreflightThread(QThread):
    """Finish an in-flight read before releasing reserved instruments."""
    def __init__(self, callback, parent=None):
        super().__init__(parent)
        self.callback = callback
        self.cancel = threading.Event()
        self.result = None
        self.error = ""

    def run(self):
        try:
            if not self.cancel.is_set():
                self.result = self.callback()
        except BaseException as exc:
            self.error = str(exc)


class MeasurementThread(QThread):
    def __init__(self, worker, parent=None):
        super().__init__(parent)
        self.worker = worker
        self.unhandled_error = ""
        self.terminal = None
        worker.finished.connect(self._success, Qt.ConnectionType.DirectConnection)
        worker.stopped.connect(self._stopped, Qt.ConnectionType.DirectConnection)
        worker.error.connect(self._error, Qt.ConnectionType.DirectConnection)

    @Slot(str)
    def _success(self, detail): self.terminal = "finished", detail

    @Slot(str)
    def _stopped(self, detail): self.terminal = "stopped", detail

    @Slot(str)
    def _error(self, detail): self.terminal = "error", detail

    def run(self):
        try:
            self.worker.run()
        except BaseException as exc:
            self.unhandled_error = str(exc)


class WorkerRelay(QObject):
    """Keep run identity after a fast worker's QObject has been deleted."""
    event = Signal(int, str, object)
    def __init__(self, generation, parent=None):
        super().__init__(parent)
        self.generation = generation

    @Slot(object)
    def record(self, value): self.event.emit(self.generation, "record", value)

    @Slot(str)
    def status(self, value): self.event.emit(self.generation, "status", value)

    @Slot(str)
    def log(self, value): self.event.emit(self.generation, "log", value)

    @Slot(float)
    def progress(self, value): self.event.emit(self.generation, "progress", value)

    @Slot(str)
    def finished(self, value): self.event.emit(self.generation, "finished", value)

    @Slot(str)
    def stopped(self, value): self.event.emit(self.generation, "stopped", value)

    @Slot(str)
    def error(self, value): self.event.emit(self.generation, "error", value)
