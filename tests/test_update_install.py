import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from app import update_install


class InstallHandoffTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='更新 space ')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.installer = self.root / update_install.INSTALLER_NAME
        self.installer.write_bytes(b'test')
        self.install_dir = self.root / 'Program 路径'
        self.install_dir.mkdir()
        self.hash = 'A' * 64

    def test_helper_arguments_and_script(self):
        with patch.dict(os.environ, {'SystemRoot': r'C:\Windows'}), patch.object(update_install.subprocess, 'Popen') as popen:
            self.assertIs(update_install.launch_after_exit(self.installer, self.hash, self.install_dir), popen.return_value)
        args = popen.call_args.args[0]
        self.assertEqual(args[1:8], ['-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass', '-WindowStyle', 'Hidden', '-File'])
        self.assertEqual(args[args.index('-Installer') + 1], str(self.installer.resolve()))
        self.assertEqual(args[args.index('-InstallDir') + 1], str(self.install_dir.resolve()))
        self.assertEqual(args[args.index('-ExpectedHash') + 1], self.hash.lower())
        self.assertEqual(args[args.index('-ParentId') + 1], str(os.getpid()))
        script = Path(args[8])
        self.assertEqual(script.parent, self.root)
        source = script.read_text(encoding='ascii')
        self.assertIn('WaitForExit(120000)', source)
        self.assertLess(source.index('WaitForExit'), source.index('ComputeHash'))
        self.assertLess(source.index('ComputeHash'), source.index('Start-Process'))
        self.assertIn("'/NORESTART'", source)
        self.assertIn(r"$InstallDir -replace '(\\+)$', '$1$1'", source)
        self.assertNotIn('/SILENT', source.upper())
        self.assertNotIn('/CLOSEAPPLICATIONS', source.upper())
        self.assertNotIn('Stop-Process', source)
        self.assertIn('MessageBox', source)
        self.assertIn('Add-Content', source)
        self.assertNotIn('shell', popen.call_args.kwargs)
        self.assertEqual(popen.call_args.kwargs['creationflags'], getattr(subprocess, 'CREATE_NO_WINDOW', 0))

    def test_invalid_inputs_do_not_launch(self):
        with patch.object(update_install.subprocess, 'Popen') as popen:
            for value in ('a' * 63, 'z' * 64, 'sha256:' + 'a' * 64, None):
                with self.subTest(value=value), self.assertRaises(ValueError):
                    update_install.launch_after_exit(self.installer, value, self.install_dir)
            with self.assertRaises(ValueError):
                update_install.launch_after_exit(self.root / 'other.exe', self.hash, self.install_dir)
            with self.assertRaises(ValueError):
                update_install.launch_after_exit(self.installer, self.hash, self.root / 'missing')
            popen.assert_not_called()

    def test_launch_failure_removes_script(self):
        with patch.dict(os.environ, {'SystemRoot': r'C:\Windows'}), patch.object(update_install.subprocess, 'Popen', side_effect=OSError('failed')):
            with self.assertRaises(OSError):
                update_install.launch_after_exit(self.installer, self.hash, self.install_dir)
        self.assertEqual(list(self.root.glob('*.ps1')), [])


    @unittest.skipUnless(os.name == 'nt', 'Requires Windows PowerShell')
    @unittest.skipIf(os.environ.get('TRANSPORT_OFFLINE_VERIFICATION') == '1',
                     'Offline sandbox blocks all non-renderer subprocesses')
    def test_real_powershell_handoff_without_installing(self):
        # The mock function is in the script after its param block, so PowerShell
        # parses the production helper normally and never starts an executable.
        hook = r'''function Start-Process {
    param([string]$FilePath, [string[]]$ArgumentList)
    Import-Module (Join-Path $PSHOME 'Modules/Microsoft.PowerShell.Utility/Microsoft.PowerShell.Utility.psd1')
    @{ FilePath = $FilePath; ArgumentList = $ArgumentList } |
        ConvertTo-Json -Compress | Set-Content -LiteralPath ($LogPath + '.json') -Encoding UTF8
}
'''
        source = update_install._HELPER.replace("$ErrorActionPreference = 'Stop'", hook + "\n$ErrorActionPreference = 'Stop'", 1)
        # Only the test copy changes the failure UI, preventing modal dialogs.
        source = source[:source.index('    try {\n        Add-Type -AssemblyName System.Windows.Forms')] + '    exit 1\n}\n'
        script = self.root / 'harness.ps1'
        script.write_text(source, encoding='ascii')
        log = self.root / 'handoff.log'
        output = Path(str(log) + '.json')
        powershell = Path(os.environ['SystemRoot']) / 'System32/WindowsPowerShell/v1.0/powershell.exe'
        flags = subprocess.CREATE_NO_WINDOW
        parent = subprocess.Popen([sys.executable, '-c', 'import sys; sys.stdin.buffer.read(1)'],
                                  stdin=subprocess.PIPE, creationflags=flags)
        self.addCleanup(lambda: parent.kill() if parent.poll() is None else None)
        digest = hashlib.sha256(self.installer.read_bytes()).hexdigest()
        def run_helper(expected):
            return subprocess.Popen([str(powershell), '-NoProfile', '-NonInteractive',
                '-ExecutionPolicy', 'Bypass', '-WindowStyle', 'Hidden', '-File', str(script),
                '-Installer', str(self.installer), '-ExpectedHash', expected,
                '-InstallDir', str(self.install_dir), '-ParentId', str(parent.pid),
                '-LogPath', str(log)], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                creationflags=flags)
        helper = run_helper(digest)
        self.addCleanup(lambda: helper.kill() if helper.poll() is None else None)
        time.sleep(1)
        self.assertIsNone(parent.poll())
        self.assertIsNone(helper.poll())
        self.assertFalse(output.exists(), 'Installer was called before parent exit')
        parent.stdin.write(b'x')
        parent.stdin.close()
        parent.wait(timeout=10)
        stdout, stderr = helper.communicate(timeout=15)
        self.assertEqual(helper.returncode, 0, (stdout, stderr, log.read_text(encoding='utf-8-sig') if log.exists() else ''))
        recorded = json.loads(output.read_text(encoding='utf-8-sig'))
        self.assertEqual(recorded['FilePath'], str(self.installer))
        self.assertEqual(recorded['ArgumentList'], [f'/DIR="{self.install_dir}"', '/NORESTART'])
        output.unlink()
        rejected = run_helper('0' * 64)
        stdout, stderr = rejected.communicate(timeout=15)
        self.assertNotEqual(rejected.returncode, 0, (stdout, stderr))
        self.assertFalse(output.exists(), 'Hash mismatch must not start installer')
        self.assertIn('SHA256 verification failed', log.read_text(encoding='utf-8-sig'))
