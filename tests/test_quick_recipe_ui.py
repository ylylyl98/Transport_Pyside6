"""Hardware-free interaction tests for virtualized inline recipe editing."""
import os
import unittest
from pathlib import Path
from unittest.mock import patch

if os.environ.get("TRANSPORT_OFFLINE_VERIFICATION") != "1":
    raise unittest.SkipTest("Run through verify_offline.py")

from PySide6 import QtCore, QtQml, QtTest, QtWidgets
from PySide6.QtQuickControls2 import QQuickStyle
from app.models import SaveRoot
from app.quick.measurements import MeasurementService
from tests.test_quick_measurements import FakeManager


class QuickRecipeUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
        QQuickStyle.setStyle("Fusion")

    def setUp(self):
        self.manager = FakeManager()
        self.service = MeasurementService(self.manager, SaveRoot())
        self.engine = QtQml.QQmlApplicationEngine()
        self.warnings = []
        self.engine.warnings.connect(lambda items: self.warnings.extend(str(i) for i in items))
        self.engine.rootContext().setContextProperty("service", self.service)

    def load(self, mode=3, key="bias_conditions", width=520):
        self.service.mode = mode
        self.model = self.service.rowModel(key)
        self.key = key
        source = ("import QtQuick\nimport QtQuick.Controls\nApplicationWindow {"
                  f"width: {width}; height: 700; visible: true; "
                  f'RecipeTable {{ objectName: "table"; anchors.fill: parent; measurement: service; rowKey: "{key}" }}' + "}")
        self.engine.loadData(source.encode(), QtCore.QUrl.fromLocalFile(str(
            Path(__file__).resolve().parents[1] / "app/quick/qml/RecipeHarness.qml")))
        self.assertTrue(self.engine.rootObjects(), self.warnings)
        self.window = self.engine.rootObjects()[0]
        self.window.requestActivate()
        QtTest.QTest.qWait(100)

    def all_items(self):
        def descendants(item):
            yield item
            for child in item.childItems():
                yield from descendants(child)
        return list(descendants(self.window.contentItem()))

    def find(self, name):
        found = self.window.findChild(QtCore.QObject, name)
        if found is None:
            found = next((item for item in self.all_items() if item.objectName() == name), None)
        self.assertIsNotNone(found, name)
        return found

    def uid(self, index=0):
        return self.model.data(self.model.index(index, 0), self.model.UidRole)

    def cell(self, uid, name):
        return self.find(f"recipeCell_{self.key}_{uid}_{name}")

    def click(self, item):
        if isinstance(item, str): item = self.find(item)
        pos = item.mapToScene(QtCore.QPointF(item.width()/2, item.height()/2)).toPoint()
        QtTest.QTest.mouseClick(self.window, QtCore.Qt.LeftButton, QtCore.Qt.NoModifier, pos)
        QtTest.QTest.qWait(50)

    def edit(self, item, text, commit=True):
        item.forceActiveFocus()
        QtTest.QTest.keyClick(self.window, QtCore.Qt.Key_A, QtCore.Qt.ControlModifier)
        for character in text:
            QtTest.QTest.keyClick(self.window, ord(character.upper()))
        if commit: QtTest.QTest.keyClick(self.window, QtCore.Qt.Key_Return)
        QtTest.QTest.qWait(50)

    def tearDown(self):
        if self.engine.rootObjects(): self.engine.rootObjects()[0].hide()
        self.engine.deleteLater()
        self.app.sendPostedEvents(None, QtCore.QEvent.DeferredDelete)
        self.service.deleteLater()
        self.app.sendPostedEvents(None, QtCore.QEvent.DeferredDelete)
        self.assertEqual(self.warnings, [])

    def test_numeric_display_focus_does_not_round_recipe_value(self):
        self.service.mode = 3
        self.service.setRowValue("bias_conditions", 0, "vtg", .123456789123)
        self.load()
        uid = self.uid()
        cell = self.cell(uid, "vtg")
        self.assertEqual(cell.property("text"), "0.123457")
        cell.forceActiveFocus()
        self.app.processEvents()
        self.assertEqual(cell.property("text"), "0.123456789123")
        self.cell(uid, "vbg").forceActiveFocus()
        self.app.processEvents()
        self.assertEqual(self.service._values[3][self.key][0]["vtg"], .123456789123)
        self.edit(cell, "2.25")
        QtTest.QTest.keyClick(self.window, QtCore.Qt.Key_Tab)
        self.assertEqual(self.service._values[3][self.key][0]["vtg"], 2.25)

    def test_inline_checkbox_numeric_and_disabled_vds_retention(self):
        self.service.mode = 3
        self.service.setValue("use_vds", True)
        self.load()
        uid = self.uid()
        self.edit(self.cell(uid, "vds"), "1.25")
        self.edit(self.cell(uid, "vtg"), "2.5")
        self.click(self.cell(uid, "enabled"))
        row = self.service._values[3][self.key][0]
        self.assertEqual((row["vds"], row["vtg"], row["enabled"]), (1.25, 2.5, False))
        self.service.setValue("use_vds", False)
        self.app.processEvents()
        self.assertTrue(self.cell(uid, "vds").property("readOnly"))
        self.assertEqual(row["vds"], 1.25)
        self.service.setValue("use_vds", True)
        self.app.processEvents()
        self.assertFalse(self.cell(uid, "vds").property("readOnly"))
        self.assertEqual(row["vds"], 1.25)

    def test_add_duplicate_remove_flush_valid_and_block_invalid_draft(self):
        self.load()
        uid = self.uid()
        self.edit(self.cell(uid, "vtg"), "2.25", commit=False)
        self.click("measurementAdd_bias_conditions")
        self.assertEqual(self.model.count, 2)
        self.assertEqual(self.service._values[3][self.key][0]["vtg"], 2.25)
        self.edit(self.cell(self.uid(1), "vbg"), "bad", commit=False)
        self.click("measurementDuplicate_bias_conditions")
        self.assertEqual(self.model.count, 2)
        with patch.object(self.service, "_begin_preflight") as preflight:
            self.service.start()
            preflight.assert_not_called()
        self.edit(self.cell(self.uid(1), "vbg"), "3", commit=True)
        self.click("measurementDuplicate_bias_conditions")
        self.assertEqual(self.model.count, 3)
        self.assertEqual(self.service._values[3][self.key][2]["vbg"], 3)
        self.click("measurementRemove_bias_conditions")
        self.assertEqual(self.model.count, 2)

    def test_draft_survives_unrelated_changes_and_telemetry(self):
        self.load()
        uid = self.uid()
        editor = self.cell(uid, "vtg")
        self.edit(editor, "4.125", commit=False)
        self.service.setRowValue(self.key, 0, "vbg", 7)
        self.manager.status_changed.emit("g1", "connected", "telemetry")
        self.app.processEvents()
        self.assertIs(self.cell(uid, "vtg"), editor)
        self.assertEqual(editor.property("text"), "4.125")
        self.assertTrue(editor.hasActiveFocus())
        QtTest.QTest.keyClick(self.window, QtCore.Qt.Key_Return)
        self.assertEqual(self.service._values[3][self.key][0]["vtg"], 4.125)

    def test_enabled_checkbox_shares_condition_heading_instead_of_field_grid(self):
        self.load()
        uid = self.uid()
        enabled = self.cell(uid, "enabled")
        heading = self.find("measurementSelect_bias_conditions_0")
        ey = enabled.mapToScene(QtCore.QPointF(0, enabled.height()/2)).y()
        hy = heading.mapToScene(QtCore.QPointF(0, heading.height()/2)).y()
        self.assertAlmostEqual(ey, hy, delta=3)
        self.assertLess(enabled.width(), 100)
        self.assertEqual(sum(item.objectName() == enabled.objectName() for item in self.all_items()), 1)
        self.assertLess(ey, self.cell(uid, "vtg").mapToScene(QtCore.QPointF(0, 0)).y())

    def test_gate_raw_switch_and_start_stop_align_without_horizontal_scroll(self):
        self.service.mode = 4
        self.service.addRow("conditions")
        self.load(4, "conditions", width=520)
        uid = self.uid()
        for width in (520, 420):
            self.window.setWidth(width)
            QtTest.QTest.qWait(100)
            name = self.cell(uid, "name").mapToScene(QtCore.QPointF(0, 0)).y()
            for axis in ("vtg", "vbg", "vds"):
                switch = self.cell(uid, f"raw_{axis}_active")
                start = self.cell(uid, f"raw_{axis}_start")
                stop = self.cell(uid, f"raw_{axis}_stop")
                sw = switch.mapToScene(QtCore.QPointF(0, switch.height()/2))
                a = start.mapToScene(QtCore.QPointF(0, 0))
                b = stop.mapToScene(QtCore.QPointF(0, 0))
                self.assertGreater(a.y(), name, axis)
                self.assertAlmostEqual(a.y(), b.y(), delta=1, msg=axis)
                self.assertGreaterEqual(sw.y(), a.y() - 2, axis)
                self.assertLessEqual(sw.y(), a.y() + start.height() + 2, axis)
                for item in (switch, start, stop):
                    x = item.mapToScene(QtCore.QPointF(0, 0)).x()
                    self.assertGreaterEqual(x, 0)
                    self.assertLessEqual(x + item.width(), width + 1)
            view = self.find("measurementRows_conditions")
            self.assertLessEqual(view.property("contentWidth"), view.width() + 1)
        self.window.setWidth(1100)
        self.click("measurementExpand_conditions")
        QtTest.QTest.qWait(200)
        view = self.find("measurementRows_conditions")
        self.assertLessEqual(view.property("contentWidth"), view.width() + 1)
        for axis in ("vtg", "vbg", "vds"):
            start = self.cell(uid, f"raw_{axis}_start")
            stop = self.cell(uid, f"raw_{axis}_stop")
            self.assertAlmostEqual(start.mapToScene(QtCore.QPointF(0, 0)).y(), stop.mapToScene(QtCore.QPointF(0, 0)).y(), delta=1)

    def test_transport_raw_gate_edit_switch_and_invalid_draft(self):
        from app.gate_transform import derived_to_gates
        self.load(5, "conditions")
        uid = self.uid()
        self.service.setValue("transport_coordinates", "Raw")
        self.app.processEvents()
        self.assertFalse(self.cell(uid, "vtg").property("readOnly"))
        self.edit(self.cell(uid, "vtg"), "1.25")
        self.edit(self.cell(uid, "vbg"), "2.5")
        row = self.service._values[5]["conditions"][0]
        ratio = self.service._values[5]["ratio"]
        target = self.service._values[5]["ratio_target"]
        before = derived_to_gates(row["doping"], row["efield"], ratio, target)
        self.edit(self.cell(uid, "vtg"), "bad", commit=False)
        self.service.setValue("transport_coordinates", "Derived")
        self.assertEqual(self.service.transportCoordinates, "Raw")
        self.assertFalse(self.service.canStart)
        self.edit(self.cell(uid, "vtg"), "1.25")
        self.service.setValue("transport_coordinates", "Derived")
        self.app.processEvents()
        self.assertEqual(self.service.transportCoordinates, "Derived")
        self.assertTrue(self.cell(uid, "vtg").property("readOnly"))
        self.assertTrue(self.cell(uid, "vbg").property("readOnly"))
        after = derived_to_gates(row["doping"], row["efield"], ratio, target)
        self.assertEqual(before, after)

    def test_sweep_derived_readonly_and_active_lock(self):
        self.load(5, "conditions")
        uid = self.uid()
        self.assertTrue(self.cell(uid, "vtg").property("readOnly"))
        self.assertTrue(self.cell(uid, "vbg").property("readOnly"))
        self.service._batch_claimed = True
        self.service.changed.emit()
        self.app.processEvents()
        self.assertTrue(self.cell(uid, "doping").property("readOnly"))
        for action in ("Add", "Duplicate", "Remove"):
            self.assertFalse(self.find(f"measurement{action}_conditions").property("enabled"))
        self.assertFalse(self.cell(uid, "enabled").property("enabled"))
        self.service._batch_claimed = False
        self.service.changed.emit()

    def test_map_region_six_values_are_inline_without_an_enable_switch(self):
        self.service.mode = 2
        self.service.addRow("regions")
        self.load(2, "regions", width=520)
        uid = self.uid()
        self.assertEqual({cell["key"] for cell in self.model.cells(0)},
                         {f"{axis}_{part}" for axis in ("vtg", "vbg") for part in ("start", "stop", "step")})
        self.edit(self.cell(uid, "vbg_step"), "0.025")
        self.assertEqual(self.service._values[2]["regions"][0]["vbg_step"], 0.025)
        self.assertFalse(any(item.objectName() == f"recipeCell_regions_{uid}_enabled" for item in self.all_items()))

    def test_wrapping_no_horizontal_scroll(self):
        self.load(width=520)
        uid = self.uid()
        for width in (520, 366):
            self.window.setWidth(width)
            QtTest.QTest.qWait(100)
            fields = [self.cell(uid, key) for key in ("vtg", "vbg", "vds", "settle_s")]
            positions = [field.mapToScene(QtCore.QPointF(0, 0)) for field in fields]
            for field, pos in zip(fields, positions):
                self.assertGreaterEqual(pos.x(), 0)
                self.assertLessEqual(pos.x() + field.width(), width + 1)
            if width == 366:
                self.assertGreater(len({round(p.y()) for p in positions}), 1)
            view = self.find("measurementRows_bias_conditions")
            self.assertLessEqual(view.property("contentWidth"), view.width() + 1)

    def test_virtualized_many_rows_and_uid_after_scroll(self):
        self.service.mode = 3
        for _ in range(99): self.service.addRow("bias_conditions")
        self.load()
        view = self.find("measurementRows_bias_conditions")
        self.assertLess(len([x for x in self.all_items() if x.objectName().startswith("measurementRow_bias_conditions_")]), 20)
        first = self.uid(0)
        self.edit(self.cell(first, "vtg"), "4.125", commit=False)
        last = self.uid(99)
        self.model.select(last)
        QtTest.QTest.qWait(200)
        self.assertTrue(self.model.flush())
        self.assertEqual(self.service._values[3][self.key][0]["vtg"], 4.125)
        self.edit(self.cell(last, "vtg"), "9.75")
        self.assertEqual(self.service._values[3][self.key][99]["vtg"], 9.75)
        self.assertEqual(self.uid(99), last)
        self.assertLessEqual(view.property("contentWidth"), view.width() + 1)
        self.model.select(first)
        QtTest.QTest.qWait(100)
        self.edit(self.cell(first, "vtg"), "bad", commit=False)
        self.model.select(last)
        QtTest.QTest.qWait(100)
        self.assertFalse(self.model.flush())
        self.assertEqual(self.service._values[3][self.key][99]["vtg"], 9.75)
        self.model.select(first)
        QtTest.QTest.qWait(100)
        self.assertEqual(self.cell(first, "vtg").property("text").lower(), "bad")
        self.edit(self.cell(first, "vtg"), "0")
        self.assertTrue(self.service.canStart)

    def test_gate_transport_switch_and_reset_release_pooled_cells_safely(self):
        from copy import deepcopy
        self.service.mode = 4
        for _ in range(8):
            self.service.addRow("conditions")
        self.service.mode = 5
        for _ in range(8):
            self.service.addRow("conditions")
        self.load(4, "conditions", width=520)
        view = self.find("measurementRows_conditions")
        self.model.select(self.uid(7))
        QtTest.QTest.qWait(100)
        view.setProperty("contentY", 0.)
        QtTest.QTest.qWait(100)
        for mode in (5, 4, 5, 4, 5):
            self.service.mode = mode
            self.model = self.service.rowModel("conditions")
            rows = deepcopy(self.service._values[mode]["conditions"])
            self.service.setValue("conditions", rows)
            self.app.sendPostedEvents(None, QtCore.QEvent.DeferredDelete)
            QtTest.QTest.qWait(80)
            self.model.select(self.uid(0))
            QtTest.QTest.qWait(50)
            uid = self.uid(0)
            enabled = self.cell(uid, "enabled")
            initial = self.service._values[mode]["conditions"][0]["enabled"]
            self.click(enabled)
            self.assertEqual(self.service._values[mode]["conditions"][0]["enabled"], not initial)
            field = "raw_vtg_start" if mode == 4 else "doping"
            self.edit(self.cell(uid, field), "0.75")
            self.assertEqual(self.service._values[mode]["conditions"][0][field], .75)
        self.assertEqual(self.warnings, [])

    def test_tall_gate_record_does_not_jump_when_focusing_a_lower_cell(self):
        self.service.mode = 4
        self.service.addRow("conditions")
        self.service.addRow("conditions")
        self.load(4, "conditions", width=366)
        self.window.setHeight(340)
        QtTest.QTest.qWait(100)
        view = self.find("measurementRows_conditions")
        view.setProperty("contentY", 100.)
        QtTest.QTest.qWait(50)
        before = view.property("contentY")
        self.cell(self.uid(0), "raw_vbg_start").forceActiveFocus()
        QtTest.QTest.qWait(50)
        self.assertAlmostEqual(view.property("contentY"), before, delta=1)

    def test_return_to_accepted_value_clears_an_external_cell_error(self):
        self.load()
        uid = self.uid()
        self.service.setRowValue(self.key, 0, "vtg", "bad")
        self.app.processEvents()
        self.assertFalse(self.service.canStart)
        self.edit(self.cell(uid, "vtg"), "0")
        self.assertTrue(self.service.canStart)

    def test_gate_checkbox_cannot_hide_an_invalid_stop_draft(self):
        self.service.mode = 4
        self.service.addRow("conditions")
        self.load(4, "conditions", width=700)
        uid = self.uid()
        stop = self.cell(uid, "raw_vtg_stop")
        self.edit(stop, "bad", commit=False)
        self.click(self.cell(uid, "raw_vtg_active"))
        self.assertTrue(self.service._values[4][self.key][0]["raw_vtg_active"])
        self.assertTrue(self.cell(uid, "raw_vtg_active").property("checked"))
        self.assertFalse(stop.property("readOnly"))
        self.edit(stop, "1")
        self.click(self.cell(uid, "raw_vtg_active"))
        self.assertFalse(self.service._values[4][self.key][0]["raw_vtg_active"])
        self.assertTrue(stop.property("readOnly"))

    def test_enlarge_reuses_body_and_commits_on_close(self):
        self.load()
        uid = self.uid()
        editor = self.cell(uid, "vtg")
        self.edit(editor, "5.25", commit=False)
        self.click("measurementExpand_bias_conditions")
        QtTest.QTest.qWait(200)
        self.assertIs(self.cell(uid, "vtg"), editor)
        self.assertEqual(self.service._values[3][self.key][0]["vtg"], 5.25)
        self.edit(editor, "6.25", commit=False)
        QtTest.QTest.keyClick(self.window, QtCore.Qt.Key_Escape)
        QtTest.QTest.qWait(200)
        self.assertEqual(self.service._values[3][self.key][0]["vtg"], 6.25)
