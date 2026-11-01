"""Exercise setup navigation and full status messages without hardware access."""
import os
from pathlib import Path
import tempfile
import time
import unittest

if os.environ.get("TRANSPORT_OFFLINE_VERIFICATION") != "1":
    raise unittest.SkipTest("Run with verify_offline.py")

from PySide6 import QtCore, QtGui, QtQml, QtQuickControls2, QtTest
from app.quick.application import ApplicationBackend
from app.quick.plot import register_types


class QuickWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.scratch = tempfile.TemporaryDirectory()
        self.addCleanup(self.scratch.cleanup)
        self.base = Path(self.scratch.name) / "not-created"
        self.backend = ApplicationBackend(offline=True)
        self.backend._instruments.setValue("base", str(self.base))
        self.backend._instruments.setValue("user", "Operator One")
        self.backend._instruments.setValue("device_id", "Sample 024")
        QtQuickControls2.QQuickStyle.setStyle("Fusion")
        register_types()
        self.engine = QtQml.QQmlApplicationEngine()
        self.warnings = []
        self.engine.warnings.connect(lambda errors: self.warnings.extend(str(e) for e in errors))
        self.engine.rootContext().setContextProperty("appBackend", self.backend)
        self.engine.loadData(b"import QtQuick\nMain { width: 1100; height: 760 }",
                             QtCore.QUrl.fromLocalFile(str(Path(__file__).resolve().parents[1]
                                                          / "app/quick/qml/WorkflowHarness.qml")))
        self.assertTrue(self.engine.rootObjects(), "\n".join(self.warnings))
        self.window = self.engine.rootObjects()[0]
        self.window.setProperty("pinInstruments", False)
        self.window.setProperty("showInstruments", False)
        self.window.requestActivate()
        QtTest.QTest.qWait(50)

    def tearDown(self):
        self.window.hide()
        self.backend.requestClose()
        end = time.monotonic() + 5
        while not self.backend.closeAccepted and time.monotonic() < end:
            QtTest.QTest.qWait(10)
        self.assertTrue(self.backend.closeAccepted)
        self.engine.deleteLater()
        QtCore.QCoreApplication.sendPostedEvents(None, QtCore.QEvent.DeferredDelete)
        self.backend.deleteLater()
        QtCore.QCoreApplication.sendPostedEvents(None, QtCore.QEvent.DeferredDelete)

    def item(self, name):
        found = self.window.findChild(QtCore.QObject, name)
        if found is None:
            def descendants(item):
                yield item
                for child in item.childItems():
                    yield from descendants(child)
            found = next((i for i in descendants(self.window.contentItem()) if i.objectName() == name), None)
        self.assertIsNotNone(found, name)
        return found

    def click(self, name):
        item = self.item(name)
        center = item.mapToScene(QtCore.QPointF(item.width() / 2, item.height() / 2)).toPoint()
        QtTest.QTest.mouseClick(self.window, QtCore.Qt.LeftButton, QtCore.Qt.NoModifier, center)
        QtTest.QTest.qWait(50)

    def test_sweep_preview_commits_current_input_and_preserves_live_data(self):
        self.window.setWidth(1536)
        self.window.setHeight(864)
        service = self.backend.measurement
        service.mode = 2
        self.backend.plotModel.setTraces([dict(x=[0., 1.], y=[2., 3.], label="Retained live data")])
        QtTest.QTest.qWait(60)
        draft = self.item("field_vtg_stop")
        draft.forceActiveFocus()
        draft.setProperty("text", "1.5")
        self.click("measurementPreviewSweep")
        end = time.monotonic() + 5
        while service.sweepPreview.busy and time.monotonic() < end:
            QtTest.QTest.qWait(10)
        self.assertEqual(service.collect_params().vtg_stop, 1.5)
        self.assertFalse(service.sweepPreview.busy)
        self.assertEqual(service.sweepPreview.error, "")
        self.assertFalse(service.sweepPreview.stale)
        self.assertTrue(self.window.property("showSweepPreview"))
        self.assertEqual(self.item("livePlotPane").property("plotModel"), service.sweepPreview.plotModel)
        self.assertEqual(self.backend.plotModel.pointCount, 2)
        self.assertFalse(self.item("livePlotPane").property("showExport"))
        self.click("liveDataTab")
        self.assertEqual(self.item("livePlotPane").property("plotModel"), self.backend.plotModel)
        self.assertEqual(self.backend.plotModel.pointCount, 2)
        self.click("sweepPreviewTab")
        for mode, xlabel in ((0, "Vds (V)"), (3, "Wavelength (nm)"), (2, "Vtg (V)")):
            service.mode = mode
            end = time.monotonic() + 5
            while (service.sweepPreview.stale or service.sweepPreview.busy) and time.monotonic() < end:
                QtTest.QTest.qWait(10)
            self.assertFalse(service.sweepPreview.stale)
            self.assertEqual(service.sweepPreview.plotModel.xLabel, xlabel)
            self.assertTrue(self.window.property("showSweepPreview"))
        self.click("liveDataTab")
        self.assertEqual(self.backend.plotModel.pointCount, 2)
        self.assertEqual(self.warnings, [])

    def test_regions_are_discoverable_and_preview_keeps_stop_visible_on_small_window(self):
        service = self.backend.measurement
        service.mode = 2
        regions = next(s for s in service.uiSections if s["title"] == "Additional gate regions")
        self.assertFalse(regions["advanced"])
        self.assertTrue(regions["configureRegions"])
        service.useGateRegions()
        QtTest.QTest.qWait(50)
        self.assertTrue(self.item("measurementAdd_regions").isVisible())
        self.window.setWidth(800)
        self.window.setHeight(600)
        QtTest.QTest.qWait(80)
        self.click("measurementPreviewSweep")
        QtTest.QTest.qWait(100)
        self.assertEqual(self.window.property("narrowPage"), 1)
        for name in ("stopAll", "startMeasurement", "stopMeasurement", "sweepPreviewTab"):
            control = self.item(name)
            origin = control.mapToScene(QtCore.QPointF())
            self.assertTrue(control.isVisible())
            self.assertGreaterEqual(origin.x(), 0)
            self.assertGreaterEqual(origin.y(), 0)
            self.assertLessEqual(origin.x() + control.width(), self.window.width() + 1)
            self.assertLessEqual(origin.y() + control.height(), self.window.height() + 1)
        service._batch_claimed = True
        service.changed.emit()
        QtTest.QTest.qWait(30)
        self.assertFalse(self.window.property("showSweepPreview"))
        self.assertFalse(self.item("sweepPreviewTab").property("enabled"))
        service._batch_claimed = False
        service.changed.emit()
        self.assertEqual(self.warnings, [])

    def test_locate_error_reopens_collapsed_setup_and_preserves_invalid_draft(self):
        self.backend._measurement.mode = 0
        self.window.setProperty("showParameters", True)
        QtTest.QTest.qWait(50)
        field = self.item("field_vds_step")
        field.forceActiveFocus()
        field.setProperty("text", "bad")
        QtTest.QTest.keyClick(self.window, QtCore.Qt.Key_Return)
        header = self.item("measurement_section_Vds trajectory")
        header.forceActiveFocus()
        QtTest.QTest.keyClick(self.window, QtCore.Qt.Key_Space)
        QtTest.QTest.qWait(30)
        self.assertFalse(field.isVisible())
        self.assertEqual(header.property("errorCount"), 1)
        self.assertNotIn("could not convert", self.backend.measurement.message)
        self.click("hideParameters")
        self.click("locateMeasurementError")
        QtTest.QTest.qWait(100)
        self.assertTrue(field.isVisible())
        self.assertTrue(field.hasActiveFocus())
        self.assertEqual(field.property("text"), "bad")
        accessible = QtGui.QAccessible.queryAccessibleInterface(field)
        self.assertIn("valid number", accessible.text(QtGui.QAccessible.Description))
        field.setProperty("text", "0.02")
        QtTest.QTest.keyClick(self.window, QtCore.Qt.Key_Return)
        QtTest.QTest.qWait(30)
        self.assertFalse(self.item("locateMeasurementError").isVisible())
        self.assertEqual(header.property("errorCount"), 0)
        self.assertTrue(self.backend.measurement.canStart)

    def test_locate_error_selects_and_focuses_a_virtualized_recipe_row(self):
        service = self.backend._measurement
        service.mode = 3
        self.window.setProperty("showParameters", True)
        for _ in range(9):
            service.addRow("bias_conditions")
        model = service.rowModel("bias_conditions")
        last = model.count - 1
        uid = model.data(model.index(last, 0), model.UidRole)
        model.rememberDraft(uid, "vtg", "bad")
        self.assertFalse(model.commit(uid, "vtg", "bad"))
        service.selectRow("bias_conditions", 0)
        QtTest.QTest.qWait(60)
        header = self.item("measurement_section_Bias recipe")
        header.forceActiveFocus()
        QtTest.QTest.keyClick(self.window, QtCore.Qt.Key_Space)
        self.click("locateMeasurementError")
        QtTest.QTest.qWait(150)
        field = self.item(f"recipeCell_bias_conditions_{uid}_vtg")
        self.assertEqual(model.selectedIndex, last)
        self.assertTrue(field.hasActiveFocus())
        self.assertEqual(field.property("text"), "bad")
        self.assertTrue(field.isVisible())

    def test_locate_error_keeps_a_hidden_parameter_editable(self):
        service = self.backend._measurement
        service.mode = 2
        service.setValue("axis_slow", "Vbg")
        service.setValue("vbg_stop", "bad")
        service.setValue("axis_slow", "None")
        self.window.setProperty("showParameters", True)
        QtTest.QTest.qWait(50)
        self.assertEqual(service.validationIssues[0]["section"], "Other invalid parameters")
        self.click("locateMeasurementError")
        QtTest.QTest.qWait(100)
        field = self.item("field_vbg_stop")
        self.assertTrue(field.isVisible())
        self.assertTrue(field.hasActiveFocus())
        field.setProperty("text", "1.25")
        QtTest.QTest.keyClick(self.window, QtCore.Qt.Key_Return)
        QtTest.QTest.qWait(30)
        self.assertEqual(service.validationIssues, [])
        self.assertFalse(self.item("locateMeasurementError").isVisible())

    def test_output_review_is_visible_and_caption_scales_with_runtime_font(self):
        self.backend._measurement.mode = 0
        self.window.setProperty("showParameters", True)
        QtQml.QQmlProperty(self.window, "width").write(800)
        QtQml.QQmlProperty(self.window, "height").write(600)
        QtTest.QTest.qWait(170)
        self.assertTrue(self.item("outputSummaryCount").isVisible())
        self.assertIn("1 CSV", self.item("outputSummaryCount").property("text"))
        self.assertIn(str(self.base), self.item("outputSummaryFolder").property("text"))
        self.assertFalse(self.base.exists(), "Reviewing output must not create measurement folders")
        self.assertTrue(self.item("field_base_name").isVisible())
        self.assertTrue(self.item("filenamePreview").isVisible())
        caption = self.item("stopFieldNotice")
        original = caption.property("font").pointSizeF()
        self.backend._set_font_size(14)
        QtTest.QTest.qWait(80)
        self.assertGreater(caption.property("font").pointSizeF(), original)
        self.assertEqual(caption.property("font").pointSizeF(), 13)
        self.assertIn("not zeroed", caption.property("text"))
        self.backend._measurement.setValue("n_sample", "bad")
        QtTest.QTest.qWait(30)
        for name in ("stopAll", "startMeasurement", "stopMeasurement", "locateMeasurementError", "showStatusDetails"):
            item = self.item(name)
            position = item.mapToScene(QtCore.QPointF())
            self.assertGreaterEqual(position.x(), 0, name)
            self.assertGreaterEqual(position.y(), 0, name)
            self.assertLessEqual(position.x() + item.width(), self.window.width(), name)
            self.assertLessEqual(position.y() + item.height(), self.window.height(), name)

    def test_setup_shortcuts_retain_pages_and_show_the_intended_destination(self):
        expected = self.base / "Operator_One" / "Sample_024"
        self.assertEqual(Path(self.backend.dataFolderPath), expected)
        self.assertFalse(self.base.exists())
        self.assertIn(str(expected), self.item("dataDestination").property("text"))
        self.click("openConnections")
        pane = self.item("instrumentPane")
        self.assertEqual(pane.property("section"), 1)
        self.assertTrue(self.window.property("showInstruments"))
        connection_page = self.item("instrumentScroller1")
        self.click("openSampleFiles")
        self.assertEqual(pane.property("section"), 5)
        editor = self.item("field_device_id")
        editor.forceActiveFocus()
        editor.setProperty("text", "Sample 025")
        QtTest.QTest.keyClick(self.window, QtCore.Qt.Key_Tab)
        QtTest.QTest.qWait(40)
        self.assertEqual(Path(self.backend.dataFolderPath), self.base / "Operator_One" / "Sample_025")
        self.assertIn("Sample_025", self.item("dataDestination").property("text"))
        self.click("openConnections")
        self.assertIs(self.item("instrumentScroller1"), connection_page)
        self.assertFalse(self.base.exists(), "Showing the save destination must not create folders")
        self.assertEqual(self.warnings, [])

    def test_instrument_navigation_groups_remember_pages_and_sync_shortcuts(self):
        self.click("openConnections")
        QtTest.QTest.qWait(300)  # Wait for the drawer to finish opening before clicking inside it.
        pane = self.item("instrumentPane")
        self.assertEqual((pane.property("section"), pane.property("currentGroup")), (1, 0))
        connection_page = self.item("instrumentScroller1")
        connection_scroll = connection_page.property("contentItem")
        connection_scroll.setProperty("contentY", 100.)
        self.click("instrumentGroup1")
        self.assertEqual(pane.property("section"), 0)
        self.click("instrumentGroup2")
        self.assertEqual(pane.property("section"), 7)
        self.click("instrumentGroup0")
        self.assertEqual(pane.property("section"), 1)
        self.assertIs(self.item("instrumentScroller1"), connection_page)
        self.assertAlmostEqual(connection_scroll.property("contentY"), 100., delta=1)
        pane.setProperty("section", 3)
        QtTest.QTest.qWait(50)
        self.assertEqual(pane.property("currentGroup"), 1)
        self.click("instrumentGroup0")
        self.click("instrumentGroup1")
        self.assertEqual(pane.property("section"), 3)
        groups = ((0, (1, 5, 6)), (1, (0, 2, 3, 4)), (2, (7,)))
        titles = {0: "Manual controls", 1: "Connections & protection", 2: "Magnet & temperature",
                  3: "Drag / primary lock-in", 4: "Drive lock-in", 5: "Sample & files",
                  6: "Signal chain", 7: "Runtime settings"}
        for group, sections in groups:
            self.click(f"instrumentGroup{group}")
            self.assertEqual(pane.property("currentGroup"), group)
            for section in sections:
                self.assertTrue(self.item(f"instrumentPage{section}").property("visible"))
                self.click(f"instrumentPage{section}")
                self.assertEqual(pane.property("section"), section)
                self.assertEqual(self.item("instrumentPageTitle").property("text"), titles[section])
                self.assertTrue(self.item(f"instrumentScroller{section}").property("visible"))
        self.click("instrumentGroup0")
        self.assertEqual(pane.property("section"), 6)
        self.click("instrumentGroup1")
        self.assertEqual(pane.property("section"), 4)
        self.click("openSampleFiles")
        self.assertEqual((pane.property("section"), pane.property("currentGroup")), (5, 0))
        editor = self.item("field_device_id")
        editor.forceActiveFocus()
        editor.setProperty("text", "Group sample")
        self.click("instrumentGroup1")
        self.click("instrumentGroup0")
        self.assertEqual(pane.property("section"), 5)
        self.assertIs(self.item("field_device_id"), editor)
        self.assertEqual(editor.property("text"), "Group sample")
        self.assertEqual(self.backend._instruments.sampleFields[1]["value"], "Group sample")
        self.assertEqual(self.warnings, [])

    def test_measurement_tabs_follow_backend_and_reject_changes_while_active(self):
        tabs = self.item("measurementMode")
        self.assertEqual(tabs.property("currentIndex"), self.backend.measurement.mode)
        # Select a visible tab through the UI, then change the model externally.
        self.click("measurementTab1")
        self.assertEqual(self.backend.measurement.mode, 1)
        self.backend.measurement.mode = 0
        QtTest.QTest.qWait(50)
        self.assertEqual(tabs.property("currentIndex"), 0)
        self.backend._measurement._batch_claimed = True
        self.backend._measurement.changed.emit()
        QtTest.QTest.qWait(50)
        self.assertFalse(tabs.property("enabled"))
        self.click("measurementTab1")
        self.assertEqual(self.backend.measurement.mode, 0)
        self.backend._measurement._batch_claimed = False
        self.backend._measurement.changed.emit()
        QtTest.QTest.qWait(50)
        self.assertTrue(tabs.property("enabled"))
        self.backend._closing = True
        self.backend.changed.emit()
        QtTest.QTest.qWait(50)
        self.assertFalse(tabs.property("enabled"))
        self.backend._closing = False
        self.backend.changed.emit()
        QtTest.QTest.qWait(50)
        self.item("measurementTab0").forceActiveFocus()
        QtTest.QTest.keyClick(self.window, QtCore.Qt.Key_Right)
        QtTest.QTest.qWait(50)
        self.assertEqual(self.backend.measurement.mode, 1)
        self.assertEqual(self.warnings, [])

    def test_focus_current_commits_measurement_editor(self):
        editor = self.item("field_vds_start")
        editor.forceActiveFocus()
        editor.setProperty("text", "0.123")
        self.assertNotEqual(self.backend._measurement._values[0]["vds_start"], 0.123)
        QtCore.QMetaObject.invokeMethod(self.item("measurementMode"), "focusCurrent")
        QtTest.QTest.qWait(50)
        self.assertEqual(self.backend._measurement._values[0]["vds_start"], 0.123)
        self.assertEqual(self.warnings, [])

    def test_measurement_tabs_reveal_all_modes_at_small_width(self):
        QtQml.QQmlProperty(self.window, "width").write(800)
        self.backend._font_size = 18
        self.backend.changed.emit()
        # Resizing and changing the font both schedule tab layout/reveal work.
        # Wait for completed frames before testing a manual scroll, so queued
        # initial-layout work cannot reset it after the simulated click.
        for _ in range(2):
            frame = QtTest.QSignalSpy(self.window.frameSwapped)
            self.window.update()
            self.assertTrue(frame.wait(2000))
        self.assertTrue(self.item("scrollMeasurementTabsRight").property("visible"))
        tabs = self.item("measurementMode")
        view = self.item("measurementTabsView")
        old_mode = self.backend.measurement.mode
        old_x = view.property("contentX")
        self.click("scrollMeasurementTabsRight")
        self.assertGreater(view.property("contentX"), old_x)
        self.assertEqual(self.backend.measurement.mode, old_mode)
        self.click("scrollMeasurementTabsLeft")
        self.assertAlmostEqual(view.property("contentX"), old_x, delta=1)
        for index in range(6):
            self.backend.measurement.mode = index
            QtTest.QTest.qWait(50)
            self.assertEqual(tabs.property("currentIndex"), index)
            tab = self.item(f"measurementTab{index}")
            x = tab.mapToItem(view, QtCore.QPointF(0, 0)).x()
            self.assertGreaterEqual(x, -1, f"mode {index} must be revealed")
            self.assertLessEqual(x + tab.width(), view.width() + 1, f"mode {index} must be revealed")
        self.assertEqual(self.warnings, [])

    def test_status_details_keyboard_escape_and_stop_stay_accessible_on_small_windows(self):
        QtQml.QQmlProperty(self.window, "width").write(800)
        QtQml.QQmlProperty(self.window, "height").write(600)
        long_message = "Review the source and protection settings.\n" * 60 + "END OF STATUS"
        self.backend.report(long_message)
        self.backend._measurement._phase = "error"
        self.backend._measurement.changed.emit()
        QtTest.QTest.qWait(50)
        self.assertEqual(self.item("measurementPhase").property("text"), "error")
        button = self.item("showStatusDetails")
        button.forceActiveFocus()
        QtTest.QTest.keyClick(self.window, QtCore.Qt.Key_Space)
        QtTest.QTest.qWait(50)
        popup = self.item("statusDetailsPopup")
        self.assertTrue(popup.property("visible"))
        self.assertFalse(popup.property("modal"))
        self.assertIn(long_message, self.item("statusDetailsText").property("text"))
        self.assertTrue(self.item("statusDetailsText").property("readOnly"))
        QtTest.QTest.keyClick(self.window, QtCore.Qt.Key_Escape)
        QtTest.QTest.qWait(40)
        self.assertFalse(popup.property("visible"))
        self.click("showStatusDetails")
        for name in ("stopAll", "startMeasurement", "stopMeasurement", "showStatusDetails", "openConnections", "openSampleFiles"):
            item = self.item(name)
            position = item.mapToScene(QtCore.QPointF(0, 0))
            self.assertGreaterEqual(position.x(), 0, name)
            self.assertGreaterEqual(position.y(), 0, name)
            self.assertLessEqual(position.x() + item.width(), self.window.width(), name)
            self.assertLessEqual(position.y() + item.height(), self.window.height(), name)
        self.click("stopAll")
        self.assertIn("hardware commands are disabled", self.backend.message)
        self.assertEqual(self.warnings, [])

    def test_measurement_actions_belong_below_plot_and_survive_narrow_pages(self):
        controls = self.item('measurementControlsRegion')
        start = self.item('startMeasurement')
        stop = self.item('stopMeasurement')
        for width, height, page in ((1536,864,1), (800,600,0), (800,600,1), (1200,600,1), (1536,864,1)):
            self.window.setWidth(width)
            self.window.setHeight(height)
            self.window.setProperty('narrowPage', page)
            QtTest.QTest.qWait(60)
            self.assertIs(self.item('startMeasurement'), start)
            self.assertIs(self.item('stopMeasurement'), stop)
            self.assertEqual(len(self.window.findChildren(QtCore.QObject, 'startMeasurement')), 1)
            self.assertTrue(controls.isVisible())
            for name in ('startMeasurement', 'stopMeasurement', 'measurementProgress', 'showStatusDetails'):
                item = self.item(name)
                self.assertTrue(item.isVisible(), name)
                corner = item.mapToScene(QtCore.QPointF(item.width(), item.height()))
                self.assertLessEqual(corner.x(), width+1, name)
                self.assertLessEqual(corner.y(), height+1, name)
            if width >= 1050:
                setup = self.item('measurementSetupRegion')
                plot = self.item('livePlotPane')
                self.assertIs(controls.parentItem(), self.item('plotControlsHost'))
                point = controls.mapToScene(QtCore.QPointF())
                self.assertAlmostEqual(point.x(), plot.mapToScene(QtCore.QPointF()).x(), delta=1)
                self.assertAlmostEqual(controls.width(), plot.width(), delta=1)
                self.assertGreaterEqual(point.y(), plot.mapToScene(QtCore.QPointF(0,plot.height())).y())
                self.assertGreater(setup.mapToScene(QtCore.QPointF(0,setup.height())).y(), point.y()+30)
            else:
                self.assertIs(controls.parentItem(), self.item('narrowControlsHost'))
        self.assertEqual(self.warnings, [])

    def test_right_panel_docks_at_125_percent_desktop_width_and_retains_editors(self):
        QtQml.QQmlProperty(self.window, "width").write(1536)
        QtQml.QQmlProperty(self.window, "height").write(864)
        plot = self.backend.plotModel
        plot.setTraces([dict(x=[0., 1., 2.], y=[1., 3., 2.], channel="Ids_DC")])
        self.click("openSampleFiles")
        QtTest.QTest.qWait(300)
        self.click("pinInstrumentPanel")
        QtTest.QTest.qWait(300)
        self.assertTrue(self.window.property("dockInstruments"))
        setup, live, dock = (self.item(name) for name in
                             ("measurementSetupRegion", "liveDataRegion", "instrumentDock"))
        setup_right = setup.mapToScene(QtCore.QPointF(setup.width(), 0)).x()
        live_left = live.mapToScene(QtCore.QPointF(0, 0)).x()
        live_right = live.mapToScene(QtCore.QPointF(live.width(), 0)).x()
        dock_left = dock.mapToScene(QtCore.QPointF(0, 0)).x()
        self.assertLess(setup_right, live_left)
        self.assertLess(live_right, dock_left)
        self.assertGreaterEqual(live.width(), 400)
        editor = self.item("field_device_id")
        editor.forceActiveFocus()
        editor.setProperty("text", "Draft sample")
        QtQml.QQmlProperty(self.window, "width").write(1100)
        QtTest.QTest.qWait(350)
        self.assertFalse(self.window.property("dockInstruments"))
        self.assertTrue(self.window.property("pinInstruments"))
        self.assertTrue(self.item("instrumentDrawer").property("visible"))
        self.assertIs(self.item("field_device_id"), editor)
        self.assertEqual(editor.property("text"), "Draft sample")
        drawer_left = self.item("instrumentRegion").mapToScene(QtCore.QPointF(0, 0)).x()
        self.assertGreaterEqual(drawer_left, setup_right)
        QtQml.QQmlProperty(self.window, "width").write(1536)
        QtTest.QTest.qWait(350)
        self.assertTrue(self.window.property("dockInstruments"))
        self.assertTrue(self.window.property("showInstruments"))
        self.assertIs(self.item("field_device_id"), editor)
        self.assertEqual(plot.pointCount, 3)
        self.click("closeInstrumentPanel")
        self.assertFalse(self.window.property("showInstruments"))
        self.click("openSampleFiles")
        self.assertTrue(self.window.property("dockInstruments"))
        self.assertIs(self.item("field_device_id"), editor)
        self.assertEqual(self.warnings, [])

    def test_resizable_setup_and_separate_log_can_be_hidden_without_losing_state(self):
        QtQml.QQmlProperty(self.window, "width").write(1536)
        self.backend._measurement.mode = 1
        QtTest.QTest.qWait(100)
        setup = self.item("measurementSetupRegion")
        width_before = setup.width()
        divider = setup.mapToScene(QtCore.QPointF(setup.width() + 4, setup.height() / 2)).toPoint()
        QtTest.QTest.mousePress(self.window, QtCore.Qt.LeftButton, pos=divider)
        QtTest.QTest.mouseMove(self.window, divider + QtCore.QPoint(55, 0), delay=40)
        QtTest.QTest.mouseRelease(self.window, QtCore.Qt.LeftButton, pos=divider + QtCore.QPoint(55, 0))
        QtTest.QTest.qWait(80)
        resized_width = setup.width()
        self.assertGreater(resized_width, width_before + 20)
        scroller = self.item("parameterScroll").property("contentItem")
        scroller.setProperty("contentY", 100.)
        self.click("hideParameters")
        self.assertFalse(setup.property("visible"))
        self.click("showParameters")
        self.assertTrue(setup.property("visible"))
        self.assertAlmostEqual(setup.width(), resized_width, delta=1)
        self.assertAlmostEqual(scroller.property("contentY"), 100., delta=1)
        QtTest.QTest.keyClick(self.window, QtCore.Qt.Key_L, QtCore.Qt.ControlModifier)
        QtTest.QTest.qWait(50)
        log, live, plot = (self.item(name) for name in
                           ("measurementLogRegion", "liveDataRegion", "livePlotPane"))
        self.assertTrue(log.property("visible"))
        self.assertAlmostEqual(log.width(), live.width(), delta=1)
        self.assertGreaterEqual(plot.height(), 100)
        self.assertLessEqual(log.y() + log.height(), live.height())
        self.click("closeMeasurementLog")
        self.assertFalse(log.property("visible"))
        QtQml.QQmlProperty(self.window, "width").write(800)
        self.window.setProperty("narrowPage", 0)
        QtTest.QTest.keyClick(self.window, QtCore.Qt.Key_L, QtCore.Qt.ControlModifier)
        QtTest.QTest.qWait(50)
        self.assertEqual(self.window.property("narrowPage"), 1)
        self.assertTrue(log.property("visible"), "Opening the log must reveal its region on narrow windows")
        self.assertEqual(self.warnings, [])
