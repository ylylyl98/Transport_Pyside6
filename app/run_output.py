from __future__ import annotations

import datetime
import json
import os
import re
import threading
from dataclasses import asdict, dataclass, is_dataclass
from typing import Iterable

from app.models import SaveRoot
from app.utils import _sanitize_base


@dataclass(frozen=True)
class PlannedOutput:
    run_id: str
    output_dir: str
    stem: str
    csv_path: str
    metadata_path: str
    log_path: str

    @property
    def csv_name(self) -> str:
        return os.path.basename(self.csv_path)

    @property
    def display_stem(self) -> str:
        """Stem with the trailing run_id removed, for preview display."""
        suffix = f"_{self.run_id}"
        return self.stem[: -len(suffix)] if self.stem.endswith(suffix) else self.stem


_run_id_lock = threading.Lock()
_run_id_counts: dict[str, int] = {}


def new_run_id() -> str:
    """Readable timestamp with a sequence for runs created in the same second."""
    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    with _run_id_lock:
        count = _run_id_counts.get(stamp, 0) + 1
        _run_id_counts[stamp] = count
    return stamp if count == 1 else f"{stamp}_{count:02d}"


def planned_output_at(output_dir: str, stem: str, run_id: str) -> PlannedOutput:
    return PlannedOutput(run_id, output_dir, stem,
                         os.path.join(output_dir, stem + ".csv"),
                         os.path.join(output_dir, stem + "_metadata.json"),
                         os.path.join(output_dir, stem + "_run_log.txt"))


def unique_planned_output(planned: PlannedOutput, reserved=()) -> PlannedOutput:
    """Check the complete output family before freezing a run's paths."""
    names = set(os.listdir(planned.output_dir)) if os.path.isdir(planned.output_dir) else set()
    names.update(os.path.basename(path) for path in reserved)

    def occupied(candidate):
        for name in names:
            if name.startswith(candidate.stem + ".") or name.startswith(candidate.stem + "_"):
                return True
            # Photocurrent inserts condition tags before the run timestamp.
            if name.startswith(candidate.display_stem + "_") and (
                f"_{candidate.run_id}." in name or f"_{candidate.run_id}_" in name
            ):
                return True
        return False

    match = re.fullmatch(r"(\d{8}_\d{6})(?:_(\d+))?", planned.run_id)
    base_id = match.group(1) if match else planned.run_id
    sequence = int(match.group(2) or 1) if match else 1
    candidate = planned
    while occupied(candidate):
        sequence += 1
        run_id = f"{base_id}_{sequence:02d}"
        stem = (f"{planned.display_stem}_{run_id}"
                if planned.stem.endswith("_" + planned.run_id)
                else f"{planned.stem}_{sequence:02d}")
        candidate = planned_output_at(planned.output_dir, stem, run_id)
    return candidate


def gate_scan_filename_parts(params, signal_chain=None, field_t=None):
    """Use the same recipe tags for ordinary and frozen field scans."""
    from app.measurement_output import gate_scan_filename_parts as recipe_parts
    parts = recipe_parts(params, signal_chain)
    if field_t is not None:
        parts.append(field_output_tag(field_t))
    return parts


def sanitize_segment(value: str, fallback: str) -> str:
    return _sanitize_base(str(value or "")) or fallback


def field_output_tag(field_t: float) -> str:
    """Return a compact, non-scientific tag suitable for batch filenames."""
    value = float(field_t)
    if abs(value) < 0.5e-12:
        value = 0.0
    text = f"{value:.12f}".rstrip("0").rstrip(".")
    if text == "-0":
        text = "0"
    return f"B_{text}T"


def save_directory(save: SaveRoot, measurement_type: str, create: bool = False) -> str:
    user = sanitize_segment(save.user, "User")
    device_id = sanitize_segment(save.device_id, "device")
    date_part = datetime.datetime.now().strftime("%Y-%m-%d")
    output_dir = os.path.abspath(os.path.join(save.base, user, device_id, date_part, measurement_type))
    if create:
        os.makedirs(output_dir, exist_ok=True)
    return output_dir


