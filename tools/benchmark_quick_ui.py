r"""Measure the current full Quick UI using isolated settings and synthetic data.

Run .venv\Scripts\python.exe -B tools\benchmark_quick_ui.py --scale 1.25
Event-drain timings measure synchronous GUI work, not end-to-end display latency.
Frame timings separately wait for an actual frameSwapped signal, with a timeout.
No acquisition, discovery, connections, or hardware commands are requested.
"""
from pathlib import Path
import argparse
import json
import os
import platform
import statistics
import sys
import time
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def distribution(samples):
    ordered = sorted(samples)
    return dict(samples_ms=[round(v, 3) for v in samples],
                median_ms=round(statistics.median(samples), 3),
                p95_ms=round(ordered[min(len(ordered) - 1, int(.95 * len(ordered)))], 3),
                max_ms=round(max(samples), 3))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--iterations", type=int, default=30)
    parser.add_argument("--width", type=int, default=1920, help="Logical window width")
    parser.add_argument("--height", type=int, default=1080, help="Logical window height")
    parser.add_argument("--scale", type=float, default=1., help="Qt scale factor, e.g. 1 or 1.25")
    parser.add_argument("--output", type=Path, default=ROOT / "tmp/verification/quick_ui",
                        help="Directory for benchmark.json and a final screenshot")
    args = parser.parse_args(argv)
    if args.iterations < 1 or args.width < 800 or args.height < 600 or not 0 < args.scale <= 4:
        parser.error("Use iterations >= 1, width >= 800, height >= 600 and 0 < scale <= 4")
    os.environ["QT_SCALE_FACTOR"] = str(args.scale)
    # Install isolation before importing controllers, services, or user config.
    from app.offline_sandbox import OfflineSandbox, configure_offscreen_font
    sandbox = OfflineSandbox(ROOT, preview=True)
    sandbox.install_hardware_guards()

    import PySide6
    from PySide6.QtCore import QEvent, QEventLoop, QObject, QTimer, QUrl, qVersion
    from PySide6.QtGui import QGuiApplication
    from PySide6.QtQml import QQmlApplicationEngine
    from PySide6.QtQuickControls2 import QQuickStyle
    from app.quick.application import ApplicationBackend
    from app.quick.plot import register_types

    QQuickStyle.setStyle("Fusion")
    app = QGuiApplication.instance() or QGuiApplication([])
    app.setQuitOnLastWindowClosed(False)
    configure_offscreen_font(app)
    register_types()
    warnings, exceptions = [], []
    original_hook = sys.excepthook
    sys.excepthook = lambda kind, exc, tb: exceptions.append(f"{kind.__name__}: {exc}")
    started = time.perf_counter()
    backend = ApplicationBackend(offline=True)
    backend_ms = (time.perf_counter() - started) * 1000
    engine = QQmlApplicationEngine()
    engine.warnings.connect(lambda items: warnings.extend(str(i) for i in items))
    engine.rootContext().setContextProperty("appBackend", backend)
    report = {}
    window = None

    def frame():
        """Observe a rendered frame rather than assuming a fixed sleep settled it."""
        loop = QEventLoop()
        timeout = QTimer()
        timeout.setSingleShot(True)
        observed = []
        def swapped():
            observed.append(True)
            loop.quit()
        window.frameSwapped.connect(swapped)
        timeout.timeout.connect(loop.quit)
        started = time.perf_counter()
        timeout.start(2000)
        QTimer.singleShot(0, window.update)
        loop.exec()
        elapsed = (time.perf_counter() - started) * 1000
        timeout.stop()
        window.frameSwapped.disconnect(swapped)
        return dict(elapsed_ms=round(elapsed, 3), observed_frame=bool(observed))

    def visual(name):
        def descendants(item):
            yield item
            for child in item.childItems():
                yield from descendants(child)
        return next(i for i in descendants(window.contentItem()) if i.objectName() == name)

    def burst(action):
        samples = []
        for i in range(args.iterations):
            started = time.perf_counter()
            action(i)
            app.processEvents()
            samples.append((time.perf_counter() - started) * 1000)
        return distribution(samples)

    try:
        started = time.perf_counter()
        engine.load(QUrl.fromLocalFile(str(ROOT / "app/quick/qml/Main.qml")))
        qml_load_ms = (time.perf_counter() - started) * 1000
        if not engine.rootObjects():
            raise RuntimeError("Main.qml failed to load: " + "\n".join(warnings))
        window = engine.rootObjects()[0]
        window.setWidth(args.width)
        window.setHeight(args.height)
        first_frame = frame()
        qml_to_frame_ms = (time.perf_counter() - started) * 1000
        modes = []
        # Start at mode 1 so all six samples perform an actual mode switch.
        measurement = backend._measurement
        measurement.mode = 1
        frame()
        for mode in (0, 1, 2, 3, 4, 5):
            started = time.perf_counter()
            measurement.mode = mode
            app.processEvents()
            drained_ms = (time.perf_counter() - started) * 1000
            rendered = frame()
            modes.append(dict(mode=mode, title=measurement.modes[mode]["title"],
                              event_drain_ms=round(drained_ms, 3), frame=rendered,
                              switch_to_frame_ms=round((time.perf_counter() - started) * 1000, 3)))

        measurement.mode = 2
        for key, value in dict(axis_slow="Vbg", vtg_start=0., vtg_stop=9.9, vtg_step=.1,
                               vbg_start=0., vbg_stop=9.9, vbg_step=.1).items():
            measurement.setValue(key, value)
        visual("measurementReviewToggle").setProperty("checked", False)
        frame()
        field = visual("field_vtg_stop")
        field.forceActiveFocus()
        draft = "9.87654321"
        accepted = measurement.collect_params().vtg_stop
        field.setProperty("text", draft)
        edits = burst(lambda i: measurement.setValue("n_sample", i + 1))
        edits["post_burst_frame"] = frame()
        edits["map_points"] = 10000
        edits["review_expanded"] = False
        edits["checks"] = dict(same_editor=visual("field_vtg_stop") == field,
                               draft_preserved=field.property("text") == draft,
                               active_focus_preserved=field.hasActiveFocus(),
                               draft_not_committed=measurement.collect_params().vtg_stop == accepted)

        window.setProperty("showInstruments", True)
        pane = window.findChild(QObject, "instrumentPane")
        pane.setProperty("section", 2)
        frame()
        target = visual("field_target_field")
        target.forceActiveFocus()
        target.setProperty("text", "0.123456")
        target_accepted = backend._magnet._values["target_field"]
        def telemetry(i):
            backend._magnet._snapshot("1000", SimpleNamespace(field_t=i * .001,
                output_field_t=i * .001, heater_on=False, monotonic_s=time.monotonic(),
                sweep_state="idle", synthetic=True))
            backend._instruments.changed.emit()
            backend._lockin.changed.emit()
        updates = burst(telemetry)
        updates["signals_per_iteration"] = 3
        updates["post_burst_frame"] = frame()
        updates["checks"] = dict(same_editor=visual("field_target_field") == target,
                                 draft_preserved=target.property("text") == "0.123456",
                                 active_focus_preserved=target.hasActiveFocus(),
                                 draft_not_committed=backend._magnet._values["target_field"] == target_accepted)

        args.output.mkdir(parents=True, exist_ok=True)
        screenshot = window.grabWindow()
        image_ok = not screenshot.isNull() and screenshot.save(str(args.output / "quick_ui.png"))
        report = dict(context=dict(python=sys.version.split()[0], qt=qVersion(), pyside=PySide6.__version__,
            os=platform.platform(), qt_platform=app.platformName(), requested_scale=args.scale,
            device_pixel_ratio=window.devicePixelRatio(), logical_width=window.width(),
            logical_height=window.height(), image_pixel_width=screenshot.width(),
            image_pixel_height=screenshot.height(), iterations=args.iterations, hardware="blocked",
            settings="temporary", render_loop=os.environ.get("QSG_RENDER_LOOP", "Qt default")),
            timing_definitions=dict(event_drain="setter/synthetic signals plus one processEvents call; excludes display refresh wait",
                                    frame="wait for next frameSwapped, including Qt scheduling; 2000ms timeout"),
            backend_construction_ms=round(backend_ms, 3), qml_load_ms=round(qml_load_ms, 3),
            qml_load_resize_to_first_frame_ms=round(qml_to_frame_ms, 3), first_frame=first_frame,
            mode_switches=modes, map_parameter_edits=edits, synthetic_telemetry=updates,
            screenshot_saved=image_ok, qml_warnings=warnings, python_exceptions=exceptions)
        frames = [first_frame, edits["post_burst_frame"], updates["post_burst_frame"]] + [m["frame"] for m in modes]
        report["success"] = (not warnings and not exceptions and image_ok and
            all(f["observed_frame"] for f in frames) and all(edits["checks"].values()) and
            all(updates["checks"].values()))
    finally:
        # Only this benchmark's guarded, offline backend is shut down.
        if window is not None:
            window.hide()
        backend.requestClose()
        shutdown_deadline = time.monotonic() + 5.
        while not backend.closeAccepted and time.monotonic() < shutdown_deadline:
            app.processEvents()
        if not backend.closeAccepted:
            for owner in (backend._magnet.magnet1000, backend._magnet.lakeshore335, backend._magnet.magnet2100):
                owner.shutdown()
        if report:
            report["shutdown_confirmed"] = backend.closeAccepted
            report["success"] = report["success"] and backend.closeAccepted
        engine.deleteLater()
        app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        backend.deleteLater()
        app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        sys.excepthook = original_hook
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / "benchmark.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))
    return 0 if report["success"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
