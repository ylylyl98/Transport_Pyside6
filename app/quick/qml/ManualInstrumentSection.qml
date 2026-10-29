import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

ColumnLayout {
    id: section
    required property var instruments
    Layout.fillWidth: true
    spacing: 10
    Label { text: "Connect instruments and verify protection first. Gate ramps require idle, 2-wire voltage-source mode."; color: Theme.muted; wrapMode: Text.Wrap; Layout.fillWidth: true }
    RowLayout {
        Layout.fillWidth: true
        Label { text: "Step size (V)"; color: Theme.muted; Layout.fillWidth: true }
        TextField { text: String(section.instruments.manualStep); enabled: !section.instruments.busy; Layout.preferredWidth: 100; onEditingFinished: section.instruments.setManualStep(text); Accessible.name: "Manual gate step voltage" }
    }
    Repeater {
        // Readback values change frequently; keep the three editable gate rows alive.
        model: ["g1", "g2", "g3"]
        delegate: ColumnLayout {
            id: gate
            required property string modelData
            readonly property var device: section.instruments.devices.find(item => item.name === gate.modelData) || ({})
            Layout.fillWidth: true
            spacing: 6
            Rectangle { Layout.fillWidth: true; implicitHeight: 1; color: Theme.border }
            Label { text: gate.device.label + " · " + gate.device.state; font.bold: true; color: Theme.text; Layout.fillWidth: true }
            Label { text: "Commanded " + gate.device.setVoltage + " V  ·  measured " + gate.device.measuredVoltage + " V\nCurrent " + gate.device.current + " A"; color: Theme.text; wrapMode: Text.Wrap; Layout.fillWidth: true }
            Label { text: gate.device.protectionApplied ? "Source protection verified" : "Source protection unverified"; color: gate.device.protectionApplied ? Theme.muted : Theme.warning; wrapMode: Text.Wrap; Layout.fillWidth: true }
            Label {
                text: "Limit " + gate.device.maxVoltage + " V / " + gate.device.currentCompliance + " A · compliance " + (!gate.device.connected || gate.device.complianceTripped === null ? "unknown" : gate.device.complianceTripped ? "TRIPPED" : "clear")
                color: gate.device.complianceTripped ? Theme.danger : Theme.muted; wrapMode: Text.Wrap; Layout.fillWidth: true
            }
            RowLayout {
                Layout.fillWidth: true
                Label { text: "Target (V)"; color: Theme.muted }
                TextField { id: target; objectName: "manualGateTarget_" + gate.modelData; text: "0"; placeholderText: "Target (V)"; Layout.fillWidth: true; Layout.minimumWidth: 60; Accessible.name: gate.device.label + " target voltage" }
                Button { text: "Ramp to target"; enabled: gate.device.canRamp; onClicked: section.instruments.command("ramp", {device: gate.device.name, target: target.text}) }
            }
            Flow {
                Layout.fillWidth: true; spacing: 6
                Button { text: "−" + section.instruments.manualStep + " V"; enabled: gate.device.canRamp; onClicked: section.instruments.command("step", {device: gate.device.name, delta: -section.instruments.manualStep}) }
                Button { text: "+" + section.instruments.manualStep + " V"; enabled: gate.device.canRamp; onClicked: section.instruments.command("step", {device: gate.device.name, delta: section.instruments.manualStep}) }
                Button { text: "Read back"; enabled: gate.device.canRead; onClicked: section.instruments.command("read", {device: gate.device.name}) }
                Button { text: "Ramp to 0 V"; enabled: gate.device.canZero; onClicked: section.instruments.command("ramp", {device: gate.device.name, target: 0}) }
            }
            Label { text: gate.device.reconnect ? "Address or mode changed — reconnect" : gate.device.detail; visible: text.length > 0; color: Theme.muted; wrapMode: Text.Wrap; Layout.fillWidth: true }
            Label { text: gate.device.readbackError; visible: text.length > 0; color: Theme.danger; wrapMode: Text.Wrap; Layout.fillWidth: true }
        }
    }
    ColumnLayout {
        Layout.fillWidth: true; spacing: 6
        Rectangle { Layout.fillWidth: true; implicitHeight: 1; color: Theme.border }
        Label { text: "DAQ analog output"; font.bold: true; color: Theme.text }
        Repeater {
            model: section.instruments.daqOutputs
            delegate: Label { required property var modelData; text: "AO" + modelData.channel + ": commanded " + modelData.commanded + " V · measured " + modelData.measured + " V" + (modelData.error ? "\n" + modelData.error : ""); wrapMode: Text.Wrap; Layout.fillWidth: true; color: modelData.error ? Theme.danger : Theme.muted }
        }
        RowLayout {
            Layout.fillWidth: true
            Label { text: "Channel"; color: Theme.muted }
            TextField { id: aoChannel; text: "0"; Layout.preferredWidth: 44; Accessible.name: "DAQ output channel" }
            Label { text: "Target (V)"; color: Theme.muted }
            TextField { id: aoTarget; text: "0"; placeholderText: "Target (V)"; Layout.fillWidth: true; Layout.minimumWidth: 60; Accessible.name: "DAQ output target voltage" }
        }
        Flow {
            Layout.fillWidth: true; spacing: 6
            Button { text: "Ramp AO to target"; enabled: !section.instruments.busy; onClicked: section.instruments.command("daq", {channel: aoChannel.text, target: aoTarget.text}) }
            Button { text: "Ramp AO to 0 V"; enabled: !section.instruments.busy; onClicked: section.instruments.command("daq", {channel: aoChannel.text, target: 0}) }
        }
    }
    ColumnLayout {
        Layout.fillWidth: true; spacing: 6
        Rectangle { Layout.fillWidth: true; implicitHeight: 1; color: Theme.border }
        Label { text: "Monochromator wavelength (nm)"; font.bold: true; color: Theme.text }
        RowLayout {
            Layout.fillWidth: true
            TextField { id: wavelength; text: "550"; Layout.fillWidth: true; Accessible.name: "Monochromator wavelength" }
            Button { text: "Set wavelength"; enabled: !section.instruments.busy; onClicked: section.instruments.command("wavelength", {target: wavelength.text}) }
        }
    }
    ColumnLayout {
        Layout.fillWidth: true; spacing: 6
        Rectangle { Layout.fillWidth: true; implicitHeight: 1; color: Theme.border }
        Label { text: "Other instrument connections"; font.bold: true; color: Theme.text; Layout.fillWidth: true }
        Repeater {
            model: section.instruments.devices.filter(item => ["g1", "g2", "g3"].indexOf(item.name) < 0)
            delegate: Label { required property var modelData; text: modelData.label + " · " + modelData.state + (modelData.reconnect ? " · reconnect required" : "") + (modelData.detail ? " · " + modelData.detail : ""); wrapMode: Text.Wrap; color: Theme.muted; Layout.fillWidth: true }
        }
    }
}
