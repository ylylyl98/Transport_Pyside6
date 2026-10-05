from __future__ import annotations

import ctypes
import sys
from pathlib import Path

from PySide6 import QtCore, QtGui, QtWidgets

APP_NAME = "Transport Measurement"
APP_ORG = "MyLab"
APP_ID = "MyLab.TransportMeasurement"


def set_windows_app_id() -> None:
    if sys.platform != "win32":
        return
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_ID)
    except Exception:
        pass


def configure_qapp(app: QtWidgets.QApplication, *, app_name: str = APP_NAME) -> None:
    app.setApplicationName(app_name)
    app.setOrganizationName(APP_ORG)
    app.setApplicationDisplayName(app_name)

    icon_path = Path(__file__).resolve().parent.parent / "assets" / "transport.ico"
    icon = QtGui.QIcon(str(icon_path))
    # QIcon loads lazily: an existing but unreadable/corrupt file can still
    # produce a non-null QIcon with no usable image.
    if icon.isNull() or icon.pixmap(32, 32).isNull():
        icon = _fallback_icon()
    app.setWindowIcon(icon)


def _fallback_icon() -> QtGui.QIcon:
    icon = QtGui.QIcon()
    for size in (16, 20, 24, 32, 40, 48, 64, 128, 256):
        pixmap = QtGui.QPixmap(size, size)
        pixmap.fill(QtGui.QColor("#1D4ED8"))
        painter = QtGui.QPainter(pixmap)
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        painter.scale(size / 64, size / 64)
        painter.setPen(QtCore.Qt.PenStyle.NoPen)
        painter.setBrush(QtGui.QColor("#0F172A"))
        painter.drawRoundedRect(8, 8, 48, 48, 10, 10)
        painter.setPen(QtGui.QColor("#FFFFFF"))
        font = QtGui.QFont("Segoe UI")
        font.setBold(True)
        font.setPixelSize(38)
        painter.setFont(font)
        painter.drawText(QtCore.QRect(0, 0, 64, 64), QtCore.Qt.AlignmentFlag.AlignCenter, "T")
        painter.end()
        icon.addPixmap(pixmap)
    return icon
