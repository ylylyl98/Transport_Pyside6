"""Automatic per-source analysis exports, with independent image/data reuse."""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from app.analysis_csv_export import csv_bytes, validate_csv_path, write_bytes_atomic
from app.export_paths import ordinary_path, validate_export_path


def _safe_name(value):
    return re.sub(r'[<>:"/\\|?*\x00-\x1f]', '-', str(value)).strip(' .') or 'plot'


def condition_stem(source):
    name = re.sub(r'_\d{8}_\d{6}(?:_\d+)?$', '', Path(source).stem)
    name = re.sub(r'_(?:LIA[0-9.eE+\-]+[fpnumkMG]?V|Pre[0-9.eE+\-]+[fpnumkMG]?A)(?=_|$)', '', name)
    name = re.sub(r'_lia_[0-9.eE+\-]+[fpnumkMG]?V_preamp_[0-9.eE+\-]+[fpnumkMG]?A(?=_|$)', '', name)
    return compact_stem(name)


def compact_stem(name):
    name = re.sub(r'(^|_)Doping(?=[+\-\d.])', r'\1D', name)
    name = re.sub(r'(^|_)DragDrive(?=_|$)', r'\1DD', name)
    name = re.sub(r'ACout([^_]+)_VacEst([^_]+)', r'AC\1-est\2', name)
    return _safe_name(name)


def export_folder(source):
    source = ordinary_path(source)
    legacy = source.parent / source.stem
    compact = source.parent / compact_stem(source.stem)
    def available(folder, *, allow_unknown=False):
        if not folder.exists():
            return True
        if not folder.is_dir():
            return False
        owners = set()
        for path in folder.glob('*_view.json'):
            try:
                payload = json.loads(path.read_text(encoding='utf-8'))
                primary = payload.get('view', {}).get('primary_path') or payload['sources'][0]['path']
                owners.add(ordinary_path(primary).resolve())
            except (OSError, ValueError, KeyError, IndexError, TypeError):
                continue
        if owners:
            return owners == {source.resolve()}
        return allow_unknown or not any(folder.iterdir())
    if legacy.exists() and available(legacy, allow_unknown=True):
        return legacy
    if available(compact):
        return compact
    if available(legacy):
        return legacy
    # A literal source name can also equal another source's abbreviation.
    # Keep its complete identity and use a readable suffix in that rare case.
    number = 2
    while True:
        candidate = legacy.with_name(f'{legacy.name}_export{number:02d}')
        if available(candidate):
            return candidate
        number += 1


def _digest(value):
    if not isinstance(value, bytes):
        value = json.dumps(value, ensure_ascii=False, sort_keys=True).encode('utf-8')
    return hashlib.sha256(value).hexdigest()


def _label(snapshot, kind):
    label = _safe_name(snapshot['signal']) + '_' + kind
    if kind == 'cut':
        data, view = snapshot['map'], snapshot['view']
        axis = view.get('fixed_axis', data['y_name'])
        unit = data['y_unit'] if axis == data['y_name'] else data['x_unit']
        label += f'_{axis}_{float(view["fixed_value"]):.12g}{unit}'
    return _safe_name(label)


def _png_path(folder, source, label):
    full = folder / f'{condition_stem(source)}_{label}.png'
    try:
        return validate_export_path(full)
    except ValueError:
        # Source identity and all conditions remain in this source's folder,
        # the visible header, PNG metadata and the accompanying view/CSV.
        return validate_export_path(folder / f'{label}.png')


def _reserve(base, reserved, *, view=False, fallback=None):
    number = 1
    while True:
        if number == 1:
            path = base
        elif view:
            path = base.with_name(f'{base.stem.removesuffix("_view")}_v{number:02d}_view.json')
        else:
            path = base.with_name(f'{base.stem}_v{number:02d}{base.suffix}')
        try:
            path = validate_export_path(path)
        except ValueError:
            if fallback is not None and fallback != base:
                return _reserve(fallback, reserved, view=view)
            raise
        if path.suffix == '.csv':
            validate_csv_path(path)
        try:
            with path.open('xb'):
                pass
            reserved.append(path)
            return path
        except FileExistsError:
            number += 1


def _saved_exports(folder, signal):
    saved = []
    for path in sorted(folder.glob('*_view.json')):
        try:
            payload = json.loads(path.read_text(encoding='utf-8'))
            if (isinstance(payload, dict) and payload.get('schema') == 'transport_plot_view_v1'
                    and payload.get('automatic_signal') == signal
                    and payload.get('export_key') == _view_key(payload)):
                saved.append((path, payload))
        except (OSError, ValueError):
            continue
    return saved


def _view_key(payload):
    try:
        images, csv_files = payload['images'], payload['csv_files']
        if len(images) != len(csv_files):
            return ''
        artifacts = [(image['path'], image['key'], data['path'], data['key'])
                     for image, data in zip(images, csv_files)]
        return _digest({'view': payload['view'], 'sources': payload['sources'], 'artifacts': artifacts})
    except (KeyError, TypeError, ValueError):
        return ''


