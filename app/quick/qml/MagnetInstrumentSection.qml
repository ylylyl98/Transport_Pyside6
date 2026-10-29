import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

ColumnLayout {
    id: section
    required property var magnet
    Layout.fillWidth: true
    spacing: 14
    function value(value, unit) { return value === null || value === undefined ? "unknown" : String(value) + (unit || "") }
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
        ComboBox { Accessible.name: "Selected magnet and temperature system"; model: ["attoDRY1000 / APS100", "attoDRY2100 / SDK"]; currentIndex: section.magnet.backend === "1000" ? 0 : 1; enabled: !section.magnet.busy; wheelEnabled: false; Layout.fillWidth: true; onActivated: section.magnet.selectBackend(index === 0 ? "1000" : "2100") }
        Flow {
            Layout.fillWidth: true; spacing: 6
            Button { text: "Detect && connect"; enabled: !section.magnet.busy; onClicked: section.magnet.command("detect") }
            Button { text: "Connect selected"; enabled: !section.magnet.busy; onClicked: section.magnet.command("connect") }
            Button { text: "Disconnect"; enabled: !section.magnet.busy; onClicked: section.magnet.command("disconnect") }
            Button { text: "Read status"; enabled: !section.magnet.busy; onClicked: section.magnet.command("refresh") }
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
        Button { id: review; text: checked ? "Hide commissioning details" : "Review commissioning details"; checkable: true; checked: !section.magnet.reviewed }
        Label { visible: review.checked; text: section.magnet.reviewDetails; color: Theme.muted; wrapMode: Text.Wrap; Layout.fillWidth: true }
        CheckBox {
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
            Button { text: "Move to target field"; enabled: !section.magnet.busy; onClicked: section.magnet.command("move") }
            Button { text: "Stop / pause field"; onClicked: section.magnet.command("stop") }
        }
    }
    ColumnLayout {
        Layout.fillWidth: true; spacing: 6
        Rectangle { Layout.fillWidth: true; implicitHeight: 1; color: Theme.border }
        Label { text: "Sample temperature"; font.bold: true; color: Theme.text }
        Flow {
            Layout.fillWidth: true; spacing: 6
            visible: section.magnet.backend === "1000"
            Button { text: "Connect LS335"; enabled: !section.magnet.busy; onClicked: section.magnet.command("connect_temperature") }
            Button { text: "Disconnect LS335"; enabled: !section.magnet.busy; onClicked: section.magnet.command("disconnect_temperature") }
        }
        FieldForm { fields: section.magnet.fields.filter(item => item.group === "Sample temperature").map(item => Object.assign({}, item, {group: ""})); target: section.magnet; locked: section.magnet.busy; Layout.fillWidth: true }
        Label { text: section.magnet.heaterAutoPreview; visible: text.length > 0; wrapMode: Text.Wrap; Layout.fillWidth: true; color: Theme.muted }
        Flow {
            Layout.fillWidth: true; spacing: 6
            Button { text: "Apply temperature"; enabled: !section.magnet.busy && section.magnet.temperatureAvailable; onClicked: section.magnet.command("temperature") }
            Button { text: section.magnet.backend === "1000" ? "Heater OFF" : "Stop temperature"; onClicked: section.magnet.command("temperature_stop") }
        }
        ColumnLayout {
            visible: section.magnet.backend === "1000"; Layout.fillWidth: true; spacing: 6
            Button { id: bands; text: checked ? "Hide automatic heater bands" : "Automatic heater bands…"; checkable: true }
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
                        Button { text: "×"; Layout.preferredWidth: 34; enabled: !section.magnet.busy; Accessible.name: "Remove heater band " + (band.index + 1); onClicked: section.magnet.removeRow("heater_bands", band.index) }
                    }
                }
                Label { text: section.magnet.heaterBandFields[0].description; color: Theme.muted; wrapMode: Text.Wrap; Layout.fillWidth: true }
                RowLayout {
                    Button { text: "Add band"; enabled: !section.magnet.busy; onClicked: section.magnet.addRow("heater_bands") }
                    Button { text: "Save heater bands"; enabled: !section.magnet.busy; onClicked: section.magnet.saveHeaterBands() }
                }
            }
        }
    }
    Rectangle { Layout.fillWidth: true; implicitHeight: 1; color: Theme.border }
    Button { id: diagnostics; text: checked ? "Hide diagnostics && log" : "Diagnostics && log…"; checkable: true }
    ColumnLayout {
        visible: diagnostics.checked; Layout.fillWidth: true
        Label { text: "Connections: " + Object.keys(section.magnet.connections).map(k => k + " " + (section.magnet.connections[k] ? "connected" : "disconnected")).join(" · "); color: Theme.muted; wrapMode: Text.Wrap; Layout.fillWidth: true }
        Label { text: "APS100 thermal diagnostics: " + section.magnet.thermalStatus; visible: section.magnet.backend !== "1000"; color: Theme.muted; wrapMode: Text.Wrap; Layout.fillWidth: true }
        ScrollView { Layout.fillWidth: true; Layout.preferredHeight: 180; TextArea { text: section.magnet.logText; readOnly: true; selectByMouse: true; wrapMode: TextEdit.Wrap } }
        Button { text: "Clear magnet log"; onClicked: section.magnet.clearLog() }
    }
}
