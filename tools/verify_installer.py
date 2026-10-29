"""Exercise an isolated real Windows install, reinstall, installed EXEs and uninstall."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
from pathlib import Path, PurePosixPath
import subprocess
import tempfile
import time
import uuid

if __package__:
    from tools.verify_frozen import isolated_environment
else:
    from verify_frozen import isolated_environment


def verify_payload(app, files):
    """Reject unsafe manifest names and verify every packaged byte."""
    if not files or not isinstance(files, dict):
        raise ValueError("Installer manifest has no files")
    for name, digest in files.items():
        relative = PurePosixPath(name)
        if relative.is_absolute() or '..' in relative.parts or '\\' in name or ':' in name:
            raise ValueError("Unsafe manifest path: " + name)
        path = app.joinpath(*relative.parts)
        if not path.is_file():
            raise RuntimeError("Missing installed file: " + name)
        with path.open('rb') as stream:
            actual = hashlib.file_digest(stream, 'sha256').hexdigest()
        if actual != digest:
            raise RuntimeError("Installed checksum mismatch: " + name)
    for required in ('Transport.exe', 'TransportWorker.exe', 'build-info.json'):
        if required not in files:
            raise ValueError("Manifest omits " + required)


def run(command, cwd, log, timeout, environment=None):
    with log.open('wb') as stream:
        try:
            result = subprocess.run(command, cwd=cwd, env=environment, stdout=stream,
                                    stderr=subprocess.STDOUT, timeout=timeout, check=False)
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(f"Process timed out after {timeout}s; see {log}") from exc
    if result.returncode != 0:
        raise RuntimeError(f"Process exited {result.returncode}; see {log}")


def wait_for_uninstall(app, files, shortcut, timeout=30):
    # Inno's launcher can exit before its temporary child removes the original
    # uninstaller. Wait for observed removal, never delete files to make it pass.
    paths = [app / name for name in files]
    paths.extend((app / 'unins000.exe', app / 'unins000.dat',
                  app / 'installer-manifest.json', shortcut))
    deadline = time.monotonic() + timeout
    while True:
        remaining = [path for path in paths if path.exists()]
        if not remaining:
            return
        if time.monotonic() >= deadline:
            raise RuntimeError('Uninstall left program files or shortcuts: '
                               + repr([str(path) for path in remaining[:10]]))
        time.sleep(0.1)


def smoke(app, output, cwd, timeout):
    for mode in ('quick', 'widgets'):
        destination = output / mode
        destination.mkdir(parents=True, exist_ok=True)
        report = destination / 'report.json'
        report.unlink(missing_ok=True)
        run([str(app / 'Transport.exe'), '--verify-package', str(destination), '--mode', mode],
            cwd, destination / 'process.log', timeout, isolated_environment())
        evidence = json.loads(report.read_text(encoding='utf-8'))
        if not (evidence.get('success') is True and evidence.get('frozen') is True
                and evidence.get('mode') == mode and evidence.get('hardware') == 'blocked'
                and Path(evidence.get('executable', '')).resolve() == (app / 'Transport.exe').resolve()):
            raise RuntimeError('Installed EXE did not publish valid offline frozen evidence: ' + mode)


def registration(app, token):
    import winreg
    parent = r'Software\Microsoft\Windows\CurrentVersion\Uninstall'
    found = []
    for view in (winreg.KEY_WOW64_64KEY, winreg.KEY_WOW64_32KEY):
        try:
            parent_key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, parent, 0, winreg.KEY_READ | view)
        except FileNotFoundError:
            continue
        with parent_key as root:
            for index in range(winreg.QueryInfoKey(root)[0]):
                name = winreg.EnumKey(root, index)
                if token not in name:
                    continue
                with winreg.OpenKey(root, name) as key:
                    values = {}
                    for value_name in ('InstallLocation', 'UninstallString', 'QuietUninstallString'):
                        values[value_name] = winreg.QueryValueEx(key, value_name)[0]
                    if Path(values['InstallLocation']).resolve() != app.resolve():
                        raise RuntimeError('Smoke registration points outside temporary install')
                    if str(app / 'unins000.exe').lower() not in values['UninstallString'].lower():
                        raise RuntimeError('Unexpected uninstall entry')
                    found.append(name)
    if not found:
        raise RuntimeError('Isolated HKCU uninstall registration missing')
    return set(found)



def verify_process_guards(command, app, files, root, output, timeout):
    """Use only our harmless renamed Windows ping, never the real application."""
    guard = root / 'guard process'
    guard.mkdir()
    fake = guard / 'Transport.exe'
    shutil.copy2(Path(os.environ['SystemRoot']) / 'System32/ping.exe', fake)
    process = subprocess.Popen([str(fake), '-t', '127.0.0.1'], cwd=guard,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        if process.poll() is not None:
            raise RuntimeError('Harmless process guard fixture did not start')
        uninstall = [str(app / 'unins000.exe'), '/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART']
        for name, guarded_command in (('install', command), ('uninstall', uninstall)):
            log = output / ('blocked-' + name + '-process.log')
            with log.open('wb') as stream:
                try:
                    completed = subprocess.run(guarded_command + ['/LOG=' + str(output / ('blocked-' + name + '.log'))],
                                               cwd=root, stdout=stream, stderr=subprocess.STDOUT,
                                               timeout=timeout, check=False)
                except subprocess.TimeoutExpired as exc:
                    raise RuntimeError('Process guard check timed out: ' + name) from exc
            if completed.returncode == 0:
                raise RuntimeError('Running Transport guard did not block ' + name)
            if process.poll() is not None:
                raise RuntimeError('Installer terminated the harmless process fixture')
            verify_payload(app, files)
    finally:
        # This handle belongs only to the ping process created above.
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=10)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--installer', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--manifest', type=Path, help='Expected manifest (defaults beside installer)')
    parser.add_argument('--timeout', type=int, default=240)
    args = parser.parse_args(argv)
    if os.name != 'nt':
        parser.error('Installer acceptance requires Windows')
    if args.timeout <= 0:
        parser.error('--timeout must be positive')
    installer, output = args.installer.resolve(), args.output.resolve()
    if not installer.is_file():
        parser.error('Missing installer: ' + str(installer))
    manifest = json.loads((args.manifest or installer.with_suffix('.manifest.json')).read_text(encoding='utf-8'))
    files = manifest['files']
    output.mkdir(parents=True, exist_ok=True)
    summary = output / 'summary.json'
    summary.unlink(missing_ok=True)
    token = uuid.uuid4().hex
    result = dict(success=False, token=token, installer=str(installer))
    # Only this freshly allocated tree is ever automatically removed.
    temporary = tempfile.mkdtemp(prefix='Transport installer smoke ')
    root = Path(temporary)
    app = root / 'installed app'
    cwd = root / 'unrelated working directory'
    cwd.mkdir()
    shortcut = Path(os.environ['APPDATA']) / 'Microsoft/Windows/Start Menu/Programs' / ('Transport Measurement Smoke ' + token)
    installed = False
    try:
        command = [str(installer), '/CURRENTUSER', '/VERYSILENT', '/SUPPRESSMSGBOXES',
                   '/NORESTART', '/TASKS=', '/NOCLOSEAPPLICATIONS', '/NORESTARTAPPLICATIONS',
                   '/SMOKETEST=' + token, '/DIR=' + str(app)]
        installed = True
        run(command + ['/LOG=' + str(output / 'install.log')], cwd, output / 'install-process.log', args.timeout)
        installed = True
        verify_payload(app, files)
        if json.loads((app / 'installer-manifest.json').read_text(encoding='utf-8')) != manifest:
            raise RuntimeError('Installed manifest differs from release manifest')
        registration(app, token)
        if not (shortcut / 'Transport Measurement.lnk').is_file():
            raise RuntimeError('Start menu shortcut missing: ' + str(shortcut))
        sentinels = {app / 'user-data/sentinel.csv': b'user measurement\n',
                     app / 'user-config/sentinel.json': b'{"preserve":true}\n'}
        for path, data in sentinels.items():
            path.parent.mkdir(parents=True)
            path.write_bytes(data)
        verify_process_guards(command, app, files, root, output, args.timeout)
        result['process_guards'] = True
        smoke(app, output / 'installed', cwd, args.timeout)
        run(command + ['/LOG=' + str(output / 'upgrade.log')], cwd, output / 'upgrade-process.log', args.timeout)
        verify_payload(app, files)
        if json.loads((app / 'installer-manifest.json').read_text(encoding='utf-8')) != manifest:
            raise RuntimeError('Installed manifest differs from release manifest')
        registration(app, token)
        smoke(app, output / 'upgraded', cwd, args.timeout)
        for path, data in sentinels.items():
            if path.read_bytes() != data:
                raise RuntimeError('Upgrade modified user data: ' + str(path))
        run([str(app / 'unins000.exe'), '/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART',
             '/LOG=' + str(output / 'uninstall.log')], cwd, output / 'uninstall-process.log', args.timeout)
        installed = False
        wait_for_uninstall(app, files, shortcut, timeout=min(args.timeout, 30))
        try:
            registration(app, token)
        except RuntimeError as exc:
            if 'registration missing' not in str(exc):
                raise
        else:
            raise RuntimeError('Uninstall registration remains')
        for path, data in sentinels.items():
            if path.read_bytes() != data:
                raise RuntimeError('Uninstall removed user data: ' + str(path))
        result['success'] = True
    except (OSError, ValueError, KeyError, RuntimeError) as exc:
        result['error'] = str(exc)
    finally:
        if installed and (app / 'unins000.exe').is_file() and 'timed out' not in result.get('error', ''):
            try:
                run([str(app / 'unins000.exe'), '/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART'],
                    cwd, output / 'cleanup-process.log', args.timeout)
            except (OSError, RuntimeError) as exc:
                result['cleanup_error'] = str(exc)
        summary.write_text(json.dumps(result, indent=2), encoding='utf-8')
    if result['success']:
        shutil.rmtree(root)
    else:
        result['retained_directory'] = str(root)
        result.setdefault('cleanup_error', 'Temporary tree retained after failure; an installer child may still be running.')
        summary.write_text(json.dumps(result, indent=2), encoding='utf-8')
    print(json.dumps(result, indent=2), flush=True)
    return 0 if result['success'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
