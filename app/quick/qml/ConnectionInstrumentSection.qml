import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

ColumnLayout {
    id: section
    required property var instruments
    Layout.fillWidth: true
    spacing: 14
    Flow {
        Layout.fillWidth: true; spacing: 6
        Button { text: "Scan resources"; enabled: !section.instruments.busy; onClicked: section.instruments.command("scan", {}) }
        Button { text: "Connect all"; enabled: !section.instruments.busy; onClicked: section.instruments.command("connect", {}) }
        Button { text: "Disconnect all"; enabled: !section.instruments.busy; onClicked: section.instruments.command("disconnect", {}) }
    }
    Label { text: "Check addresses and source protection below, then Connect all. Reconnect after changing an address or source mode."; color: Theme.muted; wrapMode: Text.Wrap; Layout.fillWidth: true }
    Label { text: Object.keys(section.instruments.discovered).map(k => k.toUpperCase() + ": " + section.instruments.discovered[k].join(", ")).join("\n"); visible: text.length > 0; wrapMode: Text.Wrap; Layout.fillWidth: true; color: Theme.muted }
    Label { text: section.instruments.devices.map(device => device.label + ": " + device.state + (device.reconnect ? " · reconnect required" : "")).join("  ·  "); color: Theme.muted; wrapMode: Text.Wrap; Layout.fillWidth: true }
    Repeater {
        model: [1, 2, 3]
        delegate: ColumnLayout {
            id: gate
            required property int modelData
            Layout.fillWidth: true
            spacing: 6
            Rectangle { Layout.fillWidth: true; implicitHeight: 1; color: Theme.border }
            Label { text: "G" + gate.modelData + " · address, source & protection"; font.bold: true; color: Theme.text; wrapMode: Text.Wrap; Layout.fillWidth: true }
            // Keep each source's address beside its mode, limits and Apply action.
            FieldForm { fields: section.instruments.connectionFields.filter(item => item.key === "gate" + gate.modelData || item.key.indexOf("gate" + gate.modelData + "_") === 0).map(item => Object.assign({}, item, {group: "", label: item.key === "gate" + gate.modelData ? "VISA address" : item.label})); target: section.instruments; locked: section.instruments.busy; Layout.fillWidth: true }
            Button { text: "Apply G" + gate.modelData + " protection"; enabled: !section.instruments.busy; onClicked: section.instruments.command("protection", {device: "g" + gate.modelData}) }
        }
    }
    ColumnLayout {
        Layout.fillWidth: true; spacing: 6
        Rectangle { Layout.fillWidth: true; implicitHeight: 1; color: Theme.border }
        Label { text: "DAQ, monochromator & lock-ins"; font.bold: true; color: Theme.text; wrapMode: Text.Wrap; Layout.fillWidth: true }
        FieldForm { fields: section.instruments.connectionFields.filter(item => item.group === "Instrument addresses" && ["gate1", "gate2", "gate3"].indexOf(item.key) < 0).map(item => Object.assign({}, item, {group: ""})); target: section.instruments; locked: section.instruments.busy; Layout.fillWidth: true }
    }
}
