"""Run offline checks without touching instrument sessions or user settings."""
from pathlib import Path
import os
import sys
import tempfile
import unittest
import json
import threading
import faulthandler
import shlex

ROOT = Path(__file__).resolve().parent
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
sys.dont_write_bytecode = True
scratch = tempfile.TemporaryDirectory(prefix="transport-pyside6-check-")
os.environ["QT_QPA_PLATFORM"] = "offscreen"
os.environ["QT_API"] = "pyside6"
os.environ["TRANSPORT_OFFLINE_VERIFICATION"] = "1"
os.environ["MPLCONFIGDIR"] = str(Path(scratch.name) / "matplotlib")
os.environ["SPECTRALSWEEP_CONFIG_PATH"] = str(Path(scratch.name) / "config.json")
os.environ["APPDATA"] = scratch.name
os.environ["LOCALAPPDATA"] = scratch.name

def audit(event, args):
    if event == "subprocess.Popen":
        executable, command, cwd, _ = args
        # Permit only the detached, hardware-free renderer exercised by the
        # process lifecycle tests. Keep arbitrary subprocesses blocked.
        argv = ([part.strip('"') for part in shlex.split(command, posix=False)]
                if isinstance(command, str) else command)
        interpreters = (Path(sys.executable), Path(sys.executable).with_name("pythonw.exe"))
        if (executable and Path(executable) in interpreters and cwd and Path(cwd) == ROOT
                and len(argv) in (6, 8) and argv[0] == executable
                and Path(argv[1]) == ROOT / "transport_png_exporter.py"
                and argv[2] == "--spool" and Path(argv[3]).parent == Path(tempfile.gettempdir())
                and Path(argv[3]).name.startswith("transport-png-")
                and argv[4] == "--owner-pid" and argv[5] == str(os.getpid())
                and (len(argv) == 6 or (argv[6] == "--after-pid" and argv[7].isdigit()))):
            return
    if event in ("socket.connect", "socket.bind", "socket.sendto", "socket.sendmsg",
                 "socket.getaddrinfo", "socket.gethostbyname", "socket.gethostbyaddr",
                 "subprocess.Popen", "os.system"):
        raise RuntimeError("Offline verification blocks " + event)

sys.addaudithook(audit)
from PySide6 import QtCore, QtGui, QtWidgets
for scope in (QtCore.QSettings.Scope.UserScope, QtCore.QSettings.Scope.SystemScope):
    QtCore.QSettings.setPath(QtCore.QSettings.Format.IniFormat, scope, scratch.name)

def blocked(*args, **kwargs):
    raise RuntimeError("Real hardware access is disabled during offline verification")

import pyvisa
import nidaqmx
import nidaqmx.system
pyvisa.ResourceManager = blocked
nidaqmx.Task = blocked
nidaqmx.system.System.local = blocked

import app.hw_discovery
app.hw_discovery.scan_all = lambda: {"gpib": [], "asrl": [], "daq": []}
from utils.config import cfg
cfg.filename.base_out = scratch.name

# Keep a QApplication alive across all test classes and check only this copy.
application = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
# The offscreen platform may not discover Windows system fonts automatically.
font_file = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts" / "segoeui.ttf"
if font_file.exists():
    QtGui.QFontDatabase.addApplicationFont(str(font_file))
    application.setFont(QtGui.QFont("Segoe UI", 9))
assert Path(app.hw_discovery.__file__).is_relative_to(ROOT)
faulthandler.enable()
faulthandler.dump_traceback_later(90)
watchdog = threading.Timer(150, lambda: os._exit(124))
watchdog.daemon = True
watchdog.start()

if __name__ == "__main__":
    suite = (unittest.defaultTestLoader.loadTestsFromNames(sys.argv[1:])
             if len(sys.argv) > 1 else unittest.TestSuite([
                 unittest.defaultTestLoader.discover("tests"),
                 unittest.defaultTestLoader.loadTestsFromName("spectra_app.test_attodry2100_controller"),
             ]))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    report = {"tests": result.testsRun, "failures": len(result.failures),
              "errors": len(result.errors), "skipped": len(result.skipped),
              "qt_binding": "PySide6", "hardware": "blocked",
              "settings": "temporary", "success": result.wasSuccessful()}
    (ROOT / ("offline_results_" + (sys.argv[1].replace(".", "_") if len(sys.argv) > 1 else "all") + ".json")).write_text(json.dumps(report, indent=2), encoding="utf-8")
    watchdog.cancel()
    faulthandler.cancel_dump_traceback_later()
    sys.exit(0 if result.wasSuccessful() else 1)
