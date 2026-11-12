import unittest
from types import SimpleNamespace
from unittest.mock import Mock
from app.bias_control import bias2_target, validate_bias2, ramp_bias2
from app.models import DualGateParams


class Manager:
    def __init__(self):
        self.g4 = SimpleNamespace(identity="KEITHLEY INSTRUMENTS INC.,MODEL 2400,1,1", cached_source_voltage_range_v=20.0, voltage=0.0, set_voltage=Mock())
        self.readbacks = {"g4": {}}
    def get_session(self, name): return self.g4 if name == "g4" else None
    def is_connected(self, name): return True
    def needs_reconnect(self, name): return False
    def protection_is_applied(self, name): return True
    def is_voltage_source_mode(self, name): return True
    def applied_gate_voltage_limit(self, name): return 1.0


class Bias2Tests(unittest.TestCase):
    def test_legacy_photocurrent_condition_retains_bias2_ramp(self):
        from app.models import Connections, PhotocurrentParams, SaveRoot
        from app.workers.photocurrent import PhotocurrentWorker
        params = PhotocurrentParams(bias_conditions=[], bias2_mode="Fixed", bias2_ramp=.01)
        worker = PhotocurrentWorker(params, SaveRoot(), Connections())
        self.assertEqual(worker._active_conditions()[0].bias2_ramp, .01)

    def test_disabled_does_not_require_manager(self):
        p = DualGateParams()
        self.assertEqual(validate_bias2(p, [0], None), [])
        self.assertIsNone(bias2_target(p, 0.7))
        self.assertIsNone(ramp_bias2(None, p, 0.7))

    def test_follow_targets_and_limits(self):
        p = DualGateParams(vds_source="NI DAQ", bias2_mode="Follow", bias2_offset=.1)
        m = Manager()
        self.assertEqual(validate_bias2(p, [{"vds": .2}, {"vds": -.1}], m), [.30000000000000004, 0.0])
        with self.assertRaises(ValueError): validate_bias2(p, [1], m)
        with self.assertRaises(ValueError): validate_bias2(p, [0], m, False)

    def test_follow_rejects_bias1_unrepresentable_target(self):
        events = []
        g3 = FakeSource("g3", events)
        g3.cached_source_voltage_range_v = 20.0
        g4 = FakeSource("g4", events)
        g4.cached_source_voltage_range_v = 2.0
        manager = FullManager({"g3": g3, "g4": g4})
        params = DualGateParams(bias2_mode="Follow", bias2_offset=0)
        with self.assertRaisesRegex(ValueError, "Bias1 target"):
            validate_bias2(params, [.00005], manager)

    def test_resolution_and_protection_fail_closed(self):
        p = DualGateParams(bias2_mode="Fixed", bias2_voltage=.1001)
        m = Manager()
        with self.assertRaisesRegex(ValueError, "representable"): validate_bias2(p, [0], m)
        p.bias2_voltage = .1
        m.protection_is_applied = lambda name: False
        with self.assertRaisesRegex(ValueError, "protection"): validate_bias2(p, [0], m)
        m.protection_is_applied = lambda name: True
        m.g4.cached_source_voltage_range_v = None
        with self.assertRaisesRegex(ValueError, "resolution"): validate_bias2(p, [0], m)

class FakeSource:
    identity = "KEITHLEY,MODEL 2400,1,1"
    cached_source_voltage_range_v = 20.0
    def __init__(self, name, events):
        self.name, self.events, self.voltage = name, events, 0.0
    def get_voltage_setpoint(self): return self.voltage
    def refresh_source_voltage_range(self): return self.cached_source_voltage_range_v
    def set_voltage(self, value):
        self.voltage = value
        self.events.append((self.name, round(value, 6)))
    def ramp_voltage(self, value, *args): self.set_voltage(value)
    def acquire(self): return {"current": 1e-9 if self.name == "g3" else 2e-9}


class FakeDaq:
    def acquire(self): return {"ai0": 1., "ai1": 2., "ai2": 3.}


class FullManager(Manager):
    def __init__(self, sessions): self.sessions, self.readbacks = sessions, {"g4": {}}
    def get_session(self, name): return self.sessions.get(name)