def _reusable(saved, folder, group, kind, key, checked):
    for _view_path, payload in saved:
        entries = payload.get(group, [])
        if not isinstance(entries, list):
            continue
        for item in entries:
            if not isinstance(item, dict) or item.get('kind') != kind or item.get('key') != key:
                continue
            try:
                path = validate_export_path(item['path'])
                if path.parent.resolve() != folder.resolve():
                    continue
                if path not in checked:
                    checked[path] = _digest(path.read_bytes())
                if checked[path] == item.get('sha256'):
                    return path
            except (OSError, ValueError, KeyError, TypeError):
                continue
    return None


def export_automatic(snapshot, *, cut_only=False, heatmap_only=False):
    # Lazy import: PNG rendering imports this module only for automatic exports.
    from app.png_export import DPI, _heatmap_snapshot, _write_png, _description, build_analysis_figure
    from app.plot_export_labels import captions

    primary = snapshot.get('map', {}).get('path') or snapshot['records'][0]['path']
    source = ordinary_path(primary)
    folder = export_folder(source)
    if snapshot.get('map'):
        kinds = ['cut'] if cut_only else ['heatmap'] if heatmap_only or not snapshot['traces'] else ['heatmap', 'cut']
    else:
        kinds = ['curve']
    plans = []
    for kind in kinds:
        label = _label(snapshot, kind)
        png = _png_path(folder, source, label)
        data_path = folder / f'{label}.csv'
        validate_csv_path(data_path)
        content = csv_bytes(snapshot, kind)
        data_key = _digest(content)
        rendered = _heatmap_snapshot(snapshot) if kind == 'heatmap' else snapshot
        title, detail, _ = captions(rendered)
        description = _description(rendered, detail)
        properties = ('map_xlim', 'map_ylim', 'clim', 'cmap') if kind == 'heatmap' else ('xlim', 'ylim', 'xscale', 'yscale', 'legend')
        image_key = _digest({'renderer': 1, 'data': data_key,
                             'title': title, 'description': description,
                             'view': {name: snapshot['view'].get(name) for name in properties}})
        plans.append({'kind': kind, 'png': png, 'csv': data_path, 'bytes': content,
                      'data_key': data_key, 'image_key': image_key, 'rendered': rendered,
                      'title': title, 'description': description})
    validate_export_path(folder / f'{_safe_name(snapshot["signal"])}_view.json')
    folder.mkdir(parents=True, exist_ok=True)
    saved = _saved_exports(folder, snapshot['signal'])
    checked, reserved, reused = {}, [], []
    try:
        for plan in plans:
            for group, field, key_name in (('images', 'png', 'image_key'), ('csv_files', 'csv', 'data_key')):
                old = _reusable(saved, folder, group, plan['kind'], plan[key_name], checked)
                plan[field + '_new'] = old is None
                fallback = folder / f'{_label(snapshot, plan["kind"])}.png' if field == 'png' else None
                plan[field] = old if old else _reserve(plan[field], reserved, fallback=fallback)
                if old:
                    reused.append(str(old))
        view = {**snapshot['view'], 'signal': snapshot['signal'],
                'x': snapshot.get('map', {}).get('x_name', snapshot['x_name']), 'kind': snapshot['kind'],
                'map_y': snapshot.get('map', {}).get('y_name', 'None (1D)'),
                'cut_only': bool(cut_only), 'heatmap_only': bool(heatmap_only)}
        export_key = _digest({'view': view, 'sources': snapshot['records'],
                              'artifacts': [(str(p['png']), p['image_key'], str(p['csv']), p['data_key']) for p in plans]})
        view_path = next((path for path, payload in saved if payload.get('export_key') == export_key
                          and len(str(ordinary_path(path)).encode('utf-16-le')) // 2 <= 259), None)
        new_view = view_path is None
        if new_view:
            view_path = _reserve(folder / f'{_safe_name(snapshot["signal"])}_view.json', reserved, view=True)
        for plan in plans:
            if plan['png_new']:
                kind = plan['kind']
                figure = build_analysis_figure(plan['rendered'], cut_only=kind == 'cut', map_only=kind == 'heatmap')
                _write_png(figure, plan['png'], plan['title'], plan['description'],
                           [record['path'] for record in plan['rendered']['records']])
            if plan['csv_new']:
                write_bytes_atomic(plan['csv'], plan['bytes'])
        if new_view:
            images = [{'path': str(p['png']), 'kind': p['kind'], 'key': p['image_key'],
                       'sha256': _digest(p['png'].read_bytes())} for p in plans]
            csv_files = [{'path': str(p['csv']), 'kind': p['kind'], 'key': p['data_key'],
                          'sha256': p['data_key']} for p in plans]
            payload = {'schema': 'transport_plot_view_v1', 'automatic_signal': snapshot['signal'],
                       'export_key': export_key, 'sources': snapshot['records'], 'view': view,
                       'images': images, 'csv_files': csv_files,
                       'image': {'width': 1200, 'height': 900, 'dpi': DPI}, 'warnings': snapshot.get('warnings', [])}
            write_bytes_atomic(view_path, json.dumps(payload, ensure_ascii=False, indent=2).encode('utf-8'))
        return {'outputs': [str(p['png']) for p in plans], 'csv_outputs': [str(p['csv']) for p in plans],
                'view_path': str(view_path), 'reused_outputs': reused, 'warnings': snapshot.get('warnings', [])}
    except Exception:
        for path in reserved:
            path.unlink(missing_ok=True)
        raise
