"""Self-describing analysis CSVs with exactly three Origin-style label rows."""
from __future__ import annotations

import csv
import io
import json
import os
import tempfile
from itertools import zip_longest
from pathlib import Path

import numpy as np

from app.plot_export_labels import conditions


def _comment(**fields):
    # JSON quoting keeps commas, quotes and embedded newlines in one CSV row.
    return '; '.join(f'{key}={json.dumps(value, ensure_ascii=False)}' for key, value in fields.items())


def _source_comment(record, **fields):
    metadata = record.get('metadata', {})
    return _comment(ExportFormat='transport_analysis_csv_v1', SourceFile=Path(record['path']).name, SourcePath=record['path'],
                    CreatedAt=record.get('created_at', ''), Status=record.get('status', ''),
                    Device=metadata.get('save_root', {}).get('device_id', ''),
                    Conditions=conditions(record), Parameters=metadata.get('params', {}),
                    SignalChain=metadata.get('signal_chain') or metadata.get('validation', {}).get('calibration', {}).get('signal_chain', {}),
                    InstrumentSettings=metadata.get('lockin_settings', {}),
                    ExperimentContext=metadata.get('experiment_context', {}), **fields)


def csv_bytes(snapshot, kind):
    """Export all selected samples, independent of zoom, color scale and display units."""
    stream = io.StringIO(newline='')
    writer = csv.writer(stream, lineterminator='\r\n')
    records = {record['path']: record for record in snapshot['records']}
    view = snapshot['view']
    selection = {'direction': view.get('direction', 'All'), 'pass': view.get('pass', 'All rows')}
    if kind == 'heatmap':
        data = snapshot['map']
        x, y, z = (np.asarray(data[key], dtype=float) for key in ('x', 'y', 'z'))
        if x.ndim != 1 or y.ndim != 1 or z.shape != (len(y), len(x)):
            raise ValueError('Heatmap matrix shape does not match its coordinates')
        record = records[data['path']]
        info = dict(Signal=snapshot['signal'], X=data['x_name'], XUnit=data['x_unit'],
                    Y=data['y_name'], YUnit=data['y_unit'], SignalUnit=data['signal_unit'],
                    Selection=selection, Normalization='raw',
                    Processing='measured-coordinate grid; no interpolation; full selected grid')
        writer.writerow([data['y_name'], *map(float, x)])
        writer.writerow([data['y_unit'], *([data['signal_unit']] * len(x))])
        writer.writerow([_source_comment(record, **info),
                         *[_comment(Signal=snapshot['signal'], X=data['x_name'], XUnit=data['x_unit'],
                                    XValue=float(value), Normalization='raw') for value in x]])
        for coordinate, row in zip(y, z):
            writer.writerow([float(coordinate), *map(float, row)])
    else:
        names, units, comments, columns = [], [], [], []
        for trace in snapshot['traces']:
            x, y = (np.asarray(trace[key], dtype=float) for key in ('x', 'y'))
            if x.ndim != 1 or y.ndim != 1 or len(x) != len(y):
                raise ValueError('Trace coordinates and signal values must have matching lengths')
            info = dict(Signal=snapshot['signal'], X=snapshot['x_name'], XUnit=snapshot['x_unit'],
                        SignalUnit=snapshot['y_unit'], Selection=selection,
                        Direction=trace.get('direction', ''), Condition=trace.get('condition', ''),
                        Normalization=view.get('normalization', 'raw'), Processing='full selected trace')
            if kind == 'cut':
                data = snapshot['map']
                fixed_axis = view.get('fixed_axis', data['y_name'])
                info['FixedAxis'] = fixed_axis
                info['FixedValue'] = view['fixed_value']
                info['FixedUnit'] = data['y_unit'] if fixed_axis == data['y_name'] else data['x_unit']
            names.extend([snapshot['x_name'], snapshot['signal']])
            units.extend([snapshot['x_unit'], snapshot['y_unit']])
            comments.extend([_source_comment(records[trace['path']], **info), _comment(**info)])
            columns.extend([map(float, x), map(float, y)])
        writer.writerows([names, units, comments])
        writer.writerows(zip_longest(*columns, fillvalue=''))
    return stream.getvalue().encode('utf-8-sig')


def validate_csv_path(path):
    """Origin compatibility budget: absolute UTF-16 path, including the extension."""
    from app.export_paths import validate_export_path
    return validate_export_path(path, label='CSV (Origin)')


def temporary_path(target):
    """Short sibling name permits atomic replace without repeating the long stem."""
    target = Path(target)
    descriptor, name = tempfile.mkstemp(prefix='.export-', suffix=target.suffix, dir=target.parent)
    os.close(descriptor)
    return Path(name)


def write_bytes_atomic(path, content):
    temporary = temporary_path(path)
    try:
        temporary.write_bytes(content)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
