"""Ordinary Windows paths for exported files consumed by desktop applications."""
from pathlib import Path


def ordinary_path(path):
    """Remove filesystem-only extended prefixes from application-facing paths."""
    value = str(path)
    if value.startswith('\\\\?\\UNC\\'):
        value = '\\\\' + value[8:]
    elif value.startswith('\\\\?\\'):
        value = value[4:]
    return Path(value).absolute()


def validate_export_path(path, *, label='Export'):
    path = ordinary_path(path)
    length = len(str(path).encode('utf-16-le')) // 2
    if length > 259:
        raise ValueError(f'{label} path is {length} characters; compatibility limit is 259. '
                         f'Use a shorter filename or parent folder: {path}')
    if any(len(part.encode('utf-16-le')) // 2 > 255 for part in path.parts):
        raise ValueError(f'Export filename or folder exceeds 255 characters: {path}')
    return path