def compose_output_stem(
    device_id: str,
    measurement_type: str,
    filename_stem: str,
    summary_parts: Iterable[str] = (),
    run_id: str | None = None,
    filename_measurement_type: str | None = None,
) -> str:
    """Build a filename with the user stem as its authoritative run label."""
    clean_device_id = sanitize_segment(device_id, "device")
    clean_measurement_type = sanitize_segment(measurement_type, "measurement")
    clean_user_stem = _sanitize_base(str(filename_stem or ""))
    fallback_label = sanitize_segment(
        filename_measurement_type or clean_measurement_type,
        clean_measurement_type,
    )
    filename_label = clean_user_stem or fallback_label
    clean_parts = [sanitize_segment(part, "") for part in summary_parts]
    clean_parts = [part for part in clean_parts if part]
    amplitude_pattern = r"AC(?:out|set)[0-9.eE+\-]+[fpnumkMG]?V"
    contact_parts = list(dict.fromkeys(part for part in clean_parts
        if part.startswith("AC") and not re.fullmatch(amplitude_pattern, part)))
    clean_parts = [part for part in clean_parts if part not in contact_parts]
    # Contact belongs with the experimental description. Preserve an existing
    # exact, underscore-delimited occurrence wherever the user placed it.
    contact_parts = [part for part in contact_parts
        if f"_{part}_" not in f"_{filename_label}_"]
    # Keep user descriptions, but replace generated AC amplitude tags and avoid
    # repeating exact structured tags already entered in the free-form label.
    tags = set(clean_parts)
    has_ac_amplitude = any(re.fullmatch(amplitude_pattern, part) for part in clean_parts)
    estimate_pattern = r"VacEst[0-9.eE+\-]+[fpnumkMG]?V"
    has_ac_estimate = any(re.fullmatch(estimate_pattern, part) for part in clean_parts)
    label_parts = filename_label.split('_')
    label_parts = [part for part in label_parts if part not in tags and not (
        has_ac_amplitude and re.fullmatch(amplitude_pattern, part)) and not (
        has_ac_estimate and re.fullmatch(estimate_pattern, part))]
    filename_label = '_'.join(label_parts)
    clean_run_id = sanitize_segment(run_id, "") or new_run_id()
    return "_".join(part for part in [clean_device_id, filename_label, *contact_parts, *clean_parts, clean_run_id] if part)


def build_planned_output(
    save: SaveRoot,
    measurement_type: str,
    filename_stem: str,
    summary_parts: Iterable[str] = (),
    run_id: str | None = None,
    create_dir: bool = False,
    filename_measurement_type: str | None = None,
    freeze: bool = False,
) -> PlannedOutput:
    run_id = sanitize_segment(run_id, "") or new_run_id()
    measurement_type = sanitize_segment(measurement_type, "measurement")
    output_dir = save_directory(save, measurement_type, create=create_dir)
    stem = compose_output_stem(save.device_id, measurement_type, filename_stem,
                               summary_parts, run_id, filename_measurement_type)
    planned = planned_output_at(output_dir, stem, run_id)
    return planned if freeze else unique_planned_output(planned)


def planned_output_warning(planned: PlannedOutput, save: SaveRoot) -> str:
    warnings: list[str] = []
    if not str(save.user or "").strip():
        warnings.append("Operator is blank")
    if not str(save.device_id or "").strip():
        warnings.append("Device ID is blank")
    if not str(save.base or "").strip():
        warnings.append("Data root is blank")
    if os.path.exists(planned.csv_path):
        warnings.append("CSV already exists")
    if os.path.exists(planned.metadata_path):
        warnings.append("metadata already exists")
    if os.path.exists(planned.log_path):
        warnings.append("run log already exists")
    return "; ".join(warnings)


def output_blocking_reason(planned: PlannedOutput, save: SaveRoot) -> str:
    missing = []
    if not str(save.user or "").strip():
        missing.append("Operator")
    if not str(save.device_id or "").strip():
        missing.append("Device ID")
    if not str(save.base or "").strip():
        missing.append("Data Root")
    if missing:
        return "Fill in required save settings before starting: " + ", ".join(missing) + "."
    existing = [path for path in (planned.csv_path, planned.metadata_path, planned.log_path) if os.path.exists(path)]
    if existing:
        names = ", ".join(os.path.basename(path) for path in existing)
        return "Output file already exists. Change the filename stem or reset the preview before starting: " + names
    try:
        os.makedirs(planned.output_dir, exist_ok=True)
    except Exception as ex:
        return f"Cannot create output folder: {planned.output_dir}\n{ex}"
    return ""


def to_jsonable(value):
    if is_dataclass(value):
        return asdict(value)
    if isinstance(value, dict):
        return {str(k): to_jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [to_jsonable(v) for v in value]
    return value


def promote_experiment_metadata(payload: dict) -> dict:
    """Keep large snapshots once at the JSON root, leaving scalar calibration intact."""
    payload = to_jsonable(payload)
    chain = payload.get("signal_chain")
    if not isinstance(chain, dict):
        chain = payload.get("validation", {}).get("calibration", {}).get("signal_chain")
    if isinstance(chain, dict):
        for key in ("lockin_settings", "experiment_context"):
            value = chain.pop(key, None)
            if value is not None:
                payload[key] = value
    return payload


def write_run_metadata(path: str, payload: dict) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    payload = promote_experiment_metadata(payload)
    payload.setdefault("created_at", datetime.datetime.now().isoformat(timespec="seconds"))
    payload.setdefault("status", "running")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(to_jsonable(payload), f, indent=2, sort_keys=True)
        f.write("\n")


def update_run_metadata_status(path: str, status: str, detail: str = "", safe_state_failures: list[str] | None = None) -> None:
    if not path:
        return
    try:
        with open(path, "r", encoding="utf-8") as f:
            payload = json.load(f)
    except Exception:
        payload = {}
    payload["status"] = status
    payload["completed_at"] = datetime.datetime.now().isoformat(timespec="seconds")
    payload["detail"] = detail
    payload["safe_state"] = {
        "ok": not safe_state_failures,
        "failures": list(safe_state_failures or []),
    }
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(to_jsonable(payload), f, indent=2, sort_keys=True)
        f.write("\n")
