"""Source-grid validation and ramps using simulated instruments only."""
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from app.voltage_resolution import validate_source_targets
from app.utils import safe_ramp
from app.bias_control import ramp_pair
from app.models import DualGateParams
from tests.test_keithley_protection import ConnectionSafetyKeithley
from instruments.instrument import PyvisaInstrument


class GridTests(unittest.TestCase):
    def source(self, start=0.):
        source = ConnectionSafetyKeithley(start)
        source._output_channels = (source.VOLT_OUTPUT,)
        return source

    def writes(self, source):
        return [float(command.split()[-1]) for command in source.commands if command.startswith(':SOUR:VOLT:LEV ')]

    def test_refresh_once_and_validate_all_targets_without_writes(self):
        source = self.source()
        self.assertEqual(validate_source_targets(source, [0., .0005, .001], 'Bias1', .0006, True), .0005)
        self.assertEqual(source.events.count(('query', ':SOUR:VOLT:RANG?')), 1)
        self.assertEqual(self.writes(source), [])
        for target, step in ((.0001, .001), (.001, .0001), (21., .001)):
            with self.subTest(target=target,step=step), self.assertRaises(ValueError):
                validate_source_targets(source, [target], 'Bias1', step)
        self.assertEqual(source.events.count(('query', ':SOUR:VOLT:RANG?')), 1)

    def test_unknown_or_failed_refresh_is_closed(self):
        source = self.source()
        source._identity = 'MODEL 2450'
        with self.assertRaisesRegex(ValueError, 'unsupported'):
            validate_source_targets(source, [0.], 'Bias1', refresh=True)
        source._identity = 'MODEL 2400'
        with patch.object(source, 'refresh_source_voltage_range', side_effect=RuntimeError('offline read')):
            with self.assertRaisesRegex(ValueError, 'refresh failed'):
                validate_source_targets(source, [0.], 'Bias1', refresh=True)
        self.assertFalse(self.writes(source))

    def test_ramp_quantizes_intermediates_without_exceeding_step(self):
        source = self.source()
        with patch('app.utils.time.sleep'):
            safe_ramp(source.set_voltage, 0., .002, .00075, 0.)
        values = self.writes(source)
        self.assertEqual(values, [.0005, .001, .0015, .002])
        self.assertTrue(all(abs(b-a) <= .00075 + 1e-12 for a,b in zip([0.,*values], values)))
        self.assertEqual(source.events.count(('query', ':SOUR:VOLT:RANG?')), 1)

    def test_existing_unaligned_level_ramps_to_zero_and_rejects_unaligned_final(self):
        source = self.source(.0007)
        safe_ramp(source.set_voltage, 0., 0., .05, 0.)
        self.assertEqual(self.writes(source), [.0005, 0.])
        before = list(self.writes(source))
        with self.assertRaisesRegex(ValueError, 'not representable'):
            safe_ramp(source.set_voltage, 0., .0007, .05, 0.)
        self.assertEqual(self.writes(source), before)

    def test_plain_tiny_step_fails_instead_of_hanging(self):
        for step in (0., -1., float('nan'), 1e-12):
            with self.subTest(step=step), self.assertRaises(ValueError):
                safe_ramp(lambda v: None, 0., 1., step, 0.)

    def test_connect_takes_over_unaligned_level_without_changing_range(self):
        source = self.source(.0007)
        with patch.object(PyvisaInstrument, 'connect', autospec=True,
                          side_effect=lambda obj: setattr(obj, '_my_instr', object()) or obj), \
             patch('app.utils.time.sleep'):
            source.connect()
        self.assertEqual(self.writes(source), [.0005, 0.])
        self.assertIn(('query', ':SOUR:VOLT:RANG?'), source.events)
        self.assertFalse(any(kind == 'write' and ':SOUR:VOLT:RANG ' in value for kind,value in source.events))

    def test_pair_unknown_primary_resolution_fails_before_writes(self):
        primary, secondary = self.source(), self.source()
        primary._identity = 'MODEL 2450'
        primary.cached_source_voltage_range_v = secondary.cached_source_voltage_range_v = 20.
        p = DualGateParams(bias2_mode='Follow', bias2_offset=.1)
        with self.assertRaisesRegex(ValueError, 'unsupported'):
            ramp_pair(primary, None, 0, secondary, p, .1, .05, step_time=0)
        self.assertEqual(self.writes(primary), [])
        self.assertEqual(self.writes(secondary), [])
