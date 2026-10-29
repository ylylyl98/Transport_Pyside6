import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

if os.environ.get("TRANSPORT_OFFLINE_VERIFICATION") != "1":
    raise unittest.SkipTest("Run with verify_offline.py")

from PySide6 import QtCore, QtQml, QtWidgets, QtTest
from PySide6.QtQuickControls2 import QQuickStyle
from app.quick.application import ApplicationBackend
from app.quick.plot import register_types
from utils.config import cfg


class QuickApplicationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        QQuickStyle.setStyle("Fusion")
        register_types()

    def setUp(self):
        self.application = QtWidgets.QApplication.instance()
        self.backend = ApplicationBackend(offline=True)

    def tearDown(self):
        self.backend.requestClose()
        for _ in range(20):
            self.application.processEvents()
        if not self.backend.closeAccepted:
            self.backend._magnet.magnet1000.shutdown()
            self.backend._magnet.lakeshore335.shutdown()
            self.backend._magnet.magnet2100.shutdown()
        self.backend.deleteLater()
        self.application.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)
        self.application.processEvents()

    def test_all_measurement_and_instrument_pages_load_without_widget_views(self):
        prior_widgets = len(QtWidgets.QApplication.allWidgets())
        engine = QtQml.QQmlApplicationEngine()
        warnings, exceptions = [], []
        engine.warnings.connect(lambda items: warnings.extend(str(i) for i in items))
        engine.rootContext().setContextProperty("appBackend", self.backend)
        with patch.object(sys, "excepthook", lambda *args: exceptions.append(str(args[1]))):
            engine.load(QtCore.QUrl.fromLocalFile(str(Path(__file__).resolve().parents[1] / "app/quick/qml/Main.qml")))
            self.assertEqual(len(engine.rootObjects()), 1, warnings)
            window = engine.rootObjects()[0]
            pane = window.findChild(QtCore.QObject, "instrumentPane")
            self.assertIsNotNone(pane)
            for i in range(len(self.backend._measurement.modes)):
                self.backend._measurement.mode = i
                self.application.processEvents()
            for i in range(8):
                pane.setProperty("section", i)
                self.application.processEvents()
            self.backend._magnet.selectBackend("2100")
            self.application.processEvents()
            self.backend.dark = True
            self.application.processEvents()
            self.assertEqual(len(QtWidgets.QApplication.allWidgets()), prior_widgets)
            self.assertEqual(warnings, [])
            self.assertEqual(exceptions, [])
            window.hide()
        engine.deleteLater()
        self.application.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)

    def test_shutdown_failure_keeps_window_and_instrument_owners_available(self):
        with patch.object(self.backend._instruments.manager, "shutdown", side_effect=RuntimeError("zero unconfirmed")):
            self.backend.requestClose()
            self.application.processEvents()
        self.assertFalse(self.backend.closeAccepted)
        self.assertFalse(self.backend.closing)
        self.assertTrue(self.backend._magnet.magnet1000._thread.isRunning())
        self.assertIn("zero unconfirmed", self.backend.message)
        self.assertFalse(self.backend._updates._closed)

    def test_update_banner_preserves_stop_controls_and_requires_download_click(self):
        service = self.backend._updates
        service._current = 'v2026.10.28'
        engine = QtQml.QQmlApplicationEngine()
        warnings = []
        engine.warnings.connect(lambda items: warnings.extend(str(item) for item in items))
        engine.rootContext().setContextProperty("appBackend", self.backend)
        engine.load(QtCore.QUrl.fromLocalFile(str(Path(__file__).resolve().parents[1] / "app/quick/qml/Main.qml")))
        self.assertTrue(engine.rootObjects(), warnings)
        window = engine.rootObjects()[0]
        window.setWidth(800)
        window.setHeight(600)
        banner = window.findChild(QtCore.QObject, "updateBanner")
        self.assertFalse(banner.property('visible'))
        release_url = 'https://github.com/ylylyl98/Transport_Pyside6/releases/tag/v2026.10.29'
        with patch.object(service, '_eligible', return_value=True), patch('app.update_check.QtGui.QDesktopServices.openUrl') as opened:
            service._receive({
                'tag_name': 'v2026.10.29', 'draft': False, 'prerelease': False,
                'html_url': release_url,
                'assets': [{'name': 'Transport-Setup-windows-x64.exe',
                            'browser_download_url': release_url.replace('/tag/', '/download/') + '/Transport-Setup-windows-x64.exe'}],
            }, '')
            QtTest.QTest.qWait(100)
            self.assertTrue(banner.property('visible'))
            opened.assert_not_called()
            for name in ('stopMeasurement', 'stopAll', 'downloadUpdate', 'dismissUpdate'):
                item = window.findChild(QtCore.QObject, name)
                position = item.mapToScene(QtCore.QPointF(0, 0))
                self.assertGreaterEqual(position.x(), 0)
                self.assertGreaterEqual(position.y(), 0)
                self.assertLessEqual(position.x() + item.width(), window.width())
                self.assertLessEqual(position.y() + item.height(), window.height())
            download = window.findChild(QtCore.QObject, 'downloadUpdate')
            QtTest.QTest.mouseClick(window, QtCore.Qt.MouseButton.LeftButton,
                                   pos=download.mapToScene(QtCore.QPointF(download.width()/2, download.height()/2)).toPoint())
            opened.assert_called_once_with(QtCore.QUrl(release_url))
            dismiss = window.findChild(QtCore.QObject, 'dismissUpdate')
            QtTest.QTest.mouseClick(window, QtCore.Qt.MouseButton.LeftButton,
                                   pos=dismiss.mapToScene(QtCore.QPointF(dismiss.width()/2, dismiss.height()/2)).toPoint())
            self.assertFalse(service.available)
            self.application.processEvents()
            self.assertFalse(banner.property('visible'))
        self.assertEqual(warnings, [])
        window.hide()
        engine.deleteLater()
        self.application.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)

    def test_small_window_drawer_and_long_confirmation_keep_actions_reachable(self):
        engine = QtQml.QQmlApplicationEngine()
        warnings = []
        engine.warnings.connect(lambda items: warnings.extend(str(item) for item in items))
        engine.rootContext().setContextProperty("appBackend", self.backend)
        engine.load(QtCore.QUrl.fromLocalFile(str(Path(__file__).resolve().parents[1] / "app/quick/qml/Main.qml")))
        self.assertTrue(engine.rootObjects(), warnings)
        window = engine.rootObjects()[0]
        window.setWidth(800)
        window.setHeight(600)
        window.setProperty("showInstruments", True)
        QtTest.QTest.qWait(100)
        drawer = window.findChild(QtCore.QObject, "instrumentDrawer")
        self.assertFalse(drawer.property("modal"))
        for name in ("startMeasurement", "stopMeasurement", "stopAll"):
            item = window.findChild(QtCore.QObject, name)
            position = item.mapToScene(QtCore.QPointF(0, 0))
            self.assertGreaterEqual(position.x(), 0)
            self.assertGreaterEqual(position.y(), 0)
            self.assertLessEqual(position.x() + item.width(), window.width())
            self.assertLessEqual(position.y() + item.height(), window.height())
        stop = window.findChild(QtCore.QObject, "stopAll")
        QtTest.QTest.mouseClick(window, QtCore.Qt.MouseButton.LeftButton, pos=stop.mapToScene(QtCore.QPointF(stop.width()/2, stop.height()/2)).toPoint())
        self.assertIn("hardware commands are disabled", self.backend.message)
        self.backend.confirmationRequested.emit("layout-test", "Safety confirmation", "Review the target and heater state.\n" * 100)
        QtTest.QTest.qWait(100)
        dialog = window.findChild(QtCore.QObject, "confirmationDialog")
        scroller = window.findChild(QtCore.QObject, "confirmationScroll")
        self.assertLessEqual(dialog.property("height"), window.height() - 48)
        self.assertGreater(scroller.property("contentHeight"), scroller.property("availableHeight"))
        self.assertEqual(warnings, [])
        window.hide()
        engine.deleteLater()
        self.application.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)

    def test_expand_keeps_dirty_text_and_read_only_dialog_cannot_save(self):
        self.backend._measurement.setValue("base_name", "long measurement name " * 4)
        engine = QtQml.QQmlApplicationEngine()
        warnings = []
        engine.warnings.connect(lambda items: warnings.extend(str(item) for item in items))
        engine.rootContext().setContextProperty("appBackend", self.backend)
        engine.loadData(b"""
            import QtQuick
            import QtQuick.Controls
            import QtQuick.Layouts
            ApplicationWindow {
                id: host; width: 800; height: 600; visible: true
                property bool lockEditor: false
                ColumnLayout {
                    width: 500
                    Repeater {
                        model: appBackend.measurement.fields.filter(f => f.key === "base_name")
                        FieldEditor {
                            required property var modelData
                            formField: modelData
                            locked: host.lockEditor
                            onCommitted: value => appBackend.measurement.setValue("base_name", value)
                        }
                    }
                }
            }
        """, QtCore.QUrl.fromLocalFile(str(Path(__file__).resolve().parents[1] / "app/quick/qml/FieldEditorHarness.qml")))
        window = engine.rootObjects()[0]
        QtTest.QTest.qWait(50)
        def find_visual(name):
            def descendants(item):
                yield item
                for child in item.childItems():
                    yield from descendants(child)
            return next(item for item in descendants(window.contentItem()) if item.objectName() == name)
        input_field = find_visual("field_base_name")
        draft = "uncommitted expanded measurement name " * 3
        input_field.forceActiveFocus()
        input_field.setProperty("text", draft)
        button = find_visual("expandFieldButton")
        QtTest.QTest.mouseClick(window, QtCore.Qt.MouseButton.LeftButton, pos=button.mapToScene(QtCore.QPointF(button.width()/2, button.height()/2)).toPoint())
        QtTest.QTest.qWait(50)
        editor = input_field.parentItem().parentItem().parentItem()
        dialog = editor.findChild(QtCore.QObject, "expandedFieldDialog")
        self.assertTrue(dialog.property("visible"))
        self.assertEqual(find_visual("expandedFieldText").property("text"), draft)
        QtCore.QMetaObject.invokeMethod(dialog, "accept")
        QtTest.QTest.qWait(50)
        self.assertEqual(self.backend._measurement._values[0]["base_name"], draft)
        window.setProperty("lockEditor", True)
        input_field = find_visual("field_base_name")
        input_field.forceActiveFocus()
        QtTest.QTest.keyClick(window, QtCore.Qt.Key.Key_Return, QtCore.Qt.KeyboardModifier.AltModifier)
        QtTest.QTest.qWait(50)
        editor = input_field.parentItem().parentItem().parentItem()
        dialog = editor.findChild(QtCore.QObject, "expandedFieldDialog")
        self.assertTrue(dialog.property("visible"))
        self.assertTrue(find_visual("expandedFieldText").property("readOnly"))
        self.assertEqual(warnings, [])
        window.hide()
        engine.deleteLater()
        self.application.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)

    def test_instrument_section_and_measurement_scroll_positions_are_retained(self):
        self.backend._measurement.mode = 1
        engine = QtQml.QQmlApplicationEngine()
        engine.rootContext().setContextProperty("appBackend", self.backend)
        engine.load(QtCore.QUrl.fromLocalFile(str(Path(__file__).resolve().parents[1] / "app/quick/qml/Main.qml")))
        window = engine.rootObjects()[0]
        window.setWidth(1200)
        window.setHeight(700)
        window.setProperty("showInstruments", True)
        pane = window.findChild(QtCore.QObject, "instrumentPane")
        pane.setProperty("section", 1)
        QtTest.QTest.qWait(100)
        connections = window.findChild(QtCore.QObject, "instrumentScroller1").property("contentItem")
        connections.setProperty("contentY", 150.)
        pane.setProperty("section", 5)
        QtTest.QTest.qWait(60)
        pane.setProperty("section", 1)
        QtTest.QTest.qWait(60)
        self.assertAlmostEqual(connections.property("contentY"), 150., delta=1)
        window.setProperty("showInstruments", False)
        self.backend._measurement.mode = 1
        QtTest.QTest.qWait(100)
        parameters = window.findChild(QtCore.QObject, "parameterScroll").property("contentItem")
        def wait_for_scroll_position(expected):
            # The restoration is queued behind Qt layout/rendering. CPU-heavy
            # renderer checks may delay it beyond a fixed 100 ms sleep.
            deadline = QtCore.QDeadlineTimer(2000)
            while abs(parameters.property("contentY") - expected) > 1 and not deadline.hasExpired():
                QtTest.QTest.qWait(10)
            self.assertAlmostEqual(parameters.property("contentY"), expected, delta=1)

        parameters.setProperty("contentY", 120.)
        self.backend._measurement.mode = 0
        wait_for_scroll_position(0.)
        self.backend._measurement.mode = 1
        wait_for_scroll_position(120.)
        window.hide()
        engine.deleteLater()
        self.application.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)

    def test_optional_commissioning_limits_display_and_invalidate_review(self):
        magnet = self.backend._magnet
        with patch.object(cfg.attodry2100, "minimum_temperature_k", None):
            self.assertIn("not configured", magnet.reviewDetails)
            magnet.acknowledgeReview(True)
            self.assertTrue(magnet.review_valid())
            with patch.object(cfg.magnet, "safe_control_max_field_t", cfg.magnet.safe_control_max_field_t / 2):
                self.assertFalse(magnet.review_valid())

    def test_preview_stop_cannot_call_hardware(self):
        with patch.object(self.backend._instruments.manager, "emergency_stop") as stop:
            self.backend.emergencyStop()
        stop.assert_not_called()
        self.assertIn("disabled", self.backend.message)

    def test_telemetry_does_not_invalidate_unrelated_application_properties(self):
        notifications = QtTest.QSignalSpy(self.backend.changed)
        for _ in range(30):
            self.backend._instruments.changed.emit()
            self.backend._measurement.changed.emit()
        self.assertEqual(notifications.count(), 0)
        self.backend._instruments.sampleFieldsChanged.emit()
        self.assertEqual(notifications.count(), 1)
        self.backend._measurement.mode = 1
        self.assertGreater(notifications.count(), 1)

    def test_emergency_stop_requests_heater_off_and_all_existing_stop_paths(self):
        self.backend._offline = False
        with patch.object(self.backend._magnet.lakeshore335, "heater_off") as heater, \
             patch.object(self.backend._measurement, "stop") as acquisition, \
             patch.object(self.backend._instruments.manager, "emergency_stop") as voltages, \
             patch.object(self.backend._magnet, "request_stop") as magnet:
            self.backend.emergencyStop()
            for request in (heater, acquisition, voltages, magnet):
                request.assert_called_once_with()
        self.backend._offline = True

    def test_batch_exports_keep_per_run_metadata_after_cleanup(self):
        self.backend._offline = False
        self.backend._auto_png = True
        runs = [dict(csv_paths=["one.csv"], metadata_path="one.json"),
                dict(csv_paths=["two.csv"], metadata_path="two.json")]
        with patch.object(self.backend._exporter, "submit") as submit, \
             patch.object(self.backend._history, "refresh"):
            self.backend._completed(dict(kind="series", runs=runs, csv_paths=["one.csv", "two.csv"]))
            self.assertEqual([call.args[0] for call in submit.call_args_list],
                             [{**run, "kind": "run"} for run in runs])
        self.backend._offline = True

    def test_close_cancels_manual_actions_once_while_acquisition_drains(self):
        with patch.object(self.backend._measurement, "prepare_shutdown", return_value=False), \
             patch.object(self.backend._magnet, "request_stop") as stop:
            self.backend.requestClose()
            self.backend.requestClose()
            self.application.processEvents()
            self.backend._retry_close()
            stop.assert_called_once_with()
            self.assertFalse(self.backend.closeAccepted)
        self.backend._closing = False

    def test_preview_supervisor_rebuild_preserves_isolated_state(self):
        import transport_quick_preview
        commands = []
        def child(command, **kwargs):
            commands.append(list(command))
            return 75 if len(commands) == 1 else 0
        with patch("subprocess.call", side_effect=child):
            self.assertEqual(transport_quick_preview.main(["--mode", "2", "--panel", "4"]), 0)
        self.assertEqual(len(commands), 2)
        self.assertIn("--child", commands[0])
        self.assertNotIn("--mode", commands[1])
        self.assertNotIn("--panel", commands[1])
        self.assertEqual(commands[0][commands[0].index("--state-directory") + 1],
                         commands[1][commands[1].index("--state-directory") + 1])

    def test_close_preserves_owners_until_manual_operation_drains(self):
        magnet = self.backend._magnet
        magnet._pending.add("sample_control")
        with patch.object(magnet.magnet2100, "prepare_shutdown", return_value=True) as prepare, \
             patch.object(self.backend._instruments.manager, "shutdown") as shutdown:
            self.backend.requestClose()
            self.application.processEvents()
            self.backend._retry_close()
            shutdown.assert_not_called()
            prepare.assert_called_once_with()
            self.assertFalse(self.backend.closeAccepted)
            magnet._pending.clear()
            self.backend._retry_close()
            self.assertTrue(self.backend.closeAccepted)
            shutdown.assert_called_once_with()

    def test_unconfirmed_manual_close_cancels_without_destroying_owners(self):
        magnet = self.backend._magnet
        magnet._pending.add("sample_control")
        self.backend.requestClose()
        self.application.processEvents()
        self.backend._manual_wait_started_at -= 1000
        self.backend._retry_close()
        self.assertFalse(self.backend.closing)
        self.assertFalse(self.backend.closeAccepted)
        self.assertTrue(magnet.magnet1000._thread.isRunning())
        self.assertIn("sample_control", self.backend.message)
        magnet._pending.clear()

    def test_mode_switch_retains_each_plot_and_legacy_layout_preference(self):
        first = self.backend.plotModel
        first.setTraces([dict(x=[0., 1.], y=[1., 2.], channel="Ids_DC")])
        first.fourChannels = True
        self.backend._measurement.mode = 1
        second = self.backend.plotModel
        self.assertIsNot(first, second)
        self.assertEqual(second.pointCount, 0)
        self.backend._measurement.mode = 0
        self.assertIs(self.backend.plotModel, first)
        self.assertEqual(first.pointCount, 2)
        self.assertTrue(first.fourChannels)
        key = "quick/plots/gate_scan/ordinary"
        self.backend.settings.remove(key)
        self.backend.settings.setValue("plot_mode/gate_scan/ordinary", "4-Channel Compare")
        self.backend._plot_flavors[1] = "ordinary"
        self.backend._load_plot_preferences(1)
        self.assertTrue(second.fourChannels)
        self.assertNotIn("traces", self.backend.settings.value("quick/plots/vds_sweep/ordinary", ""))

    def test_new_run_uses_recipe_channel_and_axis_without_losing_layout_policy(self):
        plot = self.backend.plotModel
        plot.fourChannels = True
        plot.xRangeMode = "Acquired data"
        plot.xAxis = "Vbg"
        self.backend._begin_plot(dict(x_axis="Vtg", plot_choice="Ids_Y", channels=["Ids_DC", "Ids_Y"],
                                     params=dict(plot_x_axis="Follow Sweep", vds_source="None")))
        self.assertTrue(plot.fourChannels)
        self.assertEqual(plot.xRangeMode, "Acquired data")
        self.assertEqual(plot.xAxis, "Follow Sweep")
        self.assertEqual(plot.channel, "Ids_Y")
