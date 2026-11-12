import json
import os
from pathlib import Path
from PySide6.QtCore import QProcess, QProcessEnvironment
import tempfile
import unittest

if os.environ.get('TRANSPORT_OFFLINE_VERIFICATION') != '1':
    raise unittest.SkipTest('Run through verify_offline.py')

SCRIPT = Path(__file__).resolve().parents[1] / 'tools' / 'Transport-SourceGit.ps1'

def run_process(program, args, env=None):
    process = QProcess()
    if env is not None:
        environment = QProcessEnvironment.systemEnvironment()
        for key, value in env.items():
            environment.insert(key, value)
        process.setProcessEnvironment(environment)
    process.start(program, list(args))
    if not process.waitForStarted(10000) or not process.waitForFinished(70000):
        process.kill()
        raise RuntimeError("Process failed to start or timed out: " + program)
    output = bytes(process.readAllStandardOutput()).decode("utf8", errors="replace")
    error = bytes(process.readAllStandardError()).decode("utf8", errors="replace")
    if process.exitCode() != 0:
        raise RuntimeError(f"{program} failed: {error}")
    return output

class SourceUpdateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='transport source ')
        self.base = Path(self.tmp.name) / 'ä¸­æ–‡ ç©ºæ ¼'
        self.base.mkdir()
        self.remote = self.base / 'è¿œç¨‹.git'
        self.seed = self.base / 'seed'
        self.local = self.base / 'æœ¬åœ° repo'
        self.git(self.base, 'init', '--bare', str(self.remote))
        self.git(self.base, 'init', '-b', 'main', str(self.seed))
        self.git(self.seed, 'config', 'user.email', 'test@example.invalid')
        self.git(self.seed, 'config', 'user.name', 'Test')
        (self.seed / 'tracked.txt').write_text('initial', encoding='utf8')
        self.git(self.seed, 'add', 'tracked.txt')
        self.git(self.seed, 'commit', '-m', 'initial')
        self.git(self.seed, 'remote', 'add', 'custom', str(self.remote))
        self.git(self.seed, 'push', '-u', 'custom', 'main')
        self.git(self.base, 'clone', '-b', 'main', '-o', 'laboratory', str(self.remote), str(self.local))
        self.git(self.local, 'config', 'user.email', 'test@example.invalid')
        self.git(self.local, 'config', 'user.name', 'Test')

    def tearDown(self): self.tmp.cleanup()
    def git(self, directory, *args):
        return run_process('git', ['-C', str(directory), *args]).strip()
    def update(self):
        env = dict(os.environ, TRANSPORT_SOURCE_TEST_ROOT=str(self.local), TRANSPORT_SOURCE_TEST_SCRIPT=str(SCRIPT))
        result = run_process('powershell', ['-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass', '-Command',
            ". $env:TRANSPORT_SOURCE_TEST_SCRIPT; Update-TransportSource $env:TRANSPORT_SOURCE_TEST_ROOT | ConvertTo-Json -Compress"], env)
        return json.loads(result)
    def advance(self):
        (self.seed / 'tracked.txt').write_text('updated', encoding='utf8')
        self.git(self.seed, 'commit', '-am', 'remote update')
        self.git(self.seed, 'push', 'custom', 'main')
    def test_dot_source_has_no_side_effect_and_git_unavailable_skips(self):
        env = dict(os.environ, TRANSPORT_SOURCE_TEST_ROOT=str(self.local), TRANSPORT_SOURCE_TEST_SCRIPT=str(SCRIPT))
        command = (". $env:TRANSPORT_SOURCE_TEST_SCRIPT; "
                   "$env:PATH=''; Update-TransportSource $env:TRANSPORT_SOURCE_TEST_ROOT | ConvertTo-Json -Compress")
        result = run_process('powershell', ['-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass', '-Command', command], env)
        self.assertEqual(json.loads(result)['Status'], 'skipped')
        self.assertEqual((self.local / 'tracked.txt').read_text(), 'initial')

    def test_current_then_remote_update_custom_remote_and_unicode_path(self):
        self.assertEqual(self.update()['Status'], 'current')
        self.advance()
        self.assertEqual(self.update()['Status'], 'updated')
        self.assertEqual((self.local / 'tracked.txt').read_text(), 'updated')
        self.assertEqual(self.git(self.local, 'rev-parse', 'HEAD'), self.git(self.seed, 'rev-parse', 'HEAD'))
    def test_dirty_and_untracked_and_staged_preserved(self):
        self.advance()
        for kind in ('dirty', 'staged', 'untracked'):
            with self.subTest(kind=kind):
                target = self.local / ('extra.txt' if kind == 'untracked' else 'tracked.txt')
                target.write_text('local edit')
                if kind == 'staged': self.git(self.local, 'add', 'tracked.txt')
                self.assertEqual(self.update()['Status'], 'skipped')
                self.assertEqual(target.read_text(), 'local edit')
                if kind == 'staged': self.git(self.local, 'restore', '--staged', 'tracked.txt')
                if kind == 'untracked': target.unlink()
                else: target.write_text('initial')
    def test_nonmain_no_upstream_failed_fetch_and_operation(self):
        self.advance()
        self.git(self.local, 'checkout', '-b', 'other')
        self.assertEqual(self.update()['Status'], 'skipped')
        self.git(self.local, 'checkout', 'main')
        self.git(self.local, 'config', '--unset', 'branch.main.merge')
        self.assertEqual(self.update()['Status'], 'skipped')
        self.git(self.local, 'config', 'branch.main.merge', 'refs/heads/main')
        (self.local / '.git' / 'MERGE_HEAD').write_text('0' * 40)
        self.assertEqual(self.update()['Status'], 'skipped')
        (self.local / '.git' / 'MERGE_HEAD').unlink()
        self.git(self.local, 'remote', 'set-url', 'laboratory', str(self.base / 'missing.git'))
        self.assertEqual(self.update()['Status'], 'skipped')
        self.assertEqual((self.local / 'tracked.txt').read_text(), 'initial')
    def test_ahead_and_diverged_never_reset(self):
        (self.local / 'tracked.txt').write_text('local commit')
        self.git(self.local, 'commit', '-am', 'local')
        before = self.git(self.local, 'rev-parse', 'HEAD')
        self.assertEqual(self.update()['Status'], 'skipped')
        self.advance()
        self.assertEqual(self.update()['Status'], 'skipped')
        self.assertEqual(self.git(self.local, 'rev-parse', 'HEAD'), before)
        self.assertEqual((self.local / 'tracked.txt').read_text(), 'local commit')
