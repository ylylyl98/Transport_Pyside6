"""Polling profile presentation, validation, and move option integration."""
import unittest
import tempfile
from pathlib import Path

from PySide6.QtCore import QCoreApplication, QSettings

from app.models import BFieldTransportParams, SaveRoot
from app.engine.bfield_transport_sweep import BFieldTransportSafetyError, TransportSweepPlan
from app.quick import parameters
from app.quick.measurements import MeasurementService
import tests.test_hysteresis_transport as fixtures
from tests.test_quick_measurements import FakeManager


class PollingConfigurationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QCoreApplication.instance() or QCoreApplication([])

    def test_service_choice_round_trip_and_settings_reload(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = QSettings(str(Path(directory) / 'quick.ini'), QSettings.Format.IniFormat)
            def service():
                result = MeasurementService(FakeManager(), SaveRoot(base=directory), settings=settings,
                                            selected_backend_callable=lambda: '1000')
                result.mode = 5
                return result
            first = service()
            self.assertEqual(first.collect_params().aps100_polling_profile, 'standard')
            for choice in ('standard', 'reduced_mode_queries', 'standard', 'reduced_mode_queries'):
                with self.subTest(choice=choice):
                    first.setValue('aps100_polling_profile', choice)
                    self.assertEqual(first.collect_params().aps100_polling_profile, choice)
                    self.assertNotIn(('', -1, 'aps100_polling_profile'), first._field_errors[5])
                    self.assertTrue(first.canStart)
            first.setValue('base_name', 'polling_reload_marker')
            settings.sync()
            second = service()
            self.assertEqual(second.collect_params().aps100_polling_profile, 'reduced_mode_queries')
            self.assertEqual(second.collect_params().base_name, 'polling_reload_marker')
            self.assertTrue(second.canStart)
            first.deleteLater()
            second.deleteLater()

    def test_default_form_values_pass_conversion(self):
        values = parameters.defaults(5)
        converted = {key: parameters.convert(value, value, key, 5)
                     for key, value in values.items()}
        self.assertEqual(parameters.build_params(converted, 5).aps100_polling_profile, 'standard')

    def test_model_default_and_quick_choices(self):
        self.assertEqual(BFieldTransportParams().aps100_polling_profile, 'standard')
        descriptor = parameters.descriptor('aps100_polling_profile', 'standard', 5)
        self.assertEqual([entry['value'] for entry in descriptor['options']],
                         ['standard', 'reduced_mode_queries'])

    def test_invalid_or_inapplicable_profiles_rejected(self):
        for profile, backend, mode, approach in (
            ('unknown', '1000', 'stepped', 'no_overshoot'),
            ('reduced_mode_queries', '2100', 'stepped', 'auto'),
            ('reduced_mode_queries', '1000', 'continuous', 'linear'),
        ):
            with self.subTest(profile=profile, backend=backend, mode=mode):
                params = BFieldTransportParams(aps100_polling_profile=profile,
                    magnet_backend=backend, measurement_mode=mode, field_approach=approach)
                with self.assertRaises(BFieldTransportSafetyError):
                    TransportSweepPlan.from_params(params)


class PollingPropagationTests(unittest.TestCase):
    setUpClass = fixtures.HysteresisAcceptanceTests.setUpClass
    setUp = fixtures.HysteresisAcceptanceTests.setUp
    reading = fixtures.HysteresisAcceptanceTests.reading
    move_result = fixtures.HysteresisAcceptanceTests.move_result

    def test_move_options_carry_selected_profile_for_measurement_and_preparation(self):
        self.params.aps100_polling_profile = 'reduced_mode_queries'
        self.controller.plan = TransportSweepPlan.from_params(self.params)
        self.controller._initial_step_direction = 1
        self.controller._step_direction = 1
        for initial in (False, True):
            options = self.controller._hysteresis_move_options(initial=initial)
            self.assertEqual(options['polling_profile'], 'reduced_mode_queries')
            self.assertEqual(options['preparation'], initial)
