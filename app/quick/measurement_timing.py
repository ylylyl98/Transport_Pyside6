"""Read-only, bounded planning estimates; no instrument I/O is performed."""
from __future__ import annotations

import math

from app.quick.parameters import build_params
from app.workers.cosweep import build_cosweep_points, sequence_point_count
from app.workers.cosweep_timing import (estimate_cosweep_seconds, estimate_cosweep_cleanup_seconds,
                                       format_cosweep_duration)
from app.constants import GATE_BIAS_RAMP_STEP_T, GATE_BIAS_RAMP_STEP_V, SAFE_RAMP_STEP_T, SAFE_RAMP_STEP_V

_LIMIT = 10000
_SAMPLE_S = .01  # planning allowance per DAQ read; not a hardware measurement


def _number(value, *, positive=False):
    value = float(value)
    if not math.isfinite(value) or value < 0 or (positive and value <= 0):
        raise ValueError('invalid timing or step value')
    return value


def _count(start, stop, step):
    _number(step, positive=True)
    count = sequence_point_count(start, stop, step)
    if count > _LIMIT:
        raise ValueError('plan exceeds preview limit')
    return count


def _ramp(a, b, step, seconds):
    return math.ceil(abs(float(b)-float(a)) / _number(step, positive=True) - 1e-9) * seconds


def _line(p):
    from app.workers.line_sweep import LineSweepWorker
    n = int(p.n_points)
    if n < 1 or n > _LIMIT:
        raise ValueError('invalid or oversized point count')
    if int(p.n_sample) < 1:
        raise ValueError('averages must be positive')
    delay = _number(p.delay)
    _number(p.vg_ramp, positive=True)
    _number(p.vds_ramp, positive=True)
    worker = LineSweepWorker.__new__(LineSweepWorker)
    worker.p = p
    trajectory = worker._build_trajectory()
    points = trajectory + (list(reversed(trajectory)) if p.sweep_both_ways else [])
    first = points[0]
    elapsed = sum(_ramp(0, first[k], GATE_BIAS_RAMP_STEP_V, GATE_BIAS_RAMP_STEP_T) for k in ('vtg', 'vbg'))
    if p.vds_source != 'None':
        elapsed += _ramp(0, first['vds'], SAFE_RAMP_STEP_V, SAFE_RAMP_STEP_T)
    for a, b in zip(points, points[1:]):
        elapsed += sum(_ramp(a[k], b[k], p.vg_ramp, GATE_BIAS_RAMP_STEP_T) for k in ('vtg', 'vbg'))
        if p.vds_source != 'None':
            elapsed += _ramp(a['vds'], b['vds'], p.vds_ramp, SAFE_RAMP_STEP_T)
    last = points[-1]
    cleanup = sum(_ramp(last[k], 0, SAFE_RAMP_STEP_V, SAFE_RAMP_STEP_T) for k in ('vtg', 'vbg'))
    if p.vds_source != 'None':
        cleanup += _ramp(last['vds'], 0, SAFE_RAMP_STEP_V, SAFE_RAMP_STEP_T)
    elapsed += cleanup
    return elapsed + len(points) * (delay + int(p.n_sample)*_SAMPLE_S), len(points), cleanup


