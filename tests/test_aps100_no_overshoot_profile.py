"""Experimental No Overshoot profile configuration and propagation."""
import unittest
import tempfile
from pathlib import Path
from PySide6.QtCore import QSettings
from PySide6 import QtCore, QtTest, QtWidgets
import tests.test_quick_forms as quick_fixtures
import tests.test_bfield_transport_ui as widget_fixtures
from app.models import BFieldTransportParams, SaveRoot
from app.engine.bfield_transport_sweep import BFieldTransportSafetyError, TransportSweepPlan
from app.quick import parameters
from app.quick.measurements import MeasurementService
import tests.test_hysteresis_transport as fixtures
from tests.test_quick_measurements import FakeManager


class NoOvershootProfileTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def test_default_choices_and_invalid_profiles(self):
        self.assertEqual(BFieldTransportParams().aps100_no_overshoot_profile, 'standard')
        options = parameters.descriptor('aps100_no_overshoot_profile', 'standard', 5)['options']
        self.assertEqual([o['value'] for o in options], ['standard', 'short_tail'])
        for profile, backend, mode, approach in (
            ('bad', '1000', 'stepped', 'no_overshoot'),
            ('short_tail', '2100', 'stepped', 'auto'),
            ('short_tail', '1000', 'continuous', 'no_overshoot'),
            ('short_tail', '1000', 'stepped', 'linear'),
            ('short_tail', '1000', 'stepped', 'linear_no_backward'),
            ('short_tail', '1000', 'stepped', 'oscillate'),
        ):
            with self.subTest(profile=profile, backend=backend, mode=mode, approach=approach):
                p = BFieldTransportParams(aps100_no_overshoot_profile=profile, magnet_backend=backend,
                                          measurement_mode=mode, field_approach=approach)
                with self.assertRaises(BFieldTransportSafetyError):
                    TransportSweepPlan.from_params(p)
        TransportSweepPlan.from_params(BFieldTransportParams(aps100_no_overshoot_profile='short_tail',
                                        magnet_backend='1000', measurement_mode='stepped', field_approach='auto'))

    def test_service_persists_choice(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = QSettings(str(Path(directory) / 'quick.ini'), QSettings.Format.IniFormat)
            def service():
                result = MeasurementService(FakeManager(), SaveRoot(base=directory), settings=settings,
                                            selected_backend_callable=lambda: '1000')
                result.mode = 5
                return result
            first = service()
            first.setValue('aps100_no_overshoot_profile', 'short_tail')
            self.assertEqual(first.collect_params().aps100_no_overshoot_profile, 'short_tail')
            settings.sync()
            second = service()
            self.assertEqual(second.collect_params().aps100_no_overshoot_profile, 'short_tail')
            first.deleteLater()
            second.deleteLater()


class NoOvershootPropagationTests(unittest.TestCase):
    setUpClass = fixtures.HysteresisAcceptanceTests.setUpClass
    setUp = fixtures.HysteresisAcceptanceTests.setUp
    reading = fixtures.HysteresisAcceptanceTests.reading
    move_result = fixtures.HysteresisAcceptanceTests.move_result

    def test_options_carry_profile(self):
        self.params.aps100_no_overshoot_profile = 'short_tail'
        self.controller.plan = TransportSweepPlan.from_params(self.params)
        self.controller._initial_step_direction = 1
        self.controller._step_direction = 1
        for initial in (False, True):
            options = self.controller._hysteresis_move_options(initial=initial)
            self.assertEqual(options['no_overshoot_profile'], 'short_tail')
            self.assertEqual(options['preparation'], initial)



class NoOvershootQuickUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        quick_fixtures.QuickFormTests.setUpClass()
        cls.app = quick_fixtures.QuickFormTests.app

    setUp = quick_fixtures.QuickFormTests.setUp
    tearDown = quick_fixtures.QuickFormTests.tearDown
    load = quick_fixtures.QuickFormTests.load
    find = quick_fixtures.QuickFormTests.find

    def test_selector_recovers_invalid_and_incompatible_choices(self):
        self.service.mode = 5
        self.service.setValue('measurement_mode', 'stepped')
        self.service.setValue('field_approach', 'no_overshoot')
        self.load()
        header = self.find('measurement_section_No Overshoot approach')
        header.forceActiveFocus()
        QtTest.QTest.keyClick(self.window, QtCore.Qt.Key_Space)
        self.app.processEvents()
        self.service.setValue('aps100_no_overshoot_profile', 'invalid')
        self.assertFalse(self.service.canStart)
        selector = self.find('field_aps100_no_overshoot_profile')
        self.assertTrue(QtCore.QMetaObject.invokeMethod(selector, 'activated', QtCore.Q_ARG(int, 1)))
        self.app.processEvents()
        self.assertEqual(self.service.collect_params().aps100_no_overshoot_profile, 'short_tail')
        self.assertTrue(self.service.canStart)
        self.assertNotIn(('', -1, 'aps100_no_overshoot_profile'), self.service._field_errors[5])
        self.service.setValue('field_approach', 'linear')
        self.app.processEvents()
        with self.assertRaises(BFieldTransportSafetyError):
            TransportSweepPlan.from_params(self.service.collect_params())
        selector = self.find('field_aps100_no_overshoot_profile')
        self.assertTrue(selector.isVisible())
        self.assertEqual(selector.property('displayText'), 'Shorter slow approach (experimental)')
        self.assertTrue(QtCore.QMetaObject.invokeMethod(selector, 'activated', QtCore.Q_ARG(int, 0)))
        self.app.processEvents()
        self.assertEqual(self.service.collect_params().aps100_no_overshoot_profile, 'standard')
        self.assertTrue(self.service.canStart)


class NoOvershootWidgetTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    setUp = widget_fixtures.BFieldTransportUiTests.setUp
    tearDown = widget_fixtures.BFieldTransportUiTests.tearDown

    def test_settings_recovery_and_lock(self):
        tab = self.tab
        selector = tab.cbo_no_overshoot_profile
        selector.setCurrentIndex(selector.findData('short_tail'))
        tab._save_settings()
        selector.setCurrentIndex(selector.findData('standard'))
        tab._load_settings()
        self.assertEqual(selector.currentData(), 'short_tail')
        tab.cbo_field_approach.setCurrentIndex(tab.cbo_field_approach.findData('linear'))
        self.assertTrue(tab._sweep_form.isRowVisible(selector))
        tab.set_sweep_locked(True)
        self.assertFalse(selector.isEnabled())
        tab.set_sweep_locked(False)
        self.assertTrue(selector.isEnabled())
