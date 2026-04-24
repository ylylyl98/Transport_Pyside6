from __future__ import annotations

import ctypes
import sys
from pathlib import Path

from PyQt6 import QtCore, QtGui, QtWidgets

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


def configure_qapp(app: QtWidgets.QApplication) -> None:
    app.setApplicationName(APP_NAME)
    app.setOrganizationName(APP_ORG)
    app.setApplicationDisplayName(APP_NAME)

    icon_path = Path(__file__).resolve().parent.parent / "assets" / "transport.ico"
    if icon_path.exists():
        app.setWindowIcon(QtGui.QIcon(str(icon_path)))
    else:
        app.setWindowIcon(_fallback_icon())


def _fallback_icon() -> QtGui.QIcon:
    pixmap = QtGui.QPixmap(64, 64)
    pixmap.fill(QtGui.QColor("#1D4ED8"))
    painter = QtGui.QPainter(pixmap)
    painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
    painter.setPen(QtCore.Qt.PenStyle.NoPen)
    painter.setBrush(QtGui.QColor("#0F172A"))
    painter.drawRoundedRect(8, 8, 48, 48, 10, 10)
    painter.setPen(QtGui.QColor("#FFFFFF"))
    font = painter.font()
    font.setBold(True)
    font.setPointSize(28)
    painter.setFont(font)
    painter.drawText(pixmap.rect(), QtCore.Qt.AlignmentFlag.AlignCenter, "T")
    painter.end()
    return QtGui.QIcon(pixmap)
