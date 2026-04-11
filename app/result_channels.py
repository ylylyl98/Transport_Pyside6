from __future__ import annotations

from math import nan


BASE_PLOT_CHANNELS = ["Ids_DC", "Ids_X", "Ids_Y"]
KEITHLEY_CHANNEL = "Ids_Keithley"
COMPARE_CHANNELS = ["Ids_DC", KEITHLEY_CHANNEL, "Ids_Y", "Ids_X"]


def has_keithley_channel(vds_source: str) -> bool:
    return vds_source == "Keithley 2400"


def plot_channel_options(vds_source: str) -> list[str]:
    options = list(BASE_PLOT_CHANNELS)
    if has_keithley_channel(vds_source):
        options.append(KEITHLEY_CHANNEL)
    return options


def plot_channel_value(record: dict, channel: str) -> float:
    value = record.get(channel, nan)
    return nan if value is None else value


def compare_channel_options(vds_source: str) -> list[str]:
    available = set(plot_channel_options(vds_source))
    return [channel for channel in COMPARE_CHANNELS if channel in available]
