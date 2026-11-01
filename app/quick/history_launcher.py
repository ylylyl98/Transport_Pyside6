"""QtCore-only client for the independent Qt Quick history process."""
from __future__ import annotations

import json
from pathlib import Path

from PySide6 import QtCore
from app.process_command import worker_command


class HistoryLauncher(QtCore.QObject):
    changed = QtCore.Signal()
    message_received = QtCore.Signal(object)
    shutdown_ready = QtCore.Signal()

    def __init__(self, folder_callable, parent=None, offline=False):
        super().__init__(parent)
        self.folder_callable, self.offline = folder_callable, offline
        self.process = QtCore.QProcess(self)
        self._stdout, self._stderr = bytearray(), ""
        self._message = "Analysis opens in a separate read-only window."
        self._closing = self._deferred_shutdown = False
        self._opening = self._launch_failed = self._isolation_failed = False
        self._reloading = self._reload_opening = self._reload_reopen = False
        self._reload_session = None
        self._png_exporter = None
        self._png_reload_done = True
        self._reload_error = ""
        self.process.started.connect(self._started)
        self.process.readyReadStandardOutput.connect(self._read_stdout)
        self.process.readyReadStandardError.connect(self._read_stderr)
        self.process.errorOccurred.connect(self._error)
        self.process.finished.connect(self._finished)
        self.open_timer = QtCore.QTimer(self)
        self.open_timer.setSingleShot(True)
        self.open_timer.setInterval(10000)
        self.open_timer.timeout.connect(self._opening_slow)
        self.refresh_timer = QtCore.QTimer(self)
        self.refresh_timer.setInterval(500)
        self.refresh_timer.setSingleShot(True)
        self.refresh_timer.timeout.connect(lambda: self._send("refresh"))

    message = QtCore.Property(str, lambda s: s._message, notify=changed)
    opening = QtCore.Property(bool, lambda s: s._opening, notify=changed)
    launchFailed = QtCore.Property(bool, lambda s: s._launch_failed, notify=changed)
    busy = QtCore.Property(bool, lambda s: s._opening or s._reloading or s.process.state() == QtCore.QProcess.ProcessState.Starting, notify=changed)

    def _opening_slow(self):
        if self._opening and not self._closing:
            self._set_message("Analysis is still opening. Measurements continue; please wait.")

    def _set_message(self, text):
        self._message = text
        self.changed.emit()

    @QtCore.Slot()
    def open(self):
        if self._closing or self._reloading or self._opening:
            return
        if self.process.state() != QtCore.QProcess.ProcessState.NotRunning:
            self._send("focus")
        elif self._reload_session is not None or self._reload_reopen:
            self._reload_reopen = True
            self.reload()
        else:
            self._start_viewer()

    def _start_viewer(self):
        command = worker_command("history", "--folder", str(self.folder_callable()), "--connected")
        self._stdout.clear()
        self._stderr = ""
        self._opening = True
        self._launch_failed = self._isolation_failed = False
        self.open_timer.start()
        self.process.setProgram(command[0])
        self.process.setArguments(command[1:])
        self.process.setWorkingDirectory(str(Path(__file__).resolve().parents[2]))
        self._set_message("Opening Analysis in a separate read-only window…")
        self.process.start()
        self.changed.emit()

    def _started(self):
        self.folderChanged()
        if self._reloading and not self._reload_opening:
            self._send("reload")
        self.changed.emit()

    def bind_exporter(self, exporter):
        self._png_exporter = exporter
        exporter.reload_finished.connect(self._png_reloaded)

    @QtCore.Slot()
    def reload(self):
        if self._closing or self._reloading:
            return
        running = self.process.state() != QtCore.QProcess.ProcessState.NotRunning
        self._reloading = True
        self._reload_opening = False
        self._reload_error = ""
        self._reload_reopen = self._reload_reopen or running
        self._png_reload_done = self._png_exporter is None
        self._set_message("Reloading analysis… finishing accepted exports first. Measurements continue.")
        if self._png_exporter:
            self._png_exporter.request_reload()
        if running:
            self._send("reload")
        self._continue_reload()

    def _png_reloaded(self, result):
        if not self._reloading or self._closing:
            return
        self._png_reload_done = True
        if not result.get("ok"):
            self._reload_error = "PNG renderer: " + result.get("error", "reload failed")
        self._continue_reload()

    def _continue_reload(self):
        if not self._reloading or self._closing or self._reload_opening or not self._png_reload_done or self.process.state() != QtCore.QProcess.ProcessState.NotRunning:
            return
        if self._reload_reopen:
            self._reload_opening = True
            self._start_viewer()
        else:
            self._finish_reload()

    def _finish_reload(self, error=""):
        error = error or self._reload_error
        self._reloading = self._reload_opening = False
        self._set_message("Analysis reload failed: " + error if error else "Analysis modules reloaded. Measurements continue.")

    def _viewer_restored(self):
        self._reload_session = None
        self._reload_reopen = False
        self._finish_reload()

    def _send(self, command, **values):
        if self.process.state() == QtCore.QProcess.ProcessState.Running:
            self.process.write((json.dumps({"command": command, **values}) + "\n").encode("utf-8"))

    @QtCore.Slot()
    def folderChanged(self):
        self._send("folder", folder=str(self.folder_callable()))

    @QtCore.Slot()
    def refresh(self):
        if self.process.state() == QtCore.QProcess.ProcessState.Running:
            self.refresh_timer.start()

    def _read_stdout(self):
        self._stdout.extend(bytes(self.process.readAllStandardOutput()))
        while b"\n" in self._stdout:
            line, _, remainder = self._stdout.partition(b"\n")
            self._stdout = bytearray(remainder)
            try:
                message = json.loads(line.decode("utf-8"))
            except (ValueError, UnicodeError):
                continue
            if not isinstance(message, dict):
                continue
            event = message.get("event")
            if event == "ready":
                self._opening = False
                self.open_timer.stop()
                if message.get("hardware_modules"):
                    self._isolation_failed = self._launch_failed = True
                    self._set_message("Analysis isolation check failed: acquisition modules were loaded")
                    self._send("close")
                elif self._reloading and self._reload_opening:
                    if self._reload_session is not None:
                        self._send("restore_session", session=self._reload_session)
                    else:
                        self._viewer_restored()
                else:
                    self._set_message("Analysis is open in a separate window. Measurements continue.")
            elif event == "reload_requested":
                self.reload()
            elif event == "reload_session" and self._reloading:
                if self._reload_session is None:
                    self._reload_session = message.get("session")
            elif event == "session_loaded" and self._reloading:
                self._viewer_restored()
            elif event == "session_error" and self._reloading:
                self._finish_reload(message.get("error", "Cannot restore analysis session"))
            self.message_received.emit(message)
            # close_ready is emitted only after snapshot serialization has
            # finished. The independent renderer drains accepted jobs itself.
            if event == "close_ready" and self._deferred_shutdown:
                self.process.kill()

    def _read_stderr(self):
        self._stderr = (self._stderr + bytes(self.process.readAllStandardError()).decode("utf-8", errors="replace"))[-8000:]

    def _error(self, *_):
        self._opening = False
        self.open_timer.stop()
        if self._closing:
            return
        self._launch_failed = True
        if self._reloading:
            self._finish_reload(self.process.errorString())
        else:
            self._set_message("Analysis could not start: " + self.process.errorString())

    def _finished(self, code, exit_status=QtCore.QProcess.ExitStatus.NormalExit):
        self._read_stdout()
        self._read_stderr()
        was_opening = self._opening
        self._opening = False
        self.open_timer.stop()
        if self._deferred_shutdown:
            self.shutdown_ready.emit()
        if self._closing:
            return
        if self._reloading:
            if self._reload_opening:
                self._finish_reload("Analysis could not load updated code. " + self._stderr[-1200:])
            else:
                self._continue_reload()
            return
        normal = code == 0 and exit_status == QtCore.QProcess.ExitStatus.NormalExit
        if self._isolation_failed:
            self._set_message("Analysis isolation check failed: acquisition modules were loaded")
            return
        self._launch_failed = was_opening or not normal or self._launch_failed
        if normal:
            self._reload_reopen = False
        if was_opening:
            self._set_message("Analysis closed before it was ready. Try opening it again. " + self._stderr[-1000:])
        elif normal and not self._launch_failed:
            self._set_message("Analysis closed. Open it again to continue.")
        elif not normal:
            self._set_message("Analysis stopped: " + self._stderr[-1000:])

    def prepare_shutdown(self):
        self._reloading = self._reload_opening = False
        if self.process.state() == QtCore.QProcess.ProcessState.NotRunning:
            self.shutdown()
            return True
        if not self._deferred_shutdown:
            self._deferred_shutdown = self._closing = True
            self.refresh_timer.stop()
            self._send("close")
            self.process.closeWriteChannel()
        return False

    def shutdown(self):
        self._closing = True
        self._opening = False
        self.open_timer.stop()
        self._reloading = self._reload_opening = False
        self.refresh_timer.stop()
        if self.process.state() != QtCore.QProcess.ProcessState.NotRunning:
            self._send("close")
            self.process.closeWriteChannel()
            # Acquisition never calls a blocking wait: normal shutdown uses
            # prepare_shutdown and waits asynchronously for shutdown_ready.

    open_viewer = open
    reload_analysis = reload
    folder_changed = folderChanged
