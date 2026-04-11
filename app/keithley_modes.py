from __future__ import annotations

KEITHLEY_MODE_VOLTAGE_2W = "voltage_2w"
KEITHLEY_MODE_OHM_4W = "ohm_4w"

KEITHLEY_MODE_LABELS = {
    KEITHLEY_MODE_VOLTAGE_2W: "2-wire Voltage Source",
    KEITHLEY_MODE_OHM_4W: "4-wire Ohms",
}


def keithley_mode_label(mode: str) -> str:
    return KEITHLEY_MODE_LABELS.get(mode, KEITHLEY_MODE_LABELS[KEITHLEY_MODE_VOLTAGE_2W])


def keithley_mode_options() -> list[tuple[str, str]]:
    return list(KEITHLEY_MODE_LABELS.items())
