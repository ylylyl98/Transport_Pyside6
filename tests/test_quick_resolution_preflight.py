import tempfile
import unittest
import time
from PySide6.QtCore import QCoreApplication
from app.models import SaveRoot
from app.quick.measurements import MeasurementService
from tests.test_quick_measurements import FakeManager


class QuickResolutionPreflightTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.manager = FakeManager()
        self.service = MeasurementService(self.manager, SaveRoot(base=self.folder.name))
        self.addCleanup(self.service.deleteLater)

    def test_nonrepresentable_gate_target_is_rejected_in_each_mode(self):
        for mode in range(5):
            with self.subTest(mode=mode):
                self.service.mode = mode
                params = self.service.collect_params()
                if mode == 0:
                    params.vtg_set = .0001
                elif mode in (1, 4):
                    params.raw_vtg_start = .0001
                    params.raw_vtg_stop = .0001
                elif mode == 2:
                    params.vtg_start = .0001
                    params.vtg_stop = .0001
                else:
                    params.bias_conditions[0].vtg = .0001
                with self.assertRaisesRegex(ValueError, 'not representable'):
                    self.service._validate(params, mode)
                self.assertFalse(self.manager.claimed)

    def test_ramp_and_cached_source_range_are_enforced(self):
        self.service.mode = 1
        params = self.service.collect_params()
        params.vg_ramp = .0001
        with self.assertRaisesRegex(ValueError, 'ramp step'):
            self.service._validate(params, 1)
        self.service.mode = 0
        params = self.service.collect_params()
        params.vtg_set = 3.
        self.manager.sessions['g1'].cached_source_voltage_range_v = 2.
        with self.assertRaisesRegex(ValueError, 'source range'):
            self.service._validate(params, 0)

    def test_reserved_refresh_rejects_new_range_before_worker_start(self):
        app = QCoreApplication.instance() or QCoreApplication([])
        self.service.mode = 0
        params = self.service.collect_params()
        params.vtg_set = .5
        required, points = self.service._validate(params, 0)
        gate = self.manager.sessions['g1']
        def change_range():
            self.assertIn('g1', self.manager.claimed)
            gate.cached_source_voltage_range_v = .2
            return .2
        gate.refresh_source_voltage_range = change_range
        self.service._begin_preflight(params, 0, required, points)
        deadline = time.monotonic() + 3
        while self.service._preflight_thread is not None and time.monotonic() < deadline:
            app.processEvents()
            time.sleep(.005)
        self.assertIsNone(self.service._preflight_thread)
        self.assertIn('source range', self.service.message)
        self.assertFalse(self.manager.claimed)
        self.assertIsNone(self.service.worker)

    def test_summary_shows_cached_range_without_hardware_io(self):
        self.service.mode = 0
        self.assertIn('G1: 20 V range / 0.0005 V step', self.service.resolutionSummary)
        self.manager.sessions['g1'].cached_source_voltage_range_v = None
        self.assertIn('G1: resolution unconfirmed', self.service.resolutionSummary)
