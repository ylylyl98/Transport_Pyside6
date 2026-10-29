import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

ColumnLayout {
    id: editor
    required property var formField
    property bool locked: false
    property string scopeKey: ""
    readonly property string fieldIdentity: scopeKey + ":" + (formField.rowKey || "") + ":" + (formField.rowIndex === undefined ? -1 : formField.rowIndex) + ":" + formField.key
    // Notify the text binding only when this value changes, rather than when
    // a sibling's refreshed descriptor replaces the containing form object.
    readonly property string committedValue: formField.value === null || formField.value === undefined ? "" : String(formField.value)
    property bool showLabel: true
    property string trailingActionText: ""
    property bool trailingActionEnabled: true
    readonly property bool readOnly: locked || Boolean(formField.readOnly)
    property bool expanding: false
    signal committed(var value)
    signal trailingActionTriggered()
    spacing: 4
    Layout.fillWidth: true
    Label {
        visible: editor.showLabel && editor.formField.kind !== "bool"
        text: editor.formField.label || editor.formField.key
        color: Theme.text
        wrapMode: Text.Wrap
        Layout.fillWidth: true
    }
    Loader {
        Layout.fillWidth: true
        sourceComponent: editor.formField.kind === "bool" ? toggleEditor : editor.formField.kind === "choice" ? choiceEditor : textEditor
    }
    Label {
        visible: text.length > 0
        text: editor.formField.error || editor.formField.description || ""
        color: editor.formField.error ? Theme.danger : Theme.muted
        font.pointSize: 9
        wrapMode: Text.Wrap
        Layout.fillWidth: true
    }
    function openExpanded(draft) {
        expanding = true
        expandedText.text = draft === undefined ? String(editor.formField.value || "") : String(draft)
        expanded.open()
        expandedText.forceActiveFocus()
    }
    Dialog {
        id: expanded
        objectName: "expandedFieldDialog"
        parent: Overlay.overlay
        anchors.centerIn: parent
        width: Math.min(parent.width - 40, 680)
        height: Math.min(parent.height - 40, 400)
        title: editor.formField.label || editor.formField.key
        modal: true
        standardButtons: editor.readOnly ? Dialog.Close : Dialog.Save | Dialog.Cancel
        onAccepted: if (!editor.readOnly) editor.committed(expandedText.text)
        onClosed: editor.expanding = false
        footer: DialogButtonBox {
            standardButtons: expanded.standardButtons
            Label {
                text: editor.readOnly ? "Read-only · select text to copy" : "Ctrl+Enter to save"
                color: Theme.muted
                padding: 8
            }
        }
        contentItem: ScrollView {
            TextArea {
                id: expandedText
                objectName: "expandedFieldText"
                selectByMouse: true
                readOnly: editor.readOnly
                wrapMode: TextEdit.WrapAnywhere
                Accessible.name: expanded.title
                Keys.onPressed: event => {
                    if (!readOnly && event.key === Qt.Key_Return && (event.modifiers & Qt.ControlModifier)) {
                        expanded.accept()
                        event.accepted = true
                    }
                }
            }
        }
    }
    Component {
        id: textEditor
        RowLayout {
        spacing: 4
        TextField {
            id: input
            Layout.fillWidth: true
            Layout.minimumWidth: 40
            objectName: "field_" + editor.formField.key
            text: { editor.fieldIdentity; return editor.committedValue }
            readOnly: editor.readOnly
            selectByMouse: true
            Accessible.name: editor.formField.label || editor.formField.key
            Accessible.description: (editor.formField.description || "") + (editor.formField.kind === "text" ? " Alt+Enter: full text." : "") + (editor.trailingActionText ? " Alt+B: " + editor.trailingActionText : "")
            placeholderText: editor.formField.kind === "text" ? "" : "Enter a value"
            onEditingFinished: {
                if (!readOnly && !editor.expanding && (editor.formField.error || text !== editor.committedValue)) editor.committed(text)
            }
            Keys.onPressed: event => {
                if (editor.trailingActionText && editor.trailingActionEnabled && !editor.locked && event.key === Qt.Key_B && (event.modifiers & Qt.AltModifier)) {
                    editor.trailingActionTriggered()
                    event.accepted = true
                }
                if (editor.formField.kind === "text" && event.key === Qt.Key_Return && (event.modifiers & Qt.AltModifier)) {
                    editor.openExpanded(text)
                    event.accepted = true
                }
            }
            background: Rectangle {
                color: input.readOnly ? Theme.background : Theme.surface
                radius: 4
                border.color: editor.formField.error ? Theme.danger : input.activeFocus ? Theme.accent : Theme.border
                border.width: input.activeFocus ? 2 : 1
            }
            color: Theme.text
        }
        ToolButton {
            objectName: "expandFieldButton"
            text: "Expand"
            focusPolicy: Qt.TabFocus
            visible: editor.formField.kind === "text" && (input.text.length > 32 || Boolean(editor.formField.expandable))
            Accessible.name: "Expand " + (editor.formField.label || editor.formField.key)
            ToolTip.visible: hovered
            ToolTip.text: "View full text (Alt+Enter)"
            onClicked: editor.openExpanded(input.text)
        }
        Button {
            text: editor.trailingActionText
            focusPolicy: Qt.TabFocus
            visible: text.length > 0
            enabled: editor.trailingActionEnabled && !editor.locked
            onClicked: editor.trailingActionTriggered()
        }
        }
    }
    Component {
        id: choiceEditor
        ComboBox {
            id: choice
            objectName: "field_" + editor.formField.key
            enabled: !editor.locked && !editor.formField.readOnly
            model: editor.formField.options || []
            wheelEnabled: false
            function labelOf(item) { return typeof item === "object" && item !== null ? item.label : String(item) }
            function valueOf(item) { return typeof item === "object" && item !== null ? item.value : item }
            currentIndex: {
                for (let i = 0; i < model.length; i++) if (valueOf(model[i]) === editor.formField.value) return i
                return -1
            }
            displayText: currentIndex >= 0 ? labelOf(model[currentIndex]) : "Choose…"
            delegate: ItemDelegate {
                required property var modelData
                required property int index
                width: choice.width
                text: choice.labelOf(modelData)
                highlighted: choice.highlightedIndex === index
            }
            Accessible.name: editor.formField.label || editor.formField.key
            onActivated: editor.committed(valueOf(model[index]))
        }
    }
    Component {
        id: toggleEditor
        CheckBox {
            id: toggle
            objectName: "field_" + editor.formField.key
            text: editor.formField.label || editor.formField.key
            checked: Boolean(editor.formField.value)
            enabled: !editor.locked && !editor.formField.readOnly
            onClicked: editor.committed(checked)
            Accessible.name: text
            contentItem: Label {
                text: toggle.text
                font: toggle.font
                color: toggle.enabled ? Theme.text : Theme.muted
                leftPadding: toggle.indicator.width + toggle.spacing
                verticalAlignment: Text.AlignVCenter
                wrapMode: Text.Wrap
            }
        }
    }
}
