"""Native Qt Quick plots. Numeric data never crosses the QML object boundary.

The acquisition slot only appends to amortized numeric buffers. A 30 Hz timer
coalesces paint requests. Painting uses a viewport-sized min/max envelope;
full-resolution samples remain available for axis changes, cuts and export.
There is no QWidget, Matplotlib canvas or image-producing worker in this path.
"""
from __future__ import annotations

import math
from copy import deepcopy
from pathlib import Path
from dataclasses import dataclass, field

import numpy as np
from PySide6.QtCore import QObject, Property, QRectF, QPointF, Qt, QTimer, Signal, Slot
from PySide6.QtGui import QColor, QFont, QImage, QPainter, QPainterPath, QPen
from PySide6.QtQml import qmlRegisterType
from PySide6.QtQuick import QQuickPaintedItem

from app.plot_ranges import FULL_SWEEP, ACQUIRED_DATA, finite_bounds, padded_limits
from app.plot_x_axis import FOLLOW_SWEEP, normalize_plot_x_selection, plot_x_axis_label
from app.quick.palettes import COLOR_TABLES, COLOR_MAP_NAMES
from app.drag_drive import DUAL_SIGNALS, dual_enabled
from app.result_channels import plot_channel_options


COLORS = ('#2978d5', '#cf650b', '#17856b', '#b749a4', '#7962bd', '#bd4e4e')
COLORS_DARK = ('#8fbaff', '#ffbd78', '#6fdbba', '#e9a1db', '#b8a8f0', '#ff9c9c')
AXIS_KEYS = {'Step Index': 'index', 'Vtg': 'vtg', 'Vbg': 'vbg', 'Vds': 'vds',
             'Doping': 'doping', 'E-field': 'efield', 'Wavelength': 'x', 'B': 'x',
             'B-field': 'x'}
DEFAULT_CHANNELS = ['Ids_DC', 'Ids_X', 'Ids_Y', 'Ids_Keithley']


class NumericBuffer:
    """Amortized O(1) append, zero-copy views, incrementally maintained bounds."""
    def __init__(self, values=()):
        values = np.asarray(values, dtype=np.float64).reshape(-1)
        self.size = len(values)
        self.storage = np.empty(max(256, self.size), dtype=np.float64)
        self.storage[:self.size] = values
        self.bounds = finite_bounds(values)

    def append(self, value):
        value = float(value) if value is not None else math.nan
        if self.size == len(self.storage):
            grown = np.empty(len(self.storage) * 2, dtype=np.float64)
            grown[:self.size] = self.storage
            self.storage = grown
        self.storage[self.size] = value
        self.size += 1
        if math.isfinite(value):
            self.bounds = (value, value) if self.bounds is None else (
                min(value, self.bounds[0]), max(value, self.bounds[1]))

    @property
    def values(self):
        return self.storage[:self.size]


@dataclass
class Trace:
    columns: dict = field(default_factory=dict)
    label: str = ''
    direction: str = ''
    channel: str = ''
    color: str = ''
    revision: int = 0
    source_index: int = 0

    def append(self, record):
        size = next(iter(self.columns.values())).size if self.columns else 0
        for key in set(self.columns) | set(record):
            if key not in self.columns:
                self.columns[key] = NumericBuffer(np.full(size, np.nan))
            self.columns[key].append(record.get(key, np.nan))
        self.revision += 1


def merge_bounds(bounds):
    available = [bound for bound in bounds if bound is not None]
    return (min(b[0] for b in available), max(b[1] for b in available)) if available else None


def _finite_runs(x, y):
    """Index finite runs once per trace revision, including sweep direction."""
    valid = np.isfinite(x) & np.isfinite(y)
    boundaries = np.flatnonzero(np.diff(np.r_[False, valid, False]))
    runs = []
    for start, stop in boundaries.reshape(-1, 2):
        xx = x[start:stop]
        order = 1 if np.all(xx[1:] >= xx[:-1]) else -1 if np.all(xx[1:] <= xx[:-1]) else 0
        runs.append((start, stop, order))
    return runs


