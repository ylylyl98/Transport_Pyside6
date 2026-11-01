"""In-app, nonmodal software update download and installation dialog."""
from PySide6 import QtCore, QtWidgets


class UpdateDialog(QtWidgets.QDialog):
    def __init__(self, service, parent=None):
        super().__init__(parent)
        self.service = service
        self.setWindowTitle('Software update')
        self.setMinimumWidth(440)
        self.setModal(False)
        layout = QtWidgets.QVBoxLayout(self)
        self.version = QtWidgets.QLabel(self)
        self.message = QtWidgets.QLabel(self)
        self.message.setWordWrap(True)
        self.progress = QtWidgets.QProgressBar(self)
        self.progress.setRange(0, 1000)
        self.bytes_label = QtWidgets.QLabel(self)
        for widget in (self.version, self.message, self.progress, self.bytes_label):
            layout.addWidget(widget)
        buttons = QtWidgets.QHBoxLayout()
        self.download = QtWidgets.QPushButton('Download update', self)
        self.cancel = QtWidgets.QPushButton('Cancel download', self)
        self.install = QtWidgets.QPushButton('Exit and install', self)
        self.notes = QtWidgets.QPushButton('Release notes', self)
        self.later = QtWidgets.QPushButton('Later', self)
        for button in (self.download, self.cancel, self.install, self.notes, self.later):
            buttons.addWidget(button)
        layout.addLayout(buttons)
        self.download.clicked.connect(service.downloadUpdate)
        self.cancel.clicked.connect(service.cancelDownload)
        self.install.clicked.connect(service.installUpdate)
        self.notes.clicked.connect(service.openRelease)
        self.later.clicked.connect(service.hideDialog)
        service.changed.connect(self.refresh)
        self.timer = QtCore.QTimer(self)
        self.timer.setInterval(500)
        self.timer.timeout.connect(self.refresh)
        self.refresh()

    def refresh(self):
        s = self.service
        state = s.downloadState
        self.version.setText(f'Installed: {s.currentVersion or "unknown"}   Available: {s.latestVersion or "unknown"}')
        self.message.setText(s.downloadMessage or s.message)
        self.progress.setValue(round(max(0., min(1., s.downloadProgress)) * 1000))
        self.bytes_label.setText(f'{s.downloadedBytes / 1048576:.1f} / {s.totalBytes / 1048576:.1f} MiB')
        self.progress.setVisible(state in ('downloading', 'cancelling', 'ready', 'installing'))
        self.bytes_label.setVisible(state in ('downloading', 'cancelling', 'ready', 'installing'))
        self.download.setText('Retry download' if state == 'error' else 'Download update')
        self.download.setVisible(state in ('idle', 'error'))
        self.download.setEnabled(s.canDownload)
        self.cancel.setVisible(state in ('downloading', 'cancelling'))
        self.cancel.setEnabled(state == 'downloading')
        self.install.setVisible(state in ('ready', 'installing'))
        self.install.setEnabled(s.canInstall)
        self.notes.setEnabled(bool(s.releaseUrl))
        if s.dialogVisible:
            if not self.isVisible():
                self.show()
                self.raise_()
            if not self.timer.isActive(): self.timer.start()
        else:
            self.timer.stop()
            if self.isVisible(): self.hide()

    def reject(self):
        self.service.hideDialog()
