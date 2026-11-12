import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

ColumnLayout {
    id: pane
    property var measurement: null
    property var magnet: null
    property bool locked: measurement ? measurement.active : false
    property var expandedSections: ({})
    property var recipeDetails: ({})
    readonly property var sections: measurement ? measurement.uiSections : []
    readonly property var issues: measurement ? measurement.validationIssues : []
    signal revealRequested(var item)
    signal sweepPreviewRequested()
    readonly property int additionalStart: {
        for (let i = sections.length - 1; i >= 0; i--)
            if (!sections[i].advanced) return i + 1
        return 0
    }
    spacing: 10
    Layout.fillWidth: true

    Label {
        objectName: "measurementCurrentReversalStatus"
        visible: pane.measurement && (pane.measurement.mode === 4 || pane.measurement.mode === 5) && pane.magnet && pane.magnet.backend === "1000"
        text: pane.magnet ? pane.magnet.currentReversalStatus + "\nChange in Instruments → Sample temperature while idle." : ""
        wrapMode: Text.Wrap
        color: Theme.muted
        Layout.fillWidth: true
    }

    ActionButton {
        objectName: "measurementPreviewSweep"
        text: "Preview sweep range"
        tone: "primary"
        Layout.fillWidth: true
        focusPolicy: Qt.StrongFocus
        enabled: pane.measurement && !pane.locked && !pane.measurement.sweepPreview.busy
        ToolTip.visible: hovered
        ToolTip.text: "Show the planned range, scan order and merged regions without applying instrument outputs"
        onClicked: { forceActiveFocus(); Qt.callLater(() => pane.sweepPreviewRequested()) }
    }

    function sectionTitle(title) {
        const text = String(title || "").replace(/^Advanced /, "")
        return text.charAt(0).toUpperCase() + text.slice(1)
    }
    function sectionKey(title) { return String(measurement ? measurement.mode : 0) + ":" + title }
    function syncRecipeDetails() {
        if (measurement) measurement.setRecipeDetailsVisible(measurement.mode === 4 && Boolean(recipeDetails[sectionKey("conditions")]))
    }
    Component.onCompleted: syncRecipeDetails()
    onRecipeDetailsChanged: syncRecipeDetails()
    Connections {
        target: pane.measurement
        function onModeChanged() { pane.syncRecipeDetails() }
    }
    function isExpanded(section) {
        const key = sectionKey(section.title)
        return expandedSections[key] === undefined ? !section.advanced : expandedSections[key]
    }
    function toggleSection(section) {
        const next = Object.assign({}, expandedSections)
        next[sectionKey(section.title)] = !isExpanded(section)
        expandedSections = next
    }
    function sectionIssues(title) { return issues.filter(issue => issue.section === title) }
    function findItem(parent, predicate) {
        if (predicate(parent)) return parent
        for (let child of parent.children || []) {
            const found = findItem(child, predicate)
            if (found) return found
        }
        return null
    }
    function revealSection(title) {
        const next = Object.assign({}, expandedSections)
        next[sectionKey(title)] = true
        expandedSections = next
        Qt.callLater(() => {
            const header = findItem(pane, item => item.objectName === "measurement_section_" + title)
            if (header) { header.forceActiveFocus(); pane.revealRequested(header) }
        })
    }
    function revealIssue(issue) {
        if (!issue) return
        if (issue.rowKey) measurement.selectRow(issue.rowKey, issue.rowIndex)
        if (measurement.mode === 4 && issue.rowKey === "conditions") {
            const details = Object.assign({}, recipeDetails)
            details[sectionKey("conditions")] = true
            recipeDetails = details
        }
        revealSection(issue.section)
        Qt.callLater(() => Qt.callLater(() => {
            const header = findItem(pane, item => item.objectName === "measurement_section_" + issue.section)
            const input = header ? findItem(header.parent, item => item.visible && item.objectName === "field_" + issue.key) : null
            if (input) { input.forceActiveFocus(); pane.revealRequested(input); return }
            if (issue.rowKey) {
                const table = findItem(pane, item => item.objectName === "recipeTable_" + issue.rowKey)
                if (table) {
                    pane.revealRequested(table)
                    table.revealField(issue.rowIndex, issue.key)
                    return
                }
            }
        }))
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
            readonly property bool fileOutput: modelData.title === "File output"
            readonly property bool rowScoped: (modelData.fields || []).some(field => Boolean(field.rowKey))
            Layout.fillWidth: true
            visible: section.fileOutput || !section.rowScoped || pane.measurement.mode !== 4 || Boolean(section.modelData.rowKey) || Boolean(pane.recipeDetails[pane.sectionKey("conditions")])
            spacing: 6
            Loader {
                Layout.fillWidth: true
                active: section.fileOutput
                visible: active
                sourceComponent: OutputSummary {
                    measurement: pane.measurement
                    formField: pane.measurement.filenameField
                    expanded: section.expanded
                    errorCount: pane.sectionIssues("File output").length
                    onToggleRequested: pane.toggleSection(section.modelData)
                    onCommitted: value => pane.commit(pane.measurement.filenameField, value)
                }
            }
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
            ActionToolButton {
                visible: !section.fileOutput
                objectName: section.fileOutput ? "" : "measurement_section_" + section.modelData.title
                readonly property int errorCount: pane.sectionIssues(section.modelData.title).length
                Layout.fillWidth: true
                padding: 6
                subtle: true
                text: (section.expanded ? "−  " : "+  ") + section.modelData.title
                font.bold: true
                Accessible.name: section.modelData.title + (errorCount ? "; " + errorCount + (errorCount === 1 ? " error" : " errors") : "")
                Accessible.description: section.expanded ? "Expanded section; activate to collapse" : "Collapsed section; activate to expand"
                contentItem: RowLayout {
                    spacing: 8
                    Label {
                        text: (section.expanded ? "−  " : "+  ") + pane.sectionTitle(section.modelData.title)
                        font.bold: true
                        color: Theme.coordinateColor(section.modelData.coordinate || "")
                        wrapMode: Text.Wrap
                        Layout.fillWidth: true
                    }
                    Label {
                        visible: pane.sectionIssues(section.modelData.title).length > 0
                        text: pane.sectionIssues(section.modelData.title).length + (pane.sectionIssues(section.modelData.title).length === 1 ? " error" : " errors")
                        color: Theme.danger
                        font.bold: true
                    }
                    Label {
                        visible: Boolean(section.modelData.advanced) && !section.modelData.recovery
                        text: "Advanced"
                        color: Theme.muted
                        font.pointSize: Theme.captionPointSize
                    }
                }
                onClicked: pane.toggleSection(section.modelData)
            }
            ColumnLayout {
                visible: !section.fileOutput && section.expanded
                Layout.fillWidth: true
                spacing: 6
                Label {
                    text: section.modelData.description || ""
                    visible: text.length > 0
                    Layout.fillWidth: true
                    wrapMode: Text.Wrap
                    color: Theme.muted
                    font.pointSize: Theme.captionPointSize
                }
                Label {
                    text: section.modelData.error || ""
                    visible: text.length > 0
                    color: Theme.danger
                    Layout.fillWidth: true
                    wrapMode: Text.Wrap
                    Accessible.name: "Condition list error: " + text
                }
                Loader {
                    Layout.fillWidth: true
                    active: Boolean(section.modelData.rowKey) && section.modelData.editable !== false
                    visible: active
                    sourceComponent: RecipeTable {
                        objectName: "recipeTable_" + rowKey
                        measurement: pane.measurement
                        rowKey: section.modelData.rowKey || ""
                        locked: pane.locked
                        onDetailsRequested: row => {
                            const next = Object.assign({}, pane.recipeDetails)
                            next[pane.sectionKey("conditions")] = true
                            pane.recipeDetails = next
                        }
                    }
                }
                ActionButton {
                    visible: Boolean(section.modelData.configureRegions)
                    text: "Use Vtg / Vbg regions"
                    enabled: !pane.locked
                    onClicked: { forceActiveFocus(); Qt.callLater(() => pane.measurement.useGateRegions()) }
                }
                RowLayout {
                    visible: pane.measurement.mode === 4 && section.modelData.rowKey === "conditions" && Boolean(pane.recipeDetails[pane.sectionKey("conditions")])
                    Layout.fillWidth: true
                    Label {
                        text: "Advanced settings for condition " + (pane.measurement.mode === 4 ? pane.measurement.rowModel("conditions").selectedIndex + 1 : 0)
                        color: Theme.accent
                        wrapMode: Text.Wrap
                        Layout.fillWidth: true
                    }
                    ActionToolButton {
                        text: "Hide advanced"
                        onClicked: {
                            const next = Object.assign({}, pane.recipeDetails)
                            next[pane.sectionKey("conditions")] = false
                            pane.recipeDetails = next
                        }
                    }
                }
                Item {
                    visible: !Boolean(section.modelData.rowKey)
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
                                model: section.fileOutput ? 0 : (section.modelData.fields || []).length
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
                        Label {
                            objectName: "measurementSampling_" + section.modelData.title
                            text: section.modelData.samplingText || ""
                            visible: text.length > 0
                            Layout.fillWidth: true
                            wrapMode: Text.Wrap
                            textFormat: Text.PlainText
                            color: section.modelData.samplingWarning ? Theme.warning : Theme.muted
                            font.pointSize: Theme.captionPointSize
                        }
                        Label {
                            objectName: "measurementHelp_" + section.modelData.title
                            text: section.modelData.helpText || ""
                            visible: text.length > 0
                            Layout.fillWidth: true
                            wrapMode: Text.Wrap
                            textFormat: Text.PlainText
                            color: Theme.muted
                            font.pointSize: Theme.captionPointSize
                        }
                        Label {
                            objectName: "measurementVoltagePreview_" + section.modelData.title
                            text: section.modelData.previewError || section.modelData.previewText || ""
                            visible: text.length > 0
                            Layout.fillWidth: true
                            wrapMode: Text.Wrap
                            textFormat: Text.PlainText
                            color: section.modelData.previewError ? Theme.danger : Theme.text
                            Accessible.name: "Gate voltage preview: " + text
                        }
                        Label {
                            visible: Boolean(section.modelData.previewText)
                            text: "Preview of committed inputs only; no voltage is applied here. Gate protection is checked before acquisition."
                            Layout.fillWidth: true
                            wrapMode: Text.Wrap
                            color: Theme.muted
                            font.pointSize: Theme.captionPointSize
                        }
                    }
                }
            }
            Rectangle { visible: !section.fileOutput; Layout.fillWidth: true; implicitHeight: 1; color: Theme.border; opacity: 0.55 }
        }
    }
    ColumnLayout {
        id: reviewGroup
        Layout.fillWidth: true
        Layout.topMargin: 6
        Layout.bottomMargin: 2
        spacing: 6
        ActionToolButton {
            id: reviewButton
            objectName: "measurementReviewToggle"
            text: (checked ? "−  " : "+  ") + "Review trajectory / batch jobs"
            checkable: true
            Layout.fillWidth: true
            contentItem: Label { text: reviewButton.text; color: reviewButton.foreground; wrapMode: Text.Wrap }
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
