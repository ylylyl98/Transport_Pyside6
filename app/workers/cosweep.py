from __future__ import annotations

import csv
import datetime
import os
import time

from PyQt6 import QtCore

from app.constants import (
    GATE_BIAS_RAMP_STEP_T,
    GATE_BIAS_RAMP_STEP_V,
    SAFE_RAMP_STEP_T,
    SAFE_RAMP_STEP_V,
    V_LIMIT,
)
from app.gate_transform import derived_to_gates, gates_to_derived, normalize_ratio_target
from app.keithley_modes import KEITHLEY_MODE_VOLTAGE_2W
from app.models import CoParams, Connections, SaveRoot
from app.plot_x_axis import record_x_value, resolve_map_x_axis
from app.result_channels import KEITHLEY_CHANNEL
from app.run_output import new_run_id, compose_output_stem, update_run_metadata_status, write_run_metadata
from app.signal_chain import signal_chain_filename_parts
from app.utils import _frange_inc, safe_ramp
from app.workers.base import RunStopped, RunWorker


def _sequence(start: float, stop: float, step: float) -> list[float]:
    """Inclusive sequence with a positive user-facing step."""
    start, stop, step = float(start), float(stop), abs(float(step))
    if step <= 0:
        if abs(stop - start) <= 1e-12:
            return [start]
        raise ValueError("Swept steps must be greater than zero.")
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
    if mode == "Derived":
        if params.axis_slow == "None" or params.axis_fast not in {"Doping", "E-field"} or params.axis_slow not in {"Doping", "E-field"} or params.axis_fast == params.axis_slow:
            raise ValueError("Derived 2D maps require Doping and E-field as distinct fast and slow axes.")
        if abs(float(params.ratio)) < 1e-12:
            raise ValueError("Derived trajectory requires a non-zero ratio.")
        if abs(float(params.vds_stop) - float(params.vds_start)) > 1e-12:
            raise ValueError("Derived 2D maps use a fixed Vds; set Vds stop equal to Vds start.")
        fast = _sequence(*_derived_axis_values(params, params.axis_fast))
        slow = _sequence(*_derived_axis_values(params, params.axis_slow))
        points = len(fast) * len(slow)
        if points > 250000:
            raise ValueError(f"This setup would run {points:,} points; the limit is 250,000.")
        for slow_value in slow:
            for fast_value in fast:
                doping, efield = _derived_pair(params.axis_fast, fast_value, params.axis_slow, slow_value)
                vtg, vbg = derived_to_gates(doping, efield, params.ratio, params.ratio_target)
                if abs(vtg) > V_LIMIT or abs(vbg) > V_LIMIT:
                    raise ValueError(f"Derived point ({doping:g}, {efield:g}) requires Vtg={vtg:.3f} V and Vbg={vbg:.3f} V, above the {V_LIMIT:.1f} V limit.")
        if abs(float(params.vds_start)) > V_LIMIT:
            raise ValueError(f"vds_start is {params.vds_start:.3f} V, above the {V_LIMIT:.1f} V limit.")
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
    raise ValueError(f"Unknown derived sweep axis: {axis}")


def _derived_pair(fast_axis: str, fast_value: float, slow_axis: str, slow_value: float) -> tuple[float, float]:
    values = {fast_axis: float(fast_value), slow_axis: float(slow_value)}
    return values["Doping"], values["E-field"]


