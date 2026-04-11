from __future__ import annotations

import csv
import datetime
import os
import time

from PyQt6 import QtCore

from app.constants import V_LIMIT
from app.models import Connections, DualGateParams, SaveRoot
from app.result_channels import KEITHLEY_CHANNEL
from app.utils import _frange_inc, _safe, _sanitize_base, clamp
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
            stem = f"{_sanitize_base(self.p.base_name)}_{g1_tag}_{g2_tag}_{tag_src}_Vtg{self.p.vtg_set:+.3f}V_Vbg{self.p.vbg_set:+.3f}V_{ts}"
            csv_path = os.path.join(self.save.path(), stem + ".csv")
            self.log.emit(f"Save -> {csv_path}")

            with open(csv_path, "a", newline="", buffering=1, encoding="utf-8") as f:
                w = csv.writer(f)
                w.writerow(["Vg1", "Vg2", "Vds", "raw_X", "raw_Y", "raw_DC", "Ids_X", "Ids_Y", "Ids_DC", KEITHLEY_CHANNEL])
                w.writerow(["V", "V", "V", "A", "A", "A", "A", "A", "A", "A"])

                _safe(self.g1, "ramp_voltage", self.p.vtg_set, self.p.vg_ramp)
                _safe(self.g2, "ramp_voltage", self.p.vbg_set, self.p.vg_ramp)

                step = self.p.vds_step if self.p.vds_stop >= self.p.vds_start else -abs(self.p.vds_step)
                vseq = _frange_inc(self.p.vds_start, self.p.vds_stop, step)
                total = max(1, len(vseq))

                for i, vds in enumerate(vseq, start=1):
                    self.check_abort_pause()
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
                    w.writerow([self.p.vtg_set, self.p.vbg_set, vds, raw_x, raw_y, raw_dc, ids_x, ids_y, ids_dc, ids_keithley])
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
                    })
                    self.progress.emit(i / total)

            self.finished.emit(csv_path)
        except Exception as ex:
            self.error.emit(str(ex))
        finally:
            try:
                _safe(self.g1, "ramp_voltage", 0.0, self.p.vg_ramp)
                _safe(self.g2, "ramp_voltage", 0.0, self.p.vg_ramp)
                if self.p.vds_source == "Keithley 2400" and self.g3 is not None:
                    _safe(self.g3, "ramp_voltage", 0.0, self.p.vds_ramp)
                else:
                    _safe(self.daq, "ramp_voltage", self.p.ao_channel, 0.0, self.p.vds_ramp)
            except Exception:
                pass
            self.log.emit("Outputs returned to 0 V; sessions kept open.")

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
