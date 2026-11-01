import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

Item {
    id: table
    required property var measurement
    required property string rowKey
    property bool locked: measurement ? measurement.active : false
    readonly property var rowModel: { measurement.mode; return rowKey ? measurement.rowModel(rowKey) : null }
    readonly property int count: rowModel ? rowModel.count : 0
    property var activeEditor: null
    property bool suppressReveal: false
    signal detailsRequested(int row)
    readonly property real preferredRecordsHeight: count ? Math.max(140, Math.min(350, records.contentHeight || 240)) : 52
    implicitHeight: actions.implicitHeight + preferredRecordsHeight + 6
    Layout.fillWidth: true
    function commitActive() {
        if (activeEditor && !activeEditor.commit()) return false
        return rowModel ? rowModel.flush() : true
    }
    function revealSelection() {
        if (rowModel && rowModel.selectedIndex >= 0)
            records.positionViewAtIndex(rowModel.selectedIndex, ListView.Contain)
    }
    function revealField(rowIndex, key) {
        records.positionViewAtIndex(rowIndex, ListView.Contain)
        Qt.callLater(() => {
            const row = records.itemAtIndex(rowIndex)
            function find(item) {
                if (item.objectName.startsWith("recipeCell_") && item.objectName.endsWith("_" + key)) return item
                for (let child of item.children || []) { const match = find(child); if (match) return match }
                return null
            }
            const field = row ? find(row) : null
            if (field) {
                const y = field.mapToItem(records.contentItem, 0, 0).y
                records.contentY = Math.max(0, Math.min(Math.max(0, records.contentHeight - records.height), y - 8))
                field.forceActiveFocus()
            }
        })
    }
    function selectVisible(uid) {
        // Clicking a lower cell in a tall record must not jump back to its top.
        suppressReveal = true
        rowModel.select(uid)
        suppressReveal = false
    }
    Connections {
        target: table.rowModel
        function onSelectedIndexChanged() { if (!table.suppressReveal) Qt.callLater(table.revealSelection) }
    }
    ActionDialog {
        id: expanded
        parent: Overlay.overlay
        anchors.centerIn: parent
        title: table.rowKey === "regions" ? "Edit gate regions" : "Edit measurement conditions"
        width: Math.min(1100, parent.width - 48)
        height: Math.min(760, parent.height - 48)
        modal: true
        standardButtons: Dialog.Close
        contentItem: Item {}
        onClosed: { table.commitActive(); expandButton.forceActiveFocus() }
    }
    Item {
        id: body
        parent: expanded.visible ? expanded.contentItem : table
        anchors.fill: parent
        ColumnLayout {
            anchors.fill: parent
            spacing: 6
            Flow {
                id: actions
                Layout.fillWidth: true
                spacing: 4
                ActionButton {
                    objectName: "measurementAdd_" + table.rowKey
                    text: table.rowKey === "regions" ? "Add region" : "Add condition"
                    enabled: !table.locked && table.count < 100
                    Accessible.name: text
                    ToolTip.visible: hovered
                    ToolTip.text: table.measurement.mode === 4 && !table.count ? "Save the current complete Gate Scan setup as the first condition" : "Append a new default row; use Duplicate to copy an existing row"
                    onClicked: if (table.commitActive()) { table.measurement.addRow(table.rowKey); Qt.callLater(table.revealSelection) }
                }
                ActionButton {
                    objectName: "measurementDuplicate_" + table.rowKey
                    text: "Duplicate"
                    enabled: !table.locked && table.count > 0 && table.count < 100
                    onClicked: if (table.commitActive()) { table.measurement.duplicateRow(table.rowKey, table.rowModel.selectedIndex); Qt.callLater(table.revealSelection) }
                }
                ActionButton {
                    objectName: "measurementRemove_" + table.rowKey
                    text: "Remove"
                    enabled: !table.locked && table.count > 0
                    onClicked: if (table.commitActive()) { table.measurement.removeRow(table.rowKey, table.rowModel.selectedIndex); Qt.callLater(table.revealSelection) }
                }
                ActionToolButton {
                    id: expandButton
                    objectName: "measurementExpand_" + table.rowKey
                    text: expanded.visible ? "Restore" : "Enlarge"
                    onClicked: {
                        if (!table.commitActive()) return
                        if (expanded.visible) expanded.close()
                        else expanded.open()
                    }
                }
                Label {
                    text: table.rowKey === "regions" ? table.count + (table.count === 1 ? " additional region" : " additional regions") + " (" + (table.count + 1) + " total)" : table.count + (table.count === 1 ? " condition" : " conditions")
                    color: Theme.muted
                    topPadding: 6
                }
            }
            ListView {
                id: records
                objectName: "measurementRows_" + table.rowKey
                Layout.fillWidth: true
                Layout.fillHeight: true
                clip: true
                model: table.rowModel
                reuseItems: true
                spacing: 6
                boundsBehavior: Flickable.StopAtBounds
                ScrollBar.vertical: ScrollBar {
                    id: verticalBar
                    policy: records.contentHeight > records.height + 1 ? ScrollBar.AlwaysOn : ScrollBar.AsNeeded
                }
                onMovementStarted: table.commitActive()
                Label {
                    anchors.fill: parent
                    visible: table.count === 0
                    text: table.rowKey === "regions" ? "Base ranges above define Region 1. Add region extends the same scan." : table.measurement.mode === 4 ? "The setup below runs at every field. Add condition saves it as the first row." : "No rows. Use Add to create one."
                    color: Theme.muted
                    wrapMode: Text.Wrap
                }
                delegate: Rectangle {
                    id: record
                    required property int index
                    required property string uid
                    required property int number
                    required property var cells
                    readonly property var enabledCells: (cells || []).filter(cell => cell && cell.spec && cell.spec.key === "enabled")
                    readonly property var fieldCells: (cells || []).filter(cell => cell && cell.spec && cell.spec.key !== "enabled")
                    readonly property bool alignedGateRows: (cells || []).some(cell => cell && cell.spec && cell.spec.key === "raw_vtg_active")
                    ListView.onPooled: {
                        // Flush and release focus before the delegate receives another UID.
                        if (table.activeEditor && table.activeEditor.uid === record.uid) {
                            table.commitActive()
                            records.forceActiveFocus()
                            table.activeEditor = null
                        }
                    }
                    objectName: "measurementRow_" + table.rowKey + "_" + index
                    width: records.width - (verticalBar.policy === ScrollBar.AlwaysOn ? verticalBar.width + 2 : 0)
                    implicitHeight: rowContent.implicitHeight + 16
                    height: implicitHeight
                    color: table.rowModel && table.rowModel.selectedIndex === index ? Theme.selection : Theme.surface
                    border.color: table.rowModel && table.rowModel.selectedIndex === index ? Theme.accent : Theme.border
                    radius: 3
                    ColumnLayout {
                        id: rowContent
                        x: 8; y: 8
                        width: parent.width - 16
                        spacing: 4
                        RowLayout {
                            Layout.fillWidth: true
                            ActionToolButton {
                                objectName: "measurementSelect_" + table.rowKey + "_" + record.index
                                text: (table.rowKey === "regions" ? "Region " : "Condition ") + (table.rowKey === "regions" ? record.number + 1 : record.number)
                                Accessible.name: "Select " + text
                                checkable: true
                                checked: table.rowModel && table.rowModel.selectedIndex === record.index
                                onClicked: table.selectVisible(record.uid)
                            }
                            Repeater {
                                model: record.enabledCells.length
                                RecipeCell {
                                    required property int index
                                    Layout.alignment: Qt.AlignVCenter
                                    Layout.fillWidth: false
                                    rowModel: table.rowModel
                                    uid: record.uid
                                    spec: record.enabledCells[index] ? record.enabledCells[index].spec || ({}) : ({})
                                    owner: table
                                    locked: table.locked
                                    compact: true
                                }
                            }
                            Item { Layout.fillWidth: true }
                            ActionToolButton {
                                visible: table.measurement.mode === 4
                                text: "Advanced settings"
                                Accessible.name: "Advanced settings for condition " + record.number
                                onClicked: if (table.commitActive()) { table.selectVisible(record.uid); table.detailsRequested(record.index) }
                            }
                        }
                        GridLayout {
                            id: grid
                            Layout.fillWidth: true
                            readonly property int capacity: Math.max(2, Math.min(4, Math.floor(width / 108)))
                            columns: record.alignedGateRows ? 3 : Math.max(2, Math.ceil(record.fieldCells.length / Math.max(1, Math.ceil(record.fieldCells.length / capacity))))
                            columnSpacing: 8
                            rowSpacing: 6
                            Repeater {
                                model: record.fieldCells.length
                                RecipeCell {
                                    required property int index
                                    readonly property var cellSpec: record.fieldCells[index] ? record.fieldCells[index].spec || ({}) : ({})
                                    Layout.fillWidth: true
                                    Layout.minimumWidth: 0
                                    Layout.preferredWidth: 130
                                    Layout.row: record.alignedGateRows && cellSpec.layoutRow !== undefined ? cellSpec.layoutRow : -1
                                    Layout.column: record.alignedGateRows && cellSpec.layoutColumn !== undefined ? cellSpec.layoutColumn : -1
                                    Layout.columnSpan: cellSpec.layoutColumnSpan || 1
                                    Layout.alignment: Qt.AlignBottom
                                    rowModel: table.rowModel
                                    uid: record.uid
                                    spec: cellSpec
                                    owner: table
                                    locked: table.locked
                                }
                            }
                        }
                        Label {
                            objectName: "recipeSampling_" + record.uid
                            readonly property var sampling: record.cells.length && record.cells[0] ? record.cells[0].spec || ({}) : ({})
                            text: sampling.samplingText || ""
                            visible: text.length > 0
                            Layout.fillWidth: true
                            wrapMode: Text.Wrap
                            textFormat: Text.PlainText
                            color: sampling.samplingWarning ? Theme.warning : Theme.muted
                            font.pointSize: Theme.captionPointSize
                        }
                    }
                }
            }
        }
    }
}
