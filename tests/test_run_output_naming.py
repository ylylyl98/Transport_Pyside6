import os
import tempfile
import unittest
from unittest.mock import patch

from app.models import SaveRoot
from app.run_output import build_planned_output, new_run_id


class OutputNamingTests(unittest.TestCase):
    def test_timestamp_names_are_readable_and_unique_within_one_second(self):
        with patch("app.run_output.datetime") as clock:
            clock.datetime.now.return_value.strftime.return_value = "20990101_010203"
            ids = [new_run_id() for _ in range(3)]
        self.assertEqual(ids, ["20990101_010203", "20990101_010203_02", "20990101_010203_03"])

    def test_existing_companion_files_get_one_shared_numeric_suffix(self):
        for attribute in ("csv_path", "metadata_path", "log_path"):
            with self.subTest(attribute=attribute), tempfile.TemporaryDirectory() as root:
                save = SaveRoot(base=root, user="operator", device_id="device")
                original = build_planned_output(save, "map_2d", "map", run_id="20990101_010203", create_dir=True)
                with open(getattr(original, attribute), "w") as stream:
                    stream.write("preserve")
                next_run = build_planned_output(save, "map_2d", "map", run_id=original.run_id)
                self.assertTrue(next_run.csv_name.endswith("20990101_010203_02.csv"))
                self.assertEqual(next_run.metadata_path, next_run.csv_path[:-4] + "_metadata.json")
                self.assertEqual(next_run.log_path, next_run.csv_path[:-4] + "_run_log.txt")
                self.assertEqual(next_run.display_stem, original.display_stem)
                with open(getattr(original, attribute)) as stream:
                    self.assertEqual(stream.read(), "preserve")
                repeated = build_planned_output(save, "map_2d", "map", run_id=next_run.run_id)
                self.assertEqual(repeated, next_run)
