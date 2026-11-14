import unittest
import json
import tempfile
from types import SimpleNamespace
from unittest.mock import patch

from PySide6 import QtWidgets

from app.quick.parameters import convert, defaults, measurement_sections
from app.engine.gate_scan_field_batch import GateScanFieldBatch
from tests.test_gate_scan_field_batch import _FakeMagnet, _FakeTab, _snapshot


class FieldApproachUiTests(unittest.TestCase):
    def test_quick_field_options_and_backend_scope(self):
        values = defaults(4)
        self.assertEqual(values['field_approach'], 'linear')
        self.assertEqual(values['field_settle_s'], 0.)
        sections = measurement_sections(values, 4, magnet_backend='1000')
        section = next(s for s in sections if s['title'] == 'Field approach')
        self.assertEqual([f['key'] for f in section['fields']],
                         ['field_rate_t_per_min', 'field_approach', 'field_settle_s'])
        self.assertEqual([o['value'] for o in section['fields'][1]['options']],
                         ['linear', 'no_overshoot', 'oscillate'])
        self.assertIn('stability', next(s for s in sections if s['title'] == 'Magnetic field targets')['description'])
        acquisition = next(s for s in sections if s['title'] == 'Acquisition')
        self.assertEqual([f['label'] for f in acquisition['fields'][:2]], ['Averages', 'Gate settle delay (s)'])

        values = defaults(5)
        for mode, expected in [('continuous', ['linear', 'no_overshoot']),
                               ('stepped', ['linear', 'no_overshoot', 'oscillate', 'linear_no_backward'])]:
            values['measurement_mode'] = mode
            sections = measurement_sections(values, 5, magnet_backend='1000')
            section = next(s for s in sections if s['title'] == ('Endpoint approach' if mode == 'continuous' else '1 · Sweep'))
            if mode == 'continuous':
                self.assertTrue(section['advanced'])
                sweep = next(s for s in sections if s['title'] == '1 · Sweep')
                self.assertNotIn('field_approach', [f['key'] for f in sweep['fields']])
            item = next(f for f in section['fields'] if f['key'] == 'field_approach')
            self.assertEqual([o['value'] for o in item['options']], expected)
            self.assertEqual(item['value'], 'linear' if mode == 'continuous' else 'no_overshoot')
        values.update(measurement_mode='continuous', field_approach='oscillate')
        sections = measurement_sections(values, 5, magnet_backend='1000')
        item = next(f for s in sections for f in s['fields'] if f['key'] == 'field_approach')
        self.assertEqual(item['value'], 'oscillate')
        self.assertIn('requires', item['error'])
        self.assertIn('unsupported', next(s for s in sections if s['title'] == '1 · Sweep')['description'])
        values.update(measurement_mode='continuous', field_approach='linear_no_backward')
        sections = measurement_sections(values, 5, magnet_backend='1000')
        item = next(f for s in sections for f in s['fields'] if f['key'] == 'field_approach')
        self.assertEqual(item['value'], 'linear_no_backward')
        self.assertIn('unsupported', item['options'][-1]['label'])
        self.assertIn('requires', item['error'])
        self.assertEqual(convert('linear_no_backward', 'auto', 'field_approach', 5), 'linear_no_backward')
        with self.assertRaises(ValueError):
            convert('linear_no_backward', 'linear', 'field_approach', 4)
        for mode in (4, 5):
            sections = measurement_sections(defaults(mode), mode, magnet_backend='2100')
            self.assertFalse(any(f['key'] == 'field_approach' for s in sections for f in s['fields']))

    def test_batch_captures_shared_settings_and_rejects_bad_values(self):
        tab = SimpleNamespace(get_field_control_settings=lambda: {'field_approach': 'oscillate', 'rate_t_per_min': .01},
                              capture_field_batch_requests=lambda: [(SimpleNamespace(), (), None, 'A'),
                                                                     (SimpleNamespace(), (), None, 'B')])
        batch = SimpleNamespace(tab=tab)
        requests = GateScanFieldBatch._capture_requests(batch, (0., 1.))
        self.assertEqual([(r.field_approach, r.field_rate_t_per_min) for r in requests], [('oscillate', .01)] * 2)
        tab.get_field_control_settings = lambda: {'field_approach': 'invalid', 'rate_t_per_min': .01}
        with self.assertRaises(ValueError):
            GateScanFieldBatch._capture_requests(batch, (0.,))
        tab.get_field_control_settings = lambda: {'field_approach': 'linear', 'rate_t_per_min': -1.}
        with self.assertRaises(ValueError):
            GateScanFieldBatch._capture_requests(batch, (0.,))

    def test_batch_passes_and_records_shared_field_control(self):
        with tempfile.TemporaryDirectory() as folder:
            magnet, tab = _FakeMagnet(), _FakeTab(folder)
            tab.get_field_control_settings = lambda: {'field_approach': 'no_overshoot', 'rate_t_per_min': .01}
            batch = GateScanFieldBatch(magnet, tab)
            batch.set_review_validator(lambda: True)
            batch._on_snapshot(_snapshot(0.))
            with patch('app.engine.gate_scan_field_batch.QtWidgets.QMessageBox.question', return_value=QtWidgets.QMessageBox.StandardButton.Yes):
                self.assertTrue(batch.start('0.25'))
            move = next(call for call in magnet.calls if call[0] == 'move')
            self.assertEqual(move[2]['field_approach'], 'no_overshoot')
            self.assertEqual(move[2]['rate_t_per_min'], .01)
            with open(batch._checkpoint_path, encoding='utf-8') as handle:
                saved = json.load(handle)
            self.assertEqual(saved['field_control'], {'field_approach': 'no_overshoot', 'rate_t_per_min': .01, 'field_settle_s': 0.})
            self.assertEqual(batch._metadata(.25, _snapshot(.25))['gate_scan_bfield_batch']['field_approach'], 'no_overshoot')
            batch._measurement_monitor_timer.stop()
            batch._cooldown_timer.stop()
            batch._verification_timer.stop()
            batch._settle_timer.stop()