class WorkerIntegrationTests(unittest.TestCase):
    def test_forward_reverse_and_line_follow_save_both_currents(self):
        import csv, tempfile
        from pathlib import Path
        from unittest.mock import patch
        from app.models import Connections, SaveRoot, LineSweepParams
        from app.workers.dual_gate import DualGateWorker
        from app.workers.line_sweep import LineSweepWorker
        cases = [(DualGateWorker, DualGateParams(vds_start=0, vds_stop=.1, vds_step=.1,
                    sweep_both_ways=True, delay=0, n_sample=1, bias2_mode="Follow", bias2_offset=.1)),
                 (LineSweepWorker, LineSweepParams(raw_vtg_start=0, raw_vtg_stop=0, raw_vds_active=True,
                    raw_vds_start=0, raw_vds_stop=.1, n_points=2, sweep_both_ways=True,
                    delay=0, n_sample=1, bias2_mode="Follow", bias2_offset=.1))]
        for worker_type, p in cases:
            with self.subTest(worker=worker_type.__name__), tempfile.TemporaryDirectory() as folder, \
                 patch('app.bias_control.time.sleep'), patch('app.utils.time.sleep'):
                events=[]; g1=FakeSource('g1',events); g2=FakeSource('g2',events)
                g3=FakeSource('g3',events); g4=FakeSource('g4',events)
                manager=FullManager(dict(g1=g1,g2=g2,g3=g3,g4=g4))
                p.output_csv_path=str(Path(folder)/'scan.csv')
                worker=worker_type(p,SaveRoot(),Connections(),g1=g1,g2=g2,g3=g3,g4=g4,daq=FakeDaq(),device_manager=manager)
                errors=[]; worker.error.connect(errors.append); worker.run()
                self.assertFalse(errors, errors)
                with open(p.output_csv_path, newline='') as f:
                    header, units, *rows=csv.reader(f)
                self.assertEqual(len(rows),4)
                self.assertEqual([float(r[header.index('Bias2')]) for r in rows],[.1,.2,.2,.1])
                self.assertTrue(all(float(r[header.index('Ibias2_Keithley')]) == 2e-9 for r in rows))
                self.assertTrue(all(float(r[header.index('Ids_Keithley')]) == 1e-9 for r in rows))
                self.assertEqual(g3.voltage,0); self.assertEqual(g4.voltage,0)
                active=[e for e in events if e[0] in ('g3','g4')]
                self.assertIn(('g4',.2),active)

    def test_map_bias1_none_does_not_touch_g3(self):
        import csv, tempfile
        from pathlib import Path
        from unittest.mock import patch
        from app.models import Connections, SaveRoot, CoParams
        from app.workers.cosweep import CoSweepWorker
        events=[]; g1=FakeSource('g1',events); g2=FakeSource('g2',events); g3=FakeSource('g3',events)
        p=CoParams(vds_source='None',vtg_start=0,vtg_stop=.1,vtg_step=.1,
                   vbg_start=0,vbg_stop=0,delay=0,n_sample=1)
        with tempfile.TemporaryDirectory() as folder, patch('app.utils.time.sleep'):
            p.output_csv_path=str(Path(folder)/'map.csv')
            worker=CoSweepWorker(p,SaveRoot(),Connections(),g1=g1,g2=g2,g3=g3,daq=FakeDaq())
            errors=[]; worker.error.connect(errors.append); worker.run()
            self.assertFalse(errors,errors)
            with open(p.output_csv_path,newline='') as f: header, units, *rows=csv.reader(f)
            self.assertTrue(rows)
            self.assertTrue(all(not row[header.index('Vds')] and not row[header.index('Bias2')] for row in rows))
            self.assertFalse([e for e in events if e[0]=='g3'])

