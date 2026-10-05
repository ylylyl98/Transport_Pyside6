"""Shared dual-lock-in calibration and DAQ acquisition, independent of Qt."""
from copy import deepcopy
import math

DUAL_SIGNALS = ('I_drag_X', 'I_drag_Y', 'I_drag_R', 'I_drive_X', 'I_drive_Y', 'I_drive_R')
DUAL_COLUMNS = ('raw_drive_X', 'raw_drive_Y', *DUAL_SIGNALS)
DUAL_UNITS = ('V', 'V', *('A',) * 6)
CHANNEL_MAP = {'drag_x': 'ai0', 'drag_y': 'ai1', 'dc': 'ai2', 'drive_x': 'ai3', 'drive_y': 'ai4'}


def dual_enabled(chain):
    return bool(((chain or {}).get('drag_drive') or {}).get('enabled'))


def _positive(value, label):
    value = float(value)
    if not math.isfinite(value) or value <= 0:
        raise ValueError(f'{label} must be finite and positive.')
    return value


def verify_dual_calibration(config, manager, settings):
    """Caller owns both lock-in resources; only read hardware here."""
    # History/PNG workers also use the channel definitions; keep their imports hardware-free.
    from instruments.SR830 import sensitivity_value

    result = deepcopy(config)
    result['series_resistance_ohm'] = _positive(result['series_resistance_ohm'], 'Series resistance')
    if result.get('drive_preamp', 'SR551') != 'SR551' or result.get('drive_preamp_gain', 10.) != 10.:
        raise ValueError('This drive configuration requires SR551 fixed gain 10.')
    result.update(drive_preamp='SR551', drive_preamp_gain=10., preamp_source='user configuration',
                  channel_map=dict(CHANNEL_MAP))
    daq = manager.get_session('daq')
    if daq is None or not set(range(5)).issubset(getattr(daq, 'ai_channel_indexes', [])):
        raise RuntimeError('Drag / Drive requires DAQ ai0..ai4. Reconnect the DAQ after selecting this mode.')
    actual = {}
    for role, name in (('drag', 'lockin'), ('drive', 'lockin_drive')):
        session = manager.get_session(name)
        values = settings[name]['values']
        if values.get('input_config') not in (0, 1):
            raise RuntimeError(f'{role.title()} lock-in must use a voltage input (A or A-B).')
        if role == 'drive':
            if values['input_config'] != 1:
                raise RuntimeError('SR551 requires Drive lock-in A-B input and both A/B output cables.')
            external = 2 if session.model == 'SR850' else 0
            if values.get('ref_source') != external:
                raise RuntimeError('Drive lock-in must use External reference from the Drag lock-in.')
        if getattr(session, '_control_state_uncertain', False):
            raise RuntimeError(f'{role.title()} lock-in state is unverified. Refresh its panel first.')
        output = session.read_analog_output_settings()
        if output.get('ch1') != 'X' or output.get('ch2') != 'Y':
            raise RuntimeError(f'{role.title()} CH1/CH2 must output X/Y for DAQ conversion.')
        for axis in ('x', 'y'):
            if output.get(f'{axis}_offset') != 0 or output.get(f'{axis}_expand') != 1:
                raise RuntimeError(f'{role.title()} {axis.upper()} requires Offset 0 and Expand 1 for DAQ conversion.')
        sensitivity = sensitivity_value(values['sensitivity'])
        result[f'{role}_sensitivity_v'] = _positive(sensitivity, f'{role} sensitivity')
        actual[role] = {**deepcopy(settings[name]), 'analog_outputs': output}
    drag, drive = (settings[name]['values'] for name in ('lockin', 'lockin_drive'))
    if drag.get('harmonic') != drive.get('harmonic') or not math.isclose(
            float(drag['frequency_hz']), float(drive['frequency_hz']), rel_tol=.001, abs_tol=.001):
        raise RuntimeError('Drag and Drive reference frequency and harmonic must match.')
    result.update(verified=True, instruments=actual)
    return result


def acquire_current_sample(daq, averages, amp_rate, lockin_rate, chain=None, check=None):
    dual = dual_enabled(chain)
    config = (chain or {}).get('drag_drive') or {}
    if dual:
        if config.get('verified') is not True:
            raise ValueError('Drag / Drive calibration must be verified before acquisition.')
        rs = _positive(config['series_resistance_ohm'], 'Series resistance')
        gain = _positive(config['drive_preamp_gain'], 'Drive preamp gain')
        drive_sens = _positive(config['drive_sensitivity_v'], 'Drive sensitivity')
        lockin_rate = 10. / _positive(config['drag_sensitivity_v'], 'Drag sensitivity')
    amp_rate = _positive(amp_rate, 'Preamp gain')
    lockin_rate = _positive(lockin_rate, 'Lock-in gain')
    count = int(averages)
    if count < 1:
        raise ValueError('Averages must be at least 1.')
    raw = [0.] * (5 if dual else 3)
    for _ in range(count):
        if check:
            check()
        values = daq.acquire()
        for index in range(len(raw)):
            try:
                value = daq.get_ai_value(index) if hasattr(daq, 'get_ai_value') else values[f'ai{index}']
                value = float(value)
                if not math.isfinite(value):
                    raise ValueError('non-finite voltage')
            except (KeyError, IndexError, TypeError, ValueError) as ex:
                raise RuntimeError(f'DAQ ai{index} missing or invalid: {ex}') from ex
            raw[index] += value / count
    result = {'raw_X': raw[0], 'raw_Y': raw[1], 'raw_DC': raw[2],
              'Ids_X': raw[0] / (amp_rate * lockin_rate),
              'Ids_Y': raw[1] / (amp_rate * lockin_rate), 'Ids_DC': raw[2] / amp_rate}
    if dual:
        dx, dy = raw[3] * drive_sens / (10. * gain * rs), raw[4] * drive_sens / (10. * gain * rs)
        result.update(raw_drive_X=raw[3], raw_drive_Y=raw[4],
                      I_drag_X=result['Ids_X'], I_drag_Y=result['Ids_Y'],
                      I_drag_R=math.hypot(result['Ids_X'], result['Ids_Y']),
                      I_drive_X=dx, I_drive_Y=dy, I_drive_R=math.hypot(dx, dy))
    return result
