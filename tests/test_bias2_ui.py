import os
import tempfile
import unittest
from types import SimpleNamespace

if os.environ.get("TRANSPORT_OFFLINE_VERIFICATION") != "1":
    raise unittest.SkipTest("Run through verify_offline.py")

from PySide6.QtWidgets import QApplication
from app.models import Connections, SaveRoot, LineSweepParams
from app.quick.measurements import MeasurementService
from app.quick.parameters import defaults, build_params, measurement_sections
from app.quick.sweep_preview import build_sweep_preview
from tests.test_quick_measurements import FakeManager


class Bias2UiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.manager = FakeManager()
        self.manager.needs_reconnect = lambda name: False
        self.manager.readbacks = {}
        self.service = MeasurementService(self.manager, SaveRoot(base=self.temp.name))

    def tearDown(self):
        self.service.deleteLater()
        self.app.processEvents()
        self.temp.cleanup()

    def add_g4(self):
        self.manager.sessions['g4'] = SimpleNamespace(
            identity="KEITHLEY INSTRUMENTS INC.,MODEL 2400,x,y", cached_source_voltage_range_v=20.)

    def test_disabled_all_normal_modes_need_no_g4(self):
        for mode in range(4):
            devices, _ = self.service._validate(build_params(defaults(mode), mode), mode)
            self.assertNotIn('g4', devices)

    def test_follow_fails_without_device_and_reserves_only_when_enabled(self):
        params = LineSweepParams(bias2_mode='Follow', bias2_offset=.02)
        with self.assertRaisesRegex(ValueError, 'Bias2|G4'):
            self.service._validate(params, 1)
        self.add_g4()
        devices, _ = self.service._validate(params, 1)
        self.assertIn('g4', devices)
        params.bias2_mode = 'Disabled'
        self.assertNotIn('g4', self.service._validate(params, 1)[0])

    def test_fixed_bias2_works_without_bias1_and_follow_is_rejected(self):
        self.add_g4()
        params = LineSweepParams(vds_source='None', bias2_mode='Fixed', bias2_voltage=.05)
        self.manager.sessions.pop('g3')
        devices, _ = self.service._validate(params, 1)
        self.assertIn('g4', devices)
        self.assertNotIn('g3', devices)
        params.bias2_mode = 'Follow'
        with self.assertRaisesRegex(ValueError, 'Bias1'):
            self.service._validate(params, 1)

    def test_photocurrent_only_enabled_rows_require_bias2(self):
        params = build_params(defaults(3), 3)
        params.bias_conditions[0].bias2_mode = 'Fixed'
        self.add_g4()
        self.assertIn('g4', self.service._validate(params, 3)[0])
        params.bias_conditions[0].bias2_mode = 'Follow'
        with self.assertRaisesRegex(ValueError, 'Bias1'):
            self.service._validate(params, 3)

    def test_forms_have_mode_specific_fields_and_condition_routing(self):
        for mode in range(6):
            values = defaults(mode)
            rowkey = 'bias_conditions' if mode == 3 else 'conditions' if mode == 5 else None
            current = values[rowkey][0] if rowkey else values
            for selection, present, absent in [('Disabled', 'bias2_mode', 'bias2_voltage'),
                                                ('Fixed', 'bias2_voltage', 'bias2_offset'),
                                                ('Follow', 'bias2_offset', 'bias2_voltage')]:
                current['bias2_mode'] = selection
                fields = {f['key']: f for s in measurement_sections(values, mode) for f in s['fields']}
                self.assertIn(present, fields)
                self.assertNotIn(absent, fields)
                if rowkey:
                    self.assertEqual(fields[present]['rowKey'], rowkey)

    def test_gate_preview_tracks_forward_and_reverse_bias2(self):
        params = LineSweepParams(raw_vds_active=True, raw_vds_start=-.1, raw_vds_stop=.1,
                                 n_points=3, sweep_both_ways=True, bias2_mode='Follow', bias2_offset=.02)
        result = build_sweep_preview(1, params, Connections())
        trace = next(t for t in result['traces'] if t['label'] == 'Bias 2')
        self.assertAlmostEqual(trace['y'][0], -.08)
        self.assertAlmostEqual(trace['y'][-1], -.08)
        self.assertAlmostEqual(max(trace['y']), .12)
        self.assertEqual(result['pointCount'], 6)
        self.assertIn('Bias 2 (Follow)', result['summary'])

    def test_preview_rejects_offset_overvoltage(self):
        with self.assertRaisesRegex(ValueError, 'Bias 2'):
            build_sweep_preview(1, LineSweepParams(bias2_mode='Follow', bias2_offset=21.), Connections())

    def test_legacy_recipe_gains_disabled_defaults(self):
        values = defaults(3)
        values['bias_conditions'] = [dict(enabled=True, vtg=0., vbg=0., vds=.1, settle_s=.5)]
        from app.quick.parameters import convert
        migrated = convert(values['bias_conditions'], defaults(3)['bias_conditions'], 'bias_conditions', 3)
        self.assertEqual(migrated[0]['bias2_mode'], 'Disabled')

    def test_second_current_can_be_selected_and_is_available_for_condition_modes(self):
        self.service.setValue('bias2_mode', 'Fixed')
        self.service.setValue('plot_choice', 'Ibias2_Keithley')
        self.assertFalse(self.service.validationIssues)
        self.assertEqual(self.service.collect_params().plot_choice, 'Ibias2_Keithley')
        self.service.mode = 5
        self.service._values[5]['conditions'][0]['bias2_mode'] = 'Fixed'
        self.assertIn('Ibias2_Keithley', self.service._plot_channels('None'))


if __name__ == '__main__':
    unittest.main()
