from __future__ import annotations

import datetime
import json
import os
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


def new_run_id() -> str:
    return datetime.datetime.now().strftime("%Y%m%d_%H%M%S")


def sanitize_segment(value: str, fallback: str) -> str:
    return _sanitize_base(str(value or "")) or fallback


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
    clean_run_id = sanitize_segment(run_id or new_run_id(), new_run_id())
    return "_".join([clean_device_id, filename_label, *clean_parts, clean_run_id])


def build_planned_output(
    save: SaveRoot,
    measurement_type: str,
    filename_stem: str,
    summary_parts: Iterable[str] = (),
    run_id: str | None = None,
    create_dir: bool = False,
    filename_measurement_type: str | None = None,
) -> PlannedOutput:
    run_id = sanitize_segment(run_id or new_run_id(), new_run_id())
    measurement_type = sanitize_segment(measurement_type, "measurement")
    stem = compose_output_stem(
        save.device_id,
        measurement_type,
        filename_stem,
        summary_parts,
        run_id,
        filename_measurement_type,
    )
    output_dir = save_directory(save, measurement_type, create=create_dir)
    return PlannedOutput(
        run_id=run_id,
        output_dir=output_dir,
        stem=stem,
        csv_path=os.path.join(output_dir, stem + ".csv"),
        metadata_path=os.path.join(output_dir, stem + "_metadata.json"),
        log_path=os.path.join(output_dir, stem + "_run_log.txt"),
    )


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


def write_run_metadata(path: str, payload: dict) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    payload = dict(payload)
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
