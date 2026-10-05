"""Scientific PNG captions from saved conditions; no GUI or instrument access."""
from __future__ import annotations

from pathlib import Path
from collections import Counter
import json
import re

from app.curve_history import MEASUREMENT_TYPES
from app.saved_conditions import quantity
from app.analysis_signals import numeric


def conditions(record, excluded=()):
    """Return labeled values without assuming missing telemetry equals zero."""
    meta = record.get("metadata", {})
    params = meta.get("params", {})
    params = params if isinstance(params, dict) else {}
    result = {}
    name = params.get("base_name")
    if not isinstance(name, str) or not name.strip():
        name = re.sub(r"_\d{8}_\d{6}(?:_\d+)?$", "", Path(record.get("path", "")).stem)
    if isinstance(name, str) and name.strip():
        result["Name"] = name.strip()
    for name, prefix in (("Vtg", "vtg"), ("Vbg", "vbg"), ("Vds", "vds")):
        if params.get("coordinate_mode") == "Derived" and name in {"Vtg", "Vbg"}:
            continue
        value = None
        if prefix + "_set" in params and meta.get("measurement") not in {"map_2d", "sweep_1d"}:
            value = quantity(params[prefix + "_set"], "V")
        elif params.get("mode") != "Derived" and "raw_" + prefix + "_start" in params:
            start = params["raw_" + prefix + "_start"]
            stop = params.get("raw_" + prefix + "_stop", start) if params.get("raw_" + prefix + "_active") else start
            value = quantity(start, "V") if start == stop else f"{quantity(start, 'V')} → {quantity(stop, 'V')}"
        elif prefix + "_start" in params:
            start, stop = params[prefix + "_start"], params.get(prefix + "_stop", params[prefix + "_start"])
            value = quantity(start, "V") if start == stop else f"{quantity(start, 'V')} → {quantity(stop, 'V')}"
        elif prefix == "vds" and "derived_vds_fixed" in params:
            if params.get("derived_vds_mode") == "Sweep":
                value = f"{quantity(params.get('derived_vds_start'), 'V')} → {quantity(params.get('derived_vds_stop'), 'V')}"
            else:
                value = quantity(params["derived_vds_fixed"], "V")
        if value:
            result[name] = value
    if params.get("mode") == "Derived":
        axis = params.get("derived_axis", "Doping")
        other = "E-field" if axis == "Doping" else "Doping"
        if "derived_fixed" in params:
            result[other] = quantity(params["derived_fixed"], "V")
    for name, value in meta.get("csv_conditions", {}).items():
        if value:
            result[name] = value
    batch = meta.get("gate_scan_bfield_batch", {})
    if "verified_field_t" in batch and "B" not in result:
        result["B"] = quantity(batch["verified_field_t"], "T")
    context = meta.get("experiment_context", {})
    for key in ("temperature", "temperature2100"):
        snapshot = context.get(key, {})
        values = snapshot.get("values", {}) or {}
        if snapshot.get("available") and "sample_temperature_k" in values and "T (start)" not in result:
            result["T (start)"] = quantity(values["sample_temperature_k"], "K")
    for key in ("magnet1000", "magnet2100"):
        snapshot = context.get(key, {})
        values = snapshot.get("values", {}) or {}
        if snapshot.get("available") and "field_t" in values and "B" not in result:
            result["B (start)"] = quantity(values["field_t"], "T")
    chain = meta.get("signal_chain") or meta.get("validation", {}).get("calibration", {}).get("signal_chain", {})
    if isinstance(chain, dict):
        # Native sidecars hoist the instrument snapshot to the JSON root;
        # older files keep it inside signal_chain.
        if isinstance(meta.get("lockin_settings"), dict):
            chain = {**chain, "lockin_settings": meta["lockin_settings"]}
        if chain.get("ac_contact"):
            result["Contact"] = str(chain["ac_contact"])
        result.update(_signal_settings(chain))
    return {key: value for key, value in result.items() if value and key not in excluded}


def _signal_settings(chain):
    result = {}
    for key, label, unit in (("preamp_gain_v_per_a", "Preamp gain", "V/A"),
                             ("ac_voltage_ratio", "AC voltage ratio", "")):
        if key in chain:
            result[label] = quantity(chain[key], unit)
    sensitivity = numeric(chain.get("preamp_sensitivity_a"))
    if "preamp_gain_v_per_a" not in chain and sensitivity > 0:
        result["Preamp gain"] = quantity(1. / sensitivity, "V/A")
    dual = chain.get("drag_drive") or {}
    instruments = dual.get("instruments") or {}
    sources = [("", chain.get("lockin_settings") or {})]
    if dual.get("enabled"):
        sources = [("Drag ", chain.get("lockin_settings") or instruments.get("drag") or {}),
                   ("Drive ", instruments.get("drive") or {})]
        result["Series R"] = quantity(dual.get("series_resistance_ohm"), "Ω")
        result["Drive preamp gain"] = quantity(dual.get("drive_preamp_gain"))
    for prefix, snapshot in sources:
        values = snapshot.get("values") or {}
        for key, label, unit in (("phase_deg", "Phase", "°"), ("sine_out_v", "Amplitude", "V"),
                                 ("frequency_hz", "f", "Hz"), ("harmonic", "Harmonic", ""),
                                 ("reserve_level", "Reserve level", "")):
            if values.get(key) is not None:
                result[prefix + label] = quantity(values[key], unit)
        for key, label in (("sensitivity", "Sensitivity"), ("time_constant", "Time constant"),
                           ("filter_slope", "Filter slope"), ("ref_source", "Reference"),
                           ("input_config", "Input"), ("input_ground", "Ground"),
                           ("input_coupling", "Coupling"), ("line_filter", "Line filter"),
                           ("reserve", "Reserve"), ("current_gain", "Current gain")):
            if values.get(key + "_label"):
                result[prefix + label] = str(values[key + "_label"])
            elif values.get(key) is not None:
                result[prefix + label] = f"code {values[key]}"
        if prefix != "Drive ":
            result.setdefault(prefix + "f", quantity(chain.get("frequency_hz"), "Hz"))
            result.setdefault(prefix + "Sensitivity", quantity(chain.get("lockin_sensitivity_v"), "V"))
    return result


