"""Background planning and live timing presentation; never commands hardware."""
from collections import deque
from copy import deepcopy
from dataclasses import asdict
from datetime import datetime, timedelta
import math
import time

from PySide6.QtCore import QObject, Property, QRunnable, QThreadPool, QTimer, Signal, Slot

from app.quick.historical_timing import HistoryTiming
from app.quick.measurement_timing import estimate_measurement_time
from app.quick.parameters import build_params


def duration(seconds):
    seconds = max(0, math.ceil(seconds))
    hours, seconds = divmod(seconds, 3600)
    minutes, seconds = divmod(seconds, 60)
    return f"{hours} h {minutes} min" if hours else f"{minutes} min {seconds} s" if minutes else f"{seconds} s"


class LiveTiming:
    """Learn whole-run seconds per fraction from positive acquisition intervals."""
    def __init__(self, started):
        self.started = started
        self.completed = 0
        self.fraction = 0.
        self.anchor = None
        self.intervals = deque(maxlen=20)
        self.last_point = started

    def observe(self, fraction, now):
        self.completed += 1
        if fraction is None or not math.isfinite(fraction):
            return
        fraction = min(1., max(0., fraction))
        if self.anchor is not None and fraction <= self.fraction:
            return
        self.last_point = now
        if self.anchor is not None:
            previous, stamp = self.anchor
            if fraction > previous and now - stamp >= .01:
                self.intervals.append((fraction - previous, now - stamp))
        if fraction > self.fraction or self.anchor is None:
            self.anchor = (fraction, now)
        self.fraction = max(self.fraction, fraction)

    def remaining(self, now, cleanup=0.):
        if self.fraction >= 1 or len(self.intervals) < 3:
            return None
        rate = sum(t for _, t in self.intervals) / sum(f for f, _ in self.intervals)
        point_interval = sum(t for _, t in self.intervals) / len(self.intervals)
        age = max(0., now - self.last_point)
        # A stalled move or stability wait must not count down to zero.
        if age > max(15., 2 * point_interval):
            return None
        return max(0., (1 - self.fraction) * rate - min(age, point_interval)) + cleanup


class _EstimateTask(QRunnable):
    def __init__(self, generation, request, history, signal, refresh_history=False):
        super().__init__()
        self.generation, self.request, self.history, self.signal = generation, request, history, signal
        self.refresh_history = refresh_history

    def run(self):
        mode, values, connections, save, backend = self.request
        try:
            if self.refresh_history:
                self.history.invalidate()
            result = estimate_measurement_time(mode, values, connections=connections,
                                               device_id=save.device_id, backend=backend)
            result['source'] = 'Parameter model'
            if result['seconds'] is not None:
                params = asdict(build_params(values, mode))
                if mode == 5:
                    params['magnet_backend'] = backend
                if mode == 4:
                    params.update({k: values[k] for k in ('fields_t', 'conditions', 'field_rate_t_per_min', 'field_settle_s', 'field_approach')})
                kind = ('vds_sweep', 'gate_scan', 'map_2d' if params.get('axis_slow') != 'None' else 'sweep_1d',
                        'photocurrent', 'bfield_gate_scan', 'bfield_transport')[mode]
                historical = self.history.lookup(kind, params, save)
                if historical:
                    result.update(seconds=historical['seconds'], source='Historical measurements',
                                  note=historical['note'], historyPath=historical['source'])
                else:
                    result['note'] = 'No matching completed run found. ' + result['note']
        except Exception as exc:
            result = dict(seconds=None, points=None, cleanup_seconds=0., source='Unavailable', note=str(exc))
        try:
            self.signal.emit(self.generation, result)
        except RuntimeError:
            pass  # Window closed while a read-only lookup was completing.