class PairSafetyTests(unittest.TestCase):
    def test_pair_interleaves_bounded_steps_from_readbacks_for_both_source_types(self):
        from unittest.mock import patch
        from app.bias_control import ramp_pair
        class Daq:
            def __init__(self, events): self.voltage=.1; self.events=events; self.reads=0
            def adopt_measured_output_as_ramp_start(self, channel): self.reads+=1; return self.voltage
            def set_voltage(self, channel, value): self.voltage=value; self.events.append(('g3',round(value,6)))
        for use_daq in (False, True):
            with self.subTest(use_daq=use_daq), patch('app.bias_control.time.sleep'):
                events=[]; primary=FakeSource('g3',events); primary.voltage=.1
                secondary=FakeSource('g4',events); secondary.voltage=.2
                daq=Daq(events)
                p=DualGateParams(bias2_mode='Follow',bias2_offset=.1,bias2_ramp=.04)
                ramp_pair(None if use_daq else primary,daq,0,secondary,p,.3,.05)
                self.assertEqual([name for name, _ in events], ['g3','g4'] * 5)
                last={'g3':.1,'g4':.2}
                for name, value in events:
                    self.assertLessEqual(abs(value-last[name]), .050000001 if name=='g3' else .040000001)
                    last[name]=value
                self.assertAlmostEqual(last['g3'],.3); self.assertAlmostEqual(last['g4'],.4)
                if use_daq: self.assertEqual(daq.reads,1)

    def test_photocurrent_and_magnetic_apply_follow_and_save(self):
        import csv, tempfile, threading
        from pathlib import Path
        from unittest.mock import patch
        from app.models import Connections, SaveRoot, PhotocurrentParams, PhotocurrentBiasCondition, BFieldTransportCondition
        from app.workers.photocurrent import PhotocurrentWorker
        from app.engine.bfield_transport_controller import BFieldTransportController
        events=[]; g3=FakeSource('g3',events); g4=FakeSource('g4',events)
        g1=FakeSource('g1',events); g2=FakeSource('g2',events)
        manager=FullManager(dict(g1=g1,g2=g2,g3=g3,g4=g4))
        condition=PhotocurrentBiasCondition(vds=.1,settle_s=0,bias2_mode='Follow',bias2_offset=.1,bias2_ramp=.02)
        p=PhotocurrentParams(use_vds=True,vds_source='Keithley 2400',wl_start=500,wl_stop=500,delay=0,n_sample=1,bias_conditions=[condition])
        with tempfile.TemporaryDirectory() as folder, patch('app.utils.time.sleep'), patch('app.bias_control.time.sleep'):
            p.output_csv_path=str(Path(folder)/'pc.csv')
            worker=PhotocurrentWorker(p,SaveRoot(),Connections(),g1=g1,g2=g2,g3=g3,g4=g4,daq=FakeDaq(),device_manager=manager,mono=SimpleNamespace(set_wavelength=lambda value: None))
            errors=[]; worker.error.connect(errors.append); worker.run(); self.assertFalse(errors,errors)
            files=list(Path(folder).glob('*.csv')); self.assertEqual(len(files),1)
            with files[0].open(newline='') as f: header,units,*rows=csv.reader(f)
            self.assertEqual(float(rows[0][header.index('Bias2')]),.2)
            self.assertEqual(float(rows[0][header.index('Ibias2_Keithley')]),2e-9)
            events.clear()
            c=BFieldTransportCondition(vds=.1,bias2_mode='Follow',bias2_offset=.1,settle_s=0)
            controller=SimpleNamespace(device_manager=manager,_check_bias_progress=lambda: None,_io_cancel=threading.Event(),_active_ao_channels=set())
            BFieldTransportController._apply_biases(controller,c)
            self.assertAlmostEqual(g3.voltage,.1); self.assertAlmostEqual(g4.voltage,.2)
            self.assertIn(('g4',.2),events)

class FailureCleanupTests(unittest.TestCase):
    def test_failed_g4_preflight_does_not_touch_other_outputs(self):
        import tempfile
        from pathlib import Path
        from app.models import Connections, SaveRoot
        from app.workers.dual_gate import DualGateWorker
        events=[]; g1=FakeSource('g1',events); g2=FakeSource('g2',events); g3=FakeSource('g3',events)
        manager=FullManager(dict(g1=g1,g2=g2,g3=g3))
        p=DualGateParams(bias2_mode='Fixed', bias2_voltage=.1)
        with tempfile.TemporaryDirectory() as folder:
            p.output_csv_path=str(Path(folder)/'failed.csv')
            worker=DualGateWorker(p,SaveRoot(),Connections(),g1=g1,g2=g2,g3=g3,daq=FakeDaq(),device_manager=manager)
            errors=[]; worker.error.connect(errors.append); worker.run()
            self.assertTrue(errors); self.assertEqual(events,[])

    def test_magnetic_g4_cleanup_lifecycle_does_not_touch_next_disabled_run(self):
        import threading
        from unittest.mock import patch
        from app.models import BFieldTransportCondition
        from app.engine.bfield_transport_controller import BFieldTransportController
        events=[]; g1=FakeSource('g1',events); g2=FakeSource('g2',events); g4=FakeSource('g4',events)
        manager=FullManager(dict(g1=g1,g2=g2,g4=g4))
        controller=SimpleNamespace(device_manager=manager,_check_bias_progress=lambda: None,_io_cancel=threading.Event(),
            _active_ao_channels=set(),_claimed=['g1','g2','g4'],_cleanup_failures=[],_active_bias2_condition=None)
        enabled=BFieldTransportCondition(vds_source='None',bias2_mode='Fixed',bias2_voltage=.1,settle_s=0)
        disabled=BFieldTransportCondition(vds_source='None',bias2_mode='Disabled',settle_s=0)
        with patch('app.bias_control.time.sleep'), patch('app.utils.time.sleep'):
            BFieldTransportController._apply_biases(controller,enabled)
            BFieldTransportController._zero_biases(controller)
            self.assertIsNone(controller._active_bias2_condition)
            controller._claimed=['g1','g2']; events.clear()
            BFieldTransportController._apply_biases(controller,disabled)
            BFieldTransportController._zero_biases(controller)
            self.assertFalse([event for event in events if event[0]=='g4'])

