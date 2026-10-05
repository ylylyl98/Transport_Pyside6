"""Detached serial PNG worker; deliberately imports no instrument/control code."""
from __future__ import annotations

import argparse
import ctypes
import json
import os
import sys
import time
from pathlib import Path


def write_json(path, payload):
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    os.replace(temporary, path)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--spool", type=Path, required=True)
    parser.add_argument("--owner-pid", type=int)
    parser.add_argument("--after-pid", type=int, help="Take over a closing reload generation after its worker exits")
    args = parser.parse_args()
    spool = args.spool
    from app.background_process import is_process_running
    if args.after_pid:
        while is_process_running(args.after_pid):
            time.sleep(.1)
        for path in spool.glob("*.job.waiting"):
            os.replace(path, path.with_suffix(".json"))
        for name in ("exited.json", "ready.json", "boot.json", "fatal.json"):
            (spool / name).unlink(missing_ok=True)
    if os.name == "nt":
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.GetCurrentProcess.restype = ctypes.c_void_p
        kernel.SetPriorityClass.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
        kernel.SetPriorityClass(kernel.GetCurrentProcess(), 0x4000)
    else:
        os.nice(5)
    write_json(spool / "boot.json", {"pid": os.getpid()})
    try:
        from app.png_export import export_run, export_analysis, load_snapshot
    except Exception as exc:
        write_json(spool / "fatal.json", {"error": str(exc)})
        return
    forbidden = ("pyvisa", "nidaqmx", "app.device_manager", "app.workers", "app.engine", "controllers")
    write_json(spool / "ready.json", {"pid": os.getpid(), "hardware_modules": [name for name in sys.modules if name.startswith(forbidden)]})
    idle_since = time.monotonic()
    while True:
        jobs = sorted(spool.glob("*.job.json"))
        if not jobs:
            if (spool / "stop").exists() or (args.owner_pid and not is_process_running(args.owner_pid)) or (not args.owner_pid and time.monotonic() - idle_since > 45):
                write_json(spool / "exited.json", {"pid": os.getpid()})
                return
            time.sleep(.1)
            continue
        for path in jobs:
            job = {}
            try:
                job = json.loads(path.read_text(encoding="utf-8"))
                if job["kind"] == "run":
                    result = export_run(job)
                elif job["kind"] == "analysis":
                    archive = Path(job["snapshot_path"])
                    if job.get("wait_for_snapshot"):
                        deadline = time.monotonic() + 120
                        while not archive.exists():
                            error_file = archive.with_suffix(".error.json")
                            if error_file.exists():
                                raise RuntimeError(json.loads(error_file.read_text(encoding="utf-8"))["error"])
                            if time.monotonic() > deadline:
                                raise TimeoutError("Preparing analysis snapshot timed out")
                            time.sleep(.1)
                    result = export_analysis(load_snapshot(job["snapshot_path"]), job["output"], job.get("cut_only", False),
                                             heatmap_only=job.get("heatmap_only", False))
                else:
                    raise ValueError("Unknown PNG job kind")
            except Exception as exc:
                result = {"error": str(exc), "outputs": []}
            write_json(path.with_name(path.name.replace(".job.json", ".result.json")), {"id": job.get("id"), **result})
            # Only delete snapshot archives owned by this queue.
            archive = Path(job.get("snapshot_path", ""))
            if archive.suffix == ".npz" and archive.parent == spool:
                archive.unlink(missing_ok=True)
                archive.with_suffix(".error.json").unlink(missing_ok=True)
            path.unlink(missing_ok=True)
            idle_since = time.monotonic()


if __name__ == "__main__":
    main()
