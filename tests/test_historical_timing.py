import json
from pathlib import Path
import tempfile
import unittest

from app.quick.historical_timing import HistoryTiming


class HistoricalTimingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.params = {'mode': 'Raw', 'n_points': 3, 'delay': 0.5,
                       'n_sample': 2, 'sweep_both_ways': False, 'vg_ramp': 0.2}

    def write_run(self, name, status='finished', log='started\nfinished', params=None):
        (self.root / (name + '_metadata.json')).write_text(json.dumps({
            'measurement': 'gate_scan', 'status': status, 'params': params or self.params,
            'created_at': '2026-11-13T12:00:00', 'completed_at': '2026-11-13T12:01:30'}))
        (self.root / (name + '_run_log.txt')).write_text(log)

    def test_matches_complete_wall_time_only(self):
        self.write_run('good')
        self.write_run('stopped', status='stopped')
        self.write_run('paused', log='measurement paused')
        result = HistoryTiming().lookup('gate_scan', self.params, self.root)
        self.assertEqual(result['seconds'], 90)
        self.assertIn('good_metadata.json', result['source'])
        self.assertIsNone(HistoryTiming().lookup('gate_scan', dict(self.params, vg_ramp=0.3), self.root))

    def test_series_log_start_and_manifest_end(self):
        p = {'start_field_t': -0.1, 'stop_field_t': 0.1, 'measurement_mode': 'continuous',
             'field_approach': 'linear', 'round_trip': True, 'acquisition_delay_s': 0.5, 'averages': 3}
        (self.root / 'run_series_manifest.json').write_text(json.dumps({
            'measurement': 'bfield_transport', 'status': 'finished', 'params': p,
            'updated_at': '2026-11-13T16:22:34-05:00'}))
        (self.root / 'run_series_log.txt').write_text('2026-11-13 16:14:59 B-field Transport series started\n')
        result = HistoryTiming().lookup('bfield_transport', p, self.root)
        self.assertEqual(result['seconds'], 455)

    def test_missing_log_and_invalid_metadata_are_ignored(self):
        self.write_run('missing')
        (self.root / 'missing_run_log.txt').unlink()
        (self.root / 'bad_metadata.json').write_text('{')
        self.assertIsNone(HistoryTiming().lookup('gate_scan', self.params, self.root))

    def test_new_timing_setting_must_exist_in_history(self):
        self.write_run('old')
        self.assertIsNone(HistoryTiming().lookup('gate_scan', dict(self.params, new_delay=1), self.root))

    def test_save_root_lookup_is_read_only(self):
        from app.models import SaveRoot
        root = SaveRoot(base=str(self.root), user='a/b', device_id='x y')
        self.assertIsNone(HistoryTiming().lookup('gate_scan', self.params, root))
        self.assertFalse((self.root / 'a_b').exists())

    def test_actual_vds_and_photocurrent_measurements(self):
        for kind, params in [('vds_sweep', {'vds_start': 0, 'vds_stop': 1, 'vds_step': .1,
                                          'delay': .5, 'n_sample': 1, 'sweep_both_ways': False}),
                             ('photocurrent', {'wl_start': 500, 'wl_stop': 600, 'wl_step': 1,
                                               'delay': .1, 'n_sample': 1, 'bias_conditions': []})]:
            self.write_run(kind, params=params)
            path = self.root / (kind + '_metadata.json')
            data = json.loads(path.read_text()); data['measurement'] = kind
            path.write_text(json.dumps(data))
            self.assertEqual(HistoryTiming().lookup(kind, params, self.root)['seconds'], 90)

    def test_batch_checkpoint_strict_recipe_and_wall_span(self):
        from dataclasses import asdict
        from app.models import LineSweepParams
        params = asdict(LineSweepParams())
        values = dict(params, fields_t='0, 1', conditions=[], field_approach='linear',
                      field_rate_t_per_min=.1, field_settle_s=2)
        data = {'schema': 'gate_scan_bfield_batch_checkpoint_v1', 'status': 'complete',
                'resumed_from': None, 'updated_at': '2026-11-13T12:05:00-05:00',
                'fields_t': [0, 1], 'field_control': {'field_approach': 'linear',
                'rate_t_per_min': .1, 'field_settle_s': 2}, 'condition_count': 1,
                'conditions': [{'frozen_gate_scan_params': params}], 'next_job_index': 2,
                'results': [{'status': 'job_complete'}, {'status': 'job_complete'}]}
        (self.root / 'batch_bfield_batch_checkpoint.json').write_text(json.dumps(data))
        (self.root / 'batch_bfield_batch_log.txt').write_text('Started: 2026-11-13T12:00:00-05:00\n')
        self.assertEqual(HistoryTiming().lookup('bfield_gate_scan', values, self.root)['seconds'], 300)
        self.assertIsNone(HistoryTiming().lookup('bfield_gate_scan', dict(values, field_settle_s=3), self.root))
        data['resumed_from'] = 'old'; (self.root / 'batch_bfield_batch_checkpoint.json').write_text(json.dumps(data))
        self.assertIsNone(HistoryTiming().lookup('bfield_gate_scan', values, self.root))

    def test_invalidate_refreshes_cached_directory(self):
        history = HistoryTiming()
        self.assertIsNone(history.lookup('gate_scan', self.params, self.root))
        self.write_run('later')
        self.assertIsNone(history.lookup('gate_scan', self.params, self.root))
        history.invalidate()
        self.assertEqual(history.lookup('gate_scan', self.params, self.root)['seconds'], 90)
