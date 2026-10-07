import unittest
import tempfile
from pathlib import Path

from PySide6 import QtWidgets

from app.sample_heater_ranges import select_heater_range, validate_heater_ranges
from app.ui.sample_heater_range_dialog import SampleHeaterRangeDialog
from utils.config import cfg


# Deliberately synthetic test bands, not instrument commissioning defaults.
TEST_BANDS = [
    {"upper_temperature_k": 10, "heater_range": 1},
    {"upper_temperature_k": 100, "heater_range": 2},
    {"upper_temperature_k": 300, "heater_range": 3},
]


class HeaterRangePolicyTests(unittest.TestCase):
    def test_boundaries_are_inclusive_and_next_band_starts_above_boundary(self):
        for target, expected in ((1, 1), (10, 1), (10.001, 2), (100, 2), (100.001, 3), (300, 3)):
            self.assertEqual(select_heater_range(target, TEST_BANDS)["heater_range"], expected)

    def test_uncovered_or_invalid_targets_are_rejected_without_last_band_fallback(self):
        for target in (0, -1, float("nan"), float("inf"), 301, "bad"):
            with self.assertRaises(ValueError):
                select_heater_range(target, TEST_BANDS)

    def test_invalid_tables_are_rejected_even_after_an_early_matching_band(self):
        bad = ([], None, {}, [None],
               [{"upper_temperature_k": 0, "heater_range": 1}],
               [{"upper_temperature_k": 10, "heater_range": 1.5}],
               [{"upper_temperature_k": 10, "heater_range": True}],
               [{"upper_temperature_k": float("nan"), "heater_range": 1}],
               [{"upper_temperature_k": True, "heater_range": 1}],
               [TEST_BANDS[0], TEST_BANDS[0]],
               [TEST_BANDS[1], TEST_BANDS[0]],
               [TEST_BANDS[0], {"upper_temperature_k": 20, "heater_range": 4}])
        for table in bad:
            with self.subTest(table=table), self.assertRaises(ValueError):
                select_heater_range(1, table)

    def test_validating_and_selecting_do_not_mutate_the_saved_table(self):
        result = validate_heater_ranges(TEST_BANDS)
        selected = select_heater_range(1, TEST_BANDS)
        selected["heater_range"] = 0
        result[0]["heater_range"] = 0
        self.assertEqual(TEST_BANDS[0]["heater_range"], 1)

    def test_an_explicit_off_band_is_supported(self):
        self.assertEqual(select_heater_range(1, [{"upper_temperature_k": 300, "heater_range": 0}])["heater_range"], 0)

    def test_saved_custom_and_instrument_sources_survive_config_reload(self):
        config = type(cfg)()
        other = type(cfg)()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            config.lakeshore335.sample_auto_heater_ranges = TEST_BANDS
            config.save(path)
            other.load(path)
            self.assertEqual(other.lakeshore335.sample_auto_heater_ranges, TEST_BANDS)
            config.lakeshore335.sample_auto_heater_ranges = []
            config.save(path)
            other.load(path)
            self.assertEqual(other.lakeshore335.sample_auto_heater_ranges, [])


class HeaterRangeEditorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def test_no_table_defaults_to_instrument_source_without_inventing_thresholds(self):
        saved = []
        dialog = SampleHeaterRangeDialog([], saved.append)
        self.addCleanup(lambda: dialog.deleteLater())
        self.assertTrue(dialog.use_instrument.isChecked())
        self.assertEqual(dialog.table.rowCount(), 0)
        dialog.save()
        self.assertEqual(saved, [[]])

    def test_custom_rows_save_validated_values(self):
        saved = []
        dialog = SampleHeaterRangeDialog(TEST_BANDS, saved.append)
        self.addCleanup(lambda: dialog.deleteLater())
        dialog.table.item(0, 0).setText("8.5")
        dialog.save()
        self.assertEqual(saved[0][0], {"upper_temperature_k": 8.5, "heater_range": 1})

    def test_invalid_or_failed_save_leaves_dialog_open(self):
        saved = []
        dialog = SampleHeaterRangeDialog(TEST_BANDS, saved.append)
        self.addCleanup(lambda: dialog.deleteLater())
        dialog.table.item(1, 0).setText("5")
        dialog.save()
        self.assertEqual(saved, [])
        self.assertIn("increasing", dialog.error_label.text())
        self.assertEqual(dialog.result(), 0)

    def test_switching_source_returns_empty_table_and_does_not_command_an_instrument(self):
        saved = []
        dialog = SampleHeaterRangeDialog(TEST_BANDS, saved.append)
        self.addCleanup(lambda: dialog.deleteLater())
        dialog.use_instrument.setChecked(True)
        dialog.save()
        self.assertEqual(saved, [[]])


if __name__ == "__main__":
    unittest.main()