class PairFailureTests(unittest.TestCase):
    def test_second_write_failure_keeps_both_cleanup_attempts_and_error_metadata(self):
        import json, tempfile
        from pathlib import Path
        from unittest.mock import patch
        from app.models import Connections, SaveRoot
        from app.workers.dual_gate import DualGateWorker
        class FailingSource(FakeSource):
            def set_voltage(self, value):
                self.voltage=value; self.events.append((self.name, round(value,6)))
                raise RuntimeError('simulated G4 write failure')
        events=[]; g1=FakeSource('g1',events); g2=FakeSource('g2',events)
        g3=FakeSource('g3',events); g4=FailingSource('g4',events)
        manager=FullManager(dict(g1=g1,g2=g2,g3=g3,g4=g4))
        p=DualGateParams(vds_start=.1,vds_stop=.1,bias2_mode='Follow',bias2_offset=.1,delay=0,n_sample=1)
        with tempfile.TemporaryDirectory() as folder, patch('app.bias_control.time.sleep'), patch('app.utils.time.sleep'):
            p.output_csv_path=str(Path(folder)/'scan.csv'); p.output_metadata_path=str(Path(folder)/'scan.json')
            worker=DualGateWorker(p,SaveRoot(),Connections(),g1=g1,g2=g2,g3=g3,g4=g4,daq=FakeDaq(),device_manager=manager)
            errors=[]; finished=[]; points=[]
            worker.error.connect(errors.append); worker.finished.connect(finished.append); worker.point_data.connect(points.append)
            worker.run()
            self.assertEqual(points,[]); self.assertEqual(finished,[])
            self.assertTrue(errors and 'cleanup failed' in errors[0].lower(), errors)
            self.assertEqual(g3.voltage,0.0)
            self.assertIn(('g4',0.0),events)
            self.assertFalse(Path(p.output_csv_path).exists())
            metadata=json.loads(Path(p.output_metadata_path).read_text())
            self.assertEqual(metadata['status'], 'error')
            self.assertFalse(metadata['safe_state']['ok'])
            self.assertTrue(any('Bias2 zero failed' in item for item in metadata['safe_state']['failures']))

    def test_stop_between_pair_writes_stops_without_sample_and_cleans_primary(self):
        import tempfile
        from pathlib import Path
        from unittest.mock import patch
        from app.models import Connections, SaveRoot
        from app.workers.dual_gate import DualGateWorker
        events=[]; g1=FakeSource('g1',events); g2=FakeSource('g2',events)
        g3=FakeSource('g3',events); g4=FakeSource('g4',events)
        manager=FullManager(dict(g1=g1,g2=g2,g3=g3,g4=g4))
        p=DualGateParams(vds_start=.1,vds_stop=.1,bias2_mode='Follow',bias2_offset=.1,delay=0,n_sample=1)
        with tempfile.TemporaryDirectory() as folder, patch('app.bias_control.time.sleep'), patch('app.utils.time.sleep'):
            p.output_csv_path=str(Path(folder)/'scan.csv')
            worker=DualGateWorker(p,SaveRoot(),Connections(),g1=g1,g2=g2,g3=g3,g4=g4,daq=FakeDaq(),device_manager=manager)
            original=g3.set_voltage
            def stop_on_write(value):
                original(value)
                if value: worker.request_stop()
            g3.set_voltage=stop_on_write
            stopped=[]; points=[]; worker.stopped.connect(stopped.append); worker.point_data.connect(points.append)
            worker.run()
            self.assertTrue(stopped); self.assertFalse(points)
            self.assertEqual(g3.voltage,0); self.assertEqual(g4.voltage,0)
            self.assertFalse([event for event in events if event[0]=='g4'])
