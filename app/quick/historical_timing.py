"""Bounded, read-only lookup of comparable completed measurement durations.

Call lookup off the UI thread: even a bounded directory walk can block on storage.
"""
from __future__ import annotations

from dataclasses import asdict, is_dataclass
from datetime import datetime
import json
import math
import os
from pathlib import Path
import re
import statistics
import time

_IGNORED = {'base_name', 'plot_choice', 'plot_x_axis', 'plot_x_resolved',
            'output_csv_path', 'output_metadata_path', 'output_log_path'}
_REQUIRED = {
    'bfield_transport': ('start_field_t', 'stop_field_t', 'measurement_mode', 'field_approach', 'round_trip', 'acquisition_delay_s', 'averages'),
    'gate_scan': ('mode', 'n_points', 'delay', 'n_sample', 'sweep_both_ways'),
    'vds_sweep': ('vds_start', 'vds_stop', 'vds_step', 'delay', 'n_sample', 'sweep_both_ways'),
    'photocurrent': ('wl_start', 'wl_stop', 'wl_step', 'delay', 'n_sample', 'bias_conditions'),
    'sweep_1d': ('axis_fast', 'delay', 'n_sample'),
    'map_2d': ('axis_fast', 'axis_slow', 'delay', 'n_sample'),
}
_PAUSE = re.compile(r'\b(?:user (?:requested )?paus(?:e|ed)|paus(?:e|ed) by user|measurement paused|run paused|resumed by user)\b', re.I)
_START = re.compile(r'^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d) B-field Transport series started\b')


def _equal(a, b):
    if isinstance(a, bool) or isinstance(b, bool):
        return type(a) is type(b) and a == b
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return math.isfinite(a) and math.isfinite(b) and math.isclose(a, b, rel_tol=1e-9, abs_tol=1e-10)
    if isinstance(a, dict) and isinstance(b, dict):
        return (all(k in b and _equal(v, b[k]) for k, v in a.items() if k not in _IGNORED)
                and all(k in a and _equal(v, a[k]) for k, v in b.items() if k not in _IGNORED))
    if isinstance(a, (list, tuple)) and isinstance(b, (list, tuple)):
        return len(a) == len(b) and all(_equal(x, y) for x, y in zip(a, b))
    return a == b


def _datetime(value):
    try:
        # Logs use local clock time; metadata may include local UTC offset.
        return datetime.fromisoformat(value).replace(tzinfo=None)
    except (ValueError, TypeError):
        return None


def _log_text(path):
    try:
        if path.stat().st_size > 4_000_000:
            return None
        return path.read_text(encoding='utf-8-sig', errors='replace')
    except OSError:
        return None


def _batch_matches(data, values):
    from app.numeric_series import parse_numeric_series
    from app.quick.parameters import build_params, defaults
    try:
        raw_fields = values['fields_t']
        fields = list(parse_numeric_series(raw_fields, 'B-field', maximum=1000)) if isinstance(raw_fields, str) else list(raw_fields)
        if not fields or not _equal(fields, data['fields_t']):
            return False
        control = data['field_control']
        if not all(_equal(control[k], values[v]) for k, v in (('field_approach', 'field_approach'),
                   ('rate_t_per_min', 'field_rate_t_per_min'), ('field_settle_s', 'field_settle_s'))):
            return False
        rows = values.get('conditions', [])
        recipes = [asdict(build_params({**defaults(4), **{k: v for k, v in row.items() if k not in ('enabled', 'name')}}, 4))
                   for row in rows if row.get('enabled')] if rows else [asdict(build_params(values, 4))]
        saved = data['conditions']
        if not recipes or len(recipes) != len(saved) or data['condition_count'] != len(recipes):
            return False
        if not all(_equal(recipe, old['frozen_gate_scan_params']) for recipe, old in zip(recipes, saved)):
            return False
        results = data['results']
        return (data['next_job_index'] == len(fields) * len(recipes) and len(results) == data['next_job_index']
                and all(row.get('status') == 'job_complete' for row in results))
    except (KeyError, TypeError, ValueError):
        return False


