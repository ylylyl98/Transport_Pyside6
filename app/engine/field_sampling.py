"""Associate measured fields with electrical read times, without instrument I/O."""
from bisect import bisect_left
import math
import time


FIELD_SAMPLE_COLUMNS = (
    "B_trigger_T", "B_acquisition_start_T", "B_acquisition_end_T",
    "B_acquisition_min_T", "B_acquisition_max_T", "B_acquisition_span_T",
    "B_acquisition_start_monotonic_s", "B_acquisition_end_monotonic_s",
    "B_DAQ_read_count", "B_assignment", "B_Keithley_T", "B_Bias2_T",
    "Keithley_read_monotonic_s", "Bias2_read_monotonic_s",
)


def field_points(snapshots):
    """Use field-query midpoint when available, otherwise snapshot timestamp."""
    points = []
    for snapshot in snapshots:
        stamp = getattr(snapshot, "field_monotonic_s", None)
        if stamp is None:
            stamp = getattr(snapshot, "monotonic_s", None)
        field = getattr(snapshot, "field_t", None)
        if not all(isinstance(v, (int, float)) and math.isfinite(v) for v in (stamp, field)):
            raise RuntimeError("Invalid field telemetry during acquisition")
        if points and stamp <= points[-1][0]:
            if (stamp, field) == points[-1]:
                continue
            raise RuntimeError("Conflicting or unordered field telemetry during acquisition")
        points.append((stamp, field))
    return points


def interpolate_fields(points, timestamps, max_gap=3.):
    """Interpolate only measured brackets; never extrapolate a requested rate."""
    stamps = [p[0] for p in points]
    result = []
    for stamp in timestamps:
        index = bisect_left(stamps, stamp)
        if index < len(stamps) and stamps[index] == stamp:
            result.append(points[index][1])
        elif index == 0 or index == len(stamps):
            raise RuntimeError("Field telemetry does not bracket the acquisition")
        else:
            left, right = points[index-1], points[index]
            if right[0] - left[0] > max_gap:
                raise RuntimeError("Field telemetry gap exceeds 3 s during acquisition")
            result.append(left[1] + (right[1]-left[1]) * (stamp-left[0]) / (right[0]-left[0]))
    return result


class FieldAssociation:
    def __init__(self):
        self.daq = []
        self.keithley = None
        self.bias2 = None

    def daq_read(self, started, finished):
        self.daq.append((started, finished))

    def finish(self, row, history, cancel, check):
        reads = self.daq + [r for r in (self.keithley, self.bias2) if r is not None]
        if not reads:
            raise RuntimeError("No electrical readings to associate with the field")
        if any(not all(math.isfinite(t) for t in r) or r[1] < r[0] for r in reads):
            raise RuntimeError("Invalid electrical acquisition timestamps")
        start, end = min(r[0] for r in reads), max(r[1] for r in reads)
        deadline = time.monotonic() + 3.
        while True:
            check()
            snapshots = history()
            points = field_points(snapshots)
            if points and points[-1][0] >= end:
                break
            if time.monotonic() >= deadline:
                raise RuntimeError("Timed out waiting for field telemetry after acquisition")
            cancel.wait(.02)
        # Even with widely spaced DAQ reads, do not bridge a monitoring outage.
        for left, right in zip(points, points[1:]):
            if right[0] > start and left[0] < end and right[0]-left[0] > 3.:
                raise RuntimeError("Field telemetry gap exceeds 3 s during acquisition")
        primary = self.daq or [self.keithley or self.bias2]
        first, last = primary[0][0], primary[-1][1]
        if row.get("Field_approach") in {"no_overshoot", "oscillate"}:
            # Include both surrounding samples: an averaging window may straddle
            # a rate change or the final pause. Do not call that a constant ramp.
            times = [p[0] for p in points]
            left = max(0, bisect_left(times, first) - 1)
            right = min(len(times), bisect_left(times, last) + 1)
            window = [s for s in snapshots if times[left] <= (
                getattr(s, "field_monotonic_s", None) or s.monotonic_s) <= times[right-1]]
            phases = {getattr(s, "approach_phase", "") or "unknown" for s in window}
            row["Field_approach_phase"] = next(iter(phases)) if len(phases) == 1 else "mixed"
            row["Endpoint_approach"] = phases != {"ramp"}
            rates = {getattr(s, "approach_rate_t_per_min", None) for s in window}
            if len(rates) != 1 or None in rates:
                row["Sweep_rate_T_per_min"] = row["Sweep_rate_A_per_s"] = None
            else:
                rate = next(iter(rates))
                if row.get("Sweep_rate_T_per_min") != rate:
                    row["Sweep_rate_A_per_s"] = None
                row["Sweep_rate_T_per_min"] = rate
        midpoints = [(a+b)/2. for a, b in primary]
        fields = interpolate_fields(points, midpoints)
        edges = interpolate_fields(points, [first, last])
        # Include intermediate readbacks so a turnaround/extremum is visible.
        window_fields = edges + fields + [b for t, b in points if first <= t <= last]
        row.update(B_measured_T=math.fsum(fields)/len(fields),
                   B_acquisition_start_T=edges[0], B_acquisition_end_T=edges[1],
                   B_acquisition_min_T=min(window_fields), B_acquisition_max_T=max(window_fields),
                   B_acquisition_span_T=max(window_fields)-min(window_fields),
                   B_acquisition_start_monotonic_s=first, B_acquisition_end_monotonic_s=last,
                   B_DAQ_read_count=len(self.daq),
                   B_assignment="read_midpoints_interpolated", B_Keithley_T=None, B_Bias2_T=None)
        for read, field_key, stamp_key in (
            (self.keithley, "B_Keithley_T", "Keithley_read_monotonic_s"),
            (self.bias2, "B_Bias2_T", "Bias2_read_monotonic_s"),
        ):
            if read is not None:
                stamp = (read[0]+read[1])/2.
                row[field_key] = interpolate_fields(points, [stamp])[0]
                row[stamp_key] = stamp
        check()
        return row
