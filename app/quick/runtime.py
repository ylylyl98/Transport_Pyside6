"""Validated runtime settings with the same transaction boundary as Widgets."""
from PySide6.QtCore import QObject, Property, Signal, Slot
from app.quick.forms import field, checked_value
from app.runtime_settings import SPECS, get_value, apply_runtime_settings


class RuntimeSettings(QObject):
    changed = Signal()
    messageRaised = Signal(str)

    def __init__(self, config, is_idle, apply_font, parent=None):
        super().__init__(parent)
        self.config, self.is_idle, self.apply_font = config, is_idle, apply_font

    @Property('QVariantList', notify=changed)
    def fields(self):
        groups = {"font_size_pt": "Appearance", "mcd.transport_endpoint_settling_enabled": "Endpoint settling · next measurement",
                  "mcd.endpoint_stable_reads": "Endpoint settling · next measurement",
                  "mcd.transport_endpoint_settling_timeout_s": "Endpoint settling · next measurement",
                  "mcd.transport_monitor_recovery_timeout_s": "Monitor recovery · next measurement",
                  "mcd.transport_monitor_recovery_reads": "Monitor recovery · next measurement"}
        return [field(s.path, s.label + s.suffix, get_value(self.config, s.path),
                      min=s.minimum, max=s.maximum,
                      group=groups.get(s.path, "Magnet sweep · next measurement"))
                for s in SPECS.values()]

    @Slot(str, 'QVariant')
    def setValue(self, key, value):
        try:
            spec = next(s for s in self.fields if s["key"] == key)
            value = checked_value(spec, value)
            old_font = self.config.font_size_pt
            apply_runtime_settings(self.config, {key: value}, is_idle=self.is_idle,
                                   apply_font=self.apply_font, rollback_appearance=lambda: self.apply_font(old_font))
            self.changed.emit()
            self.messageRaised.emit("Setting applied")
        except Exception as exc:
            self.messageRaised.emit(str(exc))
