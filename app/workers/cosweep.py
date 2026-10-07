from __future__ import annotations

import math
import csv
import math
import datetime
import os
import time

from PySide6 import QtCore

from app.constants import (
    GATE_BIAS_RAMP_STEP_T,
    GATE_BIAS_RAMP_STEP_V,
    SAFE_RAMP_STEP_T,
    SAFE_RAMP_STEP_V,
    V_LIMIT,
)
from app.cosweep_output import map_filename_parts
from app.gate_transform import derived_to_gates, gates_to_derived, normalize_ratio_target
from app.keithley_modes import KEITHLEY_MODE_VOLTAGE_2W
from app.models import CoParams, Connections, SaveRoot
from app.plot_x_axis import record_x_value, resolve_map_x_axis
from app.result_channels import KEITHLEY_CHANNEL
from app.run_output import planned_output_at, unique_planned_output, new_run_id, compose_output_stem, update_run_metadata_status, write_run_metadata
from app.utils import _frange_inc, safe_ramp
from app.workers.base import RunStopped, RunWorker


def sequence_point_count(start, stop, step):
    start, stop, step = float(start), float(stop), abs(float(step))
    if not all(math.isfinite(v) for v in (start, stop, step)):
        raise ValueError("Sweep values must be finite.")
    if step == 0:
        if abs(stop-start) <= 1e-12:
            return 1
        raise ValueError("Swept steps must be greater than zero.")
    return math.ceil(round(abs(stop-start)/step, 9)) + 1


def _sequence(start: float, stop: float, step: float) -> list[float]:
    """Inclusive sequence with a positive user-facing step."""
    start, stop, step = float(start), float(stop), abs(float(step))
    if step <= 0:
        if abs(stop - start) <= 1e-12:
            return [start]
        raise ValueError("Swept steps must be greater than zero.")
    if sequence_point_count(start, stop, step) > 250000:
        raise ValueError("This setup exceeds the limit of 250,000 points.")
    return _frange_inc(start, stop, step if stop >= start else -step)


