from __future__ import annotations

from math import nan
from app.drag_drive import DUAL_SIGNALS, measurement_mode


BASE_PLOT_CHANNELS = ["Ids_DC", "Ids_X", "Ids_Y"]
KEITHLEY_CHANNEL = "Ids_Keithley"
COMPARE_CHANNELS = ["Ids_DC", KEITHLEY_CHANNEL, "Ids_Y", "Ids_X"]


def has_keithley_channel(vds_source: str) -> bool:
    return vds_source == "Keithley 2400"


def plot_channel_options(vds_source: str, dual: bool | dict = False, *, bias2: bool = False) -> list[str]:
    chain = dual if isinstance(dual, dict) else {'drag_drive': {'enabled': dual}}
    mode = measurement_mode(chain)
    config = chain.get('drag_drive') or {}
    options = list(BASE_PLOT_CHANNELS)
    if not config.get('dc_enabled', True):
        options.remove('Ids_DC')
    if mode == 'single_drive':
        options = [name for name in options if name not in ('Ids_X', 'Ids_Y')]
    if has_keithley_channel(vds_source):
        options.append(KEITHLEY_CHANNEL)
    if bias2:
        options.append('Ibias2_Keithley')
    if mode == 'dual':
        options.extend(DUAL_SIGNALS)
    elif mode in ('single_drag', 'single_drive'):
        options.extend(f"I_{mode[7:]}_{axis}" for axis in ('X', 'Y', 'R'))
    return options


def plot_channel_value(record: dict, channel: str) -> float:
    value = record.get(channel, nan)
    return nan if value is None else value


def compare_channel_options(vds_source: str, dual: bool | dict = False, *, bias2: bool = False) -> list[str]:
    chain = dual if isinstance(dual, dict) else {'drag_drive': {'enabled': dual}}
    mode = measurement_mode(chain)
    if mode == 'dual':
        return ['I_drag_X', 'I_drag_Y', 'I_drive_X', 'I_drive_Y']
    if mode.startswith('single_'):
        available = plot_channel_options(vds_source, chain, bias2=bias2)
        return [name for name in (f'I_{mode[7:]}_X', f'I_{mode[7:]}_Y', 'Ids_DC', KEITHLEY_CHANNEL, 'Ibias2_Keithley') if name in available]
    available = set(plot_channel_options(vds_source, chain, bias2=bias2))
    return [channel for channel in (*COMPARE_CHANNELS, 'Ibias2_Keithley') if channel in available]
