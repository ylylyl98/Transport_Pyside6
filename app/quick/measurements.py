"""QObject acquisition facade. No QWidget or measurement-tab construction.

Workers retain sole responsibility for instrument motion, CSVs and safe zeroing.
Device ownership is released only after the worker's entire finally block returns.
The magnetic engines use this object as their UI-independent orchestration port.
"""
from __future__ import annotations

from copy import deepcopy
from app.condition_names import unique_condition_name
from dataclasses import asdict
from datetime import datetime
import math
import os

from PySide6.QtCore import QObject, Property, Signal, Slot, QTimer

from app.models import LineSweepParams, BFIELD_MT_FIELDS
from utils.config import cfg
from app.constants import V_LIMIT
from app.experiment_metadata import capture_run_signal_chain
from app.signal_chain import SignalChainSnapshot, signal_chain_metadata
from app.quick.output_preview import output_spec, preview_output
from app.run_output import (build_planned_output, unique_planned_output, output_blocking_reason,
                            to_jsonable)
from app.workers.dual_gate import DualGateWorker
from app.workers.line_sweep import LineSweepWorker
from app.workers.cosweep import CoSweepWorker, build_cosweep_points, validate_cosweep_params
from app.workers.photocurrent import PhotocurrentWorker
from app.voltage_resolution import source_resolution_status, validate_voltage_points
from app.plot_ranges import coordinate_ranges
from app.utils import _frange_inc
from app.gate_transform import derived_to_gates, gates_to_derived
from app.quick.sampling import SamplingPreview
from app.quick.sweep_preview import SweepPreview
from app.quick.parameters import MODES, defaults, schema, convert, row_default, build_params, legacy_values, measurement_sections, label, descriptor, normalize_transport_spacing
from app.quick.run_state import MeasurementThread, PreflightThread, WorkerRelay


