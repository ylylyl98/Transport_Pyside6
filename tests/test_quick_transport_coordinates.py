"""Offline regressions for B-field transport physical gate coordinate editing."""
from copy import deepcopy
import os
from pathlib import Path
import tempfile
import unittest

if os.environ.get("TRANSPORT_OFFLINE_VERIFICATION") != "1":
    raise unittest.SkipTest("Run through verify_offline.py")

from PySide6.QtCore import QCoreApplication, QSettings
from app.engine.bfield_transport_sweep import TransportSweepPlan
from app.gate_transform import derived_to_gates, gates_to_derived
from app.models import SaveRoot
from app.quick.measurements import MeasurementService
from tests.test_quick_measurements import FakeManager


class TransportCoordinateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QCoreApplication.instance() or QCoreApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.settings = QSettings(str(Path(self.temp.name) / "settings.ini"), QSettings.IniFormat)
        self.manager = FakeManager()
        self.service = MeasurementService(self.manager, SaveRoot(base=self.temp.name), settings=self.settings)
        self.service.mode = 5
        self.model = self.service.rowModel("conditions")

    def uid(self, row=0):
        return self.model.data(self.model.index(row, 0), self.model.UidRole)

    def cells(self, row=0):
        return {cell.spec["key"]: dict(cell.spec)
                for cell in self.model.data(self.model.index(row, 0), self.model.CellsRole)}

    def gates(self, row):
        values = self.service._values[5]
        condition = values["conditions"][row]
        return derived_to_gates(condition["doping"], condition["efield"], values["ratio"], values["ratio_target"])

    def test_session_coordinate_mode_and_derived_read_only(self):
        self.assertEqual(self.service.transportCoordinates, "Derived")
        self.assertTrue(self.cells()["vtg"]["readOnly"])
        before = deepcopy(self.service._values[5])
        self.assertFalse(self.model.commit(self.uid(), "vtg", 1.2))
        self.service.setRowValue("conditions", 0, "vbg", 2.0)
        self.assertEqual(self.service._values[5], before)
        self.service.setValue("transport_coordinates", "Raw")
        self.assertEqual(self.service.transportCoordinates, "Raw")
        self.assertFalse(self.cells()["vtg"]["readOnly"])
        self.assertFalse(self.cells()["vbg"]["readOnly"])
        self.assertEqual(self.service._values[5], before)
        self.assertNotIn("transport_coordinates", self.service._values[5])
        self.service.mode = 3
        self.service.mode = 5
        self.assertEqual(self.service.transportCoordinates, "Raw")
        self.settings.sync()
        self.assertNotIn("transport_coordinates", str(self.settings.value("quick/measurements/bfield_transport", "")))
        self.assertNotIn("transportCoordinates", self.settings.allKeys())
        other = MeasurementService(self.manager, SaveRoot(base=self.temp.name), settings=self.settings)
        other.mode = 5
        self.assertEqual(other.transportCoordinates, "Derived")
        other.deleteLater()

    def test_raw_edits_hold_other_gate_and_match_execution_plan(self):
        self.service.setValue("transport_coordinates", "Raw")
        for ratio in (2., -2.):
            for target in ("Vtg", "Vbg"):
                with self.subTest(ratio=ratio, target=target):
                    self.service.setValue("ratio", ratio)
                    self.service.setValue("ratio_target", target)
                    self.assertTrue(self.model.commit(self.uid(), "vtg", 1.25))
                    vtg, vbg_before = self.gates(0)
                    self.assertAlmostEqual(vtg, 1.25)
                    self.service.setRowValue("conditions", 0, "vbg", -0.75)
                    vtg_after, vbg_after = self.gates(0)
                    self.assertAlmostEqual(vtg_after, vtg)
                    self.assertAlmostEqual(vbg_after, -0.75)
                    row = self.service._values[5]["conditions"][0]
                    expected = gates_to_derived(vtg_after, vbg_after, ratio, target)
                    self.assertAlmostEqual(row["doping"], expected[0])
                    self.assertAlmostEqual(row["efield"], expected[1])
                    plan = TransportSweepPlan.from_params(self.service.collect_params())
                    self.assertAlmostEqual(plan.conditions[0].vtg, vtg_after)
                    self.assertAlmostEqual(plan.conditions[0].vbg, vbg_after)

    def test_raw_ratio_or_target_change_preserves_every_gate_and_rejects_zero(self):
        self.service.setValue("transport_coordinates", "Raw")
        self.service.addRow("conditions")
        for index, vtg, vbg in ((0, 1., -0.5), (1, -2., .75)):
            self.assertTrue(self.model.commit(self.uid(index), "vtg", vtg))
            self.assertTrue(self.model.commit(self.uid(index), "vbg", vbg))
        expected = [self.gates(i) for i in range(2)]
        for ratio, target in ((2., "Vtg"), (-3., "Vbg"), (-.5, "Vtg")):
            self.service.setValue("ratio", ratio)
            self.service.setValue("ratio_target", target)
            for i, (vtg, vbg) in enumerate(expected):
                actual = self.gates(i)
                self.assertAlmostEqual(actual[0], vtg)
                self.assertAlmostEqual(actual[1], vbg)
                row = self.service._values[5]["conditions"][i]
                d, e = gates_to_derived(vtg, vbg, ratio, target)
                self.assertAlmostEqual(row["doping"], d)
                self.assertAlmostEqual(row["efield"], e)
        before = deepcopy(self.service._values[5])
        self.service.setValue("ratio", 0.)
        self.assertEqual(self.service._values[5], before)
        self.assertFalse(self.service.canStart)
        self.service.setValue("ratio", before["ratio"])
        self.assertTrue(self.service.canStart)

    def test_invalid_draft_blocks_coordinate_switch_and_active_locks(self):
        self.service.setValue("transport_coordinates", "Raw")
        uid = self.uid()
        self.model.rememberDraft(uid, "vtg", "invalid")
        before = deepcopy(self.service._values[5])
        self.service.setValue("transport_coordinates", "Derived")
        self.assertEqual(self.service.transportCoordinates, "Raw")
        self.assertEqual(self.service._values[5], before)
        self.assertFalse(self.service.canStart)
        self.model.rememberDraft(uid, "vtg", "1")
        self.service.setValue("transport_coordinates", "Derived")
        self.assertEqual(self.service.transportCoordinates, "Derived")
        self.assertAlmostEqual(self.gates(0)[0], 1.)
        self.assertTrue(self.service.canStart)
        self.service._batch_claimed = True
        before = deepcopy(self.service._values[5])
        self.service.setValue("transport_coordinates", "Raw")
        self.service.setValue("ratio", 3.)
        self.assertFalse(self.model.commit(uid, "doping", 5.))
        self.assertEqual(self.service.transportCoordinates, "Derived")
        self.assertEqual(self.service._values[5], before)
        self.service._batch_claimed = False
