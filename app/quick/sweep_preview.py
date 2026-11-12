"""Hardware-free sweep previews, independent of the live acquisition plot."""
from copy import deepcopy
import math

import numpy as np
from PySide6.QtCore import QObject, Property, QRunnable, QThreadPool, Signal, Slot

from app.quick.plot import PlotModel
from app.utils import _frange_inc
from app.workers.cosweep import build_cosweep_points, validate_cosweep_params
from app.workers.line_sweep import LineSweepWorker
from app.workers.photocurrent import PhotocurrentWorker
from app.models import SaveRoot
from app.plot_x_axis import FOLLOW_SWEEP, STEP_INDEX, resolve_gate_scan_x_axis, plot_x_axis_label, record_x_value

MAX_POINTS = 250000
MAX_DRAW_VERTICES = 20000
MAX_SCATTER_POINTS = 2000


def _compress_path(vertices):
    """Remove only interior collinear points in the same direction."""
    xy = np.asarray(vertices, dtype=float).reshape(-1, 2)
    if len(xy) > 1:
        # A return scan repeats its turnaround sample. Remove consecutive
        # duplicates first, so both copies of the turning point cannot be
        # mistaken for removable collinear interior vertices.
        xy = xy[np.r_[True, np.any(xy[1:] != xy[:-1], axis=1)]]
    if len(xy) <= 2:
        return xy
    # Vectorized geometry preserves every reversal, corner and NaN break.
    a, b = np.diff(xy, axis=0)[:-1], np.diff(xy, axis=0)[1:]
    finite = np.isfinite(xy[:-2]).all(axis=1) & np.isfinite(xy[1:-1]).all(axis=1) & np.isfinite(xy[2:]).all(axis=1)
    cross = a[:, 0] * b[:, 1] - a[:, 1] * b[:, 0]
    same_direction = (a[:, 0] * b[:, 0] + a[:, 1] * b[:, 1]) >= 0
    removable = finite & (cross == 0) & same_direction
    return xy[np.r_[True, ~removable, True]]


def _trace(vertices, label, color=''):
    xy = _compress_path(vertices)
    return dict(x=xy[:, 0], y=xy[:, 1], label=label, color=color, style='line')


def _sample_trace(vertices, color='preview_curve_0'):
    xy = np.asarray(vertices, dtype=float).reshape(-1, 2)
    return dict(x=xy[:, 0], y=xy[:, 1], label='', color=color, style='scatter')


def _limit_trace(trace, limit):
    truncated = len(trace['x']) > limit
    if truncated:
        trace = {**trace, 'x': trace['x'][:limit], 'y': trace['y'][:limit]}
    return trace, truncated


def _map_path(points, params):
    vertices = [(points[0]['fast_value'], points[0]['slow_value'])]
    if params.regions:
        fast, slow = params.axis_fast, params.axis_slow
        for point in points[1:]:
            x, y = vertices[-1]
            for axis, value in point['moves']:
                if axis == fast:
                    x = value
                elif axis == slow:
                    y = value
                else:
                    raise ValueError('Unknown planned region move axis')
                vertices.append((x, y))
    else:
        vertices.extend((p['fast_value'], p['slow_value']) for p in points[1:])
    return vertices


