"""Build the portable Windows bundle and its versioned provenance/checksum."""
from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[1]


def build_environment(source=None):
    """Prevent unrelated PATH tools from supplying same-named native DLLs."""
    environment = dict(os.environ if source is None else source)
    for key in ("PYTHONPATH", "PYTHONHOME", "QT_PLUGIN_PATH", "QT_QPA_PLATFORM_PLUGIN_PATH",
                "QML_IMPORT_PATH", "QML2_IMPORT_PATH"):
        environment.pop(key, None)
    system = Path(environment.get("SystemRoot", "C:/Windows"))
    environment["PATH"] = os.pathsep.join(str(path) for path in (
        Path(sys.executable).parent, Path(sys.base_prefix), Path(sys.base_prefix) / "DLLs",
        system / "System32", system, system / "System32/Wbem",
    ))
    # Keep caches separate from other projects building on this workstation.
    environment["PYINSTALLER_CONFIG_DIR"] = str(ROOT / "tmp/pyinstaller-cache")
    return environment


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "dist")
    args = parser.parse_args(argv)
    if sys.platform != "win32" or platform.machine().lower() not in ("amd64", "x86_64"):
        parser.error("Build this Windows x64 package on Windows x64.")
    output = args.output.resolve()
    subprocess.run([
        sys.executable, "-m", "PyInstaller", "--noconfirm",
        "--distpath", str(output), "--workpath", str(ROOT / "tmp/pyinstaller-isolated"),
        str(ROOT / "tools/Transport.spec"),
    ], cwd=ROOT, env=build_environment(), check=True)
    bundle = output / "Transport"
    commit = os.environ.get("GITHUB_SHA") or subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    packages = ("PySide6", "numpy", "matplotlib", "pyvisa", "nidaqmx", "pythonnet",
                "PyInstaller", "pyinstaller-hooks-contrib")
    manifest = {
        "commit": commit, "ref": os.environ.get("GITHUB_REF", "local"),
        "python": platform.python_version(), "platform": platform.platform(),
        "dependencies": {name: importlib.metadata.version(name) for name in packages},
    }
    (bundle / "build-info.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    (bundle / "README.txt").write_text(
        "Transport Measurement - Windows x64 portable package\n\n"
        "Extract the entire ZIP, then open Transport.exe. Keep TransportWorker.exe\n"
        "and _internal beside it. Python does not need to be installed.\n"
        "For the retained Widgets interface: Transport.exe --widgets\n\n"
        "Install the required NI-DAQmx/VISA drivers separately. Configure instrument\n"
        "addresses, protection limits and any optional attoDRY SDK on this computer.\n"
        "Software tests use simulated instruments; physical instrument acceptance\n"
        "is separate. Close the application normally before replacing this folder.\n"
        "User settings and measured data are not included in this package.\n\n"
        f"Source commit: {commit}\n"
        "Source and instructions: https://github.com/ylylyl98/Transport_Pyside6\n",
        encoding="utf-8",
    )
    archive = output / "Transport-windows-x64.zip"
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zip_file:
        for path in sorted(bundle.rglob("*")):
            if path.is_file():
                zip_file.write(path, path.relative_to(output))
    with archive.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    (output / "SHA256SUMS.txt").write_text(f"{digest}  {archive.name}\n", encoding="ascii")
    print(f"Built {archive} ({archive.stat().st_size / 1024**2:.1f} MiB)", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
