import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import "NumberFormat.js" as NumberFormat

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
    property var filenameOutput: null
    property string inlineFilenameDraft: committedValue
    property int filenameRefresh: 0
    signal committed(var value)
    signal draftChanged(string value)
    signal trailingActionTriggered()
    spacing: 4
    Layout.fillWidth: true
    Label {
        visible: editor.showLabel && editor.formField.kind !== "bool"
        text: editor.formField.label || editor.formField.key
        color: Theme.coordinateColor(editor.formField.coordinate || "")
        wrapMode: Text.Wrap
        Layout.fillWidth: true
    }
    Loader {
        Layout.fillWidth: true
        sourceComponent: editor.formField.kind === "bool" ? toggleEditor : editor.formField.kind === "choice" ? choiceEditor : editor.formField.key === "base_name" ? filenameEditor : textEditor
    }
    Label {
        visible: text.length > 0
        text: editor.formField.error || editor.formField.description || ""
        color: editor.formField.error ? Theme.danger : Theme.muted
        font.pointSize: Theme.captionPointSize
        wrapMode: Text.Wrap
        Layout.fillWidth: true
    }
    function openExpanded(draft) {
        expanding = true
        expandedText.text = draft === undefined ? String(editor.formField.value || "") : String(draft)
        expanded.open()
        expandedText.forceActiveFocus()
    }
    ActionDialog {
        id: expanded
        objectName: "expandedFieldDialog"
        parent: Overlay.overlay
        anchors.centerIn: parent
        width: Math.min(parent.width - 40, 680)
        height: Math.min(parent.height - 40, editor.filenameOutput === null ? 400 : 620)
        title: editor.formField.label || editor.formField.key
        modal: true
        standardButtons: editor.readOnly ? Dialog.Close : Dialog.Save | Dialog.Cancel
        onAccepted: if (!editor.readOnly) {
            editor.committed(expandedText.text)
            if (editor.formField.key === "base_name") editor.filenameRefresh++
        }
        onRejected: if (editor.formField.key === "base_name") editor.draftChanged(editor.inlineFilenameDraft)
        onClosed: {
            editor.expanding = false
        }
        footer: ActionDialogButtonBox {
            standardButtons: expanded.standardButtons
            Label {
                text: editor.readOnly ? "Read-only · select text to copy" : "Ctrl+Enter to save"
                color: Theme.muted
                padding: 8
            }
        }
        contentItem: ScrollView {
            id: expandedScroll
            clip: true
            contentWidth: availableWidth
            ScrollBar.horizontal.policy: ScrollBar.AlwaysOff
            ColumnLayout {
                width: expandedScroll.availableWidth
                TextArea {
                    id: expandedText
                    objectName: "expandedFieldText"
                    selectByMouse: true
                    readOnly: editor.readOnly
                    wrapMode: TextEdit.WrapAnywhere
                    Layout.fillWidth: true
                    Layout.minimumHeight: 120
                    onTextChanged: if (editor.expanding && editor.formField.key === "base_name") editor.draftChanged(text)
                    Accessible.name: expanded.title
                    Keys.onPressed: event => {
                        if (!readOnly && event.key === Qt.Key_Return && (event.modifiers & Qt.ControlModifier)) {
                            expanded.accept()
                            event.accepted = true
                        }
                    }
                }
                Loader {
                    Layout.fillWidth: true
                    active: expanded.visible && editor.filenameOutput !== null
                    visible: active
                    sourceComponent: OutputPreview { output: editor.filenameOutput }
                }
            }
        }
    }
    Component {
        id: filenameEditor
        ColumnLayout {
            spacing: 4
            FontMetrics { id: filenameMetrics; font: filenameInput.font }
            ScrollView {
                Layout.fillWidth: true
                Layout.minimumWidth: 0
                Layout.preferredHeight: Math.min(filenameInput.implicitHeight, filenameMetrics.height * 5 + 12)
                Layout.minimumHeight: filenameMetrics.height + 12
                clip: true
                contentWidth: availableWidth
                ScrollBar.horizontal.policy: ScrollBar.AlwaysOff
                TextArea {
                    id: filenameInput
                    objectName: "field_" + editor.formField.key
                    text: { editor.fieldIdentity; editor.filenameRefresh; return editor.committedValue }
                    onTextChanged: { editor.inlineFilenameDraft = text; editor.draftChanged(text) }
                    readOnly: editor.readOnly
                    selectByMouse: true
                    wrapMode: TextEdit.WrapAnywhere
                    padding: 6
                    color: Theme.text
                    Accessible.name: editor.formField.label || editor.formField.key
                    Accessible.description: (editor.formField.error ? "Error: " + editor.formField.error + ". " : "") + "Wraps the complete filename. Enter: apply. Alt+Enter: expand."
                    function applyDraft() {
                        if (!readOnly && !editor.expanding && (editor.formField.error || text !== editor.committedValue)) editor.committed(text)
                    }
                    onActiveFocusChanged: if (!activeFocus) applyDraft()
                    Keys.onPressed: event => {
                        if (event.key === Qt.Key_Tab || event.key === Qt.Key_Backtab) {
                            const forward = event.key === Qt.Key_Tab && !(event.modifiers & Qt.ShiftModifier)
                            const target = nextItemInFocusChain(forward)
                            if (target) target.forceActiveFocus(forward ? Qt.TabFocusReason : Qt.BacktabFocusReason)
                            event.accepted = true
                        } else if (event.key === Qt.Key_Return || event.key === Qt.Key_Enter) {
                            if (event.modifiers & Qt.AltModifier) editor.openExpanded(text)
                            else applyDraft()
                            event.accepted = true
                        }
                    }
                    background: Rectangle {
                        color: filenameInput.readOnly ? Theme.readOnlyFill : Theme.surface
                        radius: 4
                        border.color: editor.formField.error ? Theme.danger : filenameInput.activeFocus ? Theme.focus : Theme.controlBorder
                        border.width: filenameInput.activeFocus ? 2 : 1
                    }
                }
            }
            ActionToolButton {
                objectName: "expandFieldButton"
                text: "Expand"
                Layout.alignment: Qt.AlignRight
                focusPolicy: Qt.TabFocus
                Accessible.name: "Expand file base name"
                ToolTip.visible: hovered
                ToolTip.text: "View full filename (Alt+Enter)"
                onClicked: editor.openExpanded(filenameInput.text)
            }
        }
    }
    Component {
        id: textEditor
        RowLayout {
        spacing: 4
        InputField {
            id: input
            Layout.fillWidth: true
            Layout.minimumWidth: 40
            objectName: "field_" + editor.formField.key
            property bool dirty: false
            property string draftText: ""
            property string identity: editor.fieldIdentity
            onIdentityChanged: { dirty = false; draftText = "" }
            text: dirty ? draftText : (activeFocus || (editor.formField.kind !== "number" && editor.formField.kind !== "integer") ? editor.committedValue : NumberFormat.compact(editor.committedValue))
            onTextEdited: { draftText = text; dirty = true }
            ToolTip.visible: hovered && (editor.formField.kind === "number" || editor.formField.kind === "integer")
            ToolTip.text: editor.committedValue
            readOnly: editor.readOnly
            selectByMouse: true
            Accessible.name: editor.formField.label || editor.formField.key
            Accessible.description: (editor.formField.error ? "Error: " + editor.formField.error + ". " : "") + (editor.formField.description || "") + (editor.formField.kind === "text" ? " Alt+Enter: full text." : "") + (editor.trailingActionText ? " Alt+B: " + editor.trailingActionText : "")
            placeholderText: editor.formField.kind === "text" ? "" : "Enter a value"
            onEditingFinished: {
                if (!readOnly && !editor.expanding && dirty) {
                    editor.committed(draftText)
                    if (!editor.formField.error) dirty = false
                }
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
                color: input.readOnly ? Theme.readOnlyFill : Theme.surface
                radius: 4
                border.color: editor.formField.error ? Theme.danger : input.activeFocus ? Theme.focus : Theme.controlBorder
                border.width: input.activeFocus ? 2 : 1
            }
            color: Theme.text
        }
        ActionToolButton {
            objectName: "expandFieldButton"
            text: "Expand"
            focusPolicy: Qt.TabFocus
            visible: editor.formField.kind === "text" && (input.text.length > 32 || Boolean(editor.formField.expandable))
            Accessible.name: "Expand " + (editor.formField.label || editor.formField.key)
            ToolTip.visible: hovered
            ToolTip.text: "View full text (Alt+Enter)"
            onClicked: editor.openExpanded(input.text)
        }
        ActionButton {
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
        SelectBox {
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
            Accessible.description: editor.formField.error || editor.formField.description || ""
        }
    }
    Component {
        id: toggleEditor
        StateCheckBox {
            id: toggle
            objectName: "field_" + editor.formField.key
            text: editor.formField.label || editor.formField.key
            checked: Boolean(editor.formField.value)
            enabled: !editor.locked && !editor.formField.readOnly
            onClicked: editor.committed(checked)
            Accessible.name: text
            Accessible.description: editor.formField.error || editor.formField.description || ""
            contentItem: Label {
                text: toggle.text
                font: toggle.font
                color: toggle.enabled ? Theme.text : Theme.disabledText
                leftPadding: toggle.indicator.width + toggle.spacing
                verticalAlignment: Text.AlignVCenter
                wrapMode: Text.Wrap
            }
        }
    }
}
