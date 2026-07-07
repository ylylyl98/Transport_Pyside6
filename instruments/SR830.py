from __future__ import annotations

from typing import Any

import pyvisa

from instruments import PyvisaInstrument


SENSITIVITY_LABELS = [
    "2 nV/fA",
    "5 nV/fA",
    "10 nV/fA",
    "20 nV/fA",
    "50 nV/fA",
    "100 nV/fA",
    "200 nV/fA",
    "500 nV/fA",
    "1 uV/pA",
    "2 uV/pA",
    "5 uV/pA",
    "10 uV/pA",
    "20 uV/pA",
    "50 uV/pA",
    "100 uV/pA",
    "200 uV/pA",
    "500 uV/pA",
    "1 mV/nA",
    "2 mV/nA",
    "5 mV/nA",
    "10 mV/nA",
    "20 mV/nA",
    "50 mV/nA",
    "100 mV/nA",
    "200 mV/nA",
    "500 mV/nA",
    "1 V/uA",
]

TIME_CONSTANT_LABELS = [
    "10 us",
    "30 us",
    "100 us",
    "300 us",
    "1 ms",
    "3 ms",
    "10 ms",
    "30 ms",
    "100 ms",
    "300 ms",
    "1 s",
    "3 s",
    "10 s",
    "30 s",
    "100 s",
    "300 s",
    "1 ks",
    "3 ks",
    "10 ks",
    "30 ks",
]

RESERVE_LABELS = ["High Reserve", "Normal", "Low Noise"]
FILTER_SLOPE_LABELS = ["6 dB/oct", "12 dB/oct", "18 dB/oct", "24 dB/oct"]
REFERENCE_SOURCE_LABELS = ["External", "Internal"]
INPUT_CONFIG_LABELS = ["A", "A-B", "I 1 Mohm", "I 100 Mohm"]
INPUT_COUPLING_LABELS = ["AC", "DC"]
INPUT_GROUND_LABELS = ["Float", "Ground"]
LINE_FILTER_LABELS = ["Out", "Line", "2x Line", "Both"]


def _label(labels: list[str], index: int) -> str:
    return labels[index] if 0 <= index < len(labels) else f"Code {index}"


def parse_prefixed_value(text: str):
    pieces = str(text or "").strip().split()
    if len(pieces) != 2:
        return None
    try:
        number = float(pieces[0])
    except ValueError:
        return None
    unit = pieces[1]
    if not unit:
        return None
    prefix = unit[:-1]
    scale = {
        "f": 1e-15,
        "p": 1e-12,
        "n": 1e-9,
        "u": 1e-6,
        "m": 1e-3,
        "": 1.0,
        "k": 1e3,
    }.get(prefix)
    if scale is None:
        return None
    return number * scale


def sensitivity_value(index: Any, use_current: bool = False):
    try:
        index = int(index)
    except (TypeError, ValueError):
        return None
    if index < 0 or index >= len(SENSITIVITY_LABELS):
        return None
    parts = SENSITIVITY_LABELS[index].split("/")
    if use_current and len(parts) > 1:
        voltage_parts = parts[0].strip().split()
        if not voltage_parts:
            return None
        selected = f"{voltage_parts[0]} {parts[1].strip()}"
    else:
        selected = parts[0].strip()
    return parse_prefixed_value(selected)


