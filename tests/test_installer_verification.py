"""Acceptance gate rejects corruption, stale evidence and source execution."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch, Mock

if os.environ.get('TRANSPORT_OFFLINE_VERIFICATION') != '1':
    raise unittest.SkipTest('Run with verify_offline.py')

from tools.verify_installer import verify_payload, smoke, run, verify_process_guards, wait_for_uninstall


class InstallerVerificationTests(unittest.TestCase):
    def test_uninstall_waits_for_child_cleanup_but_rejects_real_residue(self):
        with tempfile.TemporaryDirectory() as temporary:
            app = Path(temporary)
            uninstaller = app / 'unins000.exe'
            uninstaller.write_bytes(b'pending self-removal')
            with patch('tools.verify_installer.time.sleep', side_effect=lambda _: uninstaller.unlink()) as wait:
                wait_for_uninstall(app, ['Transport.exe'], app / 'shortcut', timeout=1)
                wait.assert_called_once()
            uninstaller.write_bytes(b'real residue')
            with self.assertRaisesRegex(RuntimeError, 'unins000.exe'):
                wait_for_uninstall(app, [], app / 'shortcut', timeout=0)
            self.assertTrue(uninstaller.exists())

    def test_manifest_checks_nested_runtime_payload_and_rejects_escape(self):
        with tempfile.TemporaryDirectory() as temporary:
            app = Path(temporary)
            files = {}
            for name in ('Transport.exe', 'TransportWorker.exe', 'build-info.json', '_internal/runtime.dll'):
                path = app / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(name.encode())
                files[name] = hashlib.sha256(name.encode()).hexdigest()
            verify_payload(app, files)
            (app / '_internal/runtime.dll').write_bytes(b'corrupted')
            with self.assertRaisesRegex(RuntimeError, 'checksum mismatch'):
                verify_payload(app, files)
            with self.assertRaisesRegex(ValueError, 'Unsafe manifest'):
                verify_payload(app, {'../outside': 'anything'})

    def test_smoke_cannot_accept_report_left_by_previous_run(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / 'quick').mkdir()
            (root / 'quick/report.json').write_text(json.dumps({'success': True, 'frozen': True}))
            with patch('tools.verify_installer.subprocess.run', return_value=SimpleNamespace(returncode=0)):
                with self.assertRaises(FileNotFoundError):
                    smoke(root / 'installed app', root, root, 30)

    def test_success_requires_installed_exe_identity_and_blocked_hardware(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            app = root / 'installed app'
            def publish(command, **kwargs):
                report = dict(success=True, frozen=True, mode=command[-1], hardware='blocked',
                              executable=str(app / 'Transport.exe'))
                (Path(command[2]) / 'report.json').write_text(json.dumps(report))
                self.assertEqual(command[0], str(app / 'Transport.exe'))
                self.assertEqual(kwargs['env']['QT_QPA_PLATFORM'], 'offscreen')
                return SimpleNamespace(returncode=0)
            with patch('tools.verify_installer.subprocess.run', side_effect=publish) as process:
                smoke(app, root / 'output', root, 30)
                self.assertEqual(process.call_count, 2)
            def wrong_identity(command, **kwargs):
                (Path(command[2]) / 'report.json').write_text(json.dumps(dict(
                    success=True, frozen=True, mode=command[-1], hardware='blocked', executable='other.exe')))
                return SimpleNamespace(returncode=0)
            with patch('tools.verify_installer.subprocess.run', side_effect=wrong_identity):
                with self.assertRaisesRegex(RuntimeError, 'valid offline frozen evidence'):
                    smoke(app, root / 'output', root, 30)

    def test_timeout_or_nonzero_exit_cannot_be_success(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with patch('tools.verify_installer.subprocess.run', side_effect=subprocess.TimeoutExpired('setup', 1)):
                with self.assertRaisesRegex(RuntimeError, 'timed out'):
                    run(['setup'], root, root / 'process.log', 1)
            with patch('tools.verify_installer.subprocess.run', return_value=SimpleNamespace(returncode=5)):
                with self.assertRaisesRegex(RuntimeError, 'exited 5'):
                    run(['setup'], root, root / 'process.log', 1)

    def test_process_guard_only_terminates_owned_fixture_and_requires_rejection(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            owned = Mock()
            owned.poll.return_value = None
            with patch('tools.verify_installer.shutil.copy2'), \
                 patch.dict(os.environ, {'SystemRoot': str(root)}), \
                 patch('tools.verify_installer.subprocess.Popen', return_value=owned) as launch, \
                 patch('tools.verify_installer.subprocess.run', return_value=SimpleNamespace(returncode=0)), \
                 patch('tools.verify_installer.verify_payload'):
                with self.assertRaisesRegex(RuntimeError, 'did not block install'):
                    verify_process_guards(['setup', '/TASKS='], root / 'app', {}, root, root, 10)
                self.assertEqual(Path(launch.call_args.args[0][0]).name, 'Transport.exe')
                self.assertEqual(launch.call_args.args[0][1:], ['-t', '127.0.0.1'])
                owned.terminate.assert_called_once_with()
                owned.wait.assert_called_once_with(timeout=10)
