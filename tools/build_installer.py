"""Wrap the complete Windows bundle in a per-user Inno Setup installer."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess

ROOT = Path(__file__).resolve().parents[1]
INSTALLER_NAME = "Transport-Setup-windows-x64.exe"
COMPILER_VERSION = "7.1.1"


def package_version(build_info, requested=None):
    ref = str(build_info.get("ref", ""))
    version = requested or (ref.removeprefix("refs/tags/") if ref.startswith("refs/tags/") else "")
    if version:
        version = version.removeprefix("v")
        if not re.fullmatch(r"\d+(?:\.\d+){1,3}(?:[-+][0-9A-Za-z.-]+)?", version):
            raise ValueError("Use a numeric release version, optionally with a prerelease/build suffix")
        return version
    commit = str(build_info.get("commit", ""))
    if not re.fullmatch(r"[0-9a-fA-F]{40,64}", commit):
        raise ValueError("Bundle build-info.json has no valid source commit")
    return "0.0.0-dev." + commit[:12]


def file_digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def payload_manifest(bundle, version, build_info):
    if (bundle / "installer-manifest.json").exists():
        raise ValueError("Bundle contains reserved installer-manifest.json; use the fresh portable build")
    for name in ("Transport.exe", "TransportWorker.exe", "build-info.json"):
        if not (bundle / name).is_file():
            raise ValueError("Missing bundle file: " + name)
    if not (bundle / "_internal").is_dir():
        raise ValueError("Bundle has no _internal runtime directory")
    files = {}
    for path in sorted(bundle.rglob("*")):
        if path.is_symlink() or not path.resolve().is_relative_to(bundle.resolve()):
            raise ValueError("Bundle must not include links outside its directory: " + str(path))
        if path.is_file():
            files[path.relative_to(bundle).as_posix()] = file_digest(path)
    return dict(schema=1, app_version=version, commit=build_info["commit"], files=files)


def write_checksums(output):
    artifacts = [output / "Transport-windows-x64.zip", output / INSTALLER_NAME]
    if not all(path.is_file() for path in artifacts):
        raise ValueError("Both the portable ZIP and installer must exist before writing release checksums")
    (output / "SHA256SUMS.txt").write_text(
        "".join(f"{file_digest(path)}  {path.name}\n" for path in artifacts), encoding="ascii", newline="\n")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", type=Path, default=ROOT / "dist/Transport")
    parser.add_argument("--output", type=Path, default=ROOT / "dist")
    parser.add_argument("--iscc", type=Path, default=ROOT / "tmp/inno-setup-7.1.1/ISCC.exe")
    parser.add_argument("--version", help="Override the bundle tag; otherwise use build-info.json")
    args = parser.parse_args(argv)
    if os.name != "nt":
        parser.error("Build the Windows installer on Windows")
    bundle, output, compiler = args.bundle.resolve(), args.output.resolve(), args.iscc.resolve()
    if output.is_relative_to(bundle):
        parser.error("Installer output must be outside the bundle directory")
    if not compiler.is_file():
        parser.error("ISCC.exe not found; run tools/Install-InnoSetup.ps1 first")
    compiler_version = subprocess.check_output([str(compiler), "--version"], text=True).strip()
    if compiler_version != COMPILER_VERSION:
        parser.error(f"Expected Inno Setup {COMPILER_VERSION}, found {compiler_version}")
    info = json.loads((bundle / "build-info.json").read_text(encoding="utf-8"))
    if os.environ.get("GITHUB_SHA") and info.get("commit") != os.environ["GITHUB_SHA"]:
        parser.error("Bundle commit does not match the current CI commit")
    version = package_version(info, args.version)
    manifest = payload_manifest(bundle, version, info)
    manifest["compiler"] = "Inno Setup " + compiler_version
    output.mkdir(parents=True, exist_ok=True)
    installer = output / INSTALLER_NAME
    installer.unlink(missing_ok=True)
    manifest_path = installer.with_suffix(".manifest.json")
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    subprocess.run([str(compiler), "--no-ide-signtools", "/Qp", "/DAppVersion=" + version,
                    "/DBundleDir=" + str(bundle), "/DOutputDir=" + str(output),
                    "/DManifestFile=" + str(manifest_path), str(ROOT / "tools/Transport.iss")],
                   cwd=ROOT, check=True)
    if not installer.is_file():
        raise RuntimeError("Compiler did not produce the installer")
    write_checksums(output)
    print(f"Built {installer} ({installer.stat().st_size / 1024**2:.1f} MiB), version {version}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
