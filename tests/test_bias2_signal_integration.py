"""Cross-feature regressions for two bias sources and selected AC roles."""
import csv
import math
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from app.models import Connections, CoParams, DualGateParams, SaveRoot
from app.workers.cosweep import CoSweepWorker
from app.workers.dual_gate import DualGateWorker
from tests.test_bias2_core import FakeSource, FakeDaq, FullManager


class BiasSignalIntegrationTests(unittest.TestCase):
    def run_worker(self, worker_type, params, chain=None):
        events = []
        sources = {key: FakeSource(key, events) for key in ('g1', 'g2', 'g3', 'g4')}
        with tempfile.TemporaryDirectory() as folder, patch('app.utils.time.sleep'), patch('app.bias_control.time.sleep'):
            params.output_csv_path = str(Path(folder) / 'scan.csv')
            worker = worker_type(params, SaveRoot(), Connections(), **sources,
                                 daq=FakeDaq(), device_manager=FullManager(sources),
                                 signal_chain=chain or {}, plot_choice='Ibias2_Keithley')
            failures, records, plotted = [], [], []
            worker.error.connect(failures.append)
            worker.point_data.connect(records.append)
            worker.point.connect(lambda x, y: plotted.append(y))
            worker.run()
            self.assertEqual(failures, [])
            with open(params.output_csv_path, newline='') as handle:
                columns, units, *data = csv.reader(handle)
            rows = [dict(zip(columns, row)) for row in data]
        return rows, records, plotted

    def test_map_fast_slow_and_held_bias_follow_each_physical_point(self):
        for fast, slow in (('Vds', 'Vtg'), ('Vtg', 'Vds'), ('Vtg', 'Vbg')):
            with self.subTest(fast=fast, slow=slow):
                params = CoParams(axis_fast=fast, axis_slow=slow, vtg_start=0, vtg_stop=.1,
                                  vbg_start=0, vbg_stop=.1, vds_start=0, vds_stop=.1, vds_step=.1,
                                  bias2_mode='Follow', bias2_offset=-.02, delay=0, n_sample=1)
                rows, records, plotted = self.run_worker(CoSweepWorker, params)
                self.assertEqual(len(rows), 4)
                for row, record in zip(rows, records):
                    self.assertAlmostEqual(float(row['Bias2']), float(row['Vds']) - .02)
                    self.assertAlmostEqual(record['Bias2'], record['vds'] - .02)
                    self.assertAlmostEqual(float(row['Ibias2_Keithley']), 2e-9)
                self.assertEqual(plotted, [2e-9] * 4)

    def test_single_drive_records_ac_and_both_smu_currents_without_drag(self):
        chain = {'drag_drive': dict(mode='single_drive', verified=True, drive_sensitivity_v=.1,
                 drive_preamp_gain=10., series_resistance_ohm=1000., dc_enabled=False)}
        params = DualGateParams(vds_start=0, vds_stop=.1, vds_step=.1,
                                bias2_mode='Follow', bias2_offset=.02, delay=0, n_sample=1)
        rows, records, plotted = self.run_worker(DualGateWorker, params, chain)
        self.assertEqual(len(rows), 2)
        for row in rows:
            self.assertAlmostEqual(float(row['I_drive_X']), 1e-6)
            self.assertAlmostEqual(float(row['Ids_Keithley']), 1e-9)
            self.assertAlmostEqual(float(row['Ibias2_Keithley']), 2e-9)
            self.assertTrue(math.isnan(float(row['Ids_DC'])))
            self.assertNotIn('I_drag_X', row)
        self.assertEqual(plotted, [2e-9, 2e-9])


if __name__ == '__main__':
    unittest.main()