def validate_cosweep_params(params: CoParams) -> None:
    """Validate the complete trajectory before any output or hardware motion."""
    mode = str(getattr(params, "coordinate_mode", "Raw") or "Raw")
    if mode not in {"Raw", "Derived"}:
        raise ValueError("Sweep coordinates must be Raw or Derived.")
    if int(params.n_sample) < 1:
        raise ValueError("Averages must be at least 1.")
    if params.vg_ramp <= 0 or params.vds_ramp <= 0:
        raise ValueError("Gate and Vds ramp steps must be greater than zero.")
    normalize_ratio_target(params.ratio_target)
    if params.regions:
        _merged_region_rows(params)
        if not math.isfinite(params.vds_start) or abs(params.vds_start) > V_LIMIT:
            raise ValueError(f"Fixed Vds exceeds the {V_LIMIT:g} V limit.")
        if params.vds_start != params.vds_stop:
            raise ValueError("Merged gate maps require fixed Vds.")
        return
    if mode == "Derived":
        if params.axis_slow == "None" or params.axis_fast not in {"Doping", "E-field", "Vds"} or params.axis_slow not in {"Doping", "E-field", "Vds"} or params.axis_fast == params.axis_slow:
            raise ValueError("Derived 2D maps require two distinct axes from Doping, E-field, and Vds.")
        if abs(float(params.ratio)) < 1e-12:
            raise ValueError("Derived trajectory requires a non-zero ratio.")
        if "Vds" not in (params.axis_fast, params.axis_slow) and abs(float(params.vds_stop) - float(params.vds_start)) > 1e-12:
            raise ValueError("Derived 2D maps use a fixed Vds; set Vds stop equal to Vds start.")
        fast = _sequence(*_derived_axis_values(params, params.axis_fast))
        slow = _sequence(*_derived_axis_values(params, params.axis_slow))
        points = len(fast) * len(slow)
        if points > 250000:
            raise ValueError(f"This setup would run {points:,} points; the limit is 250,000.")
        for slow_value in slow:
            for fast_value in fast:
                doping, efield = _derived_pair(params, params.axis_fast, fast_value, params.axis_slow, slow_value)
                vtg, vbg = derived_to_gates(doping, efield, params.ratio, params.ratio_target)
                if abs(vtg) > V_LIMIT or abs(vbg) > V_LIMIT:
                    raise ValueError(f"Derived point ({doping:g}, {efield:g}) requires Vtg={vtg:.3f} V and Vbg={vbg:.3f} V, above the {V_LIMIT:.1f} V limit.")
        bias_values = (fast if params.axis_fast == "Vds" else slow if params.axis_slow == "Vds" else [params.vds_start])
        if any(abs(float(value)) > V_LIMIT for value in bias_values):
            raise ValueError(f"Vds trajectory is above the {V_LIMIT:.1f} V limit.")
        return
    axes = [params.axis_fast] + ([params.axis_slow] if params.axis_slow != "None" else [])
    for field in ("vtg_start", "vtg_stop", "vbg_start", "vbg_stop", "vds_start", "vds_stop"):
        value = float(getattr(params, field))
        if abs(value) > V_LIMIT:
            raise ValueError(f"{field} is {value:.3f} V, above the {V_LIMIT:.1f} V limit.")
    for axis in axes:
        start, stop, step = _raw_axis_values(params, axis)
        if abs(start) > V_LIMIT or abs(stop) > V_LIMIT:
            raise ValueError(f"{axis} range exceeds the {V_LIMIT:.1f} V limit.")
        if abs(stop - start) > 1e-12 and abs(step) <= 1e-12:
            raise ValueError(f"{axis} swept step must be greater than zero.")
    points = len(_sequence(*_raw_axis_values(params, params.axis_fast)))
    if params.axis_slow != "None":
        points *= len(_sequence(*_raw_axis_values(params, params.axis_slow)))
    if points > 250000:
        raise ValueError(f"This setup would run {points:,} points; the limit is 250,000.")
    fast_values = _sequence(*_raw_axis_values(params, params.axis_fast))
    slow_values = _sequence(*_raw_axis_values(params, params.axis_slow)) if params.axis_slow != "None" else [0.0]
    for slow_value in slow_values:
        for fast_value in fast_values:
            vtg = fast_value if params.axis_fast == "Vtg" else (slow_value if params.axis_slow == "Vtg" else params.vtg_start)
            vbg = fast_value if params.axis_fast == "Vbg" else (slow_value if params.axis_slow == "Vbg" else params.vbg_start)
            vds = fast_value if params.axis_fast == "Vds" else (slow_value if params.axis_slow == "Vds" else params.vds_start)
            if abs(vtg) > V_LIMIT or abs(vbg) > V_LIMIT or abs(vds) > V_LIMIT:
                raise ValueError(f"Raw point requires Vtg={vtg:.3f} V, Vbg={vbg:.3f} V, Vds={vds:.3f} V, above the {V_LIMIT:.1f} V limit.")


def _raw_axis_values(params: CoParams, axis: str) -> tuple[float, float, float]:
    if axis == "Vtg":
        return params.vtg_start, params.vtg_stop, params.vtg_step
    if axis == "Vbg":
        return params.vbg_start, params.vbg_stop, params.vbg_step
    if axis == "Vds":
        return params.vds_start, params.vds_stop, params.vds_step
    raise ValueError(f"Unknown raw sweep axis: {axis}")


def _derived_axis_values(params: CoParams, axis: str) -> tuple[float, float, float]:
    if axis == "Doping":
        return params.doping_start, params.doping_stop, params.doping_step
    if axis == "E-field":
        return params.efield_start, params.efield_stop, params.efield_step
    if axis == "Vds":
        return params.vds_start, params.vds_stop, params.vds_step
    raise ValueError(f"Unknown derived sweep axis: {axis}")


def _derived_pair(params: CoParams, fast_axis: str, fast_value: float, slow_axis: str, slow_value: float) -> tuple[float, float]:
    values = {fast_axis: float(fast_value), slow_axis: float(slow_value)}
    return values.get("Doping", params.doping_start), values.get("E-field", params.efield_start)


