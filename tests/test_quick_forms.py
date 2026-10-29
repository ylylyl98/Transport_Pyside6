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

    def tearDown(self):
        if self.engine.rootObjects():
            self.engine.rootObjects()[0].hide()
        self.engine.deleteLater()
        self.app.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)
        self.service.deleteLater()
        self.app.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)
        self.directory.cleanup()
        self.assertEqual(self.warnings, [])

    def test_measurement_draft_and_focus_survive_sibling_edits_and_telemetry(self):
        self.load()
        original = self.find("field_vds_stop")
        original.forceActiveFocus()
        original.setProperty("text", "0.12345")
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

    def test_rejected_text_stays_visible_and_blocks_start_until_correction(self):
        self.load()
        original = self.find("field_n_sample")
        original.forceActiveFocus()
        original.setProperty("text", "1.5")
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
        original.setProperty("text", str(defaults(0)["n_sample"]))
        QtTest.QTest.keyClick(self.window, QtCore.Qt.Key.Key_Return)
        self.app.processEvents()
        self.assertEqual(self.service.collect_params().n_sample, defaults(0)["n_sample"])
        self.assertTrue(self.service.canStart)

    def test_selected_recipe_keeps_editor_and_routes_commit_to_selected_row(self):
        self.service.mode = 4
        self.service.addRow("conditions")
        self.service.duplicateRow("conditions", 0)
        self.service.setRowValue("conditions", 1, "raw_vtg_stop", 3.)
        self.service.selectRow("conditions", 0)
        self.load()
        original = self.find("field_raw_vtg_stop")
        self.service.selectRow("conditions", 1)
        self.app.processEvents()
        self.assertEqual(self.find("field_raw_vtg_stop"), original)
        self.assertEqual(original.property("text"), "3")
        original.forceActiveFocus()
        original.setProperty("text", "4.5")
        QtTest.QTest.keyClick(self.window, QtCore.Qt.Key.Key_Return)
        self.app.processEvents()
        rows = self.service._values[4]["conditions"]
        self.assertEqual(rows[1]["raw_vtg_stop"], 4.5)
        self.assertNotEqual(rows[0]["raw_vtg_stop"], 4.5)
        self.assertEqual(self.find("field_raw_vtg_stop"), original)

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
        self.assertEqual(self.find("measurementEditing_bias_conditions").property("text"), "Editing condition 2 of 2")

        duplicate = self.find("measurementDuplicate_bias_conditions")
        duplicate.forceActiveFocus()
        QtTest.QTest.keyClick(self.window, QtCore.Qt.Key.Key_Space)
        self.app.processEvents()
        self.assertEqual(len(self.service._values[3]["bias_conditions"]), 3)
        self.assertEqual(self.find("measurementEditing_bias_conditions").property("text"), "Editing condition 3 of 3")
        remove = self.find("measurementRemove_bias_conditions")
        remove.forceActiveFocus()
        QtTest.QTest.keyClick(self.window, QtCore.Qt.Key.Key_Space)
        self.app.processEvents()
        self.assertEqual(len(self.service._values[3]["bias_conditions"]), 2)
        self.assertEqual(self.find("measurementEditing_bias_conditions").property("text"), "Editing condition 2 of 2")

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
        ramp.setProperty("text", "0.125")
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

    def test_recipe_partition_retains_native_tab_order_at_panel_width(self):
        self.service.mode = 3
        self.load(width=366)
        QtTest.QTest.qWait(30)
        actions = self.find("measurementRemove_bias_conditions")
        editing = self.find("measurementEditing_bias_conditions")
        field = self.find("field_vtg")
        self.assertGreater(editing.mapToScene(QtCore.QPointF()).y(),
                           actions.mapToScene(QtCore.QPointF(0, actions.height())).y())
        self.assertGreaterEqual(field.mapToScene(QtCore.QPointF()).x(), 8)
        self.assertLessEqual(field.mapToScene(QtCore.QPointF(field.width(), 0)).x(), 366)
        actions.forceActiveFocus()
        QtTest.QTest.keyClick(self.window, QtCore.Qt.Key.Key_Tab)
        self.assertEqual(self.window.activeFocusItem(), self.find("field_enabled"))
        additional = max(i for i, section in enumerate(self.service.uiSections) if not section["advanced"]) + 1
        heading = self.find(f"measurementAdditionalSettings_{additional}")
        self.assertTrue(heading.isVisible())
        section = self.find("measurement_section_Advanced ramps and conversion")
        self.assertLess(heading.mapToScene(QtCore.QPointF()).y(), section.mapToScene(QtCore.QPointF()).y())

    def test_advanced_classification_change_keeps_gate_conversion_draft_and_focus(self):
        self.service.mode = 2
        self.load(width=366)
        header = self.find("measurement_section_Gate conversion")
        header.forceActiveFocus()
        QtTest.QTest.keyClick(self.window, QtCore.Qt.Key.Key_Space)
        self.app.processEvents()
        ratio = self.find("field_ratio")
        ratio.forceActiveFocus()
        ratio.setProperty("text", "1.2345")
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
        self.assertEqual(self.find("measurementEditing_conditions").property("text"), "Editing condition 2 of 2")

    def test_generic_form_preserves_active_draft_across_refresh(self):
        self.load("FieldForm")
        original = self.find("field_vds_stop")
        original.forceActiveFocus()
        original.setProperty("text", "0.654")
        self.service.setValue("n_sample", 3)
        self.app.processEvents()
        self.assertEqual(self.find("field_vds_stop"), original)
        self.assertEqual(original.property("text"), "0.654")
        self.assertTrue(original.hasActiveFocus())
