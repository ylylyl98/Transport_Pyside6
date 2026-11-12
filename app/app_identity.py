from __future__ import annotations

import ctypes
import sys
import uuid
from pathlib import Path

from PySide6 import QtCore, QtGui

APP_NAME = "Transport Measurement"
APP_ORG = "MyLab"
APP_ID = "MyLab.TransportMeasurement"


def process_app_id():
    if getattr(sys, "frozen", False):
        return APP_ID
    from app import source_runtime
    root = source_runtime._runtime.root if source_runtime._runtime is not None else source_runtime.ROOT
    return APP_ID + ".Source." + source_runtime.root_hash(root)


def set_windows_app_id() -> None:
    if sys.platform != "win32":
        return
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(process_app_id())
    except Exception:
        pass


def source_relaunch(root, frontend="quick"):
    command = f'powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "{Path(root) / "tools" / "Start-Transport.ps1"}"'
    return command + (" -Frontend widgets" if frontend == "widgets" else "")


def set_source_window_identity(hwnd, root, tag, frontend="quick"):
    """Set shell properties with IPropertyStore, preserving the Qt settings namespace."""
    if sys.platform != "win32": return
    from ctypes import wintypes
    class GUID(ctypes.Structure):
        _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD), ("Data3", wintypes.WORD), ("Data4", ctypes.c_ubyte * 8)]
        @classmethod
        def parse(cls, text):
            return cls.from_buffer_copy(uuid.UUID(text).bytes_le)
    class PROPERTYKEY(ctypes.Structure):
        _fields_ = [("fmtid", GUID), ("pid", wintypes.DWORD)]
    class PROPVARIANT(ctypes.Structure):
        _fields_ = [("vt", wintypes.USHORT), ("reserved1", wintypes.USHORT), ("reserved2", wintypes.USHORT), ("reserved3", wintypes.USHORT), ("value", ctypes.c_void_p), ("padding", ctypes.c_void_p)]
    shell = ctypes.WinDLL("shell32", use_last_error=True)
    shell.SHGetPropertyStoreForWindow.argtypes = [wintypes.HWND, ctypes.POINTER(GUID), ctypes.POINTER(ctypes.c_void_p)]
    shell.SHGetPropertyStoreForWindow.restype = ctypes.c_long
    store = ctypes.c_void_p()
    iid = GUID.parse("886d8eeb-8cf2-4446-8d02-cdba1dbdcf99")
    hr = shell.SHGetPropertyStoreForWindow(hwnd, ctypes.byref(iid), ctypes.byref(store))
    if hr < 0: raise OSError(f"SHGetPropertyStoreForWindow failed: 0x{hr & 0xffffffff:08X}")
    vtable = ctypes.cast(store, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
    release = ctypes.WINFUNCTYPE(wintypes.ULONG, ctypes.c_void_p)(vtable[2])
    set_value = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, ctypes.POINTER(PROPERTYKEY), ctypes.POINTER(PROPVARIANT))(vtable[6])
    commit = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p)(vtable[7])
    fmtid = "9f4c2855-9f79-4b39-a8d0-e1d42de1d5f3"
    values = ((2, source_relaunch(root, frontend)), (4, APP_NAME + " [Source]"),
              (3, str(Path(root) / "assets" / "transport.ico") + ",0"), (5, APP_ID + ".Source." + tag))
    try:
        for pid, value in values:
            key = PROPERTYKEY(GUID.parse(fmtid), pid)
            buffer = ctypes.create_unicode_buffer(value)
            variant = PROPVARIANT(31, 0, 0, 0, ctypes.cast(buffer, ctypes.c_void_p), None)
            hr = set_value(store, ctypes.byref(key), ctypes.byref(variant))
            if hr < 0: raise OSError(f"SetValue failed: 0x{hr & 0xffffffff:08X}")
        hr = commit(store)
        if hr < 0: raise OSError(f"IPropertyStore.Commit failed: 0x{hr & 0xffffffff:08X}")
    finally:
        release(store)


def configure_qapp(app: QtGui.QGuiApplication, *, app_name: str = APP_NAME) -> None:
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
