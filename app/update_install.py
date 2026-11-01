"""Hand off a verified update to a separate Windows process after app exit."""
from __future__ import annotations

import os
from pathlib import Path
import re
import subprocess
import tempfile

from app.update_download import INSTALLER_NAME

# This script is static: user-provided paths are passed as arguments, never code.
_HELPER = r'''param(
    [Parameter(Mandatory=$true)][string]$Installer,
    [Parameter(Mandatory=$true)][string]$ExpectedHash,
    [Parameter(Mandatory=$true)][string]$InstallDir,
    [Parameter(Mandatory=$true)][int]$ParentId,
    [Parameter(Mandatory=$true)][string]$LogPath
)
$ErrorActionPreference = 'Stop'
function Get-TransportProcesses {
    @([System.Diagnostics.Process]::GetProcessesByName('Transport')) +
        @([System.Diagnostics.Process]::GetProcessesByName('TransportWorker'))
}
try {
    $parent = $null
    try { $parent = [System.Diagnostics.Process]::GetProcessById($ParentId) }
    catch [System.ArgumentException] { } # The parent already exited.
    if ($null -ne $parent -and -not $parent.WaitForExit(120000)) {
        throw 'The application did not exit within the allotted time.'
    }
    $deadline = [System.Diagnostics.Stopwatch]::StartNew()
    while (@(Get-TransportProcesses).Count -gt 0) {
        if ($deadline.ElapsedMilliseconds -ge 120000) {
            throw 'Close other Transport windows or wait for exports to finish, then retry the installer.'
        }
        [System.Threading.Thread]::Sleep(250)
    }
    $hasher = [System.Security.Cryptography.SHA256]::Create()
    $stream = [System.IO.File]::OpenRead($Installer)
    try { $actual = [BitConverter]::ToString($hasher.ComputeHash($stream)).Replace('-', '') }
    finally { $stream.Dispose(); $hasher.Dispose() }
    if ($actual -ine $ExpectedHash) { throw 'Installer SHA256 verification failed.' }
    # Double trailing backslashes before the closing quote for Windows argv parsing.
    $escapedDir = $InstallDir -replace '(\\+)$', '$1$1'
    $dirArg = '/DIR="' + $escapedDir + '"'
    Start-Process -FilePath $Installer -ArgumentList @($dirArg, '/NORESTART') -ErrorAction Stop | Out-Null
} catch {
    $detail = $_ | Out-String
    try { Add-Content -LiteralPath $LogPath -Value ((Get-Date -Format o) + ' ' + $detail) -Encoding UTF8 } catch {}
    $message = if ($detail -like '*Close other Transport windows*') { 'Close other Transport windows or wait for exports to finish, then retry the installer.' } else { 'The update could not be installed. See the update log for details.' }
    try {
        Add-Type -AssemblyName System.Windows.Forms
        [System.Windows.Forms.MessageBox]::Show($message, 'Transport update', 'OK', 'Error') | Out-Null
    } catch {
        try { (New-Object -ComObject WScript.Shell).Popup($message, 0, 'Transport update', 16) | Out-Null } catch {}
    }
    exit 1
}
'''


def launch_after_exit(installer_path, sha256, install_dir):
    """Start a helper; raise on preparation/launch failure, without hashing here."""
    if not isinstance(sha256, str) or re.fullmatch(r'[0-9a-fA-F]{64}', sha256) is None:
        raise ValueError('Invalid installer SHA256')
    installer = Path(installer_path).expanduser().resolve()
    if installer.name != INSTALLER_NAME or not installer.is_file():
        raise ValueError('Installer file is missing or has an invalid name')
    directory = Path(install_dir).expanduser().resolve()
    if not directory.is_dir():
        raise ValueError('Installation directory does not exist')
    system_root = os.environ.get('SystemRoot')
    if not system_root:
        raise RuntimeError('Windows system directory is unavailable')
    powershell = Path(system_root) / 'System32' / 'WindowsPowerShell' / 'v1.0' / 'powershell.exe'
    script_path = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='ascii', suffix='.ps1', prefix='install-update-', dir=installer.parent, delete=False, newline='\n') as script:
            script_path = Path(script.name)
            script.write(_HELPER)
        return subprocess.Popen([
            str(powershell), '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass', '-WindowStyle', 'Hidden',
            '-File', str(script_path), '-Installer', str(installer), '-ExpectedHash', sha256.lower(),
            '-InstallDir', str(directory), '-ParentId', str(os.getpid()),
            '-LogPath', str(installer.parent / 'update-install.log'),
        ], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
           creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    except Exception:
        if script_path is not None:
            script_path.unlink(missing_ok=True)
        raise
