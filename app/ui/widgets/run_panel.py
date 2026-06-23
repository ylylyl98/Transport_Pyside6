from __future__ import annotations

from PyQt6 import QtWidgets
from PyQt6.QtCore import Qt


class RunPanel(QtWidgets.QWidget):
    def __init__(self, start_text: str, parent=None):
        super().__init__(parent)
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)

        row = QtWidgets.QHBoxLayout()
        row.setSpacing(6)
        self.btn_start = QtWidgets.QPushButton(start_text)
        self.btn_start.setProperty("role", "primary")
        self.btn_start.setMinimumHeight(36)
        self.btn_start.setMaximumHeight(36)
        self.btn_stop = QtWidgets.QPushButton("STOP")
        self.btn_stop.setProperty("role", "danger")
        self.btn_stop.setMinimumHeight(36)
        self.btn_stop.setMaximumHeight(36)
        self.btn_stop.setMaximumWidth(80)
        self.progress = QtWidgets.QProgressBar()
        self.progress.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.progress.setRange(0, 100)
        self.progress.setFormat("%p%")
        row.addWidget(self.btn_start)
        row.addWidget(self.btn_stop)
        row.addWidget(self.progress, 1)
        layout.addLayout(row)

        status_row = QtWidgets.QHBoxLayout()
        status_row.setSpacing(6)
        self.lbl_status = QtWidgets.QLabel("Idle")
        self.lbl_status.setProperty("role", "run-status")
        self.btn_status_details = QtWidgets.QToolButton()
        self.btn_status_details.setText("Details")
        self.btn_status_details.setAutoRaise(True)
        self.btn_status_details.setProperty("role", "status-detail")
        self.btn_status_details.clicked.connect(self._show_details)
        self.btn_status_details.hide()
        status_row.addWidget(self.lbl_status, 1)
        status_row.addWidget(self.btn_status_details, 0, Qt.AlignmentFlag.AlignRight)
        layout.addLayout(status_row)
        self._status_detail = ""
        self.set_running(False)
        self.set_status_text("Idle", "idle")

    def set_running(self, running: bool):
        self.btn_start.setEnabled(not running)
        self.btn_stop.setEnabled(running)

    def set_progress_fraction(self, fraction: float):
        self.progress.setValue(int(max(0.0, min(1.0, fraction)) * 100))

    def set_status_text(self, text: str, state: str = "idle", detail: str = ""):
        self._status_detail = detail.strip()
        self.lbl_status.setText(text)
        self.lbl_status.setProperty("state", state)
        self.lbl_status.style().unpolish(self.lbl_status)
        self.lbl_status.style().polish(self.lbl_status)
        self.lbl_status.setToolTip(self._status_detail or text)
        self.btn_status_details.setVisible(bool(self._status_detail))
        self.btn_status_details.setToolTip("Open full status details" if self._status_detail else "")

    def _show_details(self):
        if not self._status_detail:
            return
        dialog = QtWidgets.QMessageBox(self)
        dialog.setWindowTitle("Status Details")
        dialog.setIcon(QtWidgets.QMessageBox.Icon.Information)
        dialog.setText(self.lbl_status.text())
        dialog.setDetailedText(self._status_detail)
        dialog.setStandardButtons(QtWidgets.QMessageBox.StandardButton.Ok)
        dialog.exec()
