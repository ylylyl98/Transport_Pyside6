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
from app.models import CoParams, Connections, SaveRoot
from app.result_channels import KEITHLEY_CHANNEL
from app.run_output import update_run_metadata_status, write_run_metadata
from app.utils import _frange_inc, _sanitize_base, safe_ramp
from app.workers.base import RunStopped, RunWorker


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
            for field in ("vtg_start", "vtg_stop", "vbg_start", "vbg_stop", "vds_start", "vds_stop"):
                value = float(getattr(self.p, field))
                if abs(value) > V_LIMIT:
                    raise RuntimeError(f"{field} is {value:.3f} V, above the {V_LIMIT:.1f} V limit.")

            fast_axis = self.p.axis_fast
            slow_axis = self.p.axis_slow
            active_axes = [fast_axis, slow_axis]
            if ("Vtg" in active_axes or abs(self.p.vtg_start) > 1e-12) and self.g1 is None:
                raise RuntimeError("G1 / Vtg is required for the selected Vtg sweep or fixed bias.")
            if ("Vbg" in active_axes or abs(self.p.vbg_start) > 1e-12) and self.g2 is None:
                raise RuntimeError("G2 / Vbg is required for the selected Vbg sweep or fixed bias.")
            if self.p.vds_source == "Keithley 2400" and self.g3 is None:
                raise RuntimeError("G3 / Vds is required for a Keithley-driven sweep.")

            def get_seq(name):
                if name == "Vtg":
                    start, stop, step = self.p.vtg_start, self.p.vtg_stop, self.p.vtg_step
                elif name == "Vbg":
                    start, stop, step = self.p.vbg_start, self.p.vbg_stop, self.p.vbg_step
                elif name == "Vds":
                    start, stop, step = self.p.vds_start, self.p.vds_stop, self.p.vds_step
                else:
                    return [0.0]
                s = step if stop >= start else -abs(step)
                if abs(s) < 1e-9:
                    return [start]
                return _frange_inc(start, stop, s)

            fast_seq = get_seq(fast_axis)
            slow_seq = get_seq(slow_axis) if slow_axis != "None" else [0.0]
            measurement_name = "map_2d" if slow_axis != "None" else "sweep_1d"

            if not csv_path:
                ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
                device_id = _sanitize_base(self.save.device_id)
                stem = f"{device_id}_{_sanitize_base(self.p.base_name)}_{measurement_name}_{fast_axis}_{slow_axis}_{ts}"
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
                    },
                )

            with open(csv_path, "x", newline="", buffering=1, encoding="utf-8") as f:
                w = csv.writer(f)
                w.writerow(["Vtg", "Vbg", "Vds", "raw_X", "raw_Y", "raw_DC", "Ids_X", "Ids_Y", "Ids_DC", KEITHLEY_CHANNEL, "Doping", "E-field", "PassIndex", "FastDirection"])
                w.writerow(["V", "V", "V", "A", "A", "A", "A", "A", "A", "A", "V", "V", "#", ""])

                if "Vtg" not in active_axes:
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
                if "Vbg" not in active_axes:
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

                total = len(fast_seq) * len(slow_seq)
                cnt = 0
                self.clear_plot.emit()

                for pass_idx, s_val in enumerate(slow_seq):
                    self.set_volt(slow_axis, s_val)
                    if slow_axis != "None" and pass_idx % 2 == 1:
                        row_fast_seq = list(reversed(fast_seq))
                        fast_direction = "reverse"
                    else:
                        row_fast_seq = fast_seq
                        fast_direction = "forward"
                    for f_val in row_fast_seq:
                        self.check_abort_pause()
                        self.status.emit(f"Point {cnt + 1}/{total}  [pass {pass_idx + 1}]")
                        self.set_volt(fast_axis, f_val)
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

                        curr_vtg = f_val if fast_axis == "Vtg" else (s_val if slow_axis == "Vtg" else self.p.vtg_start)
                        curr_vbg = f_val if fast_axis == "Vbg" else (s_val if slow_axis == "Vbg" else self.p.vbg_start)
                        curr_vds = f_val if fast_axis == "Vds" else (s_val if slow_axis == "Vds" else self.p.vds_start)
                        doping = curr_vtg + (self.p.ratio * curr_vbg)
                        efield = curr_vtg - (self.p.ratio * curr_vbg)

                        w.writerow([curr_vtg, curr_vbg, curr_vds, raw_x, raw_y, raw_dc, ids_x, ids_y, ids_dc, ids_keithley, doping, efield, pass_idx, fast_direction])
                        try:
                            f.flush()
                            os.fsync(f.fileno())
                        except Exception:
                            pass
                        y_val = self._plot_value(ids_dc, ids_x, ids_y, ids_keithley)
                        x_plot = doping if self.p.mode == "Linked" else f_val
                        self.point.emit(x_plot, y_val)
                        self.point_data.emit({
                            "x": x_plot,
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
                        safe_ramp(
                            lambda v: self.daq.set_voltage(self.p.ao_channel, v),
                            self.daq.get_ao_value(self.p.ao_channel),
                            0.0,
                            SAFE_RAMP_STEP_V,
                            SAFE_RAMP_STEP_T,
                        )
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
