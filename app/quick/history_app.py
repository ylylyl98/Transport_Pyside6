"""Independent QGuiApplication history process and existing JSON-line IPC."""
from __future__ import annotations

import argparse
import ctypes
import json
import os
import sys
import threading
from pathlib import Path

from PySide6 import QtCore, QtGui, QtQml, QtQuickControls2

from app.quick.history import HistoryBackend
from app.quick.plot import register_types


def report(event=None, **values):
    message = event if isinstance(event, dict) else {"event": event, **values}
    if sys.stdout is not None:
        sys.stdout.write(json.dumps(message) + "\n")
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


def lower_priority():
    if sys.platform == "win32":
        kernel = ctypes.windll.kernel32
        kernel.SetPriorityClass.argtypes = (ctypes.c_void_p, ctypes.c_ulong)
        kernel.SetPriorityClass(ctypes.c_void_p(-1), 0x4000)
    elif hasattr(os, "nice"):
        os.nice(5)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Qt Quick read-only Transport measurement history")
    parser.add_argument("--folder", required=True)
    parser.add_argument("--connected", action="store_true")
    parser.add_argument("--history", action="store_true")
    args = parser.parse_args(argv)
    lower_priority()
    QtQuickControls2.QQuickStyle.setStyle("Fusion")
    application = QtGui.QGuiApplication.instance() or QtGui.QGuiApplication([sys.argv[0]])
    application.setApplicationName("Transport Quick History")
    application.setOrganizationName("MyLab")
    application.setApplicationDisplayName("Transport — Measurement History")
    application.setWindowIcon(QtGui.QIcon(str(Path(__file__).resolve().parents[2] / "assets" / "transport.ico")))
    register_types()
    engine = QtQml.QQmlApplicationEngine()
    backend = HistoryBackend(args.folder)
    engine.rootContext().setContextProperty("historyBackend", backend)
    engine.rootContext().setContextProperty("historyConnected", args.connected)
    engine.load(QtCore.QUrl.fromLocalFile(str(Path(__file__).parent / "qml" / "HistoryWindow.qml")))
    if not engine.rootObjects():
        backend.shutdown()
        return 2
    window = engine.rootObjects()[0]
    closing = {"requested": False}
    close_timer = QtCore.QTimer()
    close_timer.setInterval(50)

    def close_when_durable():
        if backend.can_close():
            close_timer.stop()
            backend.shutdown()
            report("close_ready")
            window.setProperty("closeAllowed", True)
            window.close()
            application.quit()

    def request_close():
        if closing["requested"]:
            return
        closing["requested"] = True
        close_timer.start()
        close_when_durable()

    close_timer.timeout.connect(close_when_durable)
    backend.closeRequested.connect(request_close)
    backend.focusRequested.connect(lambda: (window.showNormal(), window.raise_(), window.requestActivate()))
    backend.reloadRequested.connect(lambda: report("reload_requested") if args.connected else backend.handleCommand({"command": "reload"}))
    backend.event.connect(report)
    bridge = CommandBridge()
    bridge.received.connect(backend.handleCommand)
    if args.connected:
        threading.Thread(target=bridge.read_commands, daemon=True, name="quick-history-notifications").start()
    forbidden = ("instruments", "pyvisa", "nidaqmx", "app.device_manager", "app.engine", "utils.config")
    report("ready", pid=os.getpid(), folder=backend.folder,
           widgets_loaded="PySide6.QtWidgets" in sys.modules,
           hardware_modules=[n for n in sys.modules if any(n == p or n.startswith(p + ".") for p in forbidden)])
    application.aboutToQuit.connect(backend.shutdown)
    code = application.exec()
    del engine
    return code


if __name__ == "__main__":
    raise SystemExit(main())
