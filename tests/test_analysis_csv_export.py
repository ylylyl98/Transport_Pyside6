import csv
import io
import json
import tempfile
import unittest
from pathlib import Path

import numpy as np


class AnalysisCsvExportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def snapshot(self):
        source = str(self.root / 'D1_1.67K0T_17Hz_LIA20mV_Pre10nA_ACout50mV_VacEst11mV_20261002_175020.csv')
        return {'mode': 'map', 'kind': 'map_2d', 'signal': 'I_drag_X',
                'x_name': 'Doping', 'x_unit': 'V', 'y_unit': 'A',
                'records': [{'path': source, 'created_at': '2026-10-02T21:57:10',
                             'status': 'finished', 'fingerprint': [1, 200],
                             'metadata': {'save_root': {'device_id': 'D1'},
                                          'params': {'base_name': 'sample, "test"\nnext line'},
                                          'signal_chain': {'frequency_hz': 17}}}],
                'map': {'path': source, 'x_name': 'Doping', 'y_name': 'Vds',
                        'x_unit': 'V', 'y_unit': 'V', 'signal_unit': 'A',
                        'x': np.array([-1.5, -.75, 0.]), 'y': np.array([-1.15, -.9]),
                        'z': np.array([[3.7028462316839085e-12, np.nan, 5e-12], [6e-12, 7e-12, 8e-12]])},
                'traces': [{'path': source, 'x': np.array([-1.5, -.75, 0.]),
                            'y': np.array([3.7028462316839085e-12, np.nan, 5e-12]),
                            'direction': 'All', 'condition': ''}],
                'view': {'fixed_axis': 'Vds', 'fixed_value': -1.15, 'direction': 'All',
                         'pass': 'All rows', 'normalization': 'raw', 'cmap': 'RdBu_r',
                         'map_xlim': [-1.6, .1], 'map_ylim': [-1.2, -.8], 'clim': [0., 8e-12],
                         'xlim': [-1.6, .1], 'ylim': [0., 8e-12], 'legend': True},
                'warnings': []}

    def read_csv(self, path):
        return list(csv.reader(io.StringIO(Path(path).read_text(encoding='utf-8-sig'))))

    def test_matrix_has_three_label_rows_and_roundtrips_coordinates_and_values(self):
        from app.png_export import export_analysis
        result = export_analysis(self.snapshot(), self.root / 'heatmap.png', heatmap_only=True)
        self.assertEqual(len(result.get('csv_outputs', [])), 1)
        path = Path(result['csv_outputs'][0])
        rows = self.read_csv(path)
        self.assertEqual(rows[0], ['Vds', '-1.5', '-0.75', '0.0'])
        self.assertEqual(rows[1], ['V', 'A', 'A', 'A'])
        self.assertIn('20261002_175020.csv', rows[2][0])
        self.assertIn('2026-10-02T21:57:10', rows[2][0])
        self.assertIn('SourceFile', rows[2][0])
        self.assertIn('Doping', rows[2][1])
        self.assertEqual(len(path.read_text(encoding='utf-8-sig').splitlines()), 5)
        values = np.array(rows[3:], dtype=float)
        np.testing.assert_equal(values, [[-1.15, 3.7028462316839085e-12, np.nan, 5e-12], [-.9, 6e-12, 7e-12, 8e-12]])
        saved = json.loads(Path(result['view_path']).read_text(encoding='utf-8'))
        self.assertEqual(saved['csv_files'][0]['path'], str(path))

    def test_cut_preserves_normalization_actual_fixed_x_and_unequal_trace_coordinates(self):
        from app.png_export import export_analysis
        snapshot = self.snapshot()
        snapshot.update(x_name='Vds', y_unit='')
        snapshot['view'].update(fixed_axis='Doping', fixed_value=-.75, normalization='max_abs')
        snapshot['traces'][0].update(x=np.array([-.9, -1.15]), y=np.array([1., .5]))
        other = {**snapshot['records'][0], 'path': str(self.root / 'other.csv')}
        snapshot['records'].append(other)
        snapshot['traces'].append({'path': other['path'], 'x': np.array([-1.]), 'y': np.array([.25]), 'direction': 'forward', 'condition': ''})
        result = export_analysis(snapshot, self.root / 'cut.png', cut_only=True)
        self.assertEqual(len(result.get('csv_outputs', [])), 1)
        rows = self.read_csv(result['csv_outputs'][0])
        self.assertEqual(rows[0], ['Vds', 'I_drag_X', 'Vds', 'I_drag_X'])
        self.assertEqual(rows[1], ['V', '', 'V', ''])
        self.assertIn('max_abs', rows[2][1])
        self.assertIn('-0.75', rows[2][1])
        self.assertIn('Doping', rows[2][1])
        self.assertIn('other.csv', rows[2][2])
        self.assertEqual(rows[3:], [['-0.9', '1.0', '-1.0', '0.25'], ['-1.15', '0.5', '', '']])

    def test_heatmap_stays_physical_when_cuts_normalized(self):
        from app.png_export import export_analysis
        snapshot = self.snapshot()
        snapshot['view']['normalization'] = 'max_abs'
        snapshot['y_unit'] = ''
        result = export_analysis(snapshot, self.root / 'map.png', heatmap_only=True)
        self.assertEqual(len(result.get('csv_outputs', [])), 1)
        rows = self.read_csv(result['csv_outputs'][0])
        self.assertEqual(rows[1], ['V', 'A', 'A', 'A'])
        self.assertIn('raw', rows[2][1])
        self.assertNotIn('max_abs', rows[2][1])


if __name__ == '__main__':
    unittest.main()
