import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

ColumnLayout {
    id: root
    required property var backend
    required property string axis
    required property string label
    property var range: backend.rangeState[axis]
    spacing: 2
    function apply() { backend.setRange(axis, mode.currentIndex === 0, lower.text, upper.text) }
    function syncValues() {
        if (root.range.error) return
        if (!lower.activeFocus) lower.text = root.range.auto ? Number(root.range.bounds[0]).toPrecision(6) : String(root.range.bounds[0])
        if (!upper.activeFocus) upper.text = root.range.auto ? Number(root.range.bounds[1]).toPrecision(6) : String(root.range.bounds[1])
    }
    Component.onCompleted: syncValues()
    RowLayout {
        spacing: 4
        Label {
            Layout.preferredWidth: 128
            text: root.label + (root.range.unit ? " (" + root.range.unit + ")" : "")
            color: Theme.text
            wrapMode: Text.Wrap
            ToolTip.text: root.range.title; ToolTip.visible: labelHover.hovered
            HoverHandler { id: labelHover }
        }
        ComboBox {
            id: mode; objectName: "historyRange_" + root.axis + "_mode"; model: ["Auto", "Fixed"]
            currentIndex: root.range.auto ? 0 : 1; implicitWidth: 88
            onActivated: {
                lower.text = String(root.range.bounds[0])
                upper.text = String(root.range.bounds[1])
                root.apply()
            }
            Accessible.name: root.label + " range mode"
        }
        TextField {
            id: lower; objectName: "historyRange_" + root.axis + "_lower"; implicitWidth: 104
            visible: mode.currentIndex === 1; enabled: visible
            selectByMouse: true; onEditingFinished: root.apply()
            Accessible.name: root.label + " lower bound"
            Accessible.description: root.range.error
            ToolTip.text: String(root.range.bounds[0]); ToolTip.visible: hovered
        }
        Label { text: "to"; visible: mode.currentIndex === 1 }
        TextField {
            id: upper; objectName: "historyRange_" + root.axis + "_upper"; implicitWidth: 104
            visible: mode.currentIndex === 1; enabled: visible
            selectByMouse: true; onEditingFinished: root.apply()
            Accessible.name: root.label + " upper bound"
            Accessible.description: root.range.error
            ToolTip.text: String(root.range.bounds[1]); ToolTip.visible: hovered
        }
    }
    Label {
        visible: root.range.error.length > 0
        Layout.fillWidth: true; Layout.maximumWidth: 340
        text: root.label + ": " + root.range.error
        color: Theme.danger; wrapMode: Text.Wrap
        Accessible.name: text
    }
    Connections {
        target: root.backend
        function onRangeChanged() { root.syncValues() }
    }
}