class MeasurementTiming(QObject):
    changed = Signal()
    completed = Signal(int, object)

    def __init__(self, parent=None, *, clock=time.monotonic):
        super().__init__(parent)
        self.clock = clock
        self._generation = 0
        self._busy = False
        self._pending = None
        self._request = None
        self._plan = {}
        self._history = HistoryTiming()
        self._refresh_history = False
        self._live = None
        self._active = False
        self._phase = ''
        self._paused = False
        self._last_status = ''
        self._elapsed = None
        self._summary = 'Estimated duration: calculating…'
        self._detail = ''
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(250)
        self._timer.timeout.connect(self._launch_pending)
        self._tick = QTimer(self)
        self._tick.setInterval(1000)
        self._tick.timeout.connect(self._render)
        self.completed.connect(self._complete)

    summary = Property(str, lambda s: s._summary, notify=changed)
    detail = Property(str, lambda s: s._detail, notify=changed)
    plan = Property('QVariantMap', lambda s: dict(s._plan), notify=changed)

    def request(self, mode, values, connections, save, backend, invalid=False):
        if self._active:
            return
        self._generation += 1
        self._plan = {}
        self._last_status = ''
        self._elapsed = None
        self._request = deepcopy((mode, values, connections, save, backend))
        self._pending = None if invalid else (self._generation, self._request)
        if invalid:
            self._timer.stop()
            self._summary, self._detail = 'Estimated duration unavailable', 'Correct the highlighted input fields.'
        else:
            self._summary, self._detail = 'Estimated duration: calculating…', 'Checking parameters and comparable completed measurements.'
            self._timer.start()
        self.changed.emit()

    @Slot()
    def _launch_pending(self):
        if self._busy or self._pending is None:
            return
        generation, request = self._pending
        self._pending = None
        self._busy = True
        QThreadPool.globalInstance().start(_EstimateTask(generation, request, self._history, self.completed, self._refresh_history))
        self._refresh_history = False

    @Slot(int, object)
    def _complete(self, generation, result):
        self._busy = False
        if generation == self._generation:
            self._plan = result
            self._render()
        if self._pending is not None:
            self._launch_pending()

    def begin(self):
        self._active = True
        self._live = LiveTiming(self.clock())
        self._phase, self._paused, self._last_status = 'preparing', False, ''
        self._timer.stop()
        self._launch_pending()
        self._tick.start()
        self._render()

    def finish(self, status):
        if not self._active:
            return
        self._active = False
        self._elapsed = self.clock() - self._live.started
        self._last_status = str(status)
        self._refresh_history = True
        self._tick.stop()
        self._render()

    def set_phase(self, phase, paused=False):
        if paused != self._paused and self._live:
            self._live.anchor = None  # Exclude an explicit operator pause from learned speed.
        self._phase, self._paused = phase, paused
        if self._active:
            self._render()

    @Slot(object)
    def record(self, record):
        if not self._active or self._live is None:
            return
        count = self._plan.get('points')
        fraction = (self._live.completed + 1) / count if count else None
        if self._request and self._request[0] == 5 and self._request[1]['measurement_mode'] == 'continuous':
            values = self._request[1]
            span = values['stop_field_t'] - values['start_field_t']
            field = record.get('B_measured_T')
            condition = record.get('Condition_index')
            rows = sum(bool(r.get('enabled', True)) for r in values['conditions'])
            if span and field is not None and condition is not None and rows:
                leg = min(1., max(0., (float(field) - values['start_field_t']) / span))
                backward = str(record.get('Direction', '')).lower() in ('backward', 'reverse')
                legs = 2 if values['round_trip'] else 1
                fraction = (int(condition) - 1 + ((2 - leg) if backward else leg) / legs) / rows
        self._live.observe(fraction, self.clock())
        self._render()

    @Slot()
    def _render(self):
        seconds = self._plan.get('seconds')
        note = self._plan.get('note', '')
        source = self._plan.get('source', 'Parameter model')
        if self._active and self._live:
            now = self.clock()
            elapsed = duration(now - self._live.started)
            remaining = self._live.remaining(now, self._plan.get('cleanup_seconds', 0.))
            hold = self._paused or self._phase in ('stopping', 'cleanup', 'failed', 'error', 'stopped') or 'cleanup' in self._phase
            if hold:
                self._summary = f'Elapsed: {elapsed} · Remaining time pending'
                self._detail = 'Waiting for continuation or safe cleanup; completion time is not yet known.'
            elif self._live.fraction >= 1:
                self._summary = f'Elapsed: {elapsed} · Acquisition complete; finishing'
                self._detail = 'Waiting for safe cleanup and file saving.'
            elif remaining is not None:
                finish = (datetime.now().astimezone() + timedelta(seconds=remaining)).strftime('%Y-%m-%d %H:%M:%S')
                self._summary = f'Elapsed: {elapsed} · Remaining: ~{duration(remaining)} · Finish: ~{finish}'
                self._detail = f'Live estimate from {self._live.completed:,} acquired points. Variable preparation, stabilization and cleanup may extend it.'
            else:
                estimate = f'~{duration(seconds)}' if seconds is not None else 'unavailable'
                self._summary = f'Elapsed: {elapsed} · Pre-start estimate: {estimate}'
                self._detail = ('Waiting for enough points to estimate remaining time.' if len(self._live.intervals) < 3
                                else 'Waiting for the next point; remaining time will update when acquisition resumes.') + f' {source}. {note}'
        else:
            self._summary = f'Estimated duration: ~{duration(seconds)}' if seconds is not None else 'Estimated duration unavailable'
            self._detail = f'{source}. {note}'
            if self._last_status:
                self._summary = f'Last run: {self._last_status} · Elapsed: {duration(self._elapsed)}'
                self._detail = 'Edit parameters or start again to update the next estimate. ' + self._detail
        self.changed.emit()
