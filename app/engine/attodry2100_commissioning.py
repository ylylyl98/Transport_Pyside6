"""Durable, operator-auditable attoDRY2100 commissioning evidence.

The evidence recorder deliberately depends on the controller protocol rather
than an adapter, so commissioning cannot bypass the controller's owner thread.
"""

from __future__ import annotations

import json
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _json_value(value: Any):
    if is_dataclass(value):
        return _json_value(asdict(value))
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    if hasattr(value, "__dict__") and not isinstance(value, (str, bytes)):
        return {str(key): _json_value(item) for key, item in vars(value).items()}
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


class _EvidenceRecorder:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._data = {"schema": "attodry2100_commissioning_v1", "events": []}

    def append(self, event: str, **payload):
        item = {
            "sequence": len(self._data["events"]) + 1,
            "timestamp_utc": datetime.now(timezone.utc).isoformat(),
            "event": event,
        }
        item.update({key: _json_value(value) for key, value in payload.items()})
        self._data["events"].append(item)
        self.path.write_text(json.dumps(self._data, indent=2) + "\n", encoding="utf-8")


class AttoDRY2100CommissioningEvidence:
    def __init__(self, controller, path: Path):
        self.controller = controller
        self.recorder = _EvidenceRecorder(Path(path))
        self.recorder.append("commissioning_started")

    def snapshot(self, label: str):
        value = self.controller.read_snapshot()
        self.recorder.append("snapshot", label=str(label), snapshot=value)
        return value

    def stabilization(self, duration_s: float, *, stable: bool, snapshot):
        self.recorder.append(
            "stabilization_decision",
            duration_s=float(duration_s),
            stable=bool(stable),
            snapshot=snapshot,
        )

    def stop_and_observe(self):
        self.recorder.append("stop_requested")
        try:
            acknowledgement = self.controller.stop_field_control()
        except BaseException as exc:
            self.recorder.append("stop_error", error=str(exc))
            raise
        self.recorder.append("stop_ack", acknowledgement=acknowledgement)
        try:
            observation = self.controller.read_snapshot()
        except BaseException as exc:
            self.recorder.append("stop_verification_error", error=str(exc))
            raise
        self.recorder.append("stop_verification_observation", snapshot=observation)
        return acknowledgement, observation

    def cleanup(self):
        first_error = None
        try:
            self.controller.disconnect_async()
        except BaseException as exc:
            first_error = exc
            self.recorder.append("disconnect_error", error=str(exc))
        try:
            self.controller.shutdown()
        except BaseException as exc:
            first_error = first_error or exc
            self.recorder.append("shutdown_error", error=str(exc))
        if first_error is not None:
            self.recorder.append("cleanup_error", error=str(first_error))
            raise first_error
        self.recorder.append("cleanup_complete")

