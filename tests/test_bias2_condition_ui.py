import tempfile
import unittest
from PySide6 import QtWidgets
from app.device_manager import DeviceManager
from app.models import Connections, SaveRoot
from app.settings import get_app_settings
from app.ui.tabs.photocurrent_tab import PhotocurrentTab
from app.ui.tabs.bfield_transport_tab import BFieldTransportTab


class Bias2ConditionUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.conns = Connections()
        self.manager = DeviceManager(self.conns)
        self.save = SaveRoot(base=self.folder.name, user='tester', device_id='sample')
        get_app_settings().remove('tabs/photocurrent')
        get_app_settings().remove('tabs/bfield_transport')

    def tearDown(self):
        self.folder.cleanup()

    def test_photocurrent_condition_duplicate_and_restore(self):
        tab = PhotocurrentTab(self.save, self.conns, self.manager)
        tab.tbl_bias_conditions.selectRow(0)
        tab.condition_bias2_editor.mode.setCurrentText('Follow')
        tab.condition_bias2_editor.offset.setValue(.3)
        tab.condition_bias2_editor.ramp.setValue(.01)
        tab._duplicate_selected_bias_conditions()
        self.assertEqual(tab._bias_conditions(strict=True)[1].bias2_offset, .3)
        tab._save_bias_conditions_settings()
        tab.tbl_bias_conditions.setRowCount(0)
        tab._load_bias_conditions_settings()
        self.assertTrue(tab.bias2_is_enabled())
        self.assertEqual(tab._bias_conditions(strict=True)[0].bias2_ramp, .01)
        tab.close()

    def test_bfield_condition_duplicate_reorder_and_restore(self):
        tab = BFieldTransportTab(self.save, self.conns, self.manager)
        tab.condition_table.selectRow(0)
        tab.condition_bias2_editor.mode.setCurrentText('Fixed')
        tab.condition_bias2_editor.voltage.setValue(.2)
        tab.condition_bias2_editor.ramp.setValue(.01)
        tab._duplicate_condition()
        tab._move_condition(-1)
        self.assertEqual(tab.collect_params().conditions[0].bias2_voltage, .2)
        tab._save_settings()
        tab.conditions = []
        tab._load_settings()
        self.assertEqual(tab.conditions[1].bias2_ramp, .01)
        tab.close()
