from __future__ import annotations

import math
from collections import deque
from functools import lru_cache

from app.constants import (
    GATE_BIAS_RAMP_STEP_T, GATE_BIAS_RAMP_STEP_V,
    SAFE_RAMP_STEP_T, SAFE_RAMP_STEP_V,
)
from app.models import CoParams, Connections
from app.workers.cosweep import build_cosweep_points
from instruments.DaqCard import DaqCard


# Aggregate reference from YZ324 run 919061ec6b1b_20260928_210934.
# Metadata: created 2026-09-28T21:09:45, completed 21:33:35, status finished,
# safe_state.ok true. CSV: 1111 points. Completed includes final zero return.
# This is a measured whole-run overhead, NOT a measured DAQ sample duration.
HISTORY_DURATION_SECONDS = 1430.0
HISTORY_POINT_COUNT = 1111
# Completed Ave=3 run 20260928_221602: 2026-09-28T22:16:19 to
# 2026-09-29T11:35:23, finished, safe_state.ok true, 41 x 501 points.
# Whole-run duration includes startup and final zero return.
HISTORY_AVE3_DURATION_SECONDS = 47944.0
HISTORY_AVE3_POINT_COUNT = 20541


def historical_cosweep_match(params: CoParams, connections: Connections | None,
                             device_id: str) -> bool:
    """Limit this local reference to the observed sample, route and instruments."""
    return bool(
        connections is not None and device_id == "YZ324"
        and params.coordinate_mode == "Derived"
        and params.axis_fast == "Vds" and params.axis_slow == "Doping"
        and params.vds_source == "Keithley 2400"
        and all(getattr(connections, name) == value for name, value in {
            "gate1": "GPIB0::2::INSTR", "gate2": "GPIB0::3::INSTR",
            "gate3": "GPIB0::1::INSTR", "daq_dev": "Dev1",
            "lockin": "GPIB0::8::INSTR", "gate1_mode": "voltage_2w",
            "gate2_mode": "voltage_2w", "gate3_mode": "voltage_2w",
        }.items())
    )


def cosweep_calibration_description(params: CoParams, connections: Connections | None,
                                    device_id: str) -> str:
    if not historical_cosweep_match(params, connections, device_id):
        return "Model estimate; no matching historical calibration."
    count = params.n_sample
    if count in (1, 3):
        basis = f"Historical calibration from completed Ave={count} reference. "
    elif count == 2:
        basis = "Ave=2 interpolated between completed Ave=1 and Ave=3 references. "
    else:
        basis = f"Ave={count} extrapolated from completed Ave=1 and Ave=3 references. "
    return (basis + "YZ324: Ave=1, 1111 points / 1430 s; Ave=3, "
            "20541 points / 47944 s, both including cleanup. "
            "Linear per-point residual after modeled delay and ramps; "
            "different trajectories may confound the inferred average cost. "
            "Not a measured per-read duration or independent validation. "
            "Extrapolation is less certain farther from Ave=1..3; "
            "changed instrument settings may affect timing. Live ETA adapts during acquisition.")


@lru_cache(maxsize=2)
def _historical_extra_seconds_per_point(n_sample: int = 1) -> float:
    if n_sample == 3:
        reference = CoParams(
            coordinate_mode="Derived", axis_fast="Vds", axis_slow="Doping",
            doping_start=-1.5, doping_stop=1.5, doping_step=0.075, efield_start=0,
            vds_start=0.8, vds_stop=1.3, vds_step=0.001,
            vg_ramp=0.1, vds_ramp=0.05, ratio=1, ratio_target="Vtg",
            delay=0.4, n_sample=3, vds_source="Keithley 2400")
        # No connections: model startup, all passes and cleanup without recursion.
        modeled = estimate_cosweep_seconds(reference)
        return max(0.0, (HISTORY_AVE3_DURATION_SECONDS - modeled) / HISTORY_AVE3_POINT_COUNT)

    reference = CoParams(
        coordinate_mode="Derived", axis_fast="Vds", axis_slow="Doping",
        doping_start=-1, doping_stop=1, doping_step=0.2, efield_start=0,
        vds_start=0.9, vds_stop=1.1, vds_step=0.002,
        vg_ramp=0.1, vds_ramp=0.05, ratio=1, ratio_target="Vtg",
        delay=0.4, n_sample=1, vds_source="Keithley 2400",
    )
    # No connections: evaluate the uncalibrated model without recursing.
    modeled = estimate_cosweep_seconds(reference)
    return max(0.0, (HISTORY_DURATION_SECONDS - modeled) / HISTORY_POINT_COUNT)


def format_cosweep_duration(seconds: float) -> str:
    # Round before splitting to avoid values such as 59 min 60.00 s.
    centiseconds = max(0, round(seconds * 100))
    hours, remainder = divmod(centiseconds, 360000)
    minutes, remainder = divmod(remainder, 6000)
    seconds_text = f"{remainder / 100:.2f} s"
    if hours:
        return f"{hours} h {minutes} min {seconds_text}"
    if minutes:
        return f"{minutes} min {seconds_text}"
    return seconds_text


