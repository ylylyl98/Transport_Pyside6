import csv
import io
import math
import json
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch
from types import SimpleNamespace

from PySide6 import QtWidgets
from app.models import Connections, SaveRoot, DualGateParams
from app.device_manager import DeviceManager
from app.ui.lockin_panel import LockinPanel
from app.signal_chain import SignalChainSnapshot
from app.experiment_metadata import capture_run_signal_chain


class Daq:
    ai_channel_indexes = [0, 1, 2, 3, 4]
    def __init__(self):
        self.reads = 0
        self.values = [1., -.5, .25, 2., -1.]
    def acquire(self):
        self.reads += 1
        return {f'ai{i}': v for i, v in enumerate(self.values)}
    def get_ai_value(self, index):
        return self.values[index]
    def ramp_voltage(self, *args, **kwargs):
        pass
    def get_ao_vs_gnd_value(self, index):
        return 0.


class Lockin:
    model = 'SR850'
    identity = 'SRS,SR850,0,1.0'
    def __init__(self, sensitivity, drive=False):
        self.values = dict(sensitivity=sensitivity, input_config=1 if drive else 0,
                           frequency_hz=137., ref_source=2 if drive else 0, harmonic=1)
        self.outputs = {'ch1': 'X', 'ch2': 'Y', 'x_offset': 0., 'y_offset': 0.,
                        'x_expand': 1, 'y_expand': 1}
    def read_settings(self):
        return dict(self.values)
    def read_analog_output_settings(self):
        return dict(self.outputs)


def dual_config():
    return {'enabled': True, 'series_resistance_ohm': 100000., 'drive_preamp_gain': 10.,
            'drive_preamp': 'SR551', 'verified': True,
            'drag_sensitivity_v': .05, 'drive_sensitivity_v': .1}


