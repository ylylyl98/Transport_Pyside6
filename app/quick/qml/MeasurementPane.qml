import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

ColumnLayout {
    id: pane
    property var measurement: null
    property bool locked: measurement ? measurement.active : false
    property var expandedSections: ({})
    readonly property var sections: measurement ? measurement.uiSections : []
    readonly property int additionalStart: {
        for (let i = sections.length - 1; i >= 0; i--)
            if (!sections[i].advanced) return i + 1
        return 0
    }
    spacing: 10
    Layout.fillWidth: true

    function sectionTitle(title) {
        const text = String(title || "").replace(/^Advanced /, "")
        return text.charAt(0).toUpperCase() + text.slice(1)
    }
    function sectionKey(title) { return String(measurement ? measurement.mode : 0) + ":" + title }
    function isExpanded(section) {
        const key = sectionKey(section.title)
        return expandedSections[key] === undefined ? !section.advanced : expandedSections[key]
    }
    function toggleSection(section) {
        const next = Object.assign({}, expandedSections)
        next[sectionKey(section.title)] = !isExpanded(section)
        expandedSections = next
    }
    function commit(field, value) {
        if (!measurement || locked || field.readOnly) return
        if (field.rowKey) measurement.setRowValue(field.rowKey, field.rowIndex, field.key, value)
        else measurement.setValue(field.key, value)
    }

    Repeater {
        // Keep delegates alive when values change. A QVariantList as the model
        // resets every control on each commit, including the next focused edit.
        model: pane.sections.length
        delegate: ColumnLayout {
            id: section
            required property int index
            readonly property var modelData: pane.sections[index] || ({})
            readonly property bool expanded: pane.isExpanded(modelData)
            readonly property bool rowScoped: (modelData.fields || []).some(field => Boolean(field.rowKey))
            Layout.fillWidth: true
            spacing: 6
            Label {
                objectName: "measurementAdditionalSettings_" + section.index
                visible: section.index === pane.additionalStart && Boolean(section.modelData.advanced)
                text: "Additional settings"
                color: Theme.muted
                font.bold: true
                Layout.fillWidth: true
                Layout.topMargin: 6
                Layout.bottomMargin: 2
            }
            ToolButton {
                objectName: "measurement_section_" + section.modelData.title
                Layout.fillWidth: true
                padding: 4
                text: (section.expanded ? "−  " : "+  ") + section.modelData.title
                font.bold: true
                Accessible.name: section.modelData.title
                Accessible.description: section.expanded ? "Expanded section; activate to collapse" : "Collapsed section; activate to expand"
                contentItem: RowLayout {
                    spacing: 8
                    Label {
                        text: (section.expanded ? "−  " : "+  ") + pane.sectionTitle(section.modelData.title)
                        font.bold: true
                        color: Theme.text
                        wrapMode: Text.Wrap
                        Layout.fillWidth: true
                    }
                    Label {
                        visible: Boolean(section.modelData.advanced)
                        text: "Advanced"
                        color: Theme.muted
                        font.pointSize: 9
                    }
                }
                onClicked: pane.toggleSection(section.modelData)
            }
            ColumnLayout {
                visible: section.expanded
                Layout.fillWidth: true
                spacing: 6
                Label {
                    text: section.modelData.description || ""
                    visible: text.length > 0
                    Layout.fillWidth: true
                    wrapMode: Text.Wrap
                    color: Theme.muted
                    font.pointSize: 9
                }
                Label {
                    text: section.modelData.error || ""
                    visible: text.length > 0
                    color: Theme.danger
                    Layout.fillWidth: true
                    wrapMode: Text.Wrap
                    Accessible.name: "Condition list error: " + text
                }
                ScrollView {
                    id: rowList
                    objectName: "measurementRows_" + (section.modelData.rowKey || "")
                    visible: Boolean(section.modelData.rowKey) && (section.modelData.rows || []).length > 0
                    Layout.fillWidth: true
                    Layout.preferredHeight: Math.min(170, rowsColumn.implicitHeight)
                    contentWidth: availableWidth
                    ScrollBar.horizontal.policy: ScrollBar.AlwaysOff
                    ColumnLayout {
                        id: rowsColumn
                        width: rowList.availableWidth
                        spacing: 3
                        Repeater {
                            model: (section.modelData.rows || []).length
                            delegate: ItemDelegate {
                                id: recipeRow
                                required property int index
                                readonly property var modelData: (section.modelData.rows || [])[index] || ({})
                                objectName: "measurementRow_" + section.modelData.rowKey + "_" + index
                                Layout.fillWidth: true
                                highlighted: modelData.index === section.modelData.selectedIndex
                                function revealSelection() {
                                    if (!rowList || !highlighted || !rowList.visible) return
                                    const flick = rowList.contentItem
                                    if (!flick || flick.contentY === undefined) return
                                    if (y < flick.contentY) flick.contentY = y
                                    else if (y + height > flick.contentY + flick.height)
                                        flick.contentY = Math.min(y + height - flick.height, Math.max(0, flick.contentHeight - flick.height))
                                }
                                onHighlightedChanged: if (highlighted) Qt.callLater(revealSelection)
                                Component.onCompleted: if (highlighted) Qt.callLater(revealSelection)
                                background: Rectangle {
                                    color: recipeRow.highlighted ? Theme.selection : recipeRow.hovered ? Theme.background : Theme.surface
                                    border.color: recipeRow.highlighted ? Theme.accent : Theme.border
                                    border.width: recipeRow.highlighted || recipeRow.activeFocus ? 2 : 1
                                    radius: 3
                                }
                                Accessible.name: (modelData.enabled ? "Enabled " : "Disabled ") + modelData.name + ". " + modelData.detail
                                contentItem: ColumnLayout {
                                    spacing: 2
                                    Label {
                                        text: (recipeRow.modelData.enabled ? "On · " : "Off · ") + (recipeRow.modelData.index+1) + ". " + recipeRow.modelData.name
                                        color: Theme.text
                                        font.bold: recipeRow.highlighted
                                        Layout.fillWidth: true
                                        wrapMode: Text.WrapAnywhere
                                    }
                                    Label {
                                        text: recipeRow.modelData.detail
                                        color: Theme.muted
                                        font.pointSize: 9
                                        Layout.fillWidth: true
                                        wrapMode: Text.Wrap
                                    }
                                }
                                onClicked: pane.measurement.selectRow(section.modelData.rowKey, modelData.index)
                            }
                        }
                    }
                }
                RowLayout {
                    visible: Boolean(section.modelData.rowKey)
                    Layout.fillWidth: true
                    spacing: 4
                    Button {
                        objectName: "measurementAdd_" + section.modelData.rowKey
                        text: "Add"
                        enabled: !pane.locked
                        Accessible.name: "Add " + (section.modelData.rowKey === "regions" ? "region" : "condition")
                        ToolTip.visible: hovered
                        ToolTip.text: "Add and edit a new " + (section.modelData.rowKey === "regions" ? "region" : "condition")
                        onClicked: pane.measurement.addRow(section.modelData.rowKey)
                    }
                    Button {
                        objectName: "measurementDuplicate_" + section.modelData.rowKey
                        text: "Duplicate"
                        enabled: !pane.locked && section.modelData.selectedIndex >= 0
                        Accessible.name: "Duplicate selected " + (section.modelData.rowKey === "regions" ? "region" : "condition")
                        ToolTip.visible: hovered
                        ToolTip.text: "Copy the selected row and edit the copy"
                        onClicked: pane.measurement.duplicateRow(section.modelData.rowKey, section.modelData.selectedIndex)
                    }
                    Button {
                        objectName: "measurementRemove_" + section.modelData.rowKey
                        text: "Remove"
                        enabled: !pane.locked && section.modelData.selectedIndex >= 0
                        Accessible.name: "Remove selected " + (section.modelData.rowKey === "regions" ? "region" : "condition")
                        ToolTip.visible: hovered
                        ToolTip.text: "Remove the selected row"
                        onClicked: pane.measurement.removeRow(section.modelData.rowKey, section.modelData.selectedIndex)
                    }
                }
                Rectangle {
                    visible: Boolean(section.modelData.rowKey) && section.modelData.selectedIndex >= 0
                    Layout.fillWidth: true
                    Layout.topMargin: 3
                    implicitHeight: 1
                    color: Theme.border
                }
                Item {
                    Layout.fillWidth: true
                    implicitHeight: editorContent.implicitHeight
                    Rectangle {
                        visible: section.rowScoped
                        anchors.left: parent.left
                        anchors.top: parent.top
                        anchors.bottom: parent.bottom
                        width: 2
                        color: Theme.border
                    }
                    ColumnLayout {
                        id: editorContent
                        anchors.left: parent.left
                        anchors.right: parent.right
                        anchors.leftMargin: section.rowScoped ? 10 : 0
                        spacing: 6
                        Label {
                            objectName: "measurementEditing_" + (section.modelData.rowKey || "")
                            visible: Boolean(section.modelData.rowKey) && section.modelData.selectedIndex >= 0
                            text: "Editing " + (section.modelData.rowKey === "regions" ? "region " : "condition ")
                                + (section.modelData.selectedIndex + 1) + " of " + (section.modelData.rows || []).length
                            color: Theme.accent
                            Layout.fillWidth: true
                            wrapMode: Text.Wrap
                        }
                        GridLayout {
                            Layout.fillWidth: true
                            columns: section.modelData.columns || 2
                            columnSpacing: 10
                            rowSpacing: 6
                            Repeater {
                                model: (section.modelData.fields || []).length
                                delegate: FieldEditor {
                                    required property int index
                                    readonly property var modelData: (section.modelData.fields || [])[index] || ({})
                                    Layout.fillWidth: true
                                    Layout.minimumWidth: 0
                                    Layout.preferredWidth: 150
                                    Layout.columnSpan: modelData.columnSpan || (modelData.kind === "text" || (modelData.key === "sweep_both_ways" && section.modelData.columns === 3) ? (section.modelData.columns || 2) : 1)
                                    formField: modelData
                                    scopeKey: String(pane.measurement ? pane.measurement.mode : 0)
                                    locked: pane.locked
                                    onCommitted: value => pane.commit(modelData, value)
                                }
                            }
                        }
                    }
                }
            }
            Rectangle { Layout.fillWidth: true; implicitHeight: 1; color: Theme.border; opacity: 0.55 }
        }
    }
    ColumnLayout {
        id: reviewGroup
        Layout.fillWidth: true
        Layout.topMargin: 6
        Layout.bottomMargin: 2
        spacing: 6
        ToolButton {
            id: reviewButton
            objectName: "measurementReviewToggle"
            text: (checked ? "−  " : "+  ") + "Review trajectory / batch jobs"
            checkable: true
            Layout.fillWidth: true
            contentItem: Label { text: reviewButton.text; color: Theme.accent; wrapMode: Text.Wrap }
        }
        Label {
            objectName: "measurementTrajectoryReview"
            visible: reviewButton.checked
            // A hidden binding still runs: build map previews only on request.
            text: reviewButton.checked && pane.measurement ? pane.measurement.summary : ""
            Layout.fillWidth: true
            wrapMode: Text.Wrap
            color: Theme.muted
            Accessible.name: "Trajectory and job review"
        }
    }
}