def _merged_region_rows(params: CoParams) -> list[tuple[float, list[float]]]:
    """Union sampled rectangles by slow coordinate, preserving one raster scan."""
    if params.coordinate_mode != "Raw" or {params.axis_fast, params.axis_slow} != {"Vtg", "Vbg"}:
        raise ValueError("Additional regions require a raw Vtg/Vbg 2D map.")
    fields = [f"{axis}_{part}" for axis in ("vtg", "vbg") for part in ("start", "stop", "step")]
    regions = [{key: getattr(params, key) for key in fields}, *params.regions]
    rows = {}
    count = 0
    for region in regions:
        sequences = {}
        for axis in ("vtg", "vbg"):
            start, stop, step = (float(region[f"{axis}_{part}"]) for part in ("start", "stop", "step"))
            if not all(math.isfinite(v) for v in (start, stop, step)) or step <= 0:
                raise ValueError("Region bounds must be finite and steps greater than zero.")
            if max(abs(start), abs(stop)) > V_LIMIT:
                raise ValueError(f"Region {axis} exceeds the {V_LIMIT:g} V limit.")
            intervals = abs(stop - start) / step
            if intervals > 250000:
                raise ValueError("Merged map exceeds the 250,000 point limit.")
            direction = 1 if stop >= start else -1
            values = [round(start + direction * i * step, 12) for i in range(math.floor(intervals) + 1)]
            if abs(values[-1] - stop) > 1e-10:
                values.append(round(stop, 12))
            else:
                values[-1] = round(stop, 12)
            sequences[axis] = values
        fast = sequences[params.axis_fast.lower()]
        slow = sequences[params.axis_slow.lower()]
        if len(fast) * len(slow) > 250000:
            raise ValueError("Merged map exceeds the 250,000 point limit.")
        for value in slow:
            row = rows.setdefault(value, set())
            before = len(row)
            row.update(fast)
            count += len(row) - before
            if count > 250000:
                raise ValueError("Merged map exceeds the 250,000 point limit.")
    fast_start, fast_stop, _ = _raw_axis_values(params, params.axis_fast)
    slow_start, slow_stop, _ = _raw_axis_values(params, params.axis_slow)
    return [(value, sorted(rows[value], reverse=fast_stop < fast_start))
            for value in sorted(rows, reverse=slow_stop < slow_start)]


def _region_moves(params, points):
    """Plan axis-aligned moves wholly inside the rectangle union before running.

    Startup and final zeroing retain their existing independent ramp behavior.
    """
    regions = [dict(vtg_start=params.vtg_start, vtg_stop=params.vtg_stop,
                    vbg_start=params.vbg_start, vbg_stop=params.vbg_stop), *params.regions]
    fast, slow = params.axis_fast.lower(), params.axis_slow.lower()
    rectangles = [(min(r[f"{fast}_start"], r[f"{fast}_stop"]),
                   max(r[f"{fast}_start"], r[f"{fast}_stop"]),
                   min(r[f"{slow}_start"], r[f"{slow}_stop"]),
                   max(r[f"{slow}_start"], r[f"{slow}_stop"])) for r in regions]

    def covered(a, b):
        horizontal = a[1] == b[1]
        lo, hi = sorted((a[0], b[0]) if horizontal else (a[1], b[1]))
        intervals = []
        for x0, x1, y0, y1 in rectangles:
            if horizontal and y0 - 1e-10 <= a[1] <= y1 + 1e-10:
                intervals.append((x0, x1))
            elif not horizontal and x0 - 1e-10 <= a[0] <= x1 + 1e-10:
                intervals.append((y0, y1))
        for start, stop in sorted(intervals):
            if start > lo + 1e-10:
                break
            if stop >= lo:
                lo = stop
                if lo >= hi - 1e-10:
                    return True
        return False

    previous = None
    for point in points:
        target = (point["fast_value"], point["slow_value"])
        point["moves"] = []
        if previous is None:
            point["moves"] = [(params.axis_slow, target[1]), (params.axis_fast, target[0])]
        else:
            # Prefer the existing slow-first order, then retract fast first.
            candidates = [[(previous[0], target[1]), target], [(target[0], previous[1]), target]]
            # A common column can bridge rows whose ends are both outside the overlap.
            candidates.extend([(x, previous[1]), (x, target[1]), target]
                               for rect in rectangles for x in rect[:2])
            for path in candidates:
                vertices = [previous, *path]
                if all(covered(a, b) for a, b in zip(vertices, vertices[1:])):
                    for a, b in zip(vertices, vertices[1:]):
                        if a[0] != b[0]:
                            point["moves"].append((params.axis_fast, b[0]))
                        if a[1] != b[1]:
                            point["moves"].append((params.axis_slow, b[1]))
                    break
            else:
                raise ValueError("Cannot connect these region rows without leaving the selected regions. Adjust the regions to provide a shared transition corridor.")
        previous = target


