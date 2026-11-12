"""UI independent parameter descriptions and strict conversion for QML forms."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, fields
import math
import json
import re

from app.models import BFIELD_HYSTERESIS_LABELS, BFIELD_MT_FIELDS
from app.models import (DualGateParams, LineSweepParams, CoParams, PhotocurrentParams,
                        PhotocurrentBiasCondition, BFieldTransportParams,
                        BFieldTransportCondition)
from app.gate_transform import derived_to_gates, ratio_formula_text
from app.quick.sampling import step_sampling, gate_sampling

MODES = [("vds_sweep", "Vds Sweep", DualGateParams),
         ("gate_scan", "Gate Scan", LineSweepParams),
         ("map", "2D Map", CoParams),
         ("photocurrent", "Photocurrent", PhotocurrentParams),
         ("bfield_gate_scan", "B-field Gate Scan", LineSweepParams),
         ("bfield_transport", "B-field Sweep", BFieldTransportParams)]

CHOICES = {
    "bias2_mode": ["Disabled", "Fixed", "Follow"],
    "vds_source": ["Keithley 2400", "NI DAQ AO", "None"],
    "plot_choice": ["Ids_DC", "Ids_X", "Ids_Y", "Ids_Keithley", "Ibias2_Keithley", "I_drag_X", "I_drag_Y", "I_drag_R", "I_drive_X", "I_drive_Y", "I_drive_R"],
    "plot_x_axis": ["Follow Sweep", "Step Index", "Vtg", "Vbg", "Vds", "Doping", "E-field"],
    "ratio_target": ["Vbg", "Vtg"], "derived_ratio_target": ["Vbg", "Vtg"],
    "derived_axis": ["Doping", "E-field"], "derived_vds_mode": ["Fixed", "Swept"],
    "coordinate_mode": ["Raw", "Derived"], "inner": ["Vtg", "Vbg"],
    "axis_fast": ["Vtg", "Vbg", "Vds", "Doping", "E-field"],
    "axis_slow": ["None", "Vtg", "Vbg", "Vds", "Doping", "E-field"],
    "derived_fast_axis": ["Doping", "E-field", "Vds"],
    "derived_slow_axis": ["Doping", "E-field", "Vds"],
    "sample_reversal_mode": ["unchanged", "per_point", "whole_sweep"],
    "measurement_mode": ["continuous", "stepped"], "field_spacing": ["points", "step"],
    "cooldown_policy": ["adaptive", "stay_driven", "persistent_each_row"], "final_mode": ["driven"],
}
HIDDEN = {"output_csv_path", "output_metadata_path", "output_log_path", "plot_x_resolved", "magnet_backend", "field_spacing"}
REGION_DEFAULT = {f"{axis}_{part}": value for axis in ("vtg", "vbg")
                  for part, value in (("start", 0.), ("stop", 1.), ("step", .1))}


def defaults(index):
    values = asdict(MODES[index][2]())
    if index == 4:
        values.update(fields_t="0", resume_checkpoint="", conditions=[])
    if index == 5:
        values["plot_choice"] = "Ids_DC"
    return values


def label(key):
    if key in BFIELD_HYSTERESIS_LABELS:
        return BFIELD_HYSTERESIS_LABELS[key]
    special = {"n_sample": "Averages", "n_points": "Number of points", "delay": "Acquisition delay (s)",
               "bias2_mode": "Bias 2 mode", "bias2_voltage": "Bias 2 voltage (V)",
               "bias2_offset": "Bias 2 − Bias 1 offset (V)", "bias2_ramp": "Bias 2 ramp step (V)",
               "vg_ramp": "Gate ramp step (V)", "vds_ramp": "Vds ramp step (V)",
               "fields_t": "B-field targets (T)", "resume_checkpoint": "Resume checkpoint path",
               "wl_start": "Start (nm)", "wl_stop": "Stop (nm)", "wl_step": "Step (nm)",
               "start_field_t": "Start (T)", "stop_field_t": "Stop (T)",
               "sample_reversal_mode": "Sample B current reversal", "sample_reversal_settle_s": "Reversal settling wait (s)",
               "measurement_mode": "Measurement mode", "field_spacing": "Field spacing",
               "field_points": "Points / pass (calculated)", "field_step_t": "Step size (mT)",
               "field_tolerance_t": "Field tolerance (±mT)",
               "field_stability_s": "Field stability window (s)",
               "field_settle_s": "Delay after stability (s)",
               "temperature_target_k": "Sample target (K)", "temperature_tolerance_k": "Target tolerance (K)",
               "temperature_window_s": "Stability window (s)", "temperature_span_k": "Maximum window span (K)",
               "temperature_drift_k_per_min": "Maximum absolute drift (K/min)", "temperature_timeout_s": "Wait timeout (s)",
               "rate_t_per_min": "Maximum sweep rate (T/min)", "settle_s": "Settle time (s)",
               "acquisition_delay_s": "Acquisition delay (s)", "averages": "Averages",
               "derived_ratio": "Coupling ratio r", "ratio": "Coupling ratio r",
               "ratio_target": "r multiplies", "derived_ratio_target": "r multiplies",
               "coordinate_mode": "Coordinates", "mode": "Trajectory", "axis_fast": "Fast axis",
               "axis_slow": "Slow axis / dimension", "sweep_both_ways": "Forward + reverse",
               "round_trip": "Return to start field", "use_vds": "Apply Vds", "base_name": "File base name",
               "n_points": "Points / pass", "derived_axis": "Sweep coordinate",
               "derived_start": "Start (V)", "derived_stop": "Stop (V)",
               "derived_fixed": "Held coordinate (V)", "derived_vds_mode": "Vds trajectory"}
    text = key.replace("_", " ")
    text = text[:1].upper() + text[1:]
    for old, new in (("vtg", "Vtg"), ("vbg", "Vbg"), ("vds", "Vds"), ("efield", "E-field")):
        text = text.replace(old, new).replace(old.capitalize(), new)
    if key in special:
        return special[key]
    if any(key.startswith(p) for p in ("vtg", "vbg", "vds", "raw_v", "derived_vds")) and not key.endswith(("active", "source", "mode")):
        text += " (V)"
    if key.startswith(("doping", "efield")):
        text += " (V)"
    return text


def row_default(key, mode):
    if key == "bias_conditions":
        return asdict(PhotocurrentBiasCondition())
    if key == "regions":
        return deepcopy(REGION_DEFAULT)
    if mode == 4:
        return {"enabled": True, "name": "Condition", **{k: v for k, v in asdict(LineSweepParams()).items() if k not in HIDDEN}}
    return asdict(BFieldTransportCondition())


def bias2_keys(values):
    mode = values.get("bias2_mode", "Disabled")
    return ["bias2_mode"] + (["bias2_voltage"] if mode == "Fixed" else
                             ["bias2_offset"] if mode == "Follow" else []) + (
                             ["bias2_ramp"] if mode != "Disabled" else [])


def bias2_summary(values, bias1=None):
    mode = values.get("bias2_mode", "Disabled")
    if mode == "Disabled":
        return "Bias 2 not used; no connection required."
    if mode == "Fixed":
        return f"Bias 2 = {values.get('bias2_voltage', 0):g} V (G4 / Keithley)."
    offset = values.get("bias2_offset", 0)
    text = f"Bias 2 = Bias 1 + ({offset:g} V); follows every forward/reverse point."
    if bias1 is not None:
        text += f" Target: {float(bias1) + offset:g} V."
    return text


def descriptor(key, value, mode):
    group = ("Output and plot" if key.startswith("plot_") or key == "base_name" else
             "Acquisition and ramps" if key in {"delay", "n_sample", "averages", "acquisition_delay_s", "vds_ramp", "vg_ramp"} else
             "Recipes" if key in {"conditions", "bias_conditions", "regions"} else
             "Magnet" if key in {"fields_t", "resume_checkpoint", "start_field_t", "stop_field_t", "rate_t_per_min", "round_trip", "cooldown_policy", "final_mode"} else
             "Trajectory and biases")
    result = {"key": key, "label": label(key), "value": deepcopy(value), "group": group}
    if mode == 5 and key in BFIELD_MT_FIELDS:
        result["value"] = float(value) * 1000.
    coordinate_key = key.removeprefix("raw_").removeprefix("derived_")
    for prefix, coordinate in (("doping", "Doping"), ("efield", "E-field"), ("vtg", "Vtg"), ("vbg", "Vbg"), ("vds", "Vds")):
        if coordinate_key == prefix or coordinate_key.startswith(prefix + "_"):
            result["coordinate"] = coordinate
            break
    if isinstance(value, list):
        example = row_default(key, mode)
        result.update(kind="rows", rows=deepcopy(value), columns=[descriptor(k, v, mode) for k, v in example.items()])
    elif isinstance(value, bool):
        result["kind"] = "bool"
    elif key in CHOICES or key == "mode":
        options = CHOICES.get(key, ["Raw", "Derived"] if mode in (1, 4) else ["Grid", "Linked"])
        result.update(kind="choice", options=options)
    elif isinstance(value, int):
        result.update(kind="integer", min=0 if key == "ao_channel" else 1,
                      max=1000 if key == "n_sample" else 250000)
    elif isinstance(value, float):
        result.update(kind="number", decimals=6)
    else:
        result["kind"] = "text"
    if key == "final_mode":
        result["readOnly"] = True
    if mode == 5 and key == "field_points":
        result["readOnly"] = True
    return result


def normalize_transport_spacing(values):
    """Migrate remembered point-count recipes to the equivalent step in tesla."""
    if values.get("field_spacing") == "points":
        n = values.get("field_points", 0)
        if isinstance(n, int) and n >= 2:
            values["field_step_t"] = abs(values["stop_field_t"] - values["start_field_t"]) / (n - 1)
    values["field_spacing"] = "step"


def transport_sampling_summary(values, backend="1000"):
    from app.engine.bfield_transport_sweep import build_stepped_targets
    params = BFieldTransportParams(
        start_field_t=values["start_field_t"], stop_field_t=values["stop_field_t"],
        field_spacing="step", field_step_t=values["field_step_t"], magnet_backend=backend)
    targets = build_stepped_targets(params)
    count = len(targets)
    total = 2 * count - 1 if values.get("round_trip") else count
    detail = f"{count} points / pass; {total} points / condition. Start and Stop included."
    last_step = abs(targets[-1] - targets[-2])
    if last_step < params.field_step_t - 1e-12:
        detail += f" Final interval: {last_step * 1000:g} mT (shorter)."
    return count, detail


def schema(values, mode, errors=None):
    errors = errors or {}
    if mode == 5:
        values = deepcopy(values)
        try:
            values["field_points"] = transport_sampling_summary(values, values.get("magnet_backend", "1000"))[0]
        except ValueError:
            values["field_points"] = 0
    result = []
    for key, value in values.items():
        if key in HIDDEN:
            continue
        item = descriptor(key, value, mode)
        if ("", -1, key) in errors:
            item["error"] = errors[("", -1, key)]
        if mode in (1, 4):
            if key.startswith("raw_"):
                item["visible"] = values["mode"] == "Raw"
            elif key.startswith("derived_"):
                item["visible"] = values["mode"] == "Derived" or key in ("derived_ratio", "derived_ratio_target")
        if key == "ao_channel":
            item["visible"] = values.get("vds_source") == "NI DAQ AO"
        if key.startswith("bias2_"):
            item["visible"] = key in bias2_keys(values) and mode != 3
        if mode == 5 and key in BFIELD_HYSTERESIS_LABELS:
            item["visible"] = (values.get("measurement_mode") == "stepped"
                               and values.get("magnet_backend", "1000") == "1000")
        if mode == 2:
            derived = values.get("coordinate_mode") == "Derived"
            if key.startswith(("doping_", "efield_", "derived_")):
                item["visible"] = derived
            if key.startswith(("vtg_", "vbg_")) and key != "vds_source":
                item["visible"] = not derived
            if key == "regions":
                item["visible"] = not derived and {values["axis_fast"], values["axis_slow"]} == {"Vtg", "Vbg"}
        if mode == 3 and key in {"vds_set", "vtg_set", "vbg_set"}:
            item["visible"] = False  # The complete bias recipe owns these.
        result.append(item)
    groups = {name: index for index, name in enumerate(("Trajectory and biases", "Magnet", "Recipes", "Acquisition and ramps", "Output and plot"))}
    return sorted(result, key=lambda item: groups[item["group"]])


def measurement_sections(values, mode, selected=None, errors=None, *, transport_coordinates="Derived", sampling=step_sampling, magnet_backend="1000"):
    """Compact, task-specific views of the existing acquisition parameters.

    Row routing is explicit, so a condition editor never edits ignored global
    Gate Scan values. Calculated transport values are presentation-only.
    """
    selected = selected or {}
    errors = errors or {}
    sections = []
    current = values
    row_key, row_index = "", -1

    def field(key, title=None):
        item = descriptor(key, current[key], mode)
        if mode in (1, 4):
            if key in ("derived_axis", "derived_start", "derived_stop"):
                item["coordinate"] = current["derived_axis"]
            elif key == "derived_fixed":
                item["coordinate"] = "E-field" if current["derived_axis"] == "Doping" else "Doping"
        if title:
            item["label"] = title
        item.update(rowKey=row_key, rowIndex=row_index)
        if (row_key, row_index, key) in errors:
            item["error"] = errors[(row_key, row_index, key)]
        if key == "name":
            item["columnSpan"] = 1
        if key in ("axis_fast", "axis_slow"):
            axes = ["Doping", "E-field", "Vds"] if current["coordinate_mode"] == "Derived" else ["Vtg", "Vbg", "Vds"]
            item["options"] = ([{"value": "None", "label": "None — 1D"}] if key == "axis_slow" and current["coordinate_mode"] == "Raw" else []) + axes
        if key == "cooldown_policy":
            item["options"] = [{"value": k, "label": v} for k, v in (
                ("adaptive", "Adaptive (thermal permission)"), ("stay_driven", "Stay driven between conditions"),
                ("persistent_each_row", "Persistent after each condition"))]
        return item

    def add(title, keys, *, advanced=False, description="", columns=2):
        items = [field(k) for k in keys if k in current]
        if items:
            sections.append(dict(title=title, fields=items, advanced=advanced, description=description, columns=columns))
            for key in keys:
                if not key.endswith("_step") or key[:-5] + "_start" not in current:
                    continue
                prefix = key[:-5]
                if any((row_key, row_index, prefix + "_" + part) in errors for part in ("start", "stop", "step")):
                    text, warning = "Sampling unavailable: correct the highlighted range fields", True
                else:
                    policy = "photo" if mode == 3 else "region" if mode == 2 and values["regions"] else "voltage"
                    text, warning = sampling(current[prefix + "_start"], current[prefix + "_stop"], current[key], "nm" if mode == 3 else "V", policy)
                sections[-1].update(samplingText=text, samplingWarning=warning)
        return sections[-1] if items else None

    def table(key, title, description="", advanced=False):
        rows = values[key]
        index = min(max(0, selected.get(key, 0)), len(rows)-1)
        entries = []
        for i, row in enumerate(rows):
            name = row.get("name") or ("Region" if key == "regions" else "Condition") + f" {i+1}"
            enabled = row.get("enabled", True)
            if mode == 4:
                details = row.get("mode", "Raw") + f" · {row.get('n_points', 51)} points/pass"
            elif mode == 5:
                details = f"D={row['doping']:g}, E={row['efield']:g}; Vds={row['vds']:g} V"
            elif key == "regions":
                details = f"Vtg {row['vtg_start']:g} → {row['vtg_stop']:g}; Vbg {row['vbg_start']:g} → {row['vbg_stop']:g} V"
            else:
                details = f"Vtg={row['vtg']:g}, Vbg={row['vbg']:g} V" + (f"; Vds={row['vds']:g} V" if values.get("use_vds") else "")
            if key != "regions" and row.get("bias2_mode", "Disabled") != "Disabled":
                details += " · " + bias2_summary(row, row.get("vds"))
            entries.append(dict(index=i, name=name, enabled=enabled, detail=details))
        section = dict(title=title, rowKey=key, selectedIndex=index, rows=entries,
                       fields=[], advanced=advanced, description=description, columns=2,
                       error=errors.get(("", -1, key), ""))
        sections.append(section)
        return section, index

    if mode == 4:
        add("Magnetic field targets", ["fields_t"], columns=1)
        condition_table, index = table("conditions", "Gate Scan conditions",
                         "Enabled conditions run at each field, in list order." if values["conditions"] else
                         "Settings below run at every field. Add saves them as a condition.")
        if values["conditions"]:
            current = values["conditions"][index]
            row_key, row_index = "conditions", index
            condition_table["fields"] = [field(k) for k in ("enabled", "name")]

    if mode == 0:
        add("Vds trajectory", ["vds_start", "vds_stop", "vds_step", "sweep_both_ways"], columns=3)
        add("Fixed gates", ["vtg_set", "vbg_set"])
    elif mode in (1, 4):
        sampling = add("Gate trajectory", ["mode", "n_points", "sweep_both_ways"],
                      description="All active sweep axes share Points / pass. Step is calculated from each axis range.")
        if any(scope == row_key and index == row_index and (key == "n_points" or key.startswith(("raw_", "derived_")))
               for scope, index, key in errors):
            sampling.update(samplingText="Sampling unavailable: correct the highlighted trajectory fields", samplingWarning=True)
        else:
            sampling["samplingText"] = gate_sampling(current)
        if current["mode"] == "Raw":
            for axis in ("vtg", "vbg", "vds"):
                if axis == "vds" and current["vds_source"] == "None":
                    continue
                active = current[f"raw_{axis}_active"]
                section = add(axis.replace("vt", "Vt").replace("vb", "Vb").replace("vd", "Vd") + (" sweep" if active else " fixed"),
                              [f"raw_{axis}_active", f"raw_{axis}_start"] + ([f"raw_{axis}_stop"] if active else []), columns=3 if active else 2)
                section["fields"][0]["label"] = "Sweep"
                section["fields"][1]["label"] = "Start (V)" if active else "Hold (V)"
                if active:
                    section["fields"][2]["label"] = "Stop (V)"
        else:
            axis = current["derived_axis"]
            held = "E-field" if axis == "Doping" else "Doping"
            section = add("Derived trajectory", ["derived_axis", "derived_fixed", "derived_start", "derived_stop", "derived_ratio", "derived_ratio_target"],
                description=(f"One point at {axis} Start; Stop is not visited. Hold {held} fixed." if current["n_points"] == 1 else
                             f"Sweep {axis} from Start to Stop while holding {held} fixed. Point count is set by Points / pass above."))
            for item in section["fields"]:
                if item["key"] == "derived_fixed":
                    item["label"] = f"Hold {held} (V)"
                elif item["key"] in ("derived_start", "derived_stop"):
                    item["label"] = f"{axis} {item['key'].split('_')[-1]} (V)"
            section["helpText"] = (
                ratio_formula_text(current["derived_ratio_target"]) + "\n"
                "Vtg = top-gate voltage; Vbg = back-gate voltage. "
                "r is a dimensionless weight on the selected gate, not a sweep rate.\n"
                "Doping and E-field here are weighted gate voltages in V, not calibrated carrier density or electric field. "
                "r must be non-zero; both physical gate voltage limits still apply."
            )
            section.update(previewText="", previewError="")
            try:
                if any((row_key, row_index, key) in errors for key in
                       ("derived_axis", "derived_fixed", "derived_start", "derived_stop", "derived_ratio", "derived_ratio_target")):
                    raise ValueError("Correct the highlighted coordinate fields to update the voltage preview.")
                points = []
                if current["n_points"] < 1:
                    raise ValueError("Points / pass must be at least 1")
                for endpoint in (("start",) if current["n_points"] == 1 else ("start", "stop")):
                    swept, fixed = current[f"derived_{endpoint}"], current["derived_fixed"]
                    doping, efield = (swept, fixed) if axis == "Doping" else (fixed, swept)
                    vtg, vbg = derived_to_gates(doping, efield, current["derived_ratio"], current["derived_ratio_target"])
                    if not all(math.isfinite(v) for v in (vtg, vbg)):
                        raise ValueError("Coordinate conversion must produce finite gate voltages.")
                    points.append(f"Requested {endpoint}: Vtg = {vtg:g} V; Vbg = {vbg:g} V")
                section["previewText"] = "\n".join(points)
            except (ValueError, OverflowError, ZeroDivisionError) as exc:
                section["previewError"] = str(exc)
            if current["vds_source"] != "None":
                add("Vds bias", ["derived_vds_mode"] + (["derived_vds_start", "derived_vds_stop"] if current["derived_vds_mode"] == "Swept" else ["derived_vds_fixed"]))
    elif mode == 2:
        derived = current["coordinate_mode"] == "Derived"
        add("Map trajectory", ["coordinate_mode", "axis_fast", "axis_slow"],
            description="Choose None for a 1D raw sweep; derived axes must be distinct.")
        axes = ("Doping", "E-field", "Vds") if derived else ("Vtg", "Vbg", "Vds")
        used = {current["axis_fast"], current["axis_slow"]}
        for axis in axes:
            prefix = {"E-field": "efield", "Doping": "doping"}.get(axis, axis.lower())
            swept = axis in used
            section = add(axis + (" range" if swept else " held"), [prefix+"_start"] + ([prefix+"_stop", prefix+"_step"] if swept else []), columns=3 if swept else 2)
            section["coordinate"] = axis
            if axis in ("Doping", "E-field"):
                section["description"] = ratio_formula_text(current["ratio_target"]).splitlines()[0 if axis == "Doping" else 1] + " (V)"
            for item in section["fields"]:
                item["label"] = (item["key"].rsplit("_", 1)[-1].capitalize() if swept else "Hold") + " (V)"
        add("Gate conversion", ["ratio", "ratio_target"], advanced=not derived,
            description="Doping / E-field are weighted gate voltages in V, not calibrated carrier density or electric field. r is a dimensionless weight on the selected gate and must be non-zero. Physical gate voltage limits still apply.")
    elif mode == 3:
        add("Wavelength trajectory", ["wl_start", "wl_stop", "wl_step"], columns=3)
        add("Drain bias", ["use_vds"] + (["vds_source", "ao_channel"] if current["use_vds"] and current["vds_source"] == "NI DAQ AO" else ["vds_source"] if current["use_vds"] else []))
        section, index = table("bias_conditions", "Bias recipe", "One spectrum / CSV per enabled condition.")
        if index >= 0:
            current = values["bias_conditions"][index]
            row_key, row_index = "bias_conditions", index
            section["fields"] = [field(k) for k in ("enabled", "vtg", "vbg", "vds", "settle_s") if k != "vds" or values["use_vds"]]
            add("Bias 2", bias2_keys(current), description=bias2_summary(current, current.get("vds") if values["use_vds"] else None))
            row_key, row_index, current = "", -1, values
    else:
        field_limit_text = ""
        if magnet_backend == "1000":
            from app.engine.bfield_transport_sweep import aps100_transport_field_limit
            try:
                field_limit_text = f"Start/Stop limit: ±{aps100_transport_field_limit():g} T. "
            except (TypeError, ValueError):
                field_limit_text = "Field limit unavailable; check magnet configuration. "
        add("Magnetic field trajectory", ["measurement_mode", "start_field_t", "stop_field_t", "round_trip"],
            description=(field_limit_text + "Requested maximum rate; the APS100 stored range limits can make actual rates slower. Continuous samples during motion; Stepped waits at each field." if magnet_backend == "1000" else "Stepped only; field ramp is managed by the SDK."))
        if magnet_backend == "1000":
            from app.engine.bfield_transport_sweep import effective_rate_limit_t_per_min
            try:
                limit = effective_rate_limit_t_per_min()
                limit_text = f"{limit * 10000 / 60:g} Oe/s ({limit:g} T/min)"
            except (ValueError, TypeError):
                limit_text = "unavailable; check magnet configuration"
            try:
                requested = float(current.get("rate_t_per_min", 0))
                equivalent = f"{requested * 10000 / 60:g} Oe/s"
            except (TypeError, ValueError):
                equivalent = "invalid input"
            add("Sweep rate", ["rate_t_per_min"], columns=1,
                description=f"Configured maximum: {limit_text}. Current request: {equivalent}. Stored RATE 0–4 and voltage limits may be slower; the final approach also slows down.")
        add("Sample B current reversal", ["sample_reversal_mode", "sample_reversal_settle_s"],
            description="Unchanged preserves B. Automatic modes require APS100 Stepped and A already ON; A is never changed. Per point: qualify a full ON temperature window, confirm OFF and wait, acquire, then confirm ON, wait and qualify a new ON window. Before/after ON temperatures must differ by no more than the configured temperature span before the point is saved. OFF readings remain safety monitored but are not mixed into ON windows. Whole sweep: B OFF with the existing target, span and drift checks. The configured wait does not certify transient removal. Monitoring or thermal interruptions end the run; B is restored after cleanup. If restoration fails, ownership is retained and Stop retries.")
        if current.get("measurement_mode") == "stepped":
            if magnet_backend == "1000":
                add("Hysteresis: set, wait, measure", list(BFIELD_HYSTERESIS_LABELS),
                    description="Stable offset points are measured at their readback field. Passed nominal points are logged, never revisited. History reversal, endpoint or actual-gap violations end the run. Gap 0 means twice the largest planned interval. Field and temperature have separate timeouts; protection holds still require review. Commission these limits on the magnet.")
            step_section = add("Stepped field and temperature", ["field_step_t", "field_points", "temperature_target_k"],
                description=("All points use one sample target; passive waiting only." if magnet_backend == "1000" else "SDK temperature control stays enabled at one sample target throughout the sweep.") + (" Temperature timeout ends the run; protection holds require review." if magnet_backend == "1000" else " Timeout pauses the run."))
            stability_section = add("Temperature stability and timeout", ["temperature_tolerance_k", "temperature_window_s", "temperature_span_k", "temperature_drift_k_per_min", "temperature_timeout_s"],
                description="Temperature qualification limits remain active for every point.")
            if magnet_backend == "1000":
                for item in stability_section["fields"]:
                    if item["key"] == "temperature_timeout_s":
                        item["label"] = "Temperature qualification timeout (s)"
            try:
                count, text = transport_sampling_summary(current, magnet_backend)
                for item in step_section["fields"]:
                    if item["key"] == "field_points":
                        item["value"] = count
                step_section.update(samplingText=text, samplingWarning=False)
            except ValueError as exc:
                for item in step_section["fields"]:
                    if item["key"] == "field_points":
                        item["value"] = "—"
                step_section.update(samplingText=str(exc), samplingWarning=True)
        conversion = add("Shared gate conversion", ["ratio", "ratio_target"],
            description="Choose Vtg / Vbg to enter physical gate voltages, or Doping / E-field to enter weighted gate voltages. Switching views keeps the same gate setpoints. "
                        + ("Changing r keeps Vtg / Vbg fixed in this view." if transport_coordinates == "Raw" else "One ratio applies to all conditions."))
        conversion["fields"].insert(0, dict(key="transport_coordinates", label="Fixed gate inputs", kind="choice", value=transport_coordinates,
            options=[dict(value="Raw", label="Vtg / Vbg"), dict(value="Derived", label="Doping / E-field")], columnSpan=2))
        section, index = table("conditions", "Fixed bias conditions")
        if index >= 0:
            current = deepcopy(values["conditions"][index])
            row_key, row_index = "conditions", index
            current["ratio"], current["ratio_target"] = values["ratio"], values["ratio_target"]
            error = ""
            try:
                current["vtg"], current["vbg"] = derived_to_gates(current["doping"], current["efield"], current["ratio"], current["ratio_target"])
            except ValueError as exc:
                current["vtg"], current["vbg"] = "Unavailable", "Unavailable"
                error = str(exc)
            keys = ["enabled", "name", "doping", "efield", "vds", "vds_source"]
            if current["vds_source"] == "NI DAQ AO":
                keys.append("ao_channel")
            keys += ["settle_s", "vtg", "vbg"]
            keys += [key for key in bias2_keys(current) if key in current]
            section["fields"] = [field(k) for k in keys]
            for item in section["fields"]:
                if item["key"] in ("vtg", "vbg"):
                    item.update(readOnly=transport_coordinates != "Raw", description="Calculated from shared ratio" if transport_coordinates != "Raw" else "Physical gate setpoint", error=error)
                elif item["key"] in ("doping", "efield") and transport_coordinates == "Raw":
                    item.update(readOnly=True, description="Calculated from Vtg / Vbg")
            row_key, row_index, current = "", -1, values
        if magnet_backend == "1000":
            add("Cooldown and completion", ["cooldown_policy", "final_mode"], advanced=True,
                description="Successful completion stays driven (heater ON). Stop, error and interlock retain persistent cleanup.")

    if mode in (0, 1, 2, 4):
        add("Vds source", ["vds_source"] + (["ao_channel"] if current["vds_source"] == "NI DAQ AO" else []))
        add("Bias 2", bias2_keys(current), description=bias2_summary(current))
    add("Acquisition", ["n_sample", "delay", "averages", "acquisition_delay_s"])
    ramp_keys = ["vg_ramp"] + (["vds_ramp"] if current.get("vds_source") != "None" and (mode != 3 or values["use_vds"]) else [])
    if mode in (1, 4) and current["mode"] == "Raw":
        ramp_keys += ["derived_ratio", "derived_ratio_target"]
    add("Advanced ramps and conversion", ramp_keys, advanced=True)
    file_output = add("File output", ["base_name"], columns=1)
    sections.remove(file_output)
    sections.insert(0, file_output)
    add("Plot settings", ["plot_choice", "plot_x_axis"], advanced=True)
    if mode == 4:
        row_key, row_index, current = "", -1, values
        add("Resume batch", ["resume_checkpoint"], advanced=True, columns=1,
            description="Resume uses the existing field-batch checkpoint and validation.")
    if mode == 2:
        available = not derived and {values["axis_fast"], values["axis_slow"]} == {"Vtg", "Vbg"}
        section, index = table("regions", "Additional gate regions",
            "The ranges above define Region 1. Add rectangles to form an L-shaped or stepped union. Shared points are acquired once; disconnected regions are rejected." if available else
            "Multiple rectangles are supported for Raw Vtg/Vbg maps. Choose Use Vtg / Vbg regions below to configure these axes; this does not start acquisition.")
        section.update(editable=available, configureRegions=not available)
        if index >= 0:
            row_key, row_index, current = "regions", index, values["regions"][index]
            section["fields"] = [field(k) for k in REGION_DEFAULT]
        sections.remove(section)
        sections.insert(next(i for i, item in enumerate(sections) if item["title"] == "Gate conversion"), section)
    # Keep the operational trajectory and bias controls open. Less frequently
    # edited detail remains available by expanding its section; this flag is
    # presentation-only and never changes the underlying recipe.
    collapsed = {
        0: {"Acquisition"},
        1: {"Acquisition"},
        2: {"Acquisition", "Additional gate regions", "Gate conversion"},
        3: {"Acquisition"},
        4: {"Acquisition"},
        5: {"Acquisition", "Shared gate conversion", "Sample B current reversal",
            "Hysteresis: set, wait, measure", "Temperature stability and timeout"},
    }.get(mode, set())
    for section in sections:
        if section["title"] in collapsed or section.get("advanced"):
            if section["title"] in collapsed:
                section["collapsedByDefault"] = True
            parts = []
            for item in section.get("fields", []):
                value = item.get("value")
                if isinstance(value, bool):
                    shown = "On" if value else "Off"
                elif isinstance(value, (int, float)):
                    shown = f"{value:g}"
                elif isinstance(value, str) and len(value) <= 28:
                    shown = value
                else:
                    continue
                parts.append(f"{item.get('label', item['key'])}: {shown}")
                if len(parts) == 2:
                    break
            if parts:
                section["summary"] = " · ".join(parts)
    return sections


def convert(value, default, key, mode):
    if isinstance(default, bool):
        if isinstance(value, bool):
            return value
        if value in (0, 1, "true", "false", "True", "False"):
            return value in (1, "true", "True")
        raise ValueError(f"{label(key)} must be true or false")
    if isinstance(default, (int, float)):
        number = float(value)
        if not math.isfinite(number):
            raise ValueError(f"{label(key)} must be finite")
        if isinstance(default, int):
            if number != int(number):
                raise ValueError(f"{label(key)} must be an integer")
            return int(number)
        return number
    if isinstance(default, list):
        if not isinstance(value, list):
            raise ValueError(f"{label(key)} must be rows")
        if len(value) > 100:
            raise ValueError("At most 100 conditions or regions are allowed")
        example = row_default(key, mode)
        rows = []
        for row in value:
            if not isinstance(row, dict) or any(k not in example for k in row):
                raise ValueError("Invalid condition columns")
            rows.append({k: convert(row.get(k, v), v, k, mode) for k, v in example.items()})
        return rows
    text = str(value)
    options = descriptor(key, default, mode).get("options")
    if options and text not in options:
        raise ValueError(f"{label(key)} must be one of {', '.join(options)}")
    return text


def build_params(values, mode):
    cls = MODES[mode][2]
    values = deepcopy(values)
    if mode == 3:
        values["bias_conditions"] = [PhotocurrentBiasCondition(**v) for v in values["bias_conditions"]]
    if mode == 5:
        values["conditions"] = [BFieldTransportCondition(**v) for v in values["conditions"]]
        values["final_mode"] = "driven"
    return cls(**{f.name: values[f.name] for f in fields(cls) if f.name in values})


def legacy_values(settings, mode):
    """Read the established Widgets keys without constructing the old tabs.

    This is a read-only migration. Quick writes its own schema; old controls
    and their remembered choices remain available for rollback.
    """
    prefix = ("tabs/vds_sweep", "tabs/gate_scan", "tabs/map_2d", "tabs/photocurrent",
              "tabs/bfield_gate_scan", "tabs/bfield_transport")[mode]
    result = defaults(mode)
    aliases = {"vds_source": "source", "plot_choice": "plot_y", "plot_x_axis": "plot_x",
               "n_sample": "averages", "sweep_both_ways": "sweep_bidirectional"}
    if mode == 0:
        aliases.update(vtg_set="vtg", vbg_set="vbg")
    if mode == 2:
        aliases.update(coordinate_mode="coordinates", axis_fast="fast_axis", axis_slow="slow_axis")
    if mode == 3:
        aliases.update(vds_set="manual_vds")
    if mode == 4:
        aliases.update(fields_t="bfield_fields")
    if mode == 5:
        aliases.update(acquisition_delay_s="delay_s")

    def read(key):
        return settings.value(f"{prefix}/{key}", None)

    for key, default in result.copy().items():
        if (key in HIDDEN and key != "field_spacing") or key == "final_mode":
            continue
        value = read(aliases.get(key, key))
        if value is None:
            continue
        if key == "vds_source":
            match = re.fullmatch(r"NI DAQ (?:AO |ao)?(\d+)", str(value))
            if match:
                result["ao_channel"] = int(match.group(1))
                value = "NI DAQ AO"
        if key in ("ratio_target", "derived_ratio_target"):
            value = "Vtg" if "Vtg" in str(value) else "Vbg"
        if key == "coordinate_mode":
            value = "Derived" if str(value) in ("Derived", "Doping / E-field") else "Raw"
        if isinstance(default, list) and isinstance(value, str):
            try:
                value = json.loads(value)
            except (TypeError, ValueError):
                continue
        if key == "plot_x_axis" and value == "Auto":
            value = "Follow Sweep"
        try:
            result[key] = convert(value, default, key, mode)
        except (TypeError, ValueError, OverflowError):
            continue
    if mode in (1, 4):
        if str(read("mode_derived")).lower() in ("true", "1"):
            result["mode"] = "Derived"
        if str(read("derived_axis_efield")).lower() in ("true", "1"):
            result["derived_axis"] = "E-field"
        if str(read("derived_vds_swept_mode")).lower() in ("true", "1"):
            result["derived_vds_mode"] = "Swept"
    if mode == 2:
        result["mode"] = "Linked" if str(read("link_doping_efield")).lower() in ("true", "1") else "Grid"
        derived = result["coordinate_mode"] == "Derived"
        for key, fallback in (("axis_fast", "Doping" if derived else "Vtg"), ("axis_slow", "E-field" if derived else "Vbg")):
            specific = read(("derived_" if derived else "raw_") + ("fast_axis" if key == "axis_fast" else "slow_axis"))
            if specific is not None:
                result[key] = str(specific)
            if derived and result[key] not in ("Doping", "E-field", "Vds"):
                result[key] = fallback
        if str(read("sweep_dim")) == "1D sweep":
            result["axis_slow"] = "None"
        if result["axis_fast"] == result["axis_slow"]:
            result["axis_slow"] = "E-field" if derived else "Vbg" if result["axis_fast"] != "Vbg" else "Vtg"
    if mode == 5:
        rows = result["conditions"]
        ratio = read("ratio")
        target = read("ratio_target")
        if rows:
            if ratio is None:
                result["ratio"] = rows[0]["ratio"]
            if target is None:
                result["ratio_target"] = rows[0]["ratio_target"]
            for row in rows:
                row["ratio"], row["ratio_target"] = result["ratio"], result["ratio_target"]
        result["final_mode"] = "driven"
    return result
