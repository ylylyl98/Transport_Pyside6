"""Configurable release notices; never install, restart, or inspect hardware."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import sys
import queue
import threading
import urllib.request
from urllib.parse import urlsplit

from PySide6 import QtCore, QtGui

from app.settings import get_app_settings

API_URL = 'https://api.github.com/repos/ylylyl98/Transport_Pyside6/releases/latest'
REPOSITORY_PATH = '/ylylyl98/Transport_Pyside6'
INSTALLER_NAME = 'Transport-Setup-windows-x64.exe'
MAX_RESPONSE = 1024 * 1024
NETWORK_TIMEOUT = 10


def parse_version(value):
    """Compare stable semver and calendar tags, including zero-padded dates."""
    if not isinstance(value, str) or len(value) > 100:
        return None
    match = re.fullmatch(r'v?(\d+(?:\.\d+){1,3})(?:\+[0-9A-Za-z.-]+)?', value)
    if not match:
        return None
    parts = tuple(int(part) for part in match.group(1).split('.'))
    return parts + (0,) * (4 - len(parts))


def allowed_release_url(value, *, download=False):
    if not isinstance(value, str) or len(value) > 2048 or any(ord(c) < 33 for c in value):
        return False
    try:
        url = urlsplit(value)
    except ValueError:
        return False
    prefix = REPOSITORY_PATH + ('/releases/download/' if download else '/releases/tag/')
    return (url.scheme == 'https' and url.netloc == 'github.com'
            and not url.query and not url.fragment and '\\' not in value and '%' not in value
            and url.path.startswith(prefix) and len(url.path) > len(prefix)
            and '/..' not in url.path
            and bool(re.fullmatch(r'[0-9A-Za-z.+_-]+(?:/[0-9A-Za-z.+_-]+)?' if download
                                  else r'[0-9A-Za-z.+_-]+', url.path[len(prefix):])))


def read_current_version(directory):
    """A build-info source ref is authoritative, including an untagged build."""
    directory = Path(directory)
    info_path = directory / 'build-info.json'
    try:
        if info_path.exists():
            info = json.loads(info_path.read_text(encoding='utf-8'))
            ref = info.get('ref', '')
            version = ref[len('refs/tags/'):] if isinstance(ref, str) and ref.startswith('refs/tags/') else ''
        else:
            info = json.loads((directory / 'installer-manifest.json').read_text(encoding='utf-8'))
            version = info.get('app_version', '')
        return version if parse_version(version) is not None else ''
    except (OSError, ValueError, TypeError, AttributeError):
        return ''


def release_details(payload, current_version):
    """Return only a stable newer release with this application's installer."""
    current = parse_version(current_version)
    if current is None or not isinstance(payload, dict):
        return None
    tag = payload.get('tag_name')
    latest = parse_version(tag)
    if (latest is None or payload.get('draft') is not False
            or payload.get('prerelease') is not False
            or not allowed_release_url(payload.get('html_url'))):
        return None
    assets = payload.get('assets', [])
    if not isinstance(assets, list) or not any(
        isinstance(asset, dict) and asset.get('name') == INSTALLER_NAME
        and allowed_release_url(asset.get('browser_download_url'), download=True)
        for asset in assets
    ):
        return None
    return {'version': tag, 'url': payload['html_url'], 'newer': latest > current}


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError('Release API redirects are not accepted')


def fetch_release():
    request = urllib.request.Request(API_URL, headers={
        'User-Agent': 'Transport-Measurement-Update-Check',
        'Accept': 'application/vnd.github+json',
    })
    with urllib.request.build_opener(_NoRedirect).open(request, timeout=NETWORK_TIMEOUT) as response:
        raw = response.read(MAX_RESPONSE + 1)
    if len(raw) > MAX_RESPONSE:
        raise ValueError('Release response exceeds 1 MiB')
    return json.loads(raw.decode('utf-8'))


def _fetch_into_queue(results):
    try:
        results.put((fetch_release(), ''))
    except Exception:
        # The worker has no QObject, application, settings or hardware references.
        results.put((None, 'Could not check GitHub releases. Check your connection and retry.'))