def _parameter_settings(metadata):
    """Additional saved scan parameters; output bookkeeping is not a setting."""
    params = metadata.get("params") or {}
    if not isinstance(params, dict):
        return {}
    handled = {f"{prefix}_{suffix}" for prefix in ("vtg", "vbg", "vds", "raw_vtg", "raw_vbg", "raw_vds")
               for suffix in ("start", "stop", "set")}
    handled.update(("base_name", "derived_fixed", "derived_vds_fixed", "derived_vds_start", "derived_vds_stop"))
    titles = {"n_sample": "Samples", "delay_s": "Delay (s)", "settle_s": "Settle (s)", "dwell_s": "Dwell (s)"}
    result = {}
    for key, value in params.items():
        if key in handled or key.startswith(("output_", "filename")) or key.endswith("_path") or value is None:
            continue
        if isinstance(value, bool):
            formatted = "on" if value else "off"
        elif isinstance(value, (int, float)):
            formatted = quantity(value)
        elif isinstance(value, (list, dict)):
            formatted = json.dumps(value, sort_keys=True, ensure_ascii=False)
        else:
            formatted = str(value).strip()
        if formatted:
            result[titles.get(key, key.replace("_", " ").capitalize())] = formatted
    return result


def comparison_setting_signature(metadata):
    return (tuple(conditions({"metadata": metadata}).items()), tuple(_parameter_settings(metadata).items()))


def _condition_groups(records, excluded=()):
    per_record = [conditions(record, excluded) for record in records]
    parameters = [_parameter_settings(record.get("metadata", {})) for record in records]
    varying = [key for key in dict.fromkeys(key for values in parameters for key in values)
               if any(values.get(key) != parameters[0].get(key) for values in parameters[1:])]
    for condition, values in zip(per_record, parameters):
        condition.update({key: values.get(key, "unknown") for key in varying})
    names = [condition.get("Name", "") for condition in per_record]
    if len(set(names)) > 1:
        tokens = [re.split(r"[_\s]+", name) if name else [] for name in names]
        shared = Counter(tokens[0])
        for words in tokens[1:]:
            shared &= Counter(words)
        for condition, name, words in zip(per_record, names, tokens):
            remaining, changed = shared.copy(), []
            for word in words:
                if remaining[word]:
                    remaining[word] -= 1
                else:
                    changed.append(word)
            if name:
                condition["Name"] = "_".join(changed) or name
        # An empty remainder falls back to the original name. If that collides
        # with another shortened name, retain both originals to keep them distinct.
        aliases = {}
        for condition, name in zip(per_record, names):
            aliases.setdefault(condition.get("Name", ""), set()).add(name)
        if any(len(originals) > 1 for originals in aliases.values()):
            for condition, name in zip(per_record, names):
                if name:
                    condition["Name"] = name
    common = {key: value for key, value in per_record[0].items()
              if all(condition.get(key) == value for condition in per_record[1:])} if per_record else {}
    keys = dict.fromkeys(key for condition in per_record for key in condition if key not in common)
    differences = {record["path"]: {key: condition.get(key, "unknown") for key in keys}
                   for record, condition in zip(records, per_record)}
    return common, differences


def comparison_labels(records, traces, excluded=()):
    """Use the same difference-only labels on screen and in exported figures."""
    paths = {trace["path"] for trace in traces}
    records = [record for record in records if record["path"] in paths]
    _common, differences = _condition_groups(records, excluded)
    by_path = {record["path"]: record for record in records}
    directions = {trace.get("direction", "unknown") for trace in traces}
    groups = {trace.get("condition", "") for trace in traces}
    return [trace_label({**trace,
                         "direction": trace.get("direction", "unknown") if len(directions) > 1 else "All",
                         "condition": trace.get("condition", "") if len(groups) > 1 else ""},
                        by_path[trace["path"]], differences, False) for trace in traces]


def identity(record):
    meta = record.get("metadata", {})
    device = meta.get("save_root", {}).get("device_id", "")
    description = meta.get("params", {}).get("base_name", "")
    return str(device or ""), str(description or Path(record["path"]).stem)


