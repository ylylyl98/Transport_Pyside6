from __future__ import annotations

import csv
import datetime
import os
import time

from PyQt6 import QtCore

from app.constants import SAFE_RAMP_STEP_T, SAFE_RAMP_STEP_V
from app.models import CoParams, Connections, SaveRoot
from app.result_channels import KEITHLEY_CHANNEL
from app.utils import _frange_inc, _safe, _sanitize_base, safe_ramp
from app.workers.base import RunWorker


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
        try:
            if self.daq is None:
                self.error.emit("DAQ missing.")
                return

            fast_axis = self.p.axis_fast
            slow_axis = self.p.axis_slow

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

            ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
            device_id = _sanitize_base(self.save.device_id)
            stem = f"{device_id}_{_sanitize_base(self.p.base_name)}_Megasweep_{fast_axis}_{slow_axis}_{ts}"
            csv_path = os.path.join(self.save.path(), stem + ".csv")
            self.log.emit(f"Save -> {csv_path}")

            with open(csv_path, "a", newline="", buffering=1, encoding="utf-8") as f:
                w = csv.writer(f)
                w.writerow(["Vtg", "Vbg", "Vbias", "raw_X", "raw_Y", "raw_DC", "Ids_X", "Ids_Y", "Ids_DC", KEITHLEY_CHANNEL, "Doping", "Efield"])
                w.writerow(["V", "V", "V", "A", "A", "A", "A", "A", "A", "A", "V", "V"])

                active_axes = [fast_axis, slow_axis]
                if "Vtg" not in active_axes:
                    _safe(self.g1, "ramp_voltage", self.p.vtg_start, self.p.vg_ramp)
                if "Vbg" not in active_axes:
                    _safe(self.g2, "ramp_voltage", self.p.vbg_start, self.p.vg_ramp)
                if "Vds" not in active_axes:
                    if self.p.vds_source.startswith("NI DAQ"):
                        _safe(self.daq, "ramp_voltage", self.p.ao_channel, self.p.vds_start, self.p.vds_ramp)
                    else:
                        _safe(self.g3, "ramp_voltage", self.p.vds_start, self.p.vds_ramp)

                total = len(fast_seq) * len(slow_seq)
                cnt = 0
                self.clear_plot.emit()

                for s_val in slow_seq:
                    self.set_volt(slow_axis, s_val)
                    for f_val in fast_seq:
                        self.check_abort_pause()
                        self.set_volt(fast_axis, f_val)
                        time.sleep(self.p.delay)

                        raw_x = raw_y = raw_dc = 0.0
                        for _ in range(self.p.n_sample):
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
                        doping = (self.p.ratio * curr_vtg) + curr_vbg
                        efield = (self.p.ratio * curr_vtg) - curr_vbg

                        w.writerow([curr_vtg, curr_vbg, curr_vds, raw_x, raw_y, raw_dc, ids_x, ids_y, ids_dc, ids_keithley, doping, efield])
                        y_val = self._plot_value(ids_dc, ids_x, ids_y, ids_keithley)
                        x_plot = doping if self.p.mode == "Linked" else f_val
                        self.point.emit(x_plot, y_val)
                        self.point_data.emit({
                            "x": x_plot,
                            "Ids_DC": ids_dc,
                            "Ids_X": ids_x,
                            "Ids_Y": ids_y,
                            KEITHLEY_CHANNEL: ids_keithley,
                        })
                        cnt += 1
                        self.progress.emit(cnt / total)

            self.finished.emit(csv_path)
        except Exception as ex:
            self.error.emit(str(ex))
        finally:
            try:
                if self.g1 is not None:
                    safe_ramp(self.g1.set_voltage, getattr(self.g1, "voltage", None) or 0.0, 0.0, SAFE_RAMP_STEP_V, SAFE_RAMP_STEP_T)
                if self.g2 is not None:
                    safe_ramp(self.g2.set_voltage, getattr(self.g2, "voltage", None) or 0.0, 0.0, SAFE_RAMP_STEP_V, SAFE_RAMP_STEP_T)
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
            except Exception:
                pass
            self.log.emit("Outputs returned to 0 V; sessions kept open.")

    def set_volt(self, name, val):
        if name == "Vtg":
            _safe(self.g1, "ramp_voltage", val, self.p.vg_ramp)
        elif name == "Vbg":
            _safe(self.g2, "ramp_voltage", val, self.p.vg_ramp)
        elif name == "Vds":
            if self.p.vds_source.startswith("NI DAQ"):
                _safe(self.daq, "ramp_voltage", self.p.ao_channel, val, self.p.vds_ramp)
            else:
                _safe(self.g3, "ramp_voltage", val, self.p.vds_ramp)

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
