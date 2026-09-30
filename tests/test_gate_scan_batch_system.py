import unittest

from PySide6 import QtCore
from PySide6.QtWidgets import QApplication

from app.ui.magnet_panel import MagnetPanel


class _Fake1000(QtCore.QObject):
    connected = QtCore.Signal(object)
    disconnected = QtCore.Signal()
    snapshot_updated = QtCore.Signal(object)
    transition_progress = QtCore.Signal(str, float)
    operation_finished = QtCore.Signal(str)
    error = QtCore.Signal(str)
    fault = QtCore.Signal(str)


class _Fake2100(QtCore.QObject):
    connected = QtCore.Signal(object)
    disconnected = QtCore.Signal()
    snapshot_updated = QtCore.Signal(object)
    temperature_updated = QtCore.Signal(object)
    operation_finished = QtCore.Signal(str, bool, object)
    error = QtCore.Signal(str)
    fault = QtCore.Signal(str)


class MagnetPanelBackendSignalTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_backend_changed_emits_selection_keys(self):
        panel = MagnetPanel(_Fake1000(), _Fake2100())
        seen = []
        panel.backend_changed.connect(seen.append)
        panel.backend_combo.setCurrentIndex(1)
        panel.backend_combo.setCurrentIndex(0)
        self.assertEqual(seen, ["2100", "1000"])
        panel.deleteLater()


if __name__ == "__main__":
    unittest.main()
