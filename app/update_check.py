"""Background release checks and user-requested, verified update downloads."""
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
from app.update_download import download_installer, MAX_INSTALLER_BYTES
from app.update_install import launch_after_exit

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
    if payload['html_url'] != 'https://github.com' + REPOSITORY_PATH + '/releases/tag/' + tag:
        return None
    installer = None
    expected_url = 'https://github.com' + REPOSITORY_PATH + '/releases/download/' + tag + '/' + INSTALLER_NAME
    for asset in assets:
        if not isinstance(asset, dict) or asset.get('name') != INSTALLER_NAME:
            continue
        size, digest = asset.get('size'), asset.get('digest')
        if (asset.get('browser_download_url') == expected_url
                and type(size) is int and 0 < size <= MAX_INSTALLER_BYTES
                and isinstance(digest, str) and re.fullmatch(r'sha256:[0-9a-fA-F]{64}', digest)):
            installer = dict(version=tag, url=expected_url, size=size, sha256=digest[7:].lower())
            break
    return {'version': tag, 'url': payload['html_url'], 'newer': latest > current, 'installer': installer}


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
    installRequested = QtCore.Signal()

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
        self._dialog_visible = False
        self._installer = self._download_details = None
        self._installer_path = ''
        self._download_state, self._download_message = 'idle', ''
        self._received_bytes = self._total_bytes = 0
        self._download_results = queue.Queue()
        self._download_cancel = threading.Event()
        self._install_pending = self._install_started = False
        self._install_guard = lambda: False
        self._status, self._message = 'idle', 'Check for a new version, then download and install it here.'
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
        self._download_poll = QtCore.QTimer(self)
        self._download_poll.setInterval(100)
        self._download_poll.timeout.connect(self._poll_download)
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
    dialogVisible = QtCore.Property(bool, lambda self: self._dialog_visible, notify=changed)
    downloadState = QtCore.Property(str, lambda self: self._download_state, notify=changed)
    downloadMessage = QtCore.Property(str, lambda self: self._download_message, notify=changed)
    downloadedBytes = QtCore.Property(float, lambda self: float(self._received_bytes), notify=changed)
    totalBytes = QtCore.Property(float, lambda self: float(self._total_bytes), notify=changed)
    downloadProgress = QtCore.Property(float, lambda self: self._received_bytes / self._total_bytes if self._total_bytes else 0., notify=changed)
    canDownload = QtCore.Property(bool, lambda self: bool(not self._closed and self._eligible() and self._available
        and self._installer and not self._checking and self._download_state in ('idle', 'error')), notify=changed)
    canInstall = QtCore.Property(bool, lambda self: self._can_install(), notify=changed)
    installPending = QtCore.Property(bool, lambda self: self._install_pending, notify=changed)

    def setInstallGuard(self, callback):
        self._install_guard = callback

    @QtCore.Slot()
    def refreshInstallState(self):
        if not self._closed:
            self.changed.emit()

    def _can_install(self):
        if self._closed or not self._eligible() or self._download_state != 'ready' or not self._installer_path:
            return False
        try:
            return bool(self._install_guard())
        except Exception:
            return False

    @QtCore.Slot()
    def openDialog(self):
        if not self._closed:
            self._dialog_visible = True
            self.changed.emit()

    @QtCore.Slot()
    def hideDialog(self):
        self._dialog_visible = False
        self.changed.emit()

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
        self.openDialog()
        self._begin_check(manual=True)

    def _begin_check(self, *, manual):
        if self._closed or self._checking or self._download_state in ('downloading', 'cancelling', 'ready', 'installing'):
            return
        if manual:
            self._download_state, self._download_message = 'idle', ''
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
        self._installer = None
        details = release_details(payload, self._current) if not error else None
        if error or details is None:
            self._status = 'error'
            self._message = error or 'GitHub did not return a supported stable release with a Windows installer.'
        else:
            self._latest, self._url = details['version'], details['url']
            self._installer = details['installer']
            self._available = details['newer'] and parse_version(self._ignored) != parse_version(self._latest)
            self._status = 'available' if self._available else ('ignored' if details['newer'] else 'current')
            self._message = (f'Transport {self._latest} is available. Download it here, then exit and install when idle.'
                             if self._available else ('This release is ignored.' if details['newer'] else 'You have the latest stable release.'))
            if self._available and self._installer is None:
                self._message = 'This release has no verified installer metadata. Open release notes for the manual download.'
        self.changed.emit()

    @QtCore.Slot()
    def downloadUpdate(self):
        if not self.canDownload:
            return
        self._download_details = dict(self._installer)
        self._download_results = queue.Queue()
        self._download_cancel = threading.Event()
        self._received_bytes, self._total_bytes = 0, self._download_details['size']
        self._download_state, self._download_message = 'downloading', 'Downloading and verifying the Windows installer…'
        self._dialog_visible = True
        cache = QtCore.QStandardPaths.writableLocation(QtCore.QStandardPaths.StandardLocation.GenericCacheLocation)
        if not cache:
            self._download_state, self._download_message = 'error', 'Windows could not provide an update cache directory.'
            self.changed.emit()
            return
        thread = threading.Thread(target=download_installer, kwargs={**self._download_details,
            'cache_root': cache, 'events': self._download_results, 'cancel_event': self._download_cancel},
            daemon=True, name='transport-update-download')
        self._download_poll.start()
        try:
            thread.start()
        except RuntimeError:
            self._download_poll.stop()
            self._download_state, self._download_message = 'error', 'Could not start the download. Retry the download.'
        self.changed.emit()

    @QtCore.Slot()
    def cancelDownload(self):
        if self._download_state == 'downloading':
            self._download_cancel.set()
            self._download_state, self._download_message = 'cancelling', 'Cancelling download…'
            self.changed.emit()

    def _poll_download(self):
        if self._closed:
            return
        dirty = False
        while True:
            try:
                event = self._download_results.get_nowait()
            except queue.Empty:
                break
            dirty = True
            kind = event['type']
            if kind == 'progress':
                self._received_bytes = event['received']
            else:
                self._download_poll.stop()
                if kind == 'complete' and not self._download_cancel.is_set():
                    self._installer_path = event['path']
                    self._download_state, self._download_message = 'ready', 'Download verified. Exit Transport and open the installer when idle.'
                    self._received_bytes = self._total_bytes
                elif kind == 'cancelled' or self._download_cancel.is_set():
                    if kind == 'complete':
                        Path(event['path']).unlink(missing_ok=True)
                    self._download_state, self._download_message = 'idle', 'Download cancelled. You can retry at any time.'
                else:
                    self._download_state, self._download_message = 'error', event.get('message', 'Download failed. Retry the download.')
        if dirty:
            self.changed.emit()

    @QtCore.Slot()
    def installUpdate(self):
        if not self._can_install():
            if self._download_state == 'ready':
                self._download_message = 'Wait for measurements, cleanup and instrument operations to finish before installing.'
                self.changed.emit()
            return
        self._install_pending = True
        self._download_state, self._download_message = 'installing', 'Closing Transport normally before starting the installer…'
        self.changed.emit()
        self.installRequested.emit()

    def cancelInstall(self, message=''):
        if self._install_pending and not self._install_started:
            self._install_pending = False
            self._download_state = 'ready'
            self._download_message = message or 'Transport could not close. Finish cleanup, then retry installation.'
            self._dialog_visible = True
            self.changed.emit()

    def finishShutdown(self):
        """Arm the installer only after the application confirms normal cleanup."""
        if not self._install_pending or self._install_started:
            return True
        try:
            if not self._eligible() or not self._download_details:
                raise RuntimeError('Installation is unavailable in this session.')
            launch_after_exit(self._installer_path, self._download_details['sha256'], Path(sys.executable).parent)
        except Exception:
            self.cancelInstall('Could not start the update installer. Retry Exit and install, or close Transport and install manually.')
            return False
        self._install_started = True
        return True

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
        self._download_cancel.set()
        self._download_poll.stop()
        # A pending daemon request retains only its result queue. Closing the
        # app never waits for DNS/HTTP and no worker can call a destroyed QObject.
        self._checking = False
