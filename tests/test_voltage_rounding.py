"""Regression coverage for resolution-aware targets; no physical instruments."""
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from app.voltage_resolution import validate_source_targets
from app.utils import safe_ramp
from tests import test_source_quantization as fixtures


class VoltageRoundingTests(unittest.TestCase):
    def test_screenshot_target_is_accepted_and_programmed_on_grid(self):
        source = fixtures.GridTests().source()
        validate_source_targets(source, [2.39130435], 'G2 / Vbg', refresh=True)
        with patch('app.utils.time.sleep'):
            safe_ramp(source.set_voltage, 0., 2.39130435, .1, 0.)
        writes = fixtures.GridTests().writes(source)
        self.assertEqual(writes[-1], 2.3915)
        self.assertTrue(all(abs(v / .0005 - round(v / .0005)) < 1e-7 for v in writes))

    def test_out_of_range_request_is_not_rescued_by_rounding(self):
        source = fixtures.GridTests().source()
        with self.assertRaisesRegex(ValueError, 'source range'):
            validate_source_targets(source, [20.0001], 'G2', refresh=True)
        self.assertEqual(fixtures.GridTests().writes(source), [])

    def test_half_ticks_signs_and_all_ranges(self):
        from app.voltage_resolution import RANGES, round_voltage
        for _, quantum in RANGES:
            for sign in (-1, 1):
                with self.subTest(quantum=quantum, sign=sign):
                    self.assertAlmostEqual(round_voltage(sign * 1.5 * quantum, quantum), sign * 2 * quantum)
                    self.assertEqual(round_voltage(sign * .1 * quantum, quantum), 0.)
                    self.assertEqual(round_voltage(sign * quantum, quantum), sign * quantum)
        for value in (float('nan'), float('inf')):
            with self.assertRaises(ValueError):
                round_voltage(value, .0005)

    def test_rounded_target_cannot_cross_protection_limit(self):
        from app.voltage_resolution import rounded_source_target
        source = SimpleNamespace(identity='MODEL 2400', cached_source_voltage_range_v=20.)
        with self.assertRaisesRegex(ValueError, 'protection'):
            rounded_source_target(source, 2.3913, limit=2.3913)

    def test_bias2_follow_rounds_primary_then_offset_on_its_own_range(self):
        from app.bias_control import ramp_pair, validate_bias2
        from app.models import DualGateParams
        from tests.test_bias2_core import FakeSource, FullManager
        events = []
        primary, secondary = FakeSource('g3', events), FakeSource('g4', events)
        secondary.cached_source_voltage_range_v = 2.
        params = DualGateParams(bias2_mode='Follow', bias2_offset=.000075)
        manager = FullManager({'g3': primary, 'g4': secondary})
        expected = validate_bias2(params, [.0007], manager)[0]
        result = ramp_pair(primary, None, 0, secondary, params, .0007, .05, step_time=0.)
        self.assertEqual(primary.voltage, .0005)
        self.assertEqual(secondary.voltage, .0006)
        self.assertEqual(result, expected)

    def test_direct_driver_write_rounds_and_updates_cached_output(self):
        source = fixtures.GridTests().source()
        source.refresh_source_voltage_range()
        source.set_voltage_fast(-2.39130435)
        self.assertEqual(fixtures.GridTests().writes(source), [-2.3915])
        self.assertEqual(source.output_values[source.VOLT_OUTPUT], -2.3915)

    def test_region_transition_waypoints_keep_their_order_and_values(self):
        from app.workers.base import RunWorker
        worker = RunWorker()
        worker.g1 = fixtures.GridTests().source()
        worker.g2 = fixtures.GridTests().source()
        points = [dict(vtg=.5003, vbg=.1003, moves=[('Vtg', .2003), ('Vbg', .1003), ('Vtg', .5003)])]
        worker.validate_source_trajectory(points, gate_ramp=.1, vds_ramp=.1, use_g3=False)
        self.assertEqual(points[0]['moves'], [('Vtg', .2005), ('Vbg', .1005), ('Vtg', .5005)])

    def test_bfield_preflight_and_apply_use_same_rounded_values(self):
        from app.engine.bfield_transport_controller import BFieldTransportController
        from app.engine.bfield_transport_sweep import TransportSweepPlan
        from app.models import BFieldTransportParams, BFieldTransportCondition
        sources = {name: fixtures.GridTests().source() for name in ('g1', 'g2', 'g3')}
        manager = SimpleNamespace(get_session=sources.get, is_voltage_source_mode=lambda _: True,
                                  applied_gate_voltage_limit=lambda _: 20.)
        params = BFieldTransportParams(ratio=1.15, conditions=[BFieldTransportCondition(
            doping=5.5, efield=0., vds=.0007, settle_s=0.)])
        controller = SimpleNamespace(device_manager=manager, tab=SimpleNamespace(),
            plan=TransportSweepPlan.from_params(params), _check_bias_progress=lambda: None)
        BFieldTransportController._validate_biases(controller, ['g1', 'g2', 'g3'], refresh=True)
        condition = controller.plan.conditions[0]
        self.assertEqual((condition.vtg, condition.vbg, condition.vds), (2.75, 2.3915, .0005))
        self.assertEqual((condition.doping, condition.efield), (5.5, 0.))
        with patch('app.utils.time.sleep'):
            BFieldTransportController._apply_biases(controller, condition)
        self.assertEqual(fixtures.GridTests().writes(sources['g2'])[-1], 2.3915)
        evidence = controller.plan.validation['voltage_rounding'][1]
        self.assertAlmostEqual(evidence['requested_v'], 5.5 / 2.3)
        self.assertEqual(evidence['programmed_v'], 2.3915)
