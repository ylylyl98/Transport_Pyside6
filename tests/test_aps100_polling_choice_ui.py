"""Exercise the actual QML A/B selector and recovery from rejected input."""
import unittest

from PySide6 import QtCore, QtTest
import tests.test_quick_forms as fixtures


class PollingChoiceUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fixtures.QuickFormTests.setUpClass()
        cls.app = fixtures.QuickFormTests.app

    setUp = fixtures.QuickFormTests.setUp
    tearDown = fixtures.QuickFormTests.tearDown
    load = fixtures.QuickFormTests.load
    find = fixtures.QuickFormTests.find

    def test_actual_selector_accepts_both_values_and_clears_error(self):
        self.service.mode = 5
        self.service.setValue('measurement_mode', 'stepped')
        self.service.setValue('field_approach', 'no_overshoot')
        self.load()
        header = self.find('measurement_section_Communication comparison')
        header.forceActiveFocus()
        QtTest.QTest.keyClick(self.window, QtCore.Qt.Key_Space)
        self.app.processEvents()
        self.service.setValue('aps100_polling_profile', 'invalid')
        self.assertFalse(self.service.canStart)
        for index, value, label in ((0, 'standard', 'Standard (A)'),
                                    (1, 'reduced_mode_queries', 'Fewer mode queries (B)'),
                                    (0, 'standard', 'Standard (A)')):
            selector = self.find('field_aps100_polling_profile')
            self.assertTrue(selector.isVisible())
            self.assertTrue(QtCore.QMetaObject.invokeMethod(
                selector, 'activated', QtCore.Q_ARG(int, index)))
            self.app.processEvents()
            self.assertEqual(self.service.collect_params().aps100_polling_profile, value)
            self.assertEqual(selector.property('displayText'), label)
            self.assertTrue(self.service.canStart)
            self.assertNotIn(('', -1, 'aps100_polling_profile'), self.service._field_errors[5])


if __name__ == '__main__':
    unittest.main()
