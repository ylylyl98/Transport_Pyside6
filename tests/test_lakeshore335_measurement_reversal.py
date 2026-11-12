import time
import unittest
from types import SimpleNamespace as NS
from unittest.mock import patch
from PySide6.QtCore import QCoreApplication
from app.devices.lakeshore335_adapter import MockLakeShore335Adapter
from app.sample_heating_interlock import SampleHeatingInterlock
from controllers.lakeshore335_controller import _LakeShoreWorker, LakeShore335Controller
from utils.config import cfg

class MeasurementReversalTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls): cls.app=QCoreApplication.instance() or QCoreApplication([])
    def setUp(self):
        self.gate=SampleHeatingInterlock()
        self.adapter=MockLakeShore335Adapter(sample_temperature_k=3.8,reservoir_temperature_k=3.7)
        self.worker=_LakeShoreWorker(self.gate); self.worker.adapter=self.adapter
        self.results=[]; self.snapshots=[]
        self.worker.control_result.connect(self.results.append)
        self.worker.snapshot_updated.connect(self.snapshots.append)
        self.mapping=patch.multiple(cfg.lakeshore335, sample_channel='B',reservoir_channel='A',verified_channel_mapping=True)
        self.mapping.start(); self.addCleanup(self.mapping.stop)
    def reserve(self):
        self.gate.reserve_magnet(True,owner='bfield-transport')
        self.gate.note_magnet(NS(monotonic_s=time.monotonic(),sweep_state='paused',
                                 status=NS(sweep_active=False,faulted=False)))
        self.gate.begin_measurement_input_session('bfield-transport')
    def change(self, enabled=False, owner='bfield-transport'):
        self.worker.set_measurement_sample_reversal({'owner':owner,'enabled':enabled,'request_id':'id-1'})
        return next(r for r in self.results if r.get('operation')=='measurement_current_reversal')
    def test_owner_session_switches_b_only_and_keeps_a_on(self):
        self.reserve(); before=self.adapter.read_input_configuration('A')
        result=self.change(False)
        self.assertTrue(result['ok'],result)
        self.assertEqual(result['request_id'],'id-1')
        self.assertFalse(result['inputs']['B']['current_reversal'])
        self.assertEqual(result['inputs']['A'],before)
        self.assertTrue(all(command.startswith('INTYPE B,') for command in self.adapter.control_commands))
        self.assertGreaterEqual(len(self.snapshots),2)
        self.assertGreater(self.snapshots[-1].monotonic_s,result['monotonic_s'])
        self.assertEqual(self.snapshots[-1].reservoir_temperature_k,3.7)
        self.gate.end_measurement_input_session('bfield-transport')
        self.gate.reserve_magnet(False,owner='bfield-transport')
    def test_wrong_owner_stale_or_moving_blocks_b_write(self):
        for mode in ('owner','stale','moving','fault'):
            with self.subTest(mode=mode):
                self.setUp(); self.reserve()
                if mode=='stale': self.gate.note_magnet(NS(monotonic_s=time.monotonic()-60,sweep_state='paused',status=NS(sweep_active=False,faulted=False)))
                if mode=='moving': self.gate.note_magnet(NS(monotonic_s=time.monotonic(),sweep_state='sweeping',status=NS(sweep_active=True,faulted=False)))
                if mode=='fault': self.gate.note_magnet(NS(monotonic_s=time.monotonic(),sweep_state='paused',status=NS(sweep_active=False,faulted=True)))
                result=self.change(owner='wrong' if mode=='owner' else 'bfield-transport')
                self.assertFalse(result['ok']); self.assertEqual(self.adapter.control_commands,[])
                self.gate.end_measurement_input_session('bfield-transport')
    def test_mapping_or_a_off_or_b_nondrt_rejected(self):
        for mode in ('mapping','a_off','b_diode'):
            with self.subTest(mode=mode):
                self.setUp(); self.reserve()
                if mode=='mapping': cfg.lakeshore335.sample_channel='A'
                if mode=='a_off': self.adapter._mock_control['INTYPE? A']='3,1,0,0,1'
                if mode=='b_diode': self.adapter._mock_control['INTYPE? B']='1,0,0,0,1'
                result=self.change()
                self.assertFalse(result['ok']); self.assertEqual(self.adapter.control_commands,[])
                self.gate.end_measurement_input_session('bfield-transport')
    def test_session_blocks_manual_control_and_unsafe_release(self):
        self.reserve()
        with self.assertRaises(RuntimeError): self.gate.begin_input_configuration()
        with self.assertRaises(RuntimeError): self.gate.reserve_magnet(False,owner='bfield-transport')
        with self.assertRaises(RuntimeError): self.gate.end_measurement_input_session('wrong')
        self.gate.end_measurement_input_session('bfield-transport')
    def test_front_panel_change_to_a_is_detected(self):
        self.reserve(); original=self.adapter.set_current_reversal
        def change_b(channel, enabled):
            result=original(channel,enabled)
            self.adapter._mock_control['INTYPE? A']='3,1,0,0,1'
            return result
        self.adapter.set_current_reversal=change_b
        result=self.change()
        self.assertFalse(result['ok']); self.assertIn('Reservoir A',result['error'])
        self.assertTrue(all(s.startswith('INTYPE B,') for s in self.adapter.control_commands))
        self.gate.end_measurement_input_session('bfield-transport')
    def test_async_session_drains_switches_and_requires_original_restore(self):
        controller=LakeShore335Controller(interlock=self.gate)
        try:
            controller._worker.adapter=self.adapter
            self.gate.reserve_magnet(True,owner='bfield-transport')
            self.gate.note_magnet(NS(monotonic_s=time.monotonic(),sweep_state='paused',
                                     status=NS(sweep_active=False,faulted=False)))
            inputs=self.adapter.read_input_configurations()['inputs']
            controller._on_control_result({'operation':'read_input_configurations','ok':True,'inputs':inputs,'monotonic_s':time.monotonic()})
            self.assertTrue(controller.begin_measurement_input_session('bfield-transport')['B']['current_reversal'])
            results=[]; controller.control_result.connect(results.append)
            manual_pending_events=[]
            controller.input_configuration_pending_changed.connect(manual_pending_events.append)
            previous=NS(monotonic_s=time.monotonic(), sample_temperature_k=3.8,
                        reservoir_temperature_k=3.7)
            controller.telemetry_cache.publish(previous)
            self.assertFalse(controller.set_current_reversal('A',False))
            self.assertEqual(manual_pending_events, [])
            self.assertIs(controller.telemetry_cache.get(), previous)
            def switch(enabled, request_id):
                self.assertTrue(controller.set_measurement_sample_reversal('bfield-transport',enabled,request_id))
                self.assertTrue(controller.input_configuration_pending)
                self.assertIsNotNone(controller.telemetry_cache.get())
                with self.assertRaises(RuntimeError): controller.end_measurement_input_session('bfield-transport')
                end=time.monotonic()+3.
                while controller.input_configuration_pending and time.monotonic()<end:
                    self.app.processEvents(); time.sleep(.005)
                self.assertFalse(controller.input_configuration_pending)
                result=next(r for r in results if r.get('request_id')==request_id)
                self.assertTrue(result['ok'],result)
            switch(False,'off-1')
            with self.assertRaisesRegex(RuntimeError,'not confirmed restored'):
                controller.end_measurement_input_session('bfield-transport')
            with self.assertRaises(RuntimeError): self.gate.reserve_magnet(False,owner='bfield-transport')
            switch(True,'restore-2')
            self.assertEqual(manual_pending_events, [])
            self.assertTrue(controller.end_measurement_input_session('bfield-transport'))
            self.gate.reserve_magnet(False,owner='bfield-transport')
        finally: controller.shutdown()

    def test_controller_session_requires_fresh_owner_bound_state(self):
        controller=LakeShore335Controller(interlock=self.gate)
        try:
            controller._worker.adapter=self.adapter
            self.gate.reserve_magnet(True,owner='bfield-transport')
            inputs=self.adapter.read_input_configurations()['inputs']
            controller._on_control_result({'operation':'read_input_configurations','ok':True,'inputs':inputs,'monotonic_s':time.monotonic()})
            with self.assertRaises(RuntimeError): controller.begin_measurement_input_session('wrong')
            captured=controller.begin_measurement_input_session('bfield-transport')
            self.assertTrue(captured['B']['current_reversal'])
            self.assertFalse(controller.set_measurement_sample_reversal('wrong',False,'req'))
            controller.end_measurement_input_session('bfield-transport')
        finally: controller.shutdown()

if __name__=='__main__': unittest.main()