_HEADING_LABELS = {
    "T (start)": "T₀", "B": "B", "B (start)": "B₀", "Contact": "AC",
    "Vtg": "Vtg", "Vbg": "Vbg", "Vds": "Vds", "Doping": "Doping", "E-field": "E",
    "f": "f", "Amplitude": "Vac", "Phase": "φ",
    "Drag f": "Drag f", "Drag Amplitude": "Drag Vac", "Drag Phase": "Drag φ",
    "Drive f": "Drive f", "Drive Amplitude": "Drive Vac", "Drive Phase": "Drive φ",
    "Series R": "Rs", "AC voltage ratio": "AC ratio",
}


def _compact_value(key, value):
    # Round only the displayed instrument summary, after comparing full saved
    # conditions. Axes, actual cut coordinates and difference labels stay precise.
    short_key = key.removeprefix("Drag ").removeprefix("Drive ")
    if short_key in {"f", "Amplitude", "Phase", "Series R", "AC voltage ratio"}:
        match = re.fullmatch(r"([+−\-\d.eE]+)(?:\s+(.+))?", value)
        if match:
            compact = quantity(match[1].replace("−", "-"), match[2] or "", precision=4, engineering=True)
            if compact:
                value = compact
    return value.replace(" °", "°")


def _compact_conditions(values):
    if values.get('Drag f') and values.get('Drive f') and _compact_value('Drag f', values['Drag f']) == _compact_value('Drive f', values['Drive f']):
        values = {**values, 'f': values['Drag f']}
        values.pop('Drag f')
        values.pop('Drive f')
    return " · ".join(f"{label}={_compact_value(key, values[key])}"
                      for key, label in _HEADING_LABELS.items() if values.get(key))


def comparison_heading(records, signal, excluded=(), primary_path=""):
    """Concise scientific context, with primary-only settings labeled explicitly."""
    if not records:
        return "", ""
    primary = next((record for record in records if record["path"] == primary_path), None)
    source = primary or records[0]
    device, _description = identity(source)
    name = conditions(source).get("Name", "").replace("_", " · ")
    if primary or len(records) == 1:
        title = " · ".join(filter(None, (device, name, signal)))
        if primary:
            title = "Map: " + title
    else:
        devices = list(dict.fromkeys(identity(record)[0] for record in records if identity(record)[0]))
        title = " · ".join(filter(None, (" / ".join(devices), signal, f"{len(records)} runs")))
    common, _differences = _condition_groups(records, excluded)
    detail = _compact_conditions(common)
    if len(records) > 1:
        detail = "Shared: " + detail if detail else ""
        if primary:
            selected = conditions(primary, excluded)
            unique = _compact_conditions({key: value for key, value in selected.items() if key not in common})
            if unique:
                detail = "\n".join(filter(None, (detail, "Map only: " + unique)))
    return title, detail


def captions(snapshot, automatic=False):
    records = snapshot["records"]
    devices = list(dict.fromkeys(identity(record)[0] for record in records if identity(record)[0]))
    device = "/".join(devices) or "Saved data"
    kind = MEASUREMENT_TYPES.get(snapshot["kind"], snapshot["kind"])
    title = f"{device} · {identity(records[0])[1]} · {snapshot['signal']}" if automatic else f"{device} · {kind} comparison · {snapshot['signal']} · {len(records)} runs"
    excluded = (snapshot["x_name"],)
    if snapshot.get("map"):
        excluded += (snapshot["map"]["x_name"], snapshot["map"]["y_name"])
    common, differences = _condition_groups(records, excluded)
    detail = _compact_conditions(common)
    view = snapshot["view"]
    if not automatic:
        title, detail = comparison_heading(records, snapshot["signal"], excluded,
                                           snapshot.get("map", {}).get("path", ""))
    if snapshot.get("map") and view.get("fixed_value") is not None:
        unit = snapshot["map"]["y_unit"] if view["fixed_axis"] == snapshot["map"]["y_name"] else snapshot["map"]["x_unit"]
        detail = " · ".join(filter(None, (f"Cut: {view['fixed_axis']}={quantity(view['fixed_value'], unit)}", detail)))
    statuses = sorted({str(record.get("status", "unknown")) for record in records} - {"finished", "complete", "completed"})
    if statuses:
        status = "/".join("Stopped / Partial" if s == "stopped" else "Failed / Partial" if s in {"error", "failed"} else s.title() for s in statuses)
        title += f" · {status}"
    if automatic:
        differences = {}
    return title, detail, differences


def trace_label(trace, record, differences, automatic):
    direction = trace.get("direction", "unknown")
    if automatic:
        label = direction.title()
    else:
        different = differences.get(record["path"], {})
        label = "; ".join(f"{key} = {value}" for key, value in different.items())
        if not label or sum(values == different for values in differences.values()) > 1:
            run = list(differences).index(record["path"]) + 1
            label = " · ".join(filter(None, (f"Run {run}", label)))
        if direction not in ("All", "unknown"):
            label += " · " + direction
    if trace.get("condition"):
        label += " · " + trace["condition"]
    return label
