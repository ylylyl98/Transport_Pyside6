"""Hardware-free verified installer download, intended for a background thread."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import re
import sys
import tempfile
import urllib.request
from urllib.parse import urlsplit

MAX_INSTALLER_BYTES = 1024 * 1024 * 1024
CHUNK_BYTES = 256 * 1024
MAX_REDIRECTS = 3
TIMEOUT_SECONDS = 20
INSTALLER_NAME = 'Transport-Setup-windows-x64.exe'
REPOSITORY = '/ylylyl98/Transport_Pyside6/releases/download/'


def _valid_url(url, *, initial=False):
    if not isinstance(url, str) or any(ord(c) < 33 for c in url):
        return False
    try:
        parsed = urlsplit(url)
        if parsed.scheme != 'https' or parsed.username is not None or parsed.password is not None or parsed.port not in (None, 443) or parsed.fragment:
            return False
        if parsed.hostname == 'github.com':
            return (not parsed.query and '%' not in parsed.path and '\\' not in parsed.path
                    and bool(re.fullmatch(re.escape(REPOSITORY) + r'v?[0-9]+(?:\.[0-9]+){1,3}/' + re.escape(INSTALLER_NAME), parsed.path)))
        return not initial and parsed.hostname == 'release-assets.githubusercontent.com' and bool(parsed.path) and '\\' not in parsed.path
    except ValueError:
        return False


class _Redirect(urllib.request.HTTPRedirectHandler):
    def __init__(self):
        self.count = 0

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        self.count += 1
        if self.count > MAX_REDIRECTS or not _valid_url(newurl):
            raise ValueError('Installer redirect is not permitted')
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class _Cancelled(Exception):
    pass


def download_installer(version, url, size, sha256, cache_root, events, cancel_event):
    """Download once; emit progress and exactly one terminal event to a queue."""
    temporary = None
    attempt_dir = None
    try:
        if not isinstance(version, str) or not re.fullmatch(r'v?[0-9]+(?:\.[0-9]+){1,3}', version):
            raise ValueError('Invalid release version')
        if not _valid_url(url, initial=True) or urlsplit(url).path != REPOSITORY + version + '/' + INSTALLER_NAME:
            raise ValueError('Invalid official installer URL')
        if isinstance(size, bool) or not isinstance(size, int) or not 0 < size <= MAX_INSTALLER_BYTES:
            raise ValueError('Invalid installer size')
        if not isinstance(sha256, str) or not re.fullmatch(r'(?:sha256:)?[0-9a-fA-F]{64}', sha256):
            raise ValueError('Invalid installer SHA256')
        expected_hash = sha256.removeprefix('sha256:').lower()
        root = Path(cache_root).expanduser().resolve()
        target_dir = (root / 'Transport' / 'updates' / version).resolve()
        if not target_dir.is_relative_to(root):
            raise ValueError('Invalid update cache directory')
        if getattr(sys, 'frozen', False) and target_dir.is_relative_to(Path(sys.executable).resolve().parent):
            raise ValueError('Update cache cannot be inside the program directory')
        if cancel_event.is_set():
            raise _Cancelled()
        target_dir.mkdir(parents=True, exist_ok=True)
        attempt_dir = Path(tempfile.mkdtemp(prefix='attempt-', dir=target_dir))
        descriptor, name = tempfile.mkstemp(prefix='download-', suffix='.partial', dir=attempt_dir)
        temporary = Path(name)
        digest = hashlib.sha256()
        received = 0
        request = urllib.request.Request(url, headers={'User-Agent': 'Transport-Measurement-Updater', 'Accept': 'application/octet-stream'})
        with os.fdopen(descriptor, 'wb') as output:
            opener = urllib.request.build_opener(_Redirect())
            with opener.open(request, timeout=TIMEOUT_SECONDS) as response:
                if not _valid_url(response.geturl()):
                    raise ValueError('Installer response URL is not permitted')
                length = response.headers.get('Content-Length')
                if length is not None and (not length.isdecimal() or int(length) != size):
                    raise ValueError('Installer size does not match release metadata')
                while True:
                    if cancel_event.is_set():
                        raise _Cancelled()
                    chunk = response.read(min(CHUNK_BYTES, size - received + 1))
                    if not chunk:
                        break
                    received += len(chunk)
                    if received > size:
                        raise ValueError('Installer exceeds declared size')
                    output.write(chunk)
                    digest.update(chunk)
                    events.put({'type': 'progress', 'received': received, 'total': size})
        if received != size or digest.hexdigest() != expected_hash:
            raise ValueError('Installer size or SHA256 does not match release metadata')
        if cancel_event.is_set():
            raise _Cancelled()
        target = attempt_dir / INSTALLER_NAME
        os.replace(temporary, target)
        temporary = None
        events.put({'type': 'complete', 'path': str(target), 'version': version})
    except _Cancelled:
        events.put({'type': 'cancelled'})
    except Exception:
        events.put({'type': 'error', 'message': 'Installer download or verification failed. Check the connection and retry.'})
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
        if attempt_dir is not None and temporary is not None:
            try:
                attempt_dir.rmdir()
            except OSError:
                pass