def envelope_segments(x, y, xlim, width, *, runs=None):
    """Preserve source order, extrema and NaN gaps with O(pixel width) vertices.

    Each finite run is independent. The first/last and min/max point of each
    *source-order* bucket are retained; reversal and non-monotonic data do not
    acquire false connecting lines. One neighbor outside each viewport edge
    keeps crossing lines visible. This function never mutates its source.
    """
    x, y = np.asarray(x), np.asarray(y)
    size = min(len(x), len(y))
    if not size:
        return []
    x, y = x[:size], y[:size]
    if runs is None:
        runs = _finite_runs(x, y)
    result = []
    budget = max(16, int(width) * 2)
    low, high = sorted(xlim)
    for start, stop, order in runs:
        xx, yy = x[start:stop], y[start:stop]
        if order:
            # Binary search avoids rescanning a million-point sweep while
            # panning a small viewport. Keep the crossing neighbors and source
            # order, including descending sweeps and duplicate coordinates.
            ascending = xx if order > 0 else xx[::-1]
            if high < ascending[0] or low > ascending[-1]:
                continue
            left = max(0, int(np.searchsorted(ascending, low, side='left')) - 1)
            right = min(len(xx), int(np.searchsorted(ascending, high, side='right')) + 1)
            if order < 0:
                left, right = len(xx) - right, len(xx) - left
            spans = [(left, right)]
        else:
            spans = _visible_spans(xx, low, high)
        for left, right in spans:
            sx, sy = xx[left:right], yy[left:right]
            count = len(sx)
            if count > budget:
                step = int(math.ceil(count / max(1, budget // 4)))
                nblocks = count // step
                grid = sy[:nblocks * step].reshape(nblocks, step)
                base = np.arange(nblocks) * step
                indices = np.column_stack((base, base + np.argmin(grid, axis=1),
                                           base + np.argmax(grid, axis=1), base + step - 1)).reshape(-1)
                tail = nblocks * step
                extras = ([tail, tail + int(np.argmin(sy[tail:])),
                           tail + int(np.argmax(sy[tail:])), count - 1] if tail < count else [])
                indices = np.unique(np.r_[indices, extras]).astype(int)
                sx, sy = sx[indices], sy[indices]
            result.append((sx, sy))
    return result


def _visible_spans(xx, low, high):
    """General path for nonmonotonic coordinates and outside excursions."""
    inside = (xx >= low) & (xx <= high)
    # Include a segment when either endpoint is inside, or it crosses the
    # complete viewport. Never connect across a removed finite run.
    crossing = (xx[:-1] < low) & (xx[1:] > high) | (xx[1:] < low) & (xx[:-1] > high)
    visible = inside.copy()
    visible[:-1] |= inside[1:] | crossing
    visible[1:] |= inside[:-1] | crossing
    return np.flatnonzero(np.diff(np.r_[False, visible, False])).reshape(-1, 2)


def cell_edges(values):
    values = np.asarray(values, dtype=float)
    if len(values) == 1:
        margin = max(abs(float(values[0])) * .05, .05)
        return np.array([values[0] - margin, values[0] + margin])
    mids = (values[:-1] + values[1:]) / 2
    return np.r_[values[0] - (mids[0] - values[0]), mids,
                 values[-1] + (values[-1] - mids[-1])]


def prepare_heatmap(data):
    """Validate dense grids or build exact-coordinate grids without averaging."""
    x, y, z = (np.asarray(data[key], dtype=float) for key in ('x', 'y', 'z'))
    if x.ndim != 1 or y.ndim != 1 or not x.size or not y.size:
        raise ValueError('Heatmap axes must be nonempty one-dimensional arrays')
    if not np.all(np.isfinite(x)) or not np.all(np.isfinite(y)):
        raise ValueError('Heatmap coordinates must be finite')
    if z.ndim == 1:
        if len(x) != len(y) or len(x) != len(z):
            raise ValueError('Point heatmap X/Y/Z lengths must match')
        xs, xi = np.unique(x, return_inverse=True)
        ys, yi = np.unique(y, return_inverse=True)
        if len(np.unique(yi * len(xs) + xi)) != len(x):
            raise ValueError('Duplicate coordinates; choose a direction or row/pass')
        if len(xs) * len(ys) > 2_000_000:
            raise ValueError('Heatmap exceeds 2 million cells; choose scan axes')
        grid = np.full((len(ys), len(xs)), np.nan)
        grid[yi, xi] = z
        x, y, z = xs, ys, grid
    elif z.shape != (len(y), len(x)):
        raise ValueError('Heatmap Z must have shape (len(Y), len(X))')
    if len(np.unique(x)) != len(x) or len(np.unique(y)) != len(y):
        raise ValueError('Duplicate heatmap axis coordinates')
    if z.size > 2_000_000:
        raise ValueError('Heatmap exceeds 2 million cells')
    ix, iy = np.argsort(x), np.argsort(y)
    return {'x': x[ix].copy(), 'y': y[iy].copy(), 'z': z[np.ix_(iy, ix)].copy()}


def color_table(name):
    """Standard 256-entry RGB ramps, shared visually with offline PNG export.

    Tables are static assets: importing or painting this module does not import
    Matplotlib or invoke a rendering worker.
    """
    canonical = next((key for key in COLOR_TABLES if key.lower() == str(name).lower()), 'viridis')
    return COLOR_TABLES[canonical]


class PlotModel(QObject):
    changed = Signal()
    frameReady = Signal()
    viewChanged = Signal()
    preferencesChanged = Signal()
    cutRequested = Signal(float, float)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._traces = []
        self._heatmap = None
        self._snapshot = {}
        self._plan = {}
        self._run = False
        self._group = None
        self._count = 0
        self._range_mode = FULL_SWEEP
        self._channel = 'Ids_DC'
        self._channels = DEFAULT_CHANNELS.copy()
        self._four = False
        self._channel_order = None
        self._channel_labels = {}
        self._x_axis = FOLLOW_SWEEP
        self._resolved_axis = 'Step Index'
        self._x_label = 'Step Index'
        self._y_label = ''
        self._z_label = ''
        self._title = ''
        self._interaction = 'pan'
        self._manual_x = None
        self._manual_y = {}
        self._clim = None
        self._last_auto_clim = (0., 1.)
        self._cmap = 'viridis'
        self._dirty = False
        self.revision = 0
        self._timer = QTimer(self)
        self._timer.setInterval(33)
        self._timer.timeout.connect(self.flush)

    def _queue(self, metadata=False):
        self._dirty = True
        if metadata:
            self.changed.emit()
        if not self._timer.isActive():
            self._timer.start()

    @Slot()
    def flush(self):
        if self._dirty:
            self._dirty = False
            self.revision += 1
            self.frameReady.emit()
        self._timer.stop()

    def _get_title(self): return self._title
    def _set_title(self, value):
        self._title = str(value)
        self._queue(True)
    title = Property(str, _get_title, _set_title, notify=changed)
    xLabel = Property(str, lambda s: s._x_label, notify=changed)
    yLabel = Property(str, lambda s: s._y_label or f'{s._channel} (A)', notify=changed)
    zLabel = Property(str, lambda s: s._z_label, notify=changed)
    isHeatmap = Property(bool, lambda s: s._heatmap is not None, notify=changed)
    pointCount = Property(int, lambda s: s._count, notify=frameReady)
    channelOptions = Property('QStringList', lambda s: s._channels, notify=changed)

    @Property('QStringList', notify=changed)
    def legendEntries(self):
        return list(dict.fromkeys(self.legend_text(trace) for channel in self.channels()
                                 for trace, _, _ in self.traces(channel) if trace.label))

    @staticmethod
    def legend_text(trace):
        label, direction = trace.label, trace.direction
        if direction and direction.casefold() not in (part.strip().casefold() for part in label.split('·')):
            label += f' · {direction}'
        return label

    def _set_channel(self, value):
        value = str(value)
        if value != self._channel:
            self._channel = value
            self._manual_y.clear()
            self.preferencesChanged.emit()
            self._queue(True)
    channel = Property(str, lambda s: s._channel, _set_channel, notify=changed)

    def _set_four(self, value):
        self._four = bool(value)
        self.preferencesChanged.emit()
        self._queue(True)
    fourChannels = Property(bool, lambda s: s._four, _set_four, notify=changed)

    @Slot(str)
    def setXRangeMode(self, value):
        if value not in (FULL_SWEEP, ACQUIRED_DATA):
            raise ValueError('X range must be Full sweep or Acquired data')
        self._range_mode = value
        self._manual_x = None
        self.preferencesChanged.emit()
        self.viewChanged.emit()
        self._queue(True)
    xRangeMode = Property(str, lambda s: s._range_mode, setXRangeMode, notify=changed)

    def _set_x_axis(self, value):
        self._x_axis = normalize_plot_x_selection(value)
        self._resolve_axis()
        self._manual_x = None
        self.preferencesChanged.emit()
        self._queue(True)
    xAxis = Property(str, lambda s: s._x_axis, _set_x_axis, notify=changed)

    def _set_interaction(self, value):
        self._interaction = 'zoom' if value == 'zoom' else 'pan'
        self.preferencesChanged.emit()
        self.changed.emit()
    interactionMode = Property(str, lambda s: s._interaction, _set_interaction, notify=changed)
    colorMap = Property(str, lambda s: s._cmap, lambda s, v: s.setColorMap(v), notify=changed)
    colorMapOptions = Property('QStringList', lambda s: COLOR_MAP_NAMES, constant=True)

    def _resolve_axis(self):
        self._resolved_axis = self._snapshot.get('x_axis', self._snapshot.get('resolved_axis', 'Step Index')) if self._x_axis == FOLLOW_SWEEP else self._x_axis
        params = self._snapshot.get('params', {})
        if self._resolved_axis == 'Wavelength':
            self._x_label = 'Wavelength (nm)'
        elif self._resolved_axis in ('B', 'B-field'):
            self._x_label = 'B-field (T)'
        else:
            self._x_label = plot_x_axis_label(self._resolved_axis, float(params.get('derived_ratio', params.get('ratio', 1))), params.get('derived_ratio_target', params.get('ratio_target', 'Vbg')))

    @Slot('QVariantMap')
    def beginRun(self, snapshot):
        self._channel_order = None
        self._channel_labels = {}
        self._snapshot = dict(snapshot)
        self._plan = dict(snapshot.get('planned_ranges', snapshot.get('ranges', {})))
        if not self._plan and snapshot.get('points'):
            for axis, key in AXIS_KEYS.items():
                bound = finite_bounds([point.get(key, math.nan) for point in snapshot['points']])
                if bound is not None:
                    self._plan[axis] = bound
        if 'planned_x_range' in snapshot:
            self._plan[snapshot.get('x_axis', 'Step Index')] = snapshot['planned_x_range']
        self._traces = []
        self._heatmap = None
        self._group = None
        self._count = 0
        self._run = True
        self._y_label = ''
        self._z_label = ''
        self._title = str(snapshot.get('title', ''))
        self._channel = str(snapshot.get('plot_choice', self._channel))
        params = snapshot.get('params', {})
        available = plot_channel_options(params.get('vds_source', ''), dual_enabled(snapshot.get('signal_chain'))) if params else DEFAULT_CHANNELS
        self._channels = list(snapshot.get('channels', available))
        if self._channel not in self._channels:
            self._channel = self._channels[0] if self._channels else 'Ids_DC'
        self._resolve_axis()
        self.home()

    @Slot('QVariantMap')
    def appendRecord(self, record):
        if not self._run:
            self.beginRun({})
        record = dict(record)
        group = (record.get('condition', record.get('condition_index')), record.get('direction', record.get('fast_direction', '')),
                 record.get('pass_index'), record.get('segment'), record.get('batch_index'))
        new_group = self._group != group or not self._traces
        if new_group:
            condition = '' if group[0] is None else f'Condition {group[0]}'
            try:
                source_index = max(0, int(group[0]) - 1) if group[0] is not None else 0
            except (ValueError, TypeError):
                source_index = 0
            self._traces.append(Trace(label=str(record.get('condition_name') or condition),
                                      direction=str(group[1]), source_index=source_index))
            self._group = group
        values = {}
        for key, value in record.items():
            if key in {'direction', 'fast_direction', 'condition', 'segment'}:
                continue
            try:
                canonical = {'Vtg': 'vtg', 'Vbg': 'vbg', 'Vds': 'vds', 'Doping': 'doping',
                             'E-field': 'efield', 'Efield': 'efield'}.get(key, key)
                values[canonical] = float(value) if value is not None else math.nan
            except (TypeError, ValueError):
                pass
        values.setdefault('index', float(self._count))
        values.setdefault('x', float(self._count))
        self._traces[-1].append(values)
        self._count += 1
        self._queue(metadata=new_group)

    @Slot('QVariantMap')
    def finishRun(self, result):
        """Update frozen-export status while retaining the final acquired view."""
        for key in ('status', 'detail', 'created_at'):
            if key in result:
                self._snapshot[key] = deepcopy(result[key])
        self._queue(True)

    def setTraces(self, traces, xLabel='', yLabel='', channelLabels=None):
        self._channel_labels = dict(channelLabels or {})
        result = []
        for trace in traces:
            x, y = (np.asarray(trace[k], dtype=float).reshape(-1) for k in ('x', 'y'))
            if len(x) != len(y):
                raise ValueError('Trace X and Y lengths must match')
            result.append(Trace({'x': NumericBuffer(x), 'y': NumericBuffer(y)},
                                str(trace.get('label', '')), str(trace.get('direction', '')),
                                str(trace.get('channel', '')), str(trace.get('color', ''))))
        self._traces, self._heatmap, self._run = result, None, False
        self._plan = {}
        self._x_label, self._y_label = str(xLabel), str(yLabel)
        self._count = sum(t.columns['x'].size for t in result)
        self._queue(True)

    def setHeatmap(self, data, xLabel='', yLabel='', zLabel=''):
        self._heatmap = prepare_heatmap(data)
        bounds = finite_bounds(self._heatmap['z'])
        if bounds is not None:
            self._last_auto_clim = padded_limits(bounds) if bounds[0] == bounds[1] else bounds
        self._traces, self._run = [], False
        self._plan = {}
        self._x_label = str(xLabel or data.get('xLabel', 'X'))
        self._y_label = str(yLabel or data.get('yLabel', 'Y'))
        self._z_label = str(zLabel or data.get('zLabel', 'Signal'))
        self._count = int(np.isfinite(self._heatmap['z']).sum())
        self._queue(True)

    @Slot()
    def clear(self):
        self._traces, self._heatmap, self._count = [], None, 0
        self._snapshot, self._plan, self._group = {}, {}, None
        self._run = False
        self.home()

    @Slot()
    def home(self):
        self._manual_x, self._manual_y = None, {}
        self.viewChanged.emit()
        self._queue(True)

    def channels(self):
        if self._heatmap is not None or not self._four:
            return [self._channel]
        if self._channel_order is not None:
            return self._channel_order.copy()
        # Row-major display order keeps Drag X/Y in the left column and Drive
        # X/Y in the right column, matching the established overview semantics.
        channels = [name for name in ('I_drag_X', 'I_drive_X', 'I_drag_Y', 'I_drive_Y') if name in self._channels]
        if not channels:
            channels = [name for name in ('Ids_DC', 'Ids_Keithley', 'Ids_Y', 'Ids_X') if name in self._channels]
        return (channels + [''] * 4)[:4]

    def channelLabel(self, channel):
        if self._heatmap is not None or not self._four:
            return self.yLabel
        return self._channel_labels.get(channel, f'{channel} (A)')

    def traces(self, channel):
        for trace in self._traces:
            if self._run:
                xkey = AXIS_KEYS.get(self._resolved_axis, 'x')
                if xkey not in trace.columns:
                    xkey = 'x'
                if channel not in trace.columns:
                    continue
                yield trace, trace.columns[xkey], trace.columns[channel]
            elif not trace.channel or trace.channel == channel:
                yield trace, trace.columns['x'], trace.columns['y']

    def limits(self, channel=None):
        channel = channel or self._channel
        if self._heatmap is not None:
            h = self._heatmap
            xb, yb = (cell_edges(h[k])[[0, -1]] for k in ('x', 'y'))
            xlim = tuple(xb) if self._manual_x is None else self._manual_x
            ylim = tuple(yb) if channel not in self._manual_y else self._manual_y[channel]
            return xlim, ylim
        # Every panel shares the X union, each Y uses its own samples.
        xbounds = merge_bounds(x.bounds for name in self.channels() for _, x, _ in self.traces(name))
        if self._range_mode == FULL_SWEEP:
            xbounds = merge_bounds([xbounds, self._plan.get(self._resolved_axis)])
        ybounds = merge_bounds(y.bounds for _, _, y in self.traces(channel))
        xlim = self._manual_x if self._manual_x is not None else padded_limits(xbounds or (0, 1))
        ylim = self._manual_y.get(channel, padded_limits(ybounds or (0, 1)))
        return xlim, ylim

    def set_limits(self, channel, xlim, ylim):
        self._manual_x = tuple(sorted(float(v) for v in xlim))
        self._manual_y[channel] = tuple(sorted(float(v) for v in ylim))
        self.viewChanged.emit()
        self._queue()

    @Slot(str)
    def setColorMap(self, name):
        self._cmap = next((key for key in COLOR_TABLES if key.lower() == str(name).lower()), 'viridis')
        self.preferencesChanged.emit()
        self.viewChanged.emit()
        self._queue(True)

    @Slot(float, float)
    def setColorLimits(self, low, high):
        if not math.isfinite(low) or not math.isfinite(high) or low >= high:
            raise ValueError('Color limits must be finite and increasing')
        self._clim = (float(low), float(high))
        self.viewChanged.emit()
        self._queue(True)

    @Slot()
    def autoColorLimits(self):
        self._clim = None
        self.viewChanged.emit()
        self._queue(True)

    def color_limits(self):
        if self._clim is not None:
            return self._clim
        if self._heatmap is None:
            return (0., 1.)
        xlim, ylim = self.limits()
        h = self._heatmap
        xi = (h['x'] >= xlim[0]) & (h['x'] <= xlim[1])
        yi = (h['y'] >= ylim[0]) & (h['y'] <= ylim[1])
        bounds = finite_bounds(h['z'][np.ix_(yi, xi)])
        if bounds is not None:
            self._last_auto_clim = padded_limits(bounds) if bounds[0] == bounds[1] else bounds
        return self._last_auto_clim

    @Slot(result='QVariantMap')
    def captureView(self):
        channels = self.channels()
        xlim, _ = self.limits()
        ylims = [list(self.limits(c)[1]) for c in channels]
        view = {'xlim': list(xlim), 'ylim': ylims, 'manual_x': self._manual_x is not None,
                'manual_y': [c in self._manual_y for c in channels], 'clim': list(self.color_limits()),
                'clim_auto': self._clim is None, 'cmap': self._cmap,
                'map_xlim': list(xlim), 'map_ylim': ylims[0], 'xRangeMode': self._range_mode}
        if self._four and self._heatmap is None:
            view.update(four_channels=True, channel_order=channels,
                        channel_views={c: {'xlim': list(self.limits(c)[0]), 'ylim': list(self.limits(c)[1]),
                                           'manual_x': self._manual_x is not None,
                                           'manual_y': [c in self._manual_y]} for c in channels})
        return view

    @Slot(result='QVariantMap')
    def capturePreferences(self):
        """Small per-measurement settings; no samples, plans or manual zoom."""
        return {'channel': self._channel, 'fourChannels': self._four,
                'xAxis': self._x_axis, 'xRangeMode': self._range_mode,
                'interactionMode': self._interaction, 'colorMap': self._cmap}

    @Slot('QVariantMap')
    def applyPreferences(self, preferences):
        """Accept native settings and narrow legacy parameter aliases."""
        p = dict(preferences)
        channel = str(p.get('channel', p.get('plot_choice', self._channel)))
        if not channel:
            raise ValueError('Choose a current channel')
        axis = normalize_plot_x_selection(p.get('xAxis', p.get('plot_x_axis', self._x_axis)))
        mode = p.get('xRangeMode', p.get('x_range_mode', self._range_mode))
        if mode == 'Acquired range':
            mode = ACQUIRED_DATA
        if mode not in (FULL_SWEEP, ACQUIRED_DATA):
            raise ValueError('X range must be Full sweep or Acquired data')
        layout = p.get('fourChannels', p.get('plot_mode', self._four))
        four = layout if isinstance(layout, bool) else str(layout) in ('4-Channel Compare', 'true', 'True', '1')
        cmap = str(p.get('colorMap', p.get('cmap', self._cmap)))
        cmap = next((key for key in COLOR_TABLES if key.lower() == cmap.lower()), 'viridis')
        if axis != self._x_axis or mode != self._range_mode:
            self._manual_x = None
        if channel != self._channel:
            self._manual_y.clear()
        self._channel, self._four, self._x_axis, self._range_mode = channel, bool(four), axis, mode
        self._interaction = 'zoom' if p.get('interactionMode', self._interaction) == 'zoom' else 'pan'
        self._cmap = cmap
        if self._run or not self._traces:
            self._resolve_axis()
        self.preferencesChanged.emit()
        self._queue(True)

    def exportSnapshot(self):
        """Freeze displayed full-resolution data for the detached PNG renderer.

        Every numeric array is copied before the serialization worker receives
        it. Later acquisition, buffer growth and Home/pan cannot alter this
        snapshot. No CSV is reread, so a partially flushed live run is supported.
        """
        def split_label(label, default_name, default_unit):
            if label.endswith(')') and ' (' in label:
                name, unit = label.rsplit(' (', 1)
                return name, unit[:-1]
            return label or default_name, default_unit

        def frozen(values):
            array = np.asarray(values, dtype=float).copy()
            array.setflags(write=False)
            return array

        snapshot = deepcopy(self._snapshot)
        kind = snapshot.get('measurement', snapshot.get('mode', 'gate_scan'))
        kind = 'map_2d' if kind == 'map' else kind
        source_paths = snapshot.get('csv_paths') or [snapshot.get('csv_path', str(Path('live-plot.csv').absolute()))]
        sources = []
        for path in source_paths:
            sources.append({'path': str(path), 'created_at': snapshot.get('created_at', ''),
                            'status': snapshot.get('status', 'running' if self._run else 'preview'),
                            'fingerprint': [0, 0], 'metadata': deepcopy(snapshot)})
        x_name, x_unit = split_label(self._x_label, self._resolved_axis, '')
        signal, y_unit = split_label(self._y_label, self._channel, 'A')
        view = self.captureView()
        view.update(normalization='raw', legend=True, direction='All', **{'pass': 'All rows'})
        view['ylim'] = view['ylim'][0]
        result = {'mode': 'curve', 'kind': kind, 'signal': signal, 'x_name': x_name,
                  'x_unit': x_unit, 'y_unit': y_unit, 'records': sources,
                  'traces': [], 'warnings': [], 'view': view}

        def panel(channel):
            data = {**result, 'signal': channel if self._run or self._four else signal, 'traces': [],
                    'view': {**view, 'ylim': list(self.limits(channel)[1])}}
            for trace, x, y in self.traces(channel):
                path_index = max(0, min(len(source_paths) - 1, trace.source_index))
                direction = trace.direction.lower()
                if direction in ('reverse', 'backwards', '-1', 'bwd'):
                    direction = 'backward'
                if direction in ('forwards', '1', 'fwd'):
                    direction = 'forward'
                data['traces'].append({'path': str(source_paths[path_index]), 'x': frozen(x.values),
                                       'y': frozen(y.values), 'direction': direction or 'unknown',
                                       'condition': trace.label})
            return data

        if self._heatmap is not None:
            yn, yu = split_label(self._y_label, 'Y', '')
            sn, su = split_label(self._z_label, self._channel, 'A')
            result.update(mode='map', signal=sn, y_unit=su,
                          map={'path': str(source_paths[0]), 'x_name': x_name, 'y_name': yn,
                               'x_unit': x_unit, 'y_unit': yu, 'signal_unit': su,
                               **{key: frozen(value) for key, value in self._heatmap.items()}})
        else:
            current = panel(self._channel)
            result['traces'] = current['traces']
            if self._four:
                result['panels'] = {channel: panel(channel) for channel in self.channels()}
                result['view'].update(four_channels=True, channel_order=self.channels(),
                                      channel_views={c: {'xlim': list(self.limits(c)[0]),
                                                         'ylim': list(self.limits(c)[1]),
                                                         'manual_x': self._manual_x is not None,
                                                         'manual_y': [c in self._manual_y]} for c in self.channels()})
                if not result['traces']:
                    result['traces'] = next((p['traces'] for p in result['panels'].values() if p['traces']), [])
        if not result['traces'] and self._heatmap is None:
            raise ValueError('No displayed samples to export')
        return result

    @Slot('QVariantMap')
    def applyView(self, view):
        def limits(value):
            if value is None or len(value) != 2:
                return None
            low, high = (float(v) for v in value)
            if not math.isfinite(low) or not math.isfinite(high) or low >= high:
                raise ValueError('Axis limits must be finite and increasing')
            return low, high
        if 'four_channels' in view:
            self._four = bool(view['four_channels'])
            self._channel_order = list(view['channel_order']) if self._four and view.get('channel_order') else None
        if 'signal' in view:
            self._channel = str(view['signal'])
        if 'xlim' in view or 'map_xlim' in view:
            self._manual_x = limits(view.get('xlim', view.get('map_xlim'))) if view.get('manual_x', True) else None
        ys = view.get('ylim')
        if ys is None and 'map_ylim' in view:
            ys = [view['map_ylim']]
        if ys and isinstance(ys[0], (int, float)):
            ys = [ys]
        if ys is not None:
            self._manual_y.clear()
            flags = view.get('manual_y', [True] * len(ys))
            for c, bound, manual in zip(self.channels(), ys, flags):
                if manual and (value := limits(bound)) is not None:
                    self._manual_y[c] = value
        if self._four and view.get('channel_views'):
            self._manual_y.clear()
            for c in self.channels():
                panel = view['channel_views'].get(c, {})
                if panel.get('manual_y', [True])[0] and (value := limits(panel.get('ylim'))) is not None:
                    self._manual_y[c] = value
        if 'clim' in view:
            self._clim = None if view.get('clim_auto', False) else limits(view['clim'])
        if 'cmap' in view:
            self._cmap = str(view['cmap'])
        if view.get('xRangeMode') in (FULL_SWEEP, ACQUIRED_DATA):
            self._range_mode = view['xRangeMode']
        self.viewChanged.emit()
        self._queue(True)


def _number(value):
    return f'{value:.4g}'


class QuickPlot(QQuickPaintedItem):
    modelChanged = Signal()
    themeChanged = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._model = None
        self._pressed = None
        self._drag = None
        self._cache = {}
        self._run_cache = {}
        self._image_cache = None
        self._dark = False
        self.setAntialiasing(True)
        self.setAcceptedMouseButtons(Qt.MouseButton.LeftButton | Qt.MouseButton.MiddleButton)
        self.setAcceptHoverEvents(True)

    def _set_model(self, model):
        if model is self._model:
            return
        if self._model is not None:
            try:
                self._model.frameReady.disconnect(self._request_update)
                self._model.changed.disconnect(self._request_update)
            except RuntimeError:
                # QML can clear its bindings after the owning backend has been
                # deleted during window/engine teardown.
                pass
        self._model = model
        self._cache.clear()
        self._run_cache.clear()
        if model is not None:
            model.frameReady.connect(self._request_update)
            model.changed.connect(self._request_update)
        self._request_update()
        self.modelChanged.emit()
    model = Property(QObject, lambda s: s._model, _set_model, notify=modelChanged)

    def _set_dark(self, value):
        self._dark = bool(value)
        self._request_update()
        self.themeChanged.emit()
    darkMode = Property(bool, lambda s: s._dark, _set_dark, notify=themeChanged)

    def _curve_color(self, trace, index):
        return QColor(trace.color or (COLORS_DARK if self._dark else COLORS)[index % len(COLORS)])

    def _request_update(self):
        # Path keys carry data revision, view and panel geometry. Retain paths
        # for completed sweeps when only the current acquisition trace changes.
        self._image_cache = None
        self.update()

    def panels(self):
        model = self._model
        channels = model.channels() if model else ['Ids_DC']
        columns = 2 if len(channels) == 4 else 1
        rows = 2 if len(channels) == 4 else 1
        right = 92 if model and model.isHeatmap else 20
        w = max(1., (self.width() - 14) / columns)
        h = max(1., (self.height() - 12) / rows)
        return [(c, QRectF((i % columns) * w + 78, (i // columns) * h + 28,
                           max(1., w - 78 - right), max(1., h - 82)))
                for i, c in enumerate(channels)]

    def _hit(self, position):
        return next(((c, r) for c, r in self.panels() if r.contains(position)), None)

    def _data_point(self, position, rect, xlim, ylim):
        return (xlim[0] + (position.x() - rect.left()) / rect.width() * (xlim[1] - xlim[0]),
                ylim[1] - (position.y() - rect.top()) / rect.height() * (ylim[1] - ylim[0]))

    def paint(self, painter):
        painter.fillRect(self.boundingRect(), QColor('#1d2532' if self._dark else '#ffffff'))
        painter.setFont(QFont('Segoe UI', 9))
        if self._model is None:
            return
        active = {(id(trace), channel) for channel in self._model.channels()
                  for trace, _, _ in self._model.traces(channel)}
        for cache in (self._cache, self._run_cache):
            for key in cache.keys() - active:
                del cache[key]
        for channel, rect in self.panels():
            self._paint_panel(painter, channel, rect)
        if self._drag is not None and self._pressed and self._pressed['mode'] == 'zoom':
            painter.setPen(QPen(QColor('#2978d5'), 1, Qt.PenStyle.DashLine))
            painter.setBrush(QColor(41, 120, 213, 25))
            painter.drawRect(QRectF(self._pressed['position'], self._drag).normalized())

    def _paint_panel(self, painter, channel, rect):
        model = self._model
        xlim, ylim = model.limits(channel)
        painter.setPen(QColor('#344154' if self._dark else '#dce3ed'))
        for i in range(6):
            x = rect.left() + rect.width() * i / 5
            y = rect.bottom() - rect.height() * i / 5
            painter.drawLine(QPointF(x, rect.top()), QPointF(x, rect.bottom()))
            painter.drawLine(QPointF(rect.left(), y), QPointF(rect.right(), y))
        painter.setPen(QColor('#b8c7db' if self._dark else '#465469'))
        painter.drawRect(rect)
        for i in range(6):
            x = rect.left() + rect.width() * i / 5
            y = rect.bottom() - rect.height() * i / 5
            painter.drawText(QRectF(x - 42, rect.bottom() + 6, 84, 18), Qt.AlignmentFlag.AlignHCenter, _number(xlim[0] + (xlim[1] - xlim[0]) * i / 5))
            painter.drawText(QRectF(rect.left() - 68, y - 9, 60, 18), Qt.AlignmentFlag.AlignRight, _number(ylim[0] + (ylim[1] - ylim[0]) * i / 5))
        painter.drawText(QRectF(rect.left(), rect.bottom() + 28, rect.width(), 22), Qt.AlignmentFlag.AlignHCenter, model.xLabel)
        painter.save()
        painter.translate(rect.left() - 63, rect.center().y())
        painter.rotate(-90)
        painter.drawText(QRectF(-rect.height() / 2, -15, rect.height(), 22), Qt.AlignmentFlag.AlignHCenter,
                         model.channelLabel(channel))
        painter.restore()
        title = model.title or (channel if model.fourChannels and not model.isHeatmap else '')
        painter.drawText(QRectF(rect.left(), rect.top() - 24, rect.width(), 20), Qt.AlignmentFlag.AlignHCenter, title)
        painter.save()
        painter.setClipRect(rect)
        if model.isHeatmap:
            painter.drawImage(rect, self._heatmap_image(rect, xlim, ylim))
        else:
            for index, (trace, x, y) in enumerate(model.traces(channel)):
                slot = (id(trace), channel)
                data_key = (x, y, trace.revision)
                key = (data_key, tuple(xlim), tuple(ylim), rect.x(), rect.y(), rect.width(), rect.height())
                cached = self._cache.get(slot)
                path = cached[1] if cached and cached[0] == key else None
                if path is None:
                    indexed = self._run_cache.get(slot)
                    if indexed is None or indexed[0] != data_key:
                        indexed = (data_key, _finite_runs(x.values, y.values))
                        self._run_cache[slot] = indexed
                    path = QPainterPath()
                    for xx, yy in envelope_segments(x.values, y.values, xlim, rect.width(), runs=indexed[1]):
                        px = rect.left() + (xx - xlim[0]) / (xlim[1] - xlim[0]) * rect.width()
                        py = rect.bottom() - (yy - ylim[0]) / (ylim[1] - ylim[0]) * rect.height()
                        if len(px):
                            path.moveTo(float(px[0]), float(py[0]))
                            if len(px) == 1:
                                path.lineTo(float(px[0]) + .3, float(py[0]))
                            for a, b in zip(px[1:], py[1:]):
                                path.lineTo(float(a), float(b))
                    # One path per displayed trace/channel bounds cache growth
                    # during long drags; buffers are shared with the model.
                    self._cache[slot] = key, path
                backwards = trace.direction.lower() in ('backward', 'backwards', 'reverse', '-1', 'bwd')
                pen = QPen(self._curve_color(trace, index), 1.6,
                           Qt.PenStyle.DashLine if backwards else Qt.PenStyle.SolidLine)
                pen.setCosmetic(True)
                painter.setPen(pen)
                painter.setBrush(Qt.BrushStyle.NoBrush)
                painter.drawPath(path)
                # Markers are rendered only on small traces, preserving legible
                # individual measured points without million-point overdraw.
                if x.size <= 200:
                    painter.setBrush(pen.color())
                    for xx, yy in zip(x.values, y.values):
                        if math.isfinite(xx) and math.isfinite(yy):
                            px = rect.left() + (xx - xlim[0]) / (xlim[1] - xlim[0]) * rect.width()
                            py = rect.bottom() - (yy - ylim[0]) / (ylim[1] - ylim[0]) * rect.height()
                            painter.drawEllipse(QPointF(px, py), 2.1, 2.1)
        painter.restore()
        if model.isHeatmap:
            self._paint_colorbar(painter, rect)
        else:
            legends = [(trace, i) for i, (trace, _, _) in enumerate(model.traces(channel)) if trace.label]
            if legends:
                available = max(1, min(4, int(rect.height() / 36)))
                shown = legends[:available]
                for row, (trace, i) in enumerate(shown):
                    text = model.legend_text(trace)
                    tw = min(rect.width() - 16, painter.fontMetrics().horizontalAdvance(text) + 18)
                    box = QRectF(rect.right() - tw - 5, rect.top() + 5 + row * 18, tw, 18)
                    painter.fillRect(box, QColor(29, 37, 50, 230) if self._dark else QColor(255, 255, 255, 230))
                    painter.setPen(self._curve_color(trace, i))
                    painter.drawText(box.adjusted(4, 0, -2, 0), Qt.AlignmentFlag.AlignVCenter,
                                     painter.fontMetrics().elidedText(text, Qt.TextElideMode.ElideRight, int(tw - 6)))
                if len(legends) > available:
                    text = f'+{len(legends) - available} more · View for full legend'
                    box = QRectF(rect.left() + 4, rect.top() + 5 + available * 18, rect.width() - 8, 18)
                    painter.fillRect(box, QColor('#1d2532' if self._dark else '#ffffff'))
                    painter.setPen(QColor('#b8c7db' if self._dark else '#465469'))
                    painter.drawText(box, Qt.AlignmentFlag.AlignRight, painter.fontMetrics().elidedText(text, Qt.TextElideMode.ElideRight, int(box.width())))
            if not any(y.bounds is not None for _, _, y in model.traces(channel)):
                painter.setPen(QColor('#b8c7db' if self._dark else '#465469'))
                painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, 'No data for this channel')

    def _heatmap_image(self, rect, xlim, ylim):
        model, h = self._model, self._model._heatmap
        width, height = max(1, int(rect.width())), max(1, int(rect.height()))
        clim = model.color_limits()
        key = (model.revision, width, height, tuple(xlim), tuple(ylim), clim, model._cmap)
        if self._image_cache and self._image_cache[0] == key:
            return self._image_cache[1]
        ex, ey = cell_edges(h['x']), cell_edges(h['y'])
        px = np.linspace(xlim[0], xlim[1], width, endpoint=False) + (xlim[1] - xlim[0]) / width / 2
        py = np.linspace(ylim[1], ylim[0], height, endpoint=False) - (ylim[1] - ylim[0]) / height / 2
        xi = np.searchsorted(ex, px, side='right') - 1
        yi = np.searchsorted(ey, py, side='right') - 1
        in_x = (xi >= 0) & (xi < len(h['x']))
        in_y = (yi >= 0) & (yi < len(h['y']))
        values = h['z'][np.ix_(np.clip(yi, 0, len(h['y']) - 1), np.clip(xi, 0, len(h['x']) - 1))]
        valid = np.isfinite(values) & in_y[:, None] & in_x[None, :]
        normalized = np.nan_to_num((values - clim[0]) / (clim[1] - clim[0]), nan=0., posinf=1., neginf=0.)
        indices = np.minimum((np.clip(normalized, 0, 1) * 256).astype(np.int32), 255)
        rgba = np.full((height, width, 4), 255, dtype=np.uint8)
        rgba[:, :, :3] = color_table(model._cmap)[indices]
        rgba[~valid] = (29, 37, 50, 255) if self._dark else (255, 255, 255, 255)
        image = QImage(rgba.data, width, height, width * 4, QImage.Format.Format_RGBA8888).copy()
        self._image_cache = key, image
        return image

    def _paint_colorbar(self, painter, rect):
        table = color_table(self._model._cmap)
        bar = QRectF(rect.right() + 13, rect.top(), 15, rect.height())
        for i in range(256):
            painter.fillRect(QRectF(bar.left(), bar.bottom() - (i + 1) * bar.height() / 256,
                                    bar.width(), bar.height() / 256 + 1), QColor(*[int(v) for v in table[i]]))
        painter.setPen(QColor('#b8c7db' if self._dark else '#465469'))
        low, high = self._model.color_limits()
        for frac in (0., .5, 1.):
            painter.drawText(QRectF(bar.right() + 4, bar.bottom() - frac * bar.height() - 9, 58, 18),
                             Qt.AlignmentFlag.AlignLeft, _number(low + frac * (high - low)))
        painter.save()
        painter.translate(bar.right() + 68, bar.center().y())
        painter.rotate(-90)
        painter.drawText(QRectF(-bar.height() / 2, -12, bar.height(), 22), Qt.AlignmentFlag.AlignHCenter, self._model.zLabel)
        painter.restore()

    def mousePressEvent(self, event):
        if self._model is None or (hit := self._hit(event.position())) is None:
            event.ignore()
            return
        channel, rect = hit
        self._pressed = {'channel': channel, 'rect': rect, 'position': event.position(),
                         'limits': self._model.limits(channel),
                         'mode': 'pan' if event.button() == Qt.MouseButton.MiddleButton else self._model.interactionMode}
        self._drag = event.position()
        event.accept()

    def mouseMoveEvent(self, event):
        if not self._pressed:
            return
        self._drag = event.position()
        p = self._pressed
        if p['mode'] == 'pan':
            xlim, ylim = p['limits']
            a = self._data_point(p['position'], p['rect'], xlim, ylim)
            b = self._data_point(event.position(), p['rect'], xlim, ylim)
            self._model.set_limits(p['channel'], [v + a[0] - b[0] for v in xlim], [v + a[1] - b[1] for v in ylim])
        self.update()
        event.accept()

    def mouseReleaseEvent(self, event):
        if not self._pressed:
            return
        p = self._pressed
        distance = (event.position() - p['position']).manhattanLength()
        if p['mode'] == 'zoom' and distance >= 8:
            a = self._data_point(p['position'], p['rect'], *p['limits'])
            b = self._data_point(event.position(), p['rect'], *p['limits'])
            if abs(a[0] - b[0]) > 1e-30 and abs(a[1] - b[1]) > 1e-30:
                self._model.set_limits(p['channel'], [a[0], b[0]], [a[1], b[1]])
        elif distance < 5 and self._model.isHeatmap:
            x, y = self._data_point(event.position(), p['rect'], *p['limits'])
            h = self._model._heatmap
            self._model.cutRequested.emit(float(h['x'][np.argmin(abs(h['x'] - x))]), float(h['y'][np.argmin(abs(h['y'] - y))]))
        self._pressed, self._drag = None, None
        self.update()
        event.accept()

    def mouseDoubleClickEvent(self, event):
        if self._model:
            self._model.home()
            event.accept()

    def wheelEvent(self, event):
        if self._model is None or (hit := self._hit(event.position())) is None:
            event.ignore()
            return
        channel, rect = hit
        xlim, ylim = self._model.limits(channel)
        x, y = self._data_point(event.position(), rect, xlim, ylim)
        delta = event.angleDelta().y() or event.pixelDelta().y()
        factor = math.exp(-max(-1200, min(1200, delta)) / 600.)
        self._model.set_limits(channel, [x + (v - x) * factor for v in xlim], [y + (v - y) * factor for v in ylim])
        event.accept()


_registered = False


def register_types():
    global _registered
    if not _registered:
        qmlRegisterType(QuickPlot, 'Transport.Plot', 1, 0, 'PlotItem')
        _registered = True
