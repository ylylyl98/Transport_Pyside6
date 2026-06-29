from __future__ import annotations

import csv
import datetime
import os
import re
import time

from PyQt6 import QtCore

from app.constants import (
    GATE_BIAS_RAMP_STEP_T,
    GATE_BIAS_RAMP_STEP_V,
    SAFE_RAMP_STEP_T,
    SAFE_RAMP_STEP_V,
    V_LIMIT,
)
from app.models import Connections, PhotocurrentBiasCondition, PhotocurrentParams, SaveRoot
from app.result_channels import KEITHLEY_CHANNEL
from app.run_output import update_run_metadata_status, write_run_metadata
from app.utils import _sanitize_base, safe_ramp
from app.workers.base import RunStopped, RunWorker


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

    @staticmethod
    def condition_csv_path(
        base_csv_path: str,
        condition: PhotocurrentBiasCondition,
        use_vds: bool,
    ) -> str:
        """Return the base output path dedicated to one applied bias combination."""
        stem, extension = os.path.splitext(base_csv_path)
        extension = extension or ".csv"
        parts = [
            f"Vtg_{condition.vtg:+.3f}V",
            f"Vbg_{condition.vbg:+.3f}V",
            f"Vds_{condition.vds:+.3f}V" if use_vds else "Vds_Off",
        ]
        timestamp_match = re.search(r"_(\d{8}_\d{6})$", stem)
        if timestamp_match:
            prefix = stem[: timestamp_match.start()]
            timestamp = timestamp_match.group(0)
            return prefix + "_" + "_".join(parts) + timestamp + extension
        return stem + "_" + "_".join(parts) + extension

    @classmethod
    def condition_csv_paths(
        cls,
        base_csv_path: str,
        conditions: list[PhotocurrentBiasCondition],
        use_vds: bool,
    ) -> list[str]:
        """Build unique paths, suffixing repeated applied bias combinations."""
        paths: list[str] = []
        occurrences: dict[str, int] = {}
        for condition in conditions:
            base_path = cls.condition_csv_path(base_csv_path, condition, use_vds)
            occurrences[base_path] = occurrences.get(base_path, 0) + 1
            occurrence = occurrences[base_path]
            if occurrence == 1:
                paths.append(base_path)
                continue
            stem, extension = os.path.splitext(base_path)
            timestamp_match = re.search(r"_(\d{8}_\d{6})$", stem)
            if timestamp_match:
                prefix = stem[: timestamp_match.start()]
                timestamp = timestamp_match.group(0)
                paths.append(f"{prefix}_repeat_{occurrence:02d}{timestamp}{extension}")
            else:
                paths.append(f"{stem}_repeat_{occurrence:02d}{extension}")
        return paths

    @QtCore.pyqtSlot()
    def run(self):
        csv_path = self.p.output_csv_path
        condition_paths: list[str] = []
        generated_paths: list[str] = []
        run_status = "error"
        run_detail = "Run ended before completion."
        try:
            if self.daq is None or self.mono is None:
                raise RuntimeError("Required sessions missing: DAQ or Monochromator")
            conditions = self._active_conditions()
            if not conditions:
                raise RuntimeError("Select at least one photocurrent bias condition to run.")
            if int(self.p.n_sample) < 1:
                raise RuntimeError("Averages must be at least 1.")
            if self.p.wl_step <= 0:
                raise RuntimeError("Wavelength step must be greater than zero.")
            if self.p.use_vds and self.p.vds_ramp <= 0:
                raise RuntimeError("Vds ramp step must be greater than zero.")
            if self.p.use_vds and self.p.vds_source == "None":
                raise RuntimeError("Vds bias is enabled but no Vds source is selected.")
            self._uses_g1 = any(abs(condition.vtg) > 1e-12 for condition in conditions)
            self._uses_g2 = any(abs(condition.vbg) > 1e-12 for condition in conditions)
            if self._uses_g1 and self.g1 is None:
                raise RuntimeError("G1 / Vtg is required because a selected condition uses a nonzero Vtg.")
            if self._uses_g2 and self.g2 is None:
                raise RuntimeError("G2 / Vbg is required because a selected condition uses a nonzero Vbg.")
            for condition_number, condition in enumerate(conditions, start=1):
                values = (("Vtg", condition.vtg), ("Vbg", condition.vbg))
                if self.p.use_vds:
                    values += (("Vds", condition.vds),)
                for label, value in values:
                    if abs(float(value)) > V_LIMIT:
                        raise RuntimeError(
                            f"Condition {condition_number} {label} is {value:.3f} V, above the {V_LIMIT:.1f} V limit."
                        )
                if condition.settle_s < 0:
                    raise RuntimeError(f"Condition {condition_number} settle time must be zero or greater.")

            wavelengths = self._wavelengths()
            total_points = len(conditions) * len(wavelengths)

            if not csv_path:
                ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
                tag_vds = "noVds"
                if self.p.use_vds:
                    if self.p.vds_source.startswith("NI DAQ"):
                        tag_vds = f"ao{self.p.ao_channel}"
                    elif self.p.vds_source == "Keithley 2400":
                        tag_vds = "Keithley"
                g1_tag = "Tg" if self.g1 else "NoTg"
                g2_tag = "Bg" if self.g2 else "NoBg"
                device_id = _sanitize_base(self.save.device_id)
                stem = f"{device_id}_{_sanitize_base(self.p.base_name)}_{g1_tag}_{g2_tag}_pc_{tag_vds}_{len(conditions)}conditions_{ts}"
                csv_path = os.path.join(self.save.path(), stem + ".csv")
            os.makedirs(os.path.dirname(csv_path), exist_ok=True)
            condition_paths = self.condition_csv_paths(csv_path, conditions, self.p.use_vds)
            self.log.emit(f"Save -> {len(condition_paths)} condition CSV file(s) in {os.path.dirname(csv_path)}")
            if self.p.output_metadata_path:
                write_run_metadata(
                    self.p.output_metadata_path,
                    {
                        "measurement": "photocurrent",
                        "csv_path": condition_paths[0],
                        "csv_paths": condition_paths,
                        "save_root": self.save,
                        "connections": self.conns,
                        "params": self.p,
                    },
                )

            point_index = 0
            for condition_number, (condition, condition_path) in enumerate(zip(conditions, condition_paths), start=1):
                self.check_abort_pause()
                self.status.emit(f"Condition {condition_number}/{len(conditions)}: applying biases")
                self._ramp_gate(self.g1, "G1/Vtg", condition.vtg, self._uses_g1)
                self._ramp_gate(self.g2, "G2/Vbg", condition.vbg, self._uses_g2)
                vds_now = self._ramp_vds(condition.vds)
                if condition.settle_s:
                    self.status.emit(f"Condition {condition_number}/{len(conditions)}: settling for {condition.settle_s:g} s")
                    self._wait_with_abort(condition.settle_s)

                with open(condition_path, "x", newline="", buffering=1, encoding="utf-8") as f:
                    generated_paths.append(condition_path)
                    w = csv.writer(f)
                    w.writerow(["Condition", "Wavelength", "Vtg", "Vbg", "Vds", "raw_X", "raw_Y", "raw_DC", "Ids_X", "Ids_Y", "Ids_DC", KEITHLEY_CHANNEL])
                    w.writerow(["", "nm", "V", "V", "V", "A", "A", "A", "A", "A", "A", "A"])
                    self.log.emit(f"[csv] condition {condition_number} -> {condition_path}")
                    for wavelength_index, wl in enumerate(wavelengths, start=1):
                        self.check_abort_pause()
                        self.status.emit(
                            f"Condition {condition_number}/{len(conditions)}, wavelength {wavelength_index}/{len(wavelengths)}"
                        )
                        self.mono.set_wavelength(float(wl))
                        self._wait_with_abort(max(0.0, self.p.delay))

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
                            condition_number,
                            float(wl),
                            condition.vtg,
                            condition.vbg,
                            vds_now,
                            raw_x,
                            raw_y,
                            raw_dc,
                            ids_x,
                            ids_y,
                            ids_dc,
                            ids_keithley,
                        ])
                        try:
                            f.flush()
                            os.fsync(f.fileno())
                        except Exception:
                            pass

                        y = self._plot_value(ids_dc, ids_x, ids_y, ids_keithley)
                        self.point.emit(float(wl), y)
                        self.point_data.emit({
                            "x": float(wl),
                            "condition": condition_number,
                            "Vtg": condition.vtg,
                            "Vbg": condition.vbg,
                            "Vds": vds_now,
                            "Ids_DC": ids_dc,
                            "Ids_X": ids_x,
                            "Ids_Y": ids_y,
                            KEITHLEY_CHANNEL: ids_keithley,
                        })
                        point_index += 1
                        self.progress.emit(point_index / total_points)
            run_status = "finished"
            run_detail = f"Saved {len(generated_paths)} condition CSV file(s) in: {os.path.dirname(csv_path)}"
            self.finished.emit(os.path.dirname(csv_path))
        except RunStopped as ex:
            run_status = "stopped"
            run_detail = f"{ex}. Partial condition files saved: {', '.join(generated_paths)}" if generated_paths else str(ex)
            self.stopped.emit(run_detail)
        except Exception as ex:
            run_status = "error"
            run_detail = str(ex)
            self.error.emit(run_detail)
        finally:
            failures = []
            try:
                if getattr(self, "_uses_g1", False) and self.g1 is not None:
                    safe_ramp(self.g1.set_voltage, self._source_voltage(self.g1), 0.0, SAFE_RAMP_STEP_V, SAFE_RAMP_STEP_T)
            except Exception as ex:
                failures.append(f"G1/Vtg zero failed: {ex}")
            try:
                if getattr(self, "_uses_g2", False) and self.g2 is not None:
                    safe_ramp(self.g2.set_voltage, self._source_voltage(self.g2), 0.0, SAFE_RAMP_STEP_V, SAFE_RAMP_STEP_T)
            except Exception as ex:
                failures.append(f"G2/Vbg zero failed: {ex}")
            try:
                if self.p.use_vds:
                    if self.p.vds_source == "Keithley 2400":
                        if self.g3 is not None:
                            safe_ramp(self.g3.set_voltage, self._source_voltage(self.g3), 0.0, SAFE_RAMP_STEP_V, SAFE_RAMP_STEP_T)
                    elif self.p.vds_source.startswith("NI DAQ"):
                        safe_ramp(
                            lambda v: self.daq.set_voltage(self.p.ao_channel, v),
                            self.daq.get_ao_value(self.p.ao_channel),
                            0.0,
                            SAFE_RAMP_STEP_V,
                            SAFE_RAMP_STEP_T,
                        )
            except Exception as ex:
                failures.append(f"Vds zero failed: {ex}")
            self.emit_safe_state_report(failures)
            update_run_metadata_status(self.p.output_metadata_path, run_status, run_detail, failures)

    def _active_conditions(self) -> list[PhotocurrentBiasCondition]:
        if self.p.bias_conditions:
            return [condition for condition in self.p.bias_conditions if condition.enabled]
        return [
            PhotocurrentBiasCondition(
                vtg=self.p.vtg_set,
                vbg=self.p.vbg_set,
                vds=self.p.vds_set,
            )
        ]

    def _wavelengths(self) -> list[float]:
        step = self.p.wl_step if self.p.wl_stop >= self.p.wl_start else -abs(self.p.wl_step)
        values: list[float] = []
        wl = float(self.p.wl_start)
        while True:
            values.append(float(wl))
            if (step >= 0 and wl >= self.p.wl_stop - 1e-12) or (step < 0 and wl <= self.p.wl_stop + 1e-12):
                return values
            next_wl = wl + step
            if (step >= 0 and next_wl > self.p.wl_stop) or (step < 0 and next_wl < self.p.wl_stop):
                wl = float(self.p.wl_stop)
            else:
                wl = next_wl

    def _source_voltage(self, session) -> float:
        try:
            return float(session.get_voltage_setpoint())
        except Exception:
            return getattr(session, "voltage", None) or 0.0

    def _ramp_gate(self, session, label: str, target: float, required: bool):
        if not required:
            return
        if session is None:
            raise RuntimeError(f"{label} is required by the selected bias recipe.")
        start = self._source_voltage(session)
        self.log.emit(
            f"Ramping {label} from {start:.3f} V to {target:.3f} V "
            f"({GATE_BIAS_RAMP_STEP_V:g} V/step, {GATE_BIAS_RAMP_STEP_T:g} s/step)"
        )
        safe_ramp(
            session.set_voltage,
            start,
            target,
            GATE_BIAS_RAMP_STEP_V,
            GATE_BIAS_RAMP_STEP_T,
            self.check_abort_pause,
        )

    def _ramp_vds(self, target: float) -> float:
        if not self.p.use_vds:
            return 0.0
        target = float(target)
        if self.p.vds_source == "Keithley 2400":
            if self.g3 is None:
                raise RuntimeError("Vds source is Keithley G3, but G3 is not connected.")
            safe_ramp(
                self.g3.set_voltage,
                self._source_voltage(self.g3),
                target,
                self.p.vds_ramp,
                SAFE_RAMP_STEP_T,
                self.check_abort_pause,
            )
        elif self.p.vds_source.startswith("NI DAQ"):
            safe_ramp(
                lambda value: self.daq.set_voltage(self.p.ao_channel, value),
                self.daq.get_ao_value(self.p.ao_channel),
                target,
                self.p.vds_ramp,
                SAFE_RAMP_STEP_T,
                self.check_abort_pause,
            )
        return target

    def _wait_with_abort(self, seconds: float):
        until = time.monotonic() + max(0.0, seconds)
        while time.monotonic() < until:
            self.check_abort_pause()
            time.sleep(min(0.05, until - time.monotonic()))

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
