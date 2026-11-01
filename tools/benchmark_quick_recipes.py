r"""Measure inline recipe editing through a rendered, hardware-blocked Quick UI.

Run .venv\Scripts\python.exe -B tools\benchmark_quick_recipes.py --iterations 20
Results separate synchronous event work from action-to-frame time; neither is
a physical-instrument timing measurement.
"""
import argparse
import json
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--iterations", type=int, default=20)
    parser.add_argument("--output", type=Path, default=ROOT / "tmp/verification/recipes")
    args = parser.parse_args()
    if args.iterations < 2:
        parser.error("Use at least two iterations")
    from app.offline_sandbox import OfflineSandbox, configure_offscreen_font
    sandbox = OfflineSandbox(ROOT, preview=True)
    sandbox.install_hardware_guards()
    from PySide6 import QtCore, QtGui, QtQml, QtQuickControls2, QtTest
    from app.quick.application import ApplicationBackend
    from app.quick.parameters import row_default
    from app.quick.plot import register_types
    from tools.benchmark_quick_ui import distribution

    QtQuickControls2.QQuickStyle.setStyle("Fusion")
    app = QtGui.QGuiApplication([])
    configure_offscreen_font(app)
    register_types()
    backend = ApplicationBackend(offline=True)
    engine = QtQml.QQmlApplicationEngine()
    warnings, exceptions = [], []
    previous_hook = sys.excepthook
    sys.excepthook = lambda kind, exc, tb: exceptions.append(f"{kind.__name__}: {exc}")
    engine.warnings.connect(lambda errors: warnings.extend(str(e) for e in errors))
    engine.rootContext().setContextProperty("appBackend", backend)
    engine.load(QtCore.QUrl.fromLocalFile(str(ROOT / "app/quick/qml/Main.qml")))
    assert engine.rootObjects(), warnings
    window = engine.rootObjects()[0]
    window.setWidth(1536)
    window.setHeight(864)
    window.setProperty("parameterWidth", 520)
    window.setProperty("showInstruments", False)

    def frame():
        loop = QtCore.QEventLoop()
        timer = QtCore.QTimer()
        timer.setSingleShot(True)
        observed = []
        def swapped():
            observed.append(True)
            loop.quit()
        window.frameSwapped.connect(swapped)
        timer.timeout.connect(loop.quit)
        timer.start(2000)
        QtCore.QTimer.singleShot(0, window.update)
        loop.exec()
        timer.stop()
        window.frameSwapped.disconnect(swapped)
        return bool(observed)

    results = []
    def visual(name):
        def descendants(item):
            yield item
            for child in item.childItems():
                yield from descendants(child)
        return next(item for item in descendants(window.contentItem()) if item.objectName() == name)
    try:
        assert frame()
        service = backend.measurement
        for mode, key, value_key in ((3, "bias_conditions", "vtg"),
                                     (4, "conditions", "raw_vtg_start"),
                                     (5, "conditions", "doping")):
            for count in (1, 100):
                service.mode = mode
                rows = [dict(row_default(key, mode)) for _ in range(count)]
                for i, row in enumerate(rows):
                    if "name" in row:
                        row["name"] = f"Condition {i+1}"
                service.setValue(key, rows)
                model = service.rowModel(key)
                uid = model.data(model.index(0, 0), model.UidRole)
                assert frame()
                editor = visual(f"recipeCell_{key}_{uid}_{value_key}")
                editor.forceActiveFocus()
                assert frame()
                drain, rendered = [], []
                for i in range(args.iterations):
                    started = time.perf_counter()
                    QtTest.QTest.keyClick(window, QtCore.Qt.Key_A, QtCore.Qt.ControlModifier)
                    for char in f"0.{i}":
                        QtTest.QTest.keyClick(window, ord(char))
                    app.processEvents()
                    drain.append((time.perf_counter() - started) * 1000)
                    assert frame()
                    rendered.append((time.perf_counter() - started) * 1000)
                assert model.commit(uid, value_key, editor.property("text"))
                operations = {}
                operations["typing"] = {"event_drain": distribution(drain), "action_to_frame": distribution(rendered)}
                def structure(i):
                    service.removeRow(key, count-1)
                    service.addRow(key)
                for label, action in (
                    ("cell_edit", lambda i: model.commit(uid, value_key, i * .001)),
                    ("toggle", lambda i: model.commit(uid, "enabled", i % 2 == 0)),
                    ("select_scroll", lambda i: model.select(model.data(model.index((i * 7) % count, 0), model.UidRole))),
                    ("remove_add", structure)):
                    drain, rendered = [], []
                    for i in range(args.iterations):
                        started = time.perf_counter()
                        action(i)
                        app.processEvents()
                        drain.append((time.perf_counter() - started) * 1000)
                        assert frame(), f"No rendered frame for {label}"
                        rendered.append((time.perf_counter() - started) * 1000)
                    operations[label] = {"event_drain": distribution(drain), "action_to_frame": distribution(rendered)}
                assert model.count == count
                results.append(dict(mode=mode, rows=count, operations=operations))
        args.output.mkdir(parents=True, exist_ok=True)
        report = dict(cases=results, warnings=warnings, exceptions=exceptions,
            context=dict(qt=QtCore.qVersion(), platform=app.platformName(), hardware="blocked",
                         settings="temporary", logical_size=[window.width(), window.height()],
                         setup_width=520, iterations=args.iterations),
            success=not warnings and not exceptions)
        (args.output / "benchmark.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
        for result in results:
            print(json.dumps(dict(mode=result["mode"], rows=result["rows"], p95_ms={
                name: {metric: values[metric]["p95_ms"] for metric in ("event_drain", "action_to_frame")}
                for name, values in result["operations"].items()})))
        print(f"Report: {args.output / 'benchmark.json'}; success={report['success']}")
        return 0 if report["success"] else 1
    finally:
        window.hide()
        backend.requestClose()
        deadline = time.monotonic() + 5
        while not backend.closeAccepted and time.monotonic() < deadline:
            app.processEvents()
        engine.deleteLater()
        QtCore.QCoreApplication.sendPostedEvents(None, QtCore.QEvent.DeferredDelete)
        backend.deleteLater()
        QtCore.QCoreApplication.sendPostedEvents(None, QtCore.QEvent.DeferredDelete)
        sys.excepthook = previous_hook


if __name__ == "__main__":
    raise SystemExit(main())
