"""Window icons remain usable across launchers, missing assets and viewer processes."""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

if os.environ.get("TRANSPORT_OFFLINE_VERIFICATION") != "1":
    raise unittest.SkipTest("Run through verify_offline.py")

from PySide6 import QtCore, QtGui, QtWidgets
from app import app_identity


class AppIdentityTests(unittest.TestCase):
    def setUp(self):
        self.application = QtWidgets.QApplication.instance()
        self.original_icon = self.application.windowIcon()
        self.addCleanup(self.application.setWindowIcon, self.original_icon)

    def test_source_relaunch_quotes_unicode_root_and_retains_widgets(self):
        root = Path(r"D:\测试 folder\Transport")
        command = app_identity.source_relaunch(root, "widgets")
        self.assertIn(f'"{root / "tools" / "Start-Transport.ps1"}"', command)
        self.assertTrue(command.endswith(" -Frontend widgets"))
        self.assertEqual(app_identity.APP_ID, "MyLab.TransportMeasurement")

    def test_process_identity_distinguishes_source_and_frozen(self):
        from app.source_runtime import ROOT, root_hash
        with patch.object(sys, "frozen", False, create=True):
            self.assertEqual(app_identity.process_app_id(), app_identity.APP_ID + ".Source." + root_hash(ROOT))
        with patch.object(sys, "frozen", True, create=True):
            self.assertEqual(app_identity.process_app_id(), app_identity.APP_ID)

    @unittest.skipUnless(sys.platform == "win32", "Windows shell properties")
    def test_source_window_shell_properties_accept_unicode_paths(self):
        import ctypes
        from ctypes import wintypes
        user = ctypes.WinDLL("user32", use_last_error=True)
        user.CreateWindowExW.argtypes = [wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
                                        ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                        wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, ctypes.c_void_p]
        user.CreateWindowExW.restype = wintypes.HWND
        user.DestroyWindow.argtypes = [wintypes.HWND]
        hwnd = user.CreateWindowExW(0, "STATIC", "test", 0, 0, 0, 1, 1, None, None, None, None)
        self.assertTrue(hwnd)
        try:
            app_identity.set_source_window_identity(hwnd, Path(r"D:\测试 folder\Transport"), "0123456789abcdef", "widgets")
        finally:
            user.DestroyWindow(hwnd)

    def test_packaged_icon_has_small_taskbar_and_high_dpi_images(self):
        path = Path(__file__).resolve().parents[1] / "assets" / "transport.ico"
        self.assertTrue(path.is_file(), "Windows shortcuts need a persistent icon file")
        icon = QtGui.QIcon(str(path))
        sizes = {size.width() for size in icon.availableSizes()}
        self.assertTrue({16, 32, 48, 256}.issubset(sizes), sizes)
        for size in (16, 32, 48, 256):
            self.assertFalse(icon.pixmap(size, size).isNull())

    def test_corrupt_asset_falls_back_to_a_renderable_window_icon(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "assets").mkdir()
            (root / "assets" / "transport.ico").write_bytes(b"invalid icon")
            with patch.object(app_identity, "__file__", str(root / "app" / "app_identity.py")):
                app_identity.configure_qapp(self.application)
            self.assertFalse(self.application.windowIcon().pixmap(32, 32).isNull())

    def test_missing_asset_fallback_includes_high_dpi_sizes(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(app_identity, "__file__", str(Path(directory) / "app" / "app_identity.py")):
                app_identity.configure_qapp(self.application)
            self.assertEqual(self.application.windowIcon().pixmap(256, 256).size(), QtCore.QSize(256, 256))

    def test_history_entry_point_sets_icon_and_process_identity(self):
        # Use a fresh QApplication/process, as the main app's global icon can
        # otherwise conceal missing initialization in the standalone viewer.
        probe = r'''
import ctypes, json, runpy, sys
from PySide6 import QtWidgets
class ProbeApplication(QtWidgets.QApplication):
    def exec(self):
        windows = [w for w in self.topLevelWidgets() if w.isVisible()]
        result = {"probe": True, "icon": not self.windowIcon().pixmap(32, 32).isNull(),
                  "window_icons": bool(windows) and all(not w.windowIcon().pixmap(32, 32).isNull() for w in windows),
                  "name": self.applicationName()}
        if sys.platform == "win32":
            value = ctypes.c_void_p()
            shell = ctypes.windll.shell32
            shell.GetCurrentProcessExplicitAppUserModelID.argtypes = [ctypes.POINTER(ctypes.c_void_p)]
            shell.GetCurrentProcessExplicitAppUserModelID(ctypes.byref(value))
            result["app_id"] = ctypes.wstring_at(value.value) if value.value else None
            if value.value:
                ctypes.windll.ole32.CoTaskMemFree.argtypes = [ctypes.c_void_p]
                ctypes.windll.ole32.CoTaskMemFree(value)
        print(json.dumps(result), flush=True)
        for window in windows:
            window.close()
        return 0
QtWidgets.QApplication = ProbeApplication
sys.argv = sys.argv[1:]
runpy.run_path(sys.argv[0], run_name="__main__")
'''
        with tempfile.TemporaryDirectory() as directory:
            process = QtCore.QProcess()
            root = Path(__file__).resolve().parents[1]
            process.setWorkingDirectory(str(root))
            process.start(sys.executable, ["-c", probe, str(root / "transport_history_viewer.py"), "--folder", directory])
            try:
                self.assertTrue(process.waitForFinished(15000), "History icon probe timed out")
                output = bytes(process.readAllStandardOutput()).decode()
                errors = bytes(process.readAllStandardError()).decode()
                self.assertEqual(process.exitCode(), 0, errors)
                messages = [json.loads(line) for line in output.splitlines()]
                result = next(message for message in messages if message.get("probe"))
                self.assertTrue(result["icon"], result)
                self.assertTrue(result["window_icons"], result)
                self.assertEqual(result["name"], "Transport History")
                if sys.platform == "win32":
                    self.assertEqual(result["app_id"], app_identity.process_app_id())
                ready = next(message for message in messages if message.get("event") == "ready")
                self.assertEqual(ready["hardware_modules"], [])
            finally:
                if process.state() != QtCore.QProcess.ProcessState.NotRunning:
                    process.kill()
                    process.waitForFinished(3000)
