import unittest

from app.devices.aps100_attodry1000_adapter import (
    APS100AttoDry1000Adapter,
    APS100Error,
    APS100Status,
    APS100TimeoutError,
    parse_heater_state,
)


class _HeaterAdapter(APS100AttoDry1000Adapter):
    def __init__(self, states, *, warm=0.0, cool=0.0, timeout=0.1):
        super().__init__(
            heater_warm_s=warm,
            heater_cool_s=cool,
            heater_transition_timeout_s=timeout,
            sleep_fn=lambda seconds: None,
        )
        self.states = iter(states)
        self.events = []

    def _ensure_no_fault(self):
        return APS100Status(0, False, False, False, False, False, False, False, False)

    def get_heater_state(self):
        try:
            return int(next(self.states))
        except StopIteration:
            return 2

    def get_status(self):
        return APS100Status(0, False, False, False, False, False, False, False, False)

    def _write(self, command, **_kwargs):
        self.events.append(command)

    def pause(self, **_kwargs):
        self.events.append("pause")

    def get_field_t(self):
        return 0.25

    def get_output_field_t(self):
        return 0.25

    def _hold_transition(self, seconds, *, label, progress=None):
        self.events.append((label, seconds))


class APS100HeaterTransitionTests(unittest.TestCase):
    def test_parse_preserves_transition_and_rejects_invalid(self):
        self.assertEqual(parse_heater_state("0"), 0)
        self.assertEqual(parse_heater_state("2"), 2)
        with self.assertRaisesRegex(APS100Error, "expected 0 .* 1 .* 2"):
            parse_heater_state("9")

    def test_heater_on_waits_for_2_2_1_before_warm_dwell(self):
        adapter = _HeaterAdapter([0, 2, 2, 1], warm=60.0)
        adapter.enter_driven_mode(timeout_s=0.1)
        self.assertEqual(adapter.events[0], "pause")
        self.assertEqual(adapter.events[1], "PSHTR ON")
        self.assertEqual(adapter.events[2], ("heater warming", 60.0))

    def test_heater_off_waits_for_2_0_before_cool_then_zero(self):
        adapter = _HeaterAdapter([1, 2, 0], cool=120.0)
        adapter.zero_output = lambda **_kwargs: adapter.events.append("zero")
        adapter.enter_persistent_mode(zero_leads=True, timeout_s=0.1)
        self.assertEqual(adapter.events, ["PSHTR OFF", ("heater cooling", 120.0), "zero"])

    def test_persistent_transition_timeout_never_cools_or_zeros(self):
        adapter = _HeaterAdapter([1] + [2] * 100, cool=120.0, timeout=0.1)
        adapter.zero_output = lambda **_kwargs: adapter.events.append("zero")
        with self.assertRaises(APS100TimeoutError):
            adapter.enter_persistent_mode(zero_leads=True, timeout_s=0.1)
        self.assertEqual(adapter.events, ["PSHTR OFF"])
        self.assertNotIn("zero", adapter.events)
        self.assertFalse(any(isinstance(item, tuple) and item[0] == "heater cooling" for item in adapter.events))

    def test_invalid_cleanup_state_attempts_off_but_never_zeros(self):
        class InvalidAdapter(_HeaterAdapter):
            def get_heater_state(self):
                raise APS100Error("Invalid PSHTR? response '9'")

        adapter = InvalidAdapter([], cool=120.0)
        adapter.zero_output = lambda **_kwargs: adapter.events.append("zero")
        with self.assertRaisesRegex(APS100Error, "heater OFF is unconfirmed"):
            adapter.enter_persistent_mode(zero_leads=True, timeout_s=0.1)
        self.assertEqual(adapter.events, ["PSHTR OFF"])
        self.assertNotIn("zero", adapter.events)

    def test_snapshot_unknown_state_is_not_coerced_to_off(self):
        adapter = _HeaterAdapter([2])
        adapter.get_units = lambda: "kG"
        adapter.get_operating_mode = lambda: "Manual"
        adapter.get_limits_t = lambda: (-1.0, 1.0)
        adapter.get_sweep_state = lambda: "pause"
        adapter.get_voltage_limit_v = lambda: 3.0
        adapter.get_magnet_voltage_v = lambda: 0.0
        adapter.get_output_voltage_v = lambda: 0.0
        snapshot = adapter.read_snapshot()
        self.assertIsNone(snapshot.heater_on)
        self.assertEqual(snapshot.heater_state, 2)
        self.assertFalse(snapshot.driven_mode)
        self.assertFalse(snapshot.persistent_mode)


if __name__ == "__main__":
    unittest.main()
