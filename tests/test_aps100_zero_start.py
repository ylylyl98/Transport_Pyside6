"""Strict directional moves starting near zero, with scripted readbacks."""
import unittest
from unittest.mock import patch

from app.devices.aps100_attodry1000_adapter import APS100OperationCancelled
from tests.test_aps100_preparation import PreparationAdapter


class ZeroStartAdapter(PreparationAdapter):
    def __init__(self, initial=0., reverse=False):
        super().__init__()
        self.value = initial
        self.table = {i: (10. * (i+1), .11952864) for i in range(5)}
        self.sweep_rates = []
        self.reverse = reverse

    def _write(self, command):
        self.writes.append(command)
        self.sweep_rates.append(dict(self.table))
        if self.reverse:
            self.pending = [.0004, .0072, .0068, .0047, self.target]
            self.reverse = False
        else:
            self.pending = [self.target]


class ZeroStartTests(unittest.TestCase):
    def options(self):
        return dict(acceptance_t=.0005, reversal_t=.0002, lower_t=-.1,
                    upper_t=.1, no_progress_s=15.)

    def test_near_zero_uses_reduced_rate_before_first_sweep_in_both_directions(self):
        for direction in (-1, 1):
            with self.subTest(direction=direction):
                adapter = ZeroStartAdapter(); original = adapter.get_rates(); history = {}
                with patch("app.devices.aps100_attodry1000_adapter.time.monotonic", side_effect=lambda: adapter.clock):
                    adapter._hysteresis_field_move(direction*.02, direction, .0002, 600., None, None, self.options(), history)
                self.assertAlmostEqual(adapter.sweep_rates[0][0][1], .02317392)
                self.assertTrue(history['zero_start_slow'])
                self.assertEqual(adapter.get_rates(), original)
                self.assertEqual(set(w for w in adapter.writes if w.startswith('SWEEP')),
                                 {'SWEEP DOWN SLOW' if direction < 0 else 'SWEEP UP SLOW'})

    def test_outside_zero_tolerance_keeps_normal_first_stage(self):
        adapter = ZeroStartAdapter(initial=.001)
        original = adapter.get_rates()
        with patch("app.devices.aps100_attodry1000_adapter.time.monotonic", side_effect=lambda: adapter.clock):
            adapter._hysteresis_field_move(.02, 1, .0002, 600., None, None, self.options(), {})
        self.assertEqual(adapter.sweep_rates[0], original)

    def test_recorded_reverse_warns_continues_and_restores_rates(self):
        adapter = ZeroStartAdapter(reverse=True); original = adapter.get_rates(); history = {}
        with patch("app.devices.aps100_attodry1000_adapter.time.monotonic", side_effect=lambda: adapter.clock):
            adapter._hysteresis_field_move(.01, 1, .0002, 600., None, None, self.options(), history)
        self.assertEqual(adapter.get_rates(), original)
        commands = [w for w in adapter.writes if w.startswith('SWEEP')]
        self.assertTrue(commands)
        self.assertEqual(set(commands), {'SWEEP UP SLOW'})
        self.assertTrue(history['complete'])
        self.assertTrue(history['reversal_exceeded'])
        self.assertAlmostEqual(history['max_reverse_t'], .0025)
        self.assertAlmostEqual(history['reversal_events'][0]['readback_t'], .0047)
        self.assertAlmostEqual(history['end_t'], .01)
        self.assertIn('PAUSE', adapter.writes)

    def test_sweep_fault_restores_original_rates(self):
        adapter = ZeroStartAdapter(); original = adapter.get_rates()
        def fail(command):
            self.assertAlmostEqual(adapter.table[0][1], .02317392)
            raise RuntimeError('injected sweep fault')
        adapter._write = fail
        with patch("app.devices.aps100_attodry1000_adapter.time.monotonic", side_effect=lambda: adapter.clock):
            with self.assertRaisesRegex(RuntimeError, 'injected sweep fault'):
                adapter._hysteresis_field_move(-.02, -1, .0002, 600., None, None, self.options(), {})
        self.assertEqual(adapter.get_rates(), original)

    def test_cancel_after_rate_reduction_restores_original_rates(self):
        adapter = ZeroStartAdapter(); original = adapter.get_rates()
        def cancel(command):
            raise APS100OperationCancelled()
        adapter._write = cancel
        with patch("app.devices.aps100_attodry1000_adapter.time.monotonic", side_effect=lambda: adapter.clock):
            with self.assertRaises(APS100OperationCancelled):
                adapter._hysteresis_field_move(-.02, -1, .0002, 600., None, None, self.options(), {})
        self.assertEqual(adapter.get_rates(), original)
        self.assertIn('PAUSE', adapter.writes)

    def test_minimum_rate_is_never_raised_above_existing_ceiling(self):
        adapter = ZeroStartAdapter()
        adapter.table = {i: (10. * (i+1), .0001) for i in range(5)}
        original = adapter.get_rates()
        with patch("app.devices.aps100_attodry1000_adapter.time.monotonic", side_effect=lambda: adapter.clock):
            adapter._hysteresis_field_move(-.02, -1, .0002, 600., None, None, self.options(), {})
        self.assertEqual(adapter.sweep_rates[0], original)
        self.assertEqual(adapter.get_rates(), original)


if __name__ == '__main__':
    unittest.main()
