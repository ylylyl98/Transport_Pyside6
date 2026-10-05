"""Standalone history viewer. Deliberately imports no instrument/control modules."""
from __future__ import annotations

import argparse
import ctypes
import json
import os
import sys
import threading
from pathlib import Path

from PySide6 import QtCore, QtWidgets

from app.app_identity import configure_qapp, set_windows_app_id
from app.ui.curve_compare import CurveComparePage
from app.ui.style import APP_STYLE


def report(event, **values):
    if sys.stdout is not None:
        sys.stdout.write(json.dumps({"event": event, **values}) + "\n")
        sys.stdout.flush()


class CommandBridge(QtCore.QObject):
    received = QtCore.Signal(object)

    def read_commands(self):
        if sys.stdin is None:
            return
        for line in sys.stdin:
            try:
                message = json.loads(line)
            except ValueError:
                continue
            if isinstance(message, dict):
                self.received.emit(message)
        self.received.emit({"command": "close", "reason": "parent-eof"})


class HistoryViewerWindow(QtWidgets.QMainWindow):
    def __init__(self, folder, connected=False):
        super().__init__()
        self.current_folder = Path(folder)
        self.setWindowTitle("Transport — Measurement History")
        self.resize(1280, 850)
        self.page = CurveComparePage(lambda: self.current_folder)
        self.setCentralWidget(self.page)
        self._reload_session = None
        self.page.reload_button.setVisible(connected)
        self.page.reload_requested.connect(lambda: report("reload_requested"))

    @QtCore.Slot(object)
    def handle_command(self, message):
        command = message.get("command")
        if command == "folder" and isinstance(message.get("folder"), str):
            self.current_folder = Path(message["folder"])
            self.page.follow_current_folder()
            report("folder", folder=str(self.current_folder))
        elif command == "refresh":
            self.page.refresh()
        elif command == "focus":
            self.showNormal() if self.isMinimized() else self.show()
            self.raise_()
            self.activateWindow()
        elif command == "close":
            # EOF can follow a reload while the export queue is draining. It
            # means the connection ended, not that a saved handoff is canceled.
            if message.get("reason") != "parent-eof":
                self._reload_session = None
            self.close()
        elif command == "reload":
            if self._reload_session is None:
                self._reload_session = self.page.capture_session()
                self._reload_session["geometry"] = bytes(self.saveGeometry().toBase64()).decode("ascii")
                self.page.setEnabled(False)
                self.page.refresh_timer.stop()
                self.page.plot_timer.stop()
                self.page.export_status_label.setText("Reloading analysis… finishing accepted exports first.")
                self.close()
        elif command == "restore_session":
            try:
                session = message["session"]
                self.page.restore_session(session)
                if session.get("geometry"):
                    self.restoreGeometry(QtCore.QByteArray.fromBase64(session["geometry"].encode("ascii")))
                report("session_loaded", session=self.page.capture_session())
            except (OSError, ValueError, KeyError, TypeError) as exc:
                report("session_error", error=str(exc))

    def closeEvent(self, event):
        if self.page.png_actions.exporter.pool.activeThreadCount() or (self._reload_session is not None and self.page.png_actions.exporter.busy):
            # Finish durable snapshot handoff before Transport may terminate
            # this GUI process. The detached renderer owns the remaining work.
            self.page.refresh_timer.stop()
            self.page.plot_timer.stop()
            QtCore.QTimer.singleShot(50, self.close)
            event.ignore()
            return
        if self._reload_session is not None:
            report("reload_session", session=self._reload_session)
        report("close_ready")
        self.page.shutdown()
        super().closeEvent(event)


def main():
    parser = argparse.ArgumentParser(description="Read-only Transport measurement history")
    parser.add_argument("--folder", required=True)
    parser.add_argument("--connected", action="store_true", help="Follow directory/refresh notifications from Transport over stdin")
    args = parser.parse_args()
    # Lower only this read-only process's scheduling priority. Instrument
    # ownership stays entirely in Transport; both still share disk resources.
    if sys.platform == "win32":
        kernel = ctypes.windll.kernel32
        kernel.SetPriorityClass.argtypes = (ctypes.c_void_p, ctypes.c_ulong)
        kernel.SetPriorityClass(ctypes.c_void_p(-1), 0x4000)  # BELOW_NORMAL_PRIORITY_CLASS
    elif hasattr(os, "nice"):
        os.nice(5)
    set_windows_app_id()
    application = QtWidgets.QApplication(sys.argv)
    configure_qapp(application, app_name="Transport History")
    application.setStyleSheet(APP_STYLE)
    window = HistoryViewerWindow(args.folder, connected=args.connected)
    bridge = CommandBridge()
    bridge.received.connect(window.handle_command)
    if args.connected:
        threading.Thread(target=bridge.read_commands, daemon=True, name="history-notifications").start()
    window.show()
    forbidden = ("instruments", "pyvisa", "nidaqmx", "app.device_manager", "app.engine", "utils.config")
    report("ready", pid=os.getpid(), folder=str(window.current_folder), hardware_modules=[name for name in sys.modules if any(name == prefix or name.startswith(prefix + ".") for prefix in forbidden)])
    return application.exec()


if __name__ == "__main__":
    raise SystemExit(main())
