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
    lockin_settings: dict | None = None
    experiment_context: dict | None = None
    ac_contact: str = ""
    ac_voltage_ratio: float | None = None
    drag_drive: dict | None = None

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
        _add_ac_estimate_metadata(result)
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


def sample_ac_voltage_estimate(data: dict) -> float | None:
    """User-estimated Vsample/Vsource; never a measured sample voltage."""
    ratio = data.get('ac_voltage_ratio')
    amplitude = ((data.get('lockin_settings') or {}).get('values') or {}).get('sine_out_v')
    if not all(isinstance(v, (int, float)) and math.isfinite(v) and v > 0 for v in (ratio, amplitude)):
        return None
    estimate = ratio * amplitude
    return estimate if math.isfinite(estimate) and estimate > 0 else None


def _add_ac_estimate_metadata(data: dict) -> None:
    ratio = data.get('ac_voltage_ratio')
    valid = isinstance(ratio, (int, float)) and math.isfinite(ratio) and ratio > 0
    data['ac_voltage_ratio_source'] = 'user estimate' if valid else 'not specified'
    data['ac_voltage_ratio_definition'] = 'sample-side voltage / Lock-in output voltage; same amplitude convention'
    data['sample_ac_voltage_estimate_v'] = sample_ac_voltage_estimate(data)


def signal_chain_filename_parts(snapshot: SignalChainSnapshot | dict) -> list[str]:
    if isinstance(snapshot, SignalChainSnapshot):
        frequency = snapshot.frequency_hz
        lockin = snapshot.lockin_sensitivity_v
        preamp = snapshot.preamp_sensitivity_a
    else:
        frequency = float(snapshot.get("frequency_hz", 1000.0))
        lockin = float(snapshot.get("lockin_sensitivity_v", 0.1))
        preamp = float(snapshot.get("preamp_sensitivity_a", 1e-7))
    data = snapshot.to_dict() if isinstance(snapshot, SignalChainSnapshot) else snapshot
    panel = data.get("lockin_settings") or {}
    values = panel.get("values") or {}
    verified = panel.get("source") == "instrument"
    if verified:
        measured_frequency = values.get("frequency_hz")
        if isinstance(measured_frequency, (int, float)) and math.isfinite(measured_frequency) and measured_frequency > 0:
            frequency = measured_frequency
    parts = [
        engineering_value(frequency, "Hz"),
        f"LIA{engineering_value(lockin, 'V')}",
        f"Pre{engineering_value(preamp, 'A')}",
    ]
    dual = data.get('drag_drive') or {}
    if dual.get('enabled'):
        parts.extend(['DragDrive', 'Rs' + engineering_value(dual['series_resistance_ohm'], 'Ohm')])
    amplitude = values.get("sine_out_v")
    if isinstance(amplitude, (int, float)) and math.isfinite(amplitude) and amplitude > 0:
        parts.append(('ACout' if verified else 'ACset') + engineering_value(amplitude, 'V'))
    estimate = sample_ac_voltage_estimate(data)
    if estimate is not None:
        parts.append('VacEst' + engineering_value(estimate, 'V'))
    contact = str(data.get('ac_contact') or '').strip()
    if contact:
        parts.append('AC' + contact)
    return parts


def signal_chain_metadata(snapshot: SignalChainSnapshot | dict) -> dict[str, object]:
    data = snapshot.to_dict() if isinstance(snapshot, SignalChainSnapshot) else dict(snapshot)
    _add_ac_estimate_metadata(data)
    return data
