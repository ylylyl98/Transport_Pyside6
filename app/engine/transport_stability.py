"""Passive sample-temperature qualification; never controls a heater."""
from collections import deque
import math


class TemperatureWindow:
    def __init__(self, params, maximum_gap_s):
        self.params = params
        self.maximum_gap_s = maximum_gap_s
        self.samples = deque()
        self.last_stamp = None

    def reset(self):
        self.samples.clear()
        self.last_stamp = None

    def observe(self, stamp, temperature, now):
        p = self.params
        if (not all(isinstance(v, (int, float)) and math.isfinite(v)
                    for v in (stamp, temperature, now))
                or not 0 <= now - stamp <= self.maximum_gap_s):
            self.reset()
            return False
        if self.last_stamp is not None and stamp <= self.last_stamp:
            return self.ready()
        if self.last_stamp is not None and stamp - self.last_stamp > self.maximum_gap_s:
            self.reset()
        self.last_stamp = stamp
        if abs(temperature - p.temperature_target_k) > p.temperature_tolerance_k:
            self.samples.clear()
            return False
        self.samples.append((stamp, temperature))
        # Retain the sample spanning the beginning of the requested window.
        while len(self.samples) > 2 and self.samples[1][0] <= stamp - p.temperature_window_s:
            self.samples.popleft()
        return self.ready()

    def metrics(self):
        if len(self.samples) < 2:
            return 0.0, 0.0, 0.0
        first = self.samples[0][0]
        xs = [s[0] - first for s in self.samples]
        ys = [s[1] for s in self.samples]
        xbar, ybar = sum(xs) / len(xs), sum(ys) / len(ys)
        denominator = sum((x - xbar) ** 2 for x in xs)
        slope = (60 * sum((x - xbar) * (y - ybar) for x, y in zip(xs, ys)) / denominator
                 if denominator else float('inf'))
        return xs[-1], max(ys) - min(ys), slope

    def ready(self):
        elapsed, span, slope = self.metrics()
        return (len(self.samples) >= 3 and elapsed >= self.params.temperature_window_s
                and span <= self.params.temperature_span_k
                and abs(slope) <= self.params.temperature_drift_k_per_min)
