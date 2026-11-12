from PySide6 import QtCore


class UnavailableController(QtCore.QObject):
    """Signal-only placeholder; it owns no worker and never simulates hardware."""
    connected = QtCore.Signal(object)
    disconnected = QtCore.Signal()
    snapshot_updated = QtCore.Signal(object)
    temperature_updated = QtCore.Signal(object)
    transition_progress = QtCore.Signal(object)
    operation_finished = QtCore.Signal(str, bool, object)
    error = QtCore.Signal(str)
    fault = QtCore.Signal(str)
    state_changed = QtCore.Signal(object)
    control_result = QtCore.Signal(object)
    exclusive_changed = QtCore.Signal(object)
    is_connected = False
    has_pending_work = False
    exclusive_owner = None
    latest_snapshot = None
    input_configuration_pending = False
