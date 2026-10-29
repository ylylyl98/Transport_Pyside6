"""Fast dependency probe and source-sensitive, hardware-blocked startup preflight.

Only successful checks are cached, under the ignored project virtual environment.
This helper never starts the production application or connects instruments.
"""
from __future__ import annotations

import argparse
import hashlib
from importlib import metadata
import json
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
CACHE_VERSION = 1
STARTUP_ENVIRONMENT = (
    "QSG_RHI_BACKEND", "QSG_RENDER_LOOP", "QT_QUICK_BACKEND", "QT_PLUGIN_PATH",
    "QT_QPA_PLATFORM", "QT_QPA_PLATFORM_PLUGIN_PATH", "QT_STYLE_OVERRIDE",
    "QT_QUICK_CONTROLS_STYLE", "QML_IMPORT_PATH", "QML2_IMPORT_PATH",
    "QT_SCALE_FACTOR", "QT_FONT_DPI", "PYTHONPATH", "PYTHONHOME",
)


def dependency_state(root=ROOT):
    """Validate direct pins and their active transitive requirements without pip/network."""
    try:
        from packaging.requirements import Requirement
        from packaging.utils import canonicalize_name
    except ImportError:
        return {}, ["packaging is missing"]
    versions, errors, visited, pending, distributions = {}, [], set(), [], {}
    for line in (root / "requirements.txt").read_text(encoding="utf-8-sig").splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            try:
                pending.append(Requirement(line))
            except ValueError:
                errors.append("Unsupported requirement: " + line)
    while pending:
        required = pending.pop()
        if required.marker and not required.marker.evaluate():
            continue
        key = canonicalize_name(required.name)
        if required.url:
            errors.append("Direct URL requirements need an explicit launcher check: " + required.name)
            continue
        try:
            if key not in distributions:
                data = metadata.distribution(required.name).metadata
                distributions[key] = data["Version"], data.get_all("Requires-Dist", [])
            version, dependencies = distributions[key]
        except metadata.PackageNotFoundError:
            errors.append(required.name + " is missing")
            continue
        versions[key] = version
        if version not in required.specifier:
            errors.append(f"{required.name} {version} does not satisfy {required.specifier}")
        visit = key, tuple(sorted(required.extras))
        if visit in visited:
            continue
        visited.add(visit)
        for item in dependencies:
            dependency = Requirement(item)
            if dependency.marker and not any(dependency.marker.evaluate({"extra": extra})
                                              for extra in ("", *required.extras)):
                continue
            # Its marker has been evaluated in the requiring package's extra context.
            dependency.marker = None
            pending.append(dependency)
    return dict(sorted(versions.items())), sorted(set(errors))


def source_fingerprint(root, frontend, versions):
    digest = hashlib.sha256()
    context = dict(version=CACHE_VERSION, frontend=frontend, root=str(root.resolve()),
                   python=sys.version, executable=str(Path(sys.executable).resolve()), packages=versions,
                   startup_environment={name: os.environ.get(name) for name in STARTUP_ENVIRONMENT})
    digest.update(json.dumps(context, sort_keys=True).encode())
    paths = set(root.glob("*.py"))
    for directory in ("app", "utils", "drivers", "instruments", "controllers"):
        paths.update((root / directory).rglob("*.py"))
    paths.update((root / "app/quick/qml").glob("*"))
    paths.update((root / "utils").glob("*.json"))
    paths.update(root.glob("config*.json"))
    paths.update((root / name) for name in ("requirements.txt", "tools/check_transport_startup.py"))
    for path in sorted(paths):
        if path.is_file():
            digest.update(str(path.relative_to(root)).encode())
            digest.update(path.read_bytes())
    return digest.hexdigest()


def cached_preflight(root, frontend, versions, *, force=False, runner=subprocess.run):
    """Return the child exit code; never record a failed or interrupted preflight."""
    cache = root / ".venv" / (".transport-" + frontend + "-preflight.json")
    fingerprint = source_fingerprint(root, frontend, versions)
    try:
        previous = json.loads(cache.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        previous = {}
    if not force and isinstance(previous, dict) and previous.get("fingerprint") == fingerprint:
        print("[CHECK] Unchanged source and environment: previous startup check reused.")
        return 0
    if frontend == "quick":
        command = [sys.executable, "-B", str(root / "transport_quick_preview.py"), "--check"]
    else:
        script = ("from pathlib import Path; from app.offline_sandbox import OfflineSandbox; "
                  "sandbox=OfflineSandbox(Path.cwd()); sandbox.install_hardware_guards(); "
                  "from app.ui.main_window import MainWindow")
        command = [sys.executable, "-B", "-c", script]
    print("[CHECK] Source/environment changed: running isolated startup check.", flush=True)
    result = runner(command, cwd=root)
    if result.returncode:
        return result.returncode
    # A concurrent source edit cannot become a certified cache entry.
    if source_fingerprint(root, frontend, versions) != fingerprint:
        print("[CHECK] Source changed during preflight; retry the launcher after edits finish.")
        return 1
    try:
        cache.parent.mkdir(parents=True, exist_ok=True)
        temporary = cache.with_suffix(".tmp")
        temporary.write_text(json.dumps({"fingerprint": fingerprint, "packages": versions}), encoding="utf-8")
        temporary.replace(cache)
    except OSError as exc:
        print("[WARN] Startup passed but cache could not be saved: " + str(exc))
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dependencies-only", action="store_true")
    parser.add_argument("--frontend", choices=("quick", "widgets"), default="quick")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args(argv)
    versions, errors = dependency_state()
    if errors:
        print("\n".join(errors))
        return 1
    if args.dependencies_only:
        print("[CHECK] Installed dependencies satisfy requirements.")
        return 0
    return cached_preflight(ROOT, args.frontend, versions,
                            force=args.force or os.environ.get("TRANSPORT_VALIDATE_STARTUP") == "1")


if __name__ == "__main__":
    raise SystemExit(main())
