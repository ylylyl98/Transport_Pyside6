"""Run offline checks without touching instrument sessions or user settings."""
from pathlib import Path
import os
import sys
import unittest
import json
import threading
import faulthandler

ROOT = Path(__file__).resolve().parent
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
from app.offline_sandbox import OfflineSandbox, configure_offscreen_font
sandbox = OfflineSandbox(ROOT)
scratch = sandbox.scratch
sandbox.install_hardware_guards()
from PySide6 import QtWidgets
# Keep a QApplication alive across all test classes and check only this copy.
application = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
configure_offscreen_font(application)
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
    report_dir = ROOT / "tmp" / "verification" / "reports"
    report_dir.mkdir(parents=True, exist_ok=True)
    report_path = report_dir / ("offline_results_" + (sys.argv[1].replace(".", "_") if len(sys.argv) > 1 else "all") + ".json")
    report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"Offline verification report: {report_path}", flush=True)
    watchdog.cancel()
    faulthandler.cancel_dump_traceback_later()
    sys.exit(0 if result.wasSuccessful() else 1)
