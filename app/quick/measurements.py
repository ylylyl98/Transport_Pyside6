"""QObject acquisition facade. No QWidget or measurement-tab construction.

Workers retain sole responsibility for instrument motion, CSVs and safe zeroing.
Device ownership is released only after the worker's entire finally block returns.
The magnetic engines use this object as their UI-independent orchestration port.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict
from datetime import datetime
import math
import os

from PySide6.QtCore import QObject, Property, Signal, Slot

from app.models import LineSweepParams
from app.constants import V_LIMIT
from app.experiment_metadata import capture_run_signal_chain
from app.signal_chain import SignalChainSnapshot, signal_chain_metadata
from app.measurement_output import dual_gate_filename_parts, gate_scan_filename_parts
from app.cosweep_output import map_filename_parts
from app.run_output import (build_planned_output, unique_planned_output, output_blocking_reason,
                            to_jsonable)
from app.workers.dual_gate import DualGateWorker
from app.workers.line_sweep import LineSweepWorker
from app.workers.cosweep import CoSweepWorker, build_cosweep_points, validate_cosweep_params
from app.workers.photocurrent import PhotocurrentWorker
from app.voltage_resolution import source_resolution_status, validate_voltage_points
from app.plot_ranges import coordinate_ranges
from app.utils import _frange_inc
from app.quick.parameters import MODES, defaults, schema, convert, row_default, build_params, legacy_values, measurement_sections
from app.quick.run_state import MeasurementThread, PreflightThread, WorkerRelay


class MeasurementService(QObject):
    changed = Signal()
    fieldsChanged = Signal()
    modeChanged = Signal()
    recordReady = Signal(object)
    runPrepared = Signal(object)
    runCompleted = Signal(object)
    errorRaised = Signal(str)
    confirmationRequested = Signal(str, str, str)
    shutdownReady = Signal()
    batch_run_terminal = Signal(str, str)

    def __init__(self, manager, save_root, get_global_rates=None, get_signal_chain=None,
                 parent=None, *, magnet1000=None, thermal_safety=None,
                 lakeshore_controller=None, selected_backend_callable=None,
                 review_valid_callable=None, temperature_provider=None,
                 context_provider=None, temperature_ready_callable=None,
                 worker_factories=None, settings=None):
        super().__init__(parent)
        self.device_manager = manager
        self.save = save_root
        self.conns = manager.connections
        self.get_global_rates = get_global_rates or (lambda: (1e7, 100.))
        self.get_signal_chain = get_signal_chain or SignalChainSnapshot
        self._context_provider = context_provider
        self._temperature_ready = temperature_ready_callable or (lambda: True)
        self._selected_backend = selected_backend_callable or (lambda: "1000")
        self._review_valid_callback = review_valid_callable or (lambda: False)
        self._values = [defaults(i) for i in range(len(MODES))]
        self._selected_rows = [{} for _ in MODES]
        self._field_errors = [{} for _ in MODES]
        self._mode = 0
        self._phase, self._message, self._progress = "idle", "Ready", 0.
        self._continuation = (False, False, "")
        self._logs = []
        self._log_path = ""
        self._claimed = []
        self.worker = None
        self.worker_thread = None
        self._preflight_thread = None
        self._preflight_request = None
        self._engine_calibration = None
        self._generation = 0
        self._record_index = 0
        self._terminal = None
        self._cleanup_warning = ""
        self._batch_claimed = False
        self._batch_params = None
        self._batch_calibration = None
        self._planned_output = None
        self._transport_outputs = None
        self._run_snapshot = None
        self._batch_snapshots = []
        self._confirmation = None
        self._confirmation_granted = None
        self._magnet = magnet1000
        self._batch = self._transport = None
        self._settings = settings
        self._factories = {0: DualGateWorker, 1: LineSweepWorker, 2: CoSweepWorker, 3: PhotocurrentWorker, 4: LineSweepWorker}
        self._factories.update(worker_factories or {})
        self._load_settings()
        for signal_name in ("status_changed", "operation_changed", "protection_changed"):
            signal = getattr(manager, signal_name, None)
            if signal is not None:
                signal.connect(lambda *_: self.changed.emit())
        if magnet1000 is not None:
            from app.engine.gate_scan_field_batch import GateScanFieldBatch
            from app.engine.bfield_transport_controller import BFieldTransportController
            self._batch = GateScanFieldBatch(magnet1000, self, self,
                selected_backend_callable=self._selected_backend, thermal_safety=thermal_safety,
                lakeshore_controller=lakeshore_controller)
            self._batch.set_review_validator(self._review_valid_callback)
            self._batch.measurement_temperature_provider = temperature_provider
            self._transport = BFieldTransportController(magnet1000, self, manager, thermal_safety, self)
            self._transport.lakeshore_controller = lakeshore_controller
            for engine in (self._batch, self._transport):
                engine.state_changed.connect(self._engine_state)
                engine.progress_changed.connect(self._engine_progress)
                engine.error.connect(self._engine_error)
                engine.continuation_changed.connect(self._on_continuation)
            self._transport.finished.connect(self._engine_finished)
            self._transport.stopped.connect(self._engine_stopped)
            self._transport.shutdown_ready.connect(self.shutdownReady)
            self._batch.cleanup_finished.connect(self._batch_finished)
            self._batch.activity.connect(lambda e: self.append_log(e.get("message", str(e))))
            if lakeshore_controller is not None:
                signal = getattr(lakeshore_controller, "snapshot_updated", None)
                if signal is not None:
                    signal.connect(self._transport.on_lakeshore_snapshot)
                signal = getattr(lakeshore_controller, "disconnected", None)
                if signal is not None:
                    signal.connect(self._transport.on_lakeshore_disconnected)
                    signal.connect(self._batch.on_lakeshore_disconnected)
                signal = getattr(lakeshore_controller, "snapshot_updated", None)
                if signal is not None:
                    signal.connect(self._batch.on_lakeshore_snapshot)
                signal = getattr(lakeshore_controller, "fault", None)
                if signal is not None:
                    signal.connect(self._thermal_fault)
                signal = getattr(lakeshore_controller, "error", None)
                if signal is not None:
                    signal.connect(self._thermal_error)

    modes = Property("QVariantList", lambda self: [{"id": i, "key": m[0], "title": m[1]} for i, m in enumerate(MODES)], constant=True)

    def _set_mode(self, mode):
        mode = int(mode)
        if not 0 <= mode < len(MODES):
            self._fail("Unknown measurement mode")
            return
        if self.active:
            self._fail("Wait for measurement cleanup before changing mode")
            return
        if mode != self._mode:
            self._mode = mode
            self._confirmation = self._confirmation_granted = None
            self._phase, self._message = "idle", "Ready"
            self.modeChanged.emit()
            self.fieldsChanged.emit()
            self.changed.emit()

    mode = Property(int, lambda self: self._mode, _set_mode, notify=modeChanged)
    fields = Property("QVariantList", lambda self: schema(self._values[self._mode], self._mode, self._field_errors[self._mode]), notify=fieldsChanged)
    uiSections = Property("QVariantList", lambda self: measurement_sections(self._values[self._mode], self._mode, self._selected_rows[self._mode], self._field_errors[self._mode]), notify=fieldsChanged)
    summary = Property(str, lambda self: self._summary(), notify=fieldsChanged)
    phase = Property(str, lambda self: self._phase, notify=changed)
    message = Property(str, lambda self: self._message, notify=changed)
    progress = Property(float, lambda self: self._progress, notify=changed)
    active = Property(bool, lambda self: bool(self.worker_thread is not None or self._preflight_thread is not None or self._batch_claimed or
        self._confirmation is not None or (self._batch and self._batch.active) or
        (self._transport and (self._transport.active or self._transport._cleanup_in_progress))), notify=changed)
    canStart = Property(bool, lambda self: not self.active and not self._field_errors[self._mode] and not self.device_manager.is_busy(), notify=changed)
    canStop = Property(bool, lambda self: self.active, notify=changed)
    continuationRequired = Property(bool, lambda self: self._continuation[0], notify=changed)
    canContinue = Property(bool, lambda self: self._continuation[1], notify=changed)
    continuationReason = Property(str, lambda self: self._continuation[2], notify=changed)
    logText = Property(str, lambda self: "\n".join(self._logs), notify=changed)

    def _summary(self):
        """Bounded, read-only trajectory review; never queries instruments."""
        try:
            params = self.collect_params()
            mode = self._mode
            if mode in (1, 4):
                from app.gate_scan_summary import format_gate_scan_condition
                from app.gate_transform import ratio_formula_text
                rows = self._values[4]["conditions"] if mode == 4 else []
                if rows:
                    enabled = [row for row in rows if row["enabled"]]
                    if enabled:
                        params = build_params(enabled[0], 4)
                    else:
                        raise ValueError("Enable at least one Gate Scan condition")
                text = format_gate_scan_condition(params, details=True)
                if params.mode == "Derived":
                    text += "\n" + ratio_formula_text(params.derived_ratio_target)
                if mode == 4:
                    from app.engine.gate_scan_field_batch import GateScanFieldBatch
                    targets = GateScanFieldBatch.parse_fields(self._values[4]["fields_t"])
                    preview = ", ".join(f"{v:+g}" for v in targets[:12])
                    text = f"Persistent B-field targets ({len(targets)}): {preview}{' …' if len(targets) > 12 else ''} T\n" + text
                    count = sum(bool(r['enabled']) for r in rows) if rows else 1
                    text = (f"{len(targets) * count} jobs = {len(targets)} fields × {count} enabled conditions; field-major order\n"
                            + text + (f"\nFirst enabled condition: {enabled[0]['name']}" if rows else "\nUsing default condition"))
                    if rows:
                        text += "\n" + "\n".join(f"{i+1}. {row['name']}: {format_gate_scan_condition(build_params(row, 4)).splitlines()[0]}" for i, row in enumerate(enabled[:8]))
                return text
            if mode == 0:
                if params.vds_step <= 0:
                    raise ValueError("Vds sampling step must be positive")
                count = math.ceil(round(abs(params.vds_stop-params.vds_start)/params.vds_step, 9)) + 1
                total = count * (2 if params.sweep_both_ways else 1)
                return (f"Vds: {params.vds_start:g} → {params.vds_stop:g} V, {params.vds_step:g} V per point\n"
                        f"Fixed Vtg={params.vtg_set:g} V, Vbg={params.vbg_set:g} V; {params.vds_source}\n"
                        f"{total:,} points; {'forward + backward' if params.sweep_both_ways else 'forward only'}; "
                        f"{params.n_sample} averages; acquisition delay at least {total*params.delay:g} s")
            if mode == 3:
                if params.wl_step <= 0:
                    raise ValueError("Wavelength step must be positive")
                count = math.ceil(round(abs(params.wl_stop-params.wl_start)/params.wl_step, 9)) + 1
                rows = [c for c in params.bias_conditions if c.enabled]
                text = (f"Wavelength: {params.wl_start:g} → {params.wl_stop:g} nm; {params.wl_step:g} nm per point\n"
                        f"{count:,} points × {len(rows)} enabled bias conditions; Vds {'enabled' if params.use_vds else 'off'}")
                for i, row in enumerate(rows[:8], 1):
                    text += f"\nC{i}: Vtg={row.vtg:g}, Vbg={row.vbg:g}, Vds={row.vds:g} V; settle {row.settle_s:g} s"
                return text
            if mode == 5:
                from app.engine.bfield_transport_sweep import TransportSweepPlan
                plan = TransportSweepPlan.from_params(params)
                return (f"APS100: {params.start_field_t:+g} → {params.stop_field_t:+g} T; {params.rate_t_per_min:g} T/min\n"
                        f"{len(plan.conditions)} enabled fixed-bias conditions; {'round trip' if params.round_trip else 'one way'}\n"
                        f"Cooldown: {params.cooldown_policy}; success: Driven (heater ON); stop/error: Persistent")
            from app.workers.cosweep import sequence_point_count
            from app.gate_transform import ratio_formula_text, derived_to_gates
            ranges = {"Vtg": (params.vtg_start, params.vtg_stop, params.vtg_step),
                      "Vbg": (params.vbg_start, params.vbg_stop, params.vbg_step),
                      "Vds": (params.vds_start, params.vds_stop, params.vds_step),
                      "Doping": (params.doping_start, params.doping_stop, params.doping_step),
                      "E-field": (params.efield_start, params.efield_stop, params.efield_step)}
            axes = [params.axis_fast] + ([params.axis_slow] if params.axis_slow != "None" else [])
            count = math.prod(sequence_point_count(*ranges[a]) for a in axes)
            text = f"{params.coordinate_mode} {'2D serpentine map' if len(axes) == 2 else '1D sweep'}: fast {params.axis_fast}, slow {params.axis_slow}"
            for axis in axes:
                start, stop, step = ranges[axis]
                text += f"\n{axis}: {start:g} → {stop:g}; step {step:g}"
            if count <= 10000:
                from app.workers.cosweep_timing import estimate_cosweep_seconds, format_cosweep_duration
                points = build_cosweep_points(params)
                count = len(points)
                text += f"\n{count:,} points; estimated duration {format_cosweep_duration(estimate_cosweep_seconds(params, points=points, connections=self.conns, device_id=self.save.device_id))}"
                bounds = coordinate_ranges(points)
                for axis in ("Vtg", "Vbg", "Vds"):
                    if axis in bounds:
                        text += f"\nPhysical {axis}: {bounds[axis][0]:g} … {bounds[axis][1]:g} V"
            else:
                text += f"\n{count:,} planned points; acquisition delay alone ≥{count*params.delay:g} s"
            if params.coordinate_mode == "Derived":
                text += "\n" + ratio_formula_text(params.ratio_target)
            if params.regions:
                text += f"\n{len(params.regions)} additional raw regions; merged duplicate coordinates"
            return text
        except Exception as exc:
            return "Trajectory needs attention: " + str(exc)

    def _load_settings(self):
        if self._settings is None:
            return
        import json
        for i, (key, _, _) in enumerate(MODES):
            self._values[i] = legacy_values(self._settings, i)
            saved = self._settings.value(f"quick/measurements/{key}", "")
            try:
                values = json.loads(saved) if isinstance(saved, str) and saved else {}
                for name, value in values.items():
                    if name in self._values[i]:
                        self._values[i][name] = convert(value, defaults(i)[name], name, i)
            except (TypeError, ValueError):
                pass

    def _save_settings(self):
        if self._settings is not None:
            import json
            self._settings.setValue(f"quick/measurements/{MODES[self._mode][0]}", json.dumps(self._values[self._mode]))

    @Slot(str, "QVariant")
    def setValue(self, key, value):
        if self.active:
            self._fail("Parameters are locked until cleanup finishes")
            return
        try:
            original = defaults(self._mode)
            if key not in original or key.startswith("output_"):
                raise ValueError("Unknown parameter: " + key)
            self._values[self._mode][key] = convert(value, original[key], key, self._mode)
            self._field_errors[self._mode].pop(("", -1, key), None)
            if self._mode == 2 and key == "coordinate_mode":
                # Coordinate selection must present executable axis choices.
                values = self._values[2]
                derived = values[key] == "Derived"
                allowed = {"Doping", "E-field", "Vds"} if derived else {"Vtg", "Vbg", "Vds"}
                if values["axis_fast"] not in allowed:
                    values["axis_fast"] = "Doping" if derived else "Vtg"
                if values["axis_slow"] not in allowed and (derived or values["axis_slow"] != "None"):
                    values["axis_slow"] = "E-field" if derived else "Vbg"
                if values["axis_slow"] == values["axis_fast"]:
                    values["axis_slow"] = next(a for a in ("E-field", "Doping", "Vds") if a != values["axis_fast"]) if derived else "None"
            if self._mode == 2 and key in {"coordinate_mode", "axis_fast", "axis_slow", "vds_start"}:
                values = self._values[2]
                if "Vds" not in (values["axis_fast"], values["axis_slow"]):
                    values["vds_stop"] = values["vds_start"]
            self._confirmation_granted = None
            self._save_settings()
            self.fieldsChanged.emit()
            self.changed.emit()
        except (TypeError, ValueError, OverflowError) as exc:
            if key in self._values[self._mode] and not key.startswith("output_"):
                self._field_errors[self._mode][("", -1, key)] = str(exc)
                self.fieldsChanged.emit()
            self._fail(str(exc))

    @Slot(str, int, str, "QVariant")
    def setRowValue(self, key, row, column, value):
        if self.active:
            self._fail("Parameters are locked until cleanup finishes")
            return
        try:
            if self._mode == 5 and key == "conditions" and column in {"ratio", "ratio_target", "vtg", "vbg"}:
                raise ValueError("Gate setpoints use the shared ratio and are calculated from Doping / E-field")
            rows = deepcopy(self._values[self._mode][key])
            if not isinstance(rows, list) or not 0 <= row < len(rows):
                raise ValueError("Invalid condition row")
            default = row_default(key, self._mode)
            if column not in default:
                raise ValueError("Unknown condition column")
            rows[row][column] = convert(value, default[column], column, self._mode)
            self._field_errors[self._mode].pop((key, row, column), None)
            self.setValue(key, rows)
        except (KeyError, TypeError, ValueError) as exc:
            rows = self._values[self._mode].get(key)
            if isinstance(rows, list) and 0 <= row < len(rows) and column in rows[row]:
                self._field_errors[self._mode][(key, row, column)] = str(exc)
                self.fieldsChanged.emit()
            self._fail(str(exc))

    @Slot(str)
    def addRow(self, key):
        try:
            rows = deepcopy(self._values[self._mode][key])
            if not isinstance(rows, list):
                raise ValueError("Parameter is not a row table")
            if self._mode == 4 and key == "conditions" and not rows:
                seed = row_default(key, 4)
                seed.update({k: deepcopy(v) for k, v in self._values[4].items() if k in seed})
                rows.append(seed)
            else:
                rows.append(row_default(key, self._mode))
            if not self.active:
                self._selected_rows[self._mode][key] = len(rows)-1
            self.setValue(key, rows)
        except (KeyError, ValueError) as exc:
            self._fail(str(exc))

    @Slot(str, int)
    def removeRow(self, key, row):
        try:
            rows = deepcopy(self._values[self._mode][key])
            if not isinstance(rows, list) or not 0 <= row < len(rows):
                raise ValueError("Invalid condition row")
            rows.pop(row)
            if not self.active:
                self._shift_row_errors(key, row, removing=True)
                self._selected_rows[self._mode][key] = max(0, min(row, len(rows)-1))
            self.setValue(key, rows)
        except (KeyError, ValueError) as exc:
            self._fail(str(exc))

    @Slot(str, int)
    def selectRow(self, key, row):
        rows = self._values[self._mode].get(key)
        if isinstance(rows, list) and 0 <= row < len(rows):
            self._selected_rows[self._mode][key] = row
            self.fieldsChanged.emit()

    @Slot(str, int)
    def duplicateRow(self, key, row):
        try:
            rows = deepcopy(self._values[self._mode][key])
            if not isinstance(rows, list) or not 0 <= row < len(rows):
                raise ValueError("Invalid condition row")
            if len(rows) >= 100:
                raise ValueError("At most 100 conditions or regions are allowed")
            duplicate = deepcopy(rows[row])
            if "name" in duplicate:
                duplicate["name"] += " copy"
            rows.insert(row+1, duplicate)
            if not self.active:
                self._shift_row_errors(key, row+1)
                self._selected_rows[self._mode][key] = row+1
            self.setValue(key, rows)
        except (KeyError, TypeError, ValueError) as exc:
            self._fail(str(exc))

    def _shift_row_errors(self, key, index, *, removing=False):
        """Keep rejection context attached to its existing recipe after edits."""
        errors = {}
        for (table, row, column), message in self._field_errors[self._mode].items():
            if table == key:
                if removing and row == index:
                    continue
                if row >= index:
                    row += -1 if removing else 1
            errors[(table, row, column)] = message
        self._field_errors[self._mode] = errors

    def _fail(self, message):
        self._message = str(message)
        if not self.active:
            self._phase = "error"
        self.append_log(str(message))
        self.errorRaised.emit(str(message))
        self.changed.emit()

    def append_log(self, message):
        self._logs.append(str(message))
        self._logs = self._logs[-1000:]
        if self._log_path:
            try:
                with open(self._log_path, "a", encoding="utf-8") as handle:
                    handle.write(str(message).rstrip() + "\n")
            except OSError:
                pass
        self.changed.emit()

    def _rates(self, capture=True):
        if self._engine_calibration is not None:
            return self._engine_calibration
        rates = tuple(float(v) for v in self.get_global_rates())
        if len(rates) != 2 or any(not math.isfinite(v) or v <= 0 for v in rates):
            raise ValueError("Signal-chain rates must be finite and positive")
        chain = self.get_signal_chain()
        if capture:
            chain = self.capture_run_signal_chain(chain)
        meta = signal_chain_metadata(chain)
        if (meta.get("drag_drive") or {}).get("enabled"):
            rates = rates[0], 10. / meta["drag_drive"]["drag_sensitivity_v"]
        return *rates, chain

    def capture_run_signal_chain(self, chain):
        if self._engine_calibration is not None:
            return self._engine_calibration[2]
        return capture_run_signal_chain(chain, self.device_manager,
            owns_lockin="lockin" in self._claimed,
            context=self._context_provider() if callable(self._context_provider) else None)

    def collect_params(self):
        return build_params(self._values[self._mode], self._mode)

    def _trajectory(self, params, mode):
        if mode == 0:
            if params.vds_step <= 0 or params.vds_ramp <= 0:
                raise ValueError("Vds sample and ramp steps must be positive")
            count = math.ceil(abs(params.vds_stop - params.vds_start) / params.vds_step) + 1
            if count > 250000:
                raise ValueError("The trajectory exceeds 250,000 points")
            step = params.vds_step if params.vds_stop >= params.vds_start else -params.vds_step
            sequence = _frange_inc(params.vds_start, params.vds_stop, step)
            return [{"vtg": params.vtg_set, "vbg": params.vbg_set, "vds": v} for v in sequence]
        if mode in (1, 4):
            if not 1 <= params.n_points <= 250000:
                raise ValueError("Number of points must be between 1 and 250,000")
            if params.mode == "Raw" and not any((params.raw_vtg_active, params.raw_vbg_active, params.raw_vds_active)):
                raise ValueError("Raw trajectory requires at least one active variable")
            worker = LineSweepWorker(params, self.save, self.conns)
            points = worker._build_trajectory()
            worker._validate_trajectory_limits(points)
            return points
        if mode == 2:
            validate_cosweep_params(params)
            return build_cosweep_points(params)
        if mode == 3:
            if params.wl_step <= 0 or not (0 < params.wl_start <= 1500 and 0 < params.wl_stop <= 1500):
                raise ValueError("Wavelength must be in (0, 1500] nm with a positive step")
            if math.ceil(abs(params.wl_stop-params.wl_start)/params.wl_step)+1 > 250000:
                raise ValueError("The wavelength trajectory exceeds 250,000 points")
            conditions = [c for c in params.bias_conditions if c.enabled]
            if not conditions:
                raise ValueError("Enable at least one photocurrent bias condition")
            if params.use_vds and params.vds_source == "None":
                raise ValueError("Select a Vds source or disable Apply Vds")
            if any(c.settle_s < 0 for c in conditions):
                raise ValueError("Condition settling time must be non-negative")
            return [{"vtg": c.vtg, "vbg": c.vbg, "vds": c.vds if params.use_vds else 0.} for c in conditions]
        return []

    def _validate(self, params, mode):
        for key, value in asdict(params).items():
            if isinstance(value, float) and not math.isfinite(value):
                raise ValueError(f"{key} must be finite")
        for name in ("delay", "acquisition_delay_s"):
            if hasattr(params, name) and getattr(params, name) < 0:
                raise ValueError("Acquisition delay must be non-negative")
        for name in ("vg_ramp", "vds_ramp"):
            if hasattr(params, name) and getattr(params, name) <= 0:
                raise ValueError("Ramp step must be positive")
        averages = getattr(params, "n_sample", getattr(params, "averages", 1))
        if not 1 <= averages <= 1000:
            raise ValueError("Averages must be between 1 and 1000")
        if getattr(params, "ao_channel", 0) < 0:
            raise ValueError("AO channel must be non-negative")
        points = self._trajectory(params, mode)
        required = ["daq"] + (["mono"] if mode == 3 else [])
        # Existing gate sessions must enforce fixed zero setpoints too: a
        # previous bias can remain even after a failed reconnect. The checks
        # below reject retained sessions that are no longer healthy/verified.
        # Disconnected fixed-zero gates stay
        # optional except for LineSweepWorker and explicitly swept map axes.
        for device, axis in (("g1", "vtg"), ("g2", "vbg")):
            swept_map_gate = mode == 2 and (params.coordinate_mode == "Derived" or
                {"vtg": "Vtg", "vbg": "Vbg"}[axis] in (params.axis_fast, params.axis_slow))
            if (mode in (1, 4) or swept_map_gate or self.device_manager.get_session(device) is not None
                    or any(abs(p[axis]) > 1e-12 for p in points)):
                required.append(device)
        use_vds = mode != 3 or params.use_vds
        if use_vds and params.vds_source == "Keithley 2400":
            required.append("g3")
        if use_vds and params.vds_source not in ("Keithley 2400", "NI DAQ AO"):
            raise ValueError("Unsupported Vds source")
        missing = [d for d in required if not self.device_manager.is_connected(d)]
        if missing:
            raise ValueError("Connect required devices first: " + ", ".join(missing))
        for device, axis in (("g1", "vtg"), ("g2", "vbg"), ("g3", "vds")):
            if device not in required:
                continue
            if not self.device_manager.is_voltage_source_mode(device):
                raise ValueError(device.upper() + " must use 2-wire voltage-source mode")
            if not self.device_manager.protection_is_applied(device):
                raise ValueError(device.upper() + " protection must be applied and verified")
            limit = min(V_LIMIT, self.device_manager.applied_gate_voltage_limit(device))
            if not math.isfinite(limit) or limit <= 0:
                raise ValueError(device.upper() + " voltage protection is unverified")
            if any(abs(float(p[axis])) > limit + 1e-9 for p in points):
                raise ValueError(f"{device.upper()} trajectory exceeds verified {limit:g} V protection")
        for point in points:
            if any(not math.isfinite(float(v)) or abs(float(v)) > V_LIMIT for k, v in point.items() if k in ("vtg", "vbg", "vds")):
                raise ValueError(f"Requested bias exceeds ±{V_LIMIT:g} V")
        if use_vds and params.vds_source == "NI DAQ AO":
            daq = self.device_manager.get_session("daq")
            if f"ao{params.ao_channel}" not in self.device_manager.get_ao_items():
                raise ValueError("Selected DAQ AO channel is unavailable")
            limit = float(daq.get_max_output(params.ao_channel))
            if not math.isfinite(limit) or any(abs(p["vds"]) > abs(limit) + 1e-9 for p in points):
                raise ValueError("Vds exceeds the DAQ AO range")
        if mode == 2:
            resolutions = {}
            refresh = []
            for device, axis in (("g1", "vtg"), ("g2", "vbg"), ("g3", "vds")):
                if device not in required:
                    continue
                resolution = source_resolution_status(self.device_manager.get_session(device))
                if resolution.quantum is None:
                    if resolution.refreshable:
                        refresh.append(device)
                    else:
                        raise ValueError(f"{device.upper()}: {resolution.message}")
                else:
                    resolutions[axis] = resolution.quantum
            if refresh:
                if self.device_manager.read_gate_currents(names=tuple(refresh)):
                    raise ValueError("Refreshing source ranges. Review readback and press Start again.")
                raise ValueError("Source ranges unavailable; refresh readback before starting")
            validate_voltage_points(points, resolutions)
        return required, points

    def _claim(self, required):
        devices = list(required)
        if self.device_manager.is_connected("lockin"):
            devices.append("lockin")
        if self.conns.drag_drive_enabled:
            devices.append("lockin_drive")
        devices = list(dict.fromkeys(devices))
        claimed, blocked = self.device_manager.mark_in_use(devices)
        if not claimed:
            raise ValueError("Devices already in use or protection unverified: " + ", ".join(blocked))
        self._claimed = devices

    def _release(self):
        if self._claimed:
            self.device_manager.release(self._claimed)
            self._claimed = []

    def _plan(self, params, mode, chain):
        if mode == 0:
            parts = dual_gate_filename_parts(params, chain)
        elif mode in (1, 4):
            parts = gate_scan_filename_parts(params, chain)
        elif mode == 2:
            parts = map_filename_parts(params, chain)
        else:
            from app.signal_chain import signal_chain_filename_parts
            parts = [f"WL{params.wl_start:g}to{params.wl_stop:g}nm", *signal_chain_filename_parts(chain)]
        measurement = ("map_2d" if params.axis_slow != "None" else "sweep_1d") if mode == 2 else "gate_scan" if mode == 4 else MODES[mode][0]
        planned = unique_planned_output(build_planned_output(self.save, measurement, params.base_name, parts))
        reason = output_blocking_reason(planned, self.save)
        if reason:
            raise ValueError(reason)
        if mode == 3:
            paths = PhotocurrentWorker.condition_csv_paths(planned.csv_path,
                [c for c in params.bias_conditions if c.enabled], params.use_vds)
            if any(os.path.exists(p) for p in paths):
                raise ValueError("Photocurrent condition output already exists")
        params.output_csv_path = planned.csv_path
        params.output_metadata_path = planned.metadata_path
        params.output_log_path = planned.log_path
        self._planned_output = planned
        return planned

    @Slot()
    def start(self):
        if self.active:
            self._fail("A measurement or cleanup is already in progress")
            return
        if self._field_errors[self._mode]:
            self._fail("Correct invalid fields before starting the measurement")
            return
        if self.device_manager.is_busy():
            self._fail("Wait for the current hardware operation to finish")
            return
        try:
            ready = self._temperature_ready()
            if ready is not True:
                raise ValueError(str(ready or "Sample temperature is not ready"))
            if self._mode >= 4:
                self._start_magnetic()
                return
            params = self.collect_params()
            required, points = self._validate(params, self._mode)
            self._begin_preflight(params, self._mode, required, points)
        except Exception as exc:
            if self.worker_thread is None:
                self._release()
            self._fail(str(exc))

    def _begin_preflight(self, params, mode, required, points):
        self._claim(required)
        self._engine_calibration = None
        self._preflight_request = params, mode, points
        task = PreflightThread(self._rates, self)
        self._preflight_thread = task
        task.finished.connect(self._preflight_finished)
        self._phase, self._message, self._progress = "preparing", "Verifying signal-chain settings", 0.
        self.changed.emit()
        task.start()

    @Slot()
    def _preflight_finished(self):
        task = self.sender()
        if task is not self._preflight_thread:
            return
        params, mode, points = self._preflight_request
        self._preflight_thread = self._preflight_request = None
        task.deleteLater()
        if task.cancel.is_set():
            self._release()
            self._phase, self._message = "stopped", "Stopped after the in-flight preflight read completed"
            self.changed.emit()
            return
        if task.error or task.result is None:
            self._release()
            self._fail(task.error or "Signal-chain verification returned no result")
            return
        try:
            calibration = task.result
            if mode >= 4:
                self._engine_calibration = calibration
                # Engines acquire their own complete reservation synchronously
                # on this same GUI turn, using the captured immutable settings.
                self._release()
                self._dispatch_magnetic()
            else:
                planned = self._plan(params, mode, calibration[2])
                self._run_snapshot = self._snapshot(params, mode, points, planned, calibration[2])
                self._launch(params, mode, calibration, self._run_snapshot)
        except Exception as exc:
            self._release()
            self._fail(str(exc))
        self.changed.emit()

    def _snapshot(self, params, mode, points, planned, signal_chain=None):
        axis = "Vds" if mode == 0 else "Wavelength" if mode == 3 else getattr(params, "plot_x_resolved", "Vtg")
        if mode in (1, 4):
            from app.plot_x_axis import resolve_gate_scan_x_axis
            axis = resolve_gate_scan_x_axis(params.plot_x_axis, params.mode, params.derived_axis,
                params.raw_vtg_active, params.raw_vbg_active, params.raw_vds_active)
        elif mode == 2:
            from app.plot_x_axis import resolve_map_x_axis
            axis = resolve_map_x_axis(params.plot_x_axis, params.axis_fast)
        if hasattr(params, "plot_x_resolved"):
            params.plot_x_resolved = axis
        csv_paths = PhotocurrentWorker.condition_csv_paths(planned.csv_path,
            [c for c in params.bias_conditions if c.enabled], params.use_vds) if mode == 3 else [planned.csv_path]
        if mode == 0:
            ranges = {"Vds": (min(point["vds"] for point in points), max(point["vds"] for point in points))}
        elif mode == 3:
            ranges = {"Wavelength": (min(params.wl_start, params.wl_stop), max(params.wl_start, params.wl_stop))}
        else:
            total = len(points) * (2 if getattr(params, "sweep_both_ways", False) else 1)
            ranges = coordinate_ranges(points, total_count=total)
        measurement = ("map_2d" if params.axis_slow != "None" else "sweep_1d") if mode == 2 else MODES[mode][0]
        return {"kind": "run", "measurement": measurement, "mode": MODES[mode][0], "title": MODES[mode][1], "params": asdict(params),
                "csv_path": planned.csv_path, "metadata_path": planned.metadata_path,
                "csv_paths": csv_paths,
                "log_path": planned.log_path, "x_axis": axis,
                "plot_choice": params.plot_choice, "planned_ranges": ranges,
                "channels": self._plot_channels(params.vds_source, signal_chain),
                "signal_chain": signal_chain_metadata(signal_chain if signal_chain is not None else self.get_signal_chain())}

    def _plot_channels(self, source, chain=None):
        from app.result_channels import plot_channel_options
        metadata = signal_chain_metadata(chain if chain is not None else self.get_signal_chain())
        return plot_channel_options(source, bool((metadata.get("drag_drive") or {}).get("enabled")))

    def _launch(self, params, mode, calibration, snapshot, *, batch_metadata=None):
        amp, lockin, chain = calibration
        worker = self._factories[mode](params, deepcopy(self.save), deepcopy(self.conns),
            **{d: self.device_manager.get_session(d) if d in self._claimed else None for d in ("g1", "g2", "g3", "daq", "mono")},
            plot_choice=params.plot_choice, amp_rate=amp, lkn_rate=lockin,
            signal_chain=signal_chain_metadata(chain), batch_metadata=batch_metadata or {},
            apply_connected_gate_biases=True)
        thread = MeasurementThread(worker, self)
        self._generation += 1
        self._record_index = 0
        self._terminal = None
        self._cleanup_warning = ""
        worker.moveToThread(thread)
        relay = WorkerRelay(self._generation, self)
        self._relay = relay
        relay.event.connect(self._worker_event)
        worker.point_data.connect(relay.record)
        worker.status.connect(relay.status)
        worker.log.connect(relay.log)
        worker.progress.connect(relay.progress)
        worker.finished.connect(relay.finished)
        worker.stopped.connect(relay.stopped)
        worker.error.connect(relay.error)
        thread.finished.connect(self._thread_finished)
        thread.finished.connect(worker.deleteLater)
        planned = self._planned_output
        os.makedirs(planned.output_dir, exist_ok=True)
        with open(planned.log_path, "x", encoding="utf-8") as handle:
            handle.write(f"Run: {MODES[mode][1]}\nStarted: {datetime.now().isoformat()}\nCSV: {planned.csv_path}\n")
        self.worker, self.worker_thread = worker, thread
        self._log_path = planned.log_path
        self._phase, self._message, self._progress = "preparing", "Starting measurement", 0.
        self.runPrepared.emit(snapshot)
        self.changed.emit()
        thread.start()

    def _is_current_worker(self):
        return self.worker is not None and self.sender() is self.worker

    @Slot(int, str, object)
    def _worker_event(self, generation, kind, value):
        if generation != self._generation or self.worker_thread is None:
            return
        if kind == "record":
            record = dict(value)
            for lower, upper in (("vtg", "Vtg"), ("vbg", "Vbg"), ("vds", "Vds"), ("doping", "Doping"), ("efield", "E-field")):
                if lower not in record and upper in record:
                    record[lower] = record[upper]
            if self._mode == 0:
                params = self._run_snapshot["params"]
                record.update(vtg=params["vtg_set"], vbg=params["vbg_set"], vds=record.get("x"))
            record.setdefault("index", self._record_index)
            self._record_index += 1
            self.recordReady.emit(record)
        elif kind == "status":
            if self._phase != "stopping":
                self._phase, self._message = "running", str(value)
        elif kind == "log":
            if str(value).lower().startswith("safe-state warning:"):
                self._cleanup_warning = str(value)
            self.append_log(value)
        elif kind == "progress":
            self.set_progress(value)
        elif kind in ("finished", "stopped", "error"):
            self._terminal = kind, str(value)
            self._phase, self._message = "cleanup", "Waiting for safe zeroing and output flush"
        self.changed.emit()

    @Slot(object)
    def _on_record(self, record):
        if self._is_current_worker():
            self.recordReady.emit(dict(record))

    @Slot(str)
    def _on_status(self, message):
        if self._is_current_worker() and self._phase != "stopping":
            self._phase, self._message = "running", message
            self.changed.emit()

    @Slot(str)
    def _on_log(self, message):
        if self._is_current_worker():
            if str(message).lower().startswith("safe-state warning:"):
                self._cleanup_warning = str(message)
            self.append_log(message)

    @Slot(float)
    def _on_progress(self, progress):
        if self._is_current_worker():
            self.set_progress(progress)

    @Slot(str)
    def _on_finished(self, detail):
        self._terminal_received("finished", detail)

    @Slot(str)
    def _on_stopped(self, detail):
        self._terminal_received("stopped", detail)

    @Slot(str)
    def _on_error(self, detail):
        self._terminal_received("error", detail)

    def _terminal_received(self, phase, detail):
        if self._is_current_worker():
            self._terminal = phase, detail
            self._phase, self._message = "cleanup", "Waiting for safe zeroing and output flush"
            self.changed.emit()

    @Slot()
    def _thread_finished(self):
        thread = self.sender()
        if thread is not self.worker_thread:
            return
        terminal = thread.terminal or self._terminal or ("error", thread.unhandled_error or "Worker returned without a terminal result")
        if thread.unhandled_error or self._cleanup_warning:
            terminal = "error", thread.unhandled_error or self._cleanup_warning
        self.worker, self.worker_thread = None, None
        self._relay.deleteLater()
        thread.deleteLater()
        self._phase, self._message = terminal
        self.append_log(f"Status: {terminal[0]} — {terminal[1]}")
        self._log_path = ""
        if self._batch_claimed:
            self._batch_snapshots.append({**(self._run_snapshot or {}), "status": terminal[0], "detail": terminal[1]})
            self.batch_run_terminal.emit(*terminal)
        else:
            self._release()
            self.runCompleted.emit({**(self._run_snapshot or {}), "status": terminal[0], "detail": terminal[1]})
            if terminal[0] == "error":
                self.errorRaised.emit(terminal[1])
        self.changed.emit()

    @Slot()
    def stop(self):
        if self._preflight_thread is not None:
            self._preflight_thread.cancel.set()
            self._phase, self._message = "stopping", "Waiting for the in-flight preflight read to finish"
        elif self._confirmation is not None:
            self._confirmation = self._confirmation_granted = None
            self._phase, self._message = "stopped", "Confirmation cancelled"
        elif self._transport and self._transport.active:
            self._transport.stop()
        elif self._batch and self._batch.active:
            self._batch.stop()
        else:
            self.stop_run()
        self.changed.emit()

    def stop_run(self):
        if self.worker is not None:
            self._phase, self._message = "stopping", "Stop requested; waiting for safe cleanup"
            self.worker.request_stop()
            self.changed.emit()

    def set_progress(self, value):
        self._progress = min(1., max(0., float(value)))
        self.changed.emit()

    def _start_magnetic(self):
        if self._batch is None or self._transport is None:
            raise ValueError("APS100 controller is unavailable")
        if self._selected_backend() != "1000":
            raise ValueError("B-field measurements require attoDRY1000 / APS100")
        if not self._review_valid_callback():
            raise ValueError("APS100 commissioning review is required")
        thermal = self._batch.thermal_safety
        if thermal is None or not bool(getattr(thermal, "is_armed", getattr(thermal, "armed", False))):
            raise ValueError("A commissioned, armed Lake Shore thermal evaluator is required")
        snapshot = getattr(self._magnet, "latest_snapshot", None)
        if snapshot is not None and getattr(snapshot, "heater_on", None) is False and not self.confirm_persistent_field(snapshot):
            import uuid
            token = uuid.uuid4().hex
            self._confirmation = (token, float(snapshot.field_t), self._mode)
            self._phase, self._message = "confirmation", "Confirm stored APS100 field and polarity"
            self.confirmationRequested.emit(token, "Confirm stored APS100 field",
                f"Heater is OFF; stored field is {float(snapshot.field_t):+.6f} T. Confirm field and polarity before matching supply leads.")
            self.changed.emit()
            return
        params = self.collect_params()
        if self._mode == 4:
            required = []
            rows = self._values[4]["conditions"]
            configurations = [build_params({**defaults(4), **{k: v for k, v in row.items() if k not in ("enabled", "name")}}, 4)
                              for row in rows if row["enabled"]] if rows else [params]
            if not configurations:
                raise ValueError("Enable at least one Gate Scan condition")
            for config in configurations:
                devices, _ = self._validate(config, 4)
                required.extend(devices)
            self._batch.parse_fields(self._values[4]["fields_t"])
        else:
            from app.engine.bfield_transport_sweep import TransportSweepPlan
            plan = TransportSweepPlan.from_params(params)
            required = ["daq", "g1", "g2"]
            if any(c.vds_source == "Keithley 2400" for c in plan.conditions):
                required.append("g3")
        self._begin_preflight(params, self._mode, list(dict.fromkeys(required)), [])

    def _dispatch_magnetic(self):
        if self._mode == 4:
            self._batch_snapshots = []
            if not self._batch.start(self._values[4]["fields_t"], resume_checkpoint=self._values[4]["resume_checkpoint"] or None):
                self._confirmation_granted = None
        else:
            self._transport.start(verified_calibration=self._engine_calibration)
            if self._transport.active:
                params = self._transport.plan.params
                self._run_snapshot = {"kind": "run", "measurement": MODES[5][0], "mode": MODES[5][0], "title": MODES[5][1], "params": to_jsonable(params),
                    "csv_paths": list(self._transport._transport_outputs.condition_csv_paths),
                    "manifest_path": self._transport._manifest, "metadata_path": self._transport._manifest, "x_axis": "B-field", "plot_choice": self._values[5]["plot_choice"],
                    "channels": self._plot_channels("Keithley 2400", self._engine_calibration[2]),
                    "planned_ranges": {"B-field": (min(params.start_field_t, params.stop_field_t), max(params.start_field_t, params.stop_field_t))}}
                self.runPrepared.emit(self._run_snapshot)
        self.changed.emit()

    @Slot(str, bool)
    def answerConfirmation(self, token, accepted):
        request = self._confirmation
        if request is None or request[0] != token:
            return
        self._confirmation = None
        if accepted:
            self._confirmation_granted = request[1:]
            self.start()
        else:
            self._phase, self._message = "stopped", "Stored persistent field confirmation declined"
            self.changed.emit()

    def confirm_persistent_field(self, snapshot):
        return self._confirmation_granted == (float(snapshot.field_t), self._mode)

    def transport_review_valid(self):
        return self._review_valid_callback()

    def transport_backend(self):
        return self._selected_backend()

    def freeze_output_plan(self, params):
        from app.engine.bfield_transport_sweep import TransportSweepPlan, transport_output_summary_parts, build_transport_output_paths
        plan = TransportSweepPlan.from_params(params)
        planned = unique_planned_output(build_planned_output(self.save, "bfield_transport", params.base_name,
            transport_output_summary_parts(params, self.get_signal_chain())))
        self._transport_outputs = build_transport_output_paths(planned, plan.conditions)
        return self._transport_outputs

    def validate_transport_output_ready(self, outputs):
        reason = output_blocking_reason(outputs.planned, self.save)
        if reason:
            raise ValueError(reason)
        if any(os.path.exists(path) for path in outputs.all_paths):
            raise ValueError("A B-field output path already exists")
        os.makedirs(outputs.planned.output_dir, exist_ok=True)

    def set_sweep_locked(self, locked):
        self.changed.emit()

    def accept_transport_record(self, record):
        self.recordReady.emit({**record, "x": record["B_measured_T"], "B-field": record["B_measured_T"],
            "direction": record.get("Direction", ""), "condition": record.get("Condition_index"),
            "index": record.get("Index"), "vtg": record.get("Vtg"), "vbg": record.get("Vbg"),
            "vds": record.get("Vds"), "doping": record.get("Doping"), "efield": record.get("E-field")})

    def capture_field_batch_requests(self):
        rows = self._values[4]["conditions"]
        configs = [(build_params({**defaults(4), **{k: v for k, v in row.items() if k not in ("enabled", "name")}}, 4), row["name"])
                   for row in rows if row["enabled"]] if rows else [(self.collect_params(), "Condition 1")]
        captured = []
        for params, name in configs:
            required, _ = self._validate(params, 4)
            calibration = self._rates(capture=False)
            self._plan(params, 4, calibration[2])
            captured.append((params, tuple(required), calibration, name))
        return captured

    def validate_field_batch_request(self, params):
        self._validate(params, 4)

    def begin_field_batch(self, params, required_devices, calibration=None):
        try:
            self._claim(required_devices)
            self._batch_params = deepcopy(params)
            base = calibration or self._rates(capture=False)
            self._batch_calibration = (*base[:2], self.capture_run_signal_chain(base[2]))
            self._batch_claimed = True
            self.changed.emit()
            return True
        except Exception as exc:
            self._release()
            self._fail(str(exc))
            return False

    def set_field_batch_condition(self, params):
        self._batch_params = deepcopy(params)

    def start_field_batch_measurement(self, planned, metadata):
        if not self._batch_claimed or self.worker_thread is not None:
            return False
        try:
            params = deepcopy(self._batch_params)
            params.output_csv_path, params.output_metadata_path, params.output_log_path = planned.csv_path, planned.metadata_path, planned.log_path
            self._planned_output = planned
            _, points = self._validate(params, 4)
            snapshot = self._snapshot(params, 4, points, planned, self._batch_calibration[2])
            self._run_snapshot = snapshot
            self._launch(params, 4, self._batch_calibration, snapshot, batch_metadata=metadata)
            return True
        except Exception as exc:
            self._fail(str(exc))
            return False

    def finish_field_batch(self):
        self._release()
        self._batch_claimed = False
        self._batch_params = self._batch_calibration = None
        self.changed.emit()

    def set_batch_locked(self, locked):
        self.changed.emit()

    @Slot(str, str)
    def _engine_state(self, phase, message):
        self._phase, self._message = phase, message
        self.append_log(message)
        if phase in ("failed", "stopped", "complete", "finished") and not self.active:
            self._confirmation_granted = None
            if phase == "failed" and self.sender() is self._transport:
                self.runCompleted.emit({**(self._run_snapshot or {}), "status": "error", "detail": message})
        self.changed.emit()

    @Slot(str, str)
    def _batch_finished(self, status, detail):
        self._phase, self._message = status, detail
        self._confirmation_granted = None
        self._engine_calibration = None
        self.runCompleted.emit({"kind": "run", "measurement": "bfield_gate_scan", "mode": "bfield_gate_scan",
            "status": status, "detail": detail, "runs": list(self._batch_snapshots),
            "csv_paths": [path for run in self._batch_snapshots for path in run.get("csv_paths", [])]})
        self.changed.emit()

    @Slot(int, int, str)
    def _engine_progress(self, index, count, detail):
        self._progress = min(1., max(0., index / max(count, 1)))
        self._message = detail
        self.changed.emit()

    @Slot(str)
    def _engine_error(self, message):
        self._fail(message)

    @Slot()
    def _engine_finished(self):
        self._phase, self._message, self._progress = "finished", "Measurement complete", 1.
        self._confirmation_granted = None
        self._engine_calibration = None
        self.runCompleted.emit({**(self._run_snapshot or {}), "status": "finished", "detail": self._message})
        self.changed.emit()

    @Slot(str)
    def _engine_stopped(self, detail):
        self._phase, self._message = "stopped", detail
        self._confirmation_granted = None
        self._engine_calibration = None
        self.runCompleted.emit({**(self._run_snapshot or {}), "status": "stopped", "detail": detail})
        self.changed.emit()

    @Slot()
    def continueAfterTemperatureCheck(self):
        if not self._continuation[0] or not self._continuation[1]:
            self._fail("Continue is unavailable until thermal and pause checks pass")
            return
        engine = self._batch if self._mode == 4 else self._transport
        if engine:
            engine.continue_after_temperature_check()

    @Slot(bool, bool, str)
    def _on_continuation(self, required, ready, reason):
        self._continuation = required, ready, reason
        self.changed.emit()

    @Slot(str)
    def _thermal_fault(self, message):
        if self._preflight_thread is not None:
            self._preflight_thread.cancel.set()
        if self._transport is not None:
            self._transport._on_fault(str(message))
        if self._batch is not None:
            self._batch._abort_batch("Lake Shore thermal fault: " + str(message))
        self._fail("Lake Shore thermal fault: " + str(message))

    @Slot(str)
    def _thermal_error(self, message):
        if self._preflight_thread is not None:
            self._preflight_thread.cancel.set()
        if self._transport is not None:
            self._transport.on_lakeshore_disconnected()
        if self._batch is not None:
            self._batch.on_lakeshore_disconnected()

    def prepare_shutdown(self):
        self.stop()
        if self.worker_thread is not None or self._preflight_thread is not None or (self._batch and self._batch.active):
            return False
        return self._transport.prepare_shutdown() if self._transport else True