def build_cosweep_points(params: CoParams) -> list[dict]:
    """Return the serpentine trajectory, including requested and physical values."""
    validate_cosweep_params(params)
    derived = str(getattr(params, "coordinate_mode", "Raw") or "Raw") == "Derived"
    fast_axis, slow_axis = params.axis_fast, params.axis_slow
    if derived:
        fast_seq = _sequence(*_derived_axis_values(params, fast_axis))
        slow_seq = _sequence(*_derived_axis_values(params, slow_axis))
    else:
        fast_seq = _sequence(*_raw_axis_values(params, fast_axis))
        slow_seq = _sequence(*_raw_axis_values(params, slow_axis)) if slow_axis != "None" else [0.0]
    points = []
    for pass_idx, slow_value in enumerate(slow_seq):
        row = list(reversed(fast_seq)) if slow_axis != "None" and pass_idx % 2 else fast_seq
        for fast_value in row:
            if derived:
                doping, efield = _derived_pair(fast_axis, fast_value, slow_axis, slow_value)
                vtg, vbg = derived_to_gates(doping, efield, params.ratio, params.ratio_target)
                vds = params.vds_start
            else:
                vtg = fast_value if fast_axis == "Vtg" else (slow_value if slow_axis == "Vtg" else params.vtg_start)
                vbg = fast_value if fast_axis == "Vbg" else (slow_value if slow_axis == "Vbg" else params.vbg_start)
                vds = fast_value if fast_axis == "Vds" else (slow_value if slow_axis == "Vds" else params.vds_start)
                doping, efield = gates_to_derived(vtg, vbg, params.ratio, params.ratio_target)
            points.append({"vtg": float(vtg), "vbg": float(vbg), "vds": float(vds), "doping": float(doping), "efield": float(efield), "fast_value": float(fast_value), "slow_value": float(slow_value), "pass_index": pass_idx, "fast_direction": "reverse" if row is not fast_seq else "forward"})
    return points


class CoSweepWorker(RunWorker):
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

    @QtCore.pyqtSlot()
    def run(self):
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
                signal_tags = "_".join(signal_chain_filename_parts(self.signal_chain))
                stem = compose_output_stem(
                    self.save.device_id,
                    measurement_name,
                    self.p.base_name,
                    (f"fast_{fast_axis}", f"slow_{slow_axis}", signal_tags),
                    ts,
                )
                csv_path = os.path.join(self.save.path(), stem + ".csv")
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
                w.writerow(["Vtg", "Vbg", "Vds", "Vds_measured", "raw_X", "raw_Y", "raw_DC", "Ids_X", "Ids_Y", "Ids_DC", KEITHLEY_CHANNEL, "Doping", "E-field", "PassIndex", "FastDirection"])
                w.writerow(["V", "V", "V", "V", "A", "A", "A", "A", "A", "A", "A", "V", "V", "#", ""])

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
                    else:
                        if slow_axis != "None" and pass_idx != current_pass:
                            self.set_volt(slow_axis, point["slow_value"])
                            current_pass = pass_idx
                        self.set_volt(fast_axis, point["fast_value"])
                    time.sleep(self.p.delay)

                    raw_x = raw_y = raw_dc = 0.0
                    for _ in range(self.p.n_sample):
                        self.check_abort_pause()
                        self.daq.acquire()
                        raw_x += self.daq.get_ai_value(0)
                        raw_y += self.daq.get_ai_value(1)
                        raw_dc += self.daq.get_ai_value(2)
                    raw_x /= self.p.n_sample
                    raw_y /= self.p.n_sample
                    raw_dc /= self.p.n_sample

                    ids_x = raw_x / (self.amp_rate * self.lkn_rate)
                    ids_y = raw_y / (self.amp_rate * self.lkn_rate)
                    ids_dc = raw_dc / self.amp_rate
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

                    w.writerow([curr_vtg, curr_vbg, curr_vds, vds_measured, raw_x, raw_y, raw_dc, ids_x, ids_y, ids_dc, ids_keithley, doping, efield, pass_idx, fast_direction])
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
            update_run_metadata_status(self.p.output_metadata_path, run_status, run_detail, failures)

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
        if self.plot_choice == "Ids_X":
            return ids_x
        if self.plot_choice == "Ids_Y":
            return ids_y
        if self.plot_choice == KEITHLEY_CHANNEL:
            return ids_keithley if ids_keithley is not None else float("nan")
        return ids_dc
