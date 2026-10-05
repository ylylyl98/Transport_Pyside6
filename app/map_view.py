"""Heatmap display ranges; source grids are never cropped or modified."""
import numpy as np


def visible_clim(data, xlim=None, ylim=None):
    x, y, z = (np.asarray(data[key], dtype=float) for key in ('x', 'y', 'z'))
    xi = np.ones(len(x), dtype=bool) if xlim is None else (x >= min(xlim)) & (x <= max(xlim))
    yi = np.ones(len(y), dtype=bool) if ylim is None else (y >= min(ylim)) & (y <= max(ylim))
    values = z[np.ix_(yi, xi)]
    values = values[np.isfinite(values)]
    if not values.size:
        return None
    low, high = float(values.min()), float(values.max())
    if low == high:
        margin = abs(low) * .05 if low else 1e-12
        low, high = low - margin, high + margin
    return low, high


def resolved_map_view(snapshot):
    """Resolve auto color bounds before rendering and computing PNG identity."""
    view = snapshot['view']
    if snapshot.get('map') and view.get('clim_auto', False):
        limits = visible_clim(snapshot['map'], view.get('map_xlim'), view.get('map_ylim'))
        if limits is not None:
            return {**snapshot, 'view': {**view, 'clim': list(limits)}}
    return snapshot
