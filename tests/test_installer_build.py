"""Installer metadata and integrity checks must describe the actual payload."""
import hashlib
import os
from pathlib import Path
import tempfile
import unittest

if os.environ.get("TRANSPORT_OFFLINE_VERIFICATION") != "1":
    raise unittest.SkipTest("Run with verify_offline.py")

from tools.build_installer import package_version, payload_manifest, write_checksums, INSTALLER_NAME


class InstallerBuildTests(unittest.TestCase):
    def test_tag_version_and_development_provenance(self):
        info = dict(commit="a" * 40, ref="refs/tags/v2026.10.29")
        self.assertEqual(package_version(info), "2026.10.29")
        self.assertEqual(package_version(info, "v1.2.3-rc.1"), "1.2.3-rc.1")
        self.assertEqual(package_version(dict(commit="b" * 40, ref="local")), "0.0.0-dev." + "b" * 12)
        for bad in ('../outside', '1.2"\nAppId=other', 'release'):
            with self.assertRaises(ValueError):
                package_version(info, bad)
        with self.assertRaises(ValueError):
            package_version(dict(commit="missing", ref="local"))

    def test_manifest_covers_runtime_and_rejects_incomplete_bundle(self):
        with tempfile.TemporaryDirectory() as directory:
            bundle = Path(directory)
            (bundle / '_internal').mkdir()
            names = ('Transport.exe', 'TransportWorker.exe', 'build-info.json', '_internal/runtime.dll')
            for name in names:
                (bundle / name).write_bytes(name.encode())
            manifest = payload_manifest(bundle, '1.0', dict(commit='a' * 40))
            self.assertEqual(set(manifest['files']), set(names))
            self.assertEqual(manifest['files']['_internal/runtime.dll'], hashlib.sha256(b'_internal/runtime.dll').hexdigest())
            (bundle / 'installer-manifest.json').write_text('{}')
            with self.assertRaisesRegex(ValueError, 'reserved'):
                payload_manifest(bundle, '1.0', dict(commit='a' * 40))
            (bundle / 'installer-manifest.json').unlink()
            (bundle / 'TransportWorker.exe').unlink()
            with self.assertRaisesRegex(ValueError, 'TransportWorker'):
                payload_manifest(bundle, '1.0', dict(commit='a' * 40))

    def test_checksums_cover_both_assets_and_refuse_partial_release(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            (output / 'Transport-windows-x64.zip').write_bytes(b'portable')
            with self.assertRaises(ValueError):
                write_checksums(output)
            (output / INSTALLER_NAME).write_bytes(b'installer')
            write_checksums(output)
            # The Linux release gate matches complete lines, so Windows CRLF
            # translation must not change the portable checksum format.
            self.assertNotIn(b'\r', (output / 'SHA256SUMS.txt').read_bytes())
            lines = (output / 'SHA256SUMS.txt').read_text().splitlines()
            self.assertEqual(lines, [hashlib.sha256(b'portable').hexdigest() + '  Transport-windows-x64.zip',
                                     hashlib.sha256(b'installer').hexdigest() + '  ' + INSTALLER_NAME])
