"""Pure planning for APS100 field approaches."""
import math

APPROACHES = ('linear', 'no_overshoot', 'oscillate')
HYSTERESIS_LINEAR_APPROACH = 'linear_no_backward'


def no_overshoot_intermediate(start, target, tolerance, profile='standard', *,
                              preparation=False, same_rate_band=True):
    """Plan an optional shorter slow segment, without changing either rate.

    This is an experimental geometry change, not a model of magnet dynamics.
    Keep the standard endpoint for preparation, near/crossing zero, range
    transitions, and moves too short to retain a useful reduced-rate segment.
    """
    if profile not in ('standard', 'short_tail'):
        raise ValueError('Invalid APS100 No Overshoot profile')
    start, target, tolerance = map(float, (start, target, tolerance))
    if not all(math.isfinite(v) for v in (start, target, tolerance)) or tolerance <= 0:
        raise ValueError('Invalid No Overshoot planning inputs')
    direction = 1 if target >= start else -1
    distance = abs(target - start)
    quantum = .0001
    standard = round((start + (target - start) * .8) / quantum) * quantum
    info = dict(requested_profile=profile, effective_profile='standard',
                reason='standard', reduced_rate_factor=.2)
    endpoint = standard
    if profile == 'short_tail':
        minimum_tail = max(4. * tolerance, 4. * quantum)
        if preparation:
            info['reason'] = 'preparation'
        elif start * target <= 0 or min(abs(start), abs(target)) <= minimum_tail:
            info['reason'] = 'near_or_crossing_zero'
        elif not same_rate_band:
            info['reason'] = 'rate_band_transition'
        else:
            tail = max(distance * .1, minimum_tail)
            # Round away from the target so the minimum slow distance survives
            # endpoint quantization, for either sweep direction.
            candidate = direction * math.floor((direction * target - tail) / quantum + 1e-9) * quantum
            if (direction * (candidate - standard) >= quantum - 1e-12
                    and direction * (candidate - start) > tolerance
                    and direction * (target - candidate) >= minimum_tail - 1e-12):
                endpoint = candidate
                info.update(effective_profile='short_tail', reason='shorter_slow_segment')
            else:
                info['reason'] = 'short_move'
    info.update(intermediate_t=endpoint, planned_slow_distance_t=max(0., direction * (target-endpoint)))
    return endpoint, info


def normalize_approach(value, default='linear', *, allow_hysteresis=False):
    name = default if value is None or str(value).strip().lower() == 'auto' else str(value).strip().lower()
    if name == HYSTERESIS_LINEAR_APPROACH and allow_hysteresis:
        return name
    if name not in APPROACHES:
        raise ValueError(f'Unknown field approach: {name}')
    return name


def approach_targets(start, target, tolerance, approach, limit):
    start, target, tolerance, limit = map(float, (start, target, tolerance, limit))
    if not all(math.isfinite(v) for v in (start, target, tolerance, limit)) or tolerance <= 0 or limit <= 0:
        raise ValueError('Invalid field approach inputs')
    if max(abs(start), abs(target)) > limit:
        raise ValueError('Field approach exceeds safe control limit')
    mode = normalize_approach(approach)
    distance = target - start
    if abs(distance) <= tolerance:
        return ()
    if mode == 'linear':
        return (target,)
    if mode == 'no_overshoot':
        result, current = [], start
        while abs(target - current) > 2 * tolerance:
            current += .7 * (target - current)
            result.append(current)
            if len(result) > 100:
                raise ValueError('Too many approach stages')
        return tuple(result + [target])
    result, amplitude = [], abs(distance) * .3
    sign = 1 if distance > 0 else -1
    while amplitude > 2 * tolerance:
        value = target + sign * amplitude
        if abs(value) > limit:
            raise ValueError('Oscillation excursion exceeds safe control limit')
        result.append(value)
        sign, amplitude = -sign, amplitude * .3
        if len(result) > 100:
            raise ValueError('Too many approach stages')
    return tuple(result + [target])
