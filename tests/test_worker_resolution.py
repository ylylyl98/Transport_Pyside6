import tempfile
import unittest
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
    def set_voltage(self, value): self.commands.append(value)
    def ramp_voltage(self, *args, **kwargs): self.commands.append(args)

class Guard:
    def acquire(self): raise AssertionError('acquisition must not start')
    def ramp_voltage(self, *args, **kwargs): raise AssertionError('DAQ output must not start')

class WorkerResolutionTests(unittest.TestCase):
    def test_bias2_changed_range_is_rejected_before_any_gate_write(self):
        from tests.test_bias2_core import FakeSource, FullManager
        events = []
        gates = {name: FakeSource(name, events) for name in ('g1', 'g2', 'g3', 'g4')}
        gates['g4'].cached_source_voltage_range_v = .2
        def refresh_bias2():
            gates['g4'].cached_source_voltage_range_v = 20.
        gates['g4'].refresh_source_voltage_range = refresh_bias2
        params = DualGateParams(vds_start=0., vds_stop=0., bias2_mode='Fixed', bias2_voltage=.000005)
        with tempfile.TemporaryDirectory() as folder:
            worker = DualGateWorker(params, SaveRoot(base=folder), Connections(),
                                    **gates, daq=Guard(), device_manager=FullManager(gates))
            errors = []
            worker.error.connect(errors.append)
            worker.run()
        self.assertTrue(errors)
        self.assertIn('not representable', errors[0])
        self.assertEqual(events, [])

    def test_every_worker_rejects_unrepresentable_point_before_outputs(self):
        cases = [
            (DualGateWorker, DualGateParams(vds_stop=.001, vds_step=.0003)),
            (LineSweepWorker, LineSweepParams(raw_vtg_stop=.001, n_points=4)),
            (CoSweepWorker, CoParams(vtg_stop=.001, vtg_step=.0003)),
            (PhotocurrentWorker, PhotocurrentParams(bias_conditions=[PhotocurrentBiasCondition(vtg=.0003)])),
        ]
        with tempfile.TemporaryDirectory() as folder:
            for worker_type, params in cases:
                with self.subTest(worker=worker_type.__name__):
                    gates = [Source(), Source(), Source()]
                    worker = worker_type(params, SaveRoot(base=folder, user='test', device_id='test'), Connections(),
                                         g1=gates[0], g2=gates[1], g3=gates[2], daq=Guard(), mono=Guard())
                    errors=[]; worker.error.connect(errors.append)
                    worker.run()
                    self.assertTrue(errors)
                    self.assertIn('not representable', errors[0])
                    self.assertTrue(all(not gate.commands for gate in gates))

    def test_unknown_identity_and_too_small_ramp_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            for identity, ramp in [('UNKNOWN', .05), (Source.identity, .0001)]:
                gate=Source(); gate.identity=identity
                worker=DualGateWorker(DualGateParams(vds_ramp=ramp), SaveRoot(base=folder), Connections(), g3=gate, daq=Guard())
                errors=[]; worker.error.connect(errors.append); worker.run()
                self.assertTrue(errors); self.assertFalse(gate.commands)
