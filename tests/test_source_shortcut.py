import base64
import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest

if os.environ.get("TRANSPORT_OFFLINE_VERIFICATION") != "1":
    raise unittest.SkipTest("Run through verify_offline.py")

from PySide6 import QtCore

ROOT = Path(__file__).resolve().parents[1]
POWERSHELL = Path(os.environ["SystemRoot"]) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"


def run_ps(script):
    process = QtCore.QProcess()
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    process.start(str(POWERSHELL), ["-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-EncodedCommand", encoded])
    if not process.waitForFinished(30000):
        process.kill()
        raise AssertionError("PowerShell timed out")
    out = bytes(process.readAllStandardOutput()).decode("utf-8", errors="replace")
    err = bytes(process.readAllStandardError()).decode("utf-8", errors="replace")
    if process.exitStatus() != QtCore.QProcess.ExitStatus.NormalExit or process.exitCode() != 0:
        raise AssertionError(f"PowerShell failed ({process.exitCode()}): {out} {err}")
    return out


def quote(value):
    return "'" + str(value).replace("'", "''") + "'"


class SourceShortcutTests(unittest.TestCase):
    def test_unicode_checkout_properties_and_idempotence(self):
        with tempfile.TemporaryDirectory(prefix="transport-shortcut-") as temp:
            root = Path(temp) / "Source \u6d4b\u8bd5 with spaces"
            (root / "tools").mkdir(parents=True)
            (root / "assets").mkdir()
            shutil.copyfile(ROOT / "tools" / "Update-TransportShortcut.ps1", root / "tools" / "Update-TransportShortcut.ps1")
            (root / "tools" / "Start-Transport.ps1").write_text("# fixture; never executed\n", encoding="ascii")
            shutil.copyfile(ROOT / "assets" / "transport.ico", root / "assets" / "transport.ico")
            shortcut = Path(temp) / "isolated links" / "source.lnk"
            command = "& " + quote(root / "tools" / "Update-TransportShortcut.ps1") + " -ShortcutPath " + quote(shortcut) + "; if (-not $?) { exit 1 }"
            run_ps(command)
            self.assertTrue(shortcut.exists())
            original = shortcut.stat().st_mtime_ns
            result = run_ps(command)
            self.assertIn("already configured", result)
            self.assertEqual(original, shortcut.stat().st_mtime_ns)
            query = r'''
$path = PATH_VALUE
. SCRIPT_VALUE -ShortcutPath $path | Out-Null
$link = [TransportShortcutInterop.Identity]::ReadLink($path)
$item = (New-Object -ComObject Shell.Application).Namespace([IO.Path]::GetDirectoryName($path)).ParseName([IO.Path]::GetFileName($path))
[Console]::OutputEncoding = [Text.Encoding]::UTF8
@{ target=$link[0]; arguments=$link[1]; working=$link[2]; icon=$link[3]; description=$link[4];
 id=$item.ExtendedProperty('System.AppUserModel.ID'); command=$item.ExtendedProperty('System.AppUserModel.RelaunchCommand');
 relaunchIcon=$item.ExtendedProperty('System.AppUserModel.RelaunchIconResource'); name=$item.ExtendedProperty('System.AppUserModel.RelaunchDisplayNameResource') } | ConvertTo-Json -Compress
'''.replace("PATH_VALUE", quote(shortcut)).replace("SCRIPT_VALUE", quote(root / "tools" / "Update-TransportShortcut.ps1"))
            data = json.loads(run_ps(query))
            launcher = root / "tools" / "Start-Transport.ps1"
            args = f'-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "{launcher}"'
            identity = hashlib.sha256(str(root).rstrip("\\/").lower().encode("utf-8")).hexdigest()[:16]
            self.assertEqual(data["target"].lower(), str(POWERSHELL).lower())
            self.assertEqual(data["arguments"], args)
            self.assertEqual(data["working"], str(root))
            self.assertEqual(data["icon"], str(root / "assets" / "transport.ico") + ",0")
            self.assertEqual(data["description"], "Transport Measurement (Source)")
            self.assertEqual(data["id"], "MyLab.TransportMeasurement.Source." + identity)
            self.assertEqual(data["command"].lower(), f'"{POWERSHELL}" {args}'.lower())
            self.assertEqual(data["relaunchIcon"], data["icon"])
            self.assertEqual(data["name"], data["description"])
