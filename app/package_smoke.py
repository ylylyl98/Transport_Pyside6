"""Hardware-blocked acceptance checks executed inside the distributable EXE.

This module is imported only by the explicit --verify-package entry point. It
uses the production history and PNG clients, so missing bundled worker modules,
QML imports, plugins and numeric/rendering libraries fail the package check.
"""
from __future__ import annotations

import argparse
import contextlib
import json
import os
from pathlib import Path
import sys
import time
import traceback


def _wait(application, predicate, description, timeout=45):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        application.processEvents()
        if predicate():
            return
        time.sleep(.01)
    raise TimeoutError(description)


def _qml_errors(text):
    """Ignore platform notices, but fail QML and Python diagnostics."""
    markers = ("ReferenceError:", "TypeError:", "Traceback (most recent call last)",
               "QQmlApplicationEngine failed", "is not installed", "Cannot load library")
    return [line for line in text.splitlines()
            if ".qml:" in line or any(marker in line for marker in markers)]


def _history_check(application, launcher, output, report):
    from PySide6 import QtCore
    history_folder = output / "synthetic-history"
    history_folder.mkdir(exist_ok=True)
    launcher.folder_callable = lambda: history_folder
    messages = []
    launcher.message_received.connect(messages.append)
    try:
        launcher.open_viewer()
        _wait(application, lambda: any(m.get("event") == "ready" for m in messages)
              or launcher.process.state() == QtCore.QProcess.ProcessState.NotRunning,
              "History worker did not announce readiness")
        ready = next((m for m in messages if m.get("event") == "ready"), None)
        if not ready:
            raise RuntimeError("History worker failed to start: " + launcher._stderr)
        if ready.get("hardware_modules") or ready.get("pid") == os.getpid():
            raise RuntimeError("History worker isolation failed: " + repr(ready))
        report["history"] = ready
        # Let the worker acknowledge the same close command used by production
        # before invoking the launcher's bounded emergency-cleanup fallback.
        launcher._send("close")
        launcher.process.closeWriteChannel()
        _wait(application,
              lambda: launcher.process.state() == QtCore.QProcess.ProcessState.NotRunning,
              "History worker did not close", timeout=20)
        if launcher.process.exitCode() != 0:
            raise RuntimeError("History worker exited unsuccessfully: " + launcher._stderr)
        errors = _qml_errors(launcher._stderr)
        if errors:
            raise RuntimeError("History worker diagnostics: " + "\n".join(errors))
        if not any(m.get("event") == "close_ready" for m in messages):
            raise RuntimeError("History worker did not confirm durable shutdown")
        report["history"]["closed"] = True
    finally:
        launcher.shutdown()
        if launcher.process.state() != QtCore.QProcess.ProcessState.NotRunning:
            # This QProcess was created solely by this package check.
            launcher.process.kill()
            launcher.process.waitForFinished(5000)
        (output / "history-stderr.log").write_text(launcher._stderr, encoding="utf-8")
        (output / "history-events.json").write_text(json.dumps(messages, indent=2), encoding="utf-8")


def _png_check(application, exporter, output, report):
    from PySide6.QtGui import QImage
    from app.background_process import is_process_running
    from app.quick.plot import PlotModel
    model = PlotModel()
    ready, completed = [], []
    exporter.ready.connect(ready.append)
    exporter.completed.connect(completed.append)
    try:
        model.beginRun({"x_axis": "Vtg", "measurement": "gate_scan",
                        "csv_path": str(output / "synthetic.csv"),
                        "params": {"vds_source": "Keithley 2400"}})
        for value in range(12):
            model.appendRecord({"vtg": value, "Ids_DC": value * 1e-9,
                                "Ids_X": value * 2e-9, "Ids_Y": -value * 1e-9,
                                "Ids_Keithley": value * 3e-9})
        model.fourChannels = True
        target = output / "four-channel.png"
        exporter.submit_snapshot(model.exportSnapshot(), target)
        _wait(application, lambda: bool(completed), "PNG worker did not finish the export", timeout=90)
        if completed[0].get("error"):
            raise RuntimeError("PNG export failed: " + completed[0]["error"])
        if not ready or ready[0].get("hardware_modules") or ready[0].get("pid") == os.getpid():
            raise RuntimeError("PNG worker isolation failed: " + repr(ready))
        image = QImage(str(target))
        if image.isNull() or (image.width(), image.height()) != (2400, 1800):
            raise RuntimeError("PNG output is missing, corrupt or has the wrong dimensions")
        report["png"] = dict(ready[0], width=image.width(), height=image.height(),
                             bytes=target.stat().st_size, outputs=completed[0].get("outputs", []))
    finally:
        model._timer.stop()
        exporter.shutdown()
        if exporter._pid is not None:
            _wait(application, lambda: not is_process_running(exporter._pid),
                  "PNG worker did not drain and close", timeout=20)
        log = exporter.spool / "worker-stderr.log"
        if log.exists():
            (output / "png-stderr.log").write_bytes(log.read_bytes())


