from __future__ import annotations

from PySide6 import QtWidgets
from PySide6.QtCore import Qt, Signal


class RunPanel(QtWidgets.QWidget):
    running_changed = Signal(bool)
    status_changed = Signal()
    readiness_changed = Signal()

    def __init__(self, start_text: str, parent=None):
        super().__init__(parent)
        self._running = False
        self._phase_state = "idle"
        self._start_available = True
        self._external_start_blocks: set[str] = set()
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)

        row = QtWidgets.QHBoxLayout()
        row.setSpacing(6)
        self.btn_start = QtWidgets.QPushButton(start_text)
        self.btn_start.setProperty("role", "primary")
        self.btn_start.setMinimumHeight(36)
        self.btn_start.setMaximumHeight(36)
        self.btn_stop = QtWidgets.QPushButton("Stop measurement")
        self.btn_stop.setToolTip("Stop only this measurement using its existing safe shutdown sequence")
        self.btn_stop.setProperty("role", "danger")
        self.btn_stop.setMinimumHeight(36)
        self.btn_stop.setMaximumHeight(36)
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
        self.lbl_phase = QtWidgets.QLabel("Idle")
        self.lbl_phase.setProperty("role", "phase-label")
        status_row.addWidget(self.lbl_phase)
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
        changed = self._running != bool(running)
        self._running = bool(running)
        self._refresh_start_enabled()
        self.btn_stop.setEnabled(self._running)
        if changed:
            self.running_changed.emit(self._running)

    def set_start_available(self, available: bool):
        """Set the tab-owned start condition without overriding external gates."""
        changed = self._start_available != bool(available)
        self._start_available = bool(available)
        self._refresh_start_enabled()
        if changed:
            self.readiness_changed.emit()

    def set_start_blocked(self, source: str, blocked: bool):
        """Add or remove one independent start block."""
        key = str(source).strip()
        if not key:
            raise ValueError("Start-block source must not be empty")
        changed = (key in self._external_start_blocks) != bool(blocked)
        if blocked:
            self._external_start_blocks.add(key)
        else:
            self._external_start_blocks.discard(key)
        self._refresh_start_enabled()
        if changed:
            self.readiness_changed.emit()

    def _refresh_start_enabled(self):
        self.btn_start.setEnabled(
            not self._running
            and self._start_available
            and not self._external_start_blocks
        )

    def set_progress_fraction(self, fraction: float):
        self.progress.setValue(int(max(0.0, min(1.0, fraction)) * 100))

    def set_status_text(self, text: str, state: str = "idle", detail: str = ""):
        self._phase_state = state
        self.lbl_phase.setText(self.phase_label(state, text))
        self._status_detail = detail.strip()
        self.lbl_status.setText(text)
        self.lbl_status.setVisible(str(text).casefold() != self.lbl_phase.text().casefold())
        self.lbl_status.setProperty("state", state)
        self.lbl_status.style().unpolish(self.lbl_status)
        self.lbl_status.style().polish(self.lbl_status)
        self.lbl_status.setToolTip(self._status_detail or text)
        self.btn_status_details.setVisible(bool(self._status_detail))
        self.btn_status_details.setToolTip("Open full status details" if self._status_detail else "")
        self.status_changed.emit()

    def operation_active(self):
        # Some workers finish before their asynchronous zero-return completes.
        return self._running or self._phase_state in {"stopping", "cleanup", "cleanup_overdue"}

    @staticmethod
    def phase_label(state, text=""):
        phases = {
            "idle": "Idle", "preparing": "Preparing", "configuring": "Preparing",
            "biasing": "Positioning", "positioning": "Positioning", "moving": "Positioning",
            "starting_sweep": "Preparing", "transitioning": "Positioning", "resuming": "Preparing",
            "thermal_wait": "Waiting", "settling": "Waiting", "cooldown": "Waiting",
            "thermal_hold": "Waiting", "thermal_warning": "Waiting", "endpoint_hold": "Waiting",
            "measuring": "Acquiring", "sweeping": "Acquiring", "stopping": "Stopping",
            "cleanup": "Stopping", "cleanup_overdue": "Stopping", "finished": "Complete",
            "complete": "Complete", "stopped": "Stopped", "failed": "Error", "error": "Error",
        }
        if state == "done":
            return "Stopped" if str(text).lower().startswith("stopped") else "Complete"
        if state == "running":
            lower = str(text).lower()
            if lower.startswith("ramping"):
                return "Positioning"
            if lower.startswith("point "):
                return "Acquiring"
            return "Running"
        return phases.get(state, str(state).replace("_", " ").capitalize())

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
