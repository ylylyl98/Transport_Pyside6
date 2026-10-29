"""Relocate a Windows bundle and exercise its real EXEs without the checkout."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time


def isolated_environment(source=None):
    environment = dict(os.environ if source is None else source)
    for key in ("PYTHONPATH", "PYTHONHOME", "PYTHONSTARTUP", "VIRTUAL_ENV", "QT_PLUGIN_PATH", "QT_QPA_PLATFORM_PLUGIN_PATH",
                "QML_IMPORT_PATH", "QML2_IMPORT_PATH", "QT_STYLE_OVERRIDE", "QT_SCALE_FACTOR",
                "QT_FONT_DPI", "QSG_RHI_BACKEND", "QSG_RENDER_LOOP", "TRANSPORT_UI_PREVIEW"):
        environment.pop(key, None)
    environment.update(QT_QPA_PLATFORM="offscreen", QT_QUICK_BACKEND="software",
                       QT_QUICK_CONTROLS_STYLE="Fusion", PYTHONUTF8="1")
    if os.name == "nt" and environment.get("SystemRoot"):
        system = Path(environment["SystemRoot"])
        environment["PATH"] = os.pathsep.join(str(path) for path in
                                              (system / "System32", system, system / "System32/Wbem"))
    return environment


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--timeout", type=int, default=180)
    args = parser.parse_args(argv)
    if args.timeout <= 0:
        parser.error("--timeout must be positive")
    bundle, output = args.bundle.resolve(), args.output.resolve()
    for name in ("Transport.exe", "TransportWorker.exe"):
        if not (bundle / name).is_file():
            parser.error("Missing bundle executable: " + str(bundle / name))
    output.mkdir(parents=True, exist_ok=True)
    reports = []
    # A relocated copy catches absolute development paths and missing resources.
    # TemporaryDirectory removes only this tool's freshly allocated directory.
    with tempfile.TemporaryDirectory(prefix="transport-package-check-") as temporary:
        root = Path(temporary)
        portable = root / "portable app" / "Transport"
        shutil.copytree(bundle, portable)
        cwd = root / "unrelated working directory"
        cwd.mkdir()
        environment = isolated_environment()
        for mode in ("quick", "widgets"):
            destination = output / mode
            destination.mkdir(parents=True, exist_ok=True)
            report_path = destination / "report.json"
            # Never accept evidence retained from an earlier invocation.
            report_path.unlink(missing_ok=True)
            started = time.monotonic()
            command = [str(portable / "Transport.exe"), "--verify-package", str(destination), "--mode", mode]
            with (destination / "process.log").open("wb") as log:
                try:
                    process = subprocess.run(command, cwd=cwd, env=environment, stdout=log,
                                             stderr=subprocess.STDOUT, timeout=args.timeout, check=False)
                    exit_code = process.returncode
                    error = ""
                except subprocess.TimeoutExpired:
                    exit_code, error = 124, "Package verification timed out"
            try:
                evidence = json.loads(report_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                evidence = {}
            passed = exit_code == 0 and evidence.get("success") is True and evidence.get("frozen") is True
            result = dict(mode=mode, success=passed, exit_code=exit_code,
                          elapsed_s=round(time.monotonic() - started, 2), evidence=evidence)
            if not passed:
                result["error"] = error or evidence.get("error", "EXE did not publish successful frozen evidence")
            reports.append(result)
            print(("PASS " if passed else "FAIL ") + mode
                  + (": " + result["error"] if not passed else ""), flush=True)
            if not passed:
                for name in ("runtime.log", "process.log"):
                    log_path = destination / name
                    if log_path.is_file():
                        print(log_path.read_text(encoding="utf-8", errors="replace")[-8000:], flush=True)
        (output / "summary.json").write_text(json.dumps(reports, indent=2), encoding="utf-8")
    return 0 if all(item["success"] for item in reports) else 1


if __name__ == "__main__":
    raise SystemExit(main())