class SR830(PyvisaInstrument):
    """Stanford Research Systems SR830 lock-in amplifier over GPIB."""

    def __init__(
        self,
        name: str = "SR830",
        address: str = "GPIB1::08::INSTR",
        timeout: float = 5000,
        test_mode=False,
    ):
        super().__init__(
            name=name,
            address=address,
            input_channels=("x", "y", "r", "theta"),
            termination="\n",
            timeout=timeout,
            test_mode=test_mode,
        )
        self._identity = ""
        self._last_snap_raw = ""

    def connect(self):
        try:
            super().connect()
            self._write("OUTX 1")
            self._write("OVRM 1")
            self._identity = self.get_identity().strip()
        except pyvisa.Error as e:
            print(f"SR830 {self.address} connection failed.")
            raise e
        return self

    @property
    def identity(self) -> str:
        return self._identity

    def read_outputs(self) -> dict[str, float]:
        with self.lock:
            raw = self._query("SNAP?1,2,3,4")
            self._last_snap_raw = raw.strip()
            values = [float(part.strip()) for part in raw.split(",")]
            if len(values) != 4:
                raise ValueError(f"Unexpected SR830 SNAP response: {raw!r}")
            x, y, r, theta = values
            self._input_values.update({"x": x, "y": y, "r": r, "theta": theta})
            return self._input_values.copy()

    def read_lia_status(self) -> dict[str, Any]:
        with self.lock:
            byte = int(float(self._query("LIAS?").strip()))
            return {
                "byte": byte,
                "input_overload": bool(byte & (1 << 0)),
                "filter_overload": bool(byte & (1 << 1)),
                "output_overload": bool(byte & (1 << 2)),
                "unlock": bool(byte & (1 << 3)),
                "range_change": bool(byte & (1 << 4)),
                "time_constant_change": bool(byte & (1 << 5)),
                "triggered": bool(byte & (1 << 6)),
            }

    def read_sensitivity(self) -> dict[str, Any]:
        with self.lock:
            sensitivity = self._query_index("SENS?")
            return {
                "sensitivity": sensitivity,
                "sensitivity_label": _label(SENSITIVITY_LABELS, sensitivity),
                "sensitivity_v": sensitivity_value(sensitivity, use_current=False),
            }

    def read_front_panel(self) -> dict[str, Any]:
        with self.lock:
            outputs = self.read_outputs()
            status = self.read_lia_status()
            settings = self.read_settings()
            return {
                "identity": self.identity,
                "outputs": outputs,
                "raw_snap": self._last_snap_raw,
                "status": status,
                "settings": settings,
            }

    def read_settings(self) -> dict[str, Any]:
        with self.lock:
            phase = self._query_float("PHAS?")
            ref_source = self._query_index("FMOD?")
            frequency = self._query_float("FREQ?")
            sine_out = self._query_float("SLVL?")
            sensitivity = self._query_index("SENS?")
            reserve = self._query_index("RMOD?")
            time_constant = self._query_index("OFLT?")
            filter_slope = self._query_index("OFSL?")
            input_config = self._query_index("ISRC?")
            input_ground = self._query_index("IGND?")
            input_coupling = self._query_index("ICPL?")
            line_filter = self._query_index("ILIN?")
            harmonic = self._query_index("HARM?")
            return {
                "phase_deg": phase,
                "frequency_hz": frequency,
                "sine_out_v": sine_out,
                "harmonic": harmonic,
                "ref_source": ref_source,
                "ref_source_label": _label(REFERENCE_SOURCE_LABELS, ref_source),
                "sensitivity": sensitivity,
                "sensitivity_label": _label(SENSITIVITY_LABELS, sensitivity),
                "reserve": reserve,
                "reserve_label": _label(RESERVE_LABELS, reserve),
                "time_constant": time_constant,
                "time_constant_label": _label(TIME_CONSTANT_LABELS, time_constant),
                "filter_slope": filter_slope,
                "filter_slope_label": _label(FILTER_SLOPE_LABELS, filter_slope),
                "input_config": input_config,
                "input_config_label": _label(INPUT_CONFIG_LABELS, input_config),
                "input_ground": input_ground,
                "input_ground_label": _label(INPUT_GROUND_LABELS, input_ground),
                "input_coupling": input_coupling,
                "input_coupling_label": _label(INPUT_COUPLING_LABELS, input_coupling),
                "line_filter": line_filter,
                "line_filter_label": _label(LINE_FILTER_LABELS, line_filter),
            }

    def apply_settings(self, settings: dict[str, Any]):
        with self.lock:
            if "phase_deg" in settings:
                self._write(f"PHAS {float(settings['phase_deg']):.2f}")
            if "ref_source" in settings:
                self._write_index("FMOD", settings["ref_source"], REFERENCE_SOURCE_LABELS)
            if "frequency_hz" in settings:
                self._write(f"FREQ {float(settings['frequency_hz']):.6g}")
            if "sine_out_v" in settings:
                self._write(f"SLVL {float(settings['sine_out_v']):.6g}")
            if "sensitivity" in settings:
                self._write_index("SENS", settings["sensitivity"], SENSITIVITY_LABELS)
            if "reserve" in settings:
                self._write_index("RMOD", settings["reserve"], RESERVE_LABELS)
            if "time_constant" in settings:
                self._write_index("OFLT", settings["time_constant"], TIME_CONSTANT_LABELS)
            if "filter_slope" in settings:
                self._write_index("OFSL", settings["filter_slope"], FILTER_SLOPE_LABELS)
            if "input_config" in settings:
                self._write_index("ISRC", settings["input_config"], INPUT_CONFIG_LABELS)
            if "input_ground" in settings:
                self._write_index("IGND", settings["input_ground"], INPUT_GROUND_LABELS)
            if "input_coupling" in settings:
                self._write_index("ICPL", settings["input_coupling"], INPUT_COUPLING_LABELS)
            if "line_filter" in settings:
                self._write_index("ILIN", settings["line_filter"], LINE_FILTER_LABELS)
            if "harmonic" in settings:
                harmonic = int(settings["harmonic"])
                if harmonic < 1:
                    raise ValueError("SR830 harmonic must be at least 1.")
                self._write(f"HARM {harmonic:d}")

    def auto_phase(self):
        self._write("APHS")

    def auto_gain(self):
        self._write("AGAN")

    def auto_reserve(self):
        self._write("ARSV")

    def auto_offset_x(self):
        self._write("AOFF 1")

    def auto_offset_y(self):
        self._write("AOFF 2")

    def auto_offset_r(self):
        self._write("AOFF 3")

    def acquire(self) -> dict:
        return self.read_outputs()

    def refresh(self, keys=None):
        self.read_outputs()

    def _query_index(self, command: str) -> int:
        return int(float(self._query(command).strip()))

    def _query_float(self, command: str) -> float:
        return float(self._query(command).strip())

    def _write_index(self, command: str, value: Any, labels: list[str]):
        index = int(value)
        if index < 0 or index >= len(labels):
            raise ValueError(f"{command} index {index} is out of range.")
        self._write(f"{command} {index:d}")
