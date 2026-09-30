from __future__ import annotations

import time

from PySide6.QtCore import QObject, Signal


class RunStopped(RuntimeError):
    pass


class RunWorker(QObject):
    point = Signal(float, float)
    point_data = Signal(object)
    status = Signal(str)
    log = Signal(str)
    progress = Signal(float)
    finished = Signal(str)
    stopped = Signal(str)
    error = Signal(str)
    clear_plot = Signal()

    def __init__(self):
        super().__init__()
        self._stop = False
        self._pause = False

    def request_stop(self):
        self._stop = True

    def request_pause(self, paused: bool):
        self._pause = paused

    def check_abort_pause(self):
        if self._stop:
            raise RunStopped("Stopped by user")
        while self._pause:
            time.sleep(0.05)
            if self._stop:
                raise RunStopped("Stopped by user")

    def emit_safe_state_report(self, failures: list[str]):
        if failures:
            self.log.emit("Safe-state warning: " + "; ".join(failures))
        else:
            self.log.emit("Safe state confirmed: outputs returned to 0 V; sessions kept open.")
