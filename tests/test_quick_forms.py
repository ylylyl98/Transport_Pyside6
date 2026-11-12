"""Offline interaction regressions for persistent Quick parameter editors."""
import os
import unittest
if os.environ.get("TRANSPORT_OFFLINE_VERIFICATION") != "1":
    raise unittest.SkipTest("Run through verify_offline.py")

from pathlib import Path
import tempfile
from unittest.mock import patch

from PySide6 import QtCore, QtQml, QtTest, QtWidgets
from PySide6.QtQuickControls2 import QQuickStyle
from app.models import SaveRoot
from app.quick.measurements import MeasurementService, build_cosweep_points
from app.quick.parameters import defaults
from tests.test_quick_measurements import FakeManager


class QuickFormTests(unittest.TestCase):
    def test_rate_limit_per_second_and_hysteresis_limits_are_visible(self):
        from app.quick.parameters import measurement_sections
        from app.models import BFIELD_HYSTERESIS_LABELS
        from utils.config import cfg
        values = defaults(5)
        values.update(measurement_mode="stepped", rate_t_per_min=.12)
        with patch.object(cfg.magnet, "maximum_rate_a_per_s", .01), patch.object(cfg.magnet, "coil_constant_t_per_a", .2):
            sections = measurement_sections(values, 5)
        rate = next(s for s in sections if any(f['key'] == 'rate_t_per_min' for f in s['fields']))
        self.assertIn('Configured maximum: 20 Oe/s', rate['description'])
        self.assertIn('Current request: 20 Oe/s', rate['description'])
        self.assertIn('voltage limits', rate['description'])
        fields = {f['key']: f for s in sections for f in s['fields']}
        self.assertTrue(set(BFIELD_HYSTERESIS_LABELS).issubset(fields))
        self.assertEqual(fields['field_acceptance_t']['value'], .5)
        self.assertEqual(fields['field_drift_t_per_s']['value'], .1)

    def test_stepped_form_exposes_selected_spacing_and_temperature(self):
        from app.quick.parameters import measurement_sections, build_params
        values = defaults(5)
        self.assertEqual(values["measurement_mode"], "continuous")
        values.update(measurement_mode="stepped", field_spacing="step", field_step_t=0.2)
        sections = measurement_sections(values, 5)
        keys = {item["key"] for section in sections for item in section.get("fields", [])}
        self.assertIn("field_step_t", keys)
        self.assertIn("field_points", keys)
        items = {item["key"]: item for section in sections for item in section.get("fields", [])}
        self.assertEqual(items["field_step_t"]["label"], "Step size (mT)")
        self.assertEqual(items["field_step_t"]["value"], 200.)
        self.assertTrue(items["field_points"]["readOnly"])
        self.assertEqual(items["field_points"]["value"], 6)
        self.assertNotIn("field_spacing", keys)
        self.assertIn("rate_t_per_min", keys)
        self.assertEqual(items["field_tolerance_t"]["value"], .2)
        self.assertIn("temperature_target_k", keys)
        self.assertEqual(build_params(values, 5).field_step_t, 0.2)

    def test_2100_form_explains_sdk_control_and_hides_aps_options(self):
        from app.quick.parameters import measurement_sections
        values = defaults(5)
        values["measurement_mode"] = "stepped"
        sections = measurement_sections(values, 5, magnet_backend="2100")
        keys = {field["key"] for section in sections for field in section.get("fields", [])}
        self.assertNotIn("magnet_backend", keys)
        self.assertNotIn("rate_t_per_min", keys)
        self.assertNotIn("cooldown_policy", keys)
        self.assertIn("temperature_target_k", keys)
        self.assertIn("SDK", " ".join(section.get("description", "") for section in sections))

    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
        QQuickStyle.setStyle("Fusion")

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.manager = FakeManager()
        self.service = MeasurementService(self.manager, SaveRoot(base=self.directory.name))
        self.engine = QtQml.QQmlApplicationEngine()
        self.warnings = []
        self.engine.warnings.connect(lambda items: self.warnings.extend(str(i) for i in items))
        self.engine.rootContext().setContextProperty("service", self.service)

    def load(self, component="MeasurementPane", width=700):
        source = ("import QtQuick\nimport QtQuick.Controls\nApplicationWindow {\n"
                  f"width: {width}; height: 1000; visible: true\n" + component + " {\n"
                  "width: parent.width\n" + ("measurement: service" if component == "MeasurementPane" else
                  "fields: service.fields; target: service") + "\n}\n}")
        self.engine.loadData(source.encode(), QtCore.QUrl.fromLocalFile(str(
            Path(__file__).resolve().parents[1] / "app/quick/qml/FormsHarness.qml")))
        self.assertTrue(self.engine.rootObjects(), self.warnings)
        self.window = self.engine.rootObjects()[0]
        self.app.processEvents()

    def find(self, name):
        def descendants(item):
            yield item
            for child in item.childItems():
                yield from descendants(child)
        return next(i for i in descendants(self.window.contentItem()) if i.objectName() == name)

    def recipe_cell(self, key, row, column):
        model = self.service.rowModel(key)
        uid = model.data(model.index(row, 0), model.UidRole)
        return self.find(f"recipeCell_{key}_{uid}_{column}")

    def tearDown(self):
        if self.engine.rootObjects():
            self.engine.rootObjects()[0].hide()
        self.engine.deleteLater()
        self.app.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)
        self.service.deleteLater()
        self.app.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)
        self.directory.cleanup()
        self.assertEqual(self.warnings, [])

    def type_input(self, item, value):
        item.forceActiveFocus()
        QtTest.QTest.keyClick(self.window, QtCore.Qt.Key_A, QtCore.Qt.ControlModifier)
        for character in str(value):
            QtTest.QTest.keyClick(self.window, ord(character.upper()))

    def test_hysteresis_field_tolerance_editor_commits_mt_as_tesla(self):
        self.service.mode = 5
        self.service.setValue("measurement_mode", "stepped")
        self.load()
        header = self.find("measurement_section_Hysteresis: set, wait, measure")
        header.forceActiveFocus()
        QtTest.QTest.keyClick(self.window, QtCore.Qt.Key_Space)
        self.app.processEvents()
        editor = self.find("field_field_tolerance_t")
        editor.forceActiveFocus()
        self.type_input(editor, "0.3")
        QtTest.QTest.keyClick(self.window, QtCore.Qt.Key_Return)
        self.app.processEvents()
        self.assertAlmostEqual(self.service.collect_params().field_tolerance_t, .0003)
        self.assertEqual(float(editor.property("text")), .3)
        self.assertTrue(self.find("field_field_stability_s").isVisible())
        self.assertTrue(self.find("field_field_settle_s").isVisible())

    def test_measurement_draft_and_focus_survive_sibling_edits_and_telemetry(self):
        self.load()
        original = self.find("field_vds_stop")
        original.forceActiveFocus()
        self.type_input(original, "0.12345")
        for n in range(30):
            self.service.setValue("n_sample", n + 1)
            self.manager.status_changed.emit("g1", "connected", "telemetry")
            self.app.processEvents()
        self.assertEqual(self.find("field_vds_stop"), original)

        self.assertEqual(original.property("text"), "0.12345")
        self.assertTrue(original.hasActiveFocus())
        self.assertNotEqual(self.service.collect_params().vds_stop, .12345)
        QtTest.QTest.keyClick(self.window, QtCore.Qt.Key.Key_Return)
        self.app.processEvents()
        self.assertEqual(self.service.collect_params().vds_stop, .12345)
        self.assertEqual(self.find("field_vds_stop"), original)

    def test_derived_explanation_and_preview_update_without_replacing_inputs(self):
        self.service.mode = 1
        self.service.setValue("mode", "Derived")
        self.load(width=420)
        field = self.find("field_derived_start")
        help_label = self.find("measurementHelp_Derived trajectory")
        preview = self.find("measurementVoltagePreview_Derived trajectory")
        self.assertIn("Doping = Vtg + r*Vbg", help_label.property("text"))
        self.assertTrue(help_label.isVisible())
        field.forceActiveFocus()
        self.type_input(field, "-2")
        QtTest.QTest.keyClick(self.window, QtCore.Qt.Key.Key_Return)
        self.app.processEvents()
        self.assertIn("Requested start: Vtg = -1 V; Vbg = -1 V", preview.property("text"))
        self.assertEqual(self.find("field_derived_start"), field)
        self.service.setValue("derived_ratio_target", "Vtg")
        self.app.processEvents()
        self.assertIn("Doping = r*Vtg + Vbg", help_label.property("text"))
        self.assertEqual(self.find("field_derived_start"), field)
        self.service.setValue("derived_ratio", 0.)
        self.app.processEvents()
        self.assertIn("non-zero", preview.property("text"))

    def test_rejected_text_stays_visible_and_blocks_start_until_correction(self):
        self.load()
        original = self.find("field_n_sample")
        original.forceActiveFocus()
        self.type_input(original, "1.5")
        QtTest.QTest.keyClick(self.window, QtCore.Qt.Key.Key_Return)
        self.app.processEvents()
        self.assertEqual(self.find("field_n_sample"), original)
        self.assertEqual(original.property("text"), "1.5")
        self.assertFalse(self.service.canStart)
        self.assertEqual(self.service.collect_params().n_sample, defaults(0)["n_sample"])
        with patch.object(self.service, "_begin_preflight") as preflight:
            self.service.start()
            preflight.assert_not_called()
        # Returning to the previously accepted value must also clear rejection.
        self.type_input(original, str(defaults(0)["n_sample"]))
        QtTest.QTest.keyClick(self.window, QtCore.Qt.Key.Key_Return)
        self.app.processEvents()
        self.assertEqual(self.service.collect_params().n_sample, defaults(0)["n_sample"])
        self.assertTrue(self.service.canStart)

    def test_inline_recipe_routes_by_uid_and_preserves_active_draft(self):
        self.service.mode = 4
        self.service.addRow("conditions")
        self.service.duplicateRow("conditions", 0)
        self.service.selectRow("conditions", 0)
        self.load(width=520)
        QtTest.QTest.qWait(40)
        original = self.recipe_cell("conditions", 0, "raw_vtg_start")
        original.forceActiveFocus()
        self.type_input(original, "4.5")
        self.service.setRowValue("conditions", 1, "raw_vtg_start", 3.)
        self.app.processEvents()
        self.assertIs(self.recipe_cell("conditions", 0, "raw_vtg_start"), original)
        self.assertEqual(original.property("text"), "4.5")
        self.assertTrue(original.hasActiveFocus())
        QtTest.QTest.keyClick(self.window, QtCore.Qt.Key_Return)
        self.app.processEvents()
        rows = self.service._values[4]["conditions"]
        self.assertEqual(rows[0]["raw_vtg_start"], 4.5)
        self.assertEqual(rows[1]["raw_vtg_start"], 3.)

    def test_collapsed_review_does_not_build_trajectories_on_form_edits(self):
        self.service.mode = 2
        self.service.setValue("axis_slow", "Vbg")
        with patch("app.quick.measurements.build_cosweep_points", wraps=build_cosweep_points) as build:
            self.load()
            for n in range(30):
                self.service.setValue("n_sample", n + 1)
                self.app.processEvents()
            build.assert_not_called()
            self.find("measurementReviewToggle").setProperty("checked", True)
            self.app.processEvents()
            self.assertEqual(build.call_count, 1)
            self.assertIn("points", self.find("measurementTrajectoryReview").property("text"))
            self.find("measurementReviewToggle").setProperty("checked", False)
            self.service.setValue("n_sample", 1)
            self.app.processEvents()
            self.assertEqual(build.call_count, 1)

    def test_recipe_actions_keep_selection_visible_and_identify_the_edited_row(self):
        self.service.mode = 3
        self.load()
        add = self.find("measurementAdd_bias_conditions")
        add.forceActiveFocus()
        QtTest.QTest.keyClick(self.window, QtCore.Qt.Key.Key_Space)
        self.app.processEvents()
        self.assertEqual(len(self.service._values[3]["bias_conditions"]), 2)
        self.assertEqual(self.service.rowModel("bias_conditions").selectedIndex, 1)

        duplicate = self.find("measurementDuplicate_bias_conditions")
        duplicate.forceActiveFocus()
        QtTest.QTest.keyClick(self.window, QtCore.Qt.Key.Key_Space)
        self.app.processEvents()
        self.assertEqual(len(self.service._values[3]["bias_conditions"]), 3)
        self.assertEqual(self.service.rowModel("bias_conditions").selectedIndex, 2)
        remove = self.find("measurementRemove_bias_conditions")
        remove.forceActiveFocus()
        QtTest.QTest.keyClick(self.window, QtCore.Qt.Key.Key_Space)
        self.app.processEvents()
        self.assertEqual(len(self.service._values[3]["bias_conditions"]), 2)
        self.assertEqual(self.service.rowModel("bias_conditions").selectedIndex, 1)

        for _ in range(12):
            self.service.addRow("bias_conditions")
            self.app.processEvents()
        QtTest.QTest.qWait(20)
        listing = self.find("measurementRows_bias_conditions")
        selected = self.find("measurementRow_bias_conditions_13")
        top = selected.mapToItem(listing, QtCore.QPointF()).y()
        self.assertGreaterEqual(top, -1)
        self.assertLessEqual(top + selected.height(), listing.height() + 1)
        self.service.selectRow("bias_conditions", 0)
        QtTest.QTest.qWait(20)
        first = self.find("measurementRow_bias_conditions_0")
        self.assertAlmostEqual(first.mapToItem(listing, QtCore.QPointF()).y(), 0, delta=1)

    def test_advanced_section_starts_collapsed_and_keyboard_toggle_preserves_draft(self):
        self.load()
        toggle = self.find("measurement_section_Advanced ramps and conversion")
        ramp = self.find("field_vg_ramp")
        self.assertFalse(ramp.isVisible())
        toggle.forceActiveFocus()
        QtTest.QTest.keyClick(self.window, QtCore.Qt.Key.Key_Space)
        self.app.processEvents()
        self.assertTrue(ramp.isVisible())
        ramp.forceActiveFocus()
        self.type_input(ramp, "0.125")
        self.service.setValue("n_sample", 5)
        self.app.processEvents()
        self.assertEqual(self.find("field_vg_ramp"), ramp)
        self.assertEqual(ramp.property("text"), "0.125")
        self.assertTrue(ramp.hasActiveFocus())

    def test_forward_reverse_remains_operable_after_mode_layout_changes(self):
        self.load()
        for mode in (1, 0, 4, 1):
            self.service.mode = mode
            QtTest.QTest.qWait(30)
            toggle = self.find("field_sweep_both_ways")
            self.assertTrue(toggle.isVisible())
            if mode in (1, 4):
                next_section = self.find("measurement_section_Vtg sweep")
                bottom = toggle.mapToScene(QtCore.QPointF(0, toggle.height())).y()
                self.assertLessEqual(bottom, next_section.mapToScene(QtCore.QPointF()).y())
            original = self.service.collect_params().sweep_both_ways
            toggle.forceActiveFocus()
            QtTest.QTest.keyClick(self.window, QtCore.Qt.Key.Key_Space)
            self.app.processEvents()
            self.assertEqual(self.service.collect_params().sweep_both_ways, not original)

    def test_recipe_inline_grid_retains_tab_order_at_panel_width(self):
        self.service.mode = 3
        self.load(width=366)
        QtTest.QTest.qWait(30)
        enabled = self.recipe_cell("bias_conditions", 0, "enabled")
        field = self.recipe_cell("bias_conditions", 0, "vtg")
        self.assertTrue(enabled.isVisible())
        self.assertTrue(field.isVisible())
        self.assertGreaterEqual(field.mapToScene(QtCore.QPointF()).x(), 8)
        self.assertLessEqual(field.mapToScene(QtCore.QPointF(field.width(), 0)).x(), 366)
        enabled.forceActiveFocus()
        QtTest.QTest.keyClick(self.window, QtCore.Qt.Key_Tab)
        self.assertEqual(self.window.activeFocusItem(), field)
        listing = self.find("measurementRows_bias_conditions")
        self.assertLessEqual(listing.property("contentWidth"), listing.width() + 1)
        additional = max(i for i, section in enumerate(self.service.uiSections) if not section["advanced"]) + 1
        heading = self.find(f"measurementAdditionalSettings_{additional}")
        self.assertTrue(heading.isVisible())
        section = self.find("measurement_section_Advanced ramps and conversion")
        self.assertLess(heading.mapToScene(QtCore.QPointF()).y(), section.mapToScene(QtCore.QPointF()).y())

    def test_global_vds_toggle_reverts_instead_of_hiding_an_invalid_draft(self):
        self.service.mode = 3
        self.service.setValue("use_vds", True)
        self.load(width=520)
        QtTest.QTest.qWait(30)
        field = self.recipe_cell("bias_conditions", 0, "vds")
        field.forceActiveFocus()
        QtTest.QTest.keyClick(self.window, QtCore.Qt.Key_A, QtCore.Qt.ControlModifier)
        for key in (QtCore.Qt.Key_B, QtCore.Qt.Key_A, QtCore.Qt.Key_D):
            QtTest.QTest.keyClick(self.window, key)
        toggle = self.find("field_use_vds")
        toggle.forceActiveFocus()
        QtTest.QTest.keyClick(self.window, QtCore.Qt.Key_Space)
        self.app.processEvents()
        self.assertTrue(self.service.collect_params().use_vds)
        self.assertTrue(toggle.property("checked"))
        self.assertFalse(field.property("readOnly"))
        field.forceActiveFocus()
        QtTest.QTest.keyClick(self.window, QtCore.Qt.Key_A, QtCore.Qt.ControlModifier)
        QtTest.QTest.keyClick(self.window, QtCore.Qt.Key_0)
        QtTest.QTest.keyClick(self.window, QtCore.Qt.Key_Return)
        toggle.forceActiveFocus()
        QtTest.QTest.keyClick(self.window, QtCore.Qt.Key_Space)
        self.app.processEvents()
        self.assertFalse(self.service.collect_params().use_vds)
        self.assertFalse(self.find("field_use_vds").property("checked"))
        self.assertTrue(field.property("readOnly"))

    def test_advanced_classification_change_keeps_gate_conversion_draft_and_focus(self):
        self.service.mode = 2
        self.load(width=366)
        header = self.find("measurement_section_Gate conversion")
        header.forceActiveFocus()
        QtTest.QTest.keyClick(self.window, QtCore.Qt.Key.Key_Space)
        self.app.processEvents()
        ratio = self.find("field_ratio")
        ratio.forceActiveFocus()
        self.type_input(ratio, "1.2345")
        self.service.setValue("coordinate_mode", "Derived")
        self.app.processEvents()
        self.assertEqual(self.find("field_ratio"), ratio)
        self.assertEqual(ratio.property("text"), "1.2345")
        self.assertTrue(ratio.hasActiveFocus())
        self.assertNotEqual(self.service.collect_params().ratio, 1.2345)

    def test_pending_recipe_reveal_does_not_use_a_removed_list_after_mode_change(self):
        self.service.mode = 3
        self.load(width=366)
        self.service.addRow("bias_conditions")
        self.service.mode = 4
        self.service.addRow("conditions")
        self.service.duplicateRow("conditions", 0)
        QtTest.QTest.qWait(30)
        self.assertEqual(self.service.rowModel("conditions").selectedIndex, 1)

    def test_generic_form_preserves_active_draft_across_refresh(self):
        self.load("FieldForm")
        original = self.find("field_vds_stop")
        original.forceActiveFocus()
        self.type_input(original, "0.654")
        self.service.setValue("n_sample", 3)
        self.app.processEvents()
        self.assertEqual(self.find("field_vds_stop"), original)
        self.assertEqual(original.property("text"), "0.654")
        self.assertTrue(original.hasActiveFocus())

    def test_all_filename_editors_wrap_and_restore_output_preview_at_panel_width(self):
        self.load(width=366)
        name = "1.67K01_REFImogon_ACDMCs6E1_DCMCs6E2_AmpMoTe2_" + "long_sample_description_" * 4
        for mode in range(6):
            self.service.mode = mode
            QtTest.QTest.qWait(30)
            editor = self.find("field_base_name")
            if not editor.isVisible():
                header = self.find("measurement_section_File output")
                header.forceActiveFocus()
                QtTest.QTest.keyClick(self.window, QtCore.Qt.Key_Space)
                self.app.processEvents()
            self.assertTrue(editor.isVisible())
            editor.forceActiveFocus()
            editor.setProperty("text", name)
            self.app.processEvents()
            self.assertGreater(editor.property("lineCount"), 1, mode)
            self.assertGreaterEqual(editor.width(), 250, mode)
            self.assertLessEqual(editor.width(), 366, mode)
            self.assertGreater(editor.height(), 40, mode)
            field_owner = editor.parentItem()
            while field_owner.property("formField") is None:
                field_owner = field_owner.parentItem()
            self.assertTrue(field_owner.findChild(QtCore.QObject, "expandFieldButton").isVisible(), mode)
            QtTest.QTest.keyClick(self.window, QtCore.Qt.Key_Return)
            self.app.processEvents()
            self.assertEqual(self.service.collect_params().base_name, name, mode)
            editor.setProperty("text", name + "TabApplied")
            QtTest.QTest.keyClick(self.window, QtCore.Qt.Key_Tab)
            self.app.processEvents()
            self.assertFalse(editor.hasActiveFocus(), mode)
            self.assertEqual(self.service.collect_params().base_name, name + "TabApplied", mode)
            editor.forceActiveFocus()
            QtTest.QTest.keyClick(self.window, QtCore.Qt.Key_Tab, QtCore.Qt.ShiftModifier)
            self.app.processEvents()
            self.assertFalse(editor.hasActiveFocus(), mode)
            self.assertNotIn("\t", editor.property("text"), mode)
            QtTest.QTest.qWait(170)
            preview = self.find("filenamePreview")
            self.assertTrue(preview.isVisible(), mode)
            self.assertTrue(preview.property("readOnly"), mode)
            self.assertIn(name, preview.property("text"), mode)
            self.assertIn("{timestamp}", preview.property("text"), mode)

    def test_filename_card_previews_drafts_copies_full_names_and_collapses(self):
        self.load(width=366)
        header = self.find("measurement_section_File output")
        header.forceActiveFocus()
        QtTest.QTest.keyClick(self.window, QtCore.Qt.Key_Space)
        self.app.processEvents()
        editor = self.find("field_base_name")
        self.assertTrue(editor.isVisible())
        accepted = self.service.collect_params().base_name
        draft = "sample_draft_" + "long_name_" * 15
        editor.forceActiveFocus()
        editor.setProperty("text", draft)
        QtTest.QTest.qWait(180)
        preview = self.find("filenamePreview")
        self.assertIn(draft, preview.property("text"))
        self.assertEqual(self.service.collect_params().base_name, accepted)
        self.assertIn("Draft preview", self.find("filenameDraftNotice").property("text"))
        # Input and preview share the card rather than separate scrolling sections.
        self.assertLess(editor.mapToScene(QtCore.QPointF()).y(), preview.mapToScene(QtCore.QPointF()).y())
        copy = self.find("copyFilenamePreview")
        copy.forceActiveFocus()
        QtTest.QTest.qWait(160)
        QtTest.QTest.keyClick(self.window, QtCore.Qt.Key_Space)
        self.assertEqual(self.app.clipboard().text(), preview.property("text"))
        self.assertEqual(self.service.collect_params().base_name, draft)
        header = self.find("measurement_section_File output")
        header.forceActiveFocus()
        QtTest.QTest.keyClick(self.window, QtCore.Qt.Key_Space)
        self.app.processEvents()
        self.assertFalse(editor.isVisible())
        self.assertFalse(preview.isVisible())
        self.assertTrue(self.find("outputSummaryCount").isVisible())
        QtTest.QTest.keyClick(self.window, QtCore.Qt.Key_Space)
        self.app.processEvents()
        self.assertTrue(editor.isVisible())
        self.assertEqual(editor.property("text"), draft)

    def test_field_batch_filename_card_edits_the_selected_condition(self):
        self.service.mode = 4
        self.service.setValue("fields_t", "0, 1")
        self.service.addRow("conditions")
        self.service.duplicateRow("conditions", 0)
        self.service.selectRow("conditions", 1)
        self.load(width=366)
        header = self.find("measurement_section_File output")
        header.forceActiveFocus()
        QtTest.QTest.keyClick(self.window, QtCore.Qt.Key_Space)
        QtTest.QTest.qWait(160)
        self.assertTrue(self.find("filenameEditingScope").isVisible())
        self.assertIn("Condition 2", self.find("filenameEditingScope").property("text"))
        editor = self.find("field_base_name")
        editor.forceActiveFocus()
        editor.setProperty("text", "condition_two_draft")
        QtTest.QTest.qWait(160)
        self.assertIn("condition_two_draft", self.find("filenamePreview").property("text"))
        self.assertIn("4 CSV", self.find("outputSummaryCount").property("text"))
        QtTest.QTest.keyClick(self.window, QtCore.Qt.Key_Return)
        self.assertEqual(self.service._values[4]["conditions"][1]["base_name"], "condition_two_draft")
        self.assertNotEqual(self.service._values[4]["conditions"][0]["base_name"], "condition_two_draft")
        QtTest.QTest.qWait(160)
        self.assertNotIn("Draft preview", self.find("filenameDraftNotice").property("text"))
        # Changing the selected condition must update the always-visible card,
        # even while the condition's advanced settings remain collapsed.
        self.service.selectRow("conditions", 0)
        QtTest.QTest.qWait(160)
        self.assertIn("Condition 1", self.find("filenameEditingScope").property("text"))
        self.assertNotEqual(editor.property("text"), "condition_two_draft")
        editor.setProperty("text", "condition_one")
        QtTest.QTest.keyClick(self.window, QtCore.Qt.Key_Return)
        self.assertEqual(self.service._values[4]["conditions"][0]["base_name"], "condition_one")
        self.assertEqual(self.service._values[4]["conditions"][1]["base_name"], "condition_two_draft")

    def test_wrapped_filename_expand_preserves_draft_and_respects_measurement_lock(self):
        self.load(width=366)
        editor = self.find("field_base_name")
        original = self.service.collect_params().base_name
        draft = "a_long_filename_" * 12
        editor.forceActiveFocus()
        editor.setProperty("text", draft)
        QtTest.QTest.keyClick(self.window, QtCore.Qt.Key_Return, QtCore.Qt.AltModifier)
        self.app.processEvents()
        expanded = self.find("expandedFieldText")
        self.assertEqual(expanded.property("text"), draft)
        self.assertEqual(self.service.collect_params().base_name, original)
        expanded.setProperty("text", "expanded_only_draft")
        QtTest.QTest.qWait(160)
        self.assertIn("expanded_only_draft", self.find("filenamePreview").property("text"))
        QtTest.QTest.keyClick(self.window, QtCore.Qt.Key_Escape)
        QtTest.QTest.qWait(160)
        self.assertEqual(editor.property("text"), draft)
        self.assertIn(draft, self.find("filenamePreview").property("text"))
        self.assertNotIn("expanded_only_draft", self.find("filenamePreview").property("text"))
        editor.forceActiveFocus()
        QtTest.QTest.keyClick(self.window, QtCore.Qt.Key_Return, QtCore.Qt.AltModifier)
        expanded.setProperty("text", "saved_from_large_editor")
        QtTest.QTest.keyClick(self.window, QtCore.Qt.Key_Return, QtCore.Qt.ControlModifier)
        QtTest.QTest.qWait(160)
        original = "saved_from_large_editor"
        self.assertEqual(editor.property("text"), original)
        self.assertEqual(self.service.collect_params().base_name, original)
        self.assertIn(original, self.find("filenamePreview").property("text"))
        self.service._batch_claimed = True
        self.service.changed.emit()
        self.app.processEvents()
        self.assertTrue(editor.property("readOnly"))
        editor.forceActiveFocus()
        QtTest.QTest.keyClick(self.window, QtCore.Qt.Key_Return, QtCore.Qt.AltModifier)
        self.app.processEvents()
        self.assertTrue(expanded.property("readOnly"))
        QtTest.QTest.keyClick(self.window, QtCore.Qt.Key_Escape)
        self.app.processEvents()
        self.assertEqual(self.service.collect_params().base_name, original)
        self.service._batch_claimed = False
