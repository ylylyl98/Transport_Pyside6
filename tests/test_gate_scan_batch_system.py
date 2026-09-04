import unittest

from PyQt6 import QtCore
from PyQt6.QtWidgets import QApplication

from app.ui.magnet_panel import MagnetPanel


class _Fake1000(QtCore.QObject):
    connected = QtCore.pyqtSignal(object)
    disconnected = QtCore.pyqtSignal()
    snapshot_updated = QtCore.pyqtSignal(object)
    transition_progress = QtCore.pyqtSignal(str, float)
    operation_finished = QtCore.pyqtSignal(str)
    error = QtCore.pyqtSignal(str)
    fault = QtCore.pyqtSignal(str)


class _Fake2100(QtCore.QObject):
    connected = QtCore.pyqtSignal(object)
    disconnected = QtCore.pyqtSignal()
    snapshot_updated = QtCore.pyqtSignal(object)
    temperature_updated = QtCore.pyqtSignal(object)
    operation_finished = QtCore.pyqtSignal(str, bool, object)
    error = QtCore.pyqtSignal(str)
    fault = QtCore.pyqtSignal(str)


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
