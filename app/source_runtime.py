"""Windows source-checkout instance coordination; no Qt imports at startup."""
from __future__ import annotations

import atexit
import ctypes
from ctypes import wintypes
import hashlib
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent.parent
_runtime = None


def root_hash(root):
    return hashlib.sha256(os.path.normpath(os.path.abspath(str(root))).lower().encode("utf-8")).hexdigest()[:16]


def names(root):
    tag = root_hash(root)
    base = "Local\\Transport.Source." + tag
    return {"hash": tag, "launch": base + ".Launch", "running": base + ".Running",
            "activate": base + ".Activate", "property": "Transport.Source." + tag}


def should_skip(arguments):
    return (sys.platform != "win32" or getattr(sys, "frozen", False)
            or os.environ.get("TRANSPORT_OFFLINE_VERIFICATION") == "1"
            or os.environ.get("TRANSPORT_UI_PREVIEW") == "1"
            or any(a in arguments for a in ("--preview", "--history", "--history-widgets", "--png-worker", "--verify-package", "--check", "--screenshot")))


class _Kernel:
    def __init__(self):
        self.k = ctypes.WinDLL("kernel32", use_last_error=True)
        self.k.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
        self.k.CreateMutexW.restype = wintypes.HANDLE
        self.k.CreateEventW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR]
        self.k.CreateEventW.restype = wintypes.HANDLE
        self.k.OpenEventW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
        self.k.OpenEventW.restype = wintypes.HANDLE
        for name in ("CloseHandle", "ReleaseMutex", "SetEvent"):
            getattr(self.k, name).argtypes = [wintypes.HANDLE]
            getattr(self.k, name).restype = wintypes.BOOL
        self.k.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        self.k.WaitForSingleObject.restype = wintypes.DWORD
        self.k.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        self.k.OpenProcess.restype = wintypes.HANDLE

    def mutex(self, name):
        handle = self.k.CreateMutexW(None, False, name)
        if not handle: raise ctypes.WinError(ctypes.get_last_error())
        return handle

    def wait(self, handle, ms):
        result = self.k.WaitForSingleObject(handle, ms)
        if result not in (0, 0x80, 0x102): raise ctypes.WinError(ctypes.get_last_error())
        return result != 0x102

    def event(self, name):
        handle = self.k.CreateEventW(None, False, False, name)
        if not handle: raise ctypes.WinError(ctypes.get_last_error())
        return handle

    def signal(self, name):
        handle = self.k.OpenEventW(2, False, name)
        if handle:
            try:
                if not self.k.SetEvent(handle): raise ctypes.WinError(ctypes.get_last_error())
            finally: self.k.CloseHandle(handle)
        return bool(handle)

    def alive(self, pid):
        handle = self.k.OpenProcess(0x100000, False, pid)
        if not handle: return False
        try: return not self.wait(handle, 0)
        finally: self.k.CloseHandle(handle)


class SourceRuntime:
    def __init__(self, root, kernel=None):
        self.root, self.names, self.kernel = Path(root), names(root), kernel or _Kernel()
        self.running = self.activate = None

    def begin(self, managed=False, ready=None):
        k = self.kernel
        launch = None
        launch_owned = False
        try:
            if not managed:
                launch = k.mutex(self.names["launch"])
                launch_owned = k.wait(launch, 0)
                if not launch_owned:
                    k.signal(self.names["activate"])
                    return False
            self.running = k.mutex(self.names["running"])
            if not k.wait(self.running, 0):
                k.k.CloseHandle(self.running)
                self.running = None
                k.signal(self.names["activate"])
                return False
            self.activate = k.event(self.names["activate"])
            if ready and not k.signal(ready):
                raise RuntimeError("Source launcher ready event is unavailable")
            atexit.register(self.close)
            return True
        except Exception:
            self.close()
            raise
        finally:
            if launch:
                if launch_owned:
                    k.k.ReleaseMutex(launch)
                k.k.CloseHandle(launch)

    def close(self):
        if self.activate:
            self.kernel.k.CloseHandle(self.activate)
            self.activate = None
        if self.running:
            self.kernel.k.ReleaseMutex(self.running)
            self.kernel.k.CloseHandle(self.running)
            self.running = None


def begin(arguments, root=ROOT):
    global _runtime
    if should_skip(arguments): return True
    runtime = SourceRuntime(root)
    managed_root = os.environ.get("TRANSPORT_SOURCE_MANAGED_ROOT", "")
    ready = os.environ.get("TRANSPORT_SOURCE_READY_EVENT", "")
    try: pid = int(os.environ.get("TRANSPORT_SOURCE_LAUNCHER_PID", ""))
    except ValueError: pid = 0
    managed = bool(managed_root and root_hash(managed_root) == root_hash(root) and pid > 0 and runtime.kernel.alive(pid) and ready.startswith("Local\\Transport.Source." + runtime.names["hash"] + ".Ready."))
    if not runtime.begin(managed, ready if managed else None): return False
    _runtime = runtime
    return True


def register_window(window, frontend="quick"):
    if _runtime is None: return
    from PySide6.QtCore import QTimer, Qt
    from app.app_identity import set_source_window_identity
    hwnd = int(window.winId())
    set_source_window_identity(hwnd, _runtime.root, _runtime.names["hash"], frontend)
    user = ctypes.WinDLL("user32", use_last_error=True)
    user.SetPropW.argtypes = [wintypes.HWND, wintypes.LPCWSTR, wintypes.HANDLE]
    user.SetPropW.restype = wintypes.BOOL
    user.SetForegroundWindow.argtypes = [wintypes.HWND]
    user.SetForegroundWindow.restype = wintypes.BOOL
    user.ShowWindowAsync.argtypes = [wintypes.HWND, ctypes.c_int]
    user.ShowWindowAsync.restype = wintypes.BOOL
    if not user.SetPropW(hwnd, _runtime.names["property"], 1): raise ctypes.WinError(ctypes.get_last_error())
    window.setTitle(window.title() + " [Source]") if hasattr(window, "setTitle") else window.setWindowTitle(window.windowTitle() + " [Source]")
    timer = QTimer(window)
    def activate():
        if _runtime and _runtime.kernel.wait(_runtime.activate, 0):
            if window.windowState() & Qt.WindowState.WindowMinimized:
                user.ShowWindowAsync(hwnd, 9)  # SW_RESTORE retains the previous maximized state.
            window.raise_()
            window.requestActivate() if hasattr(window, "requestActivate") else window.activateWindow()
            user.SetForegroundWindow(hwnd)
    timer.timeout.connect(activate)
    timer.start(250)
    window._source_activation_timer = timer
