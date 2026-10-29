r"""Benchmark Measurement History with synthetic CSVs and blocked hardware.

Run .venv\Scripts\python.exe -B tools\benchmark_quick_history.py --scale 1.25
Worker latency, GUI event-drain costs and timer gaps are reported separately.
Timings are diagnostic measurements, with no machine-dependent pass threshold.
"""
from pathlib import Path
import argparse
import json
import os
import platform
import statistics
import sys
import tempfile
import time
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def distribution(samples):
    ordered = sorted(samples)
    return dict(count=len(samples), median_ms=round(statistics.median(samples), 3),
                p95_ms=round(ordered[min(len(ordered)-1, int(.95*len(ordered)))], 3),
                max_ms=round(max(samples), 3)) if samples else dict(count=0)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--files", type=int, default=1000)
    parser.add_argument("--points", type=int, default=100000)
    parser.add_argument("--scale", type=float, default=1.25)
    parser.add_argument("--output", type=Path, default=ROOT / "tmp/verification/quick_history")
    args = parser.parse_args(argv)
    if args.files < 2 or args.points < 2 or not 0 < args.scale <= 4:
        parser.error("Use files >= 2, points >= 2 and 0 < scale <= 4")
    os.environ["QT_SCALE_FACTOR"] = str(args.scale)
    # Install isolation before importing application services or user settings.
    from app.offline_sandbox import OfflineSandbox, configure_offscreen_font
    sandbox = OfflineSandbox(ROOT, preview=True)
    sandbox.install_hardware_guards()

    import PySide6
    from PySide6.QtCore import QEvent, QEventLoop, QPoint, QSettings, Qt, QTimer, QUrl, qVersion
    from PySide6.QtGui import QGuiApplication
    from PySide6.QtQml import QQmlApplicationEngine, QQmlProperty
    from PySide6.QtQuickControls2 import QQuickStyle
    from PySide6.QtTest import QTest
    from app.quick import history
    from app.quick.plot import register_types

    QQuickStyle.setStyle("Fusion")
    app = QGuiApplication.instance() or QGuiApplication([])
    app.setQuitOnLastWindowClosed(False)
    configure_offscreen_font(app)
    register_types()
    warnings, exceptions = [], []
    previous_hook = sys.excepthook
    sys.excepthook = lambda kind, exc, tb: exceptions.append(f"{kind.__name__}: {exc}")
    args.output.mkdir(parents=True, exist_ok=True)
    stages, worker_samples = {}, []
    stage = "setup"
    counts = dict(index=0, read_display=0, publish=0)
    heartbeat_times = []
    heartbeat = QTimer()
    heartbeat.setTimerType(Qt.TimerType.PreciseTimer)
    heartbeat.setInterval(5)
    heartbeat.timeout.connect(lambda: heartbeat_times.append(time.perf_counter()))
    heartbeat.start()
    window = backend = engine = None
    report = {}

    def timed(name, function):
        def wrapped(*values, **kwargs):
            label = stage
            counts[name] += 1
            started = time.perf_counter()
            try:
                return function(*values, **kwargs)
            finally:
                worker_samples.append(dict(stage=label, operation=name,
                    elapsed_ms=round((time.perf_counter()-started)*1000, 3)))
        return wrapped

    def scenario(name, action, wait_idle=False, settle=.08):
        nonlocal stage
        stage = name
        start_counts = counts.copy()
        started = time.perf_counter()
        beat_start = len(heartbeat_times)
        action_started = time.perf_counter()
        action()
        action_ms = (time.perf_counter()-action_started)*1000
        drains = []
        deadline = time.monotonic()+60
        idle_since = None
        while time.monotonic() < deadline:
            drain_started = time.perf_counter()
            app.processEvents(QEventLoop.ProcessEventsFlag.AllEvents, 10)
            drains.append((time.perf_counter()-drain_started)*1000)
            if not wait_idle or not (backend.busy or backend._index_active):
                idle_since = idle_since or time.monotonic()
                if time.monotonic()-idle_since >= settle:
                    break
            else:
                idle_since = None
            time.sleep(.001)
        else:
            raise TimeoutError(f"History scenario did not settle: {name}")
        beats = [started, *heartbeat_times[beat_start:], time.perf_counter()]
        result = dict(elapsed_ms=round((time.perf_counter()-started)*1000, 3),
            requested_settle_ms=round(settle*1000, 3),
            synchronous_action_ms=round(action_ms, 3), gui_event_drain=distribution(drains),
            gui_timer_gap=distribution([(b-a)*1000 for a,b in zip(beats, beats[1:])]),
            jobs={key: counts[key]-start_counts[key] for key in counts})
        stages[name] = result
        return result

    def visual(name):
        def walk(item):
            yield item
            for child in item.childItems():
                yield from walk(child)
        return next(item for item in walk(window.contentItem()) if item.objectName() == name)

    try:
        with tempfile.TemporaryDirectory(prefix="transport-history-benchmark-") as scratch:
            folder = Path(scratch)
            metadata = json.dumps(dict(measurement="vds_sweep", created_at="2026-10-26T12:00:00",
                status="running", params=dict(vds_start=0, vds_stop=1, vtg=0, vbg=0),
                context={f"synthetic_{i}": list(range(100)) for i in range(15)}))
            for index in range(args.files):
                path = folder / f"vds_sweep_{index:05d}.csv"
                path.write_text("Vds,Ids_DC,Direction\nV,A,\n0,0,forward\n1,1e-9,forward\n", encoding="utf-8")
                path.with_name(path.stem+"_metadata.json").write_text(metadata, encoding="utf-8")
            selected = folder / "vds_sweep_00000.csv"
            with selected.open("w", encoding="utf-8", newline="") as stream:
                stream.write("Vds,Ids_DC,Direction\nV,A,\n")
                for index in range(args.points):
                    x = index / (args.points-1)
                    stream.write(f"{x:.12g},{x*1e-9:.12g},forward\n")
            backend = history.HistoryBackend(folder, autostart=False,
                settings=QSettings(str(folder / "settings.ini"), QSettings.Format.IniFormat))
            engine = QQmlApplicationEngine()
            engine.warnings.connect(lambda items: warnings.extend(str(item) for item in items))
            engine.rootContext().setContextProperty("historyBackend", backend)
            engine.rootContext().setContextProperty("historyConnected", False)
            with patch.object(history, "_index_display", timed("index", history._index_display)), \
                 patch.object(history, "_read_display", timed("read_display", history._read_display)), \
                 patch.object(history.HistoryBackend, "_publish", timed("publish", history.HistoryBackend._publish)):
                def load():
                    nonlocal window
                    engine.load(QUrl.fromLocalFile(str(ROOT / "app/quick/qml/HistoryWindow.qml")))
                    if not engine.rootObjects():
                        raise RuntimeError("HistoryWindow.qml failed: " + "\n".join(warnings))
                    window = engine.rootObjects()[0]
                    # QQmlProperty.write removes Screen-dependent QML size bindings.
                    QQmlProperty.write(window, "width", 1536)
                    QQmlProperty.write(window, "height", 864)
                scenario("window_load", load)
                scenario("index_cold", backend.refresh, True)
                scenario("index_warm", backend.refresh, True)
                scenario("selected_large_file", lambda: backend.setChecked(str(selected), True), True)
                initial = backend.export_snapshot()
                native_points = sum(len(trace["x"]) for trace in initial["traces"])
                for index in range(3):
                    scenario(f"unchanged_refresh_{index+1}", backend.refresh, True)
                automatic_refresh = getattr(backend, "_automatic_refresh", backend.refresh)
                for index in range(3):
                    scenario(f"unchanged_periodic_refresh_{index+1}", automatic_refresh, True)

                unselected = folder / "vds_sweep_00001.csv"
                with unselected.open("a", encoding="utf-8") as stream:
                    stream.write("2,2e-9,forward\n")
                scenario("unselected_file_growth", backend.refresh, True)
                before_growth = backend.export_snapshot()
                with selected.open("a", encoding="utf-8") as stream:
                    stream.write("2,2e-9,forward\n")
                scenario("selected_file_growth", backend.refresh, True)
                after_growth = backend.export_snapshot()

                search = visual("historySearch")
                typing_samples = []
                def type_search():
                    search.forceActiveFocus()
                    for character in "vds_sweep_000":
                        started = time.perf_counter()
                        QTest.keyClick(window, character, Qt.KeyboardModifier.NoModifier, 0)
                        app.processEvents()
                        typing_samples.append((time.perf_counter()-started)*1000)
                typing = scenario("search_typing_burst", type_search, settle=.25)
                typing["per_character_event_drain"] = distribution(typing_samples)
                typed_search = backend.search
                filtered_rows = len(backend._files)
                expected_filtered_rows = sum("vds_sweep_000" in record.path.name for record in backend._records)
                typing["filtered_rows"] = filtered_rows
                def reset_search():
                    QTest.keyClick(window, Qt.Key.Key_A, Qt.KeyboardModifier.ControlModifier, 0)
                    QTest.keyClick(window, Qt.Key.Key_Backspace, Qt.KeyboardModifier.NoModifier, 0)
                scenario("search_reset", reset_search, settle=.25)
                pane = visual("historyFilePane")
                def list_items(item):
                    for child in item.childItems():
                        yield child
                        yield from list_items(child)
                file_list = next(item for item in list_items(pane) if item.metaObject().className().startswith("QQuickListView"))
                scroll_samples = []
                scroll_before = file_list.property("contentY")
                def scroll():
                    for step in range(1, 13):
                        started = time.perf_counter()
                        file_list.setProperty("contentY", scroll_before+step*40)
                        app.processEvents()
                        scroll_samples.append((time.perf_counter()-started)*1000)
                scrolling = scenario("file_list_scroll", scroll)
                scrolling["per_step_event_drain"] = distribution(scroll_samples)
                scrolling["content_y_before"] = scroll_before
                scrolling["content_y_after"] = file_list.property("contentY")
                plot = visual("historyMainPlot")
                def pan():
                    center = plot.mapToScene(plot.boundingRect().center()).toPoint()
                    QTest.mousePress(window, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, center)
                    for step in range(1, 13):
                        QTest.mouseMove(window, center+QPoint(step*3, 0), 0)
                    QTest.mouseRelease(window, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, center+QPoint(36, 0))
                before_pan = backend.capture_view()
                scenario("plot_pan", pan)
                final = backend.export_snapshot()
                image = window.grabWindow()
                image_ok = not image.isNull() and image.save(str(args.output / "history.png"))
                checks = dict(indexed_files=len(backend._records)==args.files,
                    initial_native_points=native_points==args.points,
                    unselected_preserves_arrays=before_growth["traces"][0]["x"] is initial["traces"][0]["x"],
                    selected_growth_native_points=sum(len(t["x"]) for t in after_growth["traces"])==args.points+1,
                    selected_growth_last_x=float(after_growth["traces"][0]["x"][-1])==2.,
                    selected_growth_last_y=float(after_growth["traces"][0]["y"][-1])==2e-9,
                    export_arrays_readonly=all(not t[key].flags.writeable for t in final["traces"] for key in ("x","y")),
                    typing_reaches_backend=typed_search=="vds_sweep_000",
                    typing_filter_applied=filtered_rows==expected_filtered_rows,
                    search_reset_restores_files=len(backend._files)==args.files,
                    scroll_moves_viewport=file_list.property("contentY") > scroll_before,
                    pan_changes_viewport=before_pan.get("xlim") != final["view"].get("xlim"),
                    logical_dimensions=window.width()==1536 and window.height()==864,
                    screenshot_saved=image_ok)
                report = dict(context=dict(python=sys.version.split()[0], qt=qVersion(), pyside=PySide6.__version__,
                    os=platform.platform(), qt_platform=app.platformName(), requested_scale=args.scale,
                    device_pixel_ratio=window.devicePixelRatio(), logical_width=window.width(), logical_height=window.height(),
                    image_pixel_width=image.width(), image_pixel_height=image.height(), files=args.files,
                    selected_file_points=args.points, hardware="blocked", settings="temporary", data="temporary synthetic CSV only"),
                    timing_definitions=dict(elapsed="action plus queued worker/GUI completion plus requested_settle_ms; not GUI blocking",
                        synchronous_action="direct action duration; typing/pan include event dispatch",
                        worker="individual _index_display / _read_display worker elapsed; publish runs on GUI thread",
                        gui_event_drain="one processEvents(AllEvents, 10) call, includes any synchronous queued publication/paint",
                        gui_timer_gap="5ms precise timer delivery gaps, includes OS scheduling; diagnostic, no hard timing threshold"),
                    scenarios=stages, operations=worker_samples, counters=counts, checks=checks,
                    qml_warnings=warnings, python_exceptions=exceptions)
                # Persist isolated settings and stop workers before scratch data
                # is removed by TemporaryDirectory.
                window.hide()
                backend.shutdown()
    finally:
        heartbeat.stop()
        if window is not None:
            window.hide()
        if backend is not None:
            backend.shutdown()
            if report:
                report["shutdown_confirmed"] = backend._closed and not backend._pool.activeThreadCount()
        if engine is not None:
            engine.deleteLater()
            app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        if backend is not None:
            backend.deleteLater()
            app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        sys.excepthook = previous_hook
    # Optimization counters are evidence, not correctness gates: baseline may
    # legitimately republish an unchanged selected array after unrelated growth.
    required = {key: value for key, value in report["checks"].items() if key != "unselected_preserves_arrays"}
    report["success"] = all(required.values()) and not warnings and not exceptions and report["shutdown_confirmed"]
    (args.output / "benchmark.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))
    return 0 if report["success"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