def build_cosweep_points(params: CoParams) -> list[dict]:
    """Return the serpentine trajectory, including requested and physical values."""
    validate_cosweep_params(params)
    derived = str(getattr(params, "coordinate_mode", "Raw") or "Raw") == "Derived"
    fast_axis, slow_axis = params.axis_fast, params.axis_slow
    if params.regions:
        fast_seq, slow_seq = [], []
    elif derived:
        fast_seq = _sequence(*_derived_axis_values(params, fast_axis))
        slow_seq = _sequence(*_derived_axis_values(params, slow_axis))
    else:
        fast_seq = _sequence(*_raw_axis_values(params, fast_axis))
        slow_seq = _sequence(*_raw_axis_values(params, slow_axis)) if slow_axis != "None" else [0.0]
    rows = _merged_region_rows(params) if params.regions else [(value, fast_seq) for value in slow_seq]
    points = []
    for pass_idx, (slow_value, row_values) in enumerate(rows):
        reverse = slow_axis != "None" and pass_idx % 2
        row = list(reversed(row_values)) if reverse else row_values
        for fast_value in row:
            if derived:
                doping, efield = _derived_pair(params, fast_axis, fast_value, slow_axis, slow_value)
                vtg, vbg = derived_to_gates(doping, efield, params.ratio, params.ratio_target)
                vds = fast_value if fast_axis == "Vds" else (slow_value if slow_axis == "Vds" else params.vds_start)
            else:
                vtg = fast_value if fast_axis == "Vtg" else (slow_value if slow_axis == "Vtg" else params.vtg_start)
                vbg = fast_value if fast_axis == "Vbg" else (slow_value if slow_axis == "Vbg" else params.vbg_start)
                vds = fast_value if fast_axis == "Vds" else (slow_value if slow_axis == "Vds" else params.vds_start)
                doping, efield = gates_to_derived(vtg, vbg, params.ratio, params.ratio_target)
            points.append({"vtg": float(vtg), "vbg": float(vbg), "vds": float(vds), "doping": float(doping), "efield": float(efield), "fast_value": float(fast_value), "slow_value": float(slow_value), "pass_index": pass_idx, "fast_direction": "reverse" if reverse else "forward"})
    if params.regions:
        _region_moves(params, points)
    return points


