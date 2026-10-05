"""Independent lock-in range readouts through the real window signal wiring."""
import os
import unittest
from unittest.mock import patch

if os.environ.get('TRANSPORT_OFFLINE_VERIFICATION') != '1':
    raise unittest.SkipTest('Use verify_offline.py to isolate hardware and settings')

from PySide6 import QtCore, QtTest, QtWidgets
from app.ui.main_window import MainWindow
from test_lockin_control import make_session


class DualSensitivityUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance()
        with patch('app.ui.dock.ConnDock._start_scan'):
            cls.window = MainWindow()
        cls.app.processEvents()

    @classmethod
    def tearDownClass(cls):
        cls.window.close()
        cls.app.processEvents()

    def setUp(self):
        self.dock = self.window.conn_dock
        self.manager = self.window.device_manager
        self.panels = (self.window.lockin_panel, self.window.drive_lockin_panel)
        self.dock.drag_drive_settings.cbo_mode.setCurrentIndex(1)
        for name, address in (('lockin', 'GPIB0::8::INSTR'), ('lockin_drive', 'GPIB0::9::INSTR')):
            session = make_session()
            session._address = address
            self.manager.sessions[name] = session
            self.manager.states[name] = 'ok'
            self.manager._connected_addresses[name] = address
            self.manager.status_changed.emit(name, 'ok', 'SRS,SR850,123,1.0')
        self.app.processEvents()

    def tearDown(self):
        self.manager.release(['lockin', 'lockin_drive'])
        for name in ('lockin', 'lockin_drive'):
            self.manager.sessions[name] = None
            self.manager.states[name] = 'idle'
            self.manager.status_changed.emit(name, 'idle', '')
        self.dock.drag_drive_settings.cbo_mode.setCurrentIndex(0)

    def readout(self, name):
        # The workspace reparents this section into the Signal Chain dialog.
        field = self.dock.exp_signal_chain.findChild(QtWidgets.QLineEdit, name + 'SensitivityReadback')
        self.assertIsNotNone(field, 'Signal Chain needs a distinct readback for ' + name)
        return field

    def publish(self, drag=22, drive=23):
        for panel, index in zip(self.panels, (drag, drive)):
            panel._apply_data({'settings': {'sensitivity': index, 'input_config': 0}})

    def test_independent_panel_readbacks_and_unapplied_edits(self):
        self.publish()
        drag, drive = self.readout('lockin'), self.readout('lockin_drive')
        self.assertEqual(drag.text(), '50 mV')
        self.assertEqual(drive.text(), '100 mV')
        self.assertTrue(drag.isReadOnly() and drive.isReadOnly())
        self.assertIn('GPIB0::8::INSTR', self.dock.lbl_lkn_source.text())
        self.assertIn('GPIB0::9::INSTR', self.dock.lbl_drive_lkn_source.text())
        self.panels[1].cbo_sensitivity.setCurrentIndex(24)
        self.assertEqual(drive.text(), '100 mV')
        self.panels[1]._apply_data({'settings': {'sensitivity': 24}})
        self.assertEqual(drive.text(), '200 mV')
        self.assertEqual(drag.text(), '50 mV')
        self.assertEqual(self.dock.sp_lkn.value(), .05)

    def test_mode_switch_hides_drive_and_restores_ordinary_manual_control(self):
        self.publish()
        self.assertFalse(self.dock.drive_sensitivity_row.isHidden())
        self.assertTrue(self.dock.sp_lkn.isHidden())
        self.dock.drag_drive_settings.cbo_mode.setCurrentIndex(0)
        self.assertTrue(self.dock.drive_sensitivity_row.isHidden())
        self.assertFalse(self.dock.sp_lkn.isHidden())
        self.assertTrue(self.readout('lockin').isHidden())
        self.dock.drag_drive_settings.cbo_mode.setCurrentIndex(1)
        self.assertEqual(self.readout('lockin_drive').text(), '100 mV')
        self.assertFalse(self.dock.drive_sensitivity_row.isHidden())

    def test_failure_invalidates_only_affected_readback_and_discard_does_not_reverify(self):
        self.publish()
        self.panels[1]._on_worker_data({'control': {'ok': False, 'error': 'timeout'}}, 'timeout')
        self.assertEqual(self.readout('lockin_drive').text(), '--')
        self.assertIn('unverified', self.dock.lbl_drive_lkn_source.text().lower())
        self.assertEqual(self.readout('lockin').text(), '50 mV')
        self.panels[1]._discard_pending()
        self.assertEqual(self.readout('lockin_drive').text(), '--')
        self.panels[1]._apply_data({'settings': {'sensitivity': 24}})
        self.assertEqual(self.readout('lockin_drive').text(), '200 mV')

    def test_disconnect_and_reconnect_never_reuse_previous_drive_value(self):
        self.publish()
        self.manager.states['lockin_drive'] = 'idle'
        self.manager.status_changed.emit('lockin_drive', 'idle', '')
        self.assertEqual(self.readout('lockin_drive').text(), '--')
        self.assertIn('Disconnected', self.dock.lbl_drive_lkn_source.text())
        self.manager._connected_addresses['lockin_drive'] = 'GPIB0::10::INSTR'
        self.manager.states['lockin_drive'] = 'ok'
        self.manager.status_changed.emit('lockin_drive', 'ok', 'SRS,SR850,124,1.0')
        self.assertEqual(self.readout('lockin_drive').text(), '--')
        self.assertIn('Not read', self.dock.lbl_drive_lkn_source.text())
        self.assertIn('GPIB0::10::INSTR', self.dock.lbl_drive_lkn_source.text())

    def test_drive_refresh_button_routes_to_drive_worker_and_respects_run_lock(self):
        self.publish()
        drive = self.manager.get_session('lockin_drive')
        drive._my_instr.values['SENS'] = 24
        self.manager.mark_in_use(['lockin', 'lockin_drive'])
        self.assertFalse(self.dock.btn_refresh_drive_sensitivity.isEnabled())
        self.manager.release(['lockin', 'lockin_drive'])
        self.dock.btn_refresh_drive_sensitivity.click()
        timer = QtCore.QElapsedTimer()
        timer.start()
        while self.panels[1]._worker is not None and timer.elapsed() < 4000:
            QtTest.QTest.qWait(20)
        self.assertIsNone(self.panels[1]._worker)
        self.assertEqual(self.readout('lockin_drive').text(), '200 mV')
        self.assertEqual(self.readout('lockin').text(), '50 mV')
        self.assertFalse(self.manager.is_in_use('lockin_drive'))

    def test_current_input_is_not_presented_as_a_voltage_range(self):
        self.publish()
        self.panels[1]._apply_data({'model': 'SR850', 'settings': {'sensitivity': 23, 'input_config': 2}})
        self.assertEqual(self.readout('lockin_drive').text(), '--')
        self.assertIn('voltage input', self.dock.lbl_drive_lkn_source.text().lower())

    def test_connection_sensitivity_read_does_not_reverify_a_current_input_as_voltage(self):
        session = self.manager.get_session('lockin')
        session._my_instr.values['ISRC'] = 2
        self.assertFalse(self.dock.refresh_lockin_sensitivity_from_session())
        self.assertEqual(self.readout('lockin').text(), '--')
        self.assertIn('voltage input', self.dock.lbl_lkn_source.text().lower())

    def test_readback_precision_covers_smallest_range(self):
        self.publish(drag=0, drive=26)
        self.assertEqual(self.readout('lockin').text(), '2 nV')
        self.assertEqual(self.readout('lockin_drive').text(), '1 V')


if __name__ == '__main__':
    unittest.main()
