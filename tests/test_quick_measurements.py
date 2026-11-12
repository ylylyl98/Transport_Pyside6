import os
if os.environ.get("TRANSPORT_OFFLINE_VERIFICATION") != "1":
    import unittest
    raise unittest.SkipTest("Run through verify_offline.py")

import tempfile
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from PySide6.QtCore import QCoreApplication, QObject, Signal
from app.models import Connections, SaveRoot
from app.quick.measurements import MeasurementService
from app.quick.parameters import MODES, defaults, schema, build_params
from app.workers.base import RunWorker


class FakeManager(QObject):
    status_changed = Signal(str, str, str)
    operation_changed = Signal(bool, str)

    def __init__(self):
        super().__init__()
        self.connections = Connections()
        self.sessions = {k: SimpleNamespace(identity="KEITHLEY INSTRUMENTS INC.,MODEL 2400,x,y", cached_source_voltage_range_v=20., refresh_source_voltage_range=lambda: 20.,
            get_max_output=lambda channel: 10.) for k in ("g1", "g2", "g3", "daq", "mono")}
        self.claimed = set()
        self.busy = False
        self.protected = True
        self.limit = 20.
        self.releases = []

    def get_session(self, name): return self.sessions.get(name)
    def is_connected(self, name): return self.sessions.get(name) is not None
    def is_voltage_source_mode(self, name): return True
    def protection_is_applied(self, name): return self.protected
    def applied_gate_voltage_limit(self, name): return self.limit
    def get_ao_items(self): return ["ao0", "ao1"]
    def is_busy(self): return self.busy
    def mark_in_use(self, names):
        blocked = self.claimed.intersection(names)
        if blocked or self.busy:
            return False, list(blocked) or ["hardware"]
        self.claimed.update(names)
        return True, []
    def release(self, names):
        self.releases.append(list(names))
        self.claimed.difference_update(names)


class FakeWorker(RunWorker):
    instances = []
    def __init__(self, params, save, connections, **kwargs):
        super().__init__()
        self.params = params
        self.kwargs = kwargs
        self.entered = threading.Event()
        self.terminal_sent = threading.Event()
        self.finish_cleanup = threading.Event()
        self.cleaned = False
        self.__class__.instances.append(self)

    def run(self):
        self.entered.set()
        self.point_data.emit({"x": 1., "Ids_DC": 2.})
        while not self._stop and not self.finish_cleanup.is_set():
            time.sleep(.001)
        if self._stop:
            self.stopped.emit("Stopped safely")
        else:
            self.finished.emit(self.params.output_csv_path)
        self.terminal_sent.set()
        self.finish_cleanup.wait(2.)
        self.cleaned = True


class FakeMagnet(QObject):
    transport_config_result = Signal(object)
    transport_restore_result = Signal(object)
    safe_move_result = Signal(object)
    transport_sweep_result = Signal(object)
    snapshot_updated = Signal(object)
    fault = Signal(str)
    operation_finished = Signal(str)
    def __init__(self):
        super().__init__()
        self.is_connected = True
        self.owner = ""
        self.calls = []
        self.latest_snapshot = SimpleNamespace(field_t=0., heater_on=False, monotonic_s=time.monotonic(),
            output_current_a=0., output_field_t=0., sweep_state="idle", magnet_voltage_v=0., voltage_limit_v=3.,
            status=SimpleNamespace(standby=True, sweep_active=False, quench=False, power_module_failure=False, raw=4))
    def acquire_exclusive(self, owner):
        if self.owner: return False
        self.owner = owner
        self.calls.append(("acquire", owner))
        return True
    def release_exclusive(self, owner):
        self.calls.append(("release", owner))
        self.owner = ""
    def set_polling_enabled(self, enabled): self.calls.append(("polling", enabled))
    def safe_move_to_field(self, value, **kwargs):
        self.calls.append(("move", value, kwargs))
        return "move-1"
    def pause(self): self.calls.append(("pause",))
    def configure_transport(self, rate, limit): self.calls.append(("configure", rate, limit))
    def restore_transport(self, rates, limits): self.calls.append(("restore", rates, limits))
    def enter_persistent_mode(self, **kwargs): self.calls.append(("persistent", kwargs))


class FakeLake(QObject):
    snapshot_updated = Signal(object)
    disconnected = Signal()
    error = Signal(str)
    fault = Signal(str)


class QuickMeasurementTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QCoreApplication.instance() or QCoreApplication([])

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.manager = FakeManager()
        self.service = MeasurementService(self.manager, SaveRoot(base=self.directory.name),
            magnet1000=FakeMagnet(), selected_backend_callable=lambda: "1000",
            worker_factories={k: FakeWorker for k in range(5)})

    def spin(self, predicate, timeout=3.):
        end = time.monotonic() + timeout
        while not predicate() and time.monotonic() < end:
            self.app.processEvents()
            time.sleep(.001)
        self.app.processEvents()
        self.assertTrue(predicate())

    def tearDown(self):
        if self.service._preflight_thread is not None:
            self.service.stop()
            self.spin(lambda: self.service._preflight_thread is None)
        worker = self.service.worker
        if worker is not None:
            worker.request_stop()
            worker.finish_cleanup.set()
            self.spin(lambda: self.service.worker_thread is None)
        self.service.deleteLater()
        self.app.processEvents()
        self.directory.cleanup()

    def test_qobject_service_has_all_modes_and_full_dataclass_fields(self):
        self.assertEqual(len(self.service.modes), 6)
        for index, (_, _, cls) in enumerate(MODES):
            self.service.mode = index
            keys = {f["key"] for f in self.service.fields}
            self.assertTrue({k for k in defaults(index) if not k.startswith("output_") and k not in {"plot_x_resolved", "magnet_backend", "rate_t_per_min", "field_spacing"}}.issubset(keys))
            self.assertIsInstance(build_params(defaults(index), index), cls)
        self.assertIsInstance(self.service, QObject)

    def ui_fields(self):
        return {f["key"]: f for s in self.service.uiSections for f in s["fields"]}

    def test_transport_step_editor_uses_mt_and_calculates_points(self):
        self.service.mode = 5
        self.service.setValue("measurement_mode", "stepped")
        self.service.setValue("field_step_t", "300")
        params = self.service.collect_params()
        self.assertEqual(params.field_step_t, .3)
        self.assertEqual(params.field_spacing, "step")
        fields = self.ui_fields()
        self.assertEqual(fields["field_step_t"]["value"], 300.)
        self.assertEqual(fields["field_points"]["value"], 5)
        self.assertTrue(fields["field_points"]["readOnly"])
        self.assertIn("rate_t_per_min", fields)
        summaries = " ".join(s.get("samplingText", "") for s in self.service.uiSections)
        self.assertIn("9 points / condition", summaries)
        self.assertIn("100 mT (shorter)", summaries)
        self.service.setValue("field_points", 100)
        self.assertEqual(self.service.collect_params().field_step_t, .3)
        self.service.setValue("field_step_t", "invalid")
        self.assertEqual(self.service.collect_params().field_step_t, .3)
        self.assertIn("error", self.ui_fields()["field_step_t"])
        self.service.setValue("field_step_t", "250")
        self.assertEqual(self.service.collect_params().field_step_t, .25)

    def test_hysteresis_editors_convert_mt_and_survive_mode_changes(self):
        self.service.mode = 5
        self.service.setValue("measurement_mode", "stepped")
        self.service.setValue("field_tolerance_t", "0.3")
        self.service.setValue("field_acceptance_t", "0.6")
        self.service.setValue("field_drift_t_per_s", "0.2")
        self.service.setValue("field_stability_s", "3")
        self.service.setValue("field_settle_s", "2.5")
        params = self.service.collect_params()
        self.assertAlmostEqual(params.field_tolerance_t, .0003)
        self.assertAlmostEqual(params.field_acceptance_t, .0006)
        self.assertAlmostEqual(params.field_drift_t_per_s, .0002)
        self.assertEqual(params.field_stability_s, 3.)
        self.assertEqual(params.field_settle_s, 2.5)
        self.assertEqual(self.ui_fields()["field_tolerance_t"]["value"], .3)
        self.service.setValue("measurement_mode", "continuous")
        self.assertNotIn("field_tolerance_t", self.ui_fields())
        self.service.setValue("measurement_mode", "stepped")
        self.assertEqual(self.ui_fields()["field_tolerance_t"]["value"], .3)
        from app.quick.parameters import measurement_sections
        sections = measurement_sections(self.service._values[5], 5, magnet_backend="2100")
        self.assertFalse(any(f["key"] == "field_tolerance_t" for s in sections for f in s["fields"]))

    def test_old_transport_points_migrate_without_unit_change(self):
        from app.quick.parameters import normalize_transport_spacing
        values = defaults(5)
        values.update(field_spacing="points", field_points=5, start_field_t=-.5, stop_field_t=.5)
        normalize_transport_spacing(values)
        self.assertEqual(values["field_spacing"], "step")
        self.assertEqual(values["field_step_t"], .25)
        normalize_transport_spacing(values)
        self.assertEqual(values["field_step_t"], .25)

    def test_bfield_gate_selected_editor_is_execution_recipe_and_empty_add_captures_default(self):
        self.service.mode = 4
        self.service.setValue("raw_vtg_stop", 2.)
        self.service.setValue("n_points", 3)
        self.service.addRow("conditions")
        self.assertEqual(self.service._values[4]["conditions"][0]["raw_vtg_stop"], 2.)
        self.service.duplicateRow("conditions", 0)
        self.service.selectRow("conditions", 1)
        editor = self.ui_fields()["raw_vtg_stop"]
        self.assertEqual((editor["rowKey"], editor["rowIndex"]), ("conditions", 1))
        self.service.setRowValue(editor["rowKey"], editor["rowIndex"], editor["key"], 3.)
        self.assertEqual(self.service._values[4]["raw_vtg_stop"], 2.)
        captured = self.service.capture_field_batch_requests()
        self.assertEqual([p.raw_vtg_stop for p, *_ in captured], [2., 3.])
        self.service.setValue("fields_t", "0, 0.2")
        self.assertIn("4 jobs = 2 fields × 2 enabled conditions", self.service.summary)
        self.service.removeRow("conditions", 0)
        self.service.removeRow("conditions", 0)
        self.assertEqual(self.ui_fields()["raw_vtg_stop"]["rowKey"], "")
        self.assertIn("Using default condition", self.service.summary)

    def test_derived_help_and_endpoints_match_worker_for_both_axes_and_ratio_targets(self):
        from app.gate_transform import ratio_formula_text
        from app.workers.line_sweep import LineSweepWorker
        self.service.mode = 1
        self.service.setValue("mode", "Derived")
        self.service.setValue("derived_ratio", 2.)
        self.service.setValue("derived_start", -3.)
        self.service.setValue("derived_stop", 5.)
        self.service.setValue("derived_fixed", 1.)
        self.service.setValue("n_points", 3)
        for axis in ("Doping", "E-field"):
            for target in ("Vbg", "Vtg"):
                with self.subTest(axis=axis, target=target):
                    self.service.setValue("derived_axis", axis)
                    self.service.setValue("derived_ratio_target", target)
                    section = next(s for s in self.service.uiSections if s["title"] == "Derived trajectory")
                    fields = {f["key"]: f for f in section["fields"]}
                    held = "E-field" if axis == "Doping" else "Doping"
                    self.assertEqual(fields["derived_fixed"]["label"], f"Hold {held} (V)")
                    self.assertEqual(fields["derived_start"]["label"], f"{axis} start (V)")
                    self.assertEqual(fields["derived_stop"]["label"], f"{axis} stop (V)")
                    self.assertIn(axis, section["description"])
                    self.assertIn(held, section["description"])
                    self.assertIn(ratio_formula_text(target), section["helpText"])
                    self.assertIn("non-zero", section["helpText"].lower())
                    self.assertIn("physical", section["helpText"].lower())
                    self.assertIn("voltage", section["helpText"].lower())
                    self.assertIn("carrier density", section["helpText"].lower())
                    worker = LineSweepWorker(self.service.collect_params(), self.service.save, self.manager.connections)
                    points = worker._build_derived_trajectory()
                    start, stop = points[0], points[-1]
                    expected = (f"Requested start: Vtg = {start['vtg']:g} V; Vbg = {start['vbg']:g} V\n"
                                f"Requested stop: Vtg = {stop['vtg']:g} V; Vbg = {stop['vbg']:g} V")
                    self.assertEqual(section["previewText"], expected)
                    self.assertEqual(section["previewError"], "")
        self.service.setValue("derived_ratio", 0)
        section = next(s for s in self.service.uiSections if s["title"] == "Derived trajectory")
        self.assertEqual(section["previewText"], "")
        self.assertTrue(section["previewError"])
        self.service.setValue("derived_ratio", 2.)
        self.service.setValue("derived_start", "invalid")
        section = next(s for s in self.service.uiSections if s["title"] == "Derived trajectory")
        self.assertEqual(section["previewText"], "")
        self.assertTrue(section["previewError"])

    def test_derived_preview_tracks_selected_bfield_condition_and_its_errors(self):
        from app.gate_transform import ratio_formula_text
        from app.workers.line_sweep import LineSweepWorker
        self.service.mode = 4
        self.service.setValue("mode", "Derived")
        self.service.addRow("conditions")
        self.service.duplicateRow("conditions", 0)
        for row, axis, target, start in ((0, "Doping", "Vbg", 2.), (1, "E-field", "Vtg", 6.)):
            for key, value in (("mode", "Derived"), ("derived_axis", axis),
                               ("derived_ratio_target", target), ("derived_ratio", 2.),
                               ("derived_start", start), ("derived_stop", 8.), ("derived_fixed", 1.)):
                self.service.setRowValue("conditions", row, key, value)
        for row, target in ((0, "Vbg"), (1, "Vtg")):
            self.service.selectRow("conditions", row)
            section = next(s for s in self.service.uiSections if s["title"] == "Derived trajectory")
            self.assertIn(ratio_formula_text(target), section["helpText"])
            params = build_params(self.service._values[4]["conditions"][row], 4)
            worker = LineSweepWorker(params, self.service.save, self.manager.connections)
            start = worker._build_derived_trajectory()[0]
            self.assertIn(f"Requested start: Vtg = {start['vtg']:g} V; Vbg = {start['vbg']:g} V", section["previewText"])
        self.service.setRowValue("conditions", 1, "derived_start", "invalid")
        bad = next(s for s in self.service.uiSections if s["title"] == "Derived trajectory")
        self.assertEqual(bad["previewText"], "")
        self.assertTrue(bad["previewError"])
        self.service.selectRow("conditions", 0)
        good = next(s for s in self.service.uiSections if s["title"] == "Derived trajectory")
        self.assertTrue(good["previewText"])
        self.assertEqual(good["previewError"], "")

    def test_derived_map_formulas_follow_selected_ratio_target(self):
        from app.gate_transform import ratio_formula_text
        self.service.mode = 2
        self.service.setValue("coordinate_mode", "Derived")
        for target in ("Vbg", "Vtg"):
            with self.subTest(target=target):
                self.service.setValue("ratio_target", target)
                sections = self.service.uiSections
                lines = ratio_formula_text(target).splitlines()
                doping = next(s for s in sections if s["title"].startswith("Doping "))
                efield = next(s for s in sections if s["title"].startswith("E-field "))
                self.assertIn(lines[0], doping["description"])
                self.assertIn(lines[1], efield["description"])
                for section in (doping, efield):
                    for field in section["fields"]:
                        self.assertIn("(V)", field["label"])
                        self.assertNotIn("derived V", field["label"])

    def test_transport_gate_preview_matches_planner_shared_ratio_without_mutating_rows(self):
        from copy import deepcopy
        from app.engine.bfield_transport_sweep import TransportSweepPlan
        self.service.mode = 5
        self.service.setValue("ratio", 2.)
        self.service.setValue("ratio_target", "Vtg")
        self.service.setRowValue("conditions", 0, "doping", 4.)
        self.service.setRowValue("conditions", 0, "efield", 2.)
        before = deepcopy(self.service._values[5])
        fields = self.ui_fields()
        planned = TransportSweepPlan.from_params(self.service.collect_params()).conditions[0]
        self.assertEqual((fields["vtg"]["value"], fields["vbg"]["value"]), (planned.vtg, planned.vbg))
        self.assertEqual((fields["vtg"]["value"], fields["vbg"]["value"]), (1.5, 1.))
        self.assertTrue(fields["vtg"]["readOnly"])
        self.assertEqual(fields["ratio"]["rowKey"], "")
        self.assertEqual(self.service._values[5], before)
        self.service.setRowValue("conditions", 0, "vtg", 9.)
        self.assertEqual(self.service._values[5], before)
        self.service.setValue("ratio", 0.)
        self.assertEqual(self.ui_fields()["vtg"]["value"], "Unavailable")
        self.assertIn("non-zero", self.ui_fields()["vtg"]["error"])

    def test_compact_controls_follow_coordinate_dimension_and_source(self):
        self.service.mode = 1
        self.assertNotIn("raw_vbg_stop", self.ui_fields())
        self.service.setValue("mode", "Derived")
        self.assertNotIn("raw_vtg_start", self.ui_fields())
        self.assertNotIn("derived_vds_start", self.ui_fields())
        self.service.setValue("derived_vds_mode", "Swept")
        self.assertIn("derived_vds_start", self.ui_fields())
        self.service.setValue("vds_source", "None")
        self.assertNotIn("derived_vds_start", self.ui_fields())
        self.service.mode = 2
        self.assertIn("vtg_stop", self.ui_fields())
        self.assertNotIn("vbg_stop", self.ui_fields())
        self.service.setValue("coordinate_mode", "Derived")
        self.assertEqual((self.service.collect_params().axis_fast, self.service.collect_params().axis_slow), ("Doping", "E-field"))
        self.assertNotIn("None", self.ui_fields()["axis_slow"]["options"])
        self.service.setValue("vds_start", .2)
        self.assertEqual(self.service.collect_params().vds_stop, .2)
        self.assertNotIn("vds_stop", self.ui_fields())
        self.service.mode = 3
        self.assertNotIn("vds", self.ui_fields())
        self.assertNotIn("vds_source", self.ui_fields())
        self.service.setValue("use_vds", True)
        self.service.setValue("vds_source", "NI DAQ AO")
        self.assertIn("vds", self.ui_fields())
        self.assertIn("ao_channel", self.ui_fields())

    def test_recipe_duplicate_selection_and_active_lock_preserve_rows(self):
        from copy import deepcopy
        self.service.mode = 3
        self.service.setRowValue("bias_conditions", 0, "vtg", 2.)
        self.service.duplicateRow("bias_conditions", 0)
        self.assertEqual(self.ui_fields()["vtg"]["rowIndex"], 1)
        self.service.setRowValue("bias_conditions", 1, "vtg", 3.)
        self.assertEqual([r["vtg"] for r in self.service._values[3]["bias_conditions"]], [2., 3.])
        before = deepcopy(self.service._values[3])
        self.service._batch_claimed = True
        self.service.duplicateRow("bias_conditions", 0)
        self.service.removeRow("bias_conditions", 0)
        self.assertEqual(self.service._values[3], before)
        self.service._batch_claimed = False

    def test_field_rejections_are_inline_mode_local_and_clear_on_valid_edit(self):
        self.service.setValue("vds_step", "nan")
        self.assertEqual(self.service._values[0]["vds_step"], .01)
        self.assertIn("finite", self.ui_fields()["vds_step"]["error"])
        descriptor = next(f for f in self.service.fields if f["key"] == "vds_step")
        self.assertEqual(descriptor["error"], self.ui_fields()["vds_step"]["error"])
        self.service.mode = 2
        self.service.setValue("axis_fast", "Vds")
        self.assertNotIn("error", self.ui_fields()["vds_step"])
        self.service.mode = 0
        self.assertIn("error", self.ui_fields()["vds_step"])
        self.service.setValue("vds_step", ".05")
        self.assertEqual(self.ui_fields()["vds_step"]["value"], .05)
        self.assertNotIn("error", self.ui_fields()["vds_step"])

    def test_validation_issues_report_friendly_numeric_errors_and_clear(self):
        self.service.setValue("vds_step", "not a number")
        issue = self.service.validationIssues[0]
        self.assertEqual((issue["section"], issue["key"], issue["rowKey"], issue["rowIndex"]),
                         ("Vds trajectory", "vds_step", "", -1))
        self.assertIn("valid number", issue["message"])
        self.assertIn("Vds", issue["message"])
        self.service.setValue("vds_step", "0.1")
        self.assertEqual(self.service.validationIssues, [])

        self.service.mode = 3
        self.service.duplicateRow("bias_conditions", 0)
        self.service.selectRow("bias_conditions", 0)
        self.service.setRowValue("bias_conditions", 1, "vtg", "bad")
        issue = self.service.validationIssues[0]
        self.assertEqual((issue["section"], issue["key"], issue["rowKey"], issue["rowIndex"]),
                         ("Bias recipe", "vtg", "bias_conditions", 1))
        self.assertIn("valid number", issue["message"])
        self.service.setRowValue("bias_conditions", 1, "vtg", "1")
        self.assertEqual(self.service.validationIssues, [])

    def test_filename_field_tracks_selected_condition_without_advanced_details(self):
        self.service.mode = 4
        self.assertEqual(self.service.filenameField["rowKey"], "")
        self.service.addRow("conditions")
        self.service.setRowValue("conditions", 0, "base_name", "first")
        self.service.duplicateRow("conditions", 0)
        self.service.setRowValue("conditions", 1, "base_name", "second")
        self.assertFalse(self.service._recipe_details_visible)
        changes = []
        self.service.fieldsChanged.connect(lambda: changes.append(self.service.filenameField.copy()))
        self.service.selectRow("conditions", 0)
        self.assertEqual((self.service.filenameField["rowIndex"], self.service.filenameField["value"]), (0, "first"))
        self.service.selectRow("conditions", 1)
        self.assertEqual((self.service.filenameField["rowIndex"], self.service.filenameField["value"]), (1, "second"))
        self.service.setRowValue("conditions", 1, "base_name", "updated")
        self.assertEqual(changes[-1]["value"], "updated")
        self.assertEqual(changes[-1]["rowKey"], "conditions")

    def test_filename_draft_preview_is_read_only_and_targets_selected_condition(self):
        from copy import deepcopy
        field = {"key": "base_name", "rowKey": "", "rowIndex": -1}
        before = deepcopy(self.service._values)
        errors = deepcopy(self.service._field_errors)
        with patch.object(self.service, "_save_settings") as save, \
             patch("os.makedirs", side_effect=AssertionError("created directory")):
            preview = self.service.previewOutputDraft(field, "draft / name\nextra")
            self.assertIn("draft", preview["csvNames"][0])
            self.assertEqual(preview["editingScope"], "Measurement")
            self.assertEqual(self.service._values, before)
            self.assertEqual(self.service._field_errors, errors)
            save.assert_not_called()
        self.service.setValue("vds_step", "invalid")
        self.assertIn("other invalid", self.service.previewOutputDraft(field, "draft")["error"])
        self.service.setValue("vds_step", ".1")

        self.service.mode = 4
        self.service.setValue("fields_t", "0, 1")
        self.service.addRow("conditions")
        self.service.duplicateRow("conditions", 0)
        self.service.selectRow("conditions", 1)
        before = deepcopy(self.service._values)
        field = {"key": "base_name", "rowKey": "conditions", "rowIndex": 1}
        result = self.service.previewOutputDraft(field, "second_draft")
        self.assertEqual(result["csvCount"], 4)
        self.assertIn("second_draft", result["csvNames"][0])
        self.assertEqual(result["editingScope"], "Condition 2")
        self.assertIn("first field", result["note"])
        self.assertEqual(self.service._values, before)

    def test_hidden_invalid_field_has_recovery_section_until_corrected(self):
        self.service.mode = 2
        self.service.setValue("axis_fast", "Vbg")
        self.service.setValue("vbg_stop", "bad")
        self.assertFalse(any(section.get("recovery") for section in self.service.uiSections))
        self.service.setValue("axis_fast", "Vtg")
        sections = self.service.uiSections
        recovery = next(section for section in sections if section.get("recovery"))
        self.assertEqual(recovery["title"], "Other invalid parameters")
        field = next(field for field in recovery["fields"] if field["key"] == "vbg_stop")
        self.assertEqual(field["value"], self.service._values[2]["vbg_stop"])
        self.assertIn("valid number", field["error"])
        self.assertEqual(self.service.validationIssues[0]["section"], recovery["title"])
        self.service.setValue("vbg_stop", "1.5")
        self.assertEqual(self.service.validationIssues, [])
        self.assertFalse(any(section.get("recovery") for section in self.service.uiSections))

    def test_recipe_rejections_follow_row_selection_duplicate_remove_and_mode(self):
        self.service.mode = 3
        self.service.duplicateRow("bias_conditions", 0)
        self.service.selectRow("bias_conditions", 0)
        self.service.setRowValue("bias_conditions", 0, "vtg", "nan")
        self.assertEqual(self.ui_fields()["vtg"]["value"], 0.)
        self.assertIn("finite", self.ui_fields()["vtg"]["error"])
        self.service.selectRow("bias_conditions", 1)
        self.assertNotIn("error", self.ui_fields()["vtg"])
        self.service.setRowValue("bias_conditions", 1, "vtg", "inf")
        self.service.duplicateRow("bias_conditions", 0)
        self.assertNotIn("error", self.ui_fields()["vtg"])
        self.service.selectRow("bias_conditions", 2)
        self.assertIn("error", self.ui_fields()["vtg"])
        self.service.removeRow("bias_conditions", 0)
        self.service.selectRow("bias_conditions", 1)
        self.assertIn("error", self.ui_fields()["vtg"])
        self.service.mode = 5
        self.assertFalse(self.ui_fields()["vtg"].get("error"))
        self.service.mode = 3
        self.assertIn("error", self.ui_fields()["vtg"])
        self.service.setRowValue("bias_conditions", 1, "vtg", "1.25")
        self.assertNotIn("error", self.ui_fields()["vtg"])
        self.assertEqual(self.ui_fields()["vtg"]["value"], 1.25)

    def test_rejected_edit_blocks_start_until_corrected_and_only_in_its_mode(self):
        can_start_updates = []
        self.service.changed.connect(lambda: can_start_updates.append(self.service.canStart))
        with patch.object(self.service, "_begin_preflight") as preflight, patch.object(self.service, "_temperature_ready", return_value=True) as temperature:
            self.service.setValue("vds_step", "nan")
            self.assertFalse(self.service.canStart)
            self.service.start()  # Also guards a click queued before the blur rejection.
            self.assertIn("Correct invalid fields", self.service.message)
            preflight.assert_not_called()
            temperature.assert_not_called()
            self.assertFalse(self.manager.claimed)
            self.assertIsNone(self.service._preflight_thread)
            self.service.mode = 1
            self.assertTrue(self.service.canStart)
            self.service.mode = 0
            self.assertFalse(self.service.canStart)
            self.service.setValue("vds_step", ".02")
            self.assertTrue(self.service.canStart)
            self.assertTrue(can_start_updates[-1])
            self.service.start()
            preflight.assert_called_once()
        self.service.mode = 3
        self.service.setRowValue("bias_conditions", 0, "vtg", "inf")
        self.assertFalse(self.service.canStart)
        with patch.object(self.service, "_begin_preflight") as preflight:
            self.service.start()
            preflight.assert_not_called()
        self.service.setRowValue("bias_conditions", 0, "vtg", "0")
        self.assertTrue(self.service.canStart)

    def test_finish_releases_only_after_finally_and_thread_exit(self):
        results, records = [], []
        self.service.runCompleted.connect(results.append)
        self.service.recordReady.connect(records.append)
        self.service.start()
        self.spin(lambda: self.service.worker is not None)
        worker = self.service.worker
        self.assertIsNotNone(worker)
        self.spin(lambda: worker.entered.is_set())
        self.service.stop()
        self.spin(lambda: worker.terminal_sent.is_set())
        self.assertTrue(self.service.active)
        self.assertTrue(self.manager.claimed)
        self.assertEqual(results, [])
        self.assertFalse(worker.cleaned)
        worker.finish_cleanup.set()
        self.spin(lambda: not self.service.active)
        self.assertTrue(worker.cleaned)
        self.assertFalse(self.manager.claimed)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["status"], "stopped")
        self.assertEqual(len(records), 1)

    def test_duplicate_start_and_mode_change_preserve_active_run(self):
        self.service.start()
        self.spin(lambda: self.service.worker is not None)
        original = self.service.worker
        self.service.start()
        self.service.mode = 2
        self.service.setValue("vds_start", 3.)
        self.assertIs(self.service.worker, original)
        self.assertEqual(self.service.mode, 0)
        self.assertEqual(self.service._values[0]["vds_start"], 0.)

    def test_invalid_parameters_and_unverified_protection_do_not_claim(self):
        self.service.setValue("vds_step", 0.)
        self.service.start()
        self.assertIsNone(self.service.worker)
        self.assertFalse(self.manager.claimed)
        self.service.setValue("vds_step", .01)
        self.service.setValue("vtg_set", 21.)
        self.service.start()
        self.assertIsNone(self.service.worker)
        self.service.setValue("vtg_set", 0.)
        self.manager.protected = False
        self.service.start()
        self.assertIn("protection", self.service.message)
        self.assertFalse(self.manager.claimed)

    def test_nonfinite_integer_choices_rows_and_ao_are_validated(self):
        self.service.setValue("vds_start", "nan")
        self.assertEqual(self.service._values[0]["vds_start"], 0.)
        self.service.setValue("n_sample", 1.5)
        self.assertEqual(self.service._values[0]["n_sample"], 3)
        self.service.setValue("vds_source", "invented")
        self.assertEqual(self.service._values[0]["vds_source"], "Keithley 2400")
        self.service.mode = 3
        self.service.addRow("bias_conditions")
        self.service.setRowValue("bias_conditions", 1, "vtg", "2.5")
        self.assertEqual(self.service._values[3]["bias_conditions"][1]["vtg"], 2.5)
        self.service.removeRow("bias_conditions", 0)
        self.assertEqual(len(self.service._values[3]["bias_conditions"]), 1)
        self.service.mode = 0
        self.service.setValue("vds_start", 0.)
        self.service.setValue("n_sample", 3)
        self.service.setValue("vds_source", "NI DAQ AO")
        self.service.setValue("ao_channel", 99)
        self.service.start()
        self.assertIsNone(self.service.worker)
        self.assertIn("AO", self.service.message)

    def test_mode_values_are_isolated_and_late_record_is_ignored(self):
        records = []
        self.service.recordReady.connect(records.append)
        self.service.setValue("base_name", "sweep")
        self.service.mode = 1
        self.service.setValue("base_name", "line")
        self.service.mode = 0
        self.assertEqual(self.service._values[0]["base_name"], "sweep")
        late_worker = FakeWorker(build_params(defaults(0), 0), None, None)
        late_worker.point_data.connect(self.service._on_record)
        late_worker.point_data.emit({"x": 9., "Ids_DC": 1.})
        self.app.processEvents()
        self.assertEqual(records, [])

    def test_map_resolution_blocks_unrepresentable_physical_target(self):
        self.service.mode = 2
        self.service.setValue("vtg_start", .0001)
        self.service.setValue("vtg_stop", .0001)
        self.service.start()
        self.assertIsNone(self.service.worker)
        self.assertIn("representable", self.service.message)
        self.assertFalse(self.manager.claimed)

    def test_fixed_zero_gates_are_validated_and_reserved_when_connected(self):
        for mode in (0, 2, 3):
            with self.subTest(mode=mode):
                self.service.mode = mode
                if mode == 2:
                    self.service.setValue("axis_fast", "Vds")
                required, _ = self.service._validate(self.service.collect_params(), mode)
                self.assertTrue({"g1", "g2"}.issubset(required))
                self.manager.protected = False
                with self.assertRaisesRegex(ValueError, "G1 protection"):
                    self.service._validate(self.service.collect_params(), mode)
                self.manager.protected = True
                with patch.object(self.manager, "is_voltage_source_mode", return_value=False):
                    with self.assertRaisesRegex(ValueError, "G1 must use"):
                        self.service._validate(self.service.collect_params(), mode)
                self.manager.claimed.add("g1")
                with self.assertRaisesRegex(ValueError, "already in use"):
                    self.service._claim(required)
                self.manager.claimed.clear()
                self.service._claim(required)
                self.assertTrue({"g1", "g2"}.issubset(self.manager.claimed))
                self.service._release()
                gates = {name: self.manager.sessions.pop(name) for name in ("g1", "g2")}
                try:
                    required, _ = self.service._validate(self.service.collect_params(), mode)
                    self.assertTrue({"g1", "g2"}.isdisjoint(required))
                finally:
                    self.manager.sessions.update(gates)

    def test_fixed_zero_gates_with_retained_unverified_session_block_start(self):
        # A failed zero-before-reconnect retains the session with an error
        # state. It must not be treated like an absent optional zero gate.
        self.assertIsNotNone(self.manager.get_session("g1"))
        with patch.object(self.manager, "is_connected", side_effect=lambda name:
                name != "g1" and self.manager.get_session(name) is not None):
            for mode in (0, 2, 3):
                with self.subTest(mode=mode):
                    self.service.mode = mode
                    if mode == 2:
                        self.service.setValue("axis_fast", "Vds")
                    self.service.start()
                    self.assertIn("Connect required devices first: g1", self.service.message)
                    self.assertIsNone(self.service.worker)
                    self.assertIsNone(self.service._preflight_thread)
                    self.assertFalse(self.manager.claimed)

    def test_output_failure_rolls_back_reservation(self):
        with patch("app.quick.measurements.build_planned_output", side_effect=OSError("Output unavailable")):
            self.service.start()
            self.spin(lambda: not self.service.active)
        self.assertFalse(self.service.active)
        self.assertFalse(self.manager.claimed)
        self.assertIn("Output unavailable", self.service.message)

    def test_all_normal_modes_launch_real_parameter_objects(self):
        for mode in range(4):
            self.service.mode = mode
            self.service.start()
            self.spin(lambda: self.service.worker is not None or not self.service.active)
            self.assertIsNotNone(self.service.worker, self.service.message)
            worker = self.service.worker
            self.assertIsInstance(worker.params, MODES[mode][2])
            worker.finish_cleanup.set()
            self.spin(lambda: not self.service.active)

    def test_stop_preparing_waits_for_read_and_never_starts_acquisition(self):
        started, finish = threading.Event(), threading.Event()
        def slow_capture(chain):
            started.set()
            finish.wait(3.)
            return chain
        self.service.capture_run_signal_chain = slow_capture
        before = len(FakeWorker.instances)
        self.service.start()
        self.spin(started.is_set)
        self.service.stop()
        self.assertEqual(self.service.phase, "stopping")
        self.assertTrue(self.service.active)
        self.assertTrue(self.manager.claimed)
        finish.set()
        self.spin(lambda: not self.service.active)
        self.assertFalse(self.manager.claimed)
        self.assertEqual(len(FakeWorker.instances), before)
        self.assertEqual(self.service.phase, "stopped")

    def test_legacy_settings_read_all_mode_specific_aliases_without_widgets(self):
        import json
        class Settings:
            values = {"tabs/vds_sweep/source": "NI DAQ ao1", "tabs/vds_sweep/vtg": 3.,
                "tabs/gate_scan/mode_derived": True, "tabs/gate_scan/derived_axis_efield": True,
                "tabs/gate_scan/derived_vds_swept_mode": True, "tabs/gate_scan/averages": 7,
                "tabs/gate_scan/derived_ratio_target": "Vtg (top gate)",
                "tabs/map_2d/coordinates": "Doping / E-field", "tabs/map_2d/derived_fast_axis": "E-field",
                "tabs/map_2d/derived_slow_axis": "Doping", "tabs/map_2d/sweep_dim": "2D map",
                "tabs/photocurrent/bias_conditions": json.dumps([dict(enabled=True, vtg=2., vbg=1., vds=.1, settle_s=4.)]),
                "tabs/bfield_gate_scan/bfield_fields": "(-1,1,5)",
                "tabs/bfield_transport/delay_s": .3, "tabs/bfield_transport/final_mode": "persistent",
                "tabs/bfield_transport/field_spacing": "points", "tabs/bfield_transport/field_points": 5}
            def value(self, key, default=None): return self.values.get(key, default)
        service = MeasurementService(self.manager, SaveRoot(base=self.directory.name), settings=Settings())
        self.assertEqual(service._values[0]["ao_channel"], 1)
        self.assertEqual(service._values[0]["vds_source"], "NI DAQ AO")
        self.assertEqual(service._values[0]["vtg_set"], 3.)
        self.assertEqual(service._values[1]["mode"], "Derived")
        self.assertEqual(service._values[1]["derived_axis"], "E-field")
        self.assertEqual(service._values[1]["derived_vds_mode"], "Swept")
        self.assertEqual(service._values[1]["n_sample"], 7)
        self.assertEqual(service._values[1]["derived_ratio_target"], "Vtg")
        self.assertEqual(service._values[2]["axis_fast"], "E-field")
        self.assertEqual(service._values[2]["axis_slow"], "Doping")
        self.assertEqual(service._values[3]["bias_conditions"][0]["settle_s"], 4.)
        self.assertEqual(service._values[4]["fields_t"], "(-1,1,5)")
        self.assertEqual(service._values[5]["acquisition_delay_s"], .3)
        self.assertEqual(service._values[5]["final_mode"], "driven")
        self.assertEqual(service._values[5]["field_spacing"], "step")
        self.assertEqual(service._values[5]["field_step_t"], .25)
        service.deleteLater()

    def test_magnetic_confirmation_is_async_bound_to_field_and_mode(self):
        magnet = FakeMagnet()
        thermal = SimpleNamespace(is_armed=True, latest_snapshot=object(), evaluate=lambda *_: SimpleNamespace(magnet_permission=True))
        service = MeasurementService(self.manager, SaveRoot(base=self.directory.name), magnet1000=magnet,
            thermal_safety=thermal, review_valid_callable=lambda: True)
        self.addCleanup(service.deleteLater)
        service.mode = 4
        requests = []
        service.confirmationRequested.connect(lambda *request: requests.append(request))
        service.start()
        self.assertEqual(len(requests), 1)
        self.assertEqual(service.phase, "confirmation")
        self.assertTrue(service.active)
        self.assertFalse(magnet.calls)
        service.answerConfirmation("stale token", True)
        self.assertEqual(service.phase, "confirmation")
        service.answerConfirmation(requests[0][0], False)
        self.assertFalse(service.active)
        self.assertFalse(magnet.calls)
        service.start()
        token = requests[-1][0]
        magnet.latest_snapshot.field_t = .25
        service.answerConfirmation(token, True)
        self.assertEqual(service.phase, "confirmation")
        self.assertEqual(len(requests), 3)
        self.assertIn("+0.250000", requests[-1][2])
        self.assertFalse(magnet.calls)
        service.stop()
        service.mode = 5
        service.start()
        self.assertEqual(service.phase, "confirmation")
        self.assertEqual(len(requests), 4)
        service.stop()

    def test_magnetic_non_aps100_and_uncommissioned_thermal_fail_closed(self):
        magnet = FakeMagnet()
        service = MeasurementService(self.manager, SaveRoot(base=self.directory.name), magnet1000=magnet,
            review_valid_callable=lambda: True, selected_backend_callable=lambda: "2100")
        service.mode = 4
        service.start()
        self.assertFalse(service.active)
        self.assertIn("unavailable", service.message)
        service.mode = 5
        service.start()
        self.assertIn("controller is unavailable", service.message)
        service._selected_backend = lambda: "1000"
        service.start()
        self.assertFalse(service.active)
        self.assertIn("thermal", service.message)
        self.assertFalse(magnet.calls)
        service.deleteLater()

    def test_quick_2100_stepped_routes_without_aps_or_lakeshore_and_locks_engine(self):
        class Fake2100Engine(QObject):
            continuation_changed = Signal(bool, bool, str)
            state_changed = Signal(str, str)
            progress_changed = Signal(int, int, str)
            error = Signal(str)
            restoration_warning = Signal(str)
            finished = Signal()
            stopped = Signal(str)
            shutdown_ready = Signal()

            def __init__(self, magnet, tab, manager, parent=None, review_valid_callable=None):
                super().__init__(parent)
                self.tab = tab
                self.active = self._cleanup_in_progress = False
                self.started_params = None
                self.stop_count = 0

            def start(self, *, verified_calibration=None):
                self.started_params = self.tab.collect_params()
                self.active = True
                return True

            def stop(self):
                self.stop_count += 1
                self.active = False

        selected = ["2100"]
        with patch("app.engine.bfield2100_transport_controller.BField2100TransportController", Fake2100Engine):
            magnet = FakeMagnet()
            service = MeasurementService(self.manager, SaveRoot(base=self.directory.name),
                magnet1000=magnet, magnet2100=object(), thermal_safety=None,
                selected_backend_callable=lambda: selected[0], review_valid_callable=lambda: True)
            service.mode = 5
            service._values[5]["measurement_mode"] = "stepped"
            with patch.object(service, "_begin_preflight") as preflight:
                service.start()
            self.assertTrue(preflight.called, service.message)
            self.assertEqual(preflight.call_args.args[0].magnet_backend, "2100")
            self.assertFalse(magnet.calls)
            engine = service._transport._engines["2100"]
            self.assertTrue(service._transport.start())
            self.assertEqual(engine.started_params.magnet_backend, "2100")
            selected[0] = "1000"
            self.assertIs(service._transport._selected(), engine)
            service._transport.stop()
            self.assertEqual(engine.stop_count, 1)
            self.assertFalse(magnet.calls)
            service.deleteLater()

    def test_lakeshore_fault_and_read_errors_reach_engines_immediately(self):
        magnet, lake = FakeMagnet(), FakeLake()
        thermal = SimpleNamespace(is_armed=True, latest_snapshot=object(), evaluate=lambda *_: SimpleNamespace(magnet_permission=True))
        service = MeasurementService(self.manager, SaveRoot(base=self.directory.name), magnet1000=magnet,
            lakeshore_controller=lake, thermal_safety=thermal)
        with patch.object(service._transport, "_on_fault") as fault, patch.object(service._batch, "_abort_batch") as abort:
            lake.fault.emit("Temperature exceeded limit")
            fault.assert_called_once_with("Temperature exceeded limit")
            self.assertIn("Temperature exceeded limit", abort.call_args.args[0])
        with patch.object(service._transport, "on_lakeshore_disconnected") as hold, patch.object(service._batch, "on_lakeshore_disconnected") as stop:
            lake.error.emit("Communication lost")
            hold.assert_called_once()
            stop.assert_called_once()
        service.deleteLater()

    def test_summary_and_group_order_are_stable_and_coordinate_aware(self):
        self.service.mode = 1
        self.service.setValue("mode", "Derived")
        self.assertIn("Doping =", self.service.summary)
        self.assertIn("Vtg:", self.service.summary)
        fields = self.service.fields
        self.assertFalse(next(f for f in fields if f["key"] == "raw_vtg_start")["visible"])
        groups = [field["group"] for field in fields]
        for group in set(groups):
            positions = [i for i, candidate in enumerate(groups) if candidate == group]
            self.assertEqual(positions, list(range(positions[0], positions[-1]+1)))

    def test_photocurrent_worker_default_leaves_unreserved_zero_recipe_gates_untouched(self):
        from app.workers.photocurrent import PhotocurrentWorker

        class Gate:
            def __init__(self, voltage):
                self.voltage = voltage
                self.calls = []
            def get_voltage_setpoint(self):
                self.calls.append("read")
                return self.voltage
            def set_voltage(self, voltage):
                self.calls.append("write")
                self.voltage = voltage

        values = defaults(3)
        values.update(use_vds=False, wl_start=550., wl_stop=550., delay=0., n_sample=1)
        values["bias_conditions"][0].update(vtg=0., vbg=0., settle_s=0.)
        gates = [Gate(.5), Gate(-.5)]
        worker = PhotocurrentWorker(build_params(values, 3), SaveRoot(base=self.directory.name),
            Connections(), g1=gates[0], g2=gates[1],
            daq=SimpleNamespace(acquire=lambda: {"ai0": .1, "ai1": .2, "ai2": .3}),
            mono=SimpleNamespace(set_wavelength=lambda _value: None))
        completed, errors = [], []
        worker.finished.connect(completed.append)
        worker.error.connect(errors.append)
        worker.run()
        self.assertEqual(errors, [])
        self.assertEqual(len(completed), 1)
        self.assertEqual([gate.voltage for gate in gates], [.5, -.5])
        self.assertTrue(all(not gate.calls for gate in gates))
        worker.deleteLater()

    def test_real_acquisition_workers_write_csv_metadata_and_zero_simulated_outputs(self):
        from app.workers.dual_gate import DualGateWorker
        from app.workers.line_sweep import LineSweepWorker
        from app.workers.cosweep import CoSweepWorker
        from app.workers.photocurrent import PhotocurrentWorker
        import csv
        import json
        class Gate:
            identity = "KEITHLEY INSTRUMENTS INC.,MODEL 2400,x,y"
            cached_source_voltage_range_v = 20.
            def refresh_source_voltage_range(self): return self.cached_source_voltage_range_v
            voltage = 0.
            current = 1e-9
            def get_voltage_setpoint(self): return self.voltage
            def set_voltage(self, value): self.voltage = value
            def ramp_voltage(self, value, *args): self.voltage = value
            def acquire(self): return {"current": self.current}
        class Daq:
            def acquire(daq):
                # Real workers must apply both fixed zero setpoints before sampling.
                acquired_gate_states.append(tuple(self.manager.sessions[n].voltage for n in ("g1", "g2")))
                # Exercise their finally blocks after a simulated output drift.
                for name in ("g1", "g2"):
                    self.manager.sessions[name].voltage = .02
                return {"ai0": .1, "ai1": .2, "ai2": .3}
            def get_ai_value(self, index): return [.1, .2, .3][index]
            def get_max_output(self, channel): return 10.
            def ramp_voltage(self, *args, **kwargs): pass
            def get_ao_vs_gnd_value(self, channel): return 0.
        for name in ("g1", "g2", "g3"):
            self.manager.sessions[name] = Gate()
        self.manager.sessions["daq"] = Daq()
        self.manager.sessions["mono"] = SimpleNamespace(set_wavelength=lambda value: None)
        service = MeasurementService(self.manager, SaveRoot(base=self.directory.name))
        completed = []
        acquired = []
        acquired_gate_states = []
        service.runCompleted.connect(completed.append)
        service.recordReady.connect(acquired.append)
        for mode in range(4):
            for name in ("g1", "g2"):
                self.manager.sessions[name].voltage = 1.
            service.mode = mode
            service.setValue("delay", 0.)
            service.setValue("n_sample", 1)
            if mode == 0:
                service.setValue("vds_stop", 0.)
            elif mode == 1:
                service.setValue("n_points", 1)
                service.setValue("raw_vtg_stop", 0.)
            elif mode == 2:
                service.setValue("vtg_stop", 0.)
                service.setValue("vbg_stop", 0.)
            else:
                service.setValue("wl_stop", 550.)
                service.setRowValue("bias_conditions", 0, "settle_s", 0.)
            with patch.multiple("app.workers.dual_gate", GATE_BIAS_RAMP_STEP_T=0., SAFE_RAMP_STEP_T=0.), \
                    patch.multiple("app.workers.line_sweep", GATE_BIAS_RAMP_STEP_T=0., SAFE_RAMP_STEP_T=0.), \
                    patch.multiple("app.workers.cosweep", GATE_BIAS_RAMP_STEP_T=0., SAFE_RAMP_STEP_T=0.), \
                    patch.multiple("app.workers.photocurrent", GATE_BIAS_RAMP_STEP_T=0., SAFE_RAMP_STEP_T=0.):
                service.start()
                self.spin(lambda: not service.active)
            self.assertEqual(service.phase, "finished", service.message)
            result = completed[-1]
            self.assertFalse(self.manager.claimed)
            metadata = json.loads(open(result["metadata_path"], encoding="utf-8").read())
            self.assertEqual(metadata["status"], "finished")
            self.assertTrue(metadata["safe_state"]["ok"])
            for path in result["csv_paths"]:
                with open(path, newline="", encoding="utf-8") as handle:
                    rows = list(csv.reader(handle))
                self.assertEqual(len(rows), 3)
                self.assertIn("Ids_DC", rows[0])
            self.assertTrue(all(self.manager.sessions[name].voltage == 0. for name in ("g1", "g2", "g3")))
            self.assertEqual(acquired_gate_states[-1], (0., 0.))
        self.assertEqual(len(acquired), 4)
        service.deleteLater()

    def test_bfield_gate_port_runs_engine_with_no_widget_and_reports_after_release(self):
        magnet = FakeMagnet()
        decision = SimpleNamespace(magnet_permission=True, reason="Safe", state=SimpleNamespace(value="SAFE"))
        thermal = SimpleNamespace(is_armed=True, latest_snapshot=object(), evaluate=lambda *_: decision)
        service = MeasurementService(self.manager, SaveRoot(base=self.directory.name), magnet1000=magnet,
            thermal_safety=thermal, review_valid_callable=lambda: True, worker_factories={4: FakeWorker})
        service.mode = 4
        magnet.snapshot_updated.emit(magnet.latest_snapshot)
        service._confirmation_granted = 0., 4
        completed = []
        service.runCompleted.connect(lambda result: completed.append((result, set(self.manager.claimed), magnet.owner)))
        service.start()
        self.spin(lambda: bool(magnet.owner))
        self.assertTrue(self.manager.claimed)
        magnet.safe_move_result.emit({"success": True, "request_id": "move-1", "snapshot": magnet.latest_snapshot})
        self.spin(lambda: service.worker is not None or not service.active)
        self.assertIsNotNone(service.worker, service.message)
        service.worker.finish_cleanup.set()
        self.spin(lambda: not service.active)
        self.assertEqual(len(completed), 1)
        self.assertFalse(completed[0][1])
        self.assertFalse(completed[0][2])
        self.assertEqual(completed[0][0]["status"], "finished")
        self.assertEqual(len(completed[0][0]["runs"]), 1)
        service.deleteLater()

    def test_bfield_transport_port_stops_only_after_persistent_ack_and_storage_close(self):
        class Gate:
            identity = "KEITHLEY INSTRUMENTS INC.,MODEL 2400,x,y"
            cached_source_voltage_range_v = 20.
            def refresh_source_voltage_range(self): return self.cached_source_voltage_range_v
            voltage = 0.
            def get_voltage_setpoint(self): return self.voltage
            def set_voltage(self, value): self.voltage = value
        for name in ("g1", "g2", "g3"):
            self.manager.sessions[name] = Gate()
        self.manager.sessions["daq"] = SimpleNamespace(acquire=lambda: {"ai0": 0., "ai1": 0., "ai2": 0.})
        magnet = FakeMagnet()
        thermal = SimpleNamespace(is_armed=True, latest_snapshot=object(), evaluate=lambda *_: SimpleNamespace(magnet_permission=True))
        rate_threads = []
        fail_rates = True
        def rates():
            rate_threads.append(threading.get_ident())
            if fail_rates:
                raise RuntimeError("Sensitivity verification failed")
            if threading.get_ident() == threading.main_thread().ident:
                raise RuntimeError("Sensitivity reread on GUI thread")
            return 2e7, 200.
        service = MeasurementService(self.manager, SaveRoot(base=self.directory.name), magnet1000=magnet,
            thermal_safety=thermal, review_valid_callable=lambda: True, get_global_rates=rates)
        service.mode = 5
        service._confirmation_granted = 0., 5
        service.start()
        self.spin(lambda: not service.active)
        self.assertIn("Sensitivity verification failed", service.message)
        self.assertFalse(self.manager.claimed)
        self.assertFalse(magnet.calls)
        self.assertIsNone(service._engine_calibration)
        fail_rates = False
        completed = []
        prepared, records = [], []
        service.runCompleted.connect(lambda result: completed.append((result, set(self.manager.claimed), magnet.owner)))
        service.runPrepared.connect(prepared.append)
        service.recordReady.connect(records.append)
        service.start()
        self.spin(lambda: any(call[0] == "configure" for call in magnet.calls) or not service.active)
        self.assertTrue(service.active, service.message)
        self.assertEqual(len(rate_threads), 2)  # One new preflight per attempt, no GUI reread.
        self.assertNotIn(threading.main_thread().ident, rate_threads)
        self.assertEqual(service._transport._calibration, (2e7, 200.))
        self.assertTrue(magnet.owner)
        self.assertTrue(self.manager.claimed)
        self.assertEqual(prepared[0]["planned_ranges"]["B-field"], (-.5, .5))
        self.assertEqual(set(prepared[0]["channels"]), {"Ids_DC", "Ids_X", "Ids_Y", "Ids_Keithley"})
        service.accept_transport_record(dict(B_measured_T=.1, Direction="backward", Condition_index=2,
            Index=3, Vtg=0., Vbg=0., Vds=0., Doping=0., **{"E-field": 0.}))
        self.assertEqual(records[0]["x"], .1)
        self.assertEqual(records[0]["direction"], "backward")
        self.assertEqual(records[0]["condition"], 2)
        service.stop()
        self.assertFalse(completed)
        magnet.operation_finished.emit("pause")
        self.assertTrue(any(call[0] == "persistent" for call in magnet.calls))
        self.assertFalse(completed)
        self.assertTrue(magnet.owner)
        magnet.operation_finished.emit("enter_persistent_mode")
        self.assertTrue(service.active)
        self.assertEqual(service._transport._cleanup_waiting, "configuration")
        saved_rates = {i: (10. * (i + 1), .1) for i in range(5)}
        magnet.transport_config_result.emit(dict(success=True, stored_rates=saved_rates, stored_limits=(-1., 1.)))
        self.assertTrue(service.active)
        self.assertIn(("restore", saved_rates, (-1., 1.)), magnet.calls)
        self.assertFalse(any(call[0] == "move" for call in magnet.calls))
        magnet.transport_restore_result.emit(dict(success=True))
        self.spin(lambda: not service.active)
        self.assertEqual(len(completed), 1)
        result, locks, owner = completed[0]
        self.assertFalse(locks)
        self.assertFalse(owner)
        self.assertEqual(result["status"], "stopped")
        self.assertTrue(os.path.exists(result["metadata_path"]))
        self.assertTrue(all(writer is None for writer in service._transport._writers.values()))
        service.deleteLater()

    def test_thermal_continue_requires_current_engine_permission(self):
        magnet = FakeMagnet()
        service = MeasurementService(self.manager, SaveRoot(base=self.directory.name), magnet1000=magnet)
        service.mode = 5
        with patch.object(service._transport, "continue_after_temperature_check") as resume:
            service._transport.continuation_changed.emit(True, False, "Waiting for pause acknowledgement")
            self.assertTrue(service.continuationRequired)
            self.assertFalse(service.canContinue)
            service.continueAfterTemperatureCheck()
            resume.assert_not_called()
            service._transport.continuation_changed.emit(True, True, "Temperature ready")
            self.assertTrue(service.canContinue)
            service.continueAfterTemperatureCheck()
            resume.assert_called_once()
            service._transport.continuation_changed.emit(False, False, "")
            self.assertFalse(service.continuationRequired)
        service.deleteLater()


if __name__ == "__main__":
    unittest.main()
