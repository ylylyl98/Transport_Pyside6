import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import "NumberFormat.js" as NumberFormat

ColumnLayout {
    id: root
    required property var backend
    required property string axis
    required property string label
    property var range: backend.rangeState[axis]
    readonly property string rangeIdentity: backend.currentType + ":" + backend.hasMap + ":" + (axis === "y" ? backend.rangeChannel : "")
    onRangeIdentityChanged: if (lower && upper) {
        lower.dirty = false; upper.dirty = false
        // A new channel owns a different value even if the field keeps focus.
        Qt.callLater(function() {
            lower.text = lower.activeFocus ? String(root.range.bounds[0]) : NumberFormat.compact(root.range.bounds[0])
            upper.text = upper.activeFocus ? String(root.range.bounds[1]) : NumberFormat.compact(root.range.bounds[1])
        })
    }
    spacing: 2
    function apply() {
        backend.setRange(axis, mode.currentIndex === 0,
                         lower.dirty ? lower.text : String(root.range.bounds[0]),
                         upper.dirty ? upper.text : String(root.range.bounds[1]))
        if (!root.range.error) { lower.dirty = false; upper.dirty = false; syncValues() }
    }
    function syncValues() {
        if (root.range.error) return
        if (!lower.activeFocus && !lower.dirty) lower.text = NumberFormat.compact(root.range.bounds[0])
        if (!upper.activeFocus && !upper.dirty) upper.text = NumberFormat.compact(root.range.bounds[1])
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
        SelectBox {
            id: mode; objectName: "historyRange_" + root.axis + "_mode"; model: ["Auto", "Fixed"]
            currentIndex: root.range.auto ? 0 : 1; implicitWidth: 88
            onActivated: {
                lower.text = String(root.range.bounds[0])
                upper.text = String(root.range.bounds[1])
                root.apply()
            }
            Accessible.name: root.label + " range mode"
        }
        InputField {
            id: lower; objectName: "historyRange_" + root.axis + "_lower"; implicitWidth: 104
            visible: mode.currentIndex === 1; enabled: visible
            invalid: Boolean(root.range.error)
            property bool dirty: false
            selectByMouse: true
            onTextEdited: dirty = true
            onActiveFocusChanged: if (activeFocus && !dirty) text = String(root.range.bounds[0]); else if (!activeFocus && !dirty) root.syncValues()
            onEditingFinished: if (dirty) root.apply()
            Accessible.name: root.label + " lower bound"
            Accessible.description: root.range.error
            ToolTip.text: String(root.range.bounds[0]); ToolTip.visible: hovered
        }
        Label { text: "to"; visible: mode.currentIndex === 1 }
        InputField {
            id: upper; objectName: "historyRange_" + root.axis + "_upper"; implicitWidth: 104
            visible: mode.currentIndex === 1; enabled: visible
            invalid: Boolean(root.range.error)
            property bool dirty: false
            selectByMouse: true
            onTextEdited: dirty = true
            onActiveFocusChanged: if (activeFocus && !dirty) text = String(root.range.bounds[1]); else if (!activeFocus && !dirty) root.syncValues()
            onEditingFinished: if (dirty) root.apply()
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
