import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

ColumnLayout {
    id: preview
    property var output: ({})
    spacing: 4
    Layout.fillWidth: true
    Label { text: "Preview filename"; font.bold: true; color: Theme.text }
    Label {
        visible: !preview.output.error && Number(preview.output.csvCount) > 1
        text: preview.output.csvCount + " CSV files" + (preview.output.csvCount > 3 ? " (first 3 shown)" : "")
        color: Theme.muted
        wrapMode: Text.Wrap
        Layout.fillWidth: true
    }
    // No elision: filenames, paths and sidecars wrap and remain selectable.
    TextArea {
        objectName: "filenamePreview"
        Layout.fillWidth: true
        text: preview.output.error || (preview.output.csvNames || []).join("\n")
        readOnly: true
        selectByMouse: true
        wrapMode: TextEdit.WrapAnywhere
        padding: 0
        color: preview.output.error ? Theme.danger : Theme.text
        background: null
        Accessible.name: "Filename preview"
    }
    Repeater {
        model: preview.output.error ? [] : [
            {label: "Output folder", value: preview.output.folder},
            {label: "Metadata / manifest", value: preview.output.metadata},
            {label: "Run log", value: preview.output.log}
        ]
        delegate: ColumnLayout {
            required property var modelData
            Layout.fillWidth: true
            spacing: 2
            Label { text: modelData.label; color: Theme.muted }
            TextArea {
                Layout.fillWidth: true
                text: modelData.value || ""
                readOnly: true
                selectByMouse: true
                wrapMode: TextEdit.WrapAnywhere
                padding: 0
                color: Theme.text
                background: null
                Accessible.name: modelData.label
            }
        }
    }
    Label {
        text: preview.output.note || ""
        color: Theme.muted
        Layout.fillWidth: true
        wrapMode: Text.Wrap
    }
}
