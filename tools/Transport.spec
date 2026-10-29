# -*- mode: python ; coding: utf-8 -*-
"""One shared bundle, with a GUI entry and a console worker for history IPC."""
from pathlib import Path
import os
import sys

from PyInstaller.utils.hooks import copy_metadata

root = Path(SPECPATH).parent
datas = [
    (str(root / "app/quick/qml"), "app/quick/qml"),
    (str(root / "app/quick/palettes.npz"), "app/quick"),
    (str(root / "app/ui/app.qss"), "app/ui"),
    (str(root / "assets/transport.ico"), "assets"),
]
for distribution in ("nidaqmx", "pyvisa", "pythonnet"):
    # nidaqmx's nitypes/hightime dependencies query distribution versions at
    # import time; collect their metadata even when modules are found normally.
    datas += copy_metadata(distribution, recursive=True)

a = Analysis(
    [str(root / "transport_UI.py")],
    pathex=[str(root)],
    binaries=[],
    datas=datas,
    hiddenimports=[
        "PySide6.QtQuickControls2", "PySide6.QtSvg",
        "matplotlib.backends.backend_agg", "matplotlib.backends.backend_qtagg",
    ],
    hookspath=[],
    hooksconfig={"matplotlib": {"backends": ["Agg", "QtAgg"]}},
    runtime_hooks=[],
    excludes=["PyQt5", "PyQt6", "PySide2", "tkinter"],
    noarchive=False,
)
# Hardware runtimes are installed separately. Do not accidentally redistribute
# another application's DLL just because its directory appears on PATH.
allowed_binary_roots = [Path(sys.prefix).resolve(), Path(sys.base_prefix).resolve(),
                        Path(os.environ.get("SystemRoot", "C:/Windows")).resolve()]
for destination, source, _kind in a.binaries:
    if not any(Path(source).resolve().is_relative_to(folder) for folder in allowed_binary_roots):
        raise RuntimeError(f"Unexpected binary outside Python/Windows build environment: {source}")
pyz = PYZ(a.pure)
options = [("u", None, "OPTION"), ("X utf8", None, "OPTION")]
common = dict(
    exclude_binaries=True, debug=False, bootloader_ignore_signals=False,
    strip=False, upx=False, icon=str(root / "assets/transport.ico"),
)
gui = EXE(pyz, a.scripts, options, name="Transport", console=False, **common)
# A windowed bootloader has no Python stdio. History requires JSON over pipes,
# so child processes use this console bootloader with the same application code.
worker = EXE(pyz, a.scripts, options, name="TransportWorker", console=True, **common)
COLLECT(gui, worker, a.binaries, a.datas, strip=False, upx=False, name="Transport")
