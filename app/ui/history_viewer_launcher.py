"""Lightweight measurement-app entry to an isolated read-only viewer process."""
from __future__ import annotations

import json
import sys
from pathlib import Path

from PySide6 import QtCore, QtWidgets
from app.settings import get_app_settings


class HistoryViewerLauncher(QtWidgets.QWidget):
    message_received = QtCore.Signal(object)
    shutdown_ready = QtCore.Signal()

    def __init__(self, folder_callable, parent=None):
        super().__init__(parent)
        self.folder_callable = folder_callable
        self.process = QtCore.QProcess(self)
        self._stdout = bytearray()
        self._stderr = ""
        self._closing = False
        self._deferred_shutdown = False
        self._reloading = False
        self._reload_opening = False
        self._reload_session = None
        self._png_exporter = None
        self._png_reload_done = True
        self._reload_reopen = False
        self._reload_error = ""
        self.process.started.connect(self._started)
        self.process.readyReadStandardOutput.connect(self._read_stdout)
        self.process.readyReadStandardError.connect(self._read_stderr)
        self.process.errorOccurred.connect(self._error)
        self.process.finished.connect(self._finished)
        self.refresh_timer = QtCore.QTimer(self)
        self.refresh_timer.setSingleShot(True)
        self.refresh_timer.setInterval(500)
        self.refresh_timer.timeout.connect(lambda: self._send("refresh"))
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.addStretch()
        title = QtWidgets.QLabel("Measurement history & curve comparison")
        title.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(title)
        details = QtWidgets.QLabel("Browse all dates, newest first, with separate pages for each measurement type.\nThe viewer runs in its own process and reads saved data only.")
        details.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        details.setWordWrap(True)
        layout.addWidget(details)
        self.folder_label = QtWidgets.QLabel()
        self.folder_label.setWordWrap(True)
        self.folder_label.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.folder_label)
        self.open_button = QtWidgets.QPushButton("Open history / Show viewer")
        self.open_button.setProperty("role", "primary")
        self.open_button.clicked.connect(self.open_viewer)
        layout.addWidget(self.open_button, 0, QtCore.Qt.AlignmentFlag.AlignCenter)
        self.reload_button = QtWidgets.QPushButton("Reload analysis modules")
        self.reload_button.setAccessibleName("Reload analysis modules without restarting Transport")
        self.reload_button.setToolTip("Finish accepted exports, restart analysis and PNG processes, and restore the current view. Measurements keep running.")
        self.reload_button.clicked.connect(self.reload_analysis)
        layout.addWidget(self.reload_button, 0, QtCore.Qt.AlignmentFlag.AlignCenter)
        self.auto_png_check = QtWidgets.QCheckBox("Automatically save channel PNGs after measurements")
        self.auto_png_check.setChecked(get_app_settings().value("png_export/automatic", True, type=bool))
        self.auto_png_check.setToolTip("Save each available current channel in plots after acquisition cleanup. Drag / Drive also saves a 2×2 overview and 2D heatmaps, independent of the screen layout.")
        self.auto_png_check.toggled.connect(lambda enabled: get_app_settings().setValue("png_export/automatic", enabled))
        layout.addWidget(self.auto_png_check, 0, QtCore.Qt.AlignmentFlag.AlignCenter)
        self.png_status_label = QtWidgets.QLabel()
        self.png_status_label.setWordWrap(True)
        self.png_status_label.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.png_status_label)
        self.status_label = QtWidgets.QLabel("The viewer opens in an independent window.")
        self.status_label.setWordWrap(True)
        self.status_label.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.status_label)
        layout.addStretch()
        self.folder_changed()

    def open_viewer(self):
        if self._closing or self._reloading:
            return
        if self.process.state() != QtCore.QProcess.ProcessState.NotRunning:
            self._send("focus")
            return
        self._start_viewer()

    def _start_viewer(self):
        executable = Path(sys.executable)
        # The measurement launcher uses pythonw; IPC requires ordinary stdio.
        if executable.name.lower() == "pythonw.exe" and executable.with_name("python.exe").exists():
            executable = executable.with_name("python.exe")
        root = Path(__file__).resolve().parents[2]
        self._stdout.clear()
        self._stderr = ""
        self.process.setProgram(str(executable))
        self.process.setArguments(["-u", str(root / "transport_history_viewer.py"), "--folder", str(self.folder_callable()), "--connected"])
        self.process.setWorkingDirectory(str(root))
        self.status_label.setText("Opening independent history viewer…")
        self.process.start()

    def _started(self):
        self.folder_changed()
        if self._reloading and not self._reload_opening:
            self._send("reload")

    def bind_exporter(self, exporter):
        self._png_exporter = exporter
        exporter.reload_finished.connect(self._png_reloaded)

    def reload_analysis(self):
        if self._closing or self._reloading:
            return
        self._reloading = True
        self._reload_opening = False
        self._reload_session = None
        self._reload_error = ""
        self._reload_reopen = self.process.state() != QtCore.QProcess.ProcessState.NotRunning
        self._png_reload_done = self._png_exporter is None
        self.reload_button.setEnabled(False)
        self.open_button.setEnabled(False)
        self.status_label.setText("Reloading analysis… finishing accepted exports first. Measurements keep running.")
        if self._png_exporter is not None:
            self._png_exporter.request_reload()
        if self._reload_reopen:
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
        self.reload_button.setEnabled(not self._closing)
        self.open_button.setEnabled(not self._closing)
        self.status_label.setText("Analysis reload failed: " + error if error else "Analysis modules reloaded. Measurements continue in Transport.")

    def _send(self, command, **values):
        if self.process.state() == QtCore.QProcess.ProcessState.Running:
            self.process.write((json.dumps({"command": command, **values}) + "\n").encode("utf-8"))

    def folder_changed(self, *_):
        folder = str(self.folder_callable())
        self.folder_label.setText(folder)
        self._send("folder", folder=folder)

    def refresh(self, *_):
        if self.process.state() == QtCore.QProcess.ProcessState.Running:
            self.refresh_timer.start()

    def _read_stdout(self):
        self._stdout.extend(bytes(self.process.readAllStandardOutput()))
        while b"\n" in self._stdout:
            line, _, remainder = self._stdout.partition(b"\n")
            self._stdout = bytearray(remainder)
            try:
                message = json.loads(line.decode("utf-8"))
            except (UnicodeError, ValueError):
                continue
            if not isinstance(message, dict):
                continue
            if message.get("event") == "ready":
                self.status_label.setText("History viewer is open in an independent process. Measurements continue in Transport.")
                if self._reloading and self._reload_opening:
                    if self._reload_session is not None:
                        self._send("restore_session", session=self._reload_session)
                    else:
                        self._finish_reload()
            elif message.get("event") == "reload_requested":
                self.reload_analysis()
            elif message.get("event") == "reload_session" and self._reloading:
                self._reload_session = message.get("session")
            elif message.get("event") == "session_loaded" and self._reloading:
                self._finish_reload()
            elif message.get("event") == "session_error" and self._reloading:
                self._finish_reload(message.get("error", "Cannot restore analysis session"))
            self.message_received.emit(message)
            if message.get("event") == "close_ready" and self._deferred_shutdown:
                self.process.kill()

    def _read_stderr(self):
        self._stderr = (self._stderr + bytes(self.process.readAllStandardError()).decode("utf-8", errors="replace"))[-8000:]

    def _error(self, *_):
        if not self._closing:
            if self._reloading:
                self._finish_reload(self.process.errorString())
                return
            self.status_label.setText("History viewer could not start: " + self.process.errorString())

    def _finished(self, code, *_):
        if self._deferred_shutdown:
            self.shutdown_ready.emit()
        if not self._closing:
            if self._reloading:
                if self._reload_opening:
                    self._finish_reload("History viewer could not load updated code. " + self._stderr[-1000:])
                else:
                    self._continue_reload()
                return
            self.status_label.setText("History viewer closed. Open it again to continue." if code == 0 else "History viewer stopped. Open it again to retry.")
            self.status_label.setToolTip(self._stderr)

    def prepare_shutdown(self):
        self._reloading = self._reload_opening = False
        if self.process.state() == QtCore.QProcess.ProcessState.NotRunning:
            self.shutdown()
            return True
        if not self._deferred_shutdown:
            self._deferred_shutdown = True
            self._closing = True
            self.refresh_timer.stop()
            self._send("close")
            self.process.closeWriteChannel()
        return False

    def shutdown(self):
        self._closing = True
        self._reloading = self._reload_opening = False
        self.refresh_timer.stop()
        if self.process.state() != QtCore.QProcess.ProcessState.NotRunning:
            self._send("close")
            self.process.closeWriteChannel()
            # Only called after accepted measurement-app shutdown. Never wait
            # for viewer parsing/plotting on any acquisition or stop path.
            if not self.process.waitForFinished(150):
                self.process.kill()
                self.process.waitForFinished(150)
