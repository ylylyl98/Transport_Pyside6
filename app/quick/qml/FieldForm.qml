import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

ColumnLayout {
    id: form
    property var fields: []
    property var target
    property bool locked: false
    spacing: 12
    Repeater {
        model: form.fields.length
        delegate: ColumnLayout {
            id: row
            required property int index
            readonly property var modelData: form.fields[index] || ({})
            Layout.fillWidth: true
            visible: modelData.visible !== false && !modelData.hidden
            spacing: 8
            Label {
                visible: row.modelData.group && (row.index === 0 || form.fields[row.index - 1].group !== row.modelData.group)
                text: row.modelData.group || ""
                color: Theme.muted
                font.bold: true
                topPadding: row.index === 0 ? 0 : 12
                Layout.fillWidth: true
            }
            Loader {
                Layout.fillWidth: true
                sourceComponent: row.modelData.kind === "rows" ? rowsEditor : normalEditor
            }
            Component {
                id: normalEditor
                FieldEditor {
                    formField: row.modelData
                    locked: form.locked
                    onCommitted: value => form.target.setValue(row.modelData.key, value)
                }
            }
            Component {
                id: rowsEditor
                ColumnLayout {
                    Label { text: row.modelData.label; font.bold: true; color: Theme.text }
                    Repeater {
                        model: (row.modelData.rows || row.modelData.value || []).length
                        delegate: Frame {
                            id: condition
                            required property int index
                            readonly property var modelData: (row.modelData.rows || row.modelData.value || [])[index] || ({})
                            Layout.fillWidth: true
                            background: Rectangle { color: Theme.surface; border.color: Theme.border; radius: 4 }
                            ColumnLayout {
                                anchors.fill: parent
                                RowLayout {
                                    Label { text: "Condition " + (condition.index + 1); Layout.fillWidth: true; color: Theme.muted }
                                    Button {
                                        text: "Remove"; enabled: !form.locked
                                        onClicked: form.target.removeRow(row.modelData.key, condition.index)
                                    }
                                }
                                Repeater {
                                    model: (row.modelData.columns || []).length
                                    delegate: FieldEditor {
                                        required property int index
                                        readonly property var modelData: (row.modelData.columns || [])[index] || ({})
                                        formField: Object.assign({}, modelData, {value: condition.modelData[modelData.key]})
                                        locked: form.locked
                                        onCommitted: value => form.target.setRowValue(row.modelData.key, condition.index, modelData.key, value)
                                    }
                                }
                            }
                        }
                    }
                    Button { text: "Add condition"; enabled: !form.locked; onClicked: form.target.addRow(row.modelData.key) }
                }
            }
        }
    }
}
