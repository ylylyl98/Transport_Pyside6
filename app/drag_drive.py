"""Shared dual-lock-in calibration and DAQ acquisition, independent of Qt."""
from copy import deepcopy
import math

DUAL_SIGNALS = ('I_drag_X', 'I_drag_Y', 'I_drag_R', 'I_drive_X', 'I_drive_Y', 'I_drive_R')
DUAL_COLUMNS = ('raw_drive_X', 'raw_drive_Y', *DUAL_SIGNALS)
DUAL_UNITS = ('V', 'V', *('A',) * 6)
CHANNEL_MAP = {'drag_x': 'ai0', 'drag_y': 'ai1', 'dc': 'ai2', 'drive_x': 'ai3', 'drive_y': 'ai4'}


def measurement_mode(chain):
    config = (chain or {}).get('drag_drive') or {}
    mode = config.get('mode')
    if mode is None:
        return 'dual' if config.get('enabled') else 'ordinary'
    if mode not in {'ordinary', 'single_drag', 'single_drive', 'dual'}:
        raise ValueError(f'Unknown lock-in measurement mode: {mode}')
    return mode


def dual_enabled(chain):
    return measurement_mode(chain) == 'dual'


def acquisition_columns(chain):
    mode = measurement_mode(chain)
    if mode == 'dual':
        return DUAL_COLUMNS
    if mode in ('single_drag', 'single_drive'):
        role = mode.removeprefix('single_')
        return tuple(f'I_{role}_{axis}' for axis in ('X', 'Y', 'R'))
    return ()


def acquisition_units(chain):
    return ('V', 'V', *('A',) * 6) if dual_enabled(chain) else ('A',) * len(acquisition_columns(chain))


def _positive(value, label):
    value = float(value)
    if not math.isfinite(value) or value <= 0:
        raise ValueError(f'{label} must be finite and positive.')
    return value


def verify_measurement_calibration(config, manager, settings):
    """Caller owns both lock-in resources; only read hardware here."""
    # History/PNG workers also use the channel definitions; keep their imports hardware-free.
    from instruments.SR830 import sensitivity_value

    result = deepcopy(config)
    mode = measurement_mode({'drag_drive': result})
    if mode == 'ordinary':
        return result
    if mode in ('dual', 'single_drive'):
        result['series_resistance_ohm'] = _positive(result['series_resistance_ohm'], 'Series resistance')
    if mode in ('dual', 'single_drive') and (result.get('drive_preamp', 'SR551') != 'SR551' or result.get('drive_preamp_gain', 10.) != 10.):
        raise ValueError('This drive configuration requires SR551 fixed gain 10.')
    result.update(drive_preamp='SR551', drive_preamp_gain=10., preamp_source='user configuration',
                  channel_map=(dict(CHANNEL_MAP) if mode == 'dual' else {mode[7:] + '_x': 'ai0', mode[7:] + '_y': 'ai1', 'dc': 'ai2'}))
    daq = manager.get_session('daq')
    needed = {0, 1} | ({2} if result.get('dc_enabled', True) else set()) | ({3, 4} if mode == 'dual' else set())
    if daq is None or not needed.issubset(getattr(daq, 'ai_channel_indexes', [])):
        raise RuntimeError(f'Measurement requires DAQ channels {sorted(needed)}. Reconnect the DAQ after selecting this mode.')
    actual = {}
    roles = (('drag', 'lockin'), ('drive', 'lockin_drive')) if mode == 'dual' else ((mode[7:], 'lockin'),)
    for role, name in roles:
        session = manager.get_session(name)
        values = settings[name]['values']
        if values.get('input_config') not in (0, 1):
            raise RuntimeError(f'{role.title()} lock-in must use a voltage input (A or A-B).')
        if role == 'drive':
            if values['input_config'] != 1:
                raise RuntimeError('SR551 requires Drive lock-in A-B input and both A/B output cables.')
            external = 2 if session.model == 'SR850' else 0
            if mode == 'dual' and values.get('ref_source') != external:
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
    if mode == 'dual':
        drag, drive = (settings[name]['values'] for name in ('lockin', 'lockin_drive'))
        if drag.get('harmonic') != drive.get('harmonic') or not math.isclose(
                float(drag['frequency_hz']), float(drive['frequency_hz']), rel_tol=.001, abs_tol=.001):
            raise RuntimeError('Drag and Drive reference frequency and harmonic must match.')
    result.update(verified=True, instruments=actual)
    return result



