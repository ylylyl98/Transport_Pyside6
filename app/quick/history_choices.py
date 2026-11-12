"""Resolve analysis selectors from finite, row-aligned saved measurements.

This module is called only by the analysis worker. It does not access hardware or
mutate parsed arrays; the existing bounded SnapshotCache owns availability masks.
"""
from pathlib import Path

import numpy as np

from app.analysis_signals import numeric, numeric_column, signal_values
from app.curve_history import SIGNAL_COLUMNS, X_COLUMNS, cached_csv_snapshot, resolve_column, normalize_direction
from app.curve_map import infer_map_axes


def _availability(record, cache, direction, pass_index, map_2d):
    def build():
        headers, units, rows, _warnings, _conditions = cached_csv_snapshot(record.path, cache, record.fingerprint)
        selected = np.ones(len(rows), dtype=bool)
        direction_names = ('FastDirection', 'Direction') if map_2d else ('Direction', 'FastDirection')
        direction_column = next((headers.index(name) for name in direction_names if name in headers), None)
        if direction != 'All':
            if direction_column is None:
                # Curve loading labels missing direction as unknown; maps skip it.
                if map_2d or direction != 'unknown':
                    selected[:] = False
            else:
                selected &= np.asarray([normalize_direction(str(row[direction_column])) == direction for row in rows], dtype=bool)
        if map_2d and pass_index != 'All rows' and 'PassIndex' in headers:
            values = numeric_column(rows, headers.index('PassIndex'))
            finite = values[np.isfinite(values)]
            chosen = float(np.min(finite)) if pass_index == 'First pass' and len(finite) else numeric(pass_index)
            # With no pass values the renderer's First pass becomes no filter.
            if not (pass_index == 'First pass' and not len(finite)):
                selected &= values == chosen
        axes, signals, varying = {}, {}, set()
        for name in X_COLUMNS:
            if name == 'Step Index':
                axes[name] = selected.copy()
                continue
            column = resolve_column(headers, name)
            if column in headers:
                values = numeric_column(rows, headers.index(column))
                mask = np.isfinite(values) & selected
                if np.any(mask):
                    axes[name] = mask
                    valid = values[mask]
                    if len(valid) > 1 and np.min(valid) != np.max(valid):
                        varying.add(name)
        for name in SIGNAL_COLUMNS:
            try:
                column = resolve_column(headers, name)
                values, _unit, _warnings = signal_values(headers, units, rows, column)
                mask = np.isfinite(values) & selected
                if np.any(mask):
                    signals[name] = mask
            except (ValueError, TypeError, IndexError, OverflowError):
                continue
        return axes, signals, varying, bool(np.any(selected))
    if cache is None:
        return build()
    return cache.get(('availability', str(record.path), record.fingerprint, direction, pass_index, map_2d), build)


