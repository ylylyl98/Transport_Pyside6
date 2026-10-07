import unittest
from unittest.mock import MagicMock, patch
from dataclasses import replace

from app.devices.aps100_attodry1000_adapter import MockAPS100Adapter
from controllers.magnet_controller import _MagnetWorker
from utils.config import cfg


class GateScanRateTests(unittest.TestCase):
    def run_move(self, *, gate=True, target=2.0, start=0.0, mismatch=False, boundary=40.0, stalled=False):
        worker = _MagnetWorker()
        snapshot = replace(MockAPS100Adapter().read_snapshot(), field_t=start,
                           output_field_t=start)
        adapter = MagicMock()
        adapter.coil_constant_t_per_a = cfg.magnet.coil_constant_t_per_a
        adapter.read_snapshot.return_value = snapshot
        adapter.get_status.return_value.sweep_active = False
        rates = {0: (boundary, 0.01951488), 1: (44.27, 0.20856528)}
        adapter.get_rates.side_effect = lambda: dict(rates)
        def set_rate(rate, **kwargs):
            if not mismatch:
                rates[0] = (boundary, rate)
            return {0: rate}
        adapter.set_rate_t_per_min.side_effect = set_rate
        adapter.safe_move_to_field.return_value = snapshot
        if stalled:
            def move(*args, **kwargs):
                with patch("controllers.magnet_controller.time.monotonic", side_effect=[0.0, 121.0]):
                    kwargs["progress"]("ramping field", 1.0)
                    kwargs["progress"]("ramping field", 1.0)
                return snapshot
            adapter.safe_move_to_field.side_effect = move
        worker.adapter = adapter
        results = []
        worker.safe_move_result.connect(results.append)
        worker.safe_move_to_field("test", target, "persistent", True, True,
                                  2.0, 3600.0, 0.002, gate)
        return adapter, results[-1]

    def test_residual_slow_sweep_rate_replaced_before_gate_move(self):
        adapter, result = self.run_move()
        self.assertTrue(result["success"])
        plan = result["audit"]["gate_scan_rate_plan"]
        self.assertAlmostEqual(plan["rate_t_per_min"], 0.41835024)
        self.assertAlmostEqual(plan["expected_ramp_s"], 286.84, delta=0.1)
        self.assertEqual(result["audit"]["stored_rates"][0][1], 0.01951488)
        calls = [c[0] for c in adapter.mock_calls]
        self.assertLess(calls.index("set_rate_t_per_min"), calls.index("safe_move_to_field"))

    def test_eight_tesla_and_polarity_reversal_use_default_with_scaled_timeout(self):
        for start in (0.0, -8.0):
            with self.subTest(start=start):
                adapter, result = self.run_move(start=start, target=8.0)
                self.assertTrue(result["success"])
                plan = result["audit"]["gate_scan_rate_plan"]
                self.assertGreater(plan["timeout_s"], plan["expected_ramp_s"])
                self.assertEqual(adapter.safe_move_to_field.call_args.kwargs["timeout_s"], plan["timeout_s"])

    def test_bad_readback_or_range_prevents_movement(self):
        for kwargs in ({"mismatch": True}, {"boundary": 39.0}, {"target": 8.2}):
            with self.subTest(kwargs=kwargs):
                adapter, result = self.run_move(**kwargs)
                self.assertFalse(result["success"])
                adapter.safe_move_to_field.assert_not_called()

    def test_transport_move_does_not_override_measurement_rate(self):
        adapter, result = self.run_move(gate=False)
        self.assertTrue(result["success"])
        adapter.set_rate_t_per_min.assert_not_called()
        self.assertEqual(adapter.safe_move_to_field.call_args.kwargs["timeout_s"], 3600.0)

    def test_stalled_gate_move_pauses_and_reports_field(self):
        adapter, result = self.run_move(stalled=True)
        self.assertFalse(result["success"])
        self.assertIn("stalled at 1 T", result["error"])
        adapter.pause.assert_called_with(confirm=False)
