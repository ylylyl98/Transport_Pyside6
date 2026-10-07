"""Shadow diagnostics must not command hardware or weaken live protection."""
import tempfile
import time
import unittest
from unittest.mock import patch

from PySide6.QtWidgets import QApplication
from app.engine.gate_scan_field_batch import GateScanFieldBatch, GateScanBatchRequest
from app.thermal_safety import ThermalSafetyEvaluator
from utils.config import LakeShore335Config
from test_gate_scan_field_batch import _FakeMagnet, _FakeTab
import test_field_thermal_policy as policy_tests


class FieldThermalIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.magnet = _FakeMagnet()
        self.tab = _FakeTab(self.directory.name)
        self.safety = ThermalSafetyEvaluator(LakeShore335Config(field_envelope_enabled=False, field_envelope_comparison_enabled=True))
        self.batch = GateScanFieldBatch(self.magnet, self.tab, thermal_safety=self.safety)
        self.batch._state.request = GateScanBatchRequest((-.4,), self.tab.params, (), 'test', '')
        self.batch._state.phase = 'moving'
        self.batch._state.move_request_id = 'move-1'
        self.temperature, self.snapshot = policy_tests.FieldThermalPolicyTests().inputs(now=time.monotonic())
        self.batch._snapshot = self.snapshot

    def test_missing_move_readbacks_cannot_reuse_previous_field(self):
        self.batch._update_field_comparison(self.temperature)
        self.assertEqual(self.batch._comparison_record['state'], 'MONITOR_FAULT')
        self.assertIsNone(self.batch._comparison_record['actual_field_t'])
        self.assertEqual(self.magnet.calls, [])

    def test_current_query_responses_produce_low_field_comparison(self):
        for command, response in [('IMAG?', '-0.4 T'), ('*STB?', '0'), ('PSHTR?', '1')]:
            self.batch._on_diagnostic_readback(dict(command=command, response=response,
                request_id='move-1', monotonic_s=time.monotonic()))
        self.batch._update_field_comparison(self.temperature)
        self.assertEqual(self.batch._comparison_record['state'], 'WITHIN_ENVELOPE')
        self.assertEqual(self.batch._comparison_record['ceiling_k'], 5.5)
        self.assertEqual(len(self.batch._comparison_record['readbacks']), 3)
        self.assertEqual(self.magnet.calls, [])

    def test_comparison_exception_cannot_suppress_live_trip(self):
        with patch.object(self.batch, '_evaluate_field_comparison', side_effect=ValueError('bad data')), \
             patch.object(self.batch, '_abort_batch') as abort, \
             patch.object(self.batch, '_record_temperature_telemetry'):
            self.batch.on_lakeshore_snapshot(self.temperature)
        abort.assert_called_once()
        self.assertIn('thermal stop', abort.call_args.args[0])
        self.assertEqual(self.batch._comparison_record['state'], 'MONITOR_FAULT')

    def test_precharge_observation_does_not_block_or_change_existing_move(self):
        self.safety.latest_snapshot = self.temperature
        result = self.batch._call_safe_move(-.4)
        self.assertEqual(result, 'move-1')
        self.assertEqual(self.batch._comparison_record['state'], 'PRECHARGE_BLOCKED')
        self.assertEqual(self.magnet.calls[0][0], 'move')

    def test_other_move_readbacks_are_ignored(self):
        self.batch._on_diagnostic_readback(dict(command='IMAG?', response='9 T', request_id='old'))
        self.assertEqual(self.batch._comparison_readbacks, {})
