import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

Pane {
    id: card
    required property var measurement
    required property var formField
    property bool expanded: true
    property int errorCount: 0
    readonly property string identity: measurement.mode + ":" + (formField.rowKey || "") + ":" + (formField.rowIndex === undefined ? -1 : formField.rowIndex)
    property string draftName: String(formField.value || "")
    property var output: ({})
    readonly property bool dirty: draftName !== String(formField.value || "")
    signal toggleRequested()
    signal committed(var value)
    padding: 8
    Layout.fillWidth: true
    background: Rectangle { color: Theme.surface; border.color: Theme.border; radius: 4 }
    function refreshPreview() { output = measurement.previewOutputDraft(formField, draftName) }
    function updateDraft(value) { draftName = value; previewTimer.restart() }
    onIdentityChanged: { draftName = String(formField.value || ""); previewTimer.restart() }
    Component.onCompleted: refreshPreview()
    Timer { id: previewTimer; interval: 120; onTriggered: card.refreshPreview() }
    Connections {
        target: card.measurement
        function onOutputPreviewChanged() { previewTimer.restart() }
        function onUiSectionsChanged() { previewTimer.restart() }
    }
    contentItem: ColumnLayout {
        spacing: 8
        ActionToolButton {
            id: header
            objectName: "measurement_section_File output"
            readonly property int errorCount: card.errorCount
            Layout.fillWidth: true
            subtle: true
            padding: 4
            Accessible.name: "File output" + (card.errorCount ? "; " + card.errorCount + " errors" : "")
            Accessible.description: card.expanded ? "Expanded; activate to collapse" : "Collapsed; activate to edit the filename and review output"
            onClicked: card.toggleRequested()
            contentItem: RowLayout {
                spacing: 8
                Label { text: (card.expanded ? "−  " : "+  ") + "File output"; font.bold: true; color: Theme.text }
                Label {
                    visible: !card.expanded
                    text: card.formField.value || "Default name"
                    color: Theme.muted
                    elide: Text.ElideMiddle
                    Layout.fillWidth: true
                    Layout.minimumWidth: 0
                }
                Item { visible: card.expanded; Layout.fillWidth: true }
                Label {
                    objectName: "outputSummaryCount"
                    text: card.output.error ? "Check parameters" : (card.output.csvCount || 0) + " CSV" + (Number(card.output.csvCount) === 1 ? " file" : " files")
                    color: card.output.error || card.errorCount ? Theme.danger : Theme.muted
                    font.pointSize: Theme.captionPointSize
                }
            }
        }
        ColumnLayout {
            visible: card.expanded
            Layout.fillWidth: true
            spacing: 8
            Label {
                objectName: "filenameEditingScope"
                visible: Boolean(card.formField.rowKey)
                text: "Editing " + (card.output.editingScope || "condition " + (Number(card.formField.rowIndex) + 1)) + " · preview at first field target"
                color: Theme.accent
                wrapMode: Text.Wrap
                Layout.fillWidth: true
            }
            FieldEditor {
                formField: card.formField
                scopeKey: card.identity
                locked: card.measurement.active
                filenameOutput: card.output
                onDraftChanged: value => card.updateDraft(value)
                onCommitted: value => card.committed(value)
            }
            RowLayout {
                Layout.fillWidth: true
                Label { text: "Final CSV filename preview"; color: Theme.muted; Layout.fillWidth: true; wrapMode: Text.Wrap }
                ActionToolButton {
                    objectName: "copyFilenamePreview"
                    text: "Copy"
                    enabled: !card.output.error && (card.output.csvNames || []).length > 0
                    Accessible.name: "Copy complete preview filenames"
                    onClicked: {
                        previewTimer.stop()
                        card.refreshPreview()
                        if (!card.output.error) { filenamePreview.selectAll(); filenamePreview.copy(); filenamePreview.deselect() }
                    }
                }
            }
            FontMetrics { id: previewMetrics; font: filenamePreview.font }
            ScrollView {
                objectName: "filenamePreviewScroll"
                Layout.fillWidth: true
                Layout.minimumWidth: 0
                Layout.preferredHeight: Math.min(filenamePreview.implicitHeight, previewMetrics.height * 3 + 4)
                Layout.minimumHeight: previewMetrics.height + 4
                contentWidth: availableWidth
                clip: true
                ScrollBar.horizontal.policy: ScrollBar.AlwaysOff
                TextArea {
                    id: filenamePreview
                    objectName: "filenamePreview"
                    text: card.output.error || (card.output.csvNames || []).join("\n")
                    readOnly: true
                    selectByMouse: true
                    wrapMode: TextEdit.WrapAnywhere
                    padding: 0
                    color: card.output.error ? Theme.danger : Theme.text
                    background: null
                    Accessible.name: "Final CSV filename preview"
                }
            }
            Label {
                objectName: "filenameDraftNotice"
                text: card.dirty ? "Draft preview · Enter or leave the field to apply" : "{timestamp} and any collision suffix are assigned on Start."
                color: card.dirty ? Theme.warning : Theme.muted
                font.pointSize: Theme.captionPointSize
                wrapMode: Text.Wrap
                Layout.fillWidth: true
            }
            Label {
                objectName: "outputSummaryFolder"
                visible: !card.output.error
                text: "Folder: " + (card.output.folder || "")
                color: Theme.muted
                font.pointSize: Theme.captionPointSize
                wrapMode: Text.WrapAnywhere
                Layout.fillWidth: true
                Accessible.name: text
            }
            ActionToolButton {
                id: details
                objectName: "reviewOutput"
                text: checked ? "Hide details" : "Details"
                checkable: true
                Accessible.description: "Show full filenames, output folder, metadata and run log"
            }
            Loader {
                Layout.fillWidth: true
                active: card.expanded && details.checked
                visible: active
                sourceComponent: OutputPreview { output: card.output }
            }
        }
    }
}