def estimate_measurement_time(mode, values, *, connections=None, device_id='', backend='1000'):
    """Return modeled seconds, display text, caveat, and planned acquisition points.

    Read and instrument-I/O durations are allowances. Initial voltages are assumed
    zero. Field estimates exclude unknown positioning, thermal and SDK delays.
    """
    try:
        mode = int(mode)
        if mode not in range(6):
            raise ValueError('unknown measurement mode')
        p = build_params(values, mode)
        note = 'Approximate planning model; initial voltages and instrument I/O vary. Bias 2 motion and current-read overhead are excluded.'
        points = None
        cleanup_seconds = 0.0
        if mode == 0:
            n = _count(p.vds_start, p.vds_stop, p.vds_step)
            if int(p.n_sample) < 1: raise ValueError('averages must be positive')
            delay = _number(p.delay)
            # Use the actual inclusive step generator, including a shortened last interval.
            from app.utils import _frange_inc
            seq = _frange_inc(p.vds_start, p.vds_stop, p.vds_step if p.vds_stop >= p.vds_start else -p.vds_step)
            if p.sweep_both_ways: seq += list(reversed(seq))
            points = len(seq)
            seconds = points * (delay + int(p.n_sample)*_SAMPLE_S)
            seconds += sum(_ramp(0, x, GATE_BIAS_RAMP_STEP_V, GATE_BIAS_RAMP_STEP_T) + _ramp(x, 0, SAFE_RAMP_STEP_V, SAFE_RAMP_STEP_T) for x in (p.vtg_set, p.vbg_set))
            cleanup_seconds = _ramp(seq[-1], 0, SAFE_RAMP_STEP_V, SAFE_RAMP_STEP_T) + sum(_ramp(x, 0, SAFE_RAMP_STEP_V, SAFE_RAMP_STEP_T) for x in (p.vtg_set, p.vbg_set))
            seconds += _ramp(0, seq[0], SAFE_RAMP_STEP_V, SAFE_RAMP_STEP_T) + _ramp(seq[-1], 0, SAFE_RAMP_STEP_V, SAFE_RAMP_STEP_T)
            seconds += sum(_ramp(a,b,p.vds_ramp,SAFE_RAMP_STEP_T) for a,b in zip(seq,seq[1:]))
        elif mode == 1:
            seconds, points, cleanup_seconds = _line(p)
        elif mode == 2:
            axes = {'Vtg':'vtg', 'Vbg':'vbg', 'Vds':'vds', 'Doping':'doping', 'E-field':'efield'}
            names = [p.axis_fast] + ([p.axis_slow] if p.axis_slow != 'None' else [])
            def size(obj):
                return math.prod(_count(getattr(obj, axes[a]+'_start'), getattr(obj, axes[a]+'_stop'), getattr(obj, axes[a]+'_step')) for a in names)
            total = size(p)
            for region in p.regions:
                # Region steps are shared with the base sweep.
                from types import SimpleNamespace
                merged = vars(p).copy(); merged.update(region)
                total += size(SimpleNamespace(**merged))
            if total > _LIMIT: raise ValueError('plan exceeds preview limit')
            _number(p.delay)
            if int(p.n_sample) < 1: raise ValueError('averages must be positive')
            trajectory = build_cosweep_points(p)
            points = len(trajectory)
            if not points: raise ValueError('empty trajectory')
            seconds = estimate_cosweep_seconds(p, points=trajectory, connections=connections, device_id=device_id)
            cleanup_seconds = estimate_cosweep_cleanup_seconds(p, trajectory[-1])
            note = "Approximate co-sweep model; initial voltages and instrument I/O vary."
        elif mode == 3:
            n = _count(p.wl_start, p.wl_stop, p.wl_step)
            rows = [c for c in p.bias_conditions if c.enabled]
            if not rows: raise ValueError('no enabled conditions')
            if int(p.n_sample) < 1: raise ValueError('averages must be positive')
            points = n * len(rows)
            if points > _LIMIT: raise ValueError('plan exceeds preview limit')
            seconds = points * (_number(p.delay) + int(p.n_sample)*_SAMPLE_S)
            last = [0., 0., 0.]
            for row in rows:
                seconds += _number(row.settle_s)
                current = [row.vtg, row.vbg, row.vds if p.use_vds else 0.]
                seconds += sum(_ramp(last[i], current[i], GATE_BIAS_RAMP_STEP_V if i < 2 else p.vds_ramp, GATE_BIAS_RAMP_STEP_T if i < 2 else SAFE_RAMP_STEP_T) for i in range(3))
                last = current
            cleanup_seconds = sum(_ramp(x, 0, SAFE_RAMP_STEP_V, SAFE_RAMP_STEP_T) for x in last)
            seconds += cleanup_seconds
            note += ' Monochromator movement time is variable.'
        elif mode == 4:
            if values.get('resume_checkpoint'):
                raise ValueError('remaining work in a resumed batch is unknown')
            from app.engine.gate_scan_field_batch import GateScanFieldBatch
            fields = GateScanFieldBatch.parse_fields(values['fields_t'])
            rows = values.get('conditions', [])
            enabled = [r for r in rows if r.get('enabled', True)] if rows else [values]
            if not enabled: raise ValueError('no enabled conditions')
            if len(fields)*len(enabled) > _LIMIT: raise ValueError('plan exceeds preview limit')
            results = [_line(build_params(r, 4)) for r in enabled]
            points = len(fields)*sum(x[1] for x in results)
            if points > _LIMIT: raise ValueError('plan exceeds preview limit')
            seconds = len(fields)*(sum(x[0] for x in results) + _number(values.get('field_settle_s', 0)))
            if str(backend) == '1000':
                rate = _number(values.get('field_rate_t_per_min'), positive=True)
                seconds += sum(abs(b-a) for a, b in zip(fields, fields[1:])) / rate * 60
            cleanup_seconds = results[-1][2]
            note = ('Nominal acquisition and inter-field movement at the configured rate; initial field, stabilization, '
                    'supply transitions, Bias 2 motion and current-read overhead are variable or excluded.')
        else:
            from app.engine.bfield_transport_sweep import TransportSweepPlan, build_stepped_targets
            p.magnet_backend = str(backend)
            if p.measurement_mode == 'stepped':
                # Bound before plan validation, which itself generates the trajectory.
                n = _count(p.start_field_t, p.stop_field_t, p.field_step_t) if p.field_spacing == 'step' else int(p.field_points)
                if n < 2 or n > _LIMIT: raise ValueError('invalid or oversized point count')
                enabled_count = sum(bool(c.enabled) for c in p.conditions)
                if enabled_count * (2*n-1 if p.round_trip else n) > _LIMIT:
                    raise ValueError('plan exceeds preview limit')
            plan = TransportSweepPlan.from_params(p)
            rows = list(plan.conditions)
            if not rows: raise ValueError('no enabled conditions')
            rate = _number(p.rate_t_per_min, positive=True)
            distance = abs(p.stop_field_t-p.start_field_t)
            seconds = len(rows)*distance/rate*60*(2 if p.round_trip else 1)
            seconds += sum(_number(c.settle_s) for c in rows)
            if p.measurement_mode == 'continuous':
                seconds += len(rows)*(_number(p.pre_sweep_settle_s)*(2 if p.round_trip else 1) + (_number(p.turnaround_delay_s) if p.round_trip else 0))
            if p.measurement_mode == 'stepped':
                n = len(build_stepped_targets(p))
                points = len(rows)*(2*n-1 if p.round_trip else n)
                if points > _LIMIT: raise ValueError('plan exceeds preview limit')
                if int(p.averages) < 1: raise ValueError('averages must be positive')
                # The field and temperature windows run concurrently; both must
                # qualify before the separate extra field-settle delay starts.
                qualification = _number(p.temperature_window_s, positive=True)
                if str(backend) == '1000':
                    qualification = max(qualification, _number(p.field_stability_s)) + _number(p.field_settle_s)
                seconds += points*(qualification + _number(p.acquisition_delay_s) + max(0, int(p.averages)-1)*_number(p.read_delay_s) + int(p.averages)*_SAMPLE_S)
                if p.sample_reversal_mode == 'per_point':
                    # Each point switches OFF and back ON, then requalifies.
                    reversal_settle = _number(p.sample_reversal_settle_s)
                    seconds += points * (2*reversal_settle + qualification) + reversal_settle
                    cleanup_seconds = reversal_settle
                    seconds += cleanup_seconds
                elif p.sample_reversal_mode == 'whole_sweep':
                    cleanup_seconds = _number(p.sample_reversal_settle_s)
                    seconds += 2*cleanup_seconds
            note = 'Nominal estimate; initial field positioning, additional field and temperature stabilization, supply transitions, cleanup, Bias 2 motion and SDK timing vary. APS100 stored rate limits can be slower.'
        if not math.isfinite(seconds) or seconds < 0: raise ValueError('invalid estimate')
        return {'seconds': seconds, 'points': points, 'cleanup_seconds': cleanup_seconds, 'text': 'Estimated duration: ~' + format_cosweep_duration(seconds), 'note': note}
    except (ValueError, TypeError, KeyError, AttributeError, OverflowError, ZeroDivisionError, RuntimeError) as exc:
        return {'seconds': None, 'points': None, 'cleanup_seconds': 0.0, 'text': 'Estimated duration unavailable', 'note': str(exc)}
