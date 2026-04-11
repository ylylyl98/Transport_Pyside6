from __future__ import annotations

from typing import Dict, List


def list_gpib_resources() -> List[str]:
    try:
        import pyvisa

        rm = pyvisa.ResourceManager()
        return [r for r in rm.list_resources() if r.upper().startswith("GPIB")]
    except Exception:
        return []


def list_asrl_resources() -> List[str]:
    try:
        import pyvisa

        rm = pyvisa.ResourceManager()
        return [r for r in rm.list_resources() if r.upper().startswith("ASRL")]
    except Exception:
        return []


def list_daq_devices() -> List[str]:
    try:
        from nidaqmx.system import System

        return [dev.name for dev in System.local().devices]
    except Exception:
        return []


def scan_all() -> Dict[str, List[str]]:
    return {
        "gpib": list_gpib_resources(),
        "asrl": list_asrl_resources(),
        "daq": list_daq_devices(),
    }
