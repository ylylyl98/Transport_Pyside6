import unittest
from dataclasses import replace
from app.signal_chain import SignalChainSnapshot, signal_chain_filename_parts
from app.run_output import compose_output_stem


class ACFilenameTests(unittest.TestCase):
    def test_new_estimate_replaces_stale_estimate_in_user_stem(self):
        name = compose_output_stem('sample','map','test_VacEst4.4mV',
                                  ['ACout20mV','VacEst2mV'],'run')
        self.assertNotIn('VacEst4.4mV',name)
        self.assertEqual(name.count('VacEst'),1)

    def test_user_ratio_is_estimate_and_does_not_change_current_calibration(self):
        from app.signal_chain import signal_chain_metadata
        original = SignalChainSnapshot(17, .2, 1e-8, lockin_settings={
            'source':'instrument', 'values':{'sine_out_v':.02}})
        self.assertFalse(any(p.startswith('VacEst') for p in signal_chain_filename_parts(original)))
        estimated = replace(original, ac_voltage_ratio=.22)
        self.assertIn('ACout20mV', signal_chain_filename_parts(estimated))
        self.assertIn('VacEst4.4mV', signal_chain_filename_parts(estimated))
        metadata = signal_chain_metadata(estimated)
        self.assertAlmostEqual(metadata['sample_ac_voltage_estimate_v'], .0044)
        self.assertEqual(metadata['ac_voltage_ratio_source'], 'user estimate')
        self.assertEqual(estimated.lockin_scale, original.lockin_scale)
        self.assertEqual(estimated.preamp_gain_v_per_a, original.preamp_gain_v_per_a)
        for ratio in (None, 0, -1, float('nan'), float('inf')):
            invalid = replace(original, ac_voltage_ratio=ratio)
            self.assertFalse(any(p.startswith('VacEst') for p in signal_chain_filename_parts(invalid)))

    def test_ratio_ui_persists_clears_and_updates_filename_preview(self):
        from unittest.mock import patch
        from app.ui.main_window import MainWindow
        from app.settings import get_app_settings
        from app.ui.dock import ConnDock
        settings = get_app_settings()
        settings.remove('signal_chain/ac_voltage_ratio')
        with patch('app.ui.dock.ConnDock._start_scan'):
            window = MainWindow()
        try:
            dock = window.conn_dock
            self.assertEqual(dock.sp_ac_ratio.value(), 0)
            window.lockin_panel.sp_sine_out.setValue(.02)
            dock.sp_ac_ratio.setValue(.22)
            self.assertIn('4.4mV', dock.lbl_ac_estimate.text())
            self.assertIn('VacEst4.4mV', window.tab_cosweep._planned_output.stem)
            self.assertEqual(window.signal_chain_snapshot().ac_voltage_ratio, .22)
            with patch('app.ui.dock.ConnDock._start_scan'):
                reopened = ConnDock()
            try:
                reopened.load_settings()  # MainWindow performs this on startup.
                self.assertEqual(reopened.sp_ac_ratio.value(), .22)
            finally:
                reopened.close()
            dock.sp_ac_ratio.setValue(0)
            self.assertNotIn('VacEst', window.tab_cosweep._planned_output.stem)
            self.assertIsNone(window.signal_chain_snapshot().ac_voltage_ratio)
        finally:
            window.close()
            settings.remove('signal_chain/ac_voltage_ratio')

    def test_contact_follows_description_before_scan_parameters(self):
        name = compose_output_stem('YZ324', 'map', '1.67K0T',
            ['Vds0.8to1.3V', '17Hz', 'ACout20mV', 'ACMoTe2E2'], 'run')
        self.assertEqual(name, 'YZ324_1.67K0T_ACMoTe2E2_Vds0.8to1.3V_17Hz_ACout20mV_run')

    def test_existing_contact_keeps_its_position_in_description(self):
        name = compose_output_stem('YZ324', 'map', '1.67K0T_ACMoTe2E2_DCMoTe2E1',
            ['Vds0.8to1.3V', 'ACMoTe2E2', 'ACset4mV'], 'run')
        self.assertEqual(name, 'YZ324_1.67K0T_ACMoTe2E2_DCMoTe2E1_Vds0.8to1.3V_ACset4mV_run')

    def test_contact_with_separator_is_not_duplicated_or_partially_matched(self):
        for label in ('test_ACtop_gate_DC1', 'test_ACtop_gate2'):
            name = compose_output_stem('sample','map',label,['Vds0V','ACtop_gate'],'run')
            if label.endswith('DC1'):
                self.assertEqual(name, 'sample_test_ACtop_gate_DC1_Vds0V_run')
            else:
                self.assertEqual(name, 'sample_test_ACtop_gate2_ACtop_gate_Vds0V_run')

    def test_output_amplitude_is_separate_from_input_sensitivity(self):
        snapshot = SignalChainSnapshot(17, .2, 1e-8, lockin_settings={
            'source': 'instrument', 'values': {'sine_out_v': .02, 'frequency_hz': 137}})
        parts = signal_chain_filename_parts(snapshot)
        self.assertIn('ACout20mV', parts)
        self.assertIn('LIA200mV', parts)
        self.assertIn('137Hz', parts)
        self.assertNotIn('17Hz', parts)

    def test_manual_value_is_explicit_and_unknown_is_not_invented(self):
        snapshot = SignalChainSnapshot(lockin_settings={'source':'saved/manual','values':{'sine_out_v':.004}})
        self.assertIn('ACset4mV', signal_chain_filename_parts(snapshot))
        for value in (None, float('nan'), -1):
            snapshot = replace(snapshot, lockin_settings={'source':'instrument','values':{'sine_out_v':value}})
            self.assertFalse(any(p.startswith('ACout') for p in signal_chain_filename_parts(snapshot)))

    def test_exact_duplicate_tags_are_not_repeated(self):
        name = compose_output_stem('sample','map','test_ACMoTe2E2_ACout20mV',
                                   ['ACMoTe2E2','ACout20mV'], 'run')
        self.assertEqual(name.count('ACMoTe2E2'),1)
        self.assertEqual(name.count('ACout20mV'),1)

    def test_contact_metadata_and_verified_snapshot_lifetime(self):
        from app.ui.tabs.base_tab import BaseMeasurementTab, run_filename_snapshot
        from types import SimpleNamespace
        manual = SignalChainSnapshot(ac_contact='MoTe2E2')
        verified = replace(manual, lockin_settings={'source':'instrument','values':{'sine_out_v':.02}})
        self.assertIn('ACMoTe2E2',signal_chain_filename_parts(verified))
        stub = SimpleNamespace(get_signal_chain=lambda:manual)
        @run_filename_snapshot
        def start(obj):
            obj._filename_signal_chain = verified
            self.assertIs(BaseMeasurementTab.filename_signal_chain(obj), verified)
            raise RuntimeError('cancel start')
        with self.assertRaises(RuntimeError): start(stub)
        self.assertIs(BaseMeasurementTab.filename_signal_chain(stub),manual)

    def test_start_names_file_from_verified_amplitude_not_pending_panel_value(self):
        from unittest.mock import patch
        from types import SimpleNamespace
        from app.ui.tabs.cosweep_tab import CoSweepTab
        from app.device_manager import DeviceManager
        from app.models import Connections, SaveRoot
        manual = SignalChainSnapshot(17,.2,1e-8,lockin_settings={'source':'saved/manual','values':{'sine_out_v':.004}})
        verified = replace(manual,lockin_settings={'source':'instrument','values':{'sine_out_v':.02,'frequency_hz':137}})
        manager=DeviceManager(Connections())
        for name in ('g1','g2','g3'):
            manager.sessions[name]=SimpleNamespace(identity='MODEL 2400',cached_source_voltage_range_v=20)
        tab=CoSweepTab(SaveRoot(),manager.connections,manager,get_global_rates_callable=lambda:(1e8,50),get_signal_chain_callable=lambda:manual)
        try:
            with patch.object(tab,'capture_run_signal_chain',return_value=verified), \
                 patch.object(tab,'validate_output_ready',return_value=True), \
                 patch.object(tab,'_validate_required_sessions',return_value=True), \
                 patch.object(tab,'claim_run_devices',return_value=(True,[])), \
                 patch.object(tab,'begin_run_logging'), patch.object(tab,'end_run_logging'), \
                 patch('app.ui.tabs.cosweep_tab.CoSweepWorker',side_effect=RuntimeError('stop before worker')):
                tab.start_run()
            self.assertIn('ACout20mV',tab.p.output_csv_path)
            self.assertIn('137Hz',tab.p.output_csv_path)
            self.assertNotIn('ACset4mV',tab.p.output_csv_path)
            self.assertIsNone(tab._filename_signal_chain)
        finally:
            tab.close()
