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
from app.gate_transform import derived_to_gates, gates_to_derived
from app.models import Connections, LineSweepParams, SaveRoot
from app.plot_x_axis import record_x_value, resolve_gate_scan_x_axis
from app.result_channels import KEITHLEY_CHANNEL
from app.run_output import update_run_metadata_status, write_run_metadata
from app.utils import _sanitize_base, safe_ramp
from app.workers.base import RunStopped, RunWorker


def _interp(start: float, stop: float, index: int, total: int) -> float:
    if total <= 1:
        return float(start)
    frac = index / float(total - 1)
    return start + frac * (stop - start)


class LineSweepWorker(RunWorker):
    def __init__(self, params: LineSweepParams, save: SaveRoot, conns: Connections, **kw):
        super().__init__()
        self.p = params
        self.save = save
        self.conns = conns
        self.g1 = kw.get("g1")
        self.g2 = kw.get("g2")
        self.g3 = kw.get("g3")
        self.daq = kw.get("daq")
        self.plot_choice = kw.get("plot_choice", self.p.plot_choice)
        self.amp_rate = kw.get("amp_rate", 1e7)
        self.lkn_rate = kw.get("lkn_rate", 100.0)

    @QtCore.pyqtSlot()
    def run(self):
        csv_path = self.p.output_csv_path
        run_status = "error"
        run_detail = "Run ended before completion."
        try:
            if self.daq is None:
                raise RuntimeError("Required session missing: DAQ")
            if int(self.p.n_sample) < 1:
                raise RuntimeError("Averages must be at least 1.")
            if self.p.vg_ramp <= 0 or self.p.vds_ramp <= 0:
                raise RuntimeError("Gate and Vds ramp steps must be greater than zero.")

            trajectory = self._build_trajectory()
            if not trajectory:
                raise RuntimeError("No trajectory points generated.")

            self._validate_sessions()
            self._validate_trajectory_limits(trajectory)

            if not csv_path:
                ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
                device_id = _sanitize_base(self.save.device_id)
                stem = f"{device_id}_{_sanitize_base(self.p.base_name)}_{ts}"
                csv_path = os.path.join(self.save.path(), stem + ".csv")
            os.makedirs(os.path.dirname(csv_path), exist_ok=True)
            self.log.emit(f"Save -> {csv_path}")
            if self.p.output_metadata_path:
                write_run_metadata(
                    self.p.output_metadata_path,
                    {
                        "measurement": "gate_scan",
                        "csv_path": csv_path,
                        "save_root": self.save,
                        "connections": self.conns,
                        "params": self.p,
                    },
                )

            first = trajectory[0]
            self.status.emit("Ramping to sweep start position...")
            self.log.emit(
                f"Ramping G1/Vtg to {first['vtg']:.3f} V "
                f"({GATE_BIAS_RAMP_STEP_V:g} V/step, {GATE_BIAS_RAMP_STEP_T:g} s/step)"
            )
            safe_ramp(
                self.g1.set_voltage,
                getattr(self.g1, "voltage", None) or 0.0,
                first["vtg"],
                GATE_BIAS_RAMP_STEP_V,
                GATE_BIAS_RAMP_STEP_T,
                self.check_abort_pause,
            )
            self.log.emit(
                f"Ramping G2/Vbg to {first['vbg']:.3f} V "
                f"({GATE_BIAS_RAMP_STEP_V:g} V/step, {GATE_BIAS_RAMP_STEP_T:g} s/step)"
            )
            safe_ramp(
                self.g2.set_voltage,
                getattr(self.g2, "voltage", None) or 0.0,
                first["vbg"],
                GATE_BIAS_RAMP_STEP_V,
                GATE_BIAS_RAMP_STEP_T,
                self.check_abort_pause,
            )
            self._safe_ramp_vds(first["vds"], allow_stop=True)
            self.check_abort_pause()

            forward_traj = trajectory
            backward_traj = list(reversed(trajectory)) if self.p.sweep_both_ways else []
            grand_total = len(forward_traj) + len(backward_traj)

            self.clear_plot.emit()
            self.status.emit("Preparing trajectory...")

            with open(csv_path, "x", newline="", buffering=1, encoding="utf-8") as f:
                w = csv.writer(f)
                w.writerow(["Index", "Vtg", "Vbg", "Vds", "raw_X", "raw_Y", "raw_DC", "Ids_X", "Ids_Y", "Ids_DC", KEITHLEY_CHANNEL, "Doping", "E-field", "Direction"])
                w.writerow(["#", "V", "V", "V", "A", "A", "A", "A", "A", "A", "A", "V", "V", ""])
                self._run_trajectory_pass(f, w, forward_traj, "forward", 0, grand_total)
                if backward_traj:
                    self.check_abort_pause()
                    self._run_trajectory_pass(f, w, backward_traj, "backward", len(forward_traj), grand_total)
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
                self._safe_ramp_vds(0.0, allow_stop=False)
            except Exception as ex:
                failures.append(f"Vds zero failed: {ex}")
            self.emit_safe_state_report(failures)
            update_run_metadata_status(self.p.output_metadata_path, run_status, run_detail, failures)

    def _safe_ramp_vds(self, target: float, allow_stop: bool = False) -> None:
        if self.p.vds_source == "Keithley 2400":
            if self.g3 is None:
                raise RuntimeError("Required session missing: G3 / Vds")
            safe_ramp(
                self.g3.set_voltage,
                getattr(self.g3, "voltage", None) or 0.0,
                target,
                SAFE_RAMP_STEP_V,
                SAFE_RAMP_STEP_T,
                self.check_abort_pause if allow_stop else None,
            )
        else:
            safe_ramp(
                lambda v: self.daq.set_voltage(self.p.ao_channel, v),
                self.daq.get_ao_value(self.p.ao_channel),
                target,
                SAFE_RAMP_STEP_V,
                SAFE_RAMP_STEP_T,
                self.check_abort_pause if allow_stop else None,
            )

    def _run_trajectory_pass(self, f, w, trajectory: list, direction: str, idx_offset: int, grand_total: int) -> None:
        for idx, point in enumerate(trajectory, start=1):
            self.check_abort_pause()
            self.status.emit(f"Point {idx_offset + idx}/{grand_total}  [{direction}]")
            self._apply_point(point)
            time.sleep(max(0.0, self.p.delay))

            raw_x = raw_y = raw_dc = 0.0
            for _ in range(int(self.p.n_sample)):
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

            w.writerow([
                idx_offset + idx - 1,
                point["vtg"],
                point["vbg"],
                point["vds"],
                raw_x,
                raw_y,
                raw_dc,
                ids_x,
                ids_y,
                ids_dc,
                ids_keithley,
                point["doping"],
                point["efield"],
                direction,
            ])
            try:
                f.flush()
                os.fsync(f.fileno())
            except Exception:
                pass

            y_val = self._plot_value(ids_dc, ids_x, ids_y, ids_keithley)
            acquisition_index = idx_offset + idx - 1
            x_plot = self._plot_x(point, acquisition_index)
            self.point.emit(x_plot, y_val)
            self.point_data.emit({
                "x": x_plot,
                "index": float(acquisition_index),
                "vtg": point["vtg"],
                "vbg": point["vbg"],
                "vds": point["vds"],
                "doping": point["doping"],
                "efield": point["efield"],
                "plot_ratio": float(self.p.derived_ratio),
                "plot_ratio_target": self.p.derived_ratio_target,
                "Ids_DC": ids_dc,
                "Ids_X": ids_x,
                "Ids_Y": ids_y,
                KEITHLEY_CHANNEL: ids_keithley,
                "direction": direction,
            })
            self.progress.emit((idx_offset + idx) / grand_total)

    def _validate_sessions(self):
        if self.g1 is None:
            raise RuntimeError("Required session missing: G1 / Vtg")
        if self.g2 is None:
            raise RuntimeError("Required session missing: G2 / Vbg")
        if self.p.vds_source == "Keithley 2400" and self.g3 is None:
            raise RuntimeError("Required session missing: G3 / Vds")

    def _validate_trajectory_limits(self, trajectory: list[dict[str, float]]):
        for point in trajectory:
            for name in ("vtg", "vbg", "vds"):
                value = float(point[name])
                if abs(value) > V_LIMIT:
                    raise RuntimeError(f"{name.upper()} exceeds limit at {value:.3f} V (limit {V_LIMIT:.1f} V)")

    def _build_trajectory(self) -> list[dict[str, float]]:
        return self._build_raw_trajectory() if self.p.mode == "Raw" else self._build_derived_trajectory()

    def _build_raw_trajectory(self) -> list[dict[str, float]]:
        active = [self.p.raw_vtg_active, self.p.raw_vbg_active, self.p.raw_vds_active]
        if not any(active):
            raise RuntimeError("Raw trajectory needs at least one active variable.")
        count = max(1, int(self.p.n_points))
        points = []
        for idx in range(count):
            vtg = _interp(self.p.raw_vtg_start, self.p.raw_vtg_stop, idx, count) if self.p.raw_vtg_active else self.p.raw_vtg_start
            vbg = _interp(self.p.raw_vbg_start, self.p.raw_vbg_stop, idx, count) if self.p.raw_vbg_active else self.p.raw_vbg_start
            vds = _interp(self.p.raw_vds_start, self.p.raw_vds_stop, idx, count) if self.p.raw_vds_active else self.p.raw_vds_start
            doping, efield = gates_to_derived(
                vtg,
                vbg,
                self.p.derived_ratio,
                self.p.derived_ratio_target,
            )
            points.append({
                "index": float(idx),
                "vtg": float(vtg),
                "vbg": float(vbg),
                "vds": float(vds),
                "doping": float(doping),
                "efield": float(efield),
            })
        return points

    def _build_derived_trajectory(self) -> list[dict[str, float]]:
        if abs(self.p.derived_ratio) < 1e-12:
            raise RuntimeError("Derived mode requires a non-zero ratio.")
        count = max(1, int(self.p.n_points))
        points = []
        for idx in range(count):
            swept = _interp(self.p.derived_start, self.p.derived_stop, idx, count)
            if self.p.derived_axis == "Doping":
                doping = swept
                efield = self.p.derived_fixed
            else:
                doping = self.p.derived_fixed
                efield = swept
            vtg, vbg = derived_to_gates(
                doping,
                efield,
                self.p.derived_ratio,
                self.p.derived_ratio_target,
            )
            if self.p.derived_vds_mode == "Swept":
                vds = _interp(self.p.derived_vds_start, self.p.derived_vds_stop, idx, count)
            else:
                vds = self.p.derived_vds_fixed
            points.append({
                "index": float(idx),
                "vtg": float(vtg),
                "vbg": float(vbg),
                "vds": float(vds),
                "doping": float(doping),
                "efield": float(efield),
            })
        return points

    def _apply_point(self, point: dict[str, float]):
        self.g1.ramp_voltage(point["vtg"], self.p.vg_ramp)
        self.g2.ramp_voltage(point["vbg"], self.p.vg_ramp)
        if self.p.vds_source == "Keithley 2400":
            self.g3.ramp_voltage(point["vds"], self.p.vds_ramp)
        else:
            self.daq.ramp_voltage(self.p.ao_channel, point["vds"], self.p.vds_ramp)

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

    def _plot_x(self, point: dict[str, float], index: int) -> float:
        axis = resolve_gate_scan_x_axis(
            self.p.plot_x_axis,
            self.p.mode,
            self.p.derived_axis,
            self.p.raw_vtg_active,
            self.p.raw_vbg_active,
            self.p.raw_vds_active,
        )
        return record_x_value({**point, "index": float(index)}, axis)
