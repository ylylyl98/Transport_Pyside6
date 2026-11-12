import os
from pathlib import Path
import base64
import json
import shutil
import tempfile
import time
import unittest

from PySide6.QtCore import QProcess, QProcessEnvironment

if os.environ.get('TRANSPORT_OFFLINE_VERIFICATION') != '1':
    raise unittest.SkipTest('Run through verify_offline.py')

ROOT = Path(__file__).resolve().parents[1]

class SourceLauncherTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='transport launcher ')
        self.root = Path(self.tmp.name) / '测试 空格 source'
        for part in ('tools', 'app', '.venv/Scripts', 'localdata'):
            (self.root / part).mkdir(parents=True)
        for name in ('Transport_App.bat', 'tools/Start-Transport.ps1', 'app/source_runtime.py', '.venv/pyvenv.cfg', '.venv/Scripts/python.exe', '.venv/Scripts/pythonw.exe'):
            shutil.copy2(ROOT / name, self.root / name)
        (self.root / 'app/__init__.py').write_text('')
        (self.root / 'requirements.txt').write_text('')
        (self.root / 'tools/check_transport_startup.py').write_text('')
        (self.root / 'tools/Transport-SourceGit.ps1').write_text('''function Update-TransportSource([string]$Root) {
[IO.File]::AppendAllText((Join-Path $Root "git_calls"), "1`n")
[pscustomobject]@{Status="current"; Message="Current"}
}''')
        (self.root / 'tools/Update-TransportShortcut.ps1').write_text('''$root = Split-Path -Parent $PSScriptRoot
[IO.File]::AppendAllText((Join-Path $root "shortcut_calls"), "1`n")''')
        (self.root / 'transport_UI.py').write_text('''import os, sys, time, traceback
from pathlib import Path
root = Path(__file__).resolve().parent
try:
    from app import source_runtime
    if not source_runtime.begin(sys.argv): sys.exit(0)
    with (root / "started").open("a") as f: f.write(str(os.getpid()) + "\\n")
    while not (root / "stop").exists():
        if source_runtime._runtime.kernel.wait(source_runtime._runtime.activate, 0):
            with (root / "activated").open("a") as f: f.write("1\\n")
        time.sleep(.03)
    source_runtime._runtime.close()
    (root / "exited").write_text("1")
except BaseException:
    (root / "error").write_text(traceback.format_exc())
    raise
''')
        self.processes = []

    def tearDown(self):
        (self.root / 'stop').write_text('1')
        deadline = time.monotonic() + 8
        while (self.root / 'started').exists() and not (self.root / 'exited').exists() and time.monotonic() < deadline:
            time.sleep(.05)
        for p in self.processes:
            if p.state() != QProcess.NotRunning:
                killer = QProcess()
                killer.start('taskkill.exe', ['/PID', str(p.processId()), '/T', '/F'])
                killer.waitForFinished(5000)
                p.kill()
                p.waitForFinished(5000)
        if (self.root / 'started').exists() and not (self.root / 'exited').exists():
            for pid in (self.root / 'started').read_text().splitlines():
                killer = QProcess()
                killer.start('taskkill.exe', ['/PID', pid, '/T', '/F'])
                killer.waitForFinished(5000)
        self.tmp.cleanup()

    def launch(self):
        p = QProcess()
        env = QProcessEnvironment.systemEnvironment()
        for key in ('TRANSPORT_OFFLINE_VERIFICATION', 'TRANSPORT_UI_PREVIEW', 'TRANSPORT_SOURCE_MANAGED_ROOT',
                    'TRANSPORT_SOURCE_LAUNCHER_PID', 'TRANSPORT_SOURCE_READY_EVENT', 'PYTHONHOME', 'PYTHONPATH'):
            env.remove(key)
        env.insert('LOCALAPPDATA', str(self.root / 'localdata'))
        env.insert('TRANSPORT_TEST_BAT', str(self.root / 'Transport_App.bat'))
        p.setProcessEnvironment(env)
        p.setWorkingDirectory(str(self.root))
        p.setNativeArguments('/d /s /c ""%TRANSPORT_TEST_BAT%""')
        p.start('cmd.exe')
        self.assertTrue(p.waitForStarted(10000), p.errorString())
        self.processes.append(p)
        return p

    def finish(self, p):
        self.assertTrue(p.waitForFinished(45000), 'launcher timed out')
        output = bytes(p.readAllStandardOutput()).decode(errors='replace')
        error = bytes(p.readAllStandardError()).decode(errors='replace')
        detail = (self.root / 'error').read_text() if (self.root / 'error').exists() else ''
        self.assertEqual(p.exitCode(), 0, output + error + detail)

    def wait(self, name):
        end = time.monotonic() + 10
        while time.monotonic() < end:
            if (self.root / name).exists(): return
            time.sleep(.05)
        self.fail(f'{name} missing; error=' + ((self.root / 'error').read_text() if (self.root / 'error').exists() else 'none'))

    def test_launcher_handshake_activation_and_restart(self):
        self.finish(self.launch())
        self.wait('started')
        self.assertEqual(len((self.root / 'started').read_text().splitlines()), 1)
        self.finish(self.launch())
        self.wait('activated')
        for marker in ('started', 'git_calls', 'shortcut_calls'):
            self.assertEqual(len((self.root / marker).read_text().splitlines()), 1, marker)
        (self.root / 'stop').write_text('1')
        self.wait('exited')
        (self.root / 'stop').unlink()
        (self.root / 'exited').unlink()
        self.finish(self.launch())
        deadline = time.monotonic() + 5
        while len((self.root / 'started').read_text().splitlines()) < 2 and time.monotonic() < deadline:
            time.sleep(.05)
        for marker in ('started', 'git_calls', 'shortcut_calls'):
            self.assertEqual(len((self.root / marker).read_text().splitlines()), 2, marker)

    def test_concurrent_launch_during_slow_update_runs_one_bootstrap(self):
        (self.root / 'tools/Transport-SourceGit.ps1').write_text('''function Update-TransportSource([string]$Root) {
[IO.File]::AppendAllText((Join-Path $Root "git_calls"), "1`n")
Start-Sleep -Seconds 3
[pscustomobject]@{Status="current"; Message="Current"}
}''')
        first = self.launch()
        self.wait('git_calls')
        second = self.launch()
        self.finish(second)
        self.finish(first)
        self.wait('started')
        for marker in ('started', 'git_calls', 'shortcut_calls'):
            self.assertEqual(len((self.root / marker).read_text().splitlines()), 1, marker)

    def test_legacy_entry_points_are_detected_before_updating(self):
        code = r'''
. (Join-Path $env:TRANSPORT_TEST_ROOT 'tools\Start-Transport.ps1')
function Get-CimInstance {
    [pscustomobject]@{CommandLine=$script:command; ExecutablePath=(Join-Path $env:TRANSPORT_TEST_ROOT '.venv\Scripts\pythonw.exe'); ProcessId=12345}
}
$results = foreach ($entry in @('transport_UI.py', 'main.py', '-m app.quick.application', 'background_analysis.py')) {
    $script:command = 'pythonw.exe ' + $entry
    Get-TransportSourceProcesses $env:TRANSPORT_TEST_ROOT
}
$results | ConvertTo-Json -Compress
'''
        process = QProcess()
        env = QProcessEnvironment.systemEnvironment()
        env.insert('TRANSPORT_TEST_ROOT', str(self.root))
        process.setProcessEnvironment(env)
        process.start('powershell.exe', ['-NoProfile', '-ExecutionPolicy', 'Bypass', '-EncodedCommand',
                                      base64.b64encode(code.encode('utf-16-le')).decode('ascii')])
        self.assertTrue(process.waitForFinished(10000))
        self.assertEqual(process.exitCode(), 0, bytes(process.readAllStandardError()).decode(errors='replace'))
        rows = json.loads(bytes(process.readAllStandardOutput()).decode())
        self.assertEqual([row['MainId'] for row in rows], [12345, 12345, 12345, 0])
        self.assertTrue(all(row['Known'] and row['Any'] for row in rows))

    def test_system_python_relative_entries_are_uncertain_but_other_absolute_checkout_is_not(self):
        code = r'''
. (Join-Path $env:TRANSPORT_TEST_ROOT 'tools\Start-Transport.ps1')
function Get-CimInstance {
    [pscustomobject]@{CommandLine=$script:command; ExecutablePath='C:\Python313\python.exe'; ProcessId=43210}
}
$results = foreach ($entry in @('transport_UI.py', 'main.py', 'app\quick\application.py',
    'app\ui\main_window.py', '-m app.quick.application', '"C:\other checkout\transport_UI.py"')) {
    $script:command = 'C:\Python313\python.exe ' + $entry
    Get-TransportSourceProcesses $env:TRANSPORT_TEST_ROOT
}
$results | ConvertTo-Json -Compress
'''
        process = QProcess()
        env = QProcessEnvironment.systemEnvironment()
        env.insert('TRANSPORT_TEST_ROOT', str(self.root))
        process.setProcessEnvironment(env)
        process.start('powershell.exe', ['-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass', '-EncodedCommand',
                                       base64.b64encode(code.encode('utf-16-le')).decode('ascii')])
        self.assertTrue(process.waitForFinished(10000))
        self.assertEqual(process.exitCode(), 0, bytes(process.readAllStandardError()).decode(errors='replace'))
        rows = json.loads(bytes(process.readAllStandardOutput()).decode())
        self.assertEqual([row['UncertainId'] for row in rows], [43210] * 5 + [0])
        self.assertTrue(all(row['Known'] and not row['Any'] for row in rows))

    def test_uncertain_process_notifies_without_git_or_bootstrap(self):
        code = r'''
. (Join-Path $env:TRANSPORT_TEST_ROOT 'tools\Start-Transport.ps1')
function Get-TransportSourceProcesses { [pscustomobject]@{Known=$true; MainId=0; Any=$false; UncertainId=43210} }
function Show-TransportSourceNotice { param([string]$Message, [switch]$Failure)
    [IO.File]::WriteAllText((Join-Path $env:TRANSPORT_TEST_ROOT 'notice'), $Message) }
function Invoke-TransportSourceBootstrap { [IO.File]::WriteAllText((Join-Path $env:TRANSPORT_TEST_ROOT 'bootstrap_calls'), '1'); return 0 }
exit (Start-TransportSource $env:TRANSPORT_TEST_ROOT)
'''
        process = QProcess()
        env = QProcessEnvironment.systemEnvironment()
        env.insert('TRANSPORT_TEST_ROOT', str(self.root))
        env.insert('LOCALAPPDATA', str(self.root / 'localdata'))
        process.setProcessEnvironment(env)
        process.start('powershell.exe', ['-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass', '-EncodedCommand',
                                       base64.b64encode(code.encode('utf-16-le')).decode('ascii')])
        self.assertTrue(process.waitForFinished(10000))
        self.assertEqual(process.exitCode(), 0, bytes(process.readAllStandardError()).decode(errors='replace'))
        self.assertIn('No update or new instance', (self.root / 'notice').read_text(encoding='utf8'))
        for name in ('git_calls', 'bootstrap_calls', 'shortcut_calls', 'started'):
            self.assertFalse((self.root / name).exists(), name)

    def test_skipped_update_notifies_and_starts_unchanged_local_source(self):
        (self.root / 'tools/Transport-SourceGit.ps1').write_text('''function Update-TransportSource([string]$Root) {
[IO.File]::AppendAllText((Join-Path $Root "git_calls"), "1`n")
[pscustomobject]@{Status="skipped"; Message="Source update skipped: local changes."}
}''')
        (self.root / 'local_marker').write_text('local data retained')
        env = QProcessEnvironment.systemEnvironment()
        for key in ('TRANSPORT_OFFLINE_VERIFICATION', 'TRANSPORT_UI_PREVIEW', 'TRANSPORT_SOURCE_MANAGED_ROOT',
                    'TRANSPORT_SOURCE_LAUNCHER_PID', 'TRANSPORT_SOURCE_READY_EVENT', 'PYTHONHOME', 'PYTHONPATH'):
            env.remove(key)
        env.insert('LOCALAPPDATA', str(self.root / 'localdata'))
        env.insert('TRANSPORT_TEST_ROOT', str(self.root))
        env.insert('TRANSPORT_TEST_LAUNCHER', str(self.root / 'tools/Start-Transport.ps1'))
        command = ('. $env:TRANSPORT_TEST_LAUNCHER; '
                   'function Show-TransportSourceNotice { param([string]$Message, [switch]$Failure) '
                   '[IO.File]::WriteAllText((Join-Path $env:TRANSPORT_TEST_ROOT "notice"), $Message) }; '
                   'exit (Start-TransportSource $env:TRANSPORT_TEST_ROOT)')
        process = QProcess()
        process.setProcessEnvironment(env)
        process.setWorkingDirectory(str(self.root))
        process.start('powershell.exe', ['-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass', '-Command', command])
        self.assertTrue(process.waitForStarted(10000), process.errorString())
        self.processes.append(process)
        self.finish(process)
        self.wait('started')
        notice = (self.root / 'notice').read_text(encoding='utf8').lower()
        self.assertIn('skipped', notice)
        self.assertIn('local', notice)
        self.assertEqual((self.root / 'local_marker').read_text(), 'local data retained')
        self.assertEqual(len((self.root / 'git_calls').read_text().splitlines()), 1)
