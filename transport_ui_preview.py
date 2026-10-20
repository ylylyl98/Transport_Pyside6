"""Independent, hardware-blocked preview of the Transport interface.

Use tools/Transport_Preview.bat, or pass --offscreen --screenshot <path> for a render.
Ctrl+Shift+R replaces only this preview process and loads changed Python code.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
import tempfile

ROOT = Path(__file__).resolve().parent
REBUILD_EXIT_CODE = 75


def _arguments(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tab", type=int, help="Initial measurement tab, numbered from zero")
    parser.add_argument("--offscreen", action="store_true", help="Render without showing a desktop window")
    parser.add_argument("--screenshot", type=Path, help="Save one preview image and exit")
    parser.add_argument("--settings", action="store_true", help="Show or capture the runtime-settings dialog")
    parser.add_argument("--child", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--state-directory", type=Path, help=argparse.SUPPRESS)
    return parser.parse_args(argv)


def _load_view_state(directory):
    try:
        value = json.loads((directory / "preview_view.json").read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def _run_child(args):
    from app.offline_sandbox import OfflineSandbox, configure_offscreen_font
    sandbox = OfflineSandbox(ROOT, preview=True, offscreen=args.offscreen or bool(args.screenshot),
                             state_directory=args.state_directory)
    sandbox.install_hardware_guards()
    from PySide6 import QtCore, QtGui, QtWidgets
    from unittest.mock import patch
    from app.app_identity import configure_qapp
    from app.ui.main_window import MainWindow

    application = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    configure_qapp(application, app_name="Transport UI Preview — offline")
    configure_offscreen_font(application)

    def preview_history(launcher):
        launcher.status_label.setText("History processes are disabled in the UI preview.")

    with patch("app.ui.dock.ConnDock._start_scan"), patch(
            "app.ui.history_viewer_launcher.HistoryViewerLauncher.open_viewer", preview_history):
        window = MainWindow()
    window.setWindowTitle("Transport UI Preview — OFFLINE / synthetic data")
    window.tab_curve_compare.open_viewer = lambda: preview_history(window.tab_curve_compare)
    window.tab_curve_compare.auto_png_check.setChecked(False)
    window.tab_curve_compare.open_button.setEnabled(False)
    window.tab_curve_compare.reload_button.setEnabled(False)
    for tab in window._measurement_tabs():
        tab.run_panel.set_start_blocked("Offline preview: acquisition disabled", True)
        tab.run_panel.btn_start.setToolTip("Acquisition is disabled in the offline UI preview")
        # Plots use synthetic data only. No fake connected state is handed to
        # measurement controllers or instrument adapters.
        x = [index / 20 for index in range(-20, 21)]
        tab.plot.ax.plot(x, [value * 1e-9 for value in x], "o-", markersize=3, label="Synthetic preview")
        tab.plot.ax.legend()
        tab.plot.canvas.draw_idle()
    menu = window.workspace_menu.addMenu("Preview")
    rebuild = QtGui.QAction("Rebuild preview from disk", window)
    rebuild.setShortcut(QtGui.QKeySequence("Ctrl+Shift+R"))
    rebuild.setToolTip("Restart only this isolated preview; Transport measurements stay open")
    rebuild.triggered.connect(lambda: application.exit(REBUILD_EXIT_CODE))
    menu.addAction(rebuild)
    window.statusBar().showMessage("OFFLINE PREVIEW · synthetic plots · Ctrl+Shift+R rebuilds this preview only")
    state = _load_view_state(sandbox.directory)
    if isinstance(state.get("geometry"), str):
        window.restoreGeometry(QtCore.QByteArray.fromBase64(state["geometry"].encode("ascii")))
    selected = args.tab if args.tab is not None else state.get("tab", 0)
    if isinstance(selected, int) and 0 <= selected < window.tabs.count():
        window.tabs.setCurrentIndex(selected)
    page = state.get("instrument_page", 0)
    if isinstance(page, int) and 0 <= page < window.instrument_workspace.pages.count():
        window.instrument_workspace.pages.setCurrentIndex(page)
    window.show()
    if args.settings:
        window._show_runtime_settings()

    render_error = []
    if args.screenshot:
        def capture():
            try:
                args.screenshot.parent.mkdir(parents=True, exist_ok=True)
                target = window._runtime_settings_dialog if args.settings else window
                if not target.grab().save(str(args.screenshot)):
                    raise OSError("Could not save preview screenshot")
            except OSError as exc:
                render_error.append(str(exc))
            application.exit(1 if render_error else 0)
        QtCore.QTimer.singleShot(500, capture)
    try:
        result = application.exec()
    finally:
        (sandbox.directory / "preview_view.json").write_text(json.dumps({
            "tab": window.tabs.currentIndex(),
            "instrument_page": window.instrument_workspace.pages.currentIndex(),
            "geometry": bytes(window.saveGeometry().toBase64()).decode("ascii"),
        }), encoding="utf-8")
        # The preview cannot own a live instrument or an acquisition. Explicit
        # teardown avoids waiting for hardware-only shutdown acknowledgements.
        window.magnet1000.shutdown()
        window.lakeshore335.shutdown()
        window.magnet2100.shutdown()
        window.device_manager.shutdown()
        window.png_coordinator.shutdown()
        window.png_exporter.shutdown()
        window.hide()
        window.deleteLater()
        application.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)
        application.processEvents()
    if render_error:
        print(render_error[0], file=sys.stderr)
    return result


def main(argv=None):
    args = _arguments(argv)
    os.chdir(ROOT)
    if args.child:
        if args.state_directory is None:
            raise SystemExit("The preview child requires an isolated state directory")
        return _run_child(args)
    # Keep the supervisor free of Qt and application imports. It has no device
    # owners and can replace a preview child even after Python source changes.
    import subprocess
    with tempfile.TemporaryDirectory(prefix="transport-ui-preview-") as directory:
        command = [sys.executable, "-B", str(Path(__file__).resolve()), "--child", "--state-directory", directory]
        if args.tab is not None:
            command.extend(["--tab", str(args.tab)])
        if args.offscreen:
            command.append("--offscreen")
        if args.screenshot:
            command.extend(["--screenshot", str(args.screenshot.resolve())])
        if args.settings:
            command.append("--settings")
        while True:
            result = subprocess.call(command, cwd=str(ROOT))
            if result != REBUILD_EXIT_CODE:
                return result
            # Only the first launch uses --tab; later rebuilds restore the page
            # the user was viewing when they requested the rebuild.
            if "--tab" in command:
                offset = command.index("--tab")
                del command[offset:offset + 2]


if __name__ == "__main__":
    raise SystemExit(main())