class UpdateService(QtCore.QObject):
    changed = QtCore.Signal()

    def __init__(self, parent=None, *, settings=None):
        super().__init__(parent)
        self._settings = settings if settings is not None else get_app_settings()
        self._current = read_current_version(Path(sys.executable).parent) if getattr(sys, 'frozen', False) else ''
        self._latest = self._url = ''
        self._available = self._checking = self._closed = False
        self._auto = self._settings.value('updates/autoCheck', True, type=bool)
        self._results = queue.Queue()
        self._ignored = ''
        self._started = False
        self._status, self._message = 'idle', 'Updates are checked on GitHub; downloads and installation are manual.'
        self._initial = QtCore.QTimer(self)
        self._initial.setSingleShot(True)
        self._initial.setInterval(10_000)
        self._initial.timeout.connect(lambda: self._begin_check(manual=False))
        self._periodic = QtCore.QTimer(self)
        self._periodic.setInterval(6 * 60 * 60 * 1000)
        self._periodic.timeout.connect(lambda: self._begin_check(manual=False))
        self._poll = QtCore.QTimer(self)
        self._poll.setInterval(100)
        self._poll.timeout.connect(self._poll_result)
        app = QtCore.QCoreApplication.instance()
        if app is not None:
            app.aboutToQuit.connect(self.close)

    currentVersion = QtCore.Property(str, lambda self: self._current, notify=changed)
    latestVersion = QtCore.Property(str, lambda self: self._latest, notify=changed)
    status = QtCore.Property(str, lambda self: self._status, notify=changed)
    message = QtCore.Property(str, lambda self: self._message, notify=changed)
    available = QtCore.Property(bool, lambda self: self._available, notify=changed)
    checking = QtCore.Property(bool, lambda self: self._checking, notify=changed)
    autoCheck = QtCore.Property(bool, lambda self: self._auto, notify=changed)
    releaseUrl = QtCore.Property(str, lambda self: self._url, notify=changed)

    def _eligible(self):
        return (bool(getattr(sys, 'frozen', False))
                and not os.environ.get('TRANSPORT_OFFLINE_VERIFICATION')
                and not os.environ.get('TRANSPORT_UI_PREVIEW'))

    @QtCore.Slot()
    def start(self):
        if self._closed or self._started:
            return
        self._started = True
        if self._auto and self._eligible() and self._current:
            self._initial.start()
            self._periodic.start()

    @QtCore.Slot(bool)
    def setAutoCheck(self, enabled):
        self._auto = bool(enabled)
        self._settings.setValue('updates/autoCheck', self._auto)
        self._initial.stop()
        self._periodic.stop()
        if self._started and not self._closed and self._auto and self._eligible() and self._current:
            self._initial.start()
            self._periodic.start()
        self.changed.emit()

    @QtCore.Slot()
    def checkNow(self):
        self._begin_check(manual=True)

    def _begin_check(self, *, manual):
        if self._closed or self._checking:
            return
        if not self._eligible() or not self._current:
            self._status = 'disabled' if not self._eligible() else 'unknown'
            self._message = ('Update checks are disabled in source/offline/preview sessions.'
                             if not self._eligible() else 'This build has no release version. Check GitHub manually.')
            self.changed.emit()
            return
        if manual:
            self._ignored = ''
        self._checking = True
        self._status, self._message = 'checking', 'Checking GitHub releases…'
        self.changed.emit()
        self._poll.start()
        threading.Thread(target=_fetch_into_queue, args=(self._results,), daemon=True,
                         name='transport-update-check').start()

    def _poll_result(self):
        try:
            payload, error = self._results.get_nowait()
        except queue.Empty:
            return
        self._poll.stop()
        self._receive(payload, error)

    @QtCore.Slot(object, str)
    def _receive(self, payload, error):
        if self._closed:
            return
        self._checking = False
        self._available = False
        self._latest = self._url = ''
        details = release_details(payload, self._current) if not error else None
        if error or details is None:
            self._status = 'error'
            self._message = error or 'GitHub did not return a supported stable release with a Windows installer.'
        else:
            self._latest, self._url = details['version'], details['url']
            self._available = details['newer'] and parse_version(self._ignored) != parse_version(self._latest)
            self._status = 'available' if self._available else ('ignored' if details['newer'] else 'current')
            self._message = (f'Transport {self._latest} is available. Download and install it after safely stopping measurements.'
                             if self._available else ('This release is ignored.' if details['newer'] else 'You have the latest stable release.'))
        self.changed.emit()

    @QtCore.Slot()
    def ignoreLatest(self):
        if self._latest:
            self._ignored = self._latest
            self._available = False
            self._status, self._message = 'ignored', 'This release is ignored.'
            self.changed.emit()

    @QtCore.Slot()
    def openRelease(self):
        if allowed_release_url(self._url) and self._eligible():
            QtGui.QDesktopServices.openUrl(QtCore.QUrl(self._url))

    @QtCore.Slot()
    def close(self):
        if self._closed:
            return
        self._closed = True
        self._initial.stop()
        self._periodic.stop()
        self._poll.stop()
        # A pending daemon request retains only its result queue. Closing the
        # app never waits for DNS/HTTP and no worker can call a destroyed QObject.
        self._checking = False
