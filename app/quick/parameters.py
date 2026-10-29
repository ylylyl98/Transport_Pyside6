"""UI independent parameter descriptions and strict conversion for QML forms."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, fields
import math
import json
import re

from app.models import (DualGateParams, LineSweepParams, CoParams, PhotocurrentParams,
                        PhotocurrentBiasCondition, BFieldTransportParams,
                        BFieldTransportCondition)

MODES = [("vds_sweep", "Vds Sweep", DualGateParams),
         ("gate_scan", "Gate Scan", LineSweepParams),
         ("map", "2D Map", CoParams),
         ("photocurrent", "Photocurrent", PhotocurrentParams),
         ("bfield_gate_scan", "B-field Gate Scan", LineSweepParams),
         ("bfield_transport", "B-field Sweep", BFieldTransportParams)]

CHOICES = {
    "vds_source": ["Keithley 2400", "NI DAQ AO", "None"],
    "plot_choice": ["Ids_DC", "Ids_X", "Ids_Y", "Ids_Keithley", "I_drag_X", "I_drag_Y", "I_drag_R", "I_drive_X", "I_drive_Y", "I_drive_R"],
    "plot_x_axis": ["Follow Sweep", "Step Index", "Vtg", "Vbg", "Vds", "Doping", "E-field"],
    "ratio_target": ["Vbg", "Vtg"], "derived_ratio_target": ["Vbg", "Vtg"],
    "derived_axis": ["Doping", "E-field"], "derived_vds_mode": ["Fixed", "Swept"],
    "coordinate_mode": ["Raw", "Derived"], "inner": ["Vtg", "Vbg"],
    "axis_fast": ["Vtg", "Vbg", "Vds", "Doping", "E-field"],
    "axis_slow": ["None", "Vtg", "Vbg", "Vds", "Doping", "E-field"],
    "derived_fast_axis": ["Doping", "E-field", "Vds"],
    "derived_slow_axis": ["Doping", "E-field", "Vds"],
    "cooldown_policy": ["adaptive", "stay_driven", "persistent_each_row"], "final_mode": ["driven"],
}
HIDDEN = {"output_csv_path", "output_metadata_path", "output_log_path", "plot_x_resolved"}
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
    special = {"n_sample": "Averages", "n_points": "Number of points", "delay": "Acquisition delay (s)",
               "vg_ramp": "Gate ramp step (V)", "vds_ramp": "Vds ramp step (V)",
               "fields_t": "B-field targets (T)", "resume_checkpoint": "Resume checkpoint path",
               "wl_start": "Start (nm)", "wl_stop": "Stop (nm)", "wl_step": "Step (nm)",
               "start_field_t": "Start (T)", "stop_field_t": "Stop (T)",
               "rate_t_per_min": "Sweep rate (T/min)", "settle_s": "Settle time (s)",
               "acquisition_delay_s": "Acquisition delay (s)", "averages": "Averages",
               "derived_ratio": "Coupling ratio r", "ratio": "Coupling ratio r",
               "ratio_target": "r multiplies", "derived_ratio_target": "r multiplies",
               "coordinate_mode": "Coordinates", "mode": "Trajectory", "axis_fast": "Fast axis",
               "axis_slow": "Slow axis / dimension", "sweep_both_ways": "Forward + reverse",
               "round_trip": "Return to start field", "use_vds": "Apply Vds", "base_name": "File base name",
               "n_points": "Points / pass", "derived_axis": "Sweep coordinate",
               "derived_start": "Start (derived V)", "derived_stop": "Stop (derived V)",
               "derived_fixed": "Held coordinate (derived V)", "derived_vds_mode": "Vds trajectory"}
    text = key.replace("_", " ")
    text = text[:1].upper() + text[1:]
    for old, new in (("vtg", "Vtg"), ("vbg", "Vbg"), ("vds", "Vds"), ("efield", "E-field")):
        text = text.replace(old, new).replace(old.capitalize(), new)
    if key in special:
        return special[key]
    if any(key.startswith(p) for p in ("vtg", "vbg", "vds", "raw_v", "derived_vds")) and not key.endswith(("active", "source", "mode")):
        text += " (V)"
    if key.startswith(("doping", "efield")):
        text += " (derived V)"
    return text


def row_default(key, mode):
    if key == "bias_conditions":
        return asdict(PhotocurrentBiasCondition())
    if key == "regions":
        return deepcopy(REGION_DEFAULT)
    if mode == 4:
        return {"enabled": True, "name": "Condition", **{k: v for k, v in asdict(LineSweepParams()).items() if k not in HIDDEN}}
    return asdict(BFieldTransportCondition())


def descriptor(key, value, mode):
    group = ("Output and plot" if key.startswith("plot_") or key == "base_name" else
             "Acquisition and ramps" if key in {"delay", "n_sample", "averages", "acquisition_delay_s", "vds_ramp", "vg_ramp"} else
             "Recipes" if key in {"conditions", "bias_conditions", "regions"} else
             "Magnet" if key in {"fields_t", "resume_checkpoint", "start_field_t", "stop_field_t", "rate_t_per_min", "round_trip", "cooldown_policy", "final_mode"} else
             "Trajectory and biases")
    result = {"key": key, "label": label(key), "value": deepcopy(value), "group": group}
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
    return result


def schema(values, mode, errors=None):
    errors = errors or {}
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


def measurement_sections(values, mode, selected=None, errors=None):
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
        add("Gate trajectory", ["mode", "n_points", "sweep_both_ways"])
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
            add("Derived trajectory", ["derived_axis", "derived_fixed", "derived_start", "derived_stop", "derived_ratio", "derived_ratio_target"],
                description="Doping / E-field are voltage combinations; physical gate limits still apply.")
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
            for item in section["fields"]:
                item["label"] = (item["key"].rsplit("_", 1)[-1].capitalize() if swept else "Hold") + (" (derived V)" if axis in ("Doping", "E-field") else " (V)")
        add("Gate conversion", ["ratio", "ratio_target"], advanced=not derived)
    elif mode == 3:
        add("Wavelength trajectory", ["wl_start", "wl_stop", "wl_step"], columns=3)
        add("Drain bias", ["use_vds"] + (["vds_source", "ao_channel"] if current["use_vds"] and current["vds_source"] == "NI DAQ AO" else ["vds_source"] if current["use_vds"] else []))
        section, index = table("bias_conditions", "Bias recipe", "One spectrum / CSV per enabled condition.")
        if index >= 0:
            current = values["bias_conditions"][index]
            row_key, row_index = "bias_conditions", index
            section["fields"] = [field(k) for k in ("enabled", "vtg", "vbg", "vds", "settle_s") if k != "vds" or values["use_vds"]]
            row_key, row_index, current = "", -1, values
    else:
        add("Magnetic field trajectory", ["start_field_t", "stop_field_t", "rate_t_per_min", "round_trip"])
        add("Shared gate conversion", ["ratio", "ratio_target"], description="One ratio for all conditions; gate voltages are calculated.")
        section, index = table("conditions", "Fixed bias conditions")
        if index >= 0:
            current = deepcopy(values["conditions"][index])
            row_key, row_index = "conditions", index
            current["ratio"], current["ratio_target"] = values["ratio"], values["ratio_target"]
            error = ""
            try:
                from app.gate_transform import derived_to_gates
                current["vtg"], current["vbg"] = derived_to_gates(current["doping"], current["efield"], current["ratio"], current["ratio_target"])
            except ValueError as exc:
                current["vtg"], current["vbg"] = "Unavailable", "Unavailable"
                error = str(exc)
            keys = ["enabled", "name", "doping", "efield", "vds", "vds_source"]
            if current["vds_source"] == "NI DAQ AO":
                keys.append("ao_channel")
            keys += ["settle_s", "vtg", "vbg"]
            section["fields"] = [field(k) for k in keys]
            for item in section["fields"]:
                if item["key"] in ("vtg", "vbg"):
                    item.update(readOnly=True, description="Calculated from shared ratio", error=error)
            row_key, row_index, current = "", -1, values
        add("Cooldown and completion", ["cooldown_policy", "final_mode"], advanced=True,
            description="Successful completion stays driven (heater ON). Stop, error and interlock retain persistent cleanup.")

    if mode in (0, 1, 2, 4):
        add("Vds source", ["vds_source"] + (["ao_channel"] if current["vds_source"] == "NI DAQ AO" else []))
    add("Acquisition", ["n_sample", "delay", "averages", "acquisition_delay_s"])
    ramp_keys = ["vg_ramp"] + (["vds_ramp"] if current.get("vds_source") != "None" and (mode != 3 or values["use_vds"]) else [])
    if mode in (1, 4) and current["mode"] == "Raw":
        ramp_keys += ["derived_ratio", "derived_ratio_target"]
    add("Advanced ramps and conversion", ramp_keys, advanced=True)
    add("Output and plot", ["base_name", "plot_choice", "plot_x_axis"], advanced=True)
    if mode == 4:
        row_key, row_index, current = "", -1, values
        add("Resume batch", ["resume_checkpoint"], advanced=True, columns=1,
            description="Resume uses the existing field-batch checkpoint and validation.")
    if mode == 2 and (values["regions"] or (not derived and {values["axis_fast"], values["axis_slow"]} == {"Vtg", "Vbg"})):
        section, index = table("regions", "Additional gate regions", "Regions merge into the primary map; duplicate coordinates are acquired once.", advanced=True)
        if index >= 0:
            row_key, row_index, current = "regions", index, values["regions"][index]
            section["fields"] = [field(k) for k in REGION_DEFAULT]
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
        if key in HIDDEN or key == "final_mode":
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
