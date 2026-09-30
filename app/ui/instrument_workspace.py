"""Window-level instrument controls, independent of experiment parameter pages."""
from PySide6 import QtCore, QtWidgets
from app.settings import get_app_settings


class InstrumentWorkspace(QtWidgets.QWidget):
    def __init__(self, connections, magnet, lockin, manager, parent=None):
        super().__init__(parent)
        self.connections = connections
        self.manager = manager
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)
        self.summary = QtWidgets.QLabel()
        self.summary.setWordWrap(True)
        self.summary.setTextFormat(QtCore.Qt.TextFormat.PlainText)
        self.summary.setSizePolicy(QtWidgets.QSizePolicy.Policy.Ignored, QtWidgets.QSizePolicy.Policy.Preferred)
        layout.addWidget(self.summary)
        manage = QtWidgets.QPushButton("Manage connections...")
        manage.clicked.connect(self.show_devices)
        layout.addWidget(manage)
        self.pages = QtWidgets.QTabWidget()
        self.pages.addTab(self.scroll(connections), "Gate / DAQ")
        self.pages.addTab(self.scroll(magnet), "Magnet")
        self.pages.addTab(self.scroll(lockin), "Lock-in")
        layout.addWidget(self.pages, 1)
        self.devices_dialog = self.make_dialog("Device management", [
            ("Connections", connections.exp_connections),
            ("Addresses", connections.exp_hw),
            ("Protection", connections.exp_protection),
        ])
        self.experiment_dialog = self.make_dialog("Sample and output", [("Sample / Files", connections.exp_save)])
        self.signal_dialog = self.make_dialog("Signal chain", [("Signal chain", connections.exp_signal_chain)])
        self.pages.setCurrentIndex(max(0, min(2, int(get_app_settings().value("workspace/instrument_page", 0)))))
        self.pages.currentChanged.connect(self.save_page)
        manager.status_changed.connect(self.refresh_summary)
        manager.resources_changed.connect(self.refresh_summary)
        self.refresh_summary()

    @staticmethod
    def scroll(widget):
        scroll = QtWidgets.QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setSizeAdjustPolicy(QtWidgets.QAbstractScrollArea.SizeAdjustPolicy.AdjustIgnored)
        scroll.setMinimumWidth(0)
        widget.setMinimumWidth(0)
        widget.setSizePolicy(QtWidgets.QSizePolicy.Policy.Ignored, QtWidgets.QSizePolicy.Policy.Preferred)
        scroll.setWidget(widget)
        return scroll

    def make_dialog(self, title, sections):
        dialog = QtWidgets.QDialog(self.window())
        dialog.setWindowTitle(title)
        dialog.resize(570, 640)
        dialog.setModal(False)
        layout = QtWidgets.QVBoxLayout(dialog)
        tabs = QtWidgets.QTabWidget()
        for label, section in sections:
            self.connections.layout().removeWidget(section)
            section.set_expanded(True)
            section.toggle_button.hide()
            tabs.addTab(self.scroll(section), label)
        layout.addWidget(tabs)
        buttons = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(dialog.close)
        layout.addWidget(buttons)
        return dialog

    def show_dialog(self, dialog):
        dialog.show()
        dialog.raise_()
        dialog.activateWindow()

    def show_devices(self):
        self.show_dialog(self.devices_dialog)

    def show_experiment(self):
        self.show_dialog(self.experiment_dialog)

    def show_signal_chain(self):
        self.show_dialog(self.signal_dialog)

    def save_page(self, index):
        get_app_settings().setValue("workspace/instrument_page", index)

    def refresh_summary(self, *_args):
        connected = [name.upper() for name in self.manager.sessions if self.manager.is_connected(name)]
        busy = sorted(self.manager.current_in_use())
        text = "Gate / DAQ / Lock-in connected: " + (", ".join(connected) if connected else "none")
        if busy:
            text += "\nIn use: " + ", ".join(name.upper() for name in busy)
        self.summary.setText(text)
