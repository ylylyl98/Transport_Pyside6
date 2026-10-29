"""Commands for analysis children in source checkouts and frozen releases.

The console-enabled worker keeps history JSON-line IPC available while the
measurement executable remains windowed. Both executables share one bundle.
"""
from __future__ import annotations

import os
from pathlib import Path
import sys


def worker_command(mode: str, *arguments: str) -> list[str]:
    """Return an argv list without invoking a shell or starting a process."""
    if mode not in ("history", "history-widgets", "png-worker"):
        raise ValueError(f"Unknown Transport worker mode: {mode}")
    executable = Path(sys.executable)
    if getattr(sys, "frozen", False):
        return [str(executable.with_name("TransportWorker.exe")), "--" + mode, *arguments]

    root = Path(__file__).resolve().parents[1]
    if mode == "png-worker":
        if os.name == "nt" and executable.with_name("pythonw.exe").exists():
            executable = executable.with_name("pythonw.exe")
        entry = [str(root / "transport_png_exporter.py")]
    else:
        # History uses stdin/stdout for parent commands and result messages.
        if executable.name.lower() == "pythonw.exe" and executable.with_name("python.exe").exists():
            executable = executable.with_name("python.exe")
        entry = (["-u", "-B", "-m", "app.quick.history_app"] if mode == "history"
                 else ["-u", str(root / "transport_history_viewer.py")])
    return [str(executable), *entry, *arguments]