class DualConversionTests(unittest.TestCase):
    def test_dual_currents_are_available_to_history_and_png_export(self):
        from app.curve_history import read_csv_snapshot, SIGNAL_COLUMNS
        from app.png_export import export_run
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'bfield_transport.csv'
            path.write_text('B_measured_T,I_drag_X,I_drive_X,raw_drive_X\n0.1,5e-10,2e-8,2\n', encoding='utf-8')
            _headers, units, _rows, _warnings = read_csv_snapshot(path)
            self.assertEqual(units['I_drive_X'], 'A')
            self.assertEqual(units['raw_drive_X'], 'V')
            self.assertIn('I_drive_X', SIGNAL_COLUMNS)
            result = export_run({'csv_paths': [str(path)], 'measurement': 'bfield_transport'})
            self.assertFalse(result['warnings'], result['warnings'])
            self.assertEqual({Path(p).stem for p in result['outputs']},
                             {'bfield_transport_I_drag_X', 'bfield_transport_I_drive_X'})

    def test_analog_output_queries_use_each_models_routing_and_expansion_codes(self):
        from test_lockin_control import make_session
        for model, routing, expansion in [('SR850', 'FOUT', '1'), ('SR830', 'FPOP', '0')]:
            session = make_session(model)
            xy_code = '0' if model == 'SR850' else '1'
            responses = {f'{routing}? 1': xy_code, f'{routing}? 2': xy_code,
                         'OEXP? 1': f'0,{expansion}', 'OEXP? 2': f'0,{expansion}'}
            with patch.object(session, '_query', side_effect=responses.__getitem__):
                output = session.read_analog_output_settings()
            self.assertEqual((output['ch1'], output['ch2']), ('X', 'Y'))
            self.assertEqual((output['x_expand'], output['y_expand']), (1, 1))

    def test_drive_panel_operation_writes_only_the_drive_instrument(self):
        from test_lockin_control import make_session
        from app.ui.lockin_panel import LockinWorker
        drag, drive = make_session(), make_session()
        drive._address = 'GPIB1::9::INSTR'
        worker = LockinWorker(drive, 'apply', {'sensitivity': 23})
        results = []
        worker.data_ready.connect(lambda data, message: results.append(data))
        worker.run()
        self.assertEqual(drag._my_instr.calls, [])
        self.assertIn('SENS 23', drive._my_instr.calls)
        self.assertEqual(results[-1]['settings']['sensitivity'], 23)

    def sample(self, daq=None, dual=None):
        from app.drag_drive import acquire_current_sample
        return acquire_current_sample(daq or Daq(), 2, 1e7, 200.,
                                      {'drag_drive': dual if dual is not None else dual_config()})

    def test_two_independent_ranges_and_one_daq_read_per_average(self):
        daq = Daq()
        result = self.sample(daq)
        self.assertEqual(daq.reads, 2)
        self.assertAlmostEqual(result['I_drag_X'], 5e-10, delta=1e-20)
        self.assertAlmostEqual(result['I_drive_X'], 2e-8, delta=1e-20)
        self.assertAlmostEqual(result['I_drive_Y'], -1e-8, delta=1e-20)
        self.assertAlmostEqual(result['I_drive_R'], math.sqrt(5)*1e-8, delta=1e-20)
        self.assertEqual(result['Ids_DC'], 2.5e-8)
        self.assertEqual(result['raw_drive_X'], 2.)

    def test_changeable_resistor_changes_drive_only(self):
        config = dual_config()
        a = self.sample(dual=config)
        config['series_resistance_ohm'] = 200000.
        b = self.sample(dual=config)
        self.assertEqual(a['I_drag_X'], b['I_drag_X'])
        self.assertEqual(a['I_drive_X'], 2*b['I_drive_X'])

    def test_missing_ai4_is_an_error_in_dual_mode(self):
        daq = Daq()
        daq.values.pop()
        with self.assertRaisesRegex(RuntimeError, 'ai4'):
            self.sample(daq)

    def test_unverified_calibration_and_invalid_resistor_are_rejected(self):
        for change in ({'verified': False}, {'series_resistance_ohm': 0},
                       {'series_resistance_ohm': float('nan')}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.sample(dual={**dual_config(), **change})

    def test_single_mode_does_not_require_drive_channels(self):
        daq = Daq()
        daq.values = daq.values[:3]
        result = self.sample(daq, {'enabled': False})
        self.assertEqual(result['Ids_X'], 5e-10)
        self.assertNotIn('I_drive_X', result)


class DualPreflightTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def manager(self):
        manager = DeviceManager(Connections(drag_drive_enabled=True))
        manager.sessions.update(lockin=Lockin(22), lockin_drive=Lockin(23, True), daq=Daq())
        manager.states.update(lockin='ok', lockin_drive='ok', daq='ok')
        manager._connected_addresses.update(lockin=manager.connections.lockin,
                                            lockin_drive=manager.connections.drive_lockin,
                                            daq=manager.connections.daq_dev)
        return manager

    def test_daq_mode_change_requires_reconnect_and_registers_ai4(self):
        class FakeDaq(Daq):
            def __init__(self, **kwargs):
                super().__init__()
                self.ai_channel_indexes = kwargs['ai_channel_indexes']
            def connect(self):
                pass
            def close(self):
                pass
        manager = DeviceManager(Connections())
        with patch('app.device_manager.DaqCard', FakeDaq), \
                patch.object(manager, 'get_ao_items', return_value=['ao0']), \
                patch.object(manager, '_daq_detail', return_value='offline'):
            manager._connect_daq(manager.status_changed)
            self.assertEqual(manager.sessions['daq'].ai_channel_indexes, [0, 1, 2, 3])
            manager.connections.drag_drive_enabled = True
            self.assertTrue(manager.needs_reconnect('daq'))
            manager._connect_daq(manager.status_changed)
            self.assertEqual(manager.sessions['daq'].ai_channel_indexes, [0, 1, 2, 3, 4])
            self.assertFalse(manager.needs_reconnect('daq'))

    def test_duplicate_and_invalid_drive_addresses_fail_before_connect(self):
        for address in ('GPIB1::8::INSTR', 'invalid address'):
            manager = DeviceManager(Connections(drag_drive_enabled=True, drive_lockin=address))
            events = []
            manager.status_changed.connect(lambda *args: events.append(args))
            with patch('app.device_manager.SRSLockin') as constructor:
                manager._connect_lockin(manager.status_changed, 'lockin_drive')
            self.assertFalse(constructor.called)
            self.assertEqual(events[-1][1], 'err')

    def test_ordinary_capture_ignores_connected_drive(self):
        manager = self.manager()
        with patch.object(manager.sessions['lockin_drive'], 'read_settings', side_effect=AssertionError('Drive read')):
            result = capture_run_signal_chain(SignalChainSnapshot(drag_drive={'enabled': False}), manager)
        self.assertFalse(result.drag_drive['enabled'])

    def test_edited_address_cannot_close_a_session_owned_by_the_run(self):
        manager = self.manager()
        drive = manager.sessions['lockin_drive']
        manager.mark_in_use(['daq', 'lockin', 'lockin_drive'])
        manager.connections.drive_lockin = 'GPIB1::10::INSTR'
        with patch.object(drive, 'close', create=True) as close:
            manager.sync_addresses()
        self.assertFalse(close.called)
        self.assertIs(manager.sessions['lockin_drive'], drive)
        self.assertFalse(manager.connect_all())
        self.assertTrue(manager.needs_reconnect('lockin_drive'))
        manager.release(['daq', 'lockin', 'lockin_drive'])

    def test_transport_sample_and_csv_preserve_dual_currents(self):
        from app.drag_drive import DUAL_COLUMNS
        from app.engine.bfield_transport_controller import BFieldTransportController
        from app.engine.bfield_transport_sweep import TransportCsvWriter
        from app.models import BFieldTransportCondition
        controller = SimpleNamespace(
            _check_io_cancel=lambda: None, device_manager=self.manager(),
            _signal_chain={'drag_drive': dual_config()}, _calibration=(1e7, 200.),
            plan=SimpleNamespace(params=SimpleNamespace(averages=2)))
        controller._acquire_daq = BFieldTransportController._acquire_daq
        condition = BFieldTransportCondition(vds_source='NI DAQ')
        row = BFieldTransportController._read_sample(controller, {}, condition)
        self.assertAlmostEqual(row['I_drive_X'], 2e-8, delta=1e-20)
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'transport.csv'
            with TransportCsvWriter(str(path), condition, extra_columns=DUAL_COLUMNS) as writer:
                writer.write(row)
            with path.open(newline='') as handle:
                result = next(csv.DictReader(handle))
            self.assertEqual(float(result['I_drive_X']), row['I_drive_X'])

    def test_all_scan_workers_save_dual_values_aligned_with_header_units(self):
        from app.models import CoParams, LineSweepParams, PhotocurrentParams, PhotocurrentBiasCondition
        from app.workers.dual_gate import DualGateWorker
        from app.workers.cosweep import CoSweepWorker
        from app.workers.line_sweep import LineSweepWorker
        from app.workers.photocurrent import PhotocurrentWorker
        class Gate:
            voltage = 0.
            def set_voltage(self, value):
                self.voltage = value
            def ramp_voltage(self, value, *args):
                self.voltage = value
            def get_voltage_setpoint(self):
                return self.voltage
        cases = [
            (DualGateWorker, DualGateParams(vds_start=0., vds_stop=0., delay=0., n_sample=2)),
            (LineSweepWorker, LineSweepParams(n_points=2, raw_vtg_start=0., raw_vtg_stop=0.,
                raw_vbg_start=0., raw_vbg_stop=0., raw_vds_start=0., raw_vds_stop=0., delay=0., n_sample=2)),
            (CoSweepWorker, CoParams(vtg_start=0., vtg_stop=0., vbg_start=0., vbg_stop=0.,
                vds_start=0., vds_stop=0., delay=0., n_sample=2)),
            (PhotocurrentWorker, PhotocurrentParams(wl_start=500., wl_stop=500., use_vds=False,
                bias_conditions=[PhotocurrentBiasCondition(settle_s=0.)], delay=0., n_sample=2))]
        for worker_type, params in cases:
            with self.subTest(worker=worker_type.__name__), tempfile.TemporaryDirectory() as folder:
                params.vds_source = 'NI DAQ'
                params.output_csv_path = str(Path(folder) / 'scan.csv')
                params.output_metadata_path = str(Path(folder) / 'scan.json')
                daq = Daq()
                worker = worker_type(params, SaveRoot(), Connections(), daq=daq, g1=Gate(), g2=Gate(),
                    mono=SimpleNamespace(set_wavelength=lambda value: None),
                    amp_rate=1e7, lkn_rate=200., plot_choice='I_drive_X',
                    signal_chain={'drag_drive': dual_config()})
                errors, points, plots = [], [], []
                worker.error.connect(errors.append)
                worker.point_data.connect(points.append)
                worker.point.connect(lambda x, y: plots.append(y))
                worker.run()
                self.assertFalse(errors, errors)
                self.assertTrue(points)
                self.assertEqual(daq.reads, 2*len(points))
                self.assertTrue(all(abs(y-2e-8) < 1e-20 for y in plots))
                metadata = json.loads(Path(params.output_metadata_path).read_text(encoding='utf-8'))
                self.assertEqual(metadata['signal_chain']['drag_drive'], dual_config())
                for path in Path(folder).glob('*.csv'):
                    with path.open(newline='') as handle:
                        header, units, *rows = list(csv.reader(handle))
                    self.assertEqual(len(units), len(header))
                    self.assertEqual(units[header.index('raw_X')], 'V')
                    self.assertEqual(units[header.index('I_drive_X')], 'A')
                    self.assertTrue(rows)
                    for row in rows:
                        self.assertEqual(len(row), len(header))
                        self.assertAlmostEqual(float(row[header.index('I_drive_X')]), 2e-8, delta=1e-20)

    def test_addresses_and_panel_settings_are_separate(self):
        manager = self.manager()
        self.assertEqual(manager.connections.lockin, 'GPIB1::08::INSTR')
        self.assertEqual(manager.connections.drive_lockin, 'GPIB1::09::INSTR')
        drag = LockinPanel(manager)
        drive = LockinPanel(manager, session_name='lockin_drive')
        self.addCleanup(drag.close)
        self.addCleanup(drive.close)
        self.assertNotEqual(drag._settings_key('sensitivity'), drive._settings_key('sensitivity'))
        with patch.object(manager, 'mark_in_use', return_value=(False, ['test'])) as claim:
            drive.refresh_panel()
        self.assertEqual(claim.call_args.args[0], ['lockin_drive'])

    def test_capture_reads_both_and_freezes_rs_and_sensitivities(self):
        manager = self.manager()
        config = dual_config()
        config['verified'] = False
        chain = SignalChainSnapshot(drag_drive=config)
        frozen = capture_run_signal_chain(chain, manager)
        config['series_resistance_ohm'] = 200000.
        self.assertEqual(frozen.drag_drive['series_resistance_ohm'], 100000.)
        self.assertEqual(frozen.drag_drive['drag_sensitivity_v'], .05)
        self.assertEqual(frozen.drag_drive['drive_sensitivity_v'], .1)
        self.assertTrue(frozen.drag_drive['verified'])
        self.assertEqual(manager.current_in_use(), set())

    def test_unsafe_conversion_settings_block_dual_capture(self):
        for field, value in [('ch1', 'R'), ('x_offset', 1), ('y_expand', 10)]:
            manager = self.manager()
            manager.sessions['lockin_drive'].outputs[field] = value
            with self.subTest(field=field), self.assertRaises(RuntimeError):
                capture_run_signal_chain(SignalChainSnapshot(drag_drive=dual_config()), manager)
            self.assertEqual(manager.current_in_use(), set())

    def test_second_lockin_missing_or_busy_blocks_capture(self):
        for busy in (False, True):
            manager = self.manager()
            if busy:
                manager.mark_in_use(['lockin_drive'])
            else:
                manager.sessions['lockin_drive'] = None
            with self.subTest(busy=busy), self.assertRaises(RuntimeError):
                capture_run_signal_chain(SignalChainSnapshot(drag_drive=dual_config()), manager)

    def test_mode_is_explicit_defaults_off_and_freezes_during_run(self):
        from app.ui.drag_drive_settings import DragDriveSettings
        widget = DragDriveSettings()
        self.addCleanup(widget.close)
        self.addCleanup(widget.cbo_mode.setCurrentIndex, 0)
        self.assertFalse(widget.configuration()['enabled'])
        widget.cbo_mode.setCurrentIndex(1)
        self.assertEqual(widget.configuration()['series_resistance_ohm'], 100000.)
        manager = self.manager()
        widget.bind_manager(manager)
        manager.mark_in_use(['daq', 'lockin', 'lockin_drive'])
        self.assertFalse(widget.cbo_mode.isEnabled())
        self.assertFalse(widget.sp_rs.isEnabled())
        manager.release(['daq', 'lockin', 'lockin_drive'])
        self.assertTrue(widget.cbo_mode.isEnabled())

    def test_main_window_mode_updates_summary_and_all_measurement_plot_choices(self):
        from app.ui.main_window import MainWindow
        from app.signal_chain import signal_chain_filename_parts
        with patch('app.ui.dock.ConnDock._start_scan'):
            window = MainWindow()
        self.addCleanup(window.close)
        control = window.conn_dock.drag_drive_settings
        self.addCleanup(control.cbo_mode.setCurrentIndex, 0)
        control.cbo_mode.setCurrentIndex(1)
        self.assertTrue(window.connections.drag_drive_enabled)
        self.assertIn('Drag / Drive', window.instrument_workspace.summary.text())
        self.assertIn('DragDrive', signal_chain_filename_parts(window.signal_chain_snapshot()))
        bfield = window.tab_bfield_transport
        self.assertIn('I_drive_X', bfield.plot._y_axis_options)
        controller = window.bfield_transport_controller
        controller._results = [{'B_measured_T': .1, 'Ids_DC': 9e-6, 'I_drive_X': 2e-8}]
        bfield.plot._emit_plot_mode_changed('Single Plot')
        bfield.plot.y_axis_changed.emit('I_drive_X')
        self.assertEqual(list(controller._plot_line.get_ydata()), [2e-8])
        for tab in (window.tab_dual, window.tab_gate_scan, window.tab_bfield_gate_scan,
                    window.tab_cosweep, window.tab_photocurrent):
            self.assertGreaterEqual(tab.cbo_y.findText('I_drive_X'), 0)
        control.cbo_mode.setCurrentIndex(0)
        self.assertIn('Ordinary', window.instrument_workspace.summary.text())
        self.assertNotIn('I_drive_X', bfield.plot._y_axis_options)
        for tab in (window.tab_dual, window.tab_gate_scan, window.tab_bfield_gate_scan,
                    window.tab_cosweep, window.tab_photocurrent):
            self.assertEqual(tab.cbo_y.findText('I_drive_X'), -1)

    def test_dual_worker_csv_and_point_record_include_drive_from_same_sample(self):
        from app.workers.dual_gate import DualGateWorker
        worker = DualGateWorker(DualGateParams(vds_source='NI DAQ', delay=0., n_sample=2),
                                SaveRoot(), Connections(), daq=Daq(), amp_rate=1e7,
                                lkn_rate=200., signal_chain={'drag_drive': dual_config()})
        received = []
        worker.point_data.connect(received.append)
        output = io.StringIO()
        worker._run_vds_pass(output, csv.writer(output), [0.], 'forward', 0, 1)
        self.assertAlmostEqual(received[0]['I_drive_X'], 2e-8, delta=1e-20)
        self.assertEqual(worker.daq.reads, 2)
        self.assertIn('2e-08', output.getvalue())


if __name__ == '__main__':
    unittest.main()
