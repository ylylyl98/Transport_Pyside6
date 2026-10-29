"""Numeric plot archive codec with no GUI, Matplotlib or hardware imports."""
import json
from pathlib import Path

import numpy as np


def save_snapshot(snapshot, path):
    """Keep full numeric arrays out of JSON IPC and disallow pickled objects."""
    arrays = {}

    def encode(value):
        if isinstance(value, np.ndarray):
            key = f'array_{len(arrays)}'
            arrays[key] = value
            return {'array_key': key}
        if isinstance(value, Path):
            return str(value)
        if isinstance(value, dict):
            return {key: encode(item) for key, item in value.items()}
        if isinstance(value, (tuple, list)):
            return [encode(item) for item in value]
        if isinstance(value, np.generic):
            return value.item()
        return value

    data = encode(snapshot)
    with Path(path).open('wb') as stream:
        np.savez(stream, metadata=np.array(json.dumps(data)), **arrays)
    return str(path)


def load_snapshot(path):
    with np.load(path, allow_pickle=False) as archive:
        def decode(value):
            if isinstance(value, dict):
                if set(value) == {'array_key'}:
                    return archive[value['array_key']].copy()
                return {key: decode(item) for key, item in value.items()}
            if isinstance(value, list):
                return [decode(item) for item in value]
            return value
        return decode(json.loads(str(archive['metadata'])))
