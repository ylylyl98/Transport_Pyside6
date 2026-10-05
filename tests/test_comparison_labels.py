"""Compare saved settings, excluding shared values and incidental file names."""
import unittest
import json
import tempfile
from pathlib import Path

from app.plot_export_labels import captions, trace_label, comparison_labels


def record(path, phase=0., amplitude=.1):
    return {"path": path, "created_at": "2026-10-02T12:00:00", "status": "finished",
            "metadata": {"params": {"base_name": "Sample", "vtg_set": 1, "vbg_set": 0},
                         "signal_chain": {"preamp_gain_v_per_a": 1e7, "frequency_hz": 17,
                                          "lockin_settings": {"values": {"phase_deg": phase, "sine_out_v": amplitude}}}}}


def snapshot(records):
    return {"kind": "vds_sweep", "signal": "Ids_X", "x_name": "Vds", "records": records, "view": {}}


class ComparisonLabelTests(unittest.TestCase):
    def test_dual_frequency_summary_collapses_equal_display_values_only(self):
        from app.plot_export_labels import comparison_heading, conditions
        item = record('dual.csv')
        chain = item['metadata']['signal_chain']
        chain['lockin_settings']['values']['frequency_hz'] = 17.
        chain['drag_drive'] = {'enabled': True, 'instruments': {'drive': {'values': {'frequency_hz': 16.9997}}}}
        _, detail = comparison_heading([item], 'I_drag_X')
        self.assertEqual(detail.count('17 Hz'), 1)
        self.assertIn('f=17 Hz', detail)
        self.assertEqual(conditions(item)['Drive f'], '16.9997 Hz')
        chain['drag_drive']['instruments']['drive']['values']['frequency_hz'] = 19.
        _, detail = comparison_heading([item], 'I_drag_X')
        self.assertIn('Drag f=17 Hz', detail)
        self.assertIn('Drive f=19 Hz', detail)

    def test_heading_includes_identity_fixed_conditions_and_saved_excitation(self):
        from app.plot_export_labels import comparison_heading
        item = record("sample.csv", phase=30, amplitude=.02)
        item["metadata"].update(save_root={"device_id": "YZ324"}, experiment_context={
            "temperature": {"available": True, "values": {"sample_temperature_k": 1.67}},
            "magnet1000": {"available": True, "values": {"field_t": 0}}
        })
        item["metadata"]["params"]["base_name"] = "ACDCMoTe2E2_DCMoTe2E1"
        title, detail = comparison_heading([item], "I_drive_X", excluded=("Vds",))
        self.assertIn("YZ324", title)
        self.assertIn("ACDCMoTe2E2", title)
        self.assertIn("I_drive_X", title)
        for required in ("T₀=1.67 K", "B₀=0 T", "Vtg=1 V", "f=17 Hz", "Vac=20 mV", "φ=30"):
            self.assertIn(required, detail)
        self.assertNotIn("gain", detail.lower())

    def test_heading_distinguishes_primary_map_settings_from_shared_conditions(self):
        from app.plot_export_labels import comparison_heading
        records = [record("first.csv", phase=0), record("second.csv", phase=90)]
        title, detail = comparison_heading(records, "Ids_X", primary_path="second.csv")
        self.assertIn("Map:", title)
        self.assertIn("Shared:", detail)
        shared, primary = detail.split("\nMap only: ")
        self.assertNotIn("φ", shared)
        self.assertIn("φ=90", primary)
        self.assertIn("f=17 Hz", shared)

    def test_heading_does_not_invent_missing_temperature_or_field(self):
        from app.plot_export_labels import comparison_heading
        item = record("sample.csv")
        item["metadata"]["experiment_context"] = {"temperature": {"available": False, "values": {"sample_temperature_k": 1.67}}}
        _, detail = comparison_heading([item], "Ids_X")
        self.assertNotIn("1.67", detail)
        self.assertNotIn("B =", detail)

    def test_shortening_names_cannot_hide_a_real_description_difference(self):
        for names in (("A_A", "A", "A_A_A"), ("A_B_C", "B_A_C", "A_B_D")):
            with self.subTest(names=names):
                records = [record("a.csv"), record("b.csv"), record("c.csv")]
                for item, name in zip(records, names):
                    item["metadata"]["params"]["base_name"] = name
                self.assertEqual(comparison_labels(records, [{"path": item["path"]} for item in records]),
                                 ["Name = " + name for name in names])

    def test_filename_fallback_uses_differing_parts_without_acquisition_timestamp(self):
        records = [{"path": "Sample_ACDCE1_20261002_193956.csv", "metadata": {}},
                   {"path": "Sample_ACDCE2_20261002_193439.csv", "metadata": {}}]
        self.assertEqual(comparison_labels(records, [{"path": item["path"]} for item in records]),
                         ["Name = ACDCE1", "Name = ACDCE2"])

    def test_json_sampling_settings_are_compared_without_output_path_noise(self):
        records = [record("a.csv"), record("b.csv")]
        for item, samples in zip(records, (3, 9)):
            item["metadata"]["params"].update(n_sample=samples, output_csv_path=item["path"], delay_s=.1)
        labels = comparison_labels(records, [{"path": item["path"]} for item in records])
        self.assertIn("Samples = 3", labels[0])
        self.assertIn("Samples = 9", labels[1])
        self.assertNotIn("delay", " ".join(labels).lower())
        self.assertNotIn(".csv", " ".join(labels))

    def test_differing_contact_in_saved_name_is_visible_when_instrument_settings_match(self):
        records = [record("a.csv", 110, .06), record("b.csv", 110, .06)]
        for item, contact in zip(records, ("ACDCMoTe2E1", "ACDCMoTe2E2")):
            item["metadata"]["params"]["base_name"] = f"1.67K0T_REFImgon_{contact}_DCMoTe2E2_AmpMoS2E1"
        labels = comparison_labels(records, [{"path": item["path"]} for item in records], ("Vds",))
        self.assertEqual(labels, ["Name = ACDCMoTe2E1", "Name = ACDCMoTe2E2"])

    def test_name_differences_preserve_added_tokens_and_missing_descriptions(self):
        records = [record("a.csv"), record("b.csv"), record("c.csv")]
        for item, name in zip(records, ("Sample_gateA", "Sample_gateA_lightOn", "")):
            item["metadata"]["params"]["base_name"] = name
        labels = comparison_labels(records, [{"path": item["path"]} for item in records])
        self.assertIn("Sample_gateA", labels[0])
        self.assertIn("lightOn", labels[1])
        self.assertIn("Name = c", labels[2])

    def test_same_named_repeat_still_has_distinct_run_labels(self):
        records = [record("a.csv"), record("b.csv")]
        self.assertEqual(comparison_labels(records, [{"path": item["path"]} for item in records]), ["Run 1", "Run 2"])

    def test_native_metadata_writer_promoted_settings_are_compared(self):
        from app.run_output import write_run_metadata
        for manifest in (False, True):
            with self.subTest(manifest=manifest), tempfile.TemporaryDirectory() as folder:
                records = [record("a.csv"), record("b.csv", 90, .2)]
                for i, item in enumerate(records):
                    metadata = item["metadata"]
                    if manifest:
                        metadata["validation"] = {"calibration": {"signal_chain": metadata.pop("signal_chain")}}
                    path = Path(folder) / f"meta{i}.json"
                    write_run_metadata(str(path), metadata)
                    item["metadata"] = json.loads(path.read_text())
                labels = comparison_labels(records, [{"path": item["path"]} for item in records])
                self.assertIn("Phase = 0", labels[0])
                self.assertIn("Phase = 90", labels[1])
                self.assertIn("Amplitude = 200 mV", labels[1])

    def test_legacy_numeric_strings_and_invalid_preamp_values_do_not_break_comparison(self):
        records = [record("a.csv"), record("b.csv")]
        for item, sensitivity in zip(records, ("1e-7", "unknown")):
            chain = item["metadata"]["signal_chain"]
            del chain["preamp_gain_v_per_a"]
            chain["preamp_sensitivity_a"] = sensitivity
        labels = comparison_labels(records, [{"path": item["path"]} for item in records])
        self.assertIn("Preamp gain = 1e+07 V/A", labels[0])
        self.assertIn("Preamp gain = unknown", labels[1])

    def test_primary_map_without_a_cut_does_not_change_visible_curve_labels(self):
        records = [record("map.csv", 90), record("cut.csv", 0)]
        labels = comparison_labels(records, [{"path": "cut.csv", "direction": "All"}], ("Vds",))
        self.assertEqual(labels, ["Run 1"])

    def test_phase_and_amplitude_differences_replace_shared_settings_and_filename(self):
        records = [record("a.csv"), record("b.csv", 90, .2)]
        _title, detail, differences = captions(snapshot(records))
        labels = [trace_label({"direction": "forward"}, r, differences, False) for r in records]
        self.assertIn("Phase = 0", labels[0])
        self.assertIn("Phase = 90", labels[1])
        self.assertIn("Amplitude = 100 mV", labels[0])
        self.assertIn("Amplitude = 200 mV", labels[1])
        for label in labels:
            for shared in ("Vtg", "Vbg", "Preamp", "17 Hz", ".csv", "2026-"):
                self.assertNotIn(shared, label)
        self.assertIn("Vtg=1 V", detail)

    def test_identical_sweep_ranges_are_shared_instead_of_repeated(self):
        records = [record("a.csv"), record("b.csv")]
        for item in records:
            item["metadata"]["params"].update(vtg_start=0, vtg_stop=2)
            del item["metadata"]["params"]["vtg_set"]
        _title, _detail, differences = captions(snapshot(records))
        labels = [trace_label({}, r, differences, False) for r in records]
        self.assertNotIn("Vtg", " ".join(labels))
        self.assertNotEqual(labels[0], labels[1])
        self.assertIn("Run 1", labels[0])

    def test_missing_phase_is_unknown_and_not_treated_as_zero(self):
        records = [record("a.csv"), record("b.csv")]
        del records[1]["metadata"]["signal_chain"]["lockin_settings"]["values"]["phase_deg"]
        _title, _detail, differences = captions(snapshot(records))
        labels = [trace_label({}, r, differences, False) for r in records]
        self.assertIn("Phase = 0", labels[0])
        self.assertIn("Phase = unknown", labels[1])

    def test_drive_settings_have_their_own_role_and_zero_phase(self):
        records = [record("a.csv"), record("b.csv")]
        for item, phase in zip(records, (0, -90)):
            item["metadata"]["signal_chain"]["drag_drive"] = {
                "enabled": True, "series_resistance_ohm": 1e6,
                "instruments": {"drag": {"values": {"phase_deg": 0}},
                                "drive": {"values": {"phase_deg": phase, "sine_out_v": .1}}}}
        _title, _detail, differences = captions(snapshot(records))
        labels = [trace_label({}, r, differences, False) for r in records]
        self.assertIn("Drive Phase = 0", labels[0])
        self.assertIn("Drive Phase = −90", labels[1])
        self.assertNotIn("Drag Phase", " ".join(labels))


if __name__ == "__main__":
    unittest.main()
