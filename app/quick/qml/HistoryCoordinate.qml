import QtQuick
import QtQuick.Controls

SelectBox {
    id: control
    required property var backend
    objectName: "historyCutCoordinate"
    editable: true
    textRole: "label"
    valueRole: "value"
    model: backend.measuredCoordinateOptions
    property bool dirty: false
    property string draft: ""
    property bool invalid: false
    palette.text: invalid ? Theme.danger : Theme.text
    readonly property bool editing: contentItem && contentItem.activeFocus
    readonly property string coordinateIdentity: JSON.stringify([backend.folder, backend.currentType, backend.state.primary_path, backend.state.fixed_axis])
    onCoordinateIdentityChanged: {
        dirty = false; invalid = false
        Qt.callLater(syncText)
    }
    currentIndex: {
        const values = model
        for (let i = 0; i < values.length; ++i)
            if (values[i].value === backend.state.fixed_value) return i
        return -1
    }
    function syncText() {
        editText = dirty ? draft : editing ? backend.cutCoordinateExact : backend.cutCoordinateLabel
    }
    onEditingChanged: syncText()
    onCurrentIndexChanged: Qt.callLater(syncText)
    Component.onCompleted: syncText()
    onActivated: {
        // A displayed label is never parsed back into the measured coordinate.
        dirty = false; invalid = false
        backend.setOption("fixed_value", currentValue)
        syncText()
    }
    onAccepted: {
        if (!dirty) return
        if (!/^[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?$/.test(draft.trim()) || !isFinite(Number(draft))) {
            invalid = true
            syncText()
            return
        }
        const value = draft
        dirty = false; invalid = false
        backend.setOption("fixed_value", value)
        syncText()
    }
    Keys.onEscapePressed: {
        dirty = false; invalid = false
        syncText()
    }
    Connections {
        target: control.contentItem
        function onTextEdited() {
            // The TextField emits before ComboBox updates its editText.
            control.draft = control.contentItem.text
            control.dirty = true
            control.invalid = false
        }
    }
    Connections {
        target: control.backend
        function onChanged() { Qt.callLater(control.syncText) }
    }
    Accessible.name: "Exact measured cut coordinate"
    Accessible.description: invalid ? "Enter a finite coordinate, then press Enter" : "Full precision is shown while editing. Press Enter to apply."
    ToolTip.text: invalid ? "Enter a finite coordinate" : backend.cutCoordinateExact
    ToolTip.visible: hovered
}