def build_sweep_preview(mode, params, connections):
    """Build presentation-only geometry from the same planners as acquisition."""
    mode = int(mode)
    traces = []
    samples = []
    note = ''
    turn = None
    if mode == 2:
        validate_cosweep_params(params)
        points = build_cosweep_points(params)
        if not points or len(points) > MAX_POINTS:
            raise ValueError('Sweep preview requires 1–250,000 points')
        path, shortened = _limit_trace(_trace(_map_path(points, params), 'Planned sweep'), MAX_DRAW_VERTICES)
        traces.append(path)
        if shortened:
            note = f'Path display limited to first {MAX_DRAW_VERTICES:,} turning points; full scan remains {len(points):,} points.'
        if params.regions:
            # Rectangles are in the physical raw Vtg/Vbg coordinate system.
            regions = [dict(vtg_start=params.vtg_start, vtg_stop=params.vtg_stop,
                            vbg_start=params.vbg_start, vbg_stop=params.vbg_stop), *params.regions]
            for i, region in enumerate(regions, 1):
                f, s = params.axis_fast.lower(), params.axis_slow.lower()
                x0, x1 = region[f + '_start'], region[f + '_stop']
                y0, y1 = region[s + '_start'], region[s + '_stop']
                traces.append(_trace([(x0,y0), (x1,y0), (x1,y1), (x0,y1), (x0,y0)], f'Region {i}', 'preview_boundary'))
        axes = (f'{params.axis_fast} (V)', f'{params.axis_slow} (V)' if params.axis_slow != 'None' else 'Point order')
        if params.axis_slow == 'None':
            traces[0] = _trace([(p['fast_value'], i) for i, p in enumerate(points)], 'Planned sweep')
            axes = (f'{params.axis_fast} (V)', 'Point order')
        if params.axis_slow == 'None':
            endpoints = [(points[0]['fast_value'], 0), (points[-1]['fast_value'], len(points)-1)]
        else:
            endpoints = [(points[0]['fast_value'], points[0]['slow_value']),
                         (points[-1]['fast_value'], points[-1]['slow_value'])]
        if len(points) <= MAX_SCATTER_POINTS:
            samples.append(_sample_trace([(p['fast_value'], i if params.axis_slow == 'None' else p['slow_value']) for i, p in enumerate(points)]))
        xbounds = (min(p['vtg'] for p in points), max(p['vtg'] for p in points))
        ybounds = (min(p['vbg'] for p in points), max(p['vbg'] for p in points))
        summary = (f'{len(points):,} planned points. Physical Vtg: {xbounds[0]:g} to {xbounds[1]:g} V; '
                   f'Vbg: {ybounds[0]:g} to {ybounds[1]:g} V. ' + note).strip()
        if params.regions:
            summary += f' {len(params.regions) + 1} merged rectangles; shared points acquired once.'
        title = 'Map sweep preview'
    elif mode == 0:
        step = float(params.vds_step)
        if not math.isfinite(step) or step <= 0:
            raise ValueError('Vds step must be positive')
        if abs(params.vds_stop - params.vds_start) / step > MAX_POINTS - 1:
            raise ValueError('Sweep exceeds 250,000 points')
        values = _frange_inc(params.vds_start, params.vds_stop, step if params.vds_stop >= params.vds_start else -step)
        if params.sweep_both_ways:
            if len(values) > 1:
                turn = (values[-1], len(values)-1)
            values += list(reversed(values))
        points = values
        traces = [_trace([(v, i) for i, v in enumerate(values)], 'Vds')]
        if len(points) <= MAX_SCATTER_POINTS:
            samples.append(_sample_trace([(v, i) for i, v in enumerate(values)]))
        endpoints = [(values[0], 0), (values[-1], len(values)-1)]
        axes, title = ('Vds (V)', 'Point order'), 'Vds sweep preview'
        summary = f'{len(points):,} planned points (including return pass if enabled).'
    elif mode in (1, 4):
        if not 1 <= int(params.n_points) <= MAX_POINTS:
            raise ValueError('Points / pass must be between 1 and 250,000')
        worker = LineSweepWorker(params, SaveRoot(), connections)
        points = worker._build_trajectory()
        worker._validate_trajectory_limits(points)
        forward_count = len(points)
        if params.sweep_both_ways:
            points = points + list(reversed(points))
        resolved = resolve_gate_scan_x_axis(FOLLOW_SWEEP, params.mode, params.derived_axis,
            params.raw_vtg_active, params.raw_vbg_active, params.raw_vds_active and params.vds_source != 'None')
        xs = [float(i) if resolved == STEP_INDEX else record_x_value(point, resolved) for i, point in enumerate(points)]
        for curve, (key, name) in enumerate((('vtg', 'Vtg'), ('vbg', 'Vbg'), ('vds', 'Vds'))):
            color = f'preview_curve_{curve}'
            traces.append(_trace([(x, point[key]) for x, point in zip(xs, points)], name, color))
            if len(points) <= MAX_SCATTER_POINTS:
                samples.append(_sample_trace([(x, point[key]) for x, point in zip(xs, points)], color))
        endpoints = [(xs[0], points[0]['vtg']), (xs[-1], points[-1]['vtg'])]
        if params.sweep_both_ways and forward_count > 1:
            turn = (xs[forward_count-1], points[forward_count-1]['vtg'])
        axes, title = (plot_x_axis_label(resolved, params.derived_ratio, params.derived_ratio_target), 'Gate / drain voltage (V)'), 'Gate scan preview'
        summary = f'{len(points):,} planned points (including return pass if enabled).'
        if resolved == STEP_INDEX:
            summary += ' Multiple raw axes sweep together; Step Index is the shared trajectory coordinate.'
    elif mode == 3:
        if (not all(math.isfinite(float(v)) for v in (params.wl_start, params.wl_stop, params.wl_step))
                or params.wl_step <= 0 or not (0 < params.wl_start <= 1500 and 0 < params.wl_stop <= 1500)
                or abs(params.wl_stop - params.wl_start) / params.wl_step > MAX_POINTS - 1):
            raise ValueError('Wavelength bounds, step or point count are invalid')
        conditions = [c for c in params.bias_conditions if c.enabled]
        if not conditions:
            raise ValueError('Enable at least one photocurrent bias condition')
        points = PhotocurrentWorker(params, SaveRoot(), connections)._wavelengths()
        traces = [_trace([(value, i) for i, value in enumerate(points)], 'Wavelength')]
        if len(points) <= MAX_SCATTER_POINTS:
            samples.append(_sample_trace([(value, i) for i, value in enumerate(points)]))
        endpoints = [(points[0], 0), (points[-1], len(points)-1)]
        axes, title = ('Wavelength (nm)', 'Point order'), 'Photocurrent sweep preview'
        summary = f'{len(points):,} wavelengths × {len(conditions)} enabled conditions.'
    elif mode == 5:
        if params.measurement_mode == 'stepped':
            from app.engine.bfield_transport_sweep import validate_stepped_params
            forward = validate_stepped_params(params)
            points = forward + (forward[-2::-1] if params.round_trip else ())
            vertices = [(field, i) for i, field in enumerate(points)]
            traces = [_trace(vertices, 'Planned points')]
            if len(points) <= MAX_SCATTER_POINTS:
                samples.append(_sample_trace(vertices))
            endpoints = [vertices[0], vertices[-1]]
            if params.round_trip:
                turn = vertices[len(forward) - 1]
            axes, title = ('B-field (T)', 'Point order'), 'Stepped field sweep'
            summary = f'{len(points):,} points per condition; temperature waiting is variable.'
        else:
            points = []
            traces = [_trace([(params.start_field_t, 0), (params.stop_field_t, 1)], 'Field range')]
            endpoints = [(params.start_field_t, 0), (params.stop_field_t, 1)]
            axes, title = ('B-field (T)', 'Range endpoint'), 'Continuous field range'
            summary = 'Continuous field sweep: range only; acquisition point count depends on timing.'
    else:
        raise ValueError('Unknown measurement mode')
    from app.bias_control import bias2_target
    if mode in (0, 1, 2, 4) and getattr(params, 'bias2_mode', 'Disabled') != 'Disabled':
        if params.bias2_mode == 'Follow' and params.vds_source == 'None':
            raise ValueError('Bias 2 Follow requires a Bias 1 source')
        targets = [bias2_target(params, p if mode == 0 else p['vds']) for p in points]
        if any(not math.isfinite(v) or abs(v) > 20 for v in targets):
            raise ValueError('Bias 2 preview exceeds ±20 V')
        summary += f' Bias 2 ({params.bias2_mode}): {min(targets):g} to {max(targets):g} V; start {targets[0]:g}, end {targets[-1]:g} V.'
        if mode in (1, 4):
            vertices = list(zip(xs, targets))
            traces.append(_trace(vertices, 'Bias 2', 'preview_curve_3'))
            if len(points) <= MAX_SCATTER_POINTS:
                samples.append(_sample_trace(vertices, 'preview_curve_3'))
    elif mode in (3, 5):
        conditions = [c for c in (params.bias_conditions if mode == 3 else params.conditions) if c.enabled]
        for index, condition in enumerate(conditions, 1):
            if condition.bias2_mode == 'Disabled':
                continue
            bias1 = params.use_vds and params.vds_source != 'None' if mode == 3 else condition.vds_source != 'None'
            if condition.bias2_mode == 'Follow' and not bias1:
                raise ValueError(f'Condition {index}: Bias 2 Follow requires a Bias 1 source')
            target = bias2_target(condition, condition.vds)
            if not math.isfinite(target) or abs(target) > 20:
                raise ValueError(f'Condition {index}: Bias 2 exceeds ±20 V')
            summary += f' Condition {index}: Bias 2 {target:g} V ({condition.bias2_mode}).'
    traces.extend(samples)
    if mode != 5:
        if len(points) <= MAX_SCATTER_POINTS:
            summary += f' Sample markers: {len(points):,}/{len(points):,} per curve; coincident positions overlap.'
        else:
            summary += f' Sample markers hidden for dense sweeps (more than {MAX_SCATTER_POINTS:,} points); the path and all {len(points):,} planned samples are retained.'
    traces.extend([{**_trace([endpoints[0]], 'Start', 'preview_start'), 'marker': 'start'},
                   {**_trace([endpoints[1]], 'End', 'preview_end'), 'marker': 'end'}])
    if turn is not None:
        traces.append({**_trace([turn], 'Stop / turn', 'preview_turn'), 'marker': 'turn'})
        summary += ' Forward Stop / turn is marked separately from final End.'
    if np.allclose(endpoints[0], endpoints[1], rtol=0, atol=1e-12):
        summary += ' Start and End are at the same position (circle + square).'
    limited = []
    was_limited = False
    for trace in traces:
        trace, shortened = _limit_trace(trace, MAX_DRAW_VERTICES)
        limited.append(trace)
        was_limited = was_limited or shortened
    if was_limited and not note:
        summary += f' Display limited to the first {MAX_DRAW_VERTICES:,} path vertices.'
    tick_values = {}
    if samples:
        for axis in ('x', 'y'):
            values = np.unique(np.concatenate([sample[axis] for sample in samples]))
            if len(values) <= 9:
                tick_values[axis] = values.tolist()
    return dict(traces=limited, xLabel=axes[0], yLabel=axes[1], title=title, tickValues=tick_values,
                summary=summary, pointCount=None if mode == 5 and params.measurement_mode == "continuous" else len(points))


