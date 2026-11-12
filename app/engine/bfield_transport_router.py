from PySide6 import QtCore
from app.engine.bfield_transport_controller import BFieldTransportController


class BFieldTransportRouter(QtCore.QObject):
    continuation_changed = QtCore.Signal(bool, bool, str)
    state_changed = QtCore.Signal(str, str)
    progress_changed = QtCore.Signal(int, int, str)
    error = QtCore.Signal(str)
    restoration_warning = QtCore.Signal(str)
    finished = QtCore.Signal()
    stopped = QtCore.Signal(str)
    shutdown_ready = QtCore.Signal()

    def __init__(self, magnet1000, magnet2100, tab, device_manager, thermal_safety=None,
                 parent=None, selected_backend_callable=None, review_valid_callable=None):
        super().__init__(parent)
        self._backend = selected_backend_callable or (lambda: '1000')
        self._engines = {
            '1000': BFieldTransportController(magnet1000, tab, device_manager, thermal_safety, self),
        }
        if magnet2100 is not None:
            from app.engine.bfield2100_transport_controller import BField2100TransportController
            self._engines['2100'] = BField2100TransportController(
                magnet2100, tab, device_manager, self, review_valid_callable=review_valid_callable)
        self._locked_engine = None
        self._starting = False
        self._shutdown_pending = False
        for engine in self._engines.values():
            for name in ('continuation_changed', 'state_changed', 'progress_changed', 'error',
                         'restoration_warning', 'finished', 'stopped'):
                getattr(engine, name).connect(getattr(self, name))
            engine.shutdown_ready.connect(self._shutdown_progress)

    def _selected(self):
        if not self._starting and self._locked_engine is not None and not self._locked_engine.active and not self._locked_engine._cleanup_in_progress:
            self._locked_engine = None
        return self._locked_engine or self._engines[self._backend()]

    def __getattr__(self, name):
        # QObject construction may probe Python attributes before our engines exist.
        if name.startswith("__") or "_engines" not in self.__dict__:
            raise AttributeError(name)
        return getattr(self._selected(), name)

    @property
    def lakeshore_controller(self):
        return self._engines['1000'].lakeshore_controller

    @lakeshore_controller.setter
    def lakeshore_controller(self, value):
        self._engines['1000'].lakeshore_controller = value

    def start(self, *, verified_calibration=None):
        engine = self._engines[self._backend()]
        self._locked_engine = engine
        self._starting = True
        try:
            return engine.start(verified_calibration=verified_calibration)
        finally:
            self._starting = False
            if not engine.active and not engine._cleanup_in_progress:
                self._locked_engine = None

    @property
    def active(self):
        return any(e.active for e in self._engines.values())

    @property
    def _cleanup_in_progress(self):
        return any(e._cleanup_in_progress for e in self._engines.values())

    def stop(self):
        return self._selected().stop()

    def on_lakeshore_snapshot(self, *args):
        return self._engines['1000'].on_lakeshore_snapshot(*args)

    def on_lakeshore_disconnected(self, *args):
        return self._engines['1000'].on_lakeshore_disconnected(*args)

    def _on_fault(self, *args):
        return self._engines['1000']._on_fault(*args)

    def prepare_shutdown(self):
        results = [engine.prepare_shutdown() for engine in self._engines.values()]
        self._shutdown_pending = not all(results)
        return not self._shutdown_pending

    def cancel_shutdown_preparation(self):
        self._shutdown_pending = False
        for engine in self._engines.values():
            engine.cancel_shutdown_preparation()

    def _shutdown_progress(self):
        if self._shutdown_pending and self.prepare_shutdown():
            self.shutdown_ready.emit()