def estimate_cosweep_seconds(
    params: CoParams, *, points: list[dict] | None = None,
    io_seconds: float = 0.02, sample_seconds: float = 0.01,
    connections: Connections | None = None, device_id: str = "",
) -> float:
    """Approximate worker duration from zero output through final zero return.

    Keithley queries/writes assume 20 ms each, DAQ samples 10 ms,
    Keithley current reads 100 ms, and file/plot updates 10 ms per point.
    These are planning allowances, not measured hardware timings.
    Matching hardware/trajectory adds a cached historical residual per point;
    explicit delay, extra averages and ramp costs retain their own scaling.
    DAQ ramp step limits/delays and explicit safe-ramp sleeps follow the drivers.
    """
    trajectory = build_cosweep_points(params) if points is None else points
    if not trajectory:
        raise ValueError("Cannot estimate an empty trajectory.")
    current = {"Vtg": 0.0, "Vbg": 0.0, "Vds": 0.0}
    daq_bias = params.vds_source.startswith("NI DAQ")
    derived = params.coordinate_mode == "Derived"
    axes = {params.axis_fast, params.axis_slow}
    elapsed = len(trajectory) * (max(0.0, params.delay) + params.n_sample * sample_seconds + 0.01)
    if not daq_bias:
        elapsed += len(trajectory) * 0.1

    def ramp(axis: str, target: float, *, safe: bool = False, initial: bool = False) -> float:
        distance = abs(float(target) - current[axis])
        current[axis] = float(target)
        is_daq = axis == "Vds" and daq_bias
        if safe:
            step, delay = SAFE_RAMP_STEP_V, SAFE_RAMP_STEP_T
        elif initial:
            step, delay = GATE_BIAS_RAMP_STEP_V, GATE_BIAS_RAMP_STEP_T
        else:
            step = params.vds_ramp if axis == "Vds" else params.vg_ramp
            delay = DaqCard.DEFAULT_RAMP_DELAY_S if is_daq else 0.0
        if is_daq:
            step = min(step, DaqCard.MAX_RAMP_STEP_V)
        steps = max(0, math.ceil(distance / step - 1e-9))
        if safe or initial:
            return (sample_seconds if is_daq else 0.0) + steps * (delay + io_seconds)
        # Keithley ramp queries its starting voltage and includes a final write.
        if not is_daq:
            return (steps + 2) * io_seconds
        return sample_seconds + steps * (delay + io_seconds)

    if not derived:
        for axis, target in (("Vtg", params.vtg_start), ("Vbg", params.vbg_start)):
            if axis not in axes:
                elapsed += ramp(axis, target, initial=True)
    if "Vds" not in axes:
        elapsed += ramp("Vds", params.vds_start)
    previous_pass = None
    for point in trajectory:
        new_pass = point["pass_index"] != previous_pass
        if derived:
            elapsed += ramp("Vtg", point["vtg"]) + ramp("Vbg", point["vbg"])
            if params.axis_fast == "Vds" or (params.axis_slow == "Vds" and new_pass):
                elapsed += ramp("Vds", point["vds"])
        else:
            if params.axis_slow != "None" and new_pass:
                elapsed += ramp(params.axis_slow, point["slow_value"])
            elapsed += ramp(params.axis_fast, point["fast_value"])
        previous_pass = point["pass_index"]
    for axis in current:
        elapsed += ramp(axis, 0.0, safe=True)
    if historical_cosweep_match(params, connections, device_id):
        one = _historical_extra_seconds_per_point(1)
        three = _historical_extra_seconds_per_point(3)
        # Baseline already includes sample_seconds per average. Interpolating
        # residuals adds the observed excess without counting that time twice.
        residual = one + (params.n_sample - 1) * (three - one) / 2
        elapsed += len(trajectory) * residual
    return elapsed


def estimate_cosweep_cleanup_seconds(params: CoParams, last_point: dict) -> float:
    """Reserve final zero ramps separately from observed acquisition speed."""
    elapsed = 0.0
    for axis, key in (("Vtg", "vtg"), ("Vbg", "vbg"), ("Vds", "vds")):
        is_daq = axis == "Vds" and params.vds_source.startswith("NI DAQ")
        step = min(SAFE_RAMP_STEP_V, DaqCard.MAX_RAMP_STEP_V) if is_daq else SAFE_RAMP_STEP_V
        steps = max(0, math.ceil(abs(last_point[key]) / step - 1e-9))
        elapsed += steps * (SAFE_RAMP_STEP_T + 0.02) + (0.01 if is_daq else 0)
    return elapsed


class LiveCoSweepTiming:
    """Bounded-memory ETA based on worker active time, not UI delivery times."""
    def __init__(self, total_points: int, initial_seconds: float, cleanup_seconds: float):
        self.total_points = total_points
        self.cleanup_seconds = max(0.0, cleanup_seconds)
        self.initial_rate = max(0.0, initial_seconds - self.cleanup_seconds) / max(1, total_points)
        self.intervals = deque(maxlen=20)
        self.completed = 0
        self.elapsed = 0.0
        self._last_point_elapsed = 0.0
        self.remaining_seconds = max(initial_seconds, self.cleanup_seconds)

    @property
    def total_seconds(self):
        return self.elapsed + self.remaining_seconds

    def update(self, completed: int, active_elapsed: float):
        if completed <= self.completed or active_elapsed < self.elapsed:
            return
        completed = min(completed, self.total_points)
        if self.completed:
            count = completed - self.completed
            if count <= 0:
                return
            self.intervals.append((active_elapsed - self._last_point_elapsed) / count)
        self.completed = completed
        self.elapsed = active_elapsed
        self._last_point_elapsed = active_elapsed
        weight = min(len(self.intervals) / 5, 1.0)
        observed = sum(self.intervals) / len(self.intervals) if self.intervals else self.initial_rate
        rate = (1 - weight) * self.initial_rate + weight * observed
        self.remaining_seconds = (self.total_points - completed) * rate + self.cleanup_seconds

    def finish(self, active_elapsed: float):
        self.elapsed = max(self.elapsed, active_elapsed)
        self.remaining_seconds = 0.0
