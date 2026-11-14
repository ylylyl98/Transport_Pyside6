"""Offline directional preparation and settling simulations."""
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from app.devices.aps100_attodry1000_adapter import (
    APS100AttoDry1000Adapter, APS100FieldHistoryError, APS100QualityOvershoot, APS100SafetyError, APS100TimeoutError,
)


class PreparationAdapter(APS100AttoDry1000Adapter):
    def __init__(self, sign=-1, oscillate=False):
        super().__init__()
        self.sign, self.value, self.target = sign, 0., 0.
        self.clock, self.pending, self.writes, self.rates_seen = 0., [], [], []
        self.table = {i: (10. * (i+1), .12) for i in range(5)}
        self.oscillate, self.final, self.injected = oscillate, False, False
        self._sleep = lambda seconds: setattr(self, "clock", self.clock + seconds)

    def get_field_t(self):
        if self.pending:
            self.value = self.pending.pop(0)
        elif self.final and self.oscillate:
            self.value = self.target + (.0001 if int(self.clock * 10) % 2 else -.0001)
        return self.value
    def get_output_field_t(self): return self.value
    def get_rates(self): return dict(self.table)
    def restore_rates(self, rates):
        self.rates_seen.append(dict(rates)); self.table = dict(rates)
    def _ensure_no_fault(self): return SimpleNamespace(sweep_active=False)
    def get_heater_state(self): return 1
    def _set_directional_target(self, target, current):
        self.target = target
        return "UP" if target >= current else "DOWN"
    def _write(self, command):
        self.writes.append(command)
        if not self.injected and abs(self.target) > .197:
            self.pending = [self.sign*.1955, self.sign*.1936, self.target]
            self.injected = True
        else:
            self.pending = [self.target]
        self.final = abs(self.target) == .2
    def pause(self, **kwargs): self.writes.append("PAUSE")
    def read_snapshot(self):
        return SimpleNamespace(field_t=self.get_field_t(), heater_on=True,
                               status=SimpleNamespace(sweep_active=False, faulted=False))


class PreparationTests(unittest.TestCase):
    def test_ordinary_move_returns_snapshot_after_slow_rate_restoration(self):
        for sign in (-1, 1):
            with self.subTest(sign=sign):
                adapter = PreparationAdapter(sign)
                adapter.injected = True
                original = adapter.get_rates()
                restore = adapter.restore_rates
                def slow_restore(rates):
                    restore(rates)
                    adapter.clock += 3.2
                adapter.restore_rates = slow_restore
                read = adapter.read_snapshot
                def stamped_read():
                    result = read()
                    result.monotonic_s = adapter.clock
                    return result
                adapter.read_snapshot = stamped_read
                options = self.options()
                options['preparation'] = False
                history = {}
                with patch('app.devices.aps100_attodry1000_adapter.time.monotonic', side_effect=lambda: adapter.clock):
                    result = adapter._hysteresis_field_move(sign*.2, sign, .0002, 30., None, None, options, history)
                self.assertEqual(adapter.get_rates(), original)
                self.assertTrue(history['complete'])
                self.assertLess(adapter.clock-result.monotonic_s, 3., 'Successful move published a stale pre-restoration snapshot')

    def test_ordinary_move_rechecks_field_after_rate_restoration(self):
        adapter = PreparationAdapter(1)
        adapter.injected = True
        original = adapter.get_rates()
        restore = adapter.restore_rates
        def restore_with_drift(rates):
            restore(rates)
            if rates == original:
                adapter.value = .2008
        adapter.restore_rates = restore_with_drift
        options = self.options()
        options['preparation'] = False
        with patch('app.devices.aps100_attodry1000_adapter.time.monotonic', side_effect=lambda: adapter.clock):
            with self.assertRaises(APS100QualityOvershoot):
                adapter._hysteresis_field_move(.2, 1, .0002, 30., None, None, options, {})
        self.assertEqual(adapter.get_rates(), original)
        self.assertIn('PAUSE', adapter.writes)

    def options(self):
        return dict(acceptance_t=.0002, reversal_t=.0002, lower_t=-.201,
                    upper_t=.201, no_progress_s=15., preparation=True,
                    stability_s=.5, span_t=.0001, drift_t_per_s=.0001,
                    stability_timeout_s=2.)

    def test_progressive_approach_tolerates_logged_reversal_both_directions(self):
        for sign in (-1, 1):
            with self.subTest(sign=sign):
                adapter = PreparationAdapter(sign); original = adapter.get_rates(); history = {}
                progress = []
                with patch("app.devices.aps100_attodry1000_adapter.time.monotonic", side_effect=lambda: adapter.clock):
                    adapter._hysteresis_field_move(sign*.2, sign, .0002, 30., None,
                                                  lambda label, value: progress.append((label, value)), self.options(), history)
                self.assertTrue(any(label == "preparation field" for label, _ in progress))
                self.assertGreaterEqual(history["max_reverse_t"], .0019 - 1e-12)
                self.assertTrue(history["complete"])
                commands = [w for w in adapter.writes if w.startswith("SWEEP")]
                self.assertGreaterEqual(len(commands), 3)
                self.assertEqual(set(commands), {"SWEEP DOWN SLOW" if sign < 0 else "SWEEP UP SLOW"})
                self.assertEqual(adapter.get_rates(), original)
                self.assertTrue(all(table[i][1] <= original[i][1] for table in adapter.rates_seen for i in original))

    def test_unstable_endpoint_times_out_and_restores_rates(self):
        adapter = PreparationAdapter(oscillate=True); original = adapter.get_rates()
        with patch("app.devices.aps100_attodry1000_adapter.time.monotonic", side_effect=lambda: adapter.clock):
            with self.assertRaisesRegex(APS100TimeoutError, "stability"):
                adapter._hysteresis_field_move(-.2, -1, .0002, 30., None, None, self.options(), {})
        self.assertEqual(adapter.get_rates(), original)
        self.assertIn("PAUSE", adapter.writes)

    def test_excessive_preparation_reversal_fails_closed(self):
        adapter = PreparationAdapter(); opts = self.options(); opts['no_progress_s'] = 2.
        with patch("app.devices.aps100_attodry1000_adapter.time.monotonic", side_effect=lambda: adapter.clock):
            with self.assertRaisesRegex(APS100FieldHistoryError, "rate-based bound"):
                adapter._hysteresis_field_move(-.2, -1, .0002, 30., None, None, opts, {})
        self.assertIn("PAUSE", adapter.writes)

    def test_below_minimum_rate_is_never_increased(self):
        adapter = PreparationAdapter()
        adapter.table = {i: (10. * (i+1), .0001) for i in range(5)}
        adapter.injected = True
        original = adapter.get_rates()
        with patch("app.devices.aps100_attodry1000_adapter.time.monotonic", side_effect=lambda: adapter.clock):
            adapter._hysteresis_field_move(-.2, -1, .0002, 30., None, None, self.options(), {})
        self.assertTrue(all(table[i][1] <= original[i][1] for table in adapter.rates_seen for i in original))
        self.assertEqual(adapter.get_rates(), original)


if __name__ == "__main__":
    unittest.main()
