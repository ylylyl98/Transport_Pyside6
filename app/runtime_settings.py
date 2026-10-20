"""Explicit, validated runtime settings; hardware configuration stays controller-owned."""
from dataclasses import dataclass
import math


@dataclass(frozen=True)
class RuntimeSetting:
    path: str
    label: str
    kind: type
    minimum: float = 0
    maximum: float = 0
    suffix: str = ""


APPEARANCE = RuntimeSetting("font_size_pt", "Main-window font size", int, 7, 18, " pt")
NEXT_RUN_SETTINGS = (
    RuntimeSetting("mcd.transport_endpoint_settling_enabled", "Wait for stable endpoint readings", bool),
    RuntimeSetting("mcd.endpoint_stable_reads", "Consecutive endpoint readings", int, 1, 1000),
    RuntimeSetting("mcd.transport_endpoint_settling_timeout_s", "Endpoint settling timeout", float, 2, 86400, " s"),
    RuntimeSetting("mcd.transport_monitor_recovery_timeout_s", "Monitoring recovery timeout", float, 1, 86400, " s"),
    RuntimeSetting("mcd.transport_monitor_recovery_reads", "Consecutive recovery readings", int, 1, 1000),
    RuntimeSetting("mcd.sweep_leg_timeout_s", "Minimum sweep-leg timeout", float, 1, 604800, " s"),
    RuntimeSetting("mcd.sweep_progress_epsilon_t", "Minimum field-progress increment", float, 1e-9, 1, " T"),
)
SPECS = {spec.path: spec for spec in (APPEARANCE, *NEXT_RUN_SETTINGS)}


def get_value(config, path):
    value = config
    for name in path.split("."):
        value = getattr(value, name)
    return value


def _set_value(config, path, value):
    parts = path.split(".")
    target = config
    for name in parts[:-1]:
        target = getattr(target, name)
    setattr(target, parts[-1], value)


def validate_values(values):
    checked = {}
    for path, value in values.items():
        if path not in SPECS:
            raise ValueError(f"{path} requires its instrument settings or an application restart.")
        spec = SPECS[path]
        if spec.kind is bool:
            valid = type(value) is bool
        elif spec.kind is int:
            valid = type(value) is int and spec.minimum <= value <= spec.maximum
        else:
            valid = type(value) in (int, float) and math.isfinite(value) and spec.minimum <= value <= spec.maximum
        if not valid:
            raise ValueError(f"Invalid {spec.label}: {value!r}")
        checked[path] = spec.kind(value)
    return checked


def apply_runtime_settings(config, values, *, is_idle, apply_font=None,
                           rollback_appearance=None, save=None):
    """Apply on the GUI thread. Never change a run's settings while it owns hardware.

    The caller supplies a fresh idle check, a font updater that raises if
    styling fails, and a callback restoring the captured appearance on failure.
    The existing config and sub-config object identities survive.
    """
    checked = validate_values(values)
    changes = {path: value for path, value in checked.items() if get_value(config, path) != value}
    if any(path.startswith("mcd.") for path in changes) and not is_idle():
        raise RuntimeError("Wait for measurements and instrument operations to finish before applying next-run settings.")
    if not changes:
        return ()
    old = {path: get_value(config, path) for path in changes}
    font_changed = APPEARANCE.path in changes
    if font_changed and (apply_font is None or rollback_appearance is None):
        raise ValueError("An appearance updater and rollback callback are required to apply the font immediately.")
    try:
        if font_changed:
            apply_font(changes[APPEARANCE.path])
        for path, value in changes.items():
            _set_value(config, path, value)
        (save or config.save)()
    except Exception:
        for path, value in old.items():
            _set_value(config, path, value)
        if font_changed:
            rollback_appearance()
        raise
    return tuple(changes)
