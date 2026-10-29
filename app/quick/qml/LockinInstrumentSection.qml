import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

ColumnLayout {
    id: section
    required property var lockin
    property bool drive: false
    property bool measurementActive: false
    Layout.fillWidth: true
    spacing: 14
    ColumnLayout {
        Layout.fillWidth: true; spacing: 6
        Label { text: section.drive ? "Drive lock-in · drive-current reference" : "Primary / Drag lock-in · measured signal"; font.bold: true; color: Theme.text; wrapMode: Text.Wrap; Layout.fillWidth: true }
        Label { text: "Read settings to refresh the instrument. Edited fields remain pending until Apply settings verifies readback."; color: Theme.muted; wrapMode: Text.Wrap; Layout.fillWidth: true }
        Label { text: section.lockin.identity; color: Theme.muted; wrapMode: Text.Wrap; Layout.fillWidth: true }
        GridLayout {
            columns: 2; columnSpacing: 16; rowSpacing: 4; Layout.fillWidth: true
            Repeater {
                model: Object.keys(section.lockin.readouts)
                delegate: Label { required property string modelData; text: modelData.toUpperCase() + ": " + section.lockin.readouts[modelData]; color: Theme.text; Layout.fillWidth: true; wrapMode: Text.Wrap }
            }
        }
        Flow {
            Layout.fillWidth: true; spacing: 6
            Button { text: "Read settings"; enabled: section.lockin.available; onClicked: section.lockin.command("refresh") }
            Button { text: "Apply settings"; enabled: section.lockin.available && section.lockin.pendingFields.length > 0; onClicked: section.lockin.command("apply") }
        }
        Label { text: section.lockin.pendingText; color: section.lockin.pendingFields.length ? Theme.warning : Theme.muted; wrapMode: Text.Wrap; Layout.fillWidth: true }
        Button { text: "Discard pending edits"; visible: section.lockin.pendingFields.length > 0; enabled: !section.lockin.busy && !section.lockin.lockedBySweep; onClicked: section.lockin.discardPending() }
        Button { text: "Stop measurement to change settings"; visible: section.lockin.lockedBySweep; onClicked: section.lockin.requestStopForSettings() }
        Label { text: Object.keys(section.lockin.status).map(k => k.replace(/_/g, " ") + ": " + String(section.lockin.status[k])).join(" · "); visible: text.length > 0; color: Theme.muted; wrapMode: Text.Wrap; Layout.fillWidth: true }
    }
    Repeater {
        model: ["Input", "Filter", "Reference", "Output"]
        delegate: ColumnLayout {
            id: group
            required property string modelData
            Layout.fillWidth: true
            spacing: 6
            Rectangle { Layout.fillWidth: true; implicitHeight: 1; color: Theme.border }
            Label { text: group.modelData; font.bold: true; color: Theme.text }
            FieldForm { fields: section.lockin.fields.filter(item => item.group === group.modelData).map(item => Object.assign({}, item, {group: ""})); target: section.lockin; locked: section.lockin.busy || section.measurementActive; Layout.fillWidth: true }
        }
    }
    ColumnLayout {
        visible: section.lockin.supportsDisplay
        Layout.fillWidth: true
        spacing: 6
        Rectangle { Layout.fillWidth: true; implicitHeight: 1; color: Theme.border }
        Label { text: "SR850 display"; font.bold: true; color: Theme.text }
        RowLayout {
            Layout.fillWidth: true
            ColumnLayout {
                Layout.fillWidth: true; spacing: 4
                Label { text: "Scale"; color: Theme.muted }
                TextField { id: displayScale; placeholderText: "Scale"; Layout.fillWidth: true; Accessible.name: "Lock-in display scale" }
            }
            ColumnLayout {
                Layout.fillWidth: true; spacing: 4
                Label { text: "Center"; color: Theme.muted }
                TextField { id: displayCenter; placeholderText: "Center"; Layout.fillWidth: true; Accessible.name: "Lock-in display center" }
            }
        }
        Button { text: "Apply display scale"; enabled: section.lockin.displayAvailable && displayScale.text.trim().length > 0 && displayCenter.text.trim().length > 0; onClicked: section.lockin.applyDisplay(Number(displayScale.text), Number(displayCenter.text)) }
    }
    ColumnLayout {
        Layout.fillWidth: true; spacing: 6
        Rectangle { Layout.fillWidth: true; implicitHeight: 1; color: Theme.border }
        Label { text: "Advanced tools"; font.bold: true; color: Theme.text }
        Button { id: automaticActions; text: checked ? "Hide automatic actions" : "Automatic actions…"; checkable: true }
        Flow {
            visible: automaticActions.checked
            Layout.fillWidth: true; spacing: 6
            Button { text: "Auto gain"; enabled: section.lockin.autoGainAvailable; onClicked: section.lockin.command("auto_gain") }
            Button { text: "Auto phase"; enabled: section.lockin.available; onClicked: section.lockin.command("auto_phase") }
            Button { text: "Auto reserve"; enabled: section.lockin.available; onClicked: section.lockin.command("auto_reserve") }
            Button { text: "Auto display scale"; visible: section.lockin.supportsDisplay; enabled: section.lockin.displayAvailable; onClicked: section.lockin.command("auto_scale") }
            Button { text: "Auto offset X"; enabled: section.lockin.available; onClicked: section.lockin.command("auto_offset_x") }
            Button { text: "Auto offset Y"; enabled: section.lockin.available; onClicked: section.lockin.command("auto_offset_y") }
            Button { text: "Auto offset R"; enabled: section.lockin.available; onClicked: section.lockin.command("auto_offset_r") }
        }
        Button { id: diagnostics; text: checked ? "Hide diagnostic readback" : "Show diagnostic readback"; checkable: true }
        Label { visible: diagnostics.checked; text: section.lockin.diagnostics; color: Theme.muted; wrapMode: Text.WrapAnywhere; Layout.fillWidth: true }
    }
}
