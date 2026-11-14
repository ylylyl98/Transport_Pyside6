import tempfile
import unittest
import csv
from unittest.mock import patch
from pathlib import Path
from app.models import Connections, SaveRoot, DualGateParams, LineSweepParams, CoParams, PhotocurrentParams, PhotocurrentBiasCondition
from app.workers.dual_gate import DualGateWorker
from app.workers.line_sweep import LineSweepWorker
from app.workers.cosweep import CoSweepWorker
from app.workers.photocurrent import PhotocurrentWorker

class Source:
    identity = 'KEITHLEY INSTRUMENTS INC.,MODEL 2400,123,1'
    cached_source_voltage_range_v = 20.
    voltage = 0.
    def __init__(self): self.commands=[]; self.reads=0
    def refresh_source_voltage_range(self): self.reads+=1
    def get_voltage_setpoint(self): return self.voltage
    def set_voltage(self, value): self.voltage=value; self.commands.append(value)
    def ramp_voltage(self, value, step):
        from app.utils import safe_ramp
        safe_ramp(self.set_voltage, self.voltage, value, step, 0.)
    def acquire(self): return {'current': 1e-9}

class Guard:
    def acquire(self): raise AssertionError('acquisition must not start')
    def ramp_voltage(self, *args, **kwargs): raise AssertionError('DAQ output must not start')

class WorkerResolutionTests(unittest.TestCase):
    def test_bias2_changed_range_is_rejected_before_any_gate_write(self):
        from tests.test_bias2_core import FakeSource, FullManager
        events = []
        gates = {name: FakeSource(name, events) for name in ('g1', 'g2', 'g3', 'g4')}
        gates['g4'].cached_source_voltage_range_v = 20.
        def refresh_bias2():
            gates['g4'].cached_source_voltage_range_v = .2
        gates['g4'].refresh_source_voltage_range = refresh_bias2
        params = DualGateParams(vds_start=0., vds_stop=0., bias2_mode='Fixed', bias2_voltage=.5)
        with tempfile.TemporaryDirectory() as folder:
            worker = DualGateWorker(params, SaveRoot(base=folder), Connections(),
                                    **gates, daq=Guard(), device_manager=FullManager(gates))
            errors = []
            worker.error.connect(errors.append)
            worker.run()
        self.assertTrue(errors)
        self.assertIn('source range', errors[0])
        self.assertEqual(events, [])

    def test_every_worker_rounds_commands_and_csv_values(self):
        from tests.test_bias2_core import FakeDaq
        from types import SimpleNamespace
        cases = [
            (DualGateWorker, DualGateParams(vds_stop=.001, vds_step=.0003)),
            (LineSweepWorker, LineSweepParams(raw_vtg_stop=.001, n_points=4)),
            (CoSweepWorker, CoParams(vtg_stop=.001, vtg_step=.0003)),
            (PhotocurrentWorker, PhotocurrentParams(wl_start=500., wl_stop=500., use_vds=True, vds_source='Keithley 2400', bias_conditions=[PhotocurrentBiasCondition(vtg=.0003, vds=.0007, settle_s=0.)])),
        ]
        with tempfile.TemporaryDirectory() as folder:
            for worker_type, params in cases:
                with self.subTest(worker=worker_type.__name__), patch('app.utils.time.sleep'):
                    gates = [Source(), Source(), Source()]
                    params.delay=0.; params.n_sample=1
                    params.output_csv_path=str(Path(folder) / (worker_type.__name__ + '.csv'))
                    worker = worker_type(params, SaveRoot(base=folder, user='test', device_id='test'), Connections(),
                                         g1=gates[0], g2=gates[1], g3=gates[2], daq=FakeDaq(),
                                         mono=SimpleNamespace(set_wavelength=lambda value: None))
                    errors=[]; worker.error.connect(errors.append)
                    finished=[]; worker.finished.connect(finished.append)
                    worker.run()
                    self.assertFalse(errors, errors)
                    self.assertTrue(finished)
                    for gate in gates:
                        self.assertTrue(all(abs(v/.0005-round(v/.0005)) < 1e-7 for v in gate.commands))
                    paths = list(Path(folder).glob(worker_type.__name__ + '*.csv'))
                    self.assertTrue(paths)
                    for path in paths:
                        with path.open(newline='') as handle:
                            header, units, *rows = csv.reader(handle)
                        self.assertTrue(rows)
                        if worker_type is LineSweepWorker:
                            expected = [0., .0005, .0005, .001]
                            if params.sweep_both_ways:
                                expected += list(reversed(expected))
                            self.assertEqual([float(row[header.index('Vtg')]) for row in rows], expected)
                        if worker_type is PhotocurrentWorker:
                            self.assertIn('Vtg0.0003V', path.name)
                            self.assertEqual(float(rows[0][header.index('Vtg')]), .0005)
                        for row in rows:
                            for axis in ('Vtg', 'Vbg', 'Vds'):
                                value = float(row[header.index(axis)])
                                self.assertLess(abs(value/.0005-round(value/.0005)), 1e-7)

    def test_unknown_identity_and_too_small_ramp_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            for identity, ramp in [('UNKNOWN', .05), (Source.identity, .0001)]:
                gate=Source(); gate.identity=identity
                worker=DualGateWorker(DualGateParams(vds_ramp=ramp), SaveRoot(base=folder), Connections(), g3=gate, daq=Guard())
                errors=[]; worker.error.connect(errors.append); worker.run()
                self.assertTrue(errors); self.assertFalse(gate.commands)
