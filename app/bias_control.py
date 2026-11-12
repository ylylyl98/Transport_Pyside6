"""Shared second-bias target calculation and preflight safety checks."""
import math
import time

from app.constants import V_LIMIT, SAFE_RAMP_STEP_T, SAFE_RAMP_STEP_V
from app.utils import safe_ramp
from app.voltage_resolution import source_resolution_status, validate_source_targets


def bias2_target(params, bias1_voltage):
    mode = getattr(params, "bias2_mode", "Disabled")
    if mode == "Disabled":
        return None
    if mode == "Fixed":
        return float(params.bias2_voltage)
    if mode == "Follow":
        return float(bias1_voltage) + float(params.bias2_offset)
    raise ValueError(f"Unknown Bias2 mode: {mode}")


def validate_bias2(params, points, manager, bias1_enabled=True, bias1_source=None, refresh=False):
    """Validate all commanded targets against the connected source's protection and resolution."""
    mode = getattr(params, "bias2_mode", "Disabled")
    if mode == "Disabled":
        return []
    if mode not in {"Fixed", "Follow"}:
        raise ValueError(f"Unknown Bias2 mode: {mode}")
    if mode == "Follow" and not bias1_enabled:
        raise ValueError("Bias2 Follow requires Bias1 to be enabled.")
    ramp = float(getattr(params, "bias2_ramp", 0.05))
    if not math.isfinite(ramp) or ramp <= 0:
        raise ValueError("Bias2 ramp step must be finite and greater than zero.")
    session = manager.get_session("g4")
    if not manager.is_connected("g4") or manager.needs_reconnect("g4"):
        raise ValueError("Bias2 G4 is disconnected or requires reconnection.")
    if not manager.protection_is_applied("g4"):
        raise ValueError("Bias2 G4 protection has not been verified.")
    if session is None or not manager.is_voltage_source_mode("g4"):
        raise ValueError("Bias2 requires G4 connected in voltage-source mode.")
    for name in ("g1", "g2", "g3"):
        other = manager.get_session(name)
        if other is session:
            raise ValueError(f"Bias2 G4 must be a separate instrument from {name.upper()}.")
    limit = min(V_LIMIT, float(manager.applied_gate_voltage_limit("g4")))
    if not math.isfinite(limit) or limit <= 0:
        raise ValueError("Bias2 source protection limit is unavailable.")
    values1 = [float(p["vds"] if isinstance(p, dict) else p) for p in points]
    source1 = bias1_source if bias1_source is not None else getattr(params, "vds_source", None)
    if mode == "Follow" and source1 == "Keithley 2400":
        validate_source_targets(manager.get_session("g3"), values1, "Bias1", refresh=refresh)
    targets = [bias2_target(params, value) for value in values1]
    if not targets:
        raise ValueError("Bias2 requires at least one target.")
    for target in targets:
        if not math.isfinite(target) or abs(target) > limit + 1e-9:
            raise ValueError(f"Bias2 target {target:g} V exceeds verified {limit:g} V protection.")
    validate_source_targets(session, targets, "Bias2", ramp_step=ramp, refresh=refresh)
    return targets


def ramp_bias2(session, params, bias1_voltage, check_fn=None):
    target = bias2_target(params, bias1_voltage)
    if target is None:
        return None
    if session is None:
        raise RuntimeError("Bias2 G4 is not connected.")
    safe_ramp(session.set_voltage, getattr(session, "voltage", None) or 0.0,
              target, float(getattr(params, "bias2_ramp", .05)), SAFE_RAMP_STEP_T, check_fn)
    return target


def zero_bias2(session, params):
    if getattr(params, "bias2_mode", "Disabled") != "Disabled" and session is not None:
        safe_ramp(session.set_voltage, getattr(session, "voltage", None) or 0.0,
                  0.0, SAFE_RAMP_STEP_V, SAFE_RAMP_STEP_T)


def read_bias2(session, params):
    if getattr(params, "bias2_mode", "Disabled") == "Disabled" or session is None:
        return None
    values = session.acquire()
    return values.get("current", getattr(session, "current", None))


def ramp_pair(primary, daq, channel, secondary, params, target, primary_step, check_fn=None, step_time=SAFE_RAMP_STEP_T):
    """Interleave bounded source steps, starting from fresh instrument readbacks."""
    if secondary is None:
        raise RuntimeError("Bias2 G4 is not connected.")
    target = float(target)
    target2 = bias2_target(params, target)
    if target2 is None:
        raise ValueError("Paired ramp requires Bias2 enabled.")
    if check_fn:
        check_fn()
    if primary is not None:
        read1 = getattr(primary, "get_voltage_setpoint", None)
        start1 = float(read1() if callable(read1) else getattr(primary, "voltage"))
        set1 = getattr(primary, "set_voltage_fast", primary.set_voltage)
        quantum1 = validate_source_targets(primary, (target,), "Bias1", primary_step)
    else:
        start1 = float(daq.adopt_measured_output_as_ramp_start(channel))
        set1 = lambda value: daq.set_voltage(channel, value)
        quantum1 = None
    read2 = getattr(secondary, "get_voltage_setpoint", None)
    start2 = float(read2() if callable(read2) else getattr(secondary, "voltage"))
    set2 = getattr(secondary, "set_voltage_fast", secondary.set_voltage)
    quantum2 = validate_source_targets(secondary, (target2,), "Bias2", params.bias2_ramp)
    if not all(math.isfinite(v) for v in (start1, start2, target, target2)):
        raise ValueError("Paired ramp setpoints must be finite.")
    step1 = float(primary_step)
    if primary is None:
        from instruments.DaqCard import DaqCard
        step1 = min(step1, DaqCard.MAX_RAMP_STEP_V)
    step2 = float(params.bias2_ramp)
    if not math.isfinite(step1) or not math.isfinite(step2) or min(step1, step2) <= 0:
        raise ValueError("Paired ramp steps must be finite and positive.")
    def effective(step, q):
        if q is None:
            return step
        ticks = math.floor((step + 1e-12) / q)
        if ticks < 1:
            raise ValueError("Ramp step is smaller than the source resolution.")
        return ticks * q
    step1, step2 = effective(step1, quantum1), effective(step2, quantum2)
    # An existing programmed level may be off-grid. Move toward the validated
    # destination by less than one quantum before interpolating the pair.
    def align_start(value, destination, quantum, setter):
        if quantum is None or abs(value / quantum - round(value / quantum)) <= 1e-7:
            return value
        tick = math.floor(value / quantum) if value > destination else math.ceil(value / quantum)
        aligned = round(tick * quantum, 12)
        if check_fn:
            check_fn()
        setter(aligned)
        if step_time:
            time.sleep(step_time)
        return aligned
    start1 = align_start(start1, target, quantum1, set1)
    start2 = align_start(start2, target2, quantum2, set2)
    count = max(1, math.ceil(abs(target - start1) / step1), math.ceil(abs(target2 - start2) / step2))
    prev1, prev2 = start1, start2
    for i in range(1, count + 1):
        if check_fn:
            check_fn()
        value1 = target if i == count else start1 + (target - start1) * i / count
        value2 = target2 if i == count else start2 + (target2 - start2) * i / count
        if quantum1 and i != count:
            value1 = round(value1 / quantum1) * quantum1
        if i != count:
            value2 = round(value2 / quantum2) * quantum2
        if abs(value1 - prev1) > 1e-12:
            set1(value1)
            prev1 = value1
        if check_fn:
            check_fn()
        if abs(value2 - prev2) > 1e-12:
            set2(value2)
            prev2 = value2
        if step_time:
            time.sleep(step_time)
    return target2
