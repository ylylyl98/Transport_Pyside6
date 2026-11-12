import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import "NumberFormat.js" as NumberFormat

ColumnLayout {
    id: section
    required property var magnet
    Layout.fillWidth: true
    spacing: 14
    function value(value, unit) {
        if (value === null || value === undefined) return "unknown"
        if (typeof value === "boolean") return value ? "ON" : "OFF"
        return unit ? NumberFormat.quantity(value, unit.trim()) : NumberFormat.compact(value)
    }
    function readout() {
        const m = magnet.telemetry.magnet || {}
        const status = m.status || {}
        if (Object.keys(m).length === 0) return ""
        return "Heater: " + value(m.heater_on === undefined ? status.heater_on : m.heater_on)
            + " · state: " + value(m.sweep_state === undefined ? status.field_control_state : m.sweep_state)
            + "\nSupply: " + value(m.output_field_t === undefined ? m.lead_field_t : m.output_field_t, " T") + " · " + value(m.output_current_a, " A")
            + "\nMagnet voltage: " + value(m.magnet_voltage_v, " V") + " · quench: " + value(status.quench)
    }
    ColumnLayout {
        Layout.fillWidth: true; spacing: 6
        Label { text: "Selected system"; color: Theme.muted; Layout.fillWidth: true }
        SelectBox { Accessible.name: "Selected magnet and temperature system"; model: ["attoDRY1000 / APS100", "attoDRY2100 / SDK"]; currentIndex: section.magnet.backend === "1000" ? 0 : 1; enabled: !section.magnet.busy; wheelEnabled: false; Layout.fillWidth: true; onActivated: section.magnet.selectBackend(index === 0 ? "1000" : "2100") }
        Flow {
            Layout.fillWidth: true; spacing: 6
            ActionButton { text: "Detect && connect"; enabled: !section.magnet.busy; onClicked: section.magnet.command("detect") }
            ActionButton { tone: "primary"; text: "Connect selected"; enabled: !section.magnet.busy; onClicked: section.magnet.command("connect") }
            ActionButton { text: "Disconnect"; enabled: !section.magnet.busy; onClicked: section.magnet.command("disconnect") }
            ActionButton { text: "Read status"; enabled: !section.magnet.busy; onClicked: section.magnet.command("refresh") }
        }
        Label { text: section.magnet.summary; color: Theme.text; font.bold: true; wrapMode: Text.Wrap; Layout.fillWidth: true }
        Label { text: section.magnet.temperatureStatus; color: Theme.muted; wrapMode: Text.Wrap; Layout.fillWidth: true }
        Label { text: section.readout(); visible: text.length > 0; color: Theme.muted; wrapMode: Text.Wrap; Layout.fillWidth: true }
        Label { text: section.magnet.backend === "1000" ? "Thermal interlock · " + section.magnet.thermalStatus : "SDK temperature and field state shown above"; color: Theme.muted; wrapMode: Text.Wrap; Layout.fillWidth: true }
    }
    ColumnLayout {
        Layout.fillWidth: true; spacing: 6
        Rectangle { Layout.fillWidth: true; implicitHeight: 1; color: Theme.border }
        Label { text: "Commissioning review"; font.bold: true; color: Theme.text }
        ActionButton { id: review; text: checked ? "Hide commissioning details" : "Review commissioning details"; checkable: true; checked: !section.magnet.reviewed }
        Label { visible: review.checked; text: section.magnet.reviewDetails; color: Theme.muted; wrapMode: Text.Wrap; Layout.fillWidth: true }
        StateCheckBox {
            text: "I reviewed the configured limits and connections"; checked: section.magnet.reviewed; onClicked: section.magnet.acknowledgeReview(checked); Layout.fillWidth: true
            contentItem: Label { text: parent.text; wrapMode: Text.Wrap; leftPadding: parent.indicator.width + 8; color: Theme.text; verticalAlignment: Text.AlignVCenter }
        }
    }
    ColumnLayout {
        Layout.fillWidth: true; spacing: 6
        Rectangle { Layout.fillWidth: true; implicitHeight: 1; color: Theme.border }
        Label { text: "Magnetic field"; font.bold: true; color: Theme.text }
        FieldForm { fields: section.magnet.fields.filter(item => item.group === "Magnetic field").map(item => Object.assign({}, item, {group: ""})); target: section.magnet; locked: section.magnet.busy; Layout.fillWidth: true }
        Flow {
            Layout.fillWidth: true; spacing: 6
            ActionButton { tone: "primary"; text: "Move to target field"; enabled: !section.magnet.busy; onClicked: section.magnet.command("move") }
            ActionButton { tone: "danger"; text: "Stop / pause field"; onClicked: section.magnet.command("stop") }
        }
    }
    ColumnLayout {
        Layout.fillWidth: true; spacing: 6
        Rectangle { Layout.fillWidth: true; implicitHeight: 1; color: Theme.border }
        Label { text: "Sample temperature"; font.bold: true; color: Theme.text }
        Flow {
            Layout.fillWidth: true; spacing: 6
            visible: section.magnet.backend === "1000"
            ActionButton { text: "Connect LS335"; enabled: !section.magnet.busy; onClicked: section.magnet.command("connect_temperature") }
            ActionButton { text: "Disconnect LS335"; enabled: !section.magnet.busy; onClicked: section.magnet.command("disconnect_temperature") }
        }
        FieldForm { fields: section.magnet.fields.filter(item => item.group === "Sample temperature").map(item => Object.assign({}, item, {group: ""})); target: section.magnet; locked: section.magnet.busy; Layout.fillWidth: true }
        Label { text: section.magnet.heaterAutoPreview; visible: text.length > 0; wrapMode: Text.Wrap; Layout.fillWidth: true; color: Theme.muted }
        Label { text: section.magnet.heatingBlockReason; visible: text.length > 0; wrapMode: Text.Wrap; Layout.fillWidth: true; color: Theme.muted }
        Flow {
            Layout.fillWidth: true; spacing: 6
            ActionButton { tone: "primary"; text: "Apply temperature"; enabled: !section.magnet.busy && section.magnet.temperatureAvailable && !section.magnet.heatingBlockReason; onClicked: section.magnet.command("temperature") }
            ActionButton { tone: "danger"; text: section.magnet.backend === "1000" ? "Heater OFF" : "Stop temperature"; onClicked: section.magnet.command("temperature_stop") }
        }
        ColumnLayout {
            visible: section.magnet.backend === "1000"; Layout.fillWidth: true; spacing: 6
            Label { text: "LS335 sensor current reversal"; font.bold: true; color: Theme.text }
            Label { text: section.magnet.currentReversalStatus; wrapMode: Text.Wrap; Layout.fillWidth: true; color: Theme.muted }
            Label { text: "OFF uses DC excitation and keeps temperature readout active, but removes thermal EMF compensation. Change only while measurements and field motion are idle."; wrapMode: Text.Wrap; Layout.fillWidth: true; color: Theme.muted }
            Repeater {
                model: section.magnet.currentReversalChannels
                delegate: RowLayout {
                    required property var modelData
                    Layout.fillWidth: true
                    Label { text: "Input " + modelData.channel; color: Theme.text; Layout.fillWidth: true }
                    ActionButton { text: "ON"; enabled: !section.magnet.busy && modelData.supported && modelData.enabled !== true; onClicked: section.magnet.setCurrentReversal(modelData.channel, true) }
                    ActionButton { text: "OFF (DC)"; enabled: !section.magnet.busy && modelData.supported && modelData.enabled !== false; onClicked: section.magnet.setCurrentReversal(modelData.channel, false) }
                }
            }
            ActionButton { text: "Refresh input settings"; enabled: !section.magnet.busy && section.magnet.connections.temperature; onClicked: section.magnet.refreshCurrentReversal() }
            ActionButton { id: bands; text: checked ? "Hide automatic heater bands" : "Automatic heater bands…"; checkable: true }
            ColumnLayout {
                visible: bands.checked; Layout.fillWidth: true
                RowLayout {
                    Layout.fillWidth: true
                    Label { text: "Upper temperature (K)"; font.bold: true; color: Theme.muted; Layout.fillWidth: true }
                    Label { text: "Heater range"; font.bold: true; color: Theme.muted; Layout.preferredWidth: 130 }
                    Item { Layout.preferredWidth: 34 }
                }
                Repeater {
                    model: section.magnet.heaterBandFields[0].rows
                    delegate: RowLayout {
                        id: band
                        required property var modelData
                        required property int index
                        Layout.fillWidth: true
                        Repeater {
                            model: section.magnet.heaterBandFields[0].columns
                            delegate: FieldEditor {
                                required property var modelData
                                formField: Object.assign({}, modelData, {value: band.modelData[modelData.key]})
                                showLabel: false; locked: section.magnet.busy; Layout.fillWidth: true; Layout.preferredWidth: modelData.key === "heater_range" ? 130 : 160
                                onCommitted: value => section.magnet.setRowValue("heater_bands", band.index, modelData.key, value)
                            }
                        }
                        ActionButton { text: "×"; Layout.preferredWidth: 34; enabled: !section.magnet.busy; Accessible.name: "Remove heater band " + (band.index + 1); onClicked: section.magnet.removeRow("heater_bands", band.index) }
                    }
                }
                Label { text: section.magnet.heaterBandFields[0].description; color: Theme.muted; wrapMode: Text.Wrap; Layout.fillWidth: true }
                RowLayout {
                    ActionButton { text: "Add band"; enabled: !section.magnet.busy; onClicked: section.magnet.addRow("heater_bands") }
                    ActionButton { text: "Save heater bands"; enabled: !section.magnet.busy; onClicked: section.magnet.saveHeaterBands() }
                }
            }
        }
    }
    Rectangle { Layout.fillWidth: true; implicitHeight: 1; color: Theme.border }
    ActionButton { id: diagnostics; text: checked ? "Hide diagnostics && log" : "Diagnostics && log…"; checkable: true }
    ColumnLayout {
        visible: diagnostics.checked; Layout.fillWidth: true
        Label { text: "Connections: " + Object.keys(section.magnet.connections).map(k => k + " " + (section.magnet.connections[k] ? "connected" : "disconnected")).join(" · "); color: Theme.muted; wrapMode: Text.Wrap; Layout.fillWidth: true }
        Label { text: "APS100 thermal diagnostics: " + section.magnet.thermalStatus; visible: section.magnet.backend !== "1000"; color: Theme.muted; wrapMode: Text.Wrap; Layout.fillWidth: true }
        ScrollView { Layout.fillWidth: true; Layout.preferredHeight: 180; TextArea { text: section.magnet.logText; readOnly: true; selectByMouse: true; wrapMode: TextEdit.Wrap } }
        ActionButton { text: "Clear magnet log"; onClicked: section.magnet.clearLog() }
    }
}
