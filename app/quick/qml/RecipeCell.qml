import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

ColumnLayout {
    id: cell
    required property var rowModel
    required property string uid
    required property var spec
    required property var owner
    property bool locked: false
    property bool compact: false
    readonly property string acceptedValue: spec.value === undefined || spec.value === null ? "" : String(spec.value)
    readonly property bool readOnly: locked || Boolean(spec.readOnly)
    readonly property string displayValue: {
        // Format calculated coordinates only; editable values and their drafts retain
        // their original text and precision even when the form is locked.
        if (spec.readOnly && ["vtg", "vbg", "doping", "efield"].indexOf(spec.key) >= 0
                && typeof spec.value === "number" && isFinite(spec.value)) {
            const rounded = spec.value.toFixed(6).replace(/\.?0+$/, "")
            return rounded === "" || rounded === "-0" ? "0" : rounded
        }
        return acceptedValue
    }
    spacing: 2
    function commit() { return inputLoader.item && inputLoader.item.applyDraft ? inputLoader.item.applyDraft() : true }
    Component.onDestruction: if (cell.owner && cell.owner.activeEditor === cell) cell.owner.activeEditor = null
    Label {
        visible: !cell.compact && cell.spec.kind !== "bool"
        text: (cell.spec.error ? "Invalid: " : "") + (cell.spec.label || cell.spec.key || "")
        color: cell.spec.error ? Theme.danger : cell.spec.coordinate ? Theme.coordinateColor(cell.spec.coordinate) : Theme.muted
        Layout.fillWidth: true
        elide: Text.ElideRight
        ToolTip.visible: labelHover.hovered
        ToolTip.text: text + (cell.spec.error ? "\n" + cell.spec.error : "")
        HoverHandler { id: labelHover }
    }
    Loader {
        id: inputLoader
        Layout.fillWidth: true
        Layout.minimumWidth: 0
        sourceComponent: cell.spec.kind === "bool" ? toggle : cell.spec.kind === "choice" ? choice : input
    }
    Component {
        id: input
        InputField {
            id: field
            objectName: "recipeCell_" + cell.owner.rowKey + "_" + cell.uid + "_" + cell.spec.key
            // A recycled delegate always reads the draft belonging to this UID.
            text: { cell.uid; return cell.spec.readOnly ? cell.displayValue : cell.spec.draft === undefined ? cell.acceptedValue : String(cell.spec.draft) }
            property var editModel: null
            property string editUid: ""
            property string editKey: ""
            readOnly: cell.readOnly
            selectByMouse: true
            color: Theme.text
            Accessible.name: cell.spec.label || cell.spec.key
            Accessible.description: cell.spec.error || (readOnly ? "Read-only calculated value" : "Enter or Tab to apply")
            function applyDraft() {
                if (!editModel || readOnly || (text === cell.acceptedValue && cell.spec.draft === undefined && !cell.spec.error)) return true
                return editModel.commit(editUid, editKey, text)
            }
            onActiveFocusChanged: {
                if (activeFocus) {
                    editModel = cell.rowModel; editUid = cell.uid; editKey = cell.spec.key
                    cell.owner.selectVisible(editUid)
                    cell.owner.activeEditor = cell
                } else applyDraft()
            }
            onTextEdited: if (editModel && !readOnly) editModel.rememberDraft(editUid, editKey, text)
            onEditingFinished: applyDraft()
            background: Rectangle {
                color: field.readOnly ? Theme.readOnlyFill : Theme.surface
                radius: 3
                border.width: field.activeFocus ? 2 : 1
                border.color: cell.spec.error ? Theme.danger : field.activeFocus ? Theme.focus : Theme.controlBorder
            }
            ToolTip.visible: hovered && Boolean(cell.spec.error)
            ToolTip.text: cell.spec.error || ""
        }
    }
    Component {
        id: toggle
        StateCheckBox {
            objectName: "recipeCell_" + cell.owner.rowKey + "_" + cell.uid + "_" + cell.spec.key
            text: cell.spec.key === "enabled" ? (checked ? "On" : "Off") : cell.spec.label || cell.spec.key
            checked: Boolean(cell.spec.value)
            enabled: !cell.readOnly
            Accessible.name: (cell.spec.label || cell.spec.key) + (checked ? "; On" : "; Off")
            onClicked: { cell.owner.selectVisible(cell.uid); cell.rowModel.commit(cell.uid, cell.spec.key, checked) }
        }
    }
    Component {
        id: choice
        SelectBox {
            id: combo
            objectName: "recipeCell_" + cell.owner.rowKey + "_" + cell.uid + "_" + cell.spec.key
            model: cell.spec.options || []
            enabled: !cell.readOnly
            wheelEnabled: false
            function valueOf(item) { return typeof item === "object" && item !== null ? item.value : item }
            function labelOf(item) { return typeof item === "object" && item !== null ? item.label : String(item) }
            currentIndex: {
                for (let i = 0; i < model.length; ++i) if (valueOf(model[i]) === cell.spec.value) return i
                return -1
            }
            displayText: currentIndex < 0 ? "Choose…" : labelOf(model[currentIndex])
            delegate: ItemDelegate {
                required property var modelData
                required property int index
                width: combo.width
                text: combo.labelOf(modelData)
                highlighted: combo.highlightedIndex === index
            }
            Accessible.name: cell.spec.label || cell.spec.key
            onActivated: { cell.owner.selectVisible(cell.uid); cell.rowModel.commit(cell.uid, cell.spec.key, valueOf(model[index])) }
        }
    }
}
