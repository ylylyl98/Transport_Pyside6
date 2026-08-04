from __future__ import annotations

from PyQt6 import QtCore, QtGui, QtWidgets


class SafeComboBox(QtWidgets.QComboBox):
    """A combo box whose selection cannot be changed by mouse-wheel scrolling."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._read_only = False

    def setReadOnly(self, read_only: bool) -> None:
        self._read_only = bool(read_only)
        self.setProperty("readOnly", self._read_only)
        self.setFocusPolicy(
            QtCore.Qt.FocusPolicy.NoFocus
            if self._read_only
            else QtCore.Qt.FocusPolicy.StrongFocus
        )
        self.style().unpolish(self)
        self.style().polish(self)

    def isReadOnly(self) -> bool:
        return self._read_only

    def showPopup(self) -> None:
        if not self._read_only:
            super().showPopup()

    def mousePressEvent(self, event: QtGui.QMouseEvent) -> None:
        if self._read_only:
            event.accept()
            return
        super().mousePressEvent(event)

    def keyPressEvent(self, event: QtGui.QKeyEvent) -> None:
        if self._read_only:
            event.accept()
            return
        super().keyPressEvent(event)

    def wheelEvent(self, event: QtGui.QWheelEvent) -> None:
        # Let a containing scroll area consume the gesture without changing
        # an instrument or measurement selection under the pointer.
        event.ignore()
