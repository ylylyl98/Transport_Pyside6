from __future__ import annotations

import csv
import datetime
import os
import time

from PyQt6 import QtCore

from app.constants import SAFE_RAMP_STEP_T, SAFE_RAMP_STEP_V, V_LIMIT
from app.models import Connections, DualGateParams, SaveRoot
from app.result_channels import KEITHLEY_CHANNEL
from app.utils import _frange_inc, _safe, _sanitize_base, clamp, safe_ramp
from app.workers.base import RunWorker


class DualGateWorker(RunWorker):
    def __init__(self, params: DualGateParams, save: SaveRoot, conns: Connections, **kw):
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
                self.error.emit("Required session missing: DAQ")
                return

            for field in ("vds_start", "vds_stop", "vtg_set", "vbg_set"):
                setattr(self.p, field, clamp(getattr(self.p, field), -V_LIMIT, V_LIMIT))

            ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
            tag_src = "Vdsketh" if self.p.vds_source == "Keithley 2400" else f"Vdsdaq_ao{self.p.ao_channel}"
            g1_tag = "Tg" if self.g1 else "NoTg"
            g2_tag = "Bg" if self.g2 else "NoBg"
            device_id = _sanitize_base(self.save.device_id)
            stem = (
                f"{device_id}_{_sanitize_base(self.p.base_name)}"
                f"_{g1_tag}_{g2_tag}_{tag_src}"
                f"_Vtg{self.p.vtg_set:+.3f}V_Vbg{self.p.vbg_set:+.3f}V_{ts}"
            )
            csv_path = os.path.join(self.save.path(), stem + ".csv")
            self.log.emit(f"Save -> {csv_path}")

            self.status.emit("Ramping gates to set voltage...")
            if self.g1 is not None:
                safe_ramp(
                    self.g1.set_voltage,
                    getattr(self.g1, "voltage", None) or 0.0,
                    self.p.vtg_set,
                    SAFE_RAMP_STEP_V,
                    SAFE_RAMP_STEP_T,
                    self.check_abort_pause,
                )
            if self.g2 is not None:
                safe_ramp(
                    self.g2.set_voltage,
                    getattr(self.g2, "voltage", None) or 0.0,
                    self.p.vbg_set,
                    SAFE_RAMP_STEP_V,
                    SAFE_RAMP_STEP_T,
                    self.check_abort_pause,
                )

            self.status.emit("Ramping Vds to sweep start...")
            self._safe_ramp_vds(self.p.vds_start)
            self.check_abort_pause()

            step = self.p.vds_step if self.p.vds_stop >= self.p.vds_start else -abs(self.p.vds_step)
            forward_seq = _frange_inc(self.p.vds_start, self.p.vds_stop, step)
            backward_seq = list(reversed(forward_seq)) if self.p.sweep_both_ways else []
            grand_total = len(forward_seq) + len(backward_seq)
            self.clear_plot.emit()

            with open(csv_path, "a", newline="", buffering=1, encoding="utf-8") as f:
                w = csv.writer(f)
                w.writerow(["Vtg", "Vbg", "Vbias", "raw_X", "raw_Y", "raw_DC", "Ids_X", "Ids_Y", "Ids_DC", KEITHLEY_CHANNEL, "Direction"])
                w.writerow(["V", "V", "V", "A", "A", "A", "A", "A", "A", "A", ""])
                self._run_vds_pass(f, w, forward_seq, "forward", 0, grand_total)
                if backward_seq:
                    self.check_abort_pause()
                    self._run_vds_pass(f, w, backward_seq, "backward", len(forward_seq), grand_total)

            self.finished.emit(csv_path)
        except Exception as ex:
            self.error.emit(str(ex))
        finally:
            try:
                if self.g1 is not None:
                    safe_ramp(self.g1.set_voltage, getattr(self.g1, "voltage", None) or 0.0, 0.0, SAFE_RAMP_STEP_V, SAFE_RAMP_STEP_T)
                if self.g2 is not None:
                    safe_ramp(self.g2.set_voltage, getattr(self.g2, "voltage", None) or 0.0, 0.0, SAFE_RAMP_STEP_V, SAFE_RAMP_STEP_T)
                self._safe_ramp_vds(0.0)
            except Exception:
                pass
            self.log.emit("Outputs returned to 0 V; sessions kept open.")

    def _safe_ramp_vds(self, target: float) -> None:
        if self.p.vds_source == "Keithley 2400":
            if self.g3 is not None:
                safe_ramp(
                    self.g3.set_voltage,
                    getattr(self.g3, "voltage", None) or 0.0,
                    target,
                    SAFE_RAMP_STEP_V,
                    SAFE_RAMP_STEP_T,
                )
        else:
            safe_ramp(
                lambda v: self.daq.set_voltage(self.p.ao_channel, v),
                self.daq.get_ao_value(self.p.ao_channel),
                target,
                SAFE_RAMP_STEP_V,
                SAFE_RAMP_STEP_T,
            )

    def _run_vds_pass(self, f, w, vseq: list, direction: str, idx_offset: int, grand_total: int) -> None:
        for i, vds in enumerate(vseq, start=1):
            self.check_abort_pause()
            self.status.emit(f"Point {idx_offset + i}/{grand_total}  [{direction}]")
            if self.p.vds_source == "Keithley 2400":
                if self.g3 is None:
                    self.error.emit("Gate3 (Keithley Vds) not connected")
                    return
                _safe(self.g3, "ramp_voltage", vds, self.p.vds_ramp)
            else:
                _safe(self.daq, "ramp_voltage", self.p.ao_channel, vds, self.p.vds_ramp)

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
            w.writerow([self.p.vtg_set, self.p.vbg_set, vds, raw_x, raw_y, raw_dc, ids_x, ids_y, ids_dc, ids_keithley, direction])
            try:
                f.flush()
                os.fsync(f.fileno())
            except Exception:
                pass

            y = self._plot_value(ids_dc, ids_x, ids_y, ids_keithley)
            self.point.emit(vds, y)
            self.point_data.emit({
                "x": vds,
                "Ids_DC": ids_dc,
                "Ids_X": ids_x,
                "Ids_Y": ids_y,
                KEITHLEY_CHANNEL: ids_keithley,
                "direction": direction,
            })
            self.progress.emit((idx_offset + i) / grand_total)

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
