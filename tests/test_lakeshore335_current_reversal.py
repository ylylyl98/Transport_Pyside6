import unittest
from types import SimpleNamespace
from PySide6.QtCore import QCoreApplication
from app.devices.lakeshore335_adapter import (LakeShore335Adapter, LakeShore335Error,
                                              MockLakeShore335Adapter)
from controllers.lakeshore335_controller import _LakeShoreWorker, LakeShore335Controller
from app.sample_heating_interlock import SampleHeatingInterlock

class Resource:
    def __init__(self):
        self.values = {'*IDN?':'LSCI,MODEL335,SN,2.2',
                       'INTYPE? A':'2,0,4,1,1', 'INTYPE? B':'3,1,8,1,1',
                       'KRDG? A':'3.1', 'KRDG? B':'3.8',
                       'RDGST? A':'0', 'RDGST? B':'0'}
        self.writes=[]; self.ignore=False; self.mutate=None
    def query(self, command): return self.values[command]
    def write(self, command):
        self.writes.append(command)
        if self.ignore: return
        kind, args = command.split(' ', 1)
        ch, value = args.split(',',1)
        if kind == 'INTYPE':
            self.values[f'INTYPE? {ch}'] = self.mutate or value
    def close(self): pass

class CurrentReversalTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls): cls.app=QCoreApplication.instance() or QCoreApplication([])
    def adapter(self):
        resource=Resource(); adapter=LakeShore335Adapter(visa_resource=resource); adapter.connect()
        return adapter, resource
    def test_both_channels_and_off_keeps_temperature_reads(self):
        adapter,resource=self.adapter()
        initial=adapter.read_input_configurations()['inputs']
        self.assertEqual(initial['A']['sensor_type'],2)
        self.assertEqual(initial['B']['range'],8)
        state=adapter.set_current_reversal('A',False)
        self.assertEqual(resource.writes,['INTYPE A,2,0,4,0,1'])
        self.assertFalse(state['current_reversal'])
        snap=adapter.read_snapshot('B','A')
        self.assertEqual((snap.sample_temperature_k,snap.reservoir_temperature_k),(3.8,3.1))
        self.assertEqual(adapter.read_input_configuration('B'),initial['B'])
    def test_unsupported_inputs_and_invalid_values_never_write(self):
        for response in ('0,0,0,0,1','1,0,1,0,1','4,0,0,1,1','3,1,9,1,1','bad'):
            with self.subTest(response=response):
                adapter,resource=self.adapter(); resource.values['INTYPE? A']=response
                with self.assertRaises(LakeShore335Error): adapter.set_current_reversal('A',False)
                self.assertEqual(resource.writes,[])
        adapter,resource=self.adapter()
        for enabled in (0,1,'off',None):
            with self.assertRaises(LakeShore335Error): adapter.set_current_reversal('A',enabled)
        self.assertEqual(resource.writes,[])
    def test_readback_mismatch_fails_and_autorange_may_change_current_range(self):
        adapter,resource=self.adapter(); resource.ignore=True
        with self.assertRaises(LakeShore335Error): adapter.set_current_reversal('A',False)
        adapter,resource=self.adapter(); resource.mutate='3,1,6,0,1'
        result=adapter.set_current_reversal('B',False)
        self.assertEqual(result['range'],6)
        adapter,resource=self.adapter(); resource.mutate='2,0,3,0,1'
        with self.assertRaises(LakeShore335Error): adapter.set_current_reversal('A',False)
    def test_mock_read_write_and_interlock_blocks_reserved(self):
        adapter=MockLakeShore335Adapter(sample_temperature_k=4.,reservoir_temperature_k=3.)
        interlock=SampleHeatingInterlock(); worker=_LakeShoreWorker(interlock); worker.adapter=adapter
        results=[]; worker.control_result.connect(results.append)
        worker.set_current_reversal({'channel':'A','enabled':False})
        self.assertTrue(next(r for r in results if r['operation']=='set_current_reversal')['ok'])
        self.assertFalse(adapter.read_input_configuration('A')['current_reversal'])
        interlock.reserve_magnet(True)
        before=list(adapter.control_commands); results.clear()
        worker.set_current_reversal({'channel':'B','enabled':False})
        self.assertFalse(results[-1]['ok']); self.assertEqual(adapter.control_commands,before)
    def test_mode_change_emits_new_temperature_after_result_without_cross_mode_slope(self):
        adapter,resource=self.adapter()
        adapter.read_snapshot('B','A')
        resource.values['KRDG? B']='4.2'
        worker=_LakeShoreWorker(SampleHeatingInterlock()); worker.adapter=adapter
        worker.refresh_control_configuration=lambda: None
        events=[]
        worker.control_result.connect(lambda result: events.append(('result',result)))
        worker.snapshot_updated.connect(lambda snapshot: events.append(('snapshot',snapshot)))
        worker.set_current_reversal({'channel':'A','enabled':False})
        self.assertEqual([kind for kind,_ in events], ['result','snapshot'])
        self.assertTrue(events[0][1]['ok'])
        self.assertEqual(events[1][1].sample_temperature_k,4.2)
        self.assertIsNone(events[1][1].sample_slope_k_per_min)
        self.assertIsNone(events[1][1].reservoir_slope_k_per_min)

    def test_failed_readback_also_resets_temperature_slope(self):
        adapter,resource=self.adapter()
        adapter.read_snapshot('B','A')
        resource.ignore=True
        with self.assertRaises(LakeShore335Error): adapter.set_current_reversal('A',False)
        resource.values['KRDG? B']='4.2'
        self.assertIsNone(adapter.read_snapshot('B','A').sample_slope_k_per_min)

    def test_input_reservation_blocks_racing_magnet(self):
        interlock=SampleHeatingInterlock(); interlock.begin_input_configuration()
        try:
            with self.assertRaises(RuntimeError): interlock.begin_magnet()
        finally: interlock.end_sample_control()
    def test_input_config_allows_static_nonzero_and_rejects_other_commands(self):
        interlock=SampleHeatingInterlock()
        interlock.note_magnet(SimpleNamespace(field_t=3.0, heater_on=True))
        interlock.begin_input_configuration()
        try:
            with self.assertRaises(RuntimeError): interlock.begin_input_configuration()
            with self.assertRaises(RuntimeError): interlock.begin_magnet()
        finally: interlock.end_sample_control()
        interlock.reserve_magnet(True)
        with self.assertRaises(RuntimeError): interlock.begin_input_configuration()

    def test_queued_result_clears_pending_and_updates_readback(self):
        import time
        controller=LakeShore335Controller()
        controller._worker.adapter=MockLakeShore335Adapter(sample_temperature_k=4.,reservoir_temperature_k=3.)
        changes=[]; results=[]
        controller.input_configuration_pending_changed.connect(changes.append)
        controller.control_result.connect(results.append)
        try:
            self.assertTrue(controller.set_current_reversal('B',False))
            self.assertTrue(controller.input_configuration_pending)
            end=time.monotonic()+3.
            while controller.input_configuration_pending and time.monotonic()<end:
                self.app.processEvents()
                time.sleep(.005)
            self.assertFalse(controller.input_configuration_pending)
            self.assertEqual(changes,[True,False])
            self.assertTrue(any(r.get('operation')=='set_current_reversal' and r.get('ok') for r in results))
            self.assertFalse(controller.input_configurations['B']['current_reversal'])
            self.assertIsNotNone(controller.input_configurations_monotonic_s)
        finally: controller.shutdown()

    def test_input_change_fences_old_temperature_even_if_read_arrives_late(self):
        from PySide6.QtCore import QSignalBlocker
        from unittest.mock import patch
        controller=LakeShore335Controller()
        try:
            old=SimpleNamespace(monotonic_s=100.,sample_temperature_k=3.8)
            controller._cache_valid_snapshot(old)
            self.assertIs(controller.telemetry_cache.get(),old)
            blocker=QSignalBlocker(controller)
            self.assertTrue(controller.set_current_reversal('A',False))
            del blocker
            self.assertIsNone(controller.telemetry_cache.get())
            controller._cache_valid_snapshot(SimpleNamespace(monotonic_s=199.))
            self.assertIsNone(controller.telemetry_cache.get())
            with patch('controllers.lakeshore335_controller.time.monotonic', return_value=200.):
                controller._record_input_worker_result({'operation':'set_current_reversal','ok':False})
            controller._cache_valid_snapshot(SimpleNamespace(monotonic_s=199.5))
            controller._cache_valid_snapshot(SimpleNamespace(monotonic_s=float('nan')))
            self.assertIsNone(controller.telemetry_cache.get())
            fresh=SimpleNamespace(monotonic_s=201.,sample_temperature_k=3.9)
            controller._cache_valid_snapshot(fresh)
            self.assertIs(controller.telemetry_cache.get(),fresh)
            self.assertTrue(controller.input_configuration_pending)
            controller._on_control_result({'operation':'set_current_reversal','ok':False})
            self.assertFalse(controller.input_configuration_pending)
        finally: controller.shutdown()

    def test_controller_pending_covers_queue_and_completion(self):
        controller=LakeShore335Controller()
        try:
            self.assertTrue(controller.set_current_reversal('A',False))
            self.assertTrue(controller.input_configuration_pending)
            self.assertFalse(controller.set_current_reversal('B',True))
            controller._on_control_result({'operation':'set_current_reversal','ok':False})
            self.assertFalse(controller.input_configuration_pending)
            controller._on_control_result({'operation':'read_input_configurations','ok':True,
                'inputs':{'A':{'supported':True,'current_reversal':False}}, 'monotonic_s':12.5})
            self.assertEqual(controller.input_configurations_monotonic_s,12.5)
            state=controller.input_configurations
            state['A']['current_reversal']=True
            self.assertFalse(controller.input_configurations['A']['current_reversal'])
            controller._on_control_result({'operation':'read_input_configurations','ok':False,'error':'invalid'})
            self.assertIsNone(controller.input_configurations)
            self.assertIsNone(controller.input_configurations_monotonic_s)
        finally: controller.shutdown()

if __name__=='__main__': unittest.main()
