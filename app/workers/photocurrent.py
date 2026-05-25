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
from app.models import Connections, PhotocurrentParams, SaveRoot
from app.result_channels import KEITHLEY_CHANNEL
from app.utils import _safe, _sanitize_base, clamp, safe_ramp
from app.workers.base import RunWorker


class PhotocurrentWorker(RunWorker):
    def __init__(self, params: PhotocurrentParams, save: SaveRoot, conns: Connections, **kw):
        super().__init__()
        self.p = params
        self.save = save
        self.conns = conns
        self.g1 = kw.get("g1")
        self.g2 = kw.get("g2")
        self.g3 = kw.get("g3")
        self.daq = kw.get("daq")
        self.mono = kw.get("mono")
        self.plot_choice = kw.get("plot_choice")
        self.amp_rate = kw.get("amp_rate", 1e7)
        self.lkn_rate = kw.get("lkn_rate", 100.0)

    @QtCore.pyqtSlot()
    def run(self):
        try:
            if self.daq is None or self.mono is None:
                self.error.emit("Required sessions missing: DAQ or Monochromator")
                return

            for field in ("vds_set", "vtg_set", "vbg_set"):
                setattr(self.p, field, clamp(getattr(self.p, field), -V_LIMIT, V_LIMIT))

            ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
            tag_vds = "novds"
            if self.p.use_vds:
                if self.p.vds_source.startswith("NI DAQ"):
                    tag_vds = f"ao{self.p.ao_channel}"
                elif self.p.vds_source == "Keithley 2400":
                    tag_vds = "keth"
            g1_tag = "Tg" if self.g1 else "NoTg"
            g2_tag = "Bg" if self.g2 else "NoBg"
            device_id = _sanitize_base(self.save.device_id)
            stem = f"{device_id}_{_sanitize_base(self.p.base_name)}_{g1_tag}_{g2_tag}_pc_{tag_vds}_Vtg{self.p.vtg_set:+.3f}V_Vbg{self.p.vbg_set:+.3f}V_{ts}"
            csv_path = os.path.join(self.save.path(), stem + ".csv")
            self.log.emit(f"Save -> {csv_path}")

            need_header = not os.path.exists(csv_path) or os.path.getsize(csv_path) == 0
            with open(csv_path, "a", newline="", buffering=1, encoding="utf-8") as f:
                w = csv.writer(f)
                if need_header:
                    w.writerow(["Wavelength", "Vtg", "Vbg", "Vbias", "raw_X", "raw_Y", "raw_DC", "Ids_X", "Ids_Y", "Ids_DC", KEITHLEY_CHANNEL])
                    w.writerow(["nm", "V", "V", "V", "A", "A", "A", "A", "A", "A", "A"])
                    self.log.emit("[csv] header written")

                if self.g1 is not None:
                    self.log.emit(
                        f"Ramping G1/Vtg to {self.p.vtg_set:.3f} V "
                        f"({GATE_BIAS_RAMP_STEP_V:g} V/step, {GATE_BIAS_RAMP_STEP_T:g} s/step)"
                    )
                    safe_ramp(
                        self.g1.set_voltage,
                        getattr(self.g1, "voltage", None) or 0.0,
                        self.p.vtg_set,
                        GATE_BIAS_RAMP_STEP_V,
                        GATE_BIAS_RAMP_STEP_T,
                        self.check_abort_pause,
                    )
                if self.g2 is not None:
                    self.log.emit(
                        f"Ramping G2/Vbg to {self.p.vbg_set:.3f} V "
                        f"({GATE_BIAS_RAMP_STEP_V:g} V/step, {GATE_BIAS_RAMP_STEP_T:g} s/step)"
                    )
                    safe_ramp(
                        self.g2.set_voltage,
                        getattr(self.g2, "voltage", None) or 0.0,
                        self.p.vbg_set,
                        GATE_BIAS_RAMP_STEP_V,
                        GATE_BIAS_RAMP_STEP_T,
                        self.check_abort_pause,
                    )

                vds_now = 0.0
                if self.p.use_vds:
                    vds_now = float(self.p.vds_set)
                    if self.p.vds_source == "Keithley 2400":
                        if self.g3:
                            _safe(self.g3, "ramp_voltage", vds_now, self.p.vds_ramp)
                        else:
                            self.error.emit("Vds Source set to Keithley but Gate3 not connected.")
                            return
                    elif self.p.vds_source.startswith("NI DAQ"):
                        _safe(self.daq, "ramp_voltage", self.p.ao_channel, vds_now, self.p.vds_ramp)

                wl = self.p.wl_start
                step = self.p.wl_step if self.p.wl_stop >= self.p.wl_start else -abs(self.p.wl_step)
                total = max(1, int(1 + round((self.p.wl_stop - self.p.wl_start) / (step if step != 0 else 1e-9))))
                idx = 0
                while True:
                    self.check_abort_pause()
                    _safe(self.mono, "set_wavelength", float(wl))
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
                    w.writerow([float(wl), self.p.vtg_set, self.p.vbg_set, vds_now, raw_x, raw_y, raw_dc, ids_x, ids_y, ids_dc, ids_keithley])
                    try:
                        f.flush()
                        os.fsync(f.fileno())
                    except Exception:
                        pass

                    y = self._plot_value(ids_dc, ids_x, ids_y, ids_keithley)
                    self.point.emit(float(wl), y)
                    self.point_data.emit({
                        "x": float(wl),
                        "Ids_DC": ids_dc,
                        "Ids_X": ids_x,
                        "Ids_Y": ids_y,
                        KEITHLEY_CHANNEL: ids_keithley,
                    })
                    idx += 1
                    self.progress.emit(idx / total)
                    if (step >= 0 and wl >= self.p.wl_stop - 1e-12) or (step < 0 and wl <= self.p.wl_stop + 1e-12):
                        break
                    wl += step

            self.finished.emit(csv_path)
        except Exception as ex:
            self.error.emit(str(ex))
        finally:
            try:
                if self.g1 is not None:
                    safe_ramp(self.g1.set_voltage, getattr(self.g1, "voltage", None) or 0.0, 0.0, SAFE_RAMP_STEP_V, SAFE_RAMP_STEP_T)
                if self.g2 is not None:
                    safe_ramp(self.g2.set_voltage, getattr(self.g2, "voltage", None) or 0.0, 0.0, SAFE_RAMP_STEP_V, SAFE_RAMP_STEP_T)
                if self.p.use_vds:
                    if self.p.vds_source == "Keithley 2400":
                        if self.g3 is not None:
                            safe_ramp(self.g3.set_voltage, getattr(self.g3, "voltage", None) or 0.0, 0.0, SAFE_RAMP_STEP_V, SAFE_RAMP_STEP_T)
                    elif self.p.vds_source.startswith("NI DAQ"):
                        safe_ramp(
                            lambda v: self.daq.set_voltage(self.p.ao_channel, v),
                            self.daq.get_ao_value(self.p.ao_channel),
                            0.0,
                            SAFE_RAMP_STEP_V,
                            SAFE_RAMP_STEP_T,
                        )
            except Exception:
                pass
            self.log.emit("Outputs returned to 0 V; sessions kept open.")

    def _read_keithley_current(self):
        if not self.p.use_vds or self.p.vds_source != "Keithley 2400" or self.g3 is None:
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
