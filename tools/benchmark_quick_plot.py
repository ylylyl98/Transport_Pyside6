r"""Reproducible, hardware-free native Qt Quick rendering baseline.

Run .venv\Scripts\python.exe -B tools\benchmark_quick_plot.py --points 1000000
Measures full numeric loading, cold/warm native QPainter passes, one real Quick
window frame and the append-only acquisition path. No Matplotlib is imported.
Results are local machine observations, not real-time or hardware guarantees.
"""
from pathlib import Path
import argparse
import json
import os
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from app.offline_sandbox import OfflineSandbox
_sandbox = OfflineSandbox(ROOT)
_sandbox.install_hardware_guards()

import numpy as np
from PySide6.QtCore import QUrl
from PySide6.QtGui import QGuiApplication, QImage, QPainter
from PySide6.QtQml import QQmlComponent
from PySide6.QtQuick import QQuickView
from PySide6.QtQuickControls2 import QQuickStyle
from PySide6.QtTest import QTest
from app.quick.plot import PlotModel, QuickPlot, envelope_segments, register_types


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--points', type=int, default=1_000_000)
    parser.add_argument('--output', type=Path, default=ROOT / 'tmp/verification/quick_plot/benchmark.json')
    args = parser.parse_args()
    if args.points < 2:
        parser.error('--points must be at least 2')
    QQuickStyle.setStyle('Fusion')
    app = QGuiApplication([])
    register_types()
    x = np.linspace(-1, 1, args.points)
    y = np.sin(x * 50) + .03 * np.sin(x * 1000)
    y[len(y)//2:len(y)//2+3] = np.nan
    model = PlotModel()
    started = time.perf_counter()
    model.setTraces([{'x': x, 'y': y * scale, 'channel': channel, 'label': channel}
                     for channel, scale in zip(model.channelOptions, (1e-9, 1e-6, 1e-12, 1e-8))], 'Vtg (V)', 'Current (A)')
    model.fourChannels = True
    load_ms = (time.perf_counter() - started) * 1000
    view = QQuickView()
    view.engine().rootContext().setContextProperty('benchmarkModel', model)
    component = QQmlComponent(view.engine())
    qml_dir = ROOT / 'app/quick/qml'
    component.setData(b'import QtQuick\nimport "."\nPlotPane { width: 1400; height: 900; plotModel: benchmarkModel }', QUrl.fromLocalFile(str(qml_dir / '_benchmark.qml')))
    root = component.create()
    if root is None:
        raise RuntimeError('\n'.join(e.toString() for e in component.errors()))
    view.setContent(QUrl(), component, root)
    view.resize(1400, 900)
    started = time.perf_counter()
    view.show()
    QTest.qWait(60)
    app.processEvents()
    image = view.grabWindow()
    first_frame_ms = (time.perf_counter() - started) * 1000
    if image.isNull():
        raise RuntimeError('Qt Quick did not render a real window frame')
    item = root.findChild(QuickPlot, 'nativePlot')
    target = QImage(int(item.width()), int(item.height()), QImage.Format.Format_ARGB32_Premultiplied)

    def paint(cold):
        if cold:
            # Normal notifications now retain valid paths. Explicitly clear
            # both caches so the cold metric still measures first construction.
            item._cache.clear()
            item._run_cache.clear()
            item._image_cache = None
        started = time.perf_counter()
        painter = QPainter(target)
        item.paint(painter)
        painter.end()
        return (time.perf_counter() - started) * 1000

    cold = [paint(True) for _ in range(7)]
    warm = [paint(False) for _ in range(7)]
    viewport = {}
    for name, span in (("pan_1percent", .02), ("pan_full_width", 2.0)):
        samples = []
        for i in range(15):
            low = -.9 + i * .01
            model.set_limits(model.channel, (low, low + span), model.limits(model.channel)[1])
            samples.append(paint(False))
        viewport[name + "_ms_median"] = round(float(np.median(samples)), 3)
    live = PlotModel()
    live.beginRun({'x_axis': 'Vtg'})
    count = min(25_000, args.points)
    started = time.perf_counter()
    for i in range(count):
        live.appendRecord({'vtg': i*.001, 'vbg': 0., 'vds': .01, 'Ids_DC': i*1e-12,
                           'Ids_X': i*1e-11, 'Ids_Y': -i*1e-11, 'Ids_Keithley': i*1e-12})
    append_seconds = time.perf_counter() - started
    envelope_count = sum(len(a) for a, _ in envelope_segments(x, y, (-1, 1), int(item.panels()[0][1].width())))
    report = {'points_per_channel': args.points, 'channels': 4, 'retained_numeric_samples': args.points*4,
              'display_vertices_per_channel': envelope_count,
              'load_numeric_ms': round(load_ms, 3), 'first_actual_quick_frame_ms_including_60ms_wait': round(first_frame_ms, 3),
              'cold_paint_ms_median': round(float(np.median(cold)), 3),
              'warm_paint_ms_median': round(float(np.median(warm)), 3),
              'cold_cache_policy': 'discard curve paths and finite-run indexes',
              **viewport,
              'append_records': count, 'append_records_per_second': round(count / append_seconds),
              'append_render_notifications_before_event_loop': live.revision,
              'matplotlib_imported': any(name.startswith('matplotlib') for name in sys.modules),
              'qt_platform': app.platformName(), 'python': sys.version.split()[0]}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2), encoding='utf-8')
    image.save(str(args.output.with_suffix('.png')))
    print(json.dumps(report, indent=2))
    live._timer.stop()
    model._timer.stop()
    view.close()
    view.setContent(QUrl(), None, None)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
