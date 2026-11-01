import os
import unittest
from unittest.mock import patch

if os.environ.get("TRANSPORT_OFFLINE_VERIFICATION") != "1":
    raise unittest.SkipTest("Run through verify_offline.py")

from PySide6.QtCore import QCoreApplication
from PySide6.QtTest import QSignalSpy
from app.models import SaveRoot
from app.quick.measurements import MeasurementService
from tests.test_quick_measurements import FakeManager


class RecipeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QCoreApplication.instance() or QCoreApplication([])

    def setUp(self):
        self.manager = FakeManager()
        self.service = MeasurementService(self.manager, SaveRoot())

    def setup_mode(self, mode, key):
        self.service.mode = mode
        return self.service.rowModel(key)

    @staticmethod
    def uid(model, row):
        return model.data(model.index(row, 0), model.UidRole)

    @staticmethod
    def cells(model, row):
        return {cell.spec["key"]: dict(cell.spec) for cell in model.data(model.index(row, 0), model.CellsRole)}

    def test_incremental_edits_and_structural_changes_keep_ids(self):
        model = self.setup_mode(3, "bias_conditions")
        reset = QSignalSpy(model.modelReset)
        inserted = QSignalSpy(model.rowsInserted)
        removed = QSignalSpy(model.rowsRemoved)
        changed = QSignalSpy(model.dataChanged)
        first = self.uid(model, 0)
        states = model.data(model.index(0, 0), model.CellsRole)
        vtg = next(state for state in states if state.spec["key"] == "vtg")
        cell_changed = QSignalSpy(vtg.changed)
        self.assertTrue(model.commit(first, "vtg", "1.25"))
        self.assertEqual(self.service._values[3]["bias_conditions"][0]["vtg"], 1.25)
        self.assertEqual(changed.count(), 0)
        self.assertEqual(cell_changed.count(), 1)
        self.assertIs(vtg, next(state for state in model.data(model.index(0, 0), model.CellsRole) if state.spec["key"] == "vtg"))
        self.service.addRow("bias_conditions")
        second = self.uid(model, 1)
        self.service.duplicateRow("bias_conditions", 0)
        self.assertEqual(self.uid(model, 0), first)
        self.assertEqual(self.uid(model, 2), second)
        self.service.removeRow("bias_conditions", 1)
        self.assertEqual(self.uid(model, 1), second)
        self.assertEqual((reset.count(), inserted.count(), removed.count()), (0, 2, 1))
        self.assertIs(model, self.service.rowModel("bias_conditions"))
        self.assertIsNone(self.service.rowModel("conditions"))

    def test_draft_errors_reject_structure_and_never_move_to_other_row(self):
        model = self.setup_mode(3, "bias_conditions")
        first = self.uid(model, 0)
        model.rememberDraft(first, "vtg", "bad")
        self.assertFalse(model.flush())
        self.assertIn("error", self.cells(model, 0)["vtg"])
        self.service.addRow("bias_conditions")
        self.assertEqual(model.count, 1)
        model.rememberDraft(first, "vtg", "2")
        self.assertTrue(model.flush())
        self.service.addRow("bias_conditions")
        second = self.uid(model, 1)
        model.rememberDraft(second, "vbg", "3")
        self.service.removeRow("bias_conditions", 0)
        self.assertEqual(self.uid(model, 0), second)
        self.assertEqual(self.service._values[3]["bias_conditions"][0]["vbg"], 3)
        self.assertFalse(model.commit(first, "vtg", "4"))

    def test_mode_switch_and_start_flush_pending_drafts(self):
        model = self.setup_mode(3, "bias_conditions")
        uid = self.uid(model, 0)
        model.rememberDraft(uid, "vtg", "invalid")
        self.service.mode = 4
        self.assertEqual(self.service.mode, 3)
        with patch.object(self.service, "_begin_preflight") as preflight:
            self.service.start()
            preflight.assert_not_called()
        self.assertEqual(model.cells(0)[1].get("draft"), "invalid")
        model.rememberDraft(uid, "vtg", "1.5")
        self.service.mode = 4
        self.assertEqual(self.service.mode, 4)
        self.assertEqual(self.service._values[3]["bias_conditions"][0]["vtg"], 1.5)

    def test_row_updates_do_not_refresh_whole_sections_unless_gate_details_open(self):
        model = self.setup_mode(3, "bias_conditions")
        sections = QSignalSpy(self.service.uiSectionsChanged)
        fields = QSignalSpy(self.service.fieldsChanged)
        output = QSignalSpy(self.service.outputPreviewChanged)
        data = QSignalSpy(model.dataChanged)
        uid = self.uid(model, 0)
        states = model.data(model.index(0, 0), model.CellsRole)
        vtg = next(state for state in states if state.spec["key"] == "vtg")
        changed = QSignalSpy(vtg.changed)
        self.assertTrue(model.commit(uid, "vtg", "2"))
        self.assertEqual(data.count(), 0)
        self.assertEqual(changed.count(), 1)
        model.select(uid)
        self.service.addRow("bias_conditions")
        self.service.removeRow("bias_conditions", 1)
        self.assertEqual(sections.count(), 0)
        self.assertGreaterEqual(fields.count(), 4)
        self.assertGreaterEqual(output.count(), 4)
        self.assertEqual(model.selectedIndex, 0)
        self.service.setValue("wl_start", 600)
        self.assertEqual(sections.count(), 1)

        self.service.mode = 4
        sections = QSignalSpy(self.service.uiSectionsChanged)
        model = self.service.rowModel("conditions")
        self.service.addRow("conditions")  # Empty to populated changes section routing.
        self.assertEqual(sections.count(), 1)
        self.service.addRow("conditions")
        model.select(self.uid(model, 0))
        self.assertTrue(model.commit(self.uid(model, 0), "n_points", "61"))
        self.assertEqual(sections.count(), 1)
        self.service.setRecipeDetailsVisible(True)
        self.assertEqual(sections.count(), 2)
        model.select(self.uid(model, 1))
        self.assertTrue(model.commit(self.uid(model, 1), "n_points", "62"))
        self.assertEqual(sections.count(), 4)
        self.assertEqual(model.selectedIndex, 1)
        self.service.setRecipeDetailsVisible(False)
        self.service.removeRow("conditions", 1)
        self.assertEqual(sections.count(), 4)
        self.service.removeRow("conditions", 0)
        self.assertEqual(sections.count(), 5)

    def test_external_replacement_resets_only_that_model(self):
        model = self.setup_mode(3, "bias_conditions")
        reset = QSignalSpy(model.modelReset)
        before = self.uid(model, 0)
        rows = [dict(self.service._values[3]["bias_conditions"][0], vtg=7)]
        self.service.setValue("bias_conditions", rows)
        self.assertEqual(reset.count(), 1)
        self.assertNotEqual(before, self.uid(model, 0))
        self.assertEqual(self.cells(model, 0)["vtg"]["value"], 7)

    def test_invalid_photo_draft_blocks_disabling_vds_until_corrected(self):
        model = self.setup_mode(3, "bias_conditions")
        self.service.setValue("use_vds", True)
        uid = self.uid(model, 0)
        original = self.service._values[3]["bias_conditions"][0]["vds"]
        model.rememberDraft(uid, "vds", "invalid")
        sections = QSignalSpy(self.service.uiSectionsChanged)
        self.service.setValue("use_vds", False)
        self.assertTrue(self.service._values[3]["use_vds"])
        self.assertEqual(self.service._values[3]["bias_conditions"][0]["vds"], original)
        self.assertGreater(sections.count(), 0)
        self.assertFalse(self.service.canStart)
        model.rememberDraft(uid, "vds", str(original))
        self.service.setValue("use_vds", False)
        self.assertFalse(self.service._values[3]["use_vds"])
        self.assertTrue(self.service.canStart)
        self.assertTrue(self.cells(model, 0)["vds"]["readOnly"])

    def test_invalid_gate_draft_blocks_hiding_or_locking_until_corrected(self):
        model = self.setup_mode(4, "conditions")
        self.service.addRow("conditions")
        uid = self.uid(model, 0)
        states = model.data(model.index(0, 0), model.CellsRole)
        active = next(state for state in states if state.spec["key"] == "raw_vtg_active")
        mode = next(state for state in states if state.spec["key"] == "mode")
        active_changed, mode_changed = QSignalSpy(active.changed), QSignalSpy(mode.changed)
        original = self.service._values[4]["conditions"][0]["raw_vtg_stop"]
        model.rememberDraft(uid, "raw_vtg_stop", "invalid")
        self.assertFalse(model.commit(uid, "raw_vtg_active", False))
        self.assertFalse(model.commit(uid, "mode", "Derived"))
        self.assertTrue(self.service._values[4]["conditions"][0]["raw_vtg_active"])
        self.assertEqual(self.service._values[4]["conditions"][0]["mode"], "Raw")
        self.assertGreater(active_changed.count(), 0)
        self.assertGreater(mode_changed.count(), 0)
        model.rememberDraft(uid, "raw_vtg_stop", str(original))
        self.assertTrue(model.commit(uid, "raw_vtg_active", False))
        self.assertFalse(self.service._values[4]["conditions"][0]["raw_vtg_active"])
        self.assertTrue(model.commit(uid, "mode", "Derived"))
        self.assertEqual(self.service._values[4]["conditions"][0]["mode"], "Derived")
        self.assertTrue(model.flush())

    def test_cell_state_shape_drafts_and_global_dependencies(self):
        model = self.setup_mode(5, "conditions")
        uid = self.uid(model, 0)
        states = model.data(model.index(0, 0), model.CellsRole)
        by_key = {state.spec["key"]: state for state in states}
        name = by_key["name"]
        doping, vtg = by_key["doping"], by_key["vtg"]
        untouched, edited, derived = QSignalSpy(name.changed), QSignalSpy(doping.changed), QSignalSpy(vtg.changed)
        roles = QSignalSpy(model.dataChanged)
        model.rememberDraft(uid, "doping", "1.5")
        self.assertEqual(doping.spec["draft"], "1.5")
        self.assertEqual(edited.count(), 0)
        self.assertTrue(model.flush())
        self.assertEqual(edited.count(), 1)
        self.assertGreater(derived.count(), 0)
        self.assertEqual(untouched.count(), 0)
        self.assertEqual(roles.count(), 0)
        self.assertNotIn("draft", doping.spec)
        self.assertTrue(model.commit(uid, "vds_source", "NI DAQ AO"))
        self.assertEqual(roles.count(), 1)
        new_states = model.data(model.index(0, 0), model.CellsRole)
        self.assertIs(name, next(state for state in new_states if state.spec["key"] == "name"))
        self.assertIn("ao_channel", [state.spec["key"] for state in new_states])
        vbg = by_key["vbg"]
        before = vbg.spec["value"]
        self.service.setValue("ratio", 2.5)
        self.assertNotEqual(vbg.spec["value"], before)

    def test_first_gate_add_captures_current_recipe_and_names_are_unique(self):
        model = self.setup_mode(4, "conditions")
        self.service.setValue("n_points", 77)
        self.service.setValue("raw_vtg_start", 2.5)
        self.service.addRow("conditions")
        row = self.service._values[4]["conditions"][0]
        self.assertEqual((row["n_points"], row["raw_vtg_start"], row["name"]), (77, 2.5, "Condition 1"))
        self.service.addRow("conditions")
        self.assertEqual(self.service._values[4]["conditions"][1]["name"], "Condition 2")
        self.assertEqual(model.count, 2)

    def test_transport_readonly_derived_values_and_shared_ratio(self):
        model = self.setup_mode(5, "conditions")
        uid = self.uid(model, 0)
        self.service.setValue("ratio", 2.5)
        cells = self.cells(model, 0)
        self.assertTrue(cells["vtg"]["readOnly"])
        self.assertTrue(cells["vbg"]["readOnly"])
        self.assertFalse(model.commit(uid, "vtg", "20"))
        self.assertTrue(model.commit(uid, "doping", "1"))
        self.assertEqual(self.service._values[5]["conditions"][0]["doping"], 1)
        self.assertNotEqual(self.cells(model, 0)["vtg"]["value"], cells["vtg"]["value"])

    def test_locks_limit_and_model_cache_per_mode(self):
        model = self.setup_mode(3, "bias_conditions")
        uid = self.uid(model, 0)
        self.service._batch_claimed = True
        self.assertFalse(model.commit(uid, "vtg", "1"))
        self.service.addRow("bias_conditions")
        self.assertEqual(model.count, 1)
        self.service._batch_claimed = False
        for _ in range(99): self.service.addRow("bias_conditions")
        self.assertEqual(model.count, 100)
        self.service.addRow("bias_conditions")
        self.assertEqual(model.count, 100)
        self.service.mode = 4
        self.assertFalse(model.commit(uid, "vtg", "1"))
        self.service.mode = 3
        self.assertIs(model, self.service.rowModel("bias_conditions"))
        self.assertEqual(self.uid(model, 0), uid)