class CoSweepWorker(RunWorker):
    timing_updated = QtCore.Signal(object)

    def __init__(self, params: CoParams, save: SaveRoot, conns: Connections, **kw):
        super().__init__()
        self.p = params
        self.save = save
        self.conns = conns
        self.g1 = kw.get("g1")
        self.g2 = kw.get("g2")
        self.g3 = kw.get("g3")
        self.daq = kw.get("daq")
        self.plot_choice = kw.get("plot_choice")
        self.amp_rate = kw.get("amp_rate", 1e7)
        self.lkn_rate = kw.get("lkn_rate", 100.0)
        self.signal_chain = dict(kw.get("signal_chain") or {})
        self._active_derived = False
        self._timing_start = None
        self._timing_paused = 0.0
        self._timing_completed = 0

    def _emit_timing(self, phase):
        if self._timing_start is not None:
            self.timing_updated.emit({"completed": self._timing_completed,
                                      "elapsed": max(0.0, time.monotonic() - self._timing_start - self._timing_paused),
                                      "phase": phase})

    def check_abort_pause(self):
        if not self._pause or self._stop:
            return super().check_abort_pause()
        started = time.monotonic()
        self._emit_timing("paused")
        try:
            super().check_abort_pause()
        finally:
            self._timing_paused += time.monotonic() - started
            self._emit_timing("sampling")

    @QtCore.Slot()
    def run(self):
        self._timing_start = time.monotonic()
        self._timing_paused = 0.0
        self._timing_completed = 0
        csv_path = self.p.output_csv_path
        run_status = "error"
        run_detail = "Run ended before completion."
        try:
            if self.daq is None:
                raise RuntimeError("Required session missing: DAQ")
            try:
                validate_cosweep_params(self.p)
            except ValueError as ex:
                raise RuntimeError(str(ex)) from ex
            self._active_derived = str(getattr(self.p, "coordinate_mode", "Raw") or "Raw") == "Derived"
            fast_axis = self.p.axis_fast
            slow_axis = self.p.axis_slow
            active_axes = [fast_axis, slow_axis]
            if self._active_derived:
                if self.g1 is None or self.g2 is None:
                    raise RuntimeError("G1 / Vtg and G2 / Vbg are both required for a derived map.")
                for gate, label in ((self.g1, "G1 / Vtg"), (self.g2, "G2 / Vbg")):
                    operating_mode = getattr(gate, "operating_mode", None)
                    if operating_mode is not None and operating_mode != KEITHLEY_MODE_VOLTAGE_2W:
                        raise RuntimeError(f"{label} must be in 2-wire voltage source mode.")
            elif ("Vtg" in active_axes or abs(self.p.vtg_start) > 1e-12) and self.g1 is None:
                raise RuntimeError("G1 / Vtg is required for the selected Vtg sweep or fixed bias.")
            elif ("Vbg" in active_axes or abs(self.p.vbg_start) > 1e-12) and self.g2 is None:
                raise RuntimeError("G2 / Vbg is required for the selected Vbg sweep or fixed bias.")
            if self.p.vds_source == "Keithley 2400" and self.g3 is None:
                raise RuntimeError("G3 / Vds is required for a Keithley-driven sweep.")

            trajectory = build_cosweep_points(self.p)
            total = len(trajectory)
            measurement_name = "map_2d" if slow_axis != "None" else "sweep_1d"

            if not csv_path:
                ts = new_run_id()
                summary_parts = map_filename_parts(self.p, self.signal_chain)
                stem = compose_output_stem(
                    self.save.device_id,
                    measurement_name,
                    self.p.base_name,
                    summary_parts,
                    ts,
                )
                csv_path = unique_planned_output(planned_output_at(self.save.path(), stem, ts)).csv_path
            os.makedirs(os.path.dirname(csv_path), exist_ok=True)
            self.log.emit(f"Save -> {csv_path}")
            if self.p.output_metadata_path:
                write_run_metadata(
                    self.p.output_metadata_path,
                    {
                        "measurement": measurement_name,
                        "csv_path": csv_path,
                        "save_root": self.save,
                        "connections": self.conns,
                        "params": self.p,
                        "signal_chain": self.signal_chain,
                    },
                )

            with open(csv_path, "x", newline="", buffering=1, encoding="utf-8") as f:
                w = csv.writer(f)
                w.writerow(["Vtg", "Vbg", "Vds", "Vds_measured", "raw_X", "raw_Y", "raw_DC", "Ids_X", "Ids_Y", "Ids_DC", KEITHLEY_CHANNEL, "Doping", "E-field", "PassIndex", "FastDirection", *self.extra_columns()])
                w.writerow(["V", "V", "V", "V", "V", "V", "V", "A", "A", "A", "A", "V", "V", "#", "", *self.extra_units()])

                if not self._active_derived and "Vtg" not in active_axes:
                    if self.g1 is not None:
                        self.log.emit(
                            f"Ramping G1/Vtg to {self.p.vtg_start:.3f} V "
                            f"({GATE_BIAS_RAMP_STEP_V:g} V/step, {GATE_BIAS_RAMP_STEP_T:g} s/step)"
                        )
                        safe_ramp(
                            self.g1.set_voltage,
                            getattr(self.g1, "voltage", None) or 0.0,
                            self.p.vtg_start,
                            GATE_BIAS_RAMP_STEP_V,
                            GATE_BIAS_RAMP_STEP_T,
                            self.check_abort_pause,
                        )
                if not self._active_derived and "Vbg" not in active_axes:
                    if self.g2 is not None:
                        self.log.emit(
                            f"Ramping G2/Vbg to {self.p.vbg_start:.3f} V "
                            f"({GATE_BIAS_RAMP_STEP_V:g} V/step, {GATE_BIAS_RAMP_STEP_T:g} s/step)"
                        )
                        safe_ramp(
                            self.g2.set_voltage,
                            getattr(self.g2, "voltage", None) or 0.0,
                            self.p.vbg_start,
                            GATE_BIAS_RAMP_STEP_V,
                            GATE_BIAS_RAMP_STEP_T,
                            self.check_abort_pause,
                        )
                if "Vds" not in active_axes:
                    if self.p.vds_source.startswith("NI DAQ"):
                        self.daq.ramp_voltage(self.p.ao_channel, self.p.vds_start, self.p.vds_ramp)
                    else:
                        if self.g3 is None:
                            raise RuntimeError("Required session missing: G3 / Vds")
                        self.g3.ramp_voltage(self.p.vds_start, self.p.vds_ramp)

                cnt = 0
                self.clear_plot.emit()
                current_pass = None

                for point in trajectory:
                    self.check_abort_pause()
                    pass_idx = point["pass_index"]
                    fast_direction = point["fast_direction"]
                    self.status.emit(f"Point {cnt + 1}/{total}  [pass {pass_idx + 1}]")
                    if self._active_derived:
                        self.set_derived_gates(point["vtg"], point["vbg"])
                        if fast_axis == "Vds" or (slow_axis == "Vds" and pass_idx != current_pass):
                            self.set_volt("Vds", point["vds"])
                        current_pass = pass_idx
                    elif self.p.regions:
                        for axis, value in point["moves"]:
                            self.check_abort_pause()
                            self.set_volt(axis, value)
                    else:
                        if slow_axis != "None" and pass_idx != current_pass:
                            self.set_volt(slow_axis, point["slow_value"])
                            current_pass = pass_idx
                        self.set_volt(fast_axis, point["fast_value"])
                    time.sleep(self.p.delay)

                    sample = self.acquire_currents(self.p.n_sample)
                    raw_x, raw_y, raw_dc = (sample[key] for key in ("raw_X", "raw_Y", "raw_DC"))
                    ids_x, ids_y, ids_dc = (sample[key] for key in ("Ids_X", "Ids_Y", "Ids_DC"))
                    ids_keithley = self._read_keithley_current()
                    vds_measured = (
                        self.daq.get_ao_vs_gnd_value(self.p.ao_channel)
                        if self.p.vds_source.startswith("NI DAQ")
                        else None
                    )

                    curr_vtg = point["vtg"]
                    curr_vbg = point["vbg"]
                    curr_vds = point["vds"]
                    doping, efield = point["doping"], point["efield"]

                    w.writerow([curr_vtg, curr_vbg, curr_vds, vds_measured, raw_x, raw_y, raw_dc, ids_x, ids_y, ids_dc, ids_keithley, doping, efield, pass_idx, fast_direction, *self.extra_values()])
                    try:
                        f.flush()
                        os.fsync(f.fileno())
                    except Exception:
                        pass
                    y_val = self._plot_value(ids_dc, ids_x, ids_y, ids_keithley)
                    point_record = {
                        "index": float(cnt),
                        "vtg": float(curr_vtg),
                        "vbg": float(curr_vbg),
                        "vds": float(curr_vds),
                        "doping": float(doping),
                        "efield": float(efield),
                    }
                    x_axis = resolve_map_x_axis(self.p.plot_x_axis, self.p.axis_fast)
                    x_plot = record_x_value(point_record, x_axis)
                    self.point.emit(x_plot, y_val)
                    self.point_data.emit({
                        **sample,
                        "x": x_plot,
                        **point_record,
                        "vds_measured": vds_measured,
                        "plot_ratio": float(self.p.ratio),
                        "plot_ratio_target": self.p.ratio_target,
                        "Ids_DC": ids_dc,
                        "Ids_X": ids_x,
                        "Ids_Y": ids_y,
                        KEITHLEY_CHANNEL: ids_keithley,
                        "pass_index": pass_idx,
                        "fast_direction": fast_direction,
                    })
                    cnt += 1
                    self._timing_completed = cnt
                    self._emit_timing("sampling")
                    self.progress.emit(cnt / total)
            run_status = "finished"
            run_detail = csv_path
            self.finished.emit(csv_path)
        except RunStopped as ex:
            run_status = "stopped"
            run_detail = f"{ex}. Partial data saved to: {csv_path}" if csv_path else str(ex)
            self.stopped.emit(run_detail)
        except Exception as ex:
            run_status = "error"
            run_detail = str(ex)
            self.error.emit(run_detail)
        finally:
            self._emit_timing("cleanup")
            failures = []
            try:
                if self.g1 is not None:
                    safe_ramp(self.g1.set_voltage, getattr(self.g1, "voltage", None) or 0.0, 0.0, SAFE_RAMP_STEP_V, SAFE_RAMP_STEP_T)
            except Exception as ex:
                failures.append(f"G1/Vtg zero failed: {ex}")
            try:
                if self.g2 is not None:
                    safe_ramp(self.g2.set_voltage, getattr(self.g2, "voltage", None) or 0.0, 0.0, SAFE_RAMP_STEP_V, SAFE_RAMP_STEP_T)
            except Exception as ex:
                failures.append(f"G2/Vbg zero failed: {ex}")
            try:
                if self.p.vds_source.startswith("NI DAQ"):
                    if self.daq is not None:
                        self.daq.ramp_voltage(self.p.ao_channel, 0.0, SAFE_RAMP_STEP_V, SAFE_RAMP_STEP_T)
                elif self.g3 is not None:
                    safe_ramp(self.g3.set_voltage, getattr(self.g3, "voltage", None) or 0.0, 0.0, SAFE_RAMP_STEP_V, SAFE_RAMP_STEP_T)
            except Exception as ex:
                failures.append(f"Vds zero failed: {ex}")
            self.emit_safe_state_report(failures)
            try:
                update_run_metadata_status(self.p.output_metadata_path, run_status, run_detail, failures)
            finally:
                self._emit_timing("cleanup_failed" if failures else "done")

    def set_volt(self, name, val):
        if name == "Vtg":
            if self.g1 is None:
                raise RuntimeError("Required session missing: G1 / Vtg")
            self.g1.ramp_voltage(val, self.p.vg_ramp)
        elif name == "Vbg":
            if self.g2 is None:
                raise RuntimeError("Required session missing: G2 / Vbg")
            self.g2.ramp_voltage(val, self.p.vg_ramp)
        elif name == "Vds":
            if self.p.vds_source.startswith("NI DAQ"):
                self.daq.ramp_voltage(self.p.ao_channel, val, self.p.vds_ramp)
            else:
                if self.g3 is None:
                    raise RuntimeError("Required session missing: G3 / Vds")
                self.g3.ramp_voltage(val, self.p.vds_ramp)

    def set_derived_gates(self, vtg: float, vbg: float):
        """Move both gate channels for one derived-coordinate point."""
        if self.g1 is None or self.g2 is None:
            raise RuntimeError("G1 / Vtg and G2 / Vbg are both required for a derived map.")
        self.g1.ramp_voltage(float(vtg), self.p.vg_ramp)
        self.g2.ramp_voltage(float(vbg), self.p.vg_ramp)

    def _read_keithley_current(self):
        if self.p.vds_source != "Keithley 2400" or self.g3 is None:
            return None
        try:
            values = self.g3.acquire()
            return values.get("current", self.g3.current)
        except Exception:
            return None

    def _plot_value(self, ids_dc, ids_x, ids_y, ids_keithley):
        if self.plot_choice in getattr(self, "_current_sample", {}):
            return self._current_sample[self.plot_choice]
        if self.plot_choice == "Ids_X":
            return ids_x
        if self.plot_choice == "Ids_Y":
            return ids_y
        if self.plot_choice == KEITHLEY_CHANNEL:
            return ids_keithley if ids_keithley is not None else float("nan")
        return ids_dc
