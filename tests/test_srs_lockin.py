from __future__ import annotations

import os
import tempfile
import unittest
from threading import RLock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6 import QtCore, QtWidgets

from app.device_manager import DeviceManager
from app.models import Connections
from app.ui.lockin_panel import LockinPanel
from instruments.SR830 import LOCKIN_PROFILES, SRSLockin, detect_lockin_model


class FakeSRSLockin(SRSLockin):
    """Hardware-free command recorder for the shared SR830/SR850 logic."""

    def __init__(self, model: str, responses: dict[str, str] | None = None):
        self._name = "test lock-in"
        self._model = model
        self._identity = f"Stanford_Research_Systems,{model},0,1.0"
        self._expected_model = None
        self._last_snap_raw = ""
        self._input_values = {"x": None, "y": None, "r": None, "theta": None}
        self.lock = RLock()
        self.responses = dict(responses or {})
        self.writes: list[str] = []

    def _write(self, command: str, print_command=False):
        self.writes.append(command)

    def _query(self, command: str, print_command=False, print_response=False):
        return self.responses[command]


class SRSLockinDriverTests(unittest.TestCase):
    def test_identity_detection_accepts_both_models_and_rejects_others(self):
        self.assertEqual(detect_lockin_model("Stanford Research Systems,SR830,1,1.07"), "SR830")
        self.assertEqual(detect_lockin_model("Stanford_Research_Systems,SR850,1,1.0"), "SR850")
        with self.assertRaisesRegex(ValueError, "expected an SRS SR830 or SR850"):
            detect_lockin_model("Stanford Research Systems,SR860,1,1.0")

    def test_apply_uses_model_specific_reference_codes_and_current_gain(self):
        sr830 = FakeSRSLockin("SR830")
        sr830.apply_settings({"ref_source": 1, "frequency_hz": 137.0, "current_gain": 1})
        self.assertEqual(sr830.writes, ["FMOD 1", "FREQ 137"])

        sr850 = FakeSRSLockin("SR850")
        sr850.apply_settings(
            {"ref_source": 0, "frequency_hz": 137.0, "input_config": 2, "current_gain": 1}
        )
        self.assertEqual(sr850.writes, ["FMOD 0", "FREQ 137", "ISRC 2", "IGAN 1"])

    def test_sr850_settings_read_igan_and_use_sr850_labels(self):
        responses = {
            "PHAS?": "1.234",
            "FMOD?": "0",
            "FREQ?": "137.0",
            "SLVL?": "0.01",
            "SENS?": "18",
            "RMOD?": "2",
            "OFLT?": "10",
            "OFSL?": "1",
            "ISRC?": "2",
            "IGND?": "0",
            "ICPL?": "1",
            "ILIN?": "0",
            "HARM?": "1",
            "IGAN?": "1",
        }
        settings = FakeSRSLockin("SR850", responses).read_settings()
        self.assertEqual(settings["ref_source_label"], "Internal (Fixed)")
        self.assertEqual(settings["reserve_label"], "Minimum")
        self.assertEqual(settings["input_config_label"], "I")
        self.assertEqual(settings["current_gain_label"], "100 Mohm")


class SRSLockinPanelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
        cls.settings_dir = tempfile.TemporaryDirectory()
        QtCore.QSettings.setPath(
            QtCore.QSettings.Format.IniFormat,
            QtCore.QSettings.Scope.UserScope,
            cls.settings_dir.name,
        )

    @classmethod
    def tearDownClass(cls):
        cls.settings_dir.cleanup()

    def setUp(self):
        self.panel = LockinPanel(DeviceManager(Connections()))
        for widget in self.panel._setting_widgets():
            widget.setEnabled(True)

    def tearDown(self):
        self.panel.close()

    def test_sr850_frequency_is_only_editable_for_plain_internal_reference(self):
        self.panel._apply_capabilities(LOCKIN_PROFILES["SR850"])
        self.assertEqual(
            [self.panel.cbo_ref_source.itemText(i) for i in range(self.panel.cbo_ref_source.count())],
            ["Internal (Fixed)", "Internal Sweep", "External"],
        )
        self.assertEqual(self.panel.sp_harmonic.maximum(), 32767)
        self.assertTrue(self.panel.cbo_current_gain.isVisibleTo(self.panel))

        self.panel.cbo_ref_source.setCurrentIndex(0)
        self.panel._update_frequency_enabled()
        self.assertTrue(self.panel.sp_frequency.isEnabled())
        self.assertIn("frequency_hz", self.panel._collect_settings())

        self.panel.cbo_ref_source.setCurrentIndex(1)
        self.panel._update_frequency_enabled()
        self.assertFalse(self.panel.sp_frequency.isEnabled())
        self.assertNotIn("frequency_hz", self.panel._collect_settings())
        self.assertIn("Choose Internal (Fixed)", self.panel.lbl_reference_help.text())

    def test_sr830_keeps_its_original_internal_reference_mapping(self):
        self.panel._apply_capabilities(LOCKIN_PROFILES["SR830"])
        self.panel.cbo_ref_source.setCurrentIndex(1)
        self.panel._update_frequency_enabled()
        self.assertTrue(self.panel.sp_frequency.isEnabled())
        self.assertIn("frequency_hz", self.panel._collect_settings())
        self.assertFalse(self.panel.cbo_current_gain.isVisible())


if __name__ == "__main__":
    unittest.main()
