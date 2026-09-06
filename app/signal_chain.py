from __future__ import annotations

from dataclasses import asdict, dataclass
import math


def _positive(value: float) -> float:
    value = float(value)
    if not math.isfinite(value) or value <= 0:
        raise ValueError("Signal-chain sensitivity must be finite and positive")
    return value


def preamp_gain_v_per_a(sensitivity_a: float) -> float:
    """Preamp front-panel sensitivity is amperes per volt of output."""
    return 1.0 / _positive(sensitivity_a)


def sr830_xy_output_gain(sensitivity_v: float) -> float:
    """Analog X/Y BNC gain, with zero offset and unity expansion (SR830 manual)."""
    return 10.0 / _positive(sensitivity_v)


def current_from_lockin_daq_voltage(voltage_v, preamp_sensitivity_a, lockin_sensitivity_v):
    return float(voltage_v) / (preamp_gain_v_per_a(preamp_sensitivity_a)
                              * sr830_xy_output_gain(lockin_sensitivity_v))


@dataclass(frozen=True)
class SignalChainSnapshot:
    frequency_hz: float = 1000.0
    lockin_sensitivity_v: float = 0.1
    preamp_sensitivity_a: float = 1e-7
    frequency_source: str = "saved/manual"
    lockin_sensitivity_source: str = "saved/manual"
    preamp_sensitivity_source: str = "manual calibration"

    @property
    def preamp_gain_v_per_a(self) -> float:
        return preamp_gain_v_per_a(self.preamp_sensitivity_a)

    @property
    def lockin_scale(self) -> float:
        return sr830_xy_output_gain(self.lockin_sensitivity_v)

    def to_dict(self) -> dict[str, object]:
        result = asdict(self)
        result["preamp_gain_v_per_a"] = self.preamp_gain_v_per_a
        result["lockin_scale"] = self.lockin_scale
        return result


def engineering_value(value: float, unit: str, separator: str = "") -> str:
    """Format a physical value with compact, filename-safe SI units."""
    value = float(value)
    magnitude = abs(value)
    prefixes = (
        (1e9, "G"),
        (1e6, "M"),
        (1e3, "k"),
        (1.0, ""),
        (1e-3, "m"),
        (1e-6, "u"),
        (1e-9, "n"),
        (1e-12, "p"),
        (1e-15, "f"),
    )
    factor, prefix = prefixes[-1]
    for candidate_factor, candidate_prefix in prefixes:
        if magnitude >= candidate_factor:
            factor, prefix = candidate_factor, candidate_prefix
            break
    return f"{value / factor:.6g}{separator}{prefix}{unit}"


def signal_chain_filename_parts(snapshot: SignalChainSnapshot | dict) -> list[str]:
    if isinstance(snapshot, SignalChainSnapshot):
        frequency = snapshot.frequency_hz
        lockin = snapshot.lockin_sensitivity_v
        preamp = snapshot.preamp_sensitivity_a
    else:
        frequency = float(snapshot.get("frequency_hz", 1000.0))
        lockin = float(snapshot.get("lockin_sensitivity_v", 0.1))
        preamp = float(snapshot.get("preamp_sensitivity_a", 1e-7))
    return [
        f"freq_{engineering_value(frequency, 'Hz')}",
        f"lia_{engineering_value(lockin, 'V')}",
        f"preamp_{engineering_value(preamp, 'A')}",
    ]


def signal_chain_metadata(snapshot: SignalChainSnapshot | dict) -> dict[str, object]:
    return snapshot.to_dict() if isinstance(snapshot, SignalChainSnapshot) else dict(snapshot)