def _quick(application, output, report):
    from PySide6 import QtCore, QtGui, QtQml
    from PySide6.QtQuickControls2 import QQuickStyle
    from app.quick.application import ApplicationBackend
    from app.quick.plot import register_types
    QQuickStyle.setStyle("Fusion")
    register_types()
    backend = ApplicationBackend(offline=True)
    engine = QtQml.QQmlApplicationEngine()
    errors = []
    engine.warnings.connect(lambda items: errors.extend(str(item) for item in items))
    engine.rootContext().setContextProperty("appBackend", backend)
    try:
        engine.load(QtCore.QUrl.fromLocalFile(str(Path(__file__).parent / "quick/qml/Main.qml")))
        if len(engine.rootObjects()) != 1:
            raise RuntimeError("Main.qml failed to load: " + "\n".join(errors))
        window = engine.rootObjects()[0]
        pane = window.findChild(QtCore.QObject, "instrumentPane")
        if pane is None:
            raise RuntimeError("The main instrument panel is missing")
        for index in range(len(backend._measurement.modes)):
            backend._measurement.mode = index
            application.processEvents()
        for index in range(8):
            pane.setProperty("section", index)
            application.processEvents()
        backend.dark = True
        application.processEvents()
        if errors:
            raise RuntimeError("QML warnings: " + "\n".join(errors))
        icon = QtGui.QIcon(str(Path(__file__).resolve().parents[1] / "assets/transport.ico"))
        if icon.isNull() or icon.pixmap(32, 32).isNull():
            raise RuntimeError("The bundled application icon is missing")
        report["main_qml"] = dict(measurement_pages=len(backend._measurement.modes),
                                  instrument_pages=8, warnings=errors)
        _history_check(application, backend._history, output, report)
        _png_check(application, backend._exporter, output, report)
        if errors:
            raise RuntimeError("QML warnings: " + "\n".join(errors))
    finally:
        backend.requestClose()
        _wait(application, lambda: backend.closeAccepted,
              "Quick application did not close its offline services", timeout=20)
        for window in engine.rootObjects():
            window.hide()
        engine.deleteLater()
        application.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)
        application.processEvents()


def _widgets(application, output, report):
    from PySide6 import QtCore
    from app.ui.dock import ConnDock
    from app.ui.main_window import MainWindow
    # Match the offline startup test: enumeration is covered separately by
    # simulated discovery tests and is unnecessary for loading the fallback UI.
    original_scan = ConnDock._start_scan
    ConnDock._start_scan = lambda self: None
    window = None
    try:
        window = MainWindow()
        window.show()
        application.processEvents()
        if not window.isVisible() or any(window.device_manager.sessions.values()):
            raise RuntimeError("Widgets fallback failed to open without instrument sessions")
        window.tab_cosweep.plot.canvas.draw()
        report["widgets"] = dict(visible=True, plot_drawn=True)
        _history_check(application, window.tab_curve_compare, output, report)
        window.close()
        _wait(application, lambda: not window.isVisible(), "Widgets fallback did not close", timeout=20)
    finally:
        ConnDock._start_scan = original_scan
        if window is not None:
            window.tab_curve_compare.shutdown()
            window.png_exporter.shutdown()
            window.magnet1000.shutdown()
            window.lakeshore335.shutdown()
            window.magnet2100.shutdown()
            window.device_manager.shutdown()
            window.hide()
            window.deleteLater()
            application.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)
            application.processEvents()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output_directory", type=Path)
    parser.add_argument("--mode", choices=("quick", "widgets"), default="quick")
    args = parser.parse_args(argv)
    output = args.output_directory.resolve()
    output.mkdir(parents=True, exist_ok=True)
    report = dict(mode=args.mode, frozen=bool(getattr(sys, "frozen", False)),
                  executable=sys.executable, cwd=str(Path.cwd()), success=False,
                  hardware="blocked", settings="temporary")
    started = time.monotonic()
    with (output / "runtime.log").open("w", encoding="utf-8") as log:
        with contextlib.redirect_stdout(log), contextlib.redirect_stderr(log):
            original_hook = sys.excepthook
            exceptions = []
            def record_exception(kind, value, tb):
                exceptions.append(str(value))
                traceback.print_exception(kind, value, tb)
            sys.excepthook = record_exception
            try:
                from app.offline_sandbox import OfflineSandbox, blocked, configure_offscreen_font
                root = Path(__file__).resolve().parents[1]
                sandbox = OfflineSandbox(root, state_directory=output / "settings")
                # Prevent migration of any configuration from a bundled path.
                (sandbox.directory / "config.json").write_text("{}", encoding="utf-8")
                sandbox.install_hardware_guards()
                import app.devices.attodry2100_adapter
                app.devices.attodry2100_adapter._load_sdk = blocked
                from PySide6 import QtGui
                if args.mode == "widgets":
                    from PySide6.QtWidgets import QApplication
                    application = QApplication.instance() or QApplication([sys.argv[0]])
                else:
                    application = QtGui.QGuiApplication.instance() or QtGui.QGuiApplication([sys.argv[0]])
                application.setQuitOnLastWindowClosed(False)
                from app.app_identity import configure_qapp
                configure_qapp(application)
                configure_offscreen_font(application)
                (_quick if args.mode == "quick" else _widgets)(application, output, report)
                if exceptions:
                    raise RuntimeError("Unhandled Qt callback exceptions: " + "; ".join(exceptions))
                report["success"] = True
            except Exception as exc:
                report["error"] = f"{type(exc).__name__}: {exc}"
                traceback.print_exc()
            finally:
                sys.excepthook = original_hook
    report["elapsed_s"] = round(time.monotonic() - started, 2)
    (output / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    return 0 if report["success"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
