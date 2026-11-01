"""Available Analysis selectors reflect finite saved data, without modifying it."""
import os
from pathlib import Path
import tempfile
import unittest

if os.environ.get('TRANSPORT_OFFLINE_VERIFICATION') != '1':
    raise unittest.SkipTest('Run through verify_offline.py')

import numpy as np
from app.curve_cache import SnapshotCache
from app.curve_history import HistoryRecord, cached_csv_snapshot
from app.quick.history_choices import resolve_choices


class HistoryChoiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.cache = SnapshotCache()

    def record(self, name, header, lines, kind='gate_scan', metadata=None):
        path = self.root / name
        path.write_text(header + '\n' + '\n'.join(lines) + '\n', encoding='utf-8')
        stat = path.stat()
        return HistoryRecord(path, kind, '2026-01-01', 'finished', '', tuple(header.split(',')), {},
                             metadata or {}, (stat.st_mtime_ns, stat.st_size))

    @staticmethod
    def view(**changes):
        return {**dict(x='Vtg', map_y='Vbg', signal='Ids_DC', auto_axes=False, primary_path=''), **changes}

    def test_finite_overlap_aliases_and_derived_ratios(self):
        record = self.record('alias.csv', 'Vtg,Efield,Keithley_current,I_drag_X,I_drag_Y,I_drive_X,I_drive_Y,Ids_DC',
            ['0,0,1,2,3,1,1,nan', '1,1,2,4,3,0,0,nan'])
        result = resolve_choices([record], self.view(signal='Ids_Keithley'), 'gate_scan', self.cache)
        self.assertIn('Ids_Keithley', result['signals'])
        self.assertIn('E-field', result['axes'])
        self.assertIn('Drag ratio X', result['signals'])
        self.assertIn('Drag ratio Y', result['signals'])
        self.assertIn('Drag ratio R', result['signals'], 'R is derived from X/Y when R columns are absent')
        self.assertNotIn('Ids_DC', result['signals'])
        zero = self.record('zero.csv', 'Vtg,I_drag_X,I_drive_X', ['0,2,0', '1,4,0'])
        result = resolve_choices([zero], self.view(signal='Drag ratio X'), 'gate_scan', self.cache)
        self.assertNotIn('Drag ratio X', result['signals'])
        self.assertNotEqual(result['view']['signal'], 'Drag ratio X')
        self.assertTrue(any('Drag ratio X' in item for item in result['warnings']))

    def test_curve_union_requires_finite_overlap_in_same_file_and_row(self):
        a = self.record('a.csv', 'Vtg,Vbg,Ids_DC,Ids_X', ['0,nan,1,nan', 'nan,2,nan,5'])
        b = self.record('b.csv', 'Vtg,Ids_Y', ['1,7', '2,8'])
        result = resolve_choices([a, b], self.view(), 'gate_scan', self.cache)
        self.assertIn('Ids_DC', result['signals'])
        self.assertIn('Ids_Y', result['signals'])
        self.assertNotIn('Ids_X', result['signals'], 'finite X and signal in different rows cannot plot')
        self.assertIn('Vtg', result['axes'])
        self.assertIn('Step Index', result['axes'])
        result = resolve_choices([a], self.view(x='Vbg', signal='Ids_X'), 'gate_scan', self.cache)
        self.assertEqual(result['view']['x'], 'Vbg')
        self.assertEqual(result['view']['signal'], 'Ids_X')
        self.assertNotIn('Ids_DC', result['signals'])

    def test_map_signal_and_axes_belong_to_primary_and_fallback_is_explained(self):
        a = self.record('a.csv', 'Vtg,Vbg,Ids_DC', ['0,0,1', '1,0,2', '0,1,3', '1,1,4'], 'map_2d')
        b = self.record('b.csv', 'Vtg,Vbg,Ids_X', ['0,0,1', '1,0,2', '0,1,3', '1,1,4'], 'map_2d')
        result = resolve_choices([a, b], self.view(signal='Ids_X', primary_path=str(a.path)), 'map_2d', self.cache)
        self.assertEqual(result['view']['signal'], 'Ids_DC')
        self.assertNotIn('Ids_X', result['signals'])
        self.assertTrue(any('Ids_X' in item for item in result['warnings']))
        result = resolve_choices([a, b], self.view(signal='Ids_DC', primary_path=str(b.path)), 'map_2d', self.cache)
        self.assertEqual(result['view']['signal'], 'Ids_X')
        self.assertNotIn('Ids_DC', result['signals'])
        self.assertNotEqual(result['view']['x'], result['view']['map_y'])

    def test_heterogeneous_1d_map_uses_union_and_auto_axes_can_restore_2d(self):
        one = self.record('one.csv', 'Vtg,Ids_DC', ['0,1', '1,2'], 'map_2d',
                          {'measurement': 'sweep_1d', 'params': {'axis_fast': 'Vtg', 'axis_slow': 'None'}})
        second = self.record('two.csv', 'Vtg,Ids_X', ['0,3', '1,4'], 'map_2d',
                             {'measurement': 'sweep_1d', 'params': {'axis_fast': 'Vtg', 'axis_slow': 'None'}})
        two_d = self.record('map.csv', 'Vtg,Vbg,Ids_Y', ['0,0,1', '1,0,2', '0,1,3', '1,1,4'], 'map_2d',
                            {'params': {'axis_fast': 'Vtg', 'axis_slow': 'Vbg'}})
        manual = self.view(map_y='None (1D)', primary_path=str(one.path), auto_axes=False)
        result = resolve_choices([one, second], manual, 'map_2d', self.cache)
        self.assertEqual(result['signals'], ['Ids_DC', 'Ids_X'])
        self.assertEqual(result['view']['primary_path'], str(one.path))
        self.assertIn('at least one selected file', result['note'])
        automatic = dict(manual, auto_axes=True)
        result = resolve_choices([one, second], automatic, 'map_2d', self.cache)
        self.assertEqual(result['signals'], ['Ids_DC', 'Ids_X'])
        self.assertEqual(result['view']['map_y'], 'None (1D)')
        automatic['primary_path'] = str(two_d.path)
        result = resolve_choices([one, second, two_d], automatic, 'map_2d', self.cache)
        self.assertEqual(result['view']['map_y'], 'Vbg')
        self.assertEqual(result['signals'], ['Ids_Y'])
        self.assertEqual(result['view']['primary_path'], str(two_d.path))
        self.assertIn('primary map', result['note'])
        # If a nominally 2D selection cannot draw two axes, the resulting
        # one-dimensional comparison still includes the other selected files.
        nominal = self.view(map_y='Vbg', primary_path=str(one.path), auto_axes=False)
        result = resolve_choices([one, second], nominal, 'map_2d', self.cache)
        self.assertEqual(result['view']['map_y'], 'None (1D)')
        self.assertEqual(result['signals'], ['Ids_DC', 'Ids_X'])

    def test_map_falls_back_to_1d_and_auto_axes_follow_metadata(self):
        one = self.record('one.csv', 'Vtg,Ids_DC', ['0,1', '1,2'], 'map_2d')
        result = resolve_choices([one], self.view(), 'map_2d', self.cache)
        self.assertEqual(result['view']['map_y'], 'None (1D)')
        self.assertIn('Step Index', result['axes'])
        self.assertTrue(any('Y coordinate' in warning for warning in result['warnings']))
        map_record = self.record('map.csv', 'Vtg,Vbg,Ids_DC', ['0,0,1', '1,0,2', '0,1,3', '1,1,4'], 'map_2d',
                                 {'params': {'axis_fast': 'Vbg', 'axis_slow': 'Vtg'}})
        result = resolve_choices([map_record], self.view(auto_axes=True), 'map_2d', self.cache)
        self.assertEqual((result['view']['x'], result['view']['map_y']), ('Vbg', 'Vtg'))

    def test_direction_filter_hides_signals_without_matching_finite_samples(self):
        record = self.record('direction.csv', 'Vtg,Direction,Ids_DC,Ids_X',
                             ['0,forward,1,nan', '1,forward,2,nan',
                              '0,backward,nan,3', '1,reverse,nan,4'])
        forward = resolve_choices([record], self.view(direction='forward'), 'gate_scan', self.cache)
        backward = resolve_choices([record], self.view(direction='backward'), 'gate_scan', self.cache)
        self.assertEqual(forward['signals'], ['Ids_DC'])
        self.assertEqual(backward['signals'], ['Ids_X'])
        self.assertEqual(backward['view']['signal'], 'Ids_X')
        self.assertTrue(any('Ids_DC' in warning for warning in backward['warnings']))
        empty = resolve_choices([record], self.view(direction='unknown'), 'gate_scan', self.cache)
        self.assertEqual(empty['signals'], [])
        self.assertEqual(empty['axes'], [])
        self.assertEqual(empty['view']['direction'], 'unknown')
        self.assertIn('filter', empty['note'].lower())
        self.assertEqual(resolve_choices([record], self.view(direction='forward'), 'gate_scan', self.cache), forward)

    def test_map_pass_and_direction_require_same_filtered_rows(self):
        record = self.record('passes.csv', 'Vtg,Vbg,FastDirection,PassIndex,Ids_DC,Ids_X',
                             ['0,0,forward,0,1,nan', '1,0,forward,0,2,nan',
                              '0,1,backward,1,nan,3', '1,1,backward,1,nan,4'], 'map_2d')
        forward = resolve_choices([record], self.view(direction='forward', **{'pass': 'First pass'}), 'map_2d', self.cache)
        backward = resolve_choices([record], self.view(direction='backward', **{'pass': '1'}), 'map_2d', self.cache)
        self.assertEqual(forward['signals'], ['Ids_DC'])
        self.assertEqual(backward['signals'], ['Ids_X'])
        self.assertEqual(backward['view']['pass'], '1')
        self.assertEqual(backward['view']['direction'], 'backward')
        empty = resolve_choices([record], self.view(direction='backward', **{'pass': '0'}), 'map_2d', self.cache)
        self.assertEqual((empty['signals'], empty['axes']), ([], []))
        self.assertEqual(empty['view']['pass'], '0')
        self.assertEqual(empty['view']['direction'], 'backward')
        self.assertIn('filter', empty['note'].lower())

    def test_fallback_prefers_changing_physical_axis_over_constant(self):
        record = self.record('raw.csv', 'Vds,Vtg,Ids_DC', ['0,1,2', '0,2,3'])
        result = resolve_choices([record], self.view(x='Doping'), 'gate_scan', self.cache)
        self.assertEqual(result['view']['x'], 'Vtg')
        self.assertTrue(any('Doping' in warning for warning in result['warnings']))

    def test_empty_selection_and_repeated_availability_keep_data_unchanged(self):
        view = self.view(signal='Ids_X')
        empty = resolve_choices([], view, 'gate_scan', self.cache)
        self.assertEqual(empty['view'], view)
        self.assertEqual((empty['signals'], empty['axes']), ([], []))
        record = self.record('cache.csv', 'Vtg,Ids_X', ['0,2', '1,3'])
        snapshot = cached_csv_snapshot(record.path, self.cache, record.fingerprint)
        original = [column.copy() for column in snapshot[2].columns if isinstance(column, np.ndarray)]
        first = resolve_choices([record], view, 'gate_scan', self.cache)
        second = resolve_choices([record], view, 'gate_scan', self.cache)
        self.assertEqual(first, second)
        after = [column for column in snapshot[2].columns if isinstance(column, np.ndarray)]
        for expected, actual in zip(original, after):
            np.testing.assert_array_equal(actual, expected)