def verify_dual_calibration(config, manager, settings):
    return verify_measurement_calibration(config, manager, settings)


def acquire_current_sample(daq, averages, amp_rate, lockin_rate, chain=None, check=None):
    mode = measurement_mode(chain)
    config = (chain or {}).get('drag_drive') or {}
    if mode != 'ordinary' and config.get('verified') is not True:
        raise ValueError('Lock-in calibration must be verified before acquisition.')
    amp_rate = _positive(amp_rate, 'Preamp gain')
    if mode in ('dual', 'single_drag'):
        lockin_rate = 10. / _positive(config['drag_sensitivity_v'], 'Drag sensitivity')
    lockin_rate = _positive(lockin_rate, 'Lock-in gain')
    if mode in ('dual', 'single_drive'):
        rs = _positive(config['series_resistance_ohm'], 'Series resistance')
        gain = _positive(config['drive_preamp_gain'], 'Drive preamp gain')
        drive_sens = _positive(config['drive_sensitivity_v'], 'Drive sensitivity')
    dc_enabled = config.get('dc_enabled', True)
    dc_gain = _positive(config.get('dc_preamp_gain_v_per_a', amp_rate), 'DC preamp gain') if dc_enabled else None
    count = int(averages)
    if count < 1:
        raise ValueError('Averages must be at least 1.')
    indexes = [0, 1] + ([2] if dc_enabled else []) + ([3, 4] if mode == 'dual' else [])
    raw = {i: 0. for i in indexes}
    for _ in range(count):
        if check:
            check()
        values = daq.acquire()
        for index in indexes:
            try:
                if hasattr(daq, 'get_ai_value'):
                    value = daq.get_ai_value(index)
                elif isinstance(values, dict):
                    value = values[f'ai{index}']
                else:
                    configured = getattr(daq, 'ai_channel_indexes', None)
                    position = list(configured).index(index) if configured is not None else index
                    value = values[position]
                value = float(value)
                if not math.isfinite(value):
                    raise ValueError('non-finite voltage')
            except (KeyError, IndexError, TypeError, ValueError) as ex:
                raise RuntimeError(f'DAQ ai{index} missing or invalid: {ex}') from ex
            raw[index] += value / count
    drag_x = raw[0] / (amp_rate * lockin_rate) if mode != 'single_drive' else math.nan
    drag_y = raw[1] / (amp_rate * lockin_rate) if mode != 'single_drive' else math.nan
    result = {'raw_X': raw[0], 'raw_Y': raw[1], 'raw_DC': raw.get(2, math.nan),
              'Ids_X': drag_x, 'Ids_Y': drag_y,
              'Ids_DC': raw[2] / dc_gain if dc_enabled else math.nan}
    if mode in ('dual', 'single_drag'):
        result.update(I_drag_X=drag_x, I_drag_Y=drag_y, I_drag_R=math.hypot(drag_x, drag_y))
    if mode in ('dual', 'single_drive'):
        x, y = (raw[3], raw[4]) if mode == 'dual' else (raw[0], raw[1])
        dx, dy = x * drive_sens / (10. * gain * rs), y * drive_sens / (10. * gain * rs)
        result.update(I_drive_X=dx, I_drive_Y=dy, I_drive_R=math.hypot(dx, dy))
        if mode == 'dual':
            result.update(raw_drive_X=x, raw_drive_Y=y)
    return result
