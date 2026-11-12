import json
import os
import tempfile
import unittest
from pathlib import Path
from app.curve_history import discover_history
from app.plot_export_labels import conditions

class AnalysisMetadataMappingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.cache = {}
    def csv(self, name, field=False):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(('B_measured_T,Ids_DC\n0,1\n' if field else 'Wavelength,Ids_DC\nnm,A\n500,1\n'), encoding='utf-8')
        return path
    def write(self, name, value):
        path = self.root / name
        path.write_text(json.dumps(value), encoding='utf-8')
        return path
    def test_photocurrent_explicit_paths_attach_shared_metadata_and_reindex_on_edit(self):
        a, b = self.csv('first.csv'), self.csv('second.csv')
        payload = {'measurement':'photocurrent', 'status':'finished', 'csv_paths':[str(a),str(b)],
                   'signal_chain':{'drag_drive':{'mode':'single_drive','series_resistance_ohm':1000}},
                   'params':{'bias_conditions':[{'enabled':True,'bias2_mode':'Fixed','bias2_voltage':.1},
                                                {'enabled':True,'bias2_mode':'Follow','bias2_offset':.2}]}}
        source = self.write('shared_metadata.json', payload)
        records, _ = discover_history(self.root, self.cache)
        bypath = {r.path:r for r in records}
        self.assertEqual(conditions({'path':str(a), 'metadata':bypath[a].metadata})['Bias2'], 'Fixed')
        self.assertEqual(conditions({'path':str(b), 'metadata':bypath[b].metadata})['Bias2'], 'Follow')
        self.assertEqual(bypath[b].metadata['signal_chain']['drag_drive']['mode'], 'single_drive')
        old_signature = self.cache[b][0]
        payload['status'] = 'stopped'
        source.write_text(json.dumps(payload) + ' ', encoding='utf-8')
        records, _ = discover_history(self.root, self.cache)
        self.assertEqual(next(r.status for r in records if r.path == b), 'stopped')
        self.assertNotEqual(self.cache[b][0], old_signature)
        source.unlink()
        records, _ = discover_history(self.root, self.cache)
        self.assertEqual(next(r.status for r in records if r.path == b), 'unknown')

    def test_relative_pc_paths_resolve_against_metadata_parent(self):
        a, b = self.csv('data/first.csv'), self.csv('data/second.csv')
        payload = {'measurement':'photocurrent', 'csv_paths':['first.csv','second.csv'],
            'signal_chain':{'drag_drive':{'mode':'single_drive','series_resistance_ohm':1000}},
            'params':{'bias_conditions':[{'enabled':True,'bias2_mode':'Fixed','bias2_voltage':.1},
                                         {'enabled':True,'bias2_mode':'Follow','bias2_offset':.2}]}}
        (self.root / 'data' / 'shared_metadata.json').write_text(json.dumps(payload), encoding='utf-8')
        records, _ = discover_history(self.root, self.cache)
        metadata = next(r.metadata for r in records if r.path == b)
        labels = conditions({'path':str(b), 'metadata':metadata})
        self.assertEqual(labels['Bias2'], 'Follow')
        self.assertEqual(labels['Lock-in role'], 'Single Drive')
        self.assertEqual(metadata['csv_paths'], [str(a.resolve()), str(b.resolve())])
        self.assertEqual(payload['csv_paths'], ['first.csv','second.csv'])

    def test_bfield_explicit_mapping_preserves_condition_and_calibration(self):
        a, b = self.csv('one.csv', True), self.csv('two.csv', True)
        rows = [{'enabled':False,'name':'skip'}, {'enabled':True,'name':'a','bias2_mode':'Fixed','bias2_voltage':.3},
                {'enabled':True,'name':'b','bias2_mode':'Follow','bias2_offset':.4}]
        self.write('run_series_manifest.json', {'schema':'bfield_transport_manifest_v1','status':'finished',
            'csv_paths':[str(a),str(b)], 'params':{'conditions':rows}, 'conditions':rows,
            'validation':{'calibration':{'signal_chain':{'drag_drive':{'mode':'single_drag'}}}}})
        records, _ = discover_history(self.root, self.cache)
        bypath = {r.path:r for r in records}
        self.assertEqual(bypath[b].measurement, 'bfield_transport')
        self.assertEqual(bypath[b].metadata['params']['name'], 'b')
        self.assertEqual(conditions({'path':str(b), 'metadata':bypath[b].metadata})['Bias2'], 'Follow')
        self.assertEqual(bypath[b].metadata['validation']['calibration']['signal_chain']['drag_drive']['mode'], 'single_drag')

    def test_mismatched_condition_counts_do_not_assign_a_bias_condition(self):
        a, b = self.csv('one.csv', True), self.csv('two.csv', True)
        row = {'enabled':True, 'bias2_mode':'Fixed', 'bias2_voltage':.7}
        self.write('run_series_manifest.json', {'schema':'bfield_transport_manifest_v1',
            'csv_paths':[str(a),str(b)], 'params':{'conditions':[row]}, 'conditions':[row]})
        records, _ = discover_history(self.root, self.cache)
        for record in records:
            self.assertNotIn('Bias2', conditions({'path':str(record.path), 'metadata':record.metadata}))

    def test_no_guess_outside_folder_duplicate_mapping_or_legacy_manifest(self):
        local = self.csv('sample.csv')
        self.write('unrelated_metadata.json', {'measurement':'photocurrent', 'status':'finished', 'csv_paths':[str(self.root.parent / local.name)]})
        self.write('old_series_manifest.json', {'schema':'bfield_transport_manifest_v1','status':'finished', 'conditions':[{'bias2_mode':'Fixed'}]})
        records, _ = discover_history(self.root, self.cache)
        self.assertEqual(records[0].status, 'unknown')
        for name in ('a_metadata.json','b_metadata.json'):
            self.write(name, {'measurement':'photocurrent', 'status':'finished', 'csv_paths':[str(local)]})
        records, warnings = discover_history(self.root, self.cache)
        self.assertEqual(records[0].status, 'unknown')
        self.assertTrue(any('multiple explicit metadata mappings' in w for w in warnings))
