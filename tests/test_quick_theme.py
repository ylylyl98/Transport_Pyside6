"""Offline tests for shared Qt Quick control colors and interaction semantics."""
import os
from pathlib import Path
import unittest

if os.environ.get("TRANSPORT_OFFLINE_VERIFICATION") != "1":
    raise unittest.SkipTest("Run through verify_offline.py")

from PySide6 import QtCore, QtGui, QtQml, QtTest, QtWidgets
from PySide6.QtQuickControls2 import QQuickStyle


def contrast(a, b):
    def luminance(color):
        def linear(v): return v / 12.92 if v <= .04045 else ((v + .055) / 1.055) ** 2.4
        return sum(weight * linear(value) for weight, value in zip((.2126, .7152, .0722),
                   (color.redF(), color.greenF(), color.blueF())))
    x, y = sorted((luminance(a), luminance(b)))
    return (y + .05) / (x + .05)


class QuickThemeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
        QQuickStyle.setStyle("Fusion")

    def setUp(self):
        self.engine = QtQml.QQmlApplicationEngine()
        self.warnings = []
        self.engine.warnings.connect(lambda errors: self.warnings.extend(str(e) for e in errors))
        source = '''import QtQuick
import QtQuick.Controls
ApplicationWindow {
    id: window
    width: 800; height: 700; visible: true
    property bool darkMode: false
    property int clicks: 0
    property int toggles: 0
    property int selected: -1
    property int acceptedCount: 0
    property int rejectedCount: 0
    property string phaseText: "idle"
    property int controlPointSize: 10
    font.pointSize: controlPointSize
    Binding { target: Theme; property: "basePointSize"; value: window.controlPointSize }
    readonly property string mappedTone: Theme.progressTone(phaseText)
    property color disabledTextColor: Theme.disabledText
    property color disabledSurfaceColor: Theme.disabledSurface
    property color readOnlyFillColor: Theme.readOnlyFill
    ActionDialog {
        objectName: "dialog"; title: "Test"
        standardButtons: Dialog.Save | Dialog.Cancel
        onAccepted: window.acceptedCount++
        onRejected: window.rejectedCount++
        Component.onCompleted: {
            standardButton(Dialog.Save).objectName = "dialogSave"
            standardButton(Dialog.Cancel).objectName = "dialogCancel"
        }
    }
    property color textColor: Theme.text
    property color surfaceColor: Theme.surface
    property color backgroundColor: Theme.background
    property color controlSurfaceColor: Theme.controlSurface
    property color controlHoverColor: Theme.controlHover
    property color controlPressedColor: Theme.controlPressed
    property color controlBorderColor: Theme.controlBorder
    property color primaryColor: Theme.primary
    property color primaryHoverColor: Theme.primaryHover
    property color primaryPressedColor: Theme.primaryPressed
    property color primaryTextColor: Theme.primaryText
    property color dangerColor: Theme.dangerButton
    property color dangerHoverColor: Theme.dangerHover
    property color dangerPressedColor: Theme.dangerPressed
    property color dangerTextColor: Theme.dangerText
    property color progressTrackColor: Theme.progressTrack
    property color progressFillColor: Theme.progressFill
    property color progressSuccessColor: Theme.progressSuccess
    property color progressWarningColor: Theme.progressWarning
    property color progressDangerColor: Theme.progressDanger
    property color focusColor: Theme.focus
    property var coordinateColors: [Theme.coordinateDoping, Theme.coordinateEfield, Theme.coordinateVtg, Theme.coordinateVbg, Theme.coordinateVds]
    Binding { target: Theme; property: "dark"; value: window.darkMode }
    Column {
        spacing: 10
        Row {
            spacing: 8
            Button { objectName: "nativeButton"; text: "Apply" }
            ActionButton { objectName: "neutral"; text: "Apply"; hoverEnabled: true; onClicked: window.clicks++ }
            ActionButton { objectName: "primary"; text: "Apply"; tone: "primary" }
            ActionButton { objectName: "danger"; text: "Apply"; tone: "danger" }
            ActionButton { objectName: "checked"; text: "Toggle"; checkable: true }
        }
        Row {
            spacing: 8
            ToolButton { objectName: "nativeTool"; text: "Tool" }
            ActionToolButton { objectName: "tool"; text: "Tool" }
            ComboBox { objectName: "nativeSelect"; model: ["One", "Two"] }
            SelectBox { objectName: "select"; model: ["One", "Two"]; onActivated: index => window.selected = index }
        }
        Row {
            spacing: 8
            TextField { objectName: "nativeInput"; text: "Value" }
            InputField { objectName: "input"; text: "Value" }
            StateCheckBox { objectName: "check"; text: "Enabled"; onToggled: window.toggles++ }
            StateCheckBox { objectName: "tri"; text: "Partial"; tristate: true; checkState: Qt.PartiallyChecked }
            NavigationTabButton { objectName: "tab"; text: "Current page"; checked: true }
        }
        SelectBox { objectName: "editableSelect"; width: 180; editable: true; model: ["Value"]; editText: "Editable value" }
        MeasurementProgress { objectName: "progress"; width: 240; from: 0; to: 1; value: 0 }
    }
}'''
        self.engine.loadData(source.encode(), QtCore.QUrl.fromLocalFile(str(
            Path(__file__).resolve().parents[1] / "app/quick/qml/ThemeGalleryHarness.qml")))
        self.assertTrue(self.engine.rootObjects(), self.warnings)
        self.window = self.engine.rootObjects()[0]
        self.window.requestActivate()
        QtTest.QTest.qWait(100)

    def tearDown(self):
        self.window.setProperty("darkMode", False)
        self.window.hide()
        self.engine.deleteLater()
        self.app.sendPostedEvents(None, QtCore.QEvent.DeferredDelete)
        self.assertEqual(self.warnings, [])

    def item(self, name):
        item = self.window.findChild(QtCore.QObject, name)
        self.assertIsNotNone(item, name)
        return item

    def color(self, name):
        result = self.window.property(name)
        self.assertIsInstance(result, QtGui.QColor)
        return result

    def click(self, item):
        center = item.mapToScene(QtCore.QPointF(item.width()/2, item.height()/2)).toPoint()
        QtTest.QTest.mouseClick(self.window, QtCore.Qt.LeftButton, QtCore.Qt.NoModifier, center)
        QtTest.QTest.qWait(30)

    def test_color_contrast_in_light_and_dark(self):
        for dark in (False, True):
            self.window.setProperty("darkMode", dark)
            QtTest.QTest.qWait(40)
            self.assertEqual(self.item("primary").property("foreground"), self.color("primaryTextColor"))
            coordinates = self.window.property("coordinateColors").toVariant()
            self.assertEqual(len({color.name() for color in coordinates}), 5)
            for color in coordinates:
                for background in ("surfaceColor", "backgroundColor"):
                    self.assertGreaterEqual(contrast(color, self.color(background)), 4.5)
            if not dark:
                self.assertEqual(self.color("primaryTextColor").name(), "#ffffff")
            for foreground, backgrounds in (
                ("textColor", ("surfaceColor", "backgroundColor", "controlSurfaceColor", "controlHoverColor", "controlPressedColor")),
                ("primaryTextColor", ("primaryColor", "primaryHoverColor", "primaryPressedColor")),
                ("dangerTextColor", ("dangerColor", "dangerHoverColor", "dangerPressedColor")),
            ):
                for background in backgrounds:
                    with self.subTest(dark=dark, fg=foreground, bg=background):
                        self.assertGreaterEqual(contrast(self.color(foreground), self.color(background)), 4.5, f"{self.color(foreground).name()} on {self.color(background).name()}")
            for foreground, background in (("controlBorderColor", "surfaceColor"),
                                           ("controlBorderColor", "controlSurfaceColor"),
                                           ("progressFillColor", "progressTrackColor")):
                with self.subTest(dark=dark, fg=foreground, bg=background):
                    self.assertGreaterEqual(contrast(self.color(foreground), self.color(background)), 3)

    def test_preserve_standard_control_implicit_widths(self):
        for standard, styled in (("nativeButton", "neutral"), ("nativeTool", "tool"),
                                 ("nativeSelect", "select"), ("nativeInput", "input")):
            with self.subTest(control=styled):
                self.assertAlmostEqual(self.item(standard).property('implicitWidth'), self.item(styled).property('implicitWidth'), delta=1)

    def test_shared_control_heights_surfaces_and_baselines_follow_font_size(self):
        controls = ('neutral', 'primary', 'danger', 'tool', 'select', 'editableSelect', 'input', 'check', 'tri', 'tab', 'progress')
        heights = []
        for size in (10, 14, 18):
            self.window.setProperty('controlPointSize', size)
            QtTest.QTest.qWait(60)
            expected = self.item('neutral').property('height')
            heights.append(expected)
            baseline = self.item('neutral').property('baselineOffset')
            for name in controls:
                item = self.item(name)
                with self.subTest(size=size, name=name):
                    self.assertAlmostEqual(item.property('height'), expected, delta=1)
                    if name not in ('check', 'tri'):
                        background = item.property('background')
                        self.assertAlmostEqual(background.property('y'), 0, delta=.1)
                        self.assertAlmostEqual(background.property('height'), expected, delta=1)
                    if name != 'progress':
                        self.assertAlmostEqual(item.property('baselineOffset'), baseline, delta=1)
                    if name in ('check', 'tri'):
                        indicator = item.property('indicator')
                        self.assertEqual(indicator.property('height'), 14)
                        self.assertAlmostEqual(indicator.property('y') + 7, expected/2, delta=1)
            for name in ('neutral', 'tool', 'select', 'input'):
                item = self.item(name)
                item.setProperty('enabled', False)
                self.assertAlmostEqual(item.property('height'), expected, delta=1)
                item.setProperty('enabled', True)
        self.assertLess(heights[0], heights[1])
        self.assertLess(heights[1], heights[2])

    def test_button_mouse_keyboard_states_and_focus(self):
        button = self.item("neutral")
        background = button.property("background")
        initial = background.property("color")
        center = button.mapToScene(QtCore.QPointF(button.width()/2, button.height()/2)).toPoint()
        QtTest.QTest.mouseMove(self.window, center)
        QtTest.QTest.qWait(30)
        self.assertTrue(button.property("hovered"))
        self.assertNotEqual(background.property("color"), initial)
        QtTest.QTest.mousePress(self.window, QtCore.Qt.LeftButton, pos=center)
        self.assertTrue(button.property("down"))
        self.assertNotEqual(background.property("color"), initial)
        QtTest.QTest.mouseRelease(self.window, QtCore.Qt.LeftButton, pos=center)
        self.assertEqual(self.window.property("clicks"), 1)
        button.forceActiveFocus()
        QtTest.QTest.keyClick(self.window, QtCore.Qt.Key_Space)
        self.assertEqual(self.window.property("clicks"), 2)
        self.assertTrue(button.property("activeFocus"))
        self.assertTrue(any(child.property("visible")
                            for child in background.childItems()), "Focus outline must be visible independently")
        button.setProperty("enabled", False)
        self.click(button)
        self.assertEqual(self.window.property("clicks"), 2)
        checked = self.item("checked")
        self.click(checked)
        self.assertTrue(checked.property("checked"))
        self.assertTrue(checked.property("emphasized"))

    def test_checkbox_tristate_and_keyboard(self):
        checkbox = self.item("check")
        checkbox.forceActiveFocus()
        QtTest.QTest.keyClick(self.window, QtCore.Qt.Key_Space)
        self.assertTrue(checkbox.property("checked"))
        self.assertEqual(self.window.property("toggles"), 1)
        tri = self.item("tri")
        self.assertEqual(tri.property("checkState"), QtCore.Qt.PartiallyChecked)
        tri.forceActiveFocus()
        QtTest.QTest.keyClick(self.window, QtCore.Qt.Key_Space)
        self.assertNotEqual(tri.property("checkState"), QtCore.Qt.PartiallyChecked)

    def test_dialog_standard_button_signals_and_tones(self):
        dialog = self.item("dialog")
        QtCore.QMetaObject.invokeMethod(dialog, "open")
        QtTest.QTest.qWait(80)
        save, cancel = self.item("dialogSave"), self.item("dialogCancel")
        self.assertEqual(save.property("tone"), "primary")
        self.assertEqual(cancel.property("tone"), "neutral")
        self.click(save)
        self.assertEqual(self.window.property("acceptedCount"), 1)
        QtCore.QMetaObject.invokeMethod(dialog, "open")
        QtTest.QTest.qWait(80)
        self.click(cancel)
        self.assertEqual(self.window.property("rejectedCount"), 1)

    def test_progress_tone_contrast_and_phase_mapping(self):
        progress = self.item("progress")
        progress.setProperty("value", .5)
        for dark in (False, True):
            self.window.setProperty("darkMode", dark)
            QtTest.QTest.qWait(40)
            for tone in ("primary", "success", "warning", "danger"):
                with self.subTest(dark=dark, tone=tone):
                    progress.setProperty("tone", tone)
                    fill, text = progress.property("fillColor"), progress.property("fillTextColor")
                    expected = {"primary": "progressFillColor", "success": "progressSuccessColor",
                                "warning": "progressWarningColor", "danger": "progressDangerColor"}[tone]
                    self.assertEqual(fill, self.color(expected))
                    self.assertEqual(text, self.color("primaryTextColor"))
                    self.assertGreaterEqual(contrast(text, fill), 4.5, f"{text.name()} on {fill.name()}")
                    self.assertGreaterEqual(contrast(fill, self.color("progressTrackColor")), 3,
                                            f"{fill.name()} on {self.color('progressTrackColor').name()}")
            self.assertGreaterEqual(contrast(self.color("disabledTextColor"), self.color("disabledSurfaceColor")), 4.5)
            self.assertGreaterEqual(contrast(self.color("textColor"), self.color("readOnlyFillColor")), 4.5)
        for phase, tone in (("error", "danger"), ("failed", "danger"), ("complete", "success"),
                            ("finished", "success"), ("cleanup", "warning"), ("stopping", "warning"),
                            ("stopped", "warning"), ("paused", "warning"), ("running", "primary")):
            self.window.setProperty("phaseText", phase)
            self.assertEqual(self.window.property("mappedTone"), tone)

    def test_progress_range_indeterminate_and_theme_updates(self):
        progress = self.item("progress")
        for value, text in ((0, "0%"), (.5, "50%"), (1, "100%")):
            progress.setProperty("value", value)
            self.assertEqual(progress.property("progressText"), text)
        progress.setProperty("from", 10)
        progress.setProperty("to", 20)
        progress.setProperty("value", 15)
        self.assertEqual(progress.property("progressText"), "50%")
        progress.setProperty("indeterminate", True)
        self.assertEqual(progress.property("progressText"), "Working…")
        light = progress.property("fillColor")
        self.window.setProperty("darkMode", True)
        self.app.processEvents()
        self.assertNotEqual(progress.property("fillColor"), light)
        self.assertEqual(progress.property("fillColor"), self.color("progressFillColor"))
        progress.setProperty("indeterminate", False)
        QtTest.QTest.qWait(50)
        track = progress.property("contentItem")
        fill = next(item for item in track.childItems() if item.property("color") == progress.property("fillColor"))
        self.assertAlmostEqual(fill.x(), 0, delta=1)
        self.assertAlmostEqual(fill.width(), track.width() * .5, delta=1)
