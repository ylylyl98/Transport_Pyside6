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
        ActionButton { text: "Scan resources"; enabled: !section.instruments.busy; onClicked: section.instruments.command("scan", {}) }
        ActionButton {
            objectName: "instrumentsConnect"
            text: section.instruments.connectionActions.connectText
            enabled: section.instruments.connectionActions.connectEnabled
            palette.disabled.buttonText: text === "Connected" ? Theme.connectionColor("connected") : Theme.disabledText
            onClicked: section.instruments.command("connect", {})
        }
        ActionButton { objectName: "instrumentsDisconnect"; text: section.instruments.connectionActions.disconnectText; enabled: section.instruments.connectionActions.disconnectEnabled; onClicked: section.instruments.command("disconnect", {}) }
    }
    Label { text: section.instruments.connectionActions.status; color: Theme.text; wrapMode: Text.Wrap; Layout.fillWidth: true }
    Label { text: "Check addresses and source protection below before connecting. Apply connection changes after editing an address or source mode."; color: Theme.muted; wrapMode: Text.Wrap; Layout.fillWidth: true }
    Label { text: Object.keys(section.instruments.discovered).map(k => k.toUpperCase() + ": " + section.instruments.discovered[k].join(", ")).join("\n"); visible: text.length > 0; wrapMode: Text.Wrap; Layout.fillWidth: true; color: Theme.muted }
    Flow {
        Layout.fillWidth: true; spacing: 10
        Repeater {
            model: section.instruments.devices
            delegate: Label {
                required property var modelData
                objectName: "connectionStatus_" + modelData.name
                text: modelData.label + ": " + modelData.connectionStatus
                color: Theme.connectionColor(modelData.connectionStatus)
                width: Math.min(implicitWidth, parent.width)
                wrapMode: Text.Wrap
            }
        }
    }
    Repeater {
        model: [1, 2, 3, 4]
        delegate: ColumnLayout {
            id: gate
            required property int modelData
            Layout.fillWidth: true
            spacing: 6
            Rectangle { Layout.fillWidth: true; implicitHeight: 1; color: Theme.border }
            ActionButton { id: gateDetails; objectName: "connectionGateDetails_" + gate.modelData; text: (checked ? "- " : "+ ") + "G" + gate.modelData + (gate.modelData === 4 ? " / Bias 2" : gate.modelData === 3 ? " / Bias 1 (Vds)" : "") + " · address, source & protection"; checkable: true; Layout.fillWidth: true }
            // Keep each source's address beside its mode, limits and Apply action.
            FieldForm { visible: gateDetails.checked; fields: section.instruments.connectionFields.filter(item => item.key === "gate" + gate.modelData || item.key.indexOf("gate" + gate.modelData + "_") === 0).map(item => Object.assign({}, item, {group: "", label: item.key === "gate" + gate.modelData ? "VISA address" : item.label})); target: section.instruments; locked: section.instruments.busy; Layout.fillWidth: true }
            ActionButton { visible: gateDetails.checked; text: "Apply G" + gate.modelData + " protection"; enabled: !section.instruments.busy; onClicked: section.instruments.command("protection", {device: "g" + gate.modelData}) }
        }
    }
    ColumnLayout {
        Layout.fillWidth: true; spacing: 6
        Rectangle { Layout.fillWidth: true; implicitHeight: 1; color: Theme.border }
        ActionButton { id: otherAddresses; objectName: "otherInstrumentAddresses"; text: (checked ? "- " : "+ ") + "DAQ, monochromator & lock-in addresses"; checkable: true; Layout.fillWidth: true }
        FieldForm { visible: otherAddresses.checked; fields: section.instruments.connectionFields.filter(item => item.group === "Instrument addresses" && ["gate1", "gate2", "gate3", "gate4"].indexOf(item.key) < 0).map(item => Object.assign({}, item, {group: ""})); target: section.instruments; locked: section.instruments.busy; Layout.fillWidth: true }
    }
}
