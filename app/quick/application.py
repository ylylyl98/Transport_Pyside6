"""Qt Quick application composition; instrument owners outlive loaded QML."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
import time
from datetime import datetime

from PySide6.QtCore import QObject, Property, Signal, Slot, QTimer, QUrl
from PySide6.QtGui import QGuiApplication
from PySide6.QtQml import QQmlApplicationEngine
from PySide6.QtQuickControls2 import QQuickStyle

from app.app_identity import configure_qapp, set_windows_app_id
from app.update_check import UpdateService
from app.settings import get_app_settings
from app.curve_history import device_history_folder
from app.quick.instruments import InstrumentService
from app.quick.lockin import LockinService
from app.quick.magnet import MagnetService
from app.quick.measurements import MeasurementService
from app.quick.plot import PlotModel, register_types
from app.quick.runtime import RuntimeSettings
from app.drag_drive import dual_enabled
from app.ui.png_export_process import PngExportProcess
from utils.config import cfg

ROOT = Path(__file__).resolve().parents[2]


class ApplicationBackend(QObject):
    changed = Signal()
    closeReady = Signal()
    chooseFolderRequested = Signal()
    choosePlotFileRequested = Signal()
    confirmationRequested = Signal(str, str, str)

    def __init__(self, parent=None, *, offline=False):
        super().__init__(parent)
        self._offline = offline
        self.settings = get_app_settings()
        self._updates = UpdateService(self, settings=self.settings)
        self._message = "OFFLINE PREVIEW — synthetic data; acquisition disabled" if offline else "Ready"
        self._closing = False
        self._hardware_closed = False
        self._close_accepted = False
        self._rebuild_requested = False
        self._prepared_2100 = False
        self._manual_wait_started_at = None
        self._confirmations = {}
        self._font_size = cfg.font_size_pt
        self._dark = self.settings.value("quick/theme/dark", False, type=bool)
        self._auto_png = self.settings.value("png_export/automatic", True, type=bool)
        self._instruments = InstrumentService(self, offline=offline)
        self._magnet = MagnetService(self, offline=offline)
        self._lockin = LockinService(self._instruments.manager, parent=self, offline=offline)
        self._drive_lockin = LockinService(self._instruments.manager, "lockin_drive", self, offline=offline)
        self._instruments.lockins = {"lockin": self._lockin, "lockin_drive": self._drive_lockin}
        for name, lockin in self._instruments.lockins.items():
            lockin.verified.connect(lambda data, name=name: self._instruments.setVerifiedLockin(name, data))
            lockin.sensitivityInvalidated.connect(lambda reason, name=name: self._instruments.invalidateLockin(name, reason))
        self._instruments.context_provider = self._magnet.experiment_context_snapshot
        self._measurement = MeasurementService(self._instruments.manager, self._instruments.save_root,
            self._instruments.get_rates, self._instruments.signal_chain_snapshot, self,
            magnet1000=self._magnet.magnet1000, thermal_safety=self._magnet.thermal_safety,
            lakeshore_controller=self._magnet.lakeshore335, selected_backend_callable=self._magnet.selected_backend,
            review_valid_callable=self._magnet.review_valid, temperature_provider=self._magnet.measurement_settings,
            temperature_ready_callable=self._magnet.is_ready, context_provider=self._magnet.experiment_context_snapshot,
            settings=self.settings)
        self._instruments.set_active_provider(lambda: self._measurement.active or self._closing)
        self._magnet.set_active_provider(lambda: self._measurement.active or self._closing)
        for lockin in (self._lockin, self._drive_lockin):
            lockin.set_active_provider(lambda: self._measurement.active or self._closing)
        self._plots = [PlotModel(self) for _ in self._measurement.modes]
        self._plot_flavors = ["drag_drive" if self._instruments.manager.connections.drag_drive_enabled else "ordinary"] * len(self._plots)
        self._loading_plot_preferences = False
        for mode, plot in enumerate(self._plots):
            self._load_plot_preferences(mode)
            plot.preferencesChanged.connect(lambda mode=mode: self._save_plot_preferences(mode))
        self._plot = self._plots[self._measurement.mode]
        self._measurement.modeChanged.connect(self._select_plot)
        self._measurement.recordReady.connect(lambda record: self._plot.appendRecord(record))
        self._measurement.runPrepared.connect(self._begin_plot)
        self._measurement.changed.connect(self._on_measurement_changed)
        self._magnet.readinessChanged.connect(lambda *_: self._measurement.changed.emit())
        self._measurement.errorRaised.connect(self.report)
        self._measurement.confirmationRequested.connect(lambda token, title, body: self._confirmation(self._measurement, token, title, body))
        self._magnet.confirmationRequested.connect(lambda token, title, body: self._confirmation(self._magnet, token, title, body))
        for presenter in (self._instruments, self._magnet, self._lockin, self._drive_lockin):
            presenter.messageRaised.connect(self.report)
        for lockin in (self._lockin, self._drive_lockin):
            lockin.stopForSettingsRequested.connect(self._measurement.stop)
        self._runtime = RuntimeSettings(cfg, self.is_idle, self._set_font_size, self)
        self._runtime.messageRaised.connect(self.report)
        from app.quick.history_launcher import HistoryLauncher
        self._history = HistoryLauncher(lambda: device_history_folder(self._instruments.save_root), self, offline=offline)
        self._instruments.sampleFieldsChanged.connect(self._history.folderChanged)
        self._instruments.sampleFieldsChanged.connect(self.changed)
        self._history.shutdown_ready.connect(self._retry_close)
        self._exporter = PngExportProcess(self)
        self._exporter.completed.connect(lambda result: self.report(str(result)))
        if hasattr(self._history, "bind_exporter"):
            self._history.bind_exporter(self._exporter)
        self._measurement.runCompleted.connect(self._completed)
        if hasattr(self._measurement, "shutdownReady"):
            self._measurement.shutdownReady.connect(self._retry_close)

    instruments = Property(QObject, lambda self: self._instruments, constant=True)
    magnet = Property(QObject, lambda self: self._magnet, constant=True)
    lockin = Property(QObject, lambda self: self._lockin, constant=True)
    driveLockin = Property(QObject, lambda self: self._drive_lockin, constant=True)
    measurement = Property(QObject, lambda self: self._measurement, constant=True)
    plotModel = Property(QObject, lambda self: self._plot, notify=changed)
    runtime = Property(QObject, lambda self: self._runtime, constant=True)
    history = Property(QObject, lambda self: self._history, constant=True)
    updates = Property(QObject, lambda self: self._updates, constant=True)
    offline = Property(bool, lambda self: self._offline, constant=True)
    message = Property(str, lambda self: self._message, notify=changed)
    closing = Property(bool, lambda self: self._closing, notify=changed)
    closeAccepted = Property(bool, lambda self: self._close_accepted, notify=changed)
    fontSize = Property(int, lambda self: self._font_size, notify=changed)

    @staticmethod
    def _existing_directory(path):
        directory = Path(path)
        while not directory.is_dir() and directory != directory.parent:
            directory = directory.parent
        return directory

    def _export_directory(self):
        return self._existing_directory(self.settings.value("quick/exportDirectory", "") or device_history_folder(self._instruments.save_root))

    dataFolderUrl = Property(QUrl, lambda self: QUrl.fromLocalFile(str(self._existing_directory(device_history_folder(self._instruments.save_root)))), notify=changed)
    # Show the intended destination even before the first run creates it.
    dataFolderPath = Property(str, lambda self: str(device_history_folder(self._instruments.save_root)), notify=changed)
    exportFolderUrl = Property(QUrl, lambda self: QUrl.fromLocalFile(str(self._export_directory())), notify=changed)
    suggestedPlotUrl = Property(QUrl, lambda self: QUrl.fromLocalFile(str(self._export_directory() /
        (self._measurement.modes[self._measurement.mode]["key"] + datetime.now().strftime("_%Y%m%d_%H%M%S.png")))), notify=changed)

    def _set_dark(self, dark):
        self._dark = bool(dark)
        self.settings.setValue("quick/theme/dark", self._dark)
        self.changed.emit()
    dark = Property(bool, lambda self: self._dark, _set_dark, notify=changed)

    def _set_auto_png(self, enabled):
        self._auto_png = bool(enabled)
        self.settings.setValue("png_export/automatic", self._auto_png)
        self.changed.emit()
    autoPng = Property(bool, lambda self: self._auto_png, _set_auto_png, notify=changed)

    def _set_font_size(self, size):
        self._font_size = int(size)
        self.changed.emit()

    @Slot(str)
    def report(self, message):
        self._message = str(message)
        self.changed.emit()

    def is_idle(self):
        return not (self._measurement.active or self._instruments.busy or self._magnet.busy or self._lockin.busy or self._drive_lockin.busy)

    def _plot_key(self, mode):
        key = self._measurement.modes[mode]["key"]
        return "map_2d" if key == "map" else key

    def _load_plot_preferences(self, mode):
        key, flavor = self._plot_key(mode), self._plot_flavors[mode]
        raw = self.settings.value(f"quick/plots/{key}/{flavor}", "")
        try:
            values = json.loads(raw) if raw else {}
            if not isinstance(values, dict):
                values = {}
        except (ValueError, TypeError):
            values = {}
        legacy_default = "4-Channel Compare" if flavor == "drag_drive" else self.settings.value(f"plot_mode/{key}", "Single Plot")
        legacy = self.settings.value(f"plot_mode/{key}/{flavor}", legacy_default)
        values.setdefault("fourChannels", legacy == "4-Channel Compare")
        self._loading_plot_preferences = True
        try:
            self._plots[mode].applyPreferences(values)
        finally:
            self._loading_plot_preferences = False

    def _save_plot_preferences(self, mode):
        if not self._loading_plot_preferences:
            key, flavor = self._plot_key(mode), self._plot_flavors[mode]
            self.settings.setValue(f"quick/plots/{key}/{flavor}", json.dumps(self._plots[mode].capturePreferences()))

    def _select_plot(self):
        self._plot = self._plots[self._measurement.mode]
        self.changed.emit()

    def _begin_plot(self, snapshot):
        mode = self._measurement.mode
        flavor = "drag_drive" if dual_enabled(snapshot.get("signal_chain")) else "ordinary"
        if flavor != self._plot_flavors[mode]:
            self._plot_flavors[mode] = flavor
            self._load_plot_preferences(mode)
        self._plot.beginRun(snapshot)
        # Recipe choices define a new run; range/layout remain view preferences.
        self._plot.applyPreferences({"xAxis": snapshot.get("params", {}).get("plot_x_axis", "Follow Sweep")})

    def _confirmation(self, source, token, title, body):
        self._confirmations[token] = source
        self.confirmationRequested.emit(token, title, body)

    @Slot(str, bool)
    def answerConfirmation(self, token, accepted):
        source = self._confirmations.pop(token, None)
        if source is self._measurement:
            source.answerConfirmation(token, accepted)
        elif source is self._magnet:
            source.confirm(token, accepted)

    @Slot()
    def emergencyStop(self):
        if self._offline:
            self.report("Offline preview: hardware commands are disabled")
            return
        self._confirmations.clear()
        self._measurement.stop()
        if self._magnet.selected_backend() == "1000":
            self._magnet.lakeshore335.heater_off()
        self._instruments.manager.emergency_stop()
        self._magnet.request_stop()
        self.report("Stop requested. Voltage ramps and magnet pause are awaiting instrument confirmation; this does not zero magnetic field.")

    @Slot(QUrl)
    def setDataFolder(self, url):
        if url.isLocalFile():
            self._instruments.setValue("base", url.toLocalFile())

    @Slot(QUrl)
    def exportPlot(self, url):
        try:
            if not url.isLocalFile():
                raise ValueError("Choose a local PNG file")
            target = Path(url.toLocalFile()).with_suffix(".png")
            self._exporter.submit_snapshot(self._plot.exportSnapshot(), target)
            self.settings.setValue("quick/exportDirectory", str(target.parent))
            self.report("Plot snapshot queued for independent PNG / CSV export")
        except Exception as exc:
            self.report("Export failed: " + str(exc))

    @Slot(str, 'QVariant', result='QVariant')
    def preference(self, key, default):
        return self.settings.value("quick/window/" + key, default)

    @Slot(str, 'QVariant')
    def savePreference(self, key, value):
        if hasattr(value, "toVariant"):
            value = value.toVariant()
        self.settings.setValue("quick/window/" + key, value)

    def _completed(self, output):
        self._plot.finishRun(output)
        self._history.refresh()
        if self._auto_png and not self._offline and isinstance(output, dict):
            for run in output.get("runs") or [output]:
                if run.get("csv_paths"):
                    self._exporter.submit({**run, "kind": "run"})

    @Slot()
    def rebuildPreview(self):
        if self._offline:
            self._rebuild_requested = True
            self.requestClose()

    def _on_measurement_changed(self):
        if self._closing and not self._measurement.active:
            QTimer.singleShot(0, self._retry_close)

    @Slot()
    def requestClose(self):
        if self._close_accepted:
            self.closeReady.emit()
            return
        if self._closing:
            return
        if self._lockin.busy or self._drive_lockin.busy:
            self.report("Wait for the lock-in operation to finish before closing")
            return
        if self._instruments._scan_busy or self._instruments.manager._operation_thread is not None:
            self.report("Wait for instrument connection or hardware scanning to finish before closing")
            return
        self._closing = True
        self._prepared_2100 = False
        self._manual_wait_started_at = None
        self._magnet.beginShutdown()
        self.report("Stopping and confirming cleanup before closing…")
        self._measurement.stop()
        self._confirmations.clear()
        self._magnet.request_stop()
        QTimer.singleShot(0, self._retry_close)

    def _retry_close(self):
        if not self._closing or self._close_accepted:
            return
        try:
            if not self._hardware_closed:
                if not self._measurement.prepare_shutdown():
                    return
                if not self._prepared_2100:
                    if not self._magnet.magnet2100.prepare_shutdown():
                        raise RuntimeError("attoDRY2100 Stop was not confirmed. Keep the application open and retry Stop / pause.")
                    self._prepared_2100 = True
                if self._magnet.manualBusy:
                    if self._manual_wait_started_at is None:
                        self._manual_wait_started_at = time.monotonic()
                    allowance = max(30., cfg.magnet.heater_warm_s + cfg.magnet.heater_cool_s + 15.)
                    if time.monotonic() - self._manual_wait_started_at > allowance:
                        raise RuntimeError("Manual instrument operation has not confirmed completion: " + ", ".join(self._magnet.pendingOperations))
                    self.report("Waiting for manual magnet / temperature commands to finish before closing…")
                    QTimer.singleShot(100, self._retry_close)
                    return
                self._instruments.manager.shutdown()
                if self._magnet.magnet2100.shutdown() is False:
                    raise RuntimeError("attoDRY2100 safe shutdown was not confirmed")
                self._magnet.magnet1000.shutdown()
                self._magnet.lakeshore335.shutdown()
                self._magnet._timer.stop()
                self._instruments.close()
                self._lockin.close()
                self._drive_lockin.close()
                self._exporter.shutdown()
                self._hardware_closed = True
            if not self._history.prepare_shutdown():
                return
            self.settings.sync()
            self._updates.close()
            self._close_accepted = True
            self.changed.emit()
            self.closeReady.emit()
        except Exception as exc:
            self._closing = False
            self._magnet.cancelShutdown()
            transport = getattr(self._measurement, "_transport", None)
            if transport is not None:
                transport.cancel_shutdown_preparation()
            self.report("Close cancelled: " + str(exc))


def main(argv=None):
    parser = argparse.ArgumentParser(description="Transport Measurement — Qt Quick")
    parser.add_argument("--preview", action="store_true")
    parser.add_argument("--screenshot", type=Path)
    parser.add_argument("--mode", type=int, default=None)
    parser.add_argument("--panel", type=int, default=None)
    parser.add_argument("--dark", action="store_true")
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)
    if args.preview and os.environ.get("TRANSPORT_UI_PREVIEW") != "1":
        raise RuntimeError("Run the guarded transport_quick_preview.py entry point for previews")
    set_windows_app_id()
    QQuickStyle.setStyle("Fusion")
    app = QGuiApplication.instance() or QGuiApplication(sys.argv[:1])
    app.setQuitOnLastWindowClosed(True)
    configure_qapp(app, app_name="Transport Quick Preview — OFFLINE" if args.preview else "Transport Measurement")
    if args.preview:
        from app.offline_sandbox import configure_offscreen_font
        configure_offscreen_font(app)
    register_types()
    backend = ApplicationBackend(offline=args.preview)
    if args.dark:
        backend.dark = True
    if args.mode is not None:
        backend._measurement.mode = args.mode
    elif args.preview:
        backend._measurement.mode = int(backend.preference("previewMode", 0))
    engine = QQmlApplicationEngine()
    engine.rootContext().setContextProperty("appBackend", backend)
    errors = []
    original_excepthook = sys.excepthook
    def record_exception(exception_type, exception, traceback):
        errors.append(f"{exception_type.__name__}: {exception}")
        original_excepthook(exception_type, exception, traceback)
    sys.excepthook = record_exception
    engine.warnings.connect(lambda warnings: errors.extend(str(w) for w in warnings))
    engine.load(QUrl.fromLocalFile(str(Path(__file__).parent / "qml" / "Main.qml")))
    if not engine.rootObjects():
        backend.requestClose()
        app.processEvents()
        raise RuntimeError("Qt Quick interface failed to load: " + "\n".join(errors))
    window = engine.rootObjects()[0]
    pane = window.findChild(QObject, "instrumentPane")
    if args.panel is not None or args.preview:
        pane.setProperty("section", args.panel if args.panel is not None else int(backend.preference("previewPanel", 0)))
    backend.closeReady.connect(window.close)
    app.aboutToQuit.connect(backend._updates.close)
    if not args.preview and not args.check and not args.screenshot:
        backend._updates.start()
    if args.preview:
        import numpy as np
        x = np.linspace(-1, 1, 401)
        backend._plot.setTraces([dict(x=x, y=(1e-9 * (x ** 3 + .08 * np.sin(15 * x))), label="Synthetic preview", channel="Ids_DC")])
    result = []
    if args.screenshot or args.check:
        def capture():
            ok = not errors
            if args.screenshot:
                args.screenshot.parent.mkdir(parents=True, exist_ok=True)
                ok = window.grabWindow().save(str(args.screenshot)) and ok
            if errors:
                print("\n".join(errors), file=sys.stderr)
            result.append(0 if ok else 1)
            backend.requestClose()
        QTimer.singleShot(1000, capture)
    try:
        exit_code = app.exec()
        if args.preview:
            backend.savePreference("previewMode", backend._measurement.mode)
            backend.savePreference("previewPanel", pane.property("section"))
            backend.settings.sync()
    finally:
        sys.excepthook = original_excepthook
    engine.deleteLater()
    app.processEvents()
    if backend._rebuild_requested:
        return 75
    return result[-1] if result else exit_code


if __name__ == "__main__":
    raise SystemExit(main())
