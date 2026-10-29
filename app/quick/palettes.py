"""Standard sRGB colormap assets; see palettes_LICENSE.txt.

Copyright (c) 2012- Matplotlib Development Team; All Rights Reserved.
Derived from Matplotlib 3.11.1: https://matplotlib.org/stable/users/explain/colors/colormaps.html
Changes: registered colormaps sampled to static 256-entry RGB numeric tables.
The GUI reads this numeric asset without importing or rendering Matplotlib.
The separate PNG worker retains the original named-colormap renderer.
"""
from pathlib import Path

import numpy as np


with np.load(Path(__file__).with_suffix('.npz'), allow_pickle=False) as _archive:
    COLOR_TABLES = {name: _archive[name].copy() for name in _archive.files}

PREFERRED = ['RdBu_r', 'RdBu', 'viridis', 'plasma', 'inferno', 'magma',
             'cividis', 'coolwarm', 'seismic', 'gray']
COLOR_MAP_NAMES = PREFERRED + sorted(set(COLOR_TABLES) - set(PREFERRED))
