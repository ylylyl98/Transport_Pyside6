"""Optional vendor runtimes must not load for ordinary Transport imports."""
import os
import sys
import unittest
from types import ModuleType
from unittest.mock import patch

if os.environ.get("TRANSPORT_OFFLINE_VERIFICATION") != "1":
    raise unittest.SkipTest("Run through verify_offline.py")


class InstrumentImportTests(unittest.TestCase):
    def test_transport_device_manager_does_not_load_optional_lightfield_runtime(self):
        import app.device_manager
        self.assertTrue(app.device_manager.DeviceManager)
        self.assertNotIn("instruments.lightField6", sys.modules)
        self.assertNotIn("clr", sys.modules)

    def test_explicit_optional_export_and_registry_preserve_class_identity(self):
        import instruments
        fake = ModuleType("instruments.lightField6")
        fake.LightField6 = type("FakeLightField", (), {})
        try:
            with patch.dict(sys.modules, {"instruments.lightField6": fake}):
                from instruments import LightField6, supported_instruments
                self.assertIs(LightField6, fake.LightField6)
                self.assertIs(supported_instruments["LightField6"], LightField6)
                self.assertIs(supported_instruments["DaqCard"], instruments.DaqCard)
                self.assertIsInstance(supported_instruments, dict)
                self.assertEqual(set(supported_instruments), set(instruments._SUPPORTED_NAMES))
                self.assertIn("LightField6", dir(instruments))
        finally:
            instruments.__dict__.pop("LightField6", None)
            instruments.__dict__.pop("supported_instruments", None)
