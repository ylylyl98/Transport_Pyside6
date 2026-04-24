from __future__ import annotations

from typing import List, Optional

from PyQt6 import QtWidgets
from PyQt6.QtCore import Qt


class SectionHeader(QtWidgets.QLabel):
    def __init__(self, text: str, parent=None):
        super().__init__(text.upper(), parent)
        self.setProperty("role", "section-header")


class DeviceStatusItem(QtWidgets.QWidget):
    STATUS_TEXT = {
        "ok": "OK",
        "idle": "Disconnected",
        "err": "Error",
        "busy": "Busy",
        "warn": "Warning",
    }

    def __init__(self, name: str, label_text: str, parent=None):
        super().__init__(parent)
        self.name = name
        self.label_text = label_text
        self._detail = ""

        layout = QtWidgets.QHBoxLayout(self)
        layout.setContentsMargins(8, 6, 8, 6)
        layout.setSpacing(6)

        self.lbl_name = QtWidgets.QLabel(label_text)
        self.lbl_name.setProperty("role", "status-name")

        self.lbl_state = QtWidgets.QLabel("")
        self.lbl_state.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.lbl_state.setProperty("role", "status-pill")

        self.btn_details = QtWidgets.QToolButton()
        self.btn_details.setText("Details")
        self.btn_details.setAutoRaise(True)
        self.btn_details.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_details.setProperty("role", "status-detail")
        self.btn_details.clicked.connect(self._show_details)
        self.btn_details.hide()

        layout.addWidget(self.lbl_name)
        layout.addStretch(1)
        layout.addWidget(self.lbl_state)
        layout.addWidget(self.btn_details)

        self.setProperty("role", "status-item")
        self.set_status("idle", "")

    def set_status(self, state: str, detail: Optional[str] = None):
        detail = (detail or "").strip()
        self._detail = detail
        summary = self.STATUS_TEXT.get(state, state.title())
        self.lbl_state.setText(summary)
        self.lbl_state.setProperty("status", state)
        self.lbl_state.style().unpolish(self.lbl_state)
        self.lbl_state.style().polish(self.lbl_state)

        tooltip = f"{self.label_text}: {summary}"
        if detail:
            first_line = detail.splitlines()[0].strip()
            if first_line:
                tooltip += f"\n{first_line}"
        self.setToolTip(tooltip)
        self.lbl_name.setToolTip(tooltip)
        self.lbl_state.setToolTip(tooltip)

        self.btn_details.setVisible(bool(detail))
        if detail:
            self.btn_details.setToolTip("Open the full technical error details")
        else:
            self.btn_details.setToolTip("")

    def _show_details(self):
        if not self._detail:
            return
        dialog = QtWidgets.QMessageBox(self)
        dialog.setWindowTitle(f"{self.label_text} Details")
        dialog.setIcon(QtWidgets.QMessageBox.Icon.Warning)
        dialog.setText(f"{self.label_text} reported an error.")
        dialog.setInformativeText("Full technical details:")
        dialog.setDetailedText(self._detail)
        dialog.setStandardButtons(QtWidgets.QMessageBox.StandardButton.Ok)
        dialog.exec()


class StatusPanel(QtWidgets.QGroupBox):
    STATUS_TEXT = {
        "g1": "G1",
        "g2": "G2",
        "g3": "G3/Vds",
        "daq": "DAQ",
        "mono": "Mono",
    }

    def __init__(self, names: List[str], parent=None, columns: int | None = None):
        super().__init__("Device Status", parent)
        self._items: dict[str, DeviceStatusItem] = {}
        layout = QtWidgets.QGridLayout(self)
        layout.setContentsMargins(8, 14, 8, 8)
        layout.setHorizontalSpacing(8)
        layout.setVerticalSpacing(6)

        columns = columns if columns is not None else (2 if len(names) > 3 else len(names))
        columns = max(1, columns)

        for idx, name in enumerate(names):
            item = DeviceStatusItem(name, self.STATUS_TEXT.get(name, name.upper()), self)
            self._items[name] = item
            row = idx // columns
            col = idx % columns
            layout.addWidget(item, row, col)

        for col in range(columns):
            layout.setColumnStretch(col, 1)

    def label(self, name: str) -> QtWidgets.QWidget:
        return self._items[name]

    def set_status(self, name: str, state: str, detail: Optional[str] = None):
        self._items[name].set_status(state, detail)
