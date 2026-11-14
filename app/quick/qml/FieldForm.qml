import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

GridLayout {
    id: form
    property var fields: []
    property var target
    property bool locked: false
    columns: width >= 380 ? 2 : 1
    columnSpacing: 12
    rowSpacing: 8
    Repeater {
        // A separate, invisible-when-unused header keeps editors alive as values refresh.
        model: form.fields.length * 2
        delegate: Loader {
            id: row
            required property int index
            readonly property int fieldIndex: Math.floor(index / 2)
            readonly property bool header: index % 2 === 0
            readonly property var modelData: form.fields[fieldIndex] || ({})
            readonly property bool fieldVisible: modelData.visible !== false && !modelData.hidden
            readonly property bool compact: ["number", "integer", "choice", "bool"].indexOf(modelData.kind) >= 0
            visible: fieldVisible && (!header || (modelData.group && (fieldIndex === 0 || form.fields[fieldIndex - 1].group !== modelData.group)))
            active: header ? visible : true
            Layout.fillWidth: true
            Layout.alignment: Qt.AlignTop
            Layout.minimumWidth: 0
            Layout.preferredWidth: 160
            Layout.columnSpan: header || !compact ? form.columns : 1
            sourceComponent: header ? groupHeader : modelData.kind === "rows" ? rowsEditor : normalEditor
            Component {
                id: groupHeader
                Label {
                    text: row.modelData.group || ""
                    color: Theme.muted
                    font.bold: true
                    topPadding: row.fieldIndex === 0 ? 0 : 12
                }
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
                                    ActionButton {
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
                    ActionButton { text: "Add condition"; enabled: !form.locked; onClicked: form.target.addRow(row.modelData.key) }
                }
            }
        }
    }
}
