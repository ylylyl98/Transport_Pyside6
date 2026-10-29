"""Shared process-local isolation for offline tests and the UI preview.

Install before importing application modules. This is a guard against accidental
instrument access in trusted development code, not an OS security boundary.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import shlex
import sys
import tempfile


def blocked(*_args, **_kwargs):
    raise RuntimeError("Real hardware access is disabled during offline verification / UI preview")


def audit_policy(root: Path, *, preview=False):
    root = Path(root)

    def audit(event, args):
        if event == "subprocess.Popen" and not preview:
            executable, command, cwd, _ = args
            # Permit only the renderer, with the same spool/owner constraints
            # in a source checkout and a frozen release smoke test.
            argv = ([part.strip('"') for part in shlex.split(command, posix=False)]
                    if isinstance(command, str) else command)
            interpreters = (Path(sys.executable), Path(sys.executable).with_name("pythonw.exe"))
            renderer = False
            if executable and len(argv) >= 2:
                if getattr(sys, "frozen", False):
                    renderer = (Path(executable) == Path(sys.executable).with_name("TransportWorker.exe")
                                and argv[1] == "--png-worker")
                else:
                    renderer = (Path(executable) in interpreters
                                and Path(argv[1]) == root / "transport_png_exporter.py")
            if (renderer and cwd and Path(cwd) == root
                    and len(argv) in (6, 8) and argv[0] == executable
                    and argv[2] == "--spool" and Path(argv[3]).parent == Path(tempfile.gettempdir())
                    and Path(argv[3]).name.startswith("transport-png-")
                    and argv[4] == "--owner-pid" and argv[5] == str(os.getpid())
                    and (len(argv) == 6 or (argv[6] == "--after-pid" and argv[7].isdigit()))):
                return
        if event in ("socket.connect", "socket.bind", "socket.sendto", "socket.sendmsg",
                     "socket.getaddrinfo", "socket.gethostbyname", "socket.gethostbyaddr",
                     "subprocess.Popen", "os.system"):
            raise RuntimeError("Offline verification blocks " + event)

    return audit


class OfflineSandbox:
    """Own isolated settings; only the preview opts into a reusable font cache."""

    def __init__(self, root, *, preview=False, offscreen=True, state_directory=None):
        self.root = Path(root).resolve()
        self.preview = preview
        self.scratch = None if state_directory else tempfile.TemporaryDirectory(prefix="transport-pyside6-check-")
        self.directory = Path(state_directory or self.scratch.name)
        self.directory.mkdir(parents=True, exist_ok=True)
        if offscreen:
            os.environ["QT_QPA_PLATFORM"] = "offscreen"
        os.environ["QT_API"] = "pyside6"
        os.environ["TRANSPORT_OFFLINE_VERIFICATION"] = "1"
        if preview:
            os.environ["TRANSPORT_UI_PREVIEW"] = "1"
            identity = hashlib.sha256(str(self.root).encode("utf-8")).hexdigest()[:12]
            cache = Path(tempfile.gettempdir()) / ("transport-ui-preview-cache-" + identity) / "matplotlib"
            cache.mkdir(parents=True, exist_ok=True)
            # A populated explicit file prevents legacy project config migration.
            config_file = self.directory / "config.json"
            if not config_file.exists():
                config_file.write_text("{}", encoding="utf-8")
        else:
            cache = self.directory / "matplotlib"
        os.environ["MPLCONFIGDIR"] = str(cache)
        os.environ["SPECTRALSWEEP_CONFIG_PATH"] = str(self.directory / "config.json")
        os.environ["APPDATA"] = str(self.directory)
        os.environ["LOCALAPPDATA"] = str(self.directory)
        sys.dont_write_bytecode = True
        sys.addaudithook(audit_policy(self.root, preview=preview))

    def install_hardware_guards(self):
        from PySide6 import QtCore
        for scope in (QtCore.QSettings.Scope.UserScope, QtCore.QSettings.Scope.SystemScope):
            QtCore.QSettings.setPath(QtCore.QSettings.Format.IniFormat, scope, str(self.directory))
        import pyvisa
        import nidaqmx
        import nidaqmx.system
        pyvisa.ResourceManager = blocked
        nidaqmx.Task = blocked
        nidaqmx.system.System.local = blocked
        import app.hw_discovery
        app.hw_discovery.scan_all = lambda: {"gpib": [], "asrl": [], "daq": []}
        from utils.config import cfg
        cfg.filename.base_out = str(self.directory)
        assert Path(app.hw_discovery.__file__).is_relative_to(self.root)
        if self.preview:
            # QProcess bypasses CPython's subprocess audit hook. A UI preview
            # never needs analysis, renderer, or arbitrary child processes.
            QtCore.QProcess.start = blocked
            QtCore.QProcess.startDetached = blocked
            QtCore.QProcess.execute = blocked
            import app.devices.attodry2100_adapter
            app.devices.attodry2100_adapter._load_sdk = blocked


def configure_offscreen_font(application):
    from PySide6 import QtGui
    font_file = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts" / "segoeui.ttf"
    if font_file.exists():
        QtGui.QFontDatabase.addApplicationFont(str(font_file))
        application.setFont(QtGui.QFont("Segoe UI", 9))
