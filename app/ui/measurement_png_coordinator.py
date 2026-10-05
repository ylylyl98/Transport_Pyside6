"""Freeze run output paths and submit PNG work only after acquisition cleanup."""
from __future__ import annotations

from PySide6 import QtCore


class MeasurementPngCoordinator(QtCore.QObject):
    def __init__(self, tabs, exporter, enabled_callable, parent=None):
        super().__init__(parent)
        self.exporter, self.enabled_callable = exporter, enabled_callable
        self._pending = {}
        self.timer = QtCore.QTimer(self)
        self.timer.setInterval(250)
        self.timer.timeout.connect(self._flush_ready)
        for tab in tabs:
            tab.run_panel.running_changed.connect(lambda running, current=tab: self._running_changed(current, running))
            tab.run_panel.status_changed.connect(self._flush_ready)

    def _running_changed(self, tab, running):
        if running:
            if not self.enabled_callable():
                return
            params = getattr(tab, "p", None)
            controller = getattr(tab, "_execution_controller", None)
            transport = getattr(controller, "_transport_outputs", None)
            if transport is not None:
                job = {"csv_paths": list(transport.condition_csv_paths), "metadata_path": transport.manifest_path, "measurement": "bfield_transport"}
            else:
                csv = getattr(params, "output_csv_path", "")
                metadata = getattr(params, "output_metadata_path", "")
                if not csv and not metadata:
                    return
                job = {"csv_paths": [csv] if csv else [], "metadata_path": metadata}
            save = getattr(tab, "save", None)
            if save is not None:
                job["device_id"] = getattr(save, "device_id", "")
            self._pending[id(tab)] = (tab, {"kind": "run", **job})
        self._flush_ready()

    def _flush_ready(self, *_):
        for key, (tab, job) in list(self._pending.items()):
            controller = getattr(tab, "_execution_controller", None)
            if tab.run_panel.operation_active() or getattr(tab, "worker_thread", None) is not None or bool(getattr(controller, "active", False)):
                continue
            self._pending.pop(key)
            self.exporter.submit(job)
        if self._pending:
            self.timer.start()
        else:
            self.timer.stop()

    def shutdown(self):
        self._flush_ready()
        self.timer.stop()
