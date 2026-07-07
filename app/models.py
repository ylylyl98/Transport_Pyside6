from __future__ import annotations

import os
import re
from dataclasses import dataclass, field


@dataclass
class Connections:
    gate1: str = "GPIB1::23::INSTR"
    gate2: str = "GPIB1::03::INSTR"
    gate3: str = "GPIB1::07::INSTR"
    gate1_mode: str = "voltage_2w"
    gate2_mode: str = "voltage_2w"
    gate3_mode: str = "voltage_2w"
    daq_dev: str = "Dev1"
    mono: str = "ASRL13::INSTR"
    lockin: str = "GPIB1::08::INSTR"


@dataclass
class SaveRoot:
    user: str = "User"
    device_id: str = "YZ315"
    base: str = r"D:\\photocurrent\\data"

    def path(self) -> str:
        safe_user = re.sub(r"[^-_.A-Za-z0-9]+", "_", self.user).strip("_") or "User"
        safe_id = re.sub(r"[^-_.A-Za-z0-9]+", "_", self.device_id).strip("_") or "device"
        out = os.path.join(self.base, safe_user, safe_id)
        os.makedirs(out, exist_ok=True)
        return out


@dataclass
class DualGateParams:
    base_name: str = "dual_gate"
    output_csv_path: str = ""
    output_metadata_path: str = ""
    output_log_path: str = ""
    vds_source: str = "Keithley 2400"
    vds_start: float = 0.0
    vds_stop: float = 0.5
    vds_step: float = 0.01
    vds_ramp: float = 0.05
    vtg_set: float = 0.0
    vbg_set: float = 0.0
    vg_ramp: float = 0.2
    delay: float = 0.5
    n_sample: int = 3
    plot_choice: str = "Ids_DC"
    ao_channel: int = 0
    sweep_both_ways: bool = False


@dataclass
class CoParams:
    base_name: str = "dual_gate_cosweep"
    output_csv_path: str = ""
    output_metadata_path: str = ""
    output_log_path: str = ""
    vds_source: str = "Keithley 2400"
    ao_channel: int = 0
    vds_set: float = 0.0
    vds_ramp: float = 0.05
    vtg_start: float = 0.0
    vtg_stop: float = 1.0
    vtg_step: float = 0.1
    vbg_start: float = 0.0
    vbg_stop: float = 1.0
    vbg_step: float = 0.1
    ratio: float = 1.0
    vg_ramp: float = 0.2
    delay: float = 0.5
    n_sample: int = 3
    inner: str = "Vbg"
    plot_choice: str = "Ids_DC"
    mode: str = "Grid"
    axis_fast: str = "Vtg"
    axis_slow: str = "None"
    vds_start: float = 0.0
    vds_stop: float = 0.0
    vds_step: float = 0.01


@dataclass
class LineSweepParams:
    base_name: str = "gate_scan"
    output_csv_path: str = ""
    output_metadata_path: str = ""
    output_log_path: str = ""
    mode: str = "Raw"
    vds_source: str = "Keithley 2400"
    ao_channel: int = 0

    raw_vtg_active: bool = True
    raw_vtg_start: float = 0.0
    raw_vtg_stop: float = 1.0
    raw_vbg_active: bool = False
    raw_vbg_start: float = 0.0
    raw_vbg_stop: float = 0.0
    raw_vds_active: bool = False
    raw_vds_start: float = 0.0
    raw_vds_stop: float = 0.0

    derived_ratio: float = 1.0
    derived_axis: str = "Doping"
    derived_start: float = 0.0
    derived_stop: float = 1.0
    derived_fixed: float = 0.0
    derived_vds_mode: str = "Fixed"
    derived_vds_fixed: float = 0.0
    derived_vds_start: float = 0.0
    derived_vds_stop: float = 0.0

    n_points: int = 51
    vg_ramp: float = 0.2
    vds_ramp: float = 0.05
    delay: float = 0.5
    n_sample: int = 3
    plot_choice: str = "Ids_DC"
    plot_x_axis: str = "Auto"
    sweep_both_ways: bool = False


@dataclass
class PhotocurrentBiasCondition:
    """One complete gate/drain bias condition for a photocurrent spectrum."""

    enabled: bool = True
    vtg: float = 0.0
    vbg: float = 0.0
    vds: float = 0.0
    settle_s: float = 2.0


@dataclass
class PhotocurrentParams:
    base_name: str = "pcspec"
    output_csv_path: str = ""
    output_metadata_path: str = ""
    output_log_path: str = ""
    use_vds: bool = False
    vds_source: str = "None"
    ao_channel: int = 0
    vds_set: float = 0.0
    vds_ramp: float = 0.01
    vtg_set: float = 0.0
    vbg_set: float = 0.0
    vg_ramp: float = 0.2
    bias_conditions: list[PhotocurrentBiasCondition] = field(
        default_factory=lambda: [PhotocurrentBiasCondition()]
    )
    wl_start: float = 550.0
    wl_stop: float = 740.0
    wl_step: float = 0.5
    delay: float = 0.01
    n_sample: int = 1
    plot_choice: str = "Ids_DC"
