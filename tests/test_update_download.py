"""Verified installer download is isolated, bounded and cancellable."""
import hashlib
import io
import os
from pathlib import Path
import queue
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.request import Request

if os.environ.get('TRANSPORT_OFFLINE_VERIFICATION') != '1':
    raise unittest.SkipTest('Use verify_offline.py')

from app import update_download as download

URL = 'https://github.com/ylylyl98/Transport_Pyside6/releases/download/v2026.10.31/Transport-Setup-windows-x64.exe'
ASSET = 'https://release-assets.githubusercontent.com/abc?token=signed'


class Response(io.BytesIO):
    def __init__(self, data, url=ASSET, length=None):
        super().__init__(data)
        self.url = url
        self.headers = {} if length is None else {'Content-Length': str(length)}
    def geturl(self): return self.url


class Opener:
    def __init__(self, response): self.response = response
    def open(self, request, timeout): return self.response


class DownloadTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.events = queue.Queue()
        self.cancel = threading.Event()
        self.data = b'installer bytes' * 50000
        self.digest = hashlib.sha256(self.data).hexdigest()

    def call(self, response=None, **kwargs):
        options = dict(version='v2026.10.31', url=URL, size=len(self.data), sha256=self.digest,
                       cache_root=self.temp.name, events=self.events, cancel_event=self.cancel)
        options.update(kwargs)
        with patch('app.update_download.urllib.request.build_opener', return_value=Opener(response or Response(self.data))):
            download.download_installer(**options)
        rows = []
        while not self.events.empty(): rows.append(self.events.get_nowait())
        return rows

    def partials(self): return list(Path(self.temp.name).rglob('*.partial'))

    def test_complete_verified_with_progress_and_unique_attempts(self):
        for _ in range(2):
            rows = self.call()
            self.assertEqual(rows[-1]['type'], 'complete')
            self.assertEqual(rows[-1]['version'], 'v2026.10.31')
            self.assertEqual(Path(rows[-1]['path']).read_bytes(), self.data)
            self.assertEqual(rows[-2]['received'], len(self.data))
            self.assertEqual(rows[-2]['total'], len(self.data))
            if _: self.assertNotEqual(rows[-1]['path'], first)
            first = rows[-1]['path']
        self.assertFalse(self.partials())

    def test_bad_hash_size_truncation_and_connection_leave_no_partial(self):
        for response, kwargs in ((Response(self.data), {'sha256':'0'*64}),
                                 (Response(self.data[:-1]), {}),
                                 (Response(self.data + b'x'), {}),
                                 (Response(self.data, length=1), {})):
            self.assertEqual(self.call(response, **kwargs)[-1]['type'], 'error')
            self.assertFalse(self.partials())
        with patch('app.update_download.urllib.request.build_opener', side_effect=OSError('token=secret')):
            download.download_installer('v2026.10.31', URL, len(self.data), self.digest, self.temp.name, self.events, self.cancel)
        self.assertNotIn('secret', self.events.get_nowait()['message'])
        self.assertFalse(self.partials())

    def test_cancel_during_stream_leaves_no_partial_and_retry_works(self):
        class CancelResponse(Response):
            def read(inner, size):
                result = super(CancelResponse, inner).read(size)
                self.cancel.set()
                return result
        rows = self.call(CancelResponse(self.data))
        self.assertEqual(rows[-1]['type'], 'cancelled')
        self.assertFalse(self.partials())
        self.cancel.clear()
        self.assertEqual(self.call()[-1]['type'], 'complete')

    def test_reject_invalid_inputs_and_redirects(self):
        for url in (URL.replace('https:', 'http:'), URL.replace('github.com', 'evil.example'),
                    URL.replace('github.com', 'user@github.com'), URL.replace('v2026.10.31', 'v2026.10.30')):
            self.assertEqual(self.call(url=url)[-1]['type'], 'error')
        self.assertEqual(self.call(sha256='bad')[-1]['type'], 'error')
        self.assertEqual(self.call(size=download.MAX_INSTALLER_BYTES+1)[-1]['type'], 'error')
        self.assertEqual(self.call(Response(self.data, url='https://evil.example/file'))[-1]['type'], 'error')
        handler = download._Redirect()
        req = Request(URL)
        for url in ('http://release-assets.githubusercontent.com/file', 'https://evil.example/file',
                    'https://user@release-assets.githubusercontent.com/file'):
            with self.assertRaises(ValueError): handler.redirect_request(req, None, 302, '', {}, url)
        handler = download._Redirect()
        for _ in range(download.MAX_REDIRECTS):
            self.assertIsNotNone(handler.redirect_request(req, None, 302, '', {}, ASSET))
        with self.assertRaises(ValueError): handler.redirect_request(req, None, 302, '', {}, ASSET)
        self.assertFalse(self.partials())
