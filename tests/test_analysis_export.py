import json
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np

import test_analysis_csv_export as fixtures


class AutomaticExportTests(unittest.TestCase):
    setUp = fixtures.AnalysisCsvExportTests.setUp
    snapshot = fixtures.AnalysisCsvExportTests.snapshot

    def test_short_names_preserve_conditions_and_exact_source_in_csv(self):
        from app.png_export import export_analysis
        snapshot = self.snapshot()
        source = self.root / 'D1_Doping-1.5to1.5V_DragDrive_Rs100kOhm_ACout50mV_VacEst11mV_LIA20mV_Pre10nA_20261002_175020.csv'
        snapshot['records'][0]['path'] = snapshot['map']['path'] = snapshot['traces'][0]['path'] = str(source)
        result = export_analysis(snapshot, heatmap_only=True)
        expected = 'D1_D-1.5to1.5V_DD_Rs100kOhm_AC50mV-est11mV_LIA20mV_Pre10nA_20261002_175020'
        self.assertEqual(Path(result['view_path']).parent.name, expected)
        self.assertIn('D-1.5to1.5V_DD_Rs100kOhm_AC50mV-est11mV', Path(result['outputs'][0]).name)
        rows = fixtures.AnalysisCsvExportTests.read_csv(self, result['csv_outputs'][0])
        self.assertIn(source.name, rows[2][0])
        self.assertIn(str(source).replace('\\', '\\\\'), rows[2][0])

    def test_automatic_names_keep_source_folder_and_self_describing_short_csv(self):
        from app.png_export import export_analysis
        snapshot = self.snapshot()
        result = export_analysis(snapshot)
        folder = self.root / 'D1_1.67K0T_17Hz_LIA20mV_Pre10nA_AC50mV-est11mV_20261002_175020'
        self.assertEqual({Path(p).parent for p in result['outputs'] + result['csv_outputs']}, {folder})
        self.assertEqual({Path(p).name for p in result['csv_outputs']},
                         {'I_drag_X_heatmap.csv', 'I_drag_X_cut_Vds_-1.15V.csv'})
        self.assertEqual(Path(result['outputs'][0]).name, 'D1_1.67K0T_17Hz_AC50mV-est11mV_I_drag_X_heatmap.png')
        self.assertEqual(Path(result['view_path']).name, 'I_drag_X_view.json')
        saved = json.loads(Path(result['view_path']).read_text(encoding='utf-8'))
        self.assertEqual(saved['view']['fixed_value'], -1.15)
        self.assertEqual(saved['sources'][0]['path'], snapshot['records'][0]['path'])

    def long_source_snapshot(self):
        snapshot = self.snapshot()
        stem = 'D1_Doping-1.5to1.5_DragDrive_Rs100kOhm_17Hz_'
        stem += 'a' * (222 - len(str(self.root)) - 1 - len(stem))
        source = self.root / (stem + '.csv')
        snapshot['records'][0]['path'] = snapshot['map']['path'] = snapshot['traces'][0]['path'] = str(source)
        source.with_suffix('').mkdir()
        return snapshot, source

    def test_long_png_name_fits_office_without_shortening_source_folder(self):
        from PIL import Image
        from app.png_export import export_analysis
        snapshot, source = self.long_source_snapshot()
        first = export_analysis(snapshot)
        self.assertEqual(Path(first['view_path']).parent, source.with_suffix(''))
        self.assertEqual(Path(first['outputs'][0]).name, 'I_drag_X_heatmap.png')
        self.assertEqual(Path(first['outputs'][1]).name, 'I_drag_X_cut_Vds_-1.15V.png')
        for path in first['outputs'] + first['csv_outputs'] + [first['view_path']]:
            self.assertLessEqual(len(path.encode('utf-16-le')) // 2, 259)
        with Image.open(first['outputs'][0]) as image:
            self.assertEqual(json.loads(image.info['SourceFiles']), [str(source)])
            image.verify()
        with patch('app.png_export.build_analysis_figure', side_effect=AssertionError('unexpected redraw')):
            repeat = export_analysis(snapshot)
        self.assertEqual(first['outputs'], repeat['outputs'])
        self.assertEqual(first['csv_outputs'], repeat['csv_outputs'])

    def test_existing_long_png_is_not_reused_as_an_office_compatible_output(self):
        from app.png_export import export_analysis
        from app.analysis_export import condition_stem, _view_key
        snapshot, source = self.long_source_snapshot()
        first = export_analysis(snapshot, heatmap_only=True)
        legacy_png = source.with_suffix('') / f'{condition_stem(source)}_I_drag_X_heatmap.png'
        Path(first['outputs'][0]).replace(legacy_png)
        view_path = Path(first['view_path'])
        payload = json.loads(view_path.read_text(encoding='utf-8'))
        payload['images'][0]['path'] = str(legacy_png)
        payload['export_key'] = _view_key(payload)
        view_path.write_text(json.dumps(payload), encoding='utf-8')
        original = legacy_png.read_bytes()
        repaired = export_analysis(snapshot, heatmap_only=True)
        self.assertEqual(Path(repaired['outputs'][0]).name, 'I_drag_X_heatmap.png')
        self.assertEqual(first['csv_outputs'], repaired['csv_outputs'])
        self.assertEqual(legacy_png.read_bytes(), original)

    def test_version_suffix_can_switch_to_short_png_name_without_losing_old_view(self):
        from app.png_export import export_analysis
        snapshot = self.snapshot()
        label = 'I_drag_X_heatmap.png'
        size = (259 - len(str(self.root)) - 3 - len(label)) // 2
        source = self.root / ('run_' + 'a' * (size - 4) + '.csv')
        snapshot['records'][0]['path'] = snapshot['map']['path'] = snapshot['traces'][0]['path'] = str(source)
        first = export_analysis(snapshot, heatmap_only=True)
        original = Path(first['outputs'][0]).read_bytes()
        self.assertGreaterEqual(len(first['outputs'][0]), 258)
        snapshot['view']['cmap'] = 'viridis'
        second = export_analysis(snapshot, heatmap_only=True)
        self.assertEqual(Path(second['outputs'][0]).name, label)
        self.assertEqual(Path(first['outputs'][0]).read_bytes(), original)
        self.assertEqual(first['csv_outputs'], second['csv_outputs'])
        self.assertLessEqual(len(second['outputs'][0]), 259)

    def test_extended_source_prefix_is_not_exposed_in_output_paths(self):
        from app.png_export import export_analysis
        snapshot, source = self.long_source_snapshot()
        extended = '\\\\?\\' + str(source)
        snapshot['records'][0]['path'] = snapshot['map']['path'] = snapshot['traces'][0]['path'] = extended
        result = export_analysis(snapshot, heatmap_only=True)
        self.assertEqual(Path(result['view_path']).parent, source.with_suffix(''))
        for path in result['outputs'] + result['csv_outputs'] + [result['view_path']]:
            self.assertFalse(path.startswith('\\\\?\\'))

    def test_save_as_checks_json_path_before_creating_any_export_files(self):
        from app.png_export import export_analysis
        filename = 'a' * (254 - len(str(self.root)) - 1 - 4) + '.png'
        with self.assertRaisesRegex(ValueError, '259'):
            export_analysis(self.snapshot(), self.root / filename, heatmap_only=True)
        self.assertEqual(list(self.root.iterdir()), [])

    def test_versioned_export_cannot_exceed_total_path_budget(self):
        from app.png_export import export_analysis
        from app.analysis_export import _reserve
        filename = 'a' * (259 - len(str(self.root)) - 1 - 4) + '.png'
        existing = self.root / filename
        existing.write_bytes(b'original user image')
        reserved = []
        with self.assertRaisesRegex(ValueError, '259'):
            _reserve(existing, reserved)
        self.assertEqual(reserved, [])
        self.assertEqual(existing.read_bytes(), b'original user image')

    def test_repeat_reuses_files_and_does_not_redraw(self):
        from app.png_export import export_analysis
        snapshot = self.snapshot()
        first = export_analysis(snapshot)
        paths = first['outputs'] + first['csv_outputs'] + [first['view_path']]
        before = {p: Path(p).stat().st_mtime_ns for p in paths}
        with patch('app.png_export.build_analysis_figure', side_effect=AssertionError('unexpected redraw')):
            second = export_analysis(snapshot)
        self.assertEqual(first['outputs'], second['outputs'])
        self.assertEqual(first['csv_outputs'], second['csv_outputs'])
        self.assertEqual(first['view_path'], second['view_path'])
        self.assertEqual(before, {p: Path(p).stat().st_mtime_ns for p in paths})

    def test_auto_visible_color_changes_png_but_keeps_full_csv_once(self):
        from app.png_export import build_analysis_figure, export_analysis
        snapshot = self.snapshot()
        snapshot['view']['clim_auto'] = True
        first = export_analysis(snapshot, heatmap_only=True)
        original_csv = Path(first['csv_outputs'][0]).read_bytes()
        snapshot['view']['map_xlim'] = [-1.6, -1.4]
        figure = build_analysis_figure(snapshot, map_only=True)
        np.testing.assert_allclose(figure.axes[0].collections[0].get_clim(), [3.7028462316839085, 6])
        figure.clear()
        zoom = export_analysis(snapshot, heatmap_only=True)
        self.assertNotEqual(first['outputs'], zoom['outputs'])
        self.assertEqual(first['csv_outputs'], zoom['csv_outputs'])
        self.assertEqual(Path(zoom['csv_outputs'][0]).read_bytes(), original_csv)
        snapshot['view'].update(clim_auto=False, clim=[0, 10e-12], map_ylim=[-1.16, -1.14])
        fixed = export_analysis(snapshot, heatmap_only=True)
        self.assertEqual(first['csv_outputs'], fixed['csv_outputs'])
        rows = fixtures.AnalysisCsvExportTests.read_csv(self, fixed['csv_outputs'][0])
        self.assertEqual((len(rows), len(rows[0])), (5, 4))
        self.assertEqual(len(list(Path(fixed['view_path']).parent.glob('*.csv'))), 1)

    def test_existing_source_folder_and_short_name_collisions_are_preserved(self):
        from app.png_export import export_analysis
        snapshot = self.snapshot()
        legacy = Path(snapshot['records'][0]['path']).with_suffix('')
        legacy.mkdir()
        (legacy / 'notes.txt').write_text('existing export notes')
        first = export_analysis(snapshot, heatmap_only=True)
        self.assertEqual(Path(first['view_path']).parent, legacy)
        self.assertEqual((legacy / 'notes.txt').read_text(), 'existing export notes')
        original = self.root / 'sample_D-1to1_DD.csv'
        snapshot['records'][0]['path'] = snapshot['map']['path'] = snapshot['traces'][0]['path'] = str(original)
        short = export_analysis(snapshot, heatmap_only=True)
        other = self.root / 'sample_Doping-1to1_DragDrive.csv'
        snapshot['records'][0]['path'] = snapshot['map']['path'] = snapshot['traces'][0]['path'] = str(other)
        other_result = export_analysis(snapshot, heatmap_only=True)
        self.assertNotEqual(Path(short['view_path']).parent, Path(other_result['view_path']).parent)
        self.assertEqual(Path(other_result['view_path']).parent, other.with_suffix(''))

    def test_literal_source_name_cannot_reuse_another_sources_compact_folder(self):
        from app.png_export import export_analysis
        snapshot = self.snapshot()
        first_source = self.root / 'run_Doping-1to1_DragDrive.csv'
        second_source = self.root / 'run_D-1to1_DD.csv'
        snapshot['records'][0]['path'] = snapshot['map']['path'] = snapshot['traces'][0]['path'] = str(first_source)
        first = export_analysis(snapshot, heatmap_only=True)
        snapshot['records'][0]['path'] = snapshot['map']['path'] = snapshot['traces'][0]['path'] = str(second_source)
        second = export_analysis(snapshot, heatmap_only=True)
        self.assertNotEqual(Path(first['view_path']).parent, Path(second['view_path']).parent)
        repeat = export_analysis(snapshot, heatmap_only=True)
        self.assertEqual(second['csv_outputs'], repeat['csv_outputs'])

    def test_cut_and_colormap_changes_reuse_unaffected_artifacts(self):
        from app.png_export import export_analysis
        snapshot = self.snapshot()
        first = export_analysis(snapshot)
        snapshot['view']['fixed_value'] = -.9
        snapshot['traces'][0]['y'] = np.array([6e-12, 7e-12, 8e-12])
        cut = export_analysis(snapshot)
        self.assertEqual(first['outputs'][0], cut['outputs'][0])
        self.assertEqual(first['csv_outputs'][0], cut['csv_outputs'][0])
        self.assertNotEqual(first['outputs'][1], cut['outputs'][1])
        self.assertTrue(cut['csv_outputs'][1].endswith('_cut_Vds_-0.9V.csv'))
        snapshot['view']['cmap'] = 'viridis'
        color = export_analysis(snapshot)
        self.assertNotEqual(cut['outputs'][0], color['outputs'][0])
        self.assertTrue(color['outputs'][0].endswith('_heatmap_v02.png'))
        self.assertEqual(cut['csv_outputs'], color['csv_outputs'])
        self.assertEqual(cut['outputs'][1], color['outputs'][1])
        snapshot['view']['map_xlim'] = [-1., 0.]
        zoom = export_analysis(snapshot)
        self.assertNotEqual(color['outputs'][0], zoom['outputs'][0])
        self.assertEqual(color['csv_outputs'], zoom['csv_outputs'])

    def test_changed_data_and_missing_or_edited_file_are_not_reused(self):
        from app.png_export import export_analysis
        snapshot = self.snapshot()
        first = export_analysis(snapshot, heatmap_only=True)
        Path(first['csv_outputs'][0]).write_text('user edited file', encoding='utf-8')
        second = export_analysis(snapshot, heatmap_only=True)
        self.assertEqual(Path(first['csv_outputs'][0]).read_text(), 'user edited file')
        self.assertNotEqual(first['csv_outputs'], second['csv_outputs'])
        self.assertEqual(first['outputs'], second['outputs'])
        snapshot['map']['z'][0, 0] = 9e-12
        third = export_analysis(snapshot, heatmap_only=True)
        self.assertNotEqual(second['outputs'], third['outputs'])
        self.assertNotEqual(second['csv_outputs'], third['csv_outputs'])
        Path(third['outputs'][0]).unlink()
        repaired = export_analysis(snapshot, heatmap_only=True)
        self.assertTrue(Path(repaired['outputs'][0]).is_file())
        Path(repaired['outputs'][0]).write_bytes(b'edited image')
        protected = export_analysis(snapshot, heatmap_only=True)
        self.assertEqual(Path(repaired['outputs'][0]).read_bytes(), b'edited image')
        self.assertNotEqual(protected['outputs'], repaired['outputs'])

    def test_edited_view_json_is_preserved_and_not_reused_for_restore(self):
        from app.png_export import export_analysis
        snapshot = self.snapshot()
        first = export_analysis(snapshot, heatmap_only=True)
        path = Path(first['view_path'])
        edited = json.loads(path.read_text(encoding='utf-8'))
        edited['view']['map_xlim'] = [100, 200]
        path.write_text(json.dumps(edited), encoding='utf-8')
        second = export_analysis(snapshot, heatmap_only=True)
        saved = json.loads(Path(second['view_path']).read_text(encoding='utf-8'))
        self.assertEqual(saved['view']['map_xlim'], [-1.6, .1])
        self.assertEqual(json.loads(path.read_text())['view']['map_xlim'], [100, 200])

    def test_saved_condition_and_title_changes_invalidate_export(self):
        from app.png_export import export_analysis
        snapshot = self.snapshot()
        first = export_analysis(snapshot, heatmap_only=True)
        metadata = snapshot['records'][0]['metadata']
        metadata['save_root']['device_id'] = 'D2'
        metadata['params'].update(n_sample=3, delay=.4)
        second = export_analysis(snapshot, heatmap_only=True)
        self.assertNotEqual(first['outputs'], second['outputs'])
        self.assertNotEqual(first['csv_outputs'], second['csv_outputs'])
        rows = fixtures.AnalysisCsvExportTests.read_csv(self, second['csv_outputs'][0])
        self.assertIn('"n_sample": 3', rows[2][0])
        self.assertIn('"delay": 0.4', rows[2][0])

    def test_failed_batch_cleans_new_artifacts_but_keeps_reused_heatmap(self):
        from app.png_export import export_analysis
        snapshot = self.snapshot()
        first = export_analysis(snapshot, heatmap_only=True)
        folder = Path(first['view_path']).parent
        before = {p.name: p.read_bytes() for p in folder.iterdir()}
        with patch('app.png_export._write_png', side_effect=OSError('disk full')):
            with self.assertRaisesRegex(OSError, 'disk full'):
                export_analysis(snapshot)
        self.assertEqual(before, {p.name: p.read_bytes() for p in folder.iterdir()})

    def test_csv_collision_in_save_as_preserves_existing_file(self):
        from app.png_export import export_analysis
        existing = self.root / 'map_heatmap.csv'
        existing.write_text('original user data', encoding='utf-8')
        result = export_analysis(self.snapshot(), self.root / 'map.png')
        self.assertEqual(existing.read_text(), 'original user data')
        self.assertTrue(all('_02' in Path(path).name for path in result['outputs'] + result['csv_outputs']))

    def test_csv_and_view_write_failures_leave_no_partial_batch(self):
        from app.png_export import export_analysis
        from app.analysis_csv_export import write_bytes_atomic
        for suffix in ('.csv', '.json'):
            with self.subTest(suffix=suffix):
                snapshot = self.snapshot()
                from app.analysis_export import export_folder
                folder = export_folder(snapshot['records'][0]['path'])
                def fail(path, data):
                    if Path(path).suffix == suffix:
                        raise OSError('write interrupted')
                    write_bytes_atomic(path, data)
                with patch('app.analysis_export.write_bytes_atomic', side_effect=fail):
                    with self.assertRaisesRegex(OSError, 'write interrupted'):
                        export_analysis(snapshot)
                self.assertEqual(list(folder.iterdir()), [])

    def test_origin_limit_is_checked_before_creating_export_files(self):
        from app.png_export import export_analysis
        filename = 'a' * (260 - len(str(self.root)) - 1 - 4) + '.png'
        with self.assertRaisesRegex(ValueError, '259'):
            export_analysis(self.snapshot(), self.root / filename, heatmap_only=True)
        self.assertEqual(list(self.root.iterdir()), [])

    def test_utf16_budget_counts_non_bmp_characters(self):
        from app.analysis_csv_export import validate_csv_path
        path = self.root / ('a' * (259 - len(str(self.root)) - 1 - 4))
        validate_csv_path(str(path) + '.csv')
        with self.assertRaisesRegex(ValueError, '259'):
            validate_csv_path(str(path)[:-1] + '\U0001f600.csv')

    def test_exported_cut_is_not_rediscovered_as_a_new_measurement(self):
        from app.png_export import export_analysis
        from app.curve_history import discover_history
        folder = self.root / 'map_2d'
        folder.mkdir()
        source = folder / 'measurement.csv'
        source.write_text('Doping,Vds,I_drag_X\nV,V,A\n0,-1,1e-12\n', encoding='utf-8')
        snapshot = self.snapshot()
        snapshot['map']['path'] = snapshot['records'][0]['path'] = snapshot['traces'][0]['path'] = str(source)
        snapshot['traces'].append({**snapshot['traces'][0]})
        export_analysis(snapshot)
        records, warnings = discover_history(self.root)
        self.assertEqual(warnings, [])
        self.assertEqual([record.path for record in records], [source])


if __name__ == '__main__':
    unittest.main()
