"""Bounded cache for viewer-owned numeric snapshots (one plotting lane)."""
from collections import OrderedDict
from dataclasses import dataclass, fields, is_dataclass
import sys

import numpy as np


@dataclass
class CsvColumns:
    """Compact numeric columns with original categorical/group text preserved."""
    columns: tuple

    @classmethod
    def from_rows(cls, headers, rows, numeric):
        text_columns = {"Direction", "FastDirection", "Condition", "Condition_index", "PassIndex"}
        columns = []
        for i, name in enumerate(headers):
            values = [row[i] for row in rows]
            if name in text_columns:
                column = tuple(values)
            else:
                try:
                    column = np.asarray(values, dtype=float)
                except (TypeError, ValueError):
                    column = np.asarray([numeric(value) for value in values], dtype=float)
                column[~np.isfinite(column)] = np.nan
                column.setflags(write=False)
            columns.append(column)
        return cls(tuple(columns))

    def __len__(self):
        return len(self.columns[0]) if self.columns else 0

    def __iter__(self):
        return zip(*self.columns)


def _size(value):
    # CSV cells are leaves; avoid dataclass reflection for every string.
    if isinstance(value, (str, bytes, int, float, type(None))):
        return sys.getsizeof(value)
    if isinstance(value, np.ndarray):
        return value.nbytes
    if is_dataclass(value):
        return sum(_size(getattr(value, field.name)) for field in fields(value))
    if isinstance(value, (tuple, list)):
        return sys.getsizeof(value) + sum(_size(v) for v in value)
    if isinstance(value, dict):
        return sys.getsizeof(value) + sum(_size(k) + _size(v) for k, v in value.items())
    return sys.getsizeof(value)


class SnapshotCache:
    def __init__(self, max_bytes=64 * 1024 * 1024):
        self.max_bytes = max_bytes
        self.bytes_used = 0
        self._items = OrderedDict()

    def get(self, key, load):
        if key in self._items:
            value, size = self._items.pop(key)
            self._items[key] = (value, size)
            return value
        value = load()
        size = _size(value)
        if size <= self.max_bytes:
            while self._items and self.bytes_used + size > self.max_bytes:
                _old, (_value, old_size) = self._items.popitem(last=False)
                self.bytes_used -= old_size
            self._items[key] = (value, size)
            self.bytes_used += size
        return value
