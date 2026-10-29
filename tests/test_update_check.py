import io
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from PySide6 import QtCore

from app import update_check as updates


def release(tag='v2026.10.30'):
    return dict(tag_name=tag, draft=False, prerelease=False,
                html_url='https://github.com/ylylyl98/Transport_Pyside6/releases/tag/' + tag,
                assets=[dict(name=updates.INSTALLER_NAME, browser_download_url=
                             'https://github.com/ylylyl98/Transport_Pyside6/releases/download/'
                             + tag + '/' + updates.INSTALLER_NAME)])


class UpdateParsingTests(unittest.TestCase):
    def test_calendar_semver_and_older_versions(self):
        self.assertEqual(updates.parse_version('v2026.01.09'), (2026, 1, 9, 0))
        self.assertEqual(updates.parse_version('v1.2.3+build.4'), (1, 2, 3, 0))
        self.assertIsNone(updates.parse_version('v1.2.3-rc1'))
        self.assertIsNone(updates.parse_version('main'))
        self.assertTrue(updates.release_details(release(), 'v2026.10.29')['newer'])
        self.assertFalse(updates.release_details(release('v1.2.0'), 'v1.3.0')['newer'])
        self.assertFalse(updates.release_details(release('v1.2.0'), 'v1.2')['newer'])

    def test_reject_missing_installer_drafts_and_unknown_current(self):
        for field in ('draft', 'prerelease'):
            payload = release()
            payload[field] = True
            self.assertIsNone(updates.release_details(payload, 'v1.0.0'))
        payload = release()
        payload['assets'] = []
        self.assertIsNone(updates.release_details(payload, 'v1.0.0'))
        self.assertIsNone(updates.release_details(release(), ''))

    def test_strict_url_validation(self):
        valid = release()['html_url']
        self.assertTrue(updates.allowed_release_url(valid))
        for malicious in (valid.replace('https:', 'http:'),
                          valid.replace('github.com', 'github.com.evil.example'),
                          valid.replace('github.com', 'user@github.com'),
                          valid.replace('github.com', 'github.com:443'),
                          valid.replace('/ylylyl98/', '/attacker/'),
                          valid + '?redirect=evil', valid + '#fragment',
                          valid + '/../../other', valid + '%2f..', 'javascript:alert(1)'):
            with self.subTest(url=malicious):
                self.assertFalse(updates.allowed_release_url(malicious))
                payload = release()
                payload['html_url'] = malicious
                self.assertIsNone(updates.release_details(payload, 'v1.0.0'))

    def test_build_info_tag_is_authoritative(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.assertEqual(updates.read_current_version(root), '')
            (root / 'installer-manifest.json').write_text(json.dumps({'app_version': '1.2.3'}))
            self.assertEqual(updates.read_current_version(root), '1.2.3')
            path = root / 'build-info.json'
            path.write_text(json.dumps({'ref': 'refs/heads/main'}))
            self.assertEqual(updates.read_current_version(root), '')
            path.write_text(json.dumps({'ref': 'refs/tags/v2026.10.29'}))
            self.assertEqual(updates.read_current_version(root), 'v2026.10.29')
            path.write_text('not JSON')
            self.assertEqual(updates.read_current_version(root), '')

    def test_fetch_limits_headers_timeout_and_redirects(self):
        class Opener:
            def open(self, request, timeout):
                self.request, self.timeout = request, timeout
                return io.BytesIO(json.dumps(release()).encode())
        opener = Opener()
        with patch.object(updates.urllib.request, 'build_opener', return_value=opener):
            self.assertEqual(updates.fetch_release()['tag_name'], 'v2026.10.30')
        self.assertEqual(opener.request.full_url, updates.API_URL)
        self.assertEqual(opener.timeout, 10)
        self.assertEqual(opener.request.get_header('Accept'), 'application/vnd.github+json')
        self.assertIn('Transport', opener.request.get_header('User-agent'))
        with patch.object(opener, 'open', return_value=io.BytesIO(b'x' * (updates.MAX_RESPONSE + 1))), \
                patch.object(updates.urllib.request, 'build_opener', return_value=opener):
            with self.assertRaises(ValueError):
                updates.fetch_release()
        with patch.object(opener, 'open', side_effect=TimeoutError), \
                patch.object(updates.urllib.request, 'build_opener', return_value=opener):
            with self.assertRaises(TimeoutError):
                updates.fetch_release()
        with self.assertRaises(ValueError):
            updates._NoRedirect().redirect_request(None, None, 302, '', {}, 'https://evil.example')


class UpdateServiceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.settings = QtCore.QSettings(str(Path(self.temporary.name) / 'updates.ini'), QtCore.QSettings.Format.IniFormat)
        self.service = updates.UpdateService(settings=self.settings)

    def tearDown(self):
        self.service.close()
        self.temporary.cleanup()

    def test_offline_and_source_never_request(self):
        self.service._current = 'v1.0.0'
        with patch.object(updates, 'fetch_release') as fetch:
            self.service.start()
            self.service.checkNow()
            self.assertFalse(self.service._initial.isActive())
            self.assertFalse(self.service._periodic.isActive())
            fetch.assert_not_called()
            self.assertEqual(self.service.status, 'disabled')

    def test_preferences_and_session_dismiss(self):
        self.service.setAutoCheck(False)
        other = updates.UpdateService(settings=self.settings)
        self.assertFalse(other.autoCheck)
        other.close()
        self.service._current = 'v1.0.0'
        self.service._receive(release(), '')
        self.assertTrue(self.service.available)
        self.service.ignoreLatest()
        self.assertFalse(self.service.available)
        self.assertFalse(self.settings.contains('updates/ignoredVersion'))
        with patch.object(self.service, '_eligible', return_value=True), \
                patch.object(updates, 'fetch_release', return_value=release()):
            self.service.checkNow()
            result = self.service._results.get(timeout=1)
            self.service._results.put(result)
            self.service._poll_result()
        self.assertTrue(self.service.available)
        self.assertFalse(self.service.checking)

    def test_scheduling_and_unknown_builds(self):
        with patch.object(self.service, '_eligible', return_value=True), \
                patch.object(updates, 'fetch_release') as fetch:
            self.service.start()
            self.service.checkNow()
            self.assertEqual(self.service.status, 'unknown')
            fetch.assert_not_called()
            self.service._current = 'v1.2.3'
            self.service.setAutoCheck(True)
            self.assertEqual(self.service._initial.interval(), 10_000)
            self.assertEqual(self.service._periodic.interval(), 6 * 60 * 60 * 1000)
            self.assertTrue(self.service._initial.isActive())
            self.assertTrue(self.service._periodic.isActive())
            self.service.setAutoCheck(False)
            self.assertFalse(self.service._initial.isActive())
            self.assertFalse(self.service._periodic.isActive())

    def test_background_error_and_close_does_not_wait_for_network(self):
        self.service._current = 'v1.0.0'
        entered, unblock = threading.Event(), threading.Event()
        def slow_fetch():
            entered.set()
            unblock.wait(5)
            raise TimeoutError
        with patch.object(self.service, '_eligible', return_value=True), \
                patch.object(updates, 'fetch_release', side_effect=slow_fetch):
            self.service.checkNow()
            self.assertTrue(entered.wait(1))
            started = time.monotonic()
            self.service.close()
            self.assertLess(time.monotonic() - started, 0.5)
            unblock.set()
        # Detached worker cannot notify or mutate the closed QObject.
        self.assertFalse(self.service._poll.isActive())
        self.service._receive(release(), '')
        self.assertFalse(self.service.available)

    def test_failure_message_and_safe_open(self):
        self.service._receive(None, 'Could not check GitHub releases.')
        self.assertEqual(self.service.status, 'error')
        self.assertFalse(self.service.available)
        self.service._url = 'https://evil.example'
        with patch.object(updates.QtGui.QDesktopServices, 'openUrl') as opened:
            self.service.openRelease()
            opened.assert_not_called()
