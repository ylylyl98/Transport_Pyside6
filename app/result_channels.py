from __future__ import annotations

from math import nan
from app.drag_drive import DUAL_SIGNALS


BASE_PLOT_CHANNELS = ["Ids_DC", "Ids_X", "Ids_Y"]
KEITHLEY_CHANNEL = "Ids_Keithley"
COMPARE_CHANNELS = ["Ids_DC", KEITHLEY_CHANNEL, "Ids_Y", "Ids_X"]


def has_keithley_channel(vds_source: str) -> bool:
    return vds_source == "Keithley 2400"


def plot_channel_options(vds_source: str, dual: bool = False) -> list[str]:
    options = list(BASE_PLOT_CHANNELS)
    if has_keithley_channel(vds_source):
        options.append(KEITHLEY_CHANNEL)
    if dual:
        options.extend(DUAL_SIGNALS)
    return options


def plot_channel_value(record: dict, channel: str) -> float:
    value = record.get(channel, nan)
    return nan if value is None else value


def compare_channel_options(vds_source: str, dual: bool = False) -> list[str]:
    if dual:
        return ['I_drag_X', 'I_drag_Y', 'I_drive_X', 'I_drive_Y']
    available = set(plot_channel_options(vds_source))
    return [channel for channel in COMPARE_CHANNELS if channel in available]