class _PreviewTask(QRunnable):
    def __init__(self, generation, mode, params, connections, signal):
        super().__init__()
        self.generation, self.mode, self.params, self.connections, self.signal = generation, mode, params, connections, signal

    def run(self):
        try:
            result, error = build_sweep_preview(self.mode, self.params, self.connections), ''
        except Exception as exc:
            result, error = None, str(exc)
        try:
            self.signal.emit(self.generation, result, error)
        except RuntimeError:
            pass


class SweepPreview(QObject):
    changed = Signal()
    completed = Signal(int, object, str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._plot = PlotModel(self)
        self._busy, self._error, self._summary, self._stale = False, '', '', True
        self._generation = 0
        self._pool = QThreadPool(self)
        self._pool.setMaxThreadCount(1)
        self.completed.connect(self._complete)

    plotModel = Property(QObject, lambda self: self._plot, constant=True)
    busy = Property(bool, lambda self: self._busy, notify=changed)
    error = Property(str, lambda self: self._error, notify=changed)
    summary = Property(str, lambda self: self._summary, notify=changed)
    stale = Property(bool, lambda self: self._stale, notify=changed)

    @Slot()
    def invalidate(self):
        self._generation += 1
        self._stale = True
        self.changed.emit()

    @Slot(str)
    def reject(self, message):
        self._generation += 1
        self._error, self._summary, self._stale = str(message), '', True
        self._plot.clear()
        self.changed.emit()

    @Slot(int, object, object, result=bool)
    def request(self, mode, params, connections):
        if self._busy:
            return False
        try:
            params, connections = deepcopy(params), deepcopy(connections)
        except Exception as exc:
            self.reject(str(exc))
            return False
        self._generation += 1
        self._busy, self._error, self._stale = True, '', True
        self.changed.emit()
        self._pool.start(_PreviewTask(self._generation, mode, params, connections, self.completed))
        return True

    @Slot(int, object, str)
    def _complete(self, generation, result, error):
        self._busy = False
        if generation == self._generation:
            self._error = error
            self._stale = bool(error)
            if error:
                self._summary = ''
                self._plot.clear()
            else:
                self._plot.setTraces(result['traces'], result['xLabel'], result['yLabel'], tickValues=result.get('tickValues'))
                self._plot.title = result['title']
                self._plot.home()
                self._summary = result['summary']
        self.changed.emit()