class MeasurementService(QObject):
    changed = Signal()
    fieldsChanged = Signal()
    uiSectionsChanged = Signal()
    resolutionSummaryChanged = Signal()
    outputPreviewChanged = Signal()
    modeChanged = Signal()
    recordReady = Signal(object)
    runPrepared = Signal(object)
    runCompleted = Signal(object)
    errorRaised = Signal(str)
    confirmationRequested = Signal(str, str, str)
    shutdownReady = Signal()
    batch_run_terminal = Signal(str, str)

    def __init__(self, manager, save_root, get_global_rates=None, get_signal_chain=None,
                 parent=None, *, magnet1000=None, magnet2100=None, thermal_safety=None,
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
        self._selected_backend = selected_backend_callable or (lambda: cfg.setup.magnet_backend)
        self._review_valid_callback = review_valid_callable or (lambda: False)
        self._values = [defaults(i) for i in range(len(MODES))]
        self._recipe_models = {}
        self._sweep_preview = SweepPreview(self)
        self._sweep_preview_visible = False
        self._sweep_preview_pending = False
        self._sweep_preview_timer = QTimer(self)
        self._sweep_preview_timer.setSingleShot(True)
        self._sweep_preview_timer.setInterval(300)
        self._sweep_preview_timer.timeout.connect(self._update_sweep_preview)
        self._sweep_preview.changed.connect(self._sweep_preview_finished)
        self._sampling = SamplingPreview(self)
        self._sampling.updated.connect(self._sampling_changed)
        self._recipe_details_visible = False
        self._transport_coordinates = "Derived"
        self._selected_rows = [{} for _ in MODES]
        self._field_errors = [{} for _ in MODES]
        self._mode = 0
        self._mode_selection_rejected = False
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
        self._lakeshore = lakeshore_controller
        input_pending = getattr(lakeshore_controller, "input_configuration_pending_changed", None)
        if input_pending is not None:
            input_pending.connect(lambda *_: self.changed.emit())
        self._batch = self._transport = None
        self._settings = settings
        self._factories = {0: DualGateWorker, 1: LineSweepWorker, 2: CoSweepWorker, 3: PhotocurrentWorker, 4: LineSweepWorker}
        self._factories.update(worker_factories or {})
        self._load_settings()
        self._validate_2100_saved_options()
        self.fieldsChanged.connect(self.outputPreviewChanged)
        self.fieldsChanged.connect(self._invalidate_sweep_preview)
        self.fieldsChanged.connect(self.resolutionSummaryChanged)
        self.uiSectionsChanged.connect(self.resolutionSummaryChanged)
        for signal_name in ("status_changed", "operation_changed", "protection_changed", "gate_currents_read"):
            signal = getattr(manager, signal_name, None)
            if signal is not None:
                signal.connect(lambda *_: self.changed.emit())
                signal.connect(lambda *_: self.resolutionSummaryChanged.emit())
        if magnet1000 is not None:
            from app.engine.gate_scan_field_batch import GateScanFieldBatch
            self._batch = GateScanFieldBatch(magnet1000, self, self,
                selected_backend_callable=self._selected_backend, thermal_safety=thermal_safety,
                lakeshore_controller=lakeshore_controller)
            self._batch.set_review_validator(self._review_valid_callback)
            self._batch.measurement_temperature_provider = temperature_provider
        if magnet1000 is not None or magnet2100 is not None:
            from app.engine.bfield_transport_router import BFieldTransportRouter
            self._transport = BFieldTransportRouter(magnet1000, magnet2100, self, manager, thermal_safety, self,
                selected_backend_callable=self._selected_backend, review_valid_callable=self._review_valid_callback)
            self._transport.lakeshore_controller = lakeshore_controller
        for engine in (self._batch, self._transport):
            if engine is not None:
                engine.state_changed.connect(self._engine_state)
                engine.progress_changed.connect(self._engine_progress)
                engine.error.connect(self._engine_error)
                engine.continuation_changed.connect(self._on_continuation)
        if self._transport is not None:
            self._transport.finished.connect(self._engine_finished)
            self._transport.stopped.connect(self._engine_stopped)
            self._transport.shutdown_ready.connect(self.shutdownReady)
        if self._batch is not None:
            self._batch.cleanup_finished.connect(self._batch_finished)
            self._batch.activity.connect(lambda e: self.append_log(e.get("message", str(e))))
        if lakeshore_controller is not None:
            signal = getattr(lakeshore_controller, "snapshot_updated", None)
            if signal is not None:
                if self._transport is not None:
                    signal.connect(self._transport.on_lakeshore_snapshot)
                if self._batch is not None:
                    signal.connect(self._batch.on_lakeshore_snapshot)
            signal = getattr(lakeshore_controller, "disconnected", None)
            if signal is not None:
                if self._transport is not None:
                    signal.connect(self._transport.on_lakeshore_disconnected)
                if self._batch is not None:
                    signal.connect(self._batch.on_lakeshore_disconnected)
            signal = getattr(lakeshore_controller, "fault", None)
            if signal is not None:
                signal.connect(self._thermal_fault)
            signal = getattr(lakeshore_controller, "error", None)
            if signal is not None:
                signal.connect(self._thermal_error)

    modes = Property("QVariantList", lambda self: [{"id": i, "key": m[0], "title": m[1]} for i, m in enumerate(MODES)], constant=True)

    def _mode_available(self, mode):
        backend = self._selected_backend()
        return mode < 4 or (mode == 4 and backend == "1000") or (
            mode == 5 and backend in ("1000", "2100"))

    availableModes = Property("QVariantList", lambda self: [entry for entry in self.modes if self._mode_available(entry["id"])], notify=changed)

    def _set_mode(self, mode):
        mode = int(mode)
        if not 0 <= mode < len(MODES):
            self._fail("Unknown measurement mode")
            return
        if self.active:
            self._fail("Wait for measurement cleanup before changing mode")
            return
        if not self._mode_available(mode):
            self._mode_selection_rejected = True
            self._fail("Measurement mode is unavailable for the configured magnet")
            return
        selection_was_rejected = self._mode_selection_rejected
        self._mode_selection_rejected = False
        if mode == self._mode and selection_was_rejected:
            self._phase, self._message = "idle", "Ready"
            self.changed.emit()
        if mode != self._mode:
            if not self._flush_recipes():
                return
            self._mode = mode
            self._recipe_details_visible = False
            self._confirmation = self._confirmation_granted = None
            self._phase, self._message = "idle", "Ready"
            self.modeChanged.emit()
            self.fieldsChanged.emit()
            self.uiSectionsChanged.emit()
            self.changed.emit()

    mode = Property(int, lambda self: self._mode, _set_mode, notify=modeChanged)
    fields = Property("QVariantList", lambda self: schema(self._values[self._mode], self._mode, self._field_errors[self._mode]), notify=fieldsChanged)
    def _ui_sections(self, selected=None):
        values, mode = self._values[self._mode], self._mode
        errors = self._field_errors[mode]
        sections = measurement_sections(values, mode, self._selected_rows[mode] if selected is None else selected, errors,
                                        transport_coordinates=self._transport_coordinates, sampling=self._sampling.step, magnet_backend=self._selected_backend())
        channels = self._plot_channels(values.get("vds_source", "None"))
        for section in sections:
            for field in section["fields"]:
                if field["key"] == "plot_choice":
                    field["options"] = channels
        visible_keys = {field["key"] for section in sections for field in section["fields"]
                        if not field.get("rowKey")}
        recovery = []
        for (row_key, _row_index, key), message in errors.items():
            if row_key or key in visible_keys or key not in values or isinstance(values[key], list):
                continue
            field = descriptor(key, values[key], mode)
            field.update(error=message, rowKey="", rowIndex=-1)
            recovery.append(field)
        if recovery:
            sections.append(dict(title="Other invalid parameters", fields=recovery, advanced=True,
                                 recovery=True, columns=2,
                                 description="Invalid input was not applied. The last valid value is shown; re-enter a value to correct the error."))
        return sections

    uiSections = Property("QVariantList", lambda self: self._ui_sections(), notify=uiSectionsChanged)

    @Property(str, notify=uiSectionsChanged)
    def signalSummary(self):
        from app.drag_drive import measurement_mode
        chain = signal_chain_metadata(self.get_signal_chain())
        mode = measurement_mode(chain)
        labels = {"ordinary": "Ordinary", "single_drag": "Single Lock-in · Drag",
                  "single_drive": "Single Lock-in · Drive", "dual": "Two Lock-ins · Drag / Drive"}
        config = chain.get("drag_drive") or {}
        text = labels.get(mode, mode)
        if mode in ("single_drive", "dual"):
            text += f" · SR551 ×10 · Rs = {config.get('series_resistance_ohm', 100000):g} Ω"
        return text
    @Property(str, notify=resolutionSummaryChanged)
    def resolutionSummary(self):
        """Cached readback only; this summary never contacts an instrument."""
        values = self._values[self._mode]
        names = ["g1", "g2"]
        uses_g3 = values.get("vds_source") == "Keithley 2400" and (self._mode != 3 or values.get("use_vds"))
        if self._mode in (4, 5) and values.get("conditions"):
            uses_g3 = any(row.get("enabled", True) and row.get("vds_source") == "Keithley 2400"
                          for row in values["conditions"])
        if uses_g3:
            names.append("g3")
        if values.get("bias2_mode", "Disabled") != "Disabled" or any(
                row.get("enabled", True) and row.get("bias2_mode", "Disabled") != "Disabled"
                for key in ("conditions", "bias_conditions") for row in values.get(key, [])):
            names.append("g4")
        parts = []
        for name in names:
            session = self.device_manager.get_session(name)
            if session is None:
                continue
            status = source_resolution_status(session)
            if status.quantum is None:
                parts.append(f"{name.upper()}: resolution unconfirmed")
            else:
                source_range = getattr(session, "cached_source_voltage_range_v", None)
                parts.append(f"{name.upper()}: {float(source_range):g} V range / {status.quantum:g} V step")
        return " · ".join(parts)

    @Property("QVariantMap", notify=fieldsChanged)
    def filenameField(self):
        values, mode = self._values[self._mode], self._mode
        row_key, row_index = "", -1
        if mode == 4 and values["conditions"]:
            row_key = "conditions"
            row_index = min(max(0, self._selected_rows[mode].get(row_key, 0)), len(values[row_key]) - 1)
            current = values[row_key][row_index]
        else:
            current = values
        field = descriptor("base_name", current["base_name"], mode)
        field.update(rowKey=row_key, rowIndex=row_index)
        error = self._field_errors[mode].get((row_key, row_index, "base_name"))
        if error:
            field["error"] = error
        return field

    @Property("QVariantList", notify=fieldsChanged)
    def validationIssues(self):
        values, mode = self._values[self._mode], self._mode
        issues = []
        for (row_key, row_index, key), message in self._field_errors[mode].items():
            selected = dict(self._selected_rows[mode])
            if row_key:
                selected[row_key] = row_index
            sections = self._ui_sections(selected)
            match = next(((section, field) for section in sections for field in section["fields"]
                          if field["key"] == key and field.get("rowKey", "") == row_key), None)
            if match:
                section, field = match
                title, field_label = section["title"], field["label"]
            else:
                # A condition's inactive fields still belong to its recipe table.
                section = next((item for item in sections if item.get("rowKey") == (row_key or key)), None)
                title = section["title"] if section else (sections[0]["title"] if sections else "")
                field_label = label(key)
            issues.append(dict(section=title, key=key, rowKey=row_key, rowIndex=row_index,
                               label=field_label, message=message))
        return issues

    @staticmethod
    def _convert_input(value, default, key, mode):
        # Translate Python's type-conversion exception only; retain domain errors.
        if isinstance(default, (int, float)) and not isinstance(default, bool):
            try:
                float(value)
            except (TypeError, ValueError, OverflowError) as exc:
                example = "3" if isinstance(default, int) else "1.5"
                raise ValueError(f"{label(key)}: enter a valid number (for example, {example})") from exc
        return convert(value, default, key, mode)

    summary = Property(str, lambda self: self._summary(), notify=fieldsChanged)
    @Property("QVariantMap", notify=outputPreviewChanged)
    def outputPreview(self):
        try:
            if self._field_errors[self._mode]:
                raise ValueError("Correct the highlighted fields to update the output preview")
            return preview_output(self._values[self._mode], self._mode, self.save, self.get_signal_chain())
        except (ValueError, TypeError, KeyError, OverflowError) as exc:
            return {"error": str(exc), "csvNames": [], "csvCount": 0, "folder": "", "metadata": "", "log": "", "note": ""}
    @Slot("QVariantMap", str, result="QVariantMap")
    def previewOutputDraft(self, field, draft):
        """Preview a filename draft without committing it or changing validation state."""
        empty = {"csvNames": [], "csvCount": 0, "folder": "", "metadata": "", "log": "", "note": ""}
        try:
            key = field.get("key", "")
            row_key = field.get("rowKey", "") or ""
            row_index = int(field.get("rowIndex", -1))
            if key != "base_name":
                raise ValueError("Select the file base name to preview")
            if row_key and (self._mode != 4 or row_key != "conditions"):
                raise ValueError("This condition does not have an independent file base name")
            identity = (row_key, row_index if row_key else -1, key)
            if any(error_key != identity for error_key in self._field_errors[self._mode]):
                raise ValueError("Correct other invalid parameters to update the output preview")
            values = deepcopy(self._values[self._mode])
            selected = None
            if row_key:
                rows = values[row_key]
                if not 0 <= row_index < len(rows):
                    raise ValueError("The selected condition no longer exists")
                selected = row_index
                rows[row_index][key] = convert(draft, row_default(row_key, self._mode)[key], key, self._mode)
                scope = f"Condition {row_index + 1}" + (" (disabled)" if not rows[row_index].get("enabled", True) else "")
            else:
                values[key] = convert(draft, defaults(self._mode)[key], key, self._mode)
                scope = "Measurement"
            result = preview_output(values, self._mode, self.save, self.get_signal_chain(), selected_condition=selected)
            result["editingScope"] = scope
            return result
        except (ValueError, TypeError, KeyError, IndexError, OverflowError) as exc:
            return {**empty, "error": str(exc), "editingScope": ""}

    phase = Property(str, lambda self: self._phase, notify=changed)
    message = Property(str, lambda self: self._message, notify=changed)
    progress = Property(float, lambda self: self._progress, notify=changed)
    active = Property(bool, lambda self: bool(self.worker_thread is not None or self._preflight_thread is not None or self._batch_claimed or
        self._confirmation is not None or (self._batch and self._batch.active) or
        (self._transport and (self._transport.active or self._transport._cleanup_in_progress))), notify=changed)
    canStart = Property(bool, lambda self: not self.active and not self._mode_selection_rejected and self._mode_available(self._mode) and not self._field_errors[self._mode] and not self.device_manager.is_busy() and not getattr(self._lakeshore, "input_configuration_pending", False), notify=changed)
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
                from app.engine.bfield_transport_sweep import build_stepped_targets
                step_detail = ""
                if params.measurement_mode == "stepped":
                    count = len(build_stepped_targets(params))
                    total = count * 2 - 1 if params.round_trip else count
                    step_detail = f"Stepped: {total} points per condition; temperature wait is variable\n"
                    if params.magnet_backend == "1000":
                        step_detail += (f"Readback sampling; acceptance ±{params.field_acceptance_t * 1000:g} mT; field window "
                                        f"for {params.field_stability_s:g} s; extra delay {params.field_settle_s:g} s\n")
                if params.magnet_backend == "2100":
                    return (step_detail + f"attoDRY2100: {params.start_field_t:+g} → {params.stop_field_t:+g} T; SDK-managed ramp\n"
                            f"{len(plan.conditions)} enabled fixed-bias conditions; active SDK temperature control")
                return (step_detail + f"APS100: {params.start_field_t:+g} → {params.stop_field_t:+g} T; maximum {params.rate_t_per_min:g} T/min; stored range limits may be slower\n"
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
            if self._selected_backend() == "2100":
                self._values[5]["measurement_mode"] = "stepped"
            return
        import json
        for i, (key, _, _) in enumerate(MODES):
            self._values[i] = legacy_values(self._settings, i)
            saved = self._settings.value(f"quick/measurements/{key}", "")
            values = {}
            try:
                values = json.loads(saved) if isinstance(saved, str) and saved else {}
                for name, value in values.items():
                    if name in self._values[i]:
                        self._values[i][name] = convert(value, defaults(i)[name], name, i)
            except (TypeError, ValueError):
                pass
            if i == 5:
                if (self._selected_backend() == "2100" and "measurement_mode" not in values
                        and self._settings.value("tabs/bfield_transport/measurement_mode", None) is None):
                    self._values[i]["measurement_mode"] = "stepped"
                normalize_transport_spacing(self._values[i])

    def _validate_2100_saved_options(self):
        if self._selected_backend() != "2100":
            return
        for key, supported in (("measurement_mode", "stepped"), ("sample_reversal_mode", "unchanged")):
            identity = ("", -1, key)
            value = self._values[5][key]
            if value != supported:
                self._field_errors[5][identity] = (
                    f"Saved value {value!r} is unsupported on attoDRY2100; choose {supported}.")
            else:
                self._field_errors[5].pop(identity, None)

    def _save_settings(self):
        if self._settings is not None:
            import json
            self._settings.setValue(f"quick/measurements/{MODES[self._mode][0]}", json.dumps(self._values[self._mode]))

    @Slot(str, result=QObject)
    def rowModel(self, key):
        allowed = {2: {"regions"}, 3: {"bias_conditions"}, 4: {"conditions"}, 5: {"conditions"}}
        if key not in allowed.get(self._mode, set()):
            return None
        from app.quick.recipes import RecipeModel
        identity = (self._mode, key)
        if identity not in self._recipe_models:
            self._recipe_models[identity] = RecipeModel(self, *identity)
        return self._recipe_models[identity]

    def _flush_recipes(self):
        return all(model.flush() for (mode, _), model in self._recipe_models.items() if mode == self._mode)

    @Slot(bool)
    def setRecipeDetailsVisible(self, visible):
        visible = bool(visible) and self._mode == 4
        if visible != self._recipe_details_visible:
            self._recipe_details_visible = visible
            if visible:
                self.uiSectionsChanged.emit()

    def _recipe_changed(self, refresh_sections=False):
        self._confirmation_granted = None
        self._save_settings()
        self.fieldsChanged.emit()
        if refresh_sections or (self._mode == 4 and self._recipe_details_visible):
            self.uiSectionsChanged.emit()
        self.changed.emit()

    def _sampling_changed(self):
        self.uiSectionsChanged.emit()
        for (mode, _), model in self._recipe_models.items():
            if mode == self._mode:
                model.notify_all()

    transportCoordinates = Property(str, lambda self: self._transport_coordinates, notify=uiSectionsChanged)
    sweepPreview = Property(QObject, lambda self: self._sweep_preview, constant=True)

    @Slot(bool)
    def setSweepPreviewVisible(self, visible):
        self._sweep_preview_visible = bool(visible)
        if not visible:
            self._sweep_preview_timer.stop()
            self._sweep_preview_pending = False
        elif self._sweep_preview.stale and not self.active:
            self._sweep_preview_pending = True
            self._sweep_preview_timer.start()

    def _invalidate_sweep_preview(self):
        self._sweep_preview.invalidate()
        if self._sweep_preview_visible and not self.active:
            self._sweep_preview_pending = True
            self._sweep_preview_timer.start()

    def _sweep_preview_finished(self):
        if self._sweep_preview_pending and not self._sweep_preview.busy and self._sweep_preview_visible and not self.active:
            self._sweep_preview_timer.start()

    def _update_sweep_preview(self):
        if not self._sweep_preview_visible or self.active:
            return
        if self._sweep_preview.busy:
            self._sweep_preview_pending = True
            return
        self.requestSweepPreview()

    @Slot()
    def requestSweepPreview(self):
        if self.active:
            self._fail("Wait for measurement cleanup before rebuilding the sweep preview")
            return
        if self._sweep_preview.busy:
            if self._sweep_preview_visible:
                self._sweep_preview_pending = True
            return
        self._sweep_preview_timer.stop()
        self._sweep_preview_pending = False
        if not self._flush_recipes() or self._field_errors[self._mode]:
            self._sweep_preview_timer.stop()
            self._sweep_preview_pending = False
            self._sweep_preview.reject("Correct the highlighted input fields before previewing the sweep")
            return
        values = self._values[self._mode]
        if self._mode == 4 and values["conditions"]:
            index = min(max(0, self._selected_rows[4].get("conditions", 0)), len(values["conditions"]) - 1)
            params = build_params(values["conditions"][index], 4)
        else:
            params = self.collect_params()
        self._sweep_preview_timer.stop()
        self._sweep_preview_pending = False
        self._sweep_preview.request(self._mode, params, self.conns)

    @Slot()
    def useGateRegions(self):
        if self.active or self._mode != 2:
            return
        if not self._flush_recipes():
            return
        values = self._values[2]
        values.update(coordinate_mode="Raw", axis_fast="Vtg", axis_slow="Vbg", vds_stop=values["vds_start"])
        self._confirmation_granted = None
        self._save_settings()
        self.fieldsChanged.emit()
        self.uiSectionsChanged.emit()
        self.changed.emit()

    def _set_transport_coordinates(self, value):
        if self._mode != 5 or value not in ("Raw", "Derived"):
            raise ValueError("Choose Raw or Derived gate coordinates")
        if value == self._transport_coordinates:
            return
        if not self._flush_recipes():
            self.uiSectionsChanged.emit()
            return
        if self._field_errors[5]:
            raise ValueError("Correct invalid parameters before changing gate coordinates")
        values = self._values[5]
        for row in values["conditions"]:
            derived_to_gates(row["doping"], row["efield"], values["ratio"], values["ratio_target"])
        self._transport_coordinates = value
        model = self._recipe_models.get((5, "conditions"))
        if model:
            model.notify_all()
        self.uiSectionsChanged.emit()
        self.changed.emit()

    @Slot(str, "QVariant")
    def setValue(self, key, value):
        if self.active:
            self._fail("Parameters are locked until cleanup finishes")
            return
        try:
            if self._mode == 5 and key in {"field_spacing", "field_points"}:
                return  # Spacing mode and calculated point count are not editors.
            if key == "transport_coordinates":
                self._set_transport_coordinates(str(value))
                return
            original = defaults(self._mode)
            if key not in original or key.startswith("output_"):
                raise ValueError("Unknown parameter: " + key)
            model = self._recipe_models.get((self._mode, key))
            converted = self._convert_input(value, original[key], key, self._mode)
            if self._mode == 5 and self._selected_backend() == "2100":
                supported = {"measurement_mode": "stepped", "sample_reversal_mode": "unchanged"}
                if key in supported and converted != supported[key]:
                    raise ValueError(f"attoDRY2100 requires {supported[key]} for {label(key)}")
            if self._mode == 5 and key in BFIELD_MT_FIELDS:
                converted /= 1000.  # The editor uses mT; recipes and hardware retain T.
            if self._mode == 2 and key in {"coordinate_mode", "axis_fast", "axis_slow"} and not self._flush_recipes():
                self.uiSectionsChanged.emit()
                return
            replacement_rows = None
            if self._mode == 5 and key in {"ratio", "ratio_target"}:
                if not self._flush_recipes():
                    self.uiSectionsChanged.emit()
                    return
                values = self._values[5]
                if self._transport_coordinates == "Raw":
                    ratio = converted if key == "ratio" else values["ratio"]
                    target = converted if key == "ratio_target" else values["ratio_target"]
                    if abs(ratio) < 1e-12:
                        raise ValueError("Raw gate input requires a non-zero shared ratio")
                    replacement_rows = []
                    for row in values["conditions"]:
                        vtg, vbg = derived_to_gates(row["doping"], row["efield"], values["ratio"], values["ratio_target"])
                        doping, efield = gates_to_derived(vtg, vbg, ratio, target)
                        if not all(math.isfinite(v) for v in (doping, efield)):
                            raise ValueError("Gate conversion must remain finite")
                        replacement_rows.append(dict(doping=doping, efield=efield, vtg=vtg, vbg=vbg))
            if self._mode == 3 and key == "use_vds" and converted != self._values[3][key]:
                recipe = self._recipe_models.get((3, "bias_conditions"))
                if recipe is not None and not recipe.flush():
                    # A QML checkbox may already have toggled visually. Rebuild
                    # its descriptor so the accepted value is displayed again.
                    self.uiSectionsChanged.emit()
                    return
            if model is not None and not model.flush():
                return
            if model is not None:
                model.reset_rows(converted)
                self._field_errors[self._mode] = {error_key: message for error_key, message in self._field_errors[self._mode].items() if error_key[0] != key}
            else:
                self._values[self._mode][key] = converted
                if replacement_rows is not None:
                    for row, updates in zip(self._values[5]["conditions"], replacement_rows):
                        row.update(updates)
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
            self.uiSectionsChanged.emit()
            self.changed.emit()
            if key in {"use_vds", "ratio", "ratio_target"}:
                for (mode, _), recipe in self._recipe_models.items():
                    if mode == self._mode:
                        recipe.notify_all()
        except (TypeError, ValueError, OverflowError) as exc:
            if key == "transport_coordinates":
                self.uiSectionsChanged.emit()
            if key in self._values[self._mode] and not key.startswith("output_"):
                self._field_errors[self._mode][("", -1, key)] = str(exc)
                self.fieldsChanged.emit()
                self.uiSectionsChanged.emit()
            self._fail(str(exc))

    @Slot(str, int, str, "QVariant")
    def setRowValue(self, key, row, column, value):
        self._set_recipe_cell(key, row, column, value)

    def _set_recipe_cell(self, key, row, column, value, *, notify_model=True):
        if self.active:
            self._fail("Parameters are locked until cleanup finishes")
            return False
        if self._mode == 5 and key == "conditions":
            calculated = {"doping", "efield"} if self._transport_coordinates == "Raw" else {"vtg", "vbg"}
            if column in calculated or column in {"ratio", "ratio_target"}:
                self._fail("This coordinate is calculated; edit the selected gate inputs instead")
                return False
        try:
            raw_gate = self._mode == 5 and key == "conditions" and self._transport_coordinates == "Raw"
            rows = self._values[self._mode][key]
            if not isinstance(rows, list) or not 0 <= row < len(rows):
                raise ValueError("Invalid condition row")
            default = row_default(key, self._mode)
            if column not in default:
                raise ValueError("Unknown condition column")
            converted = self._convert_input(value, default[column], column, self._mode)
            changes_shape = self._mode == 4 and (column in {"mode", "vds_source"} or
                (column.startswith("raw_") and column.endswith("_active")))
            changes_shape = changes_shape or (self._mode == 5 and column == "vds_source") or column == "bias2_mode"
            model = self._recipe_models.get((self._mode, key))
            if notify_model and changes_shape and converted != rows[row][column] and model and not model.flush():
                model.notify_row(row)
                if self._mode == 4 and self._recipe_details_visible:
                    self.uiSectionsChanged.emit()
                return False
            if raw_gate and column in {"vtg", "vbg"}:
                values = self._values[5]
                vtg, vbg = derived_to_gates(rows[row]["doping"], rows[row]["efield"], values["ratio"], values["ratio_target"])
                vtg, vbg = (converted, vbg) if column == "vtg" else (vtg, converted)
                doping, efield = gates_to_derived(vtg, vbg, values["ratio"], values["ratio_target"])
                if not all(math.isfinite(v) for v in (doping, efield)):
                    raise ValueError("Gate conversion must remain finite")
                rows[row].update(vtg=vtg, vbg=vbg, doping=doping, efield=efield)
            else:
                rows[row][column] = converted
            self._field_errors[self._mode].pop((key, row, column), None)
            self._field_errors[self._mode].pop(("", -1, key), None)
            self._recipe_changed()
            model = self._recipe_models.get((self._mode, key))
            if model and notify_model:
                model.notify_row(row)
            return True
        except (KeyError, TypeError, ValueError, OverflowError) as exc:
            rows = self._values[self._mode].get(key)
            if isinstance(rows, list) and 0 <= row < len(rows) and column in rows[row]:
                self._field_errors[self._mode][(key, row, column)] = str(exc)
                self.fieldsChanged.emit()
                if self._mode == 4 and self._recipe_details_visible:
                    self.uiSectionsChanged.emit()
                model = self._recipe_models.get((self._mode, key))
                if model:
                    model.notify_row(row)
            self._fail(str(exc))
            return False

    def _recipe_operation(self, key, operation, row=-1):
        if self.active:
            self._fail("Parameters are locked until cleanup finishes")
            return
        try:
            rows = self._values[self._mode][key]
            if not isinstance(rows, list):
                raise ValueError("Parameter is not a row table")
            model = self.rowModel(key)
            was_empty = not rows
            if model is not None and not model.flush():
                return
            if operation != "add" and not 0 <= row < len(rows):
                raise ValueError("Invalid condition row")
            if operation != "remove" and len(rows) >= 100:
                raise ValueError("At most 100 conditions or regions are allowed")
            if operation == "add":
                seed = row_default(key, self._mode)
                if self._mode == 4 and key == "conditions" and not rows:
                    seed.update({k: deepcopy(v) for k, v in self._values[4].items() if k in seed})
                if "name" in seed:
                    names = [r.get("name") for r in rows]
                    prefix = "Con" if self._mode == 5 and key == "conditions" else "Condition "
                    seed["name"] = unique_condition_name(names, prefix=prefix)
                index = len(rows)
            elif operation == "duplicate":
                seed = deepcopy(rows[row])
                if "name" in seed:
                    seed["name"] = unique_condition_name((r.get("name") for r in rows), seed["name"])
                index = row + 1
                self._shift_row_errors(key, index)
            else:
                index = row
                self._shift_row_errors(key, index, removing=True)
            if operation == "remove":
                if model: model.remove(index)
                else: rows.pop(index)
                selected = max(0, min(index, len(rows)-1))
            else:
                if model: model.insert(index, seed)
                else: rows.insert(index, seed)
                selected = index
            self._selected_rows[self._mode][key] = selected
            if model:
                model.selectedIndexChanged.emit()
            self._field_errors[self._mode].pop(("", -1, key), None)
            self._recipe_changed(refresh_sections=(was_empty != (not rows)))
        except (KeyError, TypeError, ValueError, OverflowError) as exc:
            self._fail(str(exc))

    @Slot(str)
    def addRow(self, key):
        self._recipe_operation(key, "add")

    @Slot(str, int)
    def removeRow(self, key, row):
        self._recipe_operation(key, "remove", row)

    @Slot(str, int)
    def selectRow(self, key, row):
        rows = self._values[self._mode].get(key)
        if isinstance(rows, list) and 0 <= row < len(rows):
            self._selected_rows[self._mode][key] = row
            model = self._recipe_models.get((self._mode, key))
            if model: model.selectedIndexChanged.emit()
            self.fieldsChanged.emit()
            if self._mode == 4 and self._recipe_details_visible:
                self.uiSectionsChanged.emit()

    @Slot(str, int)
    def duplicateRow(self, key, row):
        self._recipe_operation(key, "duplicate", row)

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
        params = build_params(self._values[self._mode], self._mode)
        if hasattr(params, "plot_choice"):
            channels = self._plot_channels(getattr(params, "vds_source", "None"))
            if channels and params.plot_choice not in channels:
                params.plot_choice = channels[0]
        if self._mode == 5:
            params.magnet_backend = self._selected_backend()
        return params

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

    def _validate(self, params, mode, *, refresh_missing=True):
        from app.bias_control import validate_bias2
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
        use_vds = params.vds_source != "None" and (mode != 3 or params.use_vds)
        if mode == 0 and not use_vds:
            raise ValueError("Vds Sweep requires a Bias 1 source")
        if mode in (1, 4) and not use_vds and (
                params.raw_vds_active if params.mode == "Raw" else params.derived_vds_mode == "Swept"):
            raise ValueError("Enable a Bias 1 source or disable its sweep")
        if mode == 2 and not use_vds and "Vds" in (params.axis_fast, params.axis_slow):
            raise ValueError("A Vds map axis requires a Bias 1 source")
        if use_vds and params.vds_source == "Keithley 2400":
            required.append("g3")
        if use_vds and params.vds_source not in ("Keithley 2400", "NI DAQ AO"):
            raise ValueError("Unsupported Vds source")
        if mode == 3:
            for condition, point in zip((c for c in params.bias_conditions if c.enabled), points):
                validate_bias2(condition, [point], self.device_manager, bias1_enabled=use_vds,
                               bias1_source=params.vds_source)
            bias2_enabled = any(c.enabled and c.bias2_mode != "Disabled" for c in params.bias_conditions)
        else:
            validate_bias2(params, points, self.device_manager, bias1_enabled=use_vds)
            bias2_enabled = params.bias2_mode != "Disabled"
        if bias2_enabled:
            required.append("g4")
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
        if mode in (0, 1, 2, 3, 4):
            from app.constants import GATE_BIAS_RAMP_STEP_V
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
                    quantum = resolution.quantum
                    source_range = float(self.device_manager.get_session(device).cached_source_voltage_range_v)
                    if any(abs(float(point[axis])) > source_range + quantum * 1e-7 for point in points):
                        raise ValueError(f"{device.upper()} trajectory exceeds the {source_range:g} V source range.")
                    resolutions[axis] = quantum
                    ramp = (getattr(params, "vds_ramp", None) if axis == "vds" else
                            getattr(params, "vg_ramp", None) if mode in (1, 2, 4) else GATE_BIAS_RAMP_STEP_V)
                    if ramp is not None and float(ramp) + 1e-12 < quantum:
                        raise ValueError(f"{device.upper()} ramp step {float(ramp):g} V is below the {quantum:g} V source resolution.")
                    if mode == 0 and axis == "vds" and float(params.vds_step) + 1e-12 < quantum:
                        raise ValueError(f"G3 Vds sample step {float(params.vds_step):g} V is below the {quantum:g} V source resolution.")
            if refresh:
                if not refresh_missing:
                    raise ValueError("Source range remains unavailable after refresh; check source readback.")
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
        measurement, parts = output_spec(params, mode, chain)
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
        if not self._flush_recipes():
            return
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
            if self._mode_selection_rejected or not self._mode_available(self._mode):
                raise ValueError("Measurement mode is unavailable for the configured magnet; select an available mode")
            if getattr(self._lakeshore, "input_configuration_pending", False):
                raise ValueError("Wait for LS335 input configuration readback before starting a measurement")
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
        configurations = [params]
        if mode == 4:
            rows = self._values[4]["conditions"]
            if rows:
                configurations = [build_params({**defaults(4), **{k: v for k, v in row.items() if k not in ("enabled", "name")}}, 4)
                                  for row in deepcopy(rows) if row["enabled"]]
        def verify_before_start():
            if mode < 5:
                for name in ("g1", "g2", "g3", "g4"):
                    if name not in required:
                        continue
                    session = self.device_manager.get_session(name)
                    refresh = getattr(session, "refresh_source_voltage_range", None)
                    if not callable(refresh):
                        raise ValueError(f"{name.upper()}: source range refresh is unavailable.")
                    refresh()
                for configuration in configurations:
                    self._validate(configuration, mode, refresh_missing=False)
            return self._rates()
        task = PreflightThread(verify_before_start, self)
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
        values = self._values[self._mode]
        rows = values.get("bias_conditions", values.get("conditions", []))
        if self._mode == 5:
            source = "Keithley 2400" if any(row.get("enabled", True) and row.get("vds_source") == "Keithley 2400" for row in rows) else "None"
        elif self._mode == 3 and not values.get("use_vds"):
            source = "None"
        bias2 = (any(row.get("enabled", True) and row.get("bias2_mode", "Disabled") != "Disabled" for row in rows)
                 if rows else values.get("bias2_mode", "Disabled") != "Disabled")
        return plot_channel_options(source, metadata, bias2=bias2)

    def _launch(self, params, mode, calibration, snapshot, *, batch_metadata=None):
        amp, lockin, chain = calibration
        worker = self._factories[mode](params, deepcopy(self.save), deepcopy(self.conns),
            **{d: self.device_manager.get_session(d) if d in self._claimed else None for d in ("g1", "g2", "g3", "g4", "daq", "mono")},
            device_manager=self.device_manager,
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
        if not self._mode_available(self._mode):
            raise ValueError("Magnetic measurement is unavailable for the configured magnet")
        if self._selected_backend() == "2100" and self._values[5]["sample_reversal_mode"] != "unchanged":
            raise ValueError("attoDRY2100 requires unchanged sample current reversal")
        if self._mode == 4 and self._batch is None:
            raise ValueError("APS100 controller is unavailable")
        if self._mode == 5 and (self._transport is None or
                                self._selected_backend() not in self._transport._engines):
            raise ValueError("Configured magnet controller is unavailable")
        if self._selected_backend() != "1000":
            if self._mode == 4:
                raise ValueError("B-field Gate Scan requires attoDRY1000 / APS100")
            if self.collect_params().measurement_mode != "stepped":
                raise ValueError("attoDRY2100 supports stepped B-field transport only")
        if not self._review_valid_callback():
            raise ValueError("Commissioning review for the selected magnet is required")
        thermal = self._batch.thermal_safety if self._batch is not None else None
        if self._selected_backend() == "1000" and (thermal is None or not bool(getattr(thermal, "is_armed", getattr(thermal, "armed", False)))):
            raise ValueError("A commissioned, armed Lake Shore thermal evaluator is required")
        snapshot = getattr(self._magnet, "latest_snapshot", None)
        if self._selected_backend() == "1000" and snapshot is not None and getattr(snapshot, "heater_on", None) is False and not self.confirm_persistent_field(snapshot):
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
            from app.bias_control import validate_bias2
            for condition in plan.conditions:
                validate_bias2(condition, [condition.vds], self.device_manager,
                               bias1_enabled=condition.vds_source != "None")
            if any(c.bias2_mode != "Disabled" for c in plan.conditions):
                required.append("g4")
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
