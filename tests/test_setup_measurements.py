import os
import unittest
from types import SimpleNamespace

if os.environ.get("TRANSPORT_OFFLINE_VERIFICATION") != "1":
    raise unittest.SkipTest("Run through verify_offline.py")

from PySide6.QtCore import QCoreApplication, QObject, Signal
from app.models import SaveRoot
from app.quick.measurements import MeasurementService
from tests.test_quick_measurements import FakeManager


class Magnet2100(QObject):
    external_stop_requested = Signal()
    fault = Signal(str)
    disconnected = Signal()


class SetupMeasurementTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QCoreApplication.instance() or QCoreApplication([])

    def service(self, backend, magnet=None):
        return MeasurementService(FakeManager(), SaveRoot(), magnet2100=magnet,
                                  selected_backend_callable=lambda: backend)

    def test_no_magnet_rejects_mode_and_start(self):
        service = self.service("none")
        self.assertEqual([x["id"] for x in service.availableModes], [0, 1, 2, 3])
        self.assertIsNone(service._transport)
        service.mode = 5
        self.assertEqual(service.mode, 0)
        self.assertFalse(service.canStart)
        service.start()  # A rejected magnetic selection must not run the previous mode.
        self.assertFalse(service.active)
        service.mode = 0
        self.assertFalse(service._mode_selection_rejected)
        self.assertEqual(service.message, "Ready")
        service._mode = 5  # Defend against stale restored state too.
        service.start()
        self.assertFalse(service.active)
        self.assertIn("unavailable", service._message)

    def test_2100_defaults_and_incompatible_saved_values_are_recoverable(self):
        import json
        class Settings:
            def __init__(self, saved): self.saved = saved
            def value(self, key, default=None): return self.saved.get(key, default)
            def setValue(self, key, value): self.saved[key] = value
        fresh = self.service("2100", Magnet2100())
        fresh.mode = 5
        self.assertEqual(fresh._values[5]["measurement_mode"], "stepped")
        self.assertEqual(fresh._values[5]["sample_reversal_mode"], "unchanged")
        sections = fresh.uiSections
        self.assertNotIn("Sample B current reversal", [section["title"] for section in sections])
        mode = next(f for section in sections for f in section["fields"] if f["key"] == "measurement_mode")
        self.assertEqual(mode["options"], ["stepped"])
        saved = Settings({"quick/measurements/bfield_transport": json.dumps({
            "measurement_mode": "continuous", "sample_reversal_mode": "per_point"})})
        service = MeasurementService(FakeManager(), SaveRoot(), magnet2100=Magnet2100(),
            selected_backend_callable=lambda: "2100", settings=saved)
        service.mode = 5
        self.assertEqual(service._values[5]["measurement_mode"], "continuous")
        self.assertEqual(service._values[5]["sample_reversal_mode"], "per_point")
        self.assertEqual(len(service._field_errors[5]), 2)
        service.start()
        self.assertFalse(service.active)
        recovery = next(section for section in service.uiSections if section["title"] == "Unsupported saved sample reversal")
        self.assertEqual(recovery["fields"][0]["options"], ["unchanged"])
        service.setValue("measurement_mode", "stepped")
        service.setValue("sample_reversal_mode", "unchanged")
        self.assertFalse(service._field_errors[5])
        self.assertNotIn("Unsupported saved sample reversal", [s["title"] for s in service.uiSections])

    def test_empty_router_is_safe_for_desktop_initialization(self):
        from app.engine.bfield_transport_router import BFieldTransportRouter
        router = BFieldTransportRouter(None, None, None, FakeManager(),
                                       selected_backend_callable=lambda: "none")
        self.assertIsNone(getattr(router, "magnet", None))
        self.assertFalse(router.active)
        self.assertFalse(router._cleanup_in_progress)
        router.stop()
        router.on_lakeshore_snapshot(None)
        router.on_lakeshore_disconnected()
        router._on_fault("ignored")
        router.cancel_shutdown_preparation()
        self.assertTrue(router.prepare_shutdown())
        with self.assertRaisesRegex(ValueError, "unavailable"):
            router.start()

    def test_configured_mode_is_visible_without_controller_but_cannot_start(self):
        service = self.service("1000")
        self.assertEqual([x["id"] for x in service.availableModes], list(range(6)))
        service.mode = 5
        self.assertEqual(service.mode, 5)
        service.start()
        self.assertFalse(service.active)
        self.assertIn("controller is unavailable", service.message)

    def test_2100_only_router_and_stable_modes(self):
        service = self.service("2100", Magnet2100())
        self.assertEqual([x["id"] for x in service.modes], list(range(6)))
        self.assertEqual([x["id"] for x in service.availableModes], [0, 1, 2, 3, 5])
        self.assertIsNone(service._batch)
        self.assertEqual(list(service._transport._engines), ["2100"])
        self.assertIsNone(service._transport.lakeshore_controller)
        service._transport.on_lakeshore_snapshot(None)
        service._transport.on_lakeshore_disconnected()
        service._transport._on_fault("ignored")
        service.mode = 4
        self.assertEqual(service.mode, 0)
        service.mode = 5
        self.assertEqual(service.mode, 5)
        service._values[5]["measurement_mode"] = "continuous"
        with self.assertRaisesRegex(ValueError, "stepped"):
            service._start_magnetic()
        self.assertTrue(service.prepare_shutdown())


if __name__ == "__main__":
    unittest.main()
