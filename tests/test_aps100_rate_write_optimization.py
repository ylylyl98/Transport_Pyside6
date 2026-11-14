import unittest

from app.devices.aps100_attodry1000_adapter import APS100AttoDry1000Adapter, APS100Error


class FakeSerialAdapter(APS100AttoDry1000Adapter):
    """Exercise real query parsing while recording all simulated serial traffic."""
    def __init__(self, rates=None):
        self.coil_constant_t_per_a = 1.0 / 60.0
        self.rates = dict(rates or {i: .0049 for i in range(5)})
        self.boundaries = {i: float((i + 1) * 10) for i in range(5)}
        self.events = []
        self.responses = {}

    def _query(self, command):
        self.events.append(command)
        if command in self.responses:
            response = self.responses[command]
            if isinstance(response, Exception):
                raise response
            return response
        kind, index = command.split()
        index = int(index)
        return str(self.boundaries[index] if kind == 'RANGE?' else self.rates[index])

    def _write(self, command):
        self.events.append(command)
        _, index, rate = command.split()
        self.rates[int(index)] = float(rate)

    def captured(self, rates):
        return {i: (self.boundaries[i], rate) for i, rate in rates.items()}


class RateWriteOptimizationTests(unittest.TestCase):
    def test_unchanged_rate_is_freshly_read_without_write(self):
        adapter = FakeSerialAdapter()
        result = adapter.restore_rates(adapter.captured({0: .0049}))
        self.assertEqual(adapter.events, ['RANGE? 0', 'RATE? 0'])
        self.assertAlmostEqual(result[0], .0049)

    def test_changed_rate_is_read_written_and_verified(self):
        adapter = FakeSerialAdapter()
        result = adapter.restore_rates(adapter.captured({0: .0009}))
        self.assertEqual(adapter.events, ['RANGE? 0', 'RATE? 0', 'RATE 0 0.0009000', 'RATE? 0'])
        self.assertAlmostEqual(result[0], .0009)

    def test_failed_or_invalid_preread_fails_closed(self):
        for response in (RuntimeError('serial error'), 'nan', '1e999', '-1', '0'):
            with self.subTest(response=response):
                adapter = FakeSerialAdapter()
                adapter.responses['RATE? 0'] = response
                with self.assertRaises(APS100Error):
                    adapter.restore_rates(adapter.captured({0: .0009}))
                self.assertEqual(adapter.events, ['RANGE? 0', 'RATE? 0'])

    def test_changed_boundary_prevents_rate_write(self):
        adapter = FakeSerialAdapter()
        with self.assertRaises(APS100Error):
            adapter.restore_rates({0: (11., .0009)})
        self.assertEqual(adapter.events, ['RANGE? 0'])

    def test_callbacks_include_skips_and_failures_and_continue(self):
        adapter = FakeSerialAdapter()
        adapter.responses['RATE? 1'] = RuntimeError('unavailable')
        callbacks = []
        with self.assertRaises(APS100Error) as error:
            adapter.restore_rates(adapter.captured({0: .0049, 1: .0009, 2: .0009}),
                                  step_callback=lambda: callbacks.append(list(adapter.events)))
        self.assertIn('range 1', str(error.exception))
        self.assertEqual([events[-1] for events in callbacks], ['RATE? 0', 'RATE? 1', 'RATE? 2'])
        self.assertEqual(adapter.events, ['RANGE? 0', 'RATE? 0', 'RANGE? 1', 'RATE? 1',
                                          'RANGE? 2', 'RATE? 2', 'RATE 2 0.0009000', 'RATE? 2'])

    def test_write_failure_continues_other_ranges_and_aggregates(self):
        adapter = FakeSerialAdapter()
        original_write = adapter._write
        callbacks = []

        def write(command):
            if command.startswith('RATE 0 '):
                adapter.events.append(command)
                raise RuntimeError('write failed')
            original_write(command)

        adapter._write = write
        with self.assertRaises(APS100Error) as error:
            adapter.restore_rates(adapter.captured({0: .0009, 1: .0009}),
                                  step_callback=lambda: callbacks.append(1))
        self.assertIn('range 0', str(error.exception))
        self.assertIn('write failed', str(error.exception))
        self.assertEqual(adapter.rates[0], .0049)
        self.assertEqual(adapter.rates[1], .0009)
        self.assertEqual(callbacks, [1, 1])
        self.assertEqual(adapter.events[-4:], ['RANGE? 1', 'RATE? 1', 'RATE 1 0.0009000', 'RATE? 1'])

    def test_bad_postwrite_readback_fails(self):
        for response in ('0.003', '1e999', 'nan'):
            with self.subTest(response=response):
                adapter = FakeSerialAdapter()
                original_query = adapter._query
                count = [0]

                def query(command):
                    if command == 'RATE? 0':
                        count[0] += 1
                        if count[0] == 2:
                            adapter.responses[command] = response
                    return original_query(command)

                adapter._query = query
                with self.assertRaises(APS100Error):
                    adapter.restore_rates(adapter.captured({0: .0009}))
                self.assertEqual(adapter.events, ['RANGE? 0', 'RATE? 0', 'RATE 0 0.0009000', 'RATE? 0'])

    def test_nonfinite_boundary_fails_before_write(self):
        adapter = FakeSerialAdapter()
        adapter.responses['RANGE? 0'] = '1e999'
        with self.assertRaises(APS100Error):
            adapter.restore_rates(adapter.captured({0: .0009}))
        self.assertEqual(adapter.events, ['RANGE? 0'])

    def test_external_rate_change_is_detected_on_next_call(self):
        adapter = FakeSerialAdapter()
        target = adapter.captured({0: .0049})
        adapter.restore_rates(target)
        adapter.rates[0] = .002
        adapter.restore_rates(target)
        self.assertEqual(adapter.events, ['RANGE? 0', 'RATE? 0', 'RANGE? 0', 'RATE? 0',
                                          'RATE 0 0.0049000', 'RATE? 0'])

    def test_lower_and_restore_only_write_three_changed_ranges_each_time(self):
        original = {0: .0049, 1: .0049, 2: .0049, 3: .0001, 4: .0001}
        reduced = {0: .0009, 1: .0009, 2: .0009, 3: .0001, 4: .0001}
        adapter = FakeSerialAdapter(original)
        adapter.restore_rates(adapter.captured(reduced))
        adapter.restore_rates(adapter.captured(original))
        writes = [event for event in adapter.events if event.startswith('RATE ')]
        self.assertEqual(writes, [f'RATE {i} 0.0009000' for i in range(3)] +
                                 [f'RATE {i} 0.0049000' for i in range(3)])
        self.assertFalse(any(event.endswith(' 5') or event.startswith('RATE 5 ') for event in adapter.events))


if __name__ == '__main__':
    unittest.main()