def resolve_choices(records, view, kind, cache):
    """Resolve options and safe fallback without changing the caller's view.

    Curves accept a signal/axis if any one file has an overlapping finite row.
    A 2D map uses its selected primary file, matching the rendered map source.
    """
    actual = dict(view)
    records = list(records)
    if not records:
        return dict(signals=[], axes=[], view=actual, warnings=[], note='Select saved files to see available signals and coordinates.')
    is_map = kind == 'map_2d'
    if is_map:
        primary = next((r for r in records if str(r.path) == str(view.get('primary_path', ''))), records[0])
        actual['primary_path'] = str(primary.path)
        # Resolve automatic axes before selecting a source policy: a saved 1D
        # view can become a 2D map (and vice versa) when the primary changes.
        if actual.get('auto_axes'):
            try:
                inferred = infer_map_axes(primary)
                if inferred is None:
                    snapshot = cached_csv_snapshot(primary.path, cache, primary.fingerprint)
                    inferred = infer_map_axes(primary, snapshot)
                if inferred:
                    actual['x'], actual['map_y'] = inferred
            except (OSError, ValueError, TypeError, IndexError, OverflowError):
                pass
    map_2d_filter = is_map and actual.get('map_y') != 'None (1D)'
    selected = [primary] if map_2d_filter else records
    available, warnings = [], []
    direction = str(view.get('direction', 'All'))
    pass_index = str(view.get('pass', 'All rows')) if map_2d_filter else 'All rows'
    for record in selected:
        try:
            axes, signals, varying, has_rows = _availability(record, cache, direction, pass_index, map_2d_filter)
            available.append((record, axes, signals, varying, has_rows))
        except (OSError, ValueError, TypeError, IndexError, OverflowError) as exc:
            warnings.append(f'{Path(record.path).name}: cannot inspect available data ({exc})')
    if not available:
        return dict(signals=[], axes=[], view=actual, warnings=warnings,
                    note='Selected files contain no readable signal data.')

    if not any(has_rows for _, _, _, _, has_rows in available):
        return dict(signals=[], axes=[], view=actual, warnings=warnings,
                    note='No samples match the current direction and pass filters.')

    def overlap(axis, signal, other=None):
        if not axis or not signal or axis == other:
            return False
        for _record, axes, signals, _varying, _has_rows in available:
            if axis not in axes or signal not in signals or (other and other not in axes):
                continue
            mask = axes[axis] & signals[signal]
            if other:
                mask = mask & axes[other]
            if np.any(mask):
                return True
        return False

    axis_order = [a for a in X_COLUMNS if any(a in axes for _, axes, _, _, _ in available)]
    # A changing physical coordinate is more informative than a constant one.
    # Step Index stays available but does not outrank a measured coordinate.
    axis_order.sort(key=lambda a: (not any(a in varying for _, _, _, varying, _ in available), a == 'Step Index'))
    signal_order = [s for s in SIGNAL_COLUMNS if any(s in signals for _, _, signals, _, _ in available)]
    original_x, original_y, original_signal = actual.get('x', ''), actual.get('map_y', ''), actual.get('signal', '')
    # Only use a role-derived default when all selected records agree. A
    # user's still-valid explicit choice always wins over this preference.
    modes = set()
    for record in selected:
        meta = record.metadata or {}
        chain = meta.get('signal_chain') or (meta.get('validation') or {}).get('calibration', {}).get('signal_chain') or {}
        modes.add((chain.get('drag_drive') or {}).get('mode', 'ordinary'))
    role_signal = {'single_drag': 'I_drag_X', 'single_drive': 'I_drive_X'}.get(next(iter(modes)) if len(modes) == 1 else None)
    preferred = []
    if actual.get('signal_explicit') and original_signal in signal_order:
        preferred.append(original_signal)
    if role_signal in signal_order and role_signal not in preferred:
        preferred.append(role_signal)
    if original_signal in signal_order and original_signal not in preferred:
        preferred.append(original_signal)
    signal_pref = preferred + [s for s in signal_order if s not in preferred]
    x_pref = ([original_x] if original_x in axis_order else []) + [a for a in axis_order if a != original_x]
    y_pref = ([original_y] if original_y in axis_order else []) + [a for a in axis_order if a != original_y]
    map_2d = is_map and actual.get('map_y') != 'None (1D)'
    choice = None
    if map_2d:
        for sig in signal_pref:
            for x in x_pref:
                if x == 'Step Index':
                    continue
                for y in y_pref:
                    if y == 'Step Index':
                        continue
                    if overlap(x, sig, y):
                        choice = (sig, x, y)
                        break
                if choice:
                    break
            if choice:
                break
    if choice is None and map_2d:
        # A primary with no drawable 2D pair falls back to a curve. The
        # renderer compares all selected curves, so recompute the union.
        fallback = dict(actual, map_y='None (1D)', auto_axes=False)
        result = resolve_choices(records, fallback, kind, cache)
        result['view']['auto_axes'] = view.get('auto_axes', False)
        result['warnings'] = warnings + [
            f'Y coordinate {original_y} is unavailable for the selected data; using None (1D).'
        ] + result['warnings']
        return result
    if choice is None:
        for sig in signal_pref:
            for x in x_pref:
                if overlap(x, sig):
                    choice = (sig, x, None)
                    break
            if choice:
                break
    if choice is None:
        return dict(signals=[], axes=[], view=actual, warnings=warnings,
                    note='No finite signal and coordinate values overlap under the current direction and pass filters.')
    signal, x, y = choice
    actual['signal'], actual['x'] = signal, x
    if is_map:
        actual['map_y'] = y if y else 'None (1D)'
    auto_role = (not actual.get('signal_explicit') and signal == role_signal
                 and original_signal in signal_order)
    if original_signal and signal != original_signal and not auto_role:
        warnings.append(f'Signal {original_signal} is unavailable for the selected data; using {signal}.')
    if original_x and x != original_x:
        warnings.append(f'X coordinate {original_x} is unavailable for the selected data; using {x}.')
    if is_map and original_y and actual['map_y'] != original_y and not view.get('auto_axes'):
        warnings.append(f'Y coordinate {original_y} is unavailable for the selected data; using {actual["map_y"]}.')
    signals = [s for s in signal_order if overlap(x, s, y)]
    # The selector list is shared by X and Y. Include any axis that can replace
    # either current map coordinate, but never use Step Index for a 2D map.
    if y:
        axes = [a for a in axis_order if a != 'Step Index' and
                (overlap(a, signal, y) or overlap(x, signal, a))]
    else:
        axes = [a for a in axis_order if overlap(a, signal)]
    note = ('Options reflect finite, overlapping values in the primary map.' if y
            else 'Options reflect finite, overlapping values in at least one selected file.')
    return dict(signals=signals, axes=axes, view=actual, warnings=warnings, note=note)
