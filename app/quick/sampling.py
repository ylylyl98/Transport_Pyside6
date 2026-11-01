"""Bounded, cached sampling descriptions; never modify acquisition parameters."""
from functools import lru_cache
import math
from collections import OrderedDict

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal, Slot

from app.utils import _frange_inc


class _SamplingTask(QRunnable):
    def __init__(self, key, completed):
        super().__init__()
        self.key, self.completed = key, completed

    def run(self):
        result = step_sampling(*self.key)
        try:
            self.completed.emit(self.key, result)
        except RuntimeError:
            pass  # The owning form was closed while the preview was computing.


class SamplingPreview(QObject):
    """Keep large exact sequence calculations off the UI thread."""
    updated = Signal()
    completed = Signal(object, object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._results, self._pending = OrderedDict(), set()
        self._pool = QThreadPool(self)
        self._pool.setMaxThreadCount(1)
        self.completed.connect(self._complete)

    def step(self, start, stop, step, unit="V", policy="voltage"):
        key = (start, stop, step, unit, policy)
        try:
            intervals = abs(float(stop) - float(start)) / float(step)
        except (ValueError, TypeError, ZeroDivisionError, OverflowError):
            return step_sampling(*key)
        if not math.isfinite(intervals) or intervals <= 4096 or intervals > 249999:
            return step_sampling(*key)
        if key in self._results:
            self._results.move_to_end(key)
            return self._results[key]
        if key not in self._pending and len(self._pending) < 8:
            self._pending.add(key)
            self._pool.start(_SamplingTask(key, self.completed))
        return f"Points / pass: calculating… · Step: {float(step):g} {unit}", False

    @Slot(object, object)
    def _complete(self, key, result):
        self._pending.discard(key)
        self._results[key] = result
        while len(self._results) > 256:
            self._results.popitem(last=False)
        self.updated.emit()


@lru_cache(maxsize=256)
def step_sampling(start, stop, step, unit="V", policy="voltage"):
    """Describe the actual endpoint policy used by the selected worker."""
    try:
        start, stop, step = float(start), float(stop), float(step)
        if not all(math.isfinite(v) for v in (start, stop, step)) or step <= 0:
            raise ValueError("Enter finite bounds and a positive Step")
        intervals = abs(stop - start) / step
        if not math.isfinite(intervals) or intervals > 249999:
            raise ValueError("More than 250,000 points; increase Step")
        direction = 1 if stop >= start else -1
        if policy == "voltage":
            points = _frange_inc(start, stop, direction * step)
        elif policy == "region":
            points = [round(start + direction * i * step, 12) for i in range(math.floor(intervals) + 1)]
            if abs(points[-1] - stop) > 1e-10:
                points.append(round(stop, 12))
            else:
                points[-1] = round(stop, 12)
        elif policy == "photo":
            points, value = [], start
            while True:
                points.append(value)
                if (direction > 0 and value >= stop - 1e-12) or (direction < 0 and value <= stop + 1e-12):
                    break
                value = min(value + step, stop) if direction > 0 else max(value - step, stop)
                if len(points) > 250000:
                    raise ValueError("More than 250,000 points; increase Step")
        else:
            raise ValueError("Unknown sampling policy")
        text = f"Points / pass: {len(points):,} · Step: {step:g} {unit}"
        overshoot = abs(points[-1] - stop) > 1e-10
        if overshoot:
            text += f" · Actual last point: {points[-1]:g} {unit} (Stop: {stop:g} {unit})"
        elif len(points) > 1 and not math.isclose(abs(points[-1] - points[-2]), step, rel_tol=1e-8, abs_tol=1e-10):
            text += f" · Last interval: {abs(points[-1] - points[-2]):g} {unit}"
        return text, overshoot
    except (ValueError, TypeError, OverflowError) as exc:
        return "Sampling unavailable: " + str(exc), True


def point_sampling(name, start, stop, count, unit="V"):
    count = int(count)
    if count <= 0:
        return f"{name}: enter a positive point count"
    if count == 1:
        return f"{name}: 1 point · Start only ({start:g} {unit})"
    return f"{name}: {count:,} points / pass · Step: {abs(stop - start) / (count - 1):g} {unit}"


def gate_sampling(values):
    count = values["n_points"]
    if values["mode"] == "Raw":
        return "\n".join(point_sampling(axis, values[f"raw_{key}_start"], values[f"raw_{key}_stop"], count)
                         for key, axis in (("vtg", "Vtg"), ("vbg", "Vbg"), ("vds", "Vds"))
                         if values[f"raw_{key}_active"] and (key != "vds" or values["vds_source"] != "None")) or "No active sweep axis"
    rows = [point_sampling(values["derived_axis"], values["derived_start"], values["derived_stop"], count)]
    if values["derived_vds_mode"] == "Swept" and values["vds_source"] != "None":
        rows.append(point_sampling("Vds", values["derived_vds_start"], values["derived_vds_stop"], count))
    return "\n".join(rows)