class HistoryTiming:
    def __init__(self, max_dirs=300, max_files=3000, ttl_s=60):
        self.max_dirs = max_dirs
        self.max_files = max_files
        self.ttl_s = ttl_s
        self._cache = {}

    def invalidate(self):
        """Discard cached scans after a completed run."""
        self._cache.clear()

    def _records(self, root):
        root = str(Path(root).expanduser())
        cached = self._cache.get(root)
        if cached and time.monotonic() - cached[0] < self.ttl_s:
            return cached[1]
        records = []
        dirs = files = 0
        for folder, children, names in os.walk(root, followlinks=False):
            dirs += 1
            if dirs > self.max_dirs:
                break
            children.sort(reverse=True)
            for name in sorted(names, reverse=True):
                files += 1
                if files > self.max_files:
                    break
                if not (name.endswith('_metadata.json') or name.endswith('_series_manifest.json') or name.endswith('_bfield_batch_checkpoint.json')):
                    continue
                path = Path(folder) / name
                try:
                    if path.stat().st_size > 1_000_000:
                        continue
                    data = json.loads(path.read_text(encoding='utf-8-sig'))
                except (OSError, ValueError):
                    continue
                if not isinstance(data, dict) or data.get('status') not in ('finished', 'complete', 'completed'):
                    continue
                is_batch = name.endswith('_bfield_batch_checkpoint.json')
                if not is_batch and not isinstance(data.get('params'), dict):
                    continue
                if is_batch and (data.get('schema') != 'gate_scan_bfield_batch_checkpoint_v1' or data.get('resumed_from')):
                    continue
                is_series = name.endswith('_series_manifest.json')
                log = path.with_name(name[:-len('_series_manifest.json')] + '_series_log.txt' if is_series else
                                     name[:-len('_bfield_batch_checkpoint.json')] + '_bfield_batch_log.txt' if is_batch else
                                     name[:-len('_metadata.json')] + '_run_log.txt')
                text = _log_text(log)
                # A missing log cannot rule out an operator pause.
                if text is None or _PAUSE.search(text):
                    continue
                if is_batch:
                    match = re.search(r'^Started: (.+)$', text, re.M)
                    start = _datetime(match.group(1)) if match else None
                    end = _datetime(data.get('updated_at'))
                    data = dict(data, measurement='bfield_gate_scan')
                elif is_series:
                    match = _START.search(text)
                    start = _datetime(match.group(1)) if match else None
                    end = _datetime(data.get('updated_at'))
                else:
                    start, end = _datetime(data.get('created_at')), _datetime(data.get('completed_at'))
                if not start or not end:
                    continue
                seconds = (end - start).total_seconds()
                if not 0 < seconds <= 7 * 86400:
                    continue
                records.append((data, seconds, str(path), start))
            if files > self.max_files:
                break
        self._cache[root] = (time.monotonic(), records)
        return records

    def lookup(self, mode, values, save_root):
        if is_dataclass(values):
            values = asdict(values)
        if not isinstance(values, dict) or not save_root:
            return None
        if not isinstance(save_root, (str, os.PathLike)):
            # SaveRoot.path() creates directories, so reproduce its path calculation.
            base = getattr(save_root, 'base', None)
            user = getattr(save_root, 'user', None)
            device = getattr(save_root, 'device_id', None)
            if not all(isinstance(x, str) for x in (base, user, device)):
                return None
            safe_user = re.sub(r'[^-_.A-Za-z0-9]+', '_', user).strip('_') or 'User'
            safe_id = re.sub(r'[^-_.A-Za-z0-9]+', '_', device).strip('_') or 'device'
            save_root = os.path.join(base, safe_user, safe_id)
        if not save_root:
            return None
        matches = []
        for data, seconds, source, stamp in self._records(save_root):
            kind = data.get('measurement')
            if kind == mode == 'bfield_gate_scan':
                if _batch_matches(data, values):
                    matches.append((seconds, source, stamp))
                continue
            if kind != mode or kind not in _REQUIRED:
                continue
            old = data['params']
            if any(k not in old or k not in values or not _equal(old[k], values[k]) for k in _REQUIRED[kind]):
                continue
            # An absent newer setting cannot establish an equivalent recipe.
            if (any(k not in values or not _equal(v, values[k]) for k, v in old.items() if k not in _IGNORED)
                    or any(k not in old or not _equal(v, old[k]) for k, v in values.items() if k not in _IGNORED)):
                continue
            matches.append((seconds, source, stamp))
        if not matches:
            return None
        matches.sort(key=lambda row: row[2], reverse=True)
        matches = matches[:5]
        return {'seconds': statistics.median(row[0] for row in matches), 'points': None,
                'source': matches[0][1], 'note': f'Comparable completed runs: {len(matches)}; median ' + ('series time through completion checkpoint' if mode == 'bfield_gate_scan' else 'wall time')}
