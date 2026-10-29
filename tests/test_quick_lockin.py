"""Lock-in presentation tests use cached fake sessions, never VISA."""
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from PySide6.QtCore import QObject, QSettings, Signal
from app.quick.lockin import LockinService
from instruments.SR830 import LOCKIN_PROFILES


class Manager(QObject):
    status_changed = Signal(str, str, str)
    resources_changed = Signal(object)
    operation_changed = Signal(bool, str)
    gate_currents_read = Signal(dict, str)

    def __init__(self):
        super().__init__()
        self.connections = SimpleNamespace(lockin="GPIB::8", drive_lockin="GPIB::9")
        self.connected = True
        self.owned = False
        self.hardware_busy = False
        self.session = SimpleNamespace(capabilities=dict(LOCKIN_PROFILES["SR830"]), _control_state_uncertain=False, run_control=Mock())

    def is_connected(self, _name): return self.connected
    def is_in_use(self, _name): return self.owned
    def is_busy(self): return self.hardware_busy
    def get_session(self, _name): return self.session
    def mark_in_use(self, _names):
        self.owned = True
        return True, []
    def release(self, _names): self.owned = False


class QuickLockinTests(unittest.TestCase):
    def setUp(self):
        self.scratch = tempfile.TemporaryDirectory()
        self.addCleanup(self.scratch.cleanup)
        self.settings = QSettings(str(Path(self.scratch.name) / "settings.ini"), QSettings.IniFormat)
        self.manager = Manager()
        self.service = LockinService(self.manager, settings=self.settings)
        self.addCleanup(self.service.close)
        self.service._status("lockin", "ok", "SR830")

    def data(self, **values):
        return {"identity": "SR830", "settings": {"sensitivity": 18, "time_constant": 10, **values},
                "outputs": {"x": 1.2}, "status": {"input_overload": False},
                "control": {"ok": True, "verified_keys": []}}

    def test_refresh_preserves_dirty_edit_and_discard_uses_verified_readback(self):
        self.service._done(self.data(sine_out_v=.1), None)
        self.service.setValue("sine_out_v", .2)
        self.service._done(self.data(sine_out_v=.15), None)
        self.assertEqual(self.service._values["sine_out_v"], .2)
        self.assertIn("sine_out_v", self.service.pendingFields)
        self.service.discardPending()
        self.assertEqual(self.service._values["sine_out_v"], .15)
        self.assertEqual(self.service.pendingFields, [])

    def test_repeated_connected_status_does_not_erase_dirty_fields(self):
        self.service.setValue("phase_deg", 13.)
        self.service._status("lockin", "ok", "unchanged SR830")
        self.assertIn("phase_deg", self.service.pendingFields)
        self.assertEqual(self.service._values["phase_deg"], 13.)

    def test_sr850_defaults_do_not_reinterpret_sr830_reference_codes(self):
        self.settings.setValue("lockin/ref_source", 1)
        self.settings.setValue("lockin/SR850/sine_out_v", .125)
        self.manager.session = SimpleNamespace(capabilities=dict(LOCKIN_PROFILES["SR850"]))
        self.service._status("lockin", "ok", "SR850")
        self.assertEqual(self.service._values["ref_source"], 0)
        self.assertEqual(self.service._values["sine_out_v"], .125)
        self.assertTrue(self.service.supportsDisplay)
        self.service.setValue("phase_deg", 12.)
        self.service._status("lockin", "ok", "same SR850")
        self.assertIn("phase_deg", self.service.pendingFields)

    def test_action_capabilities_and_finite_display_guard_reject_before_ownership(self):
        with patch.object(self.manager, "mark_in_use") as mark:
            self.service.run_action("auto_scale")
            mark.assert_not_called()
        self.service._verified_settings["time_constant"] = 11
        self.assertFalse(self.service.autoGainAvailable)
        with patch.object(self.manager, "mark_in_use") as mark:
            self.service.run_action("auto_gain")
            mark.assert_not_called()
        self.service._profile = dict(LOCKIN_PROFILES["SR850"])
        self.service._data["display"] = {"can_scale": True, "pane": 1}
        with patch.object(self.manager, "mark_in_use") as mark:
            self.service.applyDisplay(float("nan"), 0.)
            mark.assert_not_called()
        self.assertIn("finite", self.service.message)

    def test_auto_offset_is_supported_and_apply_only_submits_pending_supported_fields(self):
        with patch.object(self.service._lane, "submit") as submit:
            self.service.run_action("auto_offset_x")
            submit.assert_called_once()
        self.service._busy = False
        self.manager.release(["lockin"])
        self.service.setValue("phase_deg", 2.5)
        self.service._dirty.add("current_gain")  # hidden and unsupported on SR830
        with patch.object(self.service._lane, "submit") as submit:
            self.service.run_action("apply")
            work = submit.call_args.args[0]
        self.manager.session.run_control.return_value = {"control": {}}
        with patch("app.quick.lockin.save_control_log", return_value="fake.json"):
            work()
        self.assertEqual(self.manager.session.run_control.call_args.args[:2], ("apply", {"phase_deg": 2.5}))
        self.manager.release(["lockin"])

    def test_active_measurement_and_close_block_commands_and_edits(self):
        self.service.set_active_provider(lambda: True)
        self.service.setValue("phase_deg", 10.)
        self.assertNotEqual(self.service._values["phase_deg"], 10.)
        stopped = []
        self.service.stopForSettingsRequested.connect(lambda: stopped.append(True))
        self.service.requestStopForSettings()
        self.assertEqual(stopped, [True])
        self.service.set_active_provider(lambda: False)
        self.service.close()
        self.assertFalse(self.service.available)
        with patch.object(self.manager, "mark_in_use") as mark:
            self.service.run_action("refresh")
            mark.assert_not_called()

    def test_failed_readback_releases_ownership_and_keeps_pending_edit(self):
        self.service.setValue("phase_deg", 12.)
        self.manager.owned = True
        self.service._busy = True
        self.service._done(None, RuntimeError("transport failed"))
        self.assertFalse(self.manager.owned)
        self.assertFalse(self.service.readbackValid)
        self.assertIn("phase_deg", self.service.pendingFields)

    def test_gate_telemetry_keeps_lockin_form_and_pending_edits_unchanged(self):
        changes = []
        self.service.fieldsChanged.connect(lambda: changes.append(True))
        self.service.setValue("phase_deg", 12.)
        self.assertEqual(changes, [True])
        changes.clear()
        self.manager.gate_currents_read.emit({"g1": {"current": 2e-9}}, "readback")
        self.manager.operation_changed.emit(True, "Gate operation")
        self.manager.resources_changed.emit({"g1"})
        self.service._status("lockin", "ok", "same session")
        self.assertEqual(changes, [])
        self.assertEqual(self.service._values["phase_deg"], 12.)
        self.assertIn("phase_deg", self.service.pendingFields)
        self.service.discardPending()
        self.assertEqual(changes, [True])


if __name__ == "__main__":
    unittest.main()
