import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import QtQuick.Dialogs

ApplicationWindow {
    id: window
    width: Math.min(Screen.desktopAvailableWidth, Math.max(1100, backend.layout.width))
    height: Math.min(Screen.desktopAvailableHeight - 48, Math.max(700, backend.layout.height))
    x: backend.layout.x; y: backend.layout.y
    minimumWidth: 1100; minimumHeight: 700
    visible: true
    title: "Transport — Analysis"
    font.family: "Segoe UI"
    font.pointSize: 10
    color: Theme.background
    palette.window: Theme.background
    palette.base: Theme.surface
    palette.windowText: Theme.text
    palette.text: Theme.text
    palette.button: Theme.controlSurface
    palette.buttonText: Theme.text
    palette.highlight: Theme.primary
    palette.highlightedText: Theme.primaryText
    palette.brightText: Theme.primaryText
    palette.mid: Theme.controlBorder
    palette.dark: Theme.controlBorder
    palette.light: Theme.controlHover
    palette.placeholderText: Theme.muted
    palette.disabled.button: Theme.disabledSurface
    palette.disabled.buttonText: Theme.disabledText
    palette.disabled.text: Theme.disabledText
    palette.disabled.windowText: Theme.disabledText
    property var backend: historyBackend
    property bool closeAllowed: false
    property bool rangesOpen: false
    property string exportScope: "both"
    property bool narrowPlots: comparisonContent.width < 1200
    onWidthChanged: if (backend) backend.setLayout("width", width)
    onHeightChanged: if (backend) backend.setLayout("height", height)
    onXChanged: if (backend) backend.setLayout("x", x)
    onYChanged: if (backend) backend.setLayout("y", y)
    onClosing: function(event) { if (!closeAllowed) { event.accepted = false; backend.closeRequested() } }
    Binding { target: Theme; property: "dark"; value: window.backend.dark }

    function chooseFolder() {
        folderDialog.currentFolder = backend.dialogFolderUrl()
        folderDialog.open()
    }
    function chooseRestoreView() {
        restoreDialog.currentFolder = backend.dialogFolderUrl()
        restoreDialog.open()
    }
    function saveAs(scope) {
        exportScope = scope
        saveDialog.selectedFile = backend.suggestedExportUrl(scope)
        saveDialog.open()
    }
    Shortcut { sequence: "Ctrl+F"; onActivated: { historySearch.forceActiveFocus(); historySearch.selectAll() } }
    Shortcut { sequence: "F5"; onActivated: backend.refresh() }
    FolderDialog { id: folderDialog; onAccepted: backend.setFolder(selectedFolder.toString()) }
    FileDialog {
        id: restoreDialog; title: "Restore analysis view"
        nameFilters: ["Analysis view (*_view.json)"]
        onAccepted: backend.restoreView(selectedFile.toString())
    }
    FileDialog {
        id: saveDialog; title: "Export displayed PNG + CSV"
        fileMode: FileDialog.SaveFile; defaultSuffix: "png"
        nameFilters: ["PNG image (*.png)"]
        onAccepted: backend.exportView(window.exportScope, selectedFile.toString())
    }
    ActionDialog {
        id: rangeDialog; objectName: "historyRangeDialog"
        title: backend.hasMap ? "Axis and color ranges" : "Axis ranges"; modal: true
        width: Math.min(window.width - 64, 640)
        anchors.centerIn: parent
        visible: window.rangesOpen
        onClosed: window.rangesOpen = false
        standardButtons: Dialog.Close
        contentItem: ColumnLayout {
            spacing: 12
            Label { text: "Use Auto to fit the displayed data, or Fixed to enter bounds."; color: Theme.muted; Layout.fillWidth: true; wrapMode: Text.Wrap }
            RowLayout {
                visible: !backend.hasMap && backend.rangeChannels.length > 1
                Layout.fillWidth: true
                Label { text: "Y channel"; color: Theme.muted }
                SelectBox {
                    objectName: "historyRangeChannel"; Layout.fillWidth: true
                    model: backend.rangeChannels
                    currentIndex: model.indexOf(backend.rangeChannel)
                    onActivated: backend.setRangeChannel(currentText)
                    Accessible.name: "Y range channel"
                }
            }
            HistoryRange { backend: window.backend; axis: "x"; label: backend.hasMap ? "Map X" : "X"; Layout.fillWidth: true }
            HistoryRange { backend: window.backend; axis: "y"; label: backend.hasMap ? "Map Y" : backend.rangeChannel === "Shared Y" ? "Shared Y" : "Y"; Layout.fillWidth: true }
            HistoryRange { backend: window.backend; axis: "color"; label: "Signal"; visible: backend.hasMap; Layout.fillWidth: true }
        }
    }
    ActionDialog {
        id: coverageDialog; objectName: "historyCoverageDetails"
        title: "Files used in this view"; modal: true
        width: Math.min(window.width - 80, 900); height: Math.min(window.height - 80, 520)
        anchors.centerIn: parent
        standardButtons: Dialog.Close
        contentItem: ScrollView {
            TextArea {
                objectName: "historyCoverageText"
                text: backend.coverageDetails
                readOnly: true; selectByMouse: true
                wrapMode: TextEdit.Wrap; color: Theme.text
            }
        }
    }
    ActionDialog {
        id: detailsDialog; objectName: "historyDetails"
        title: "History details"; modal: true
        width: Math.min(window.width - 80, 900); height: Math.min(window.height - 80, 520)
        anchors.centerIn: parent
        standardButtons: Dialog.Close
        contentItem: ColumnLayout {
            ScrollView {
                Layout.fillWidth: true; Layout.fillHeight: true
                TextArea { text: backend.statusDetails; readOnly: true; selectByMouse: true; wrapMode: TextEdit.Wrap; color: Theme.text }
            }
            ActionButton { text: "Copy all details"; onClicked: backend.copyStatus() }
        }
    }
    ColumnLayout {
        anchors.fill: parent; anchors.margins: 12; spacing: 6
        RowLayout {
            Layout.fillWidth: true
            Label { text: "Analysis"; font.pixelSize: 23; font.bold: true; color: Theme.text }
            Label { text: "Saved measurements · Read-only"; color: Theme.muted }
            Item { Layout.fillWidth: true }
            BusyIndicator { running: backend.busy; implicitWidth: 28; implicitHeight: 28 }
            ActionButton { objectName: "historyThemeToggle"; text: "Dark theme"; checkable: true; checked: backend.dark; onToggled: if (checked !== backend.dark) backend.setDark(checked); Accessible.name: "Dark theme" }
            ActionButton { text: "Refresh"; onClicked: backend.refresh() }
            ActionButton { text: "Reload analysis"; visible: historyConnected; onClicked: backend.requestReload() }
        }
        RowLayout {
            Layout.fillWidth: true
            Label { text: "Folder"; color: Theme.muted }
            Label { text: backend.folder; color: Theme.muted; elide: Text.ElideMiddle; Layout.fillWidth: true; Accessible.name: "History folder: " + text; ToolTip.text: text; ToolTip.visible: folderHover.hovered; HoverHandler { id: folderHover } }
            ActionButton { text: "Browse folder…"; onClicked: window.chooseFolder() }
            ActionButton { text: "Follow current device"; enabled: !backend.following; onClicked: backend.followCurrent(); ToolTip.text: "Follow the data folder selected in the measurement window"; ToolTip.visible: hovered }
        }
        TabBar {
            Layout.fillWidth: true
            Repeater {
                model: backend.measurementTypes
                NavigationTabButton { required property var modelData; text: modelData.label; checked: backend.currentType === modelData.key; onClicked: backend.selectType(modelData.key) }
            }
        }
        SplitView {
            id: split; Layout.fillWidth: true; Layout.fillHeight: true
            orientation: Qt.Horizontal
            handle: Rectangle {
                id: fileSplitHandle
                implicitWidth: 10
                color: "transparent"
                Rectangle {
                    anchors.centerIn: parent
                    width: fileSplitHandle.SplitHandle.pressed ? 3 : 1
                    height: parent.height
                    color: fileSplitHandle.SplitHandle.hovered || fileSplitHandle.SplitHandle.pressed ? Theme.accent : Theme.border
                }
                Rectangle {
                    anchors.centerIn: parent
                    width: 3; height: 28; radius: 1
                    color: fileSplitHandle.SplitHandle.hovered || fileSplitHandle.SplitHandle.pressed ? Theme.accent : Theme.border
                }
            }
            onResizingChanged: if (!resizing && !backend.expanded) backend.setLayout("fileWidth", filePane.width)
            Frame {
                id: filePane; objectName: "historyFilePane"
                padding: 10
                SplitView.preferredWidth: backend.expanded ? split.width : Math.min(backend.layout.fileWidth, split.width * 0.34)
                SplitView.minimumWidth: 245
                ColumnLayout {
                    anchors.fill: parent; spacing: 4
                    RowLayout {
                        Layout.fillWidth: true
                        Label { text: "Files"; font.bold: true; font.pixelSize: 17; color: Theme.text; Layout.fillWidth: true }
                        ActionButton { objectName: "historyExpandFiles"; text: backend.expanded ? "Compare files" : "Expand list"; onClicked: backend.setOption("expanded", !backend.expanded) }
                    }
                    SelectBox { objectName: "historyDateFilter"; Layout.fillWidth: true; model: backend.dates; currentIndex: Math.max(0, model.indexOf(backend.date)); onActivated: backend.setOption("date", currentText); Accessible.name: "Date filter" }
                    InputField { id: historySearch; objectName: "historySearch"; Layout.fillWidth: true; placeholderText: "Search filename, conditions, status"; text: backend.search; onTextEdited: backend.queueSearch(text); Accessible.name: "Search history"; selectByMouse: true }
                    RowLayout {
                        Layout.fillWidth: true
                        Label { text: fileList.count + " / " + backend.typeFileCount + " visible"; color: Theme.muted; Layout.fillWidth: true }
                        ActionButton { objectName: "historyClearFilters"; text: "Clear filters"; enabled: backend.search.length > 0 || backend.date !== "All dates"; onClicked: backend.clearFilters() }
                    }
                    RowLayout {
                        Label { text: backend.selectedCount > 0 ? backend.selectedCount + " selected" : "Check files to compare"; font.bold: backend.selectedCount > 0; color: Theme.text; Layout.fillWidth: true; ToolTip.text: "Selections stay checked when you filter the list"; ToolTip.visible: selectionHover.hovered; HoverHandler { id: selectionHover } }
                        ActionButton { text: "Clear selection"; enabled: backend.selectedCount > 0; onClicked: backend.clearSelection() }
                    }
                    Label {
                        objectName: "historyHiddenSelection"
                        visible: backend.hiddenSelectedCount > 0
                        text: backend.hiddenSelectedCount + " selected files hidden by filters"
                        color: Theme.accent; font.pointSize: Theme.captionPointSize
                        Layout.fillWidth: true; wrapMode: Text.Wrap
                    }
                    ListView {
                        id: fileList; Layout.fillWidth: true; Layout.fillHeight: true
                        objectName: "historyFileList"
                        clip: true; spacing: 4; model: backend.fileModel
                        reuseItems: true
                        ScrollBar.vertical: ScrollBar { }
                        onMovementEnded: backend.setOption("scroll", contentY)
                        Connections {
                            target: backend
                            function onFilesChanged() { Qt.callLater(function() { fileList.contentY = Math.max(0, Math.min(backend.scrollPosition, fileList.contentHeight - fileList.height)) }) }
                        }
                        delegate: Rectangle {
                            required property var modelData; required property int index
                            width: fileList.width; height: row.implicitHeight + 16
                            color: modelData.checked ? Theme.selection : Theme.surface
                            radius: 5; border.color: modelData.checked ? Theme.accent : Theme.border
                            RowLayout {
                                id: row; anchors.left: parent.left; anchors.right: parent.right; anchors.top: parent.top; anchors.margins: 8
                                StateCheckBox { checked: modelData.checked; onClicked: backend.setChecked(modelData.path, checked); Accessible.name: "Compare " + modelData.name }
                                ColumnLayout {
                                    Layout.fillWidth: true; spacing: 3
                                    Label { text: modelData.time; font.pixelSize: 12; color: Theme.muted }
                                    Label { text: modelData.name.replace(/_/g, "_\u200b"); wrapMode: Text.WrapAnywhere; Layout.fillWidth: true; font.bold: true; color: Theme.text }
                                    Label { text: modelData.conditions; wrapMode: Text.Wrap; Layout.fillWidth: true; font.pixelSize: 12; color: Theme.muted }
                                    Label { text: modelData.status; font.pixelSize: 12; color: modelData.status === "finished" ? Theme.muted : Theme.warning }
                                }
                            }
                        }
                        Label {
                            anchors.fill: parent; anchors.margins: 12
                            visible: fileList.count === 0
                            text: backend.emptyState.title + "\n\n" + backend.emptyState.detail
                            wrapMode: Text.Wrap; color: Theme.muted; verticalAlignment: Text.AlignVCenter
                        }
                    }
                }
            }
            Pane {
                id: comparison; objectName: "historyComparison"; visible: !backend.expanded
                SplitView.fillWidth: true; SplitView.minimumWidth: 540
                padding: 10
                background: null
                ColumnLayout {
                id: comparisonContent
                anchors.fill: parent; spacing: 6
                RowLayout {
                    Layout.fillWidth: true
                    Label { text: "Comparison"; font.pixelSize: 17; font.bold: true; color: Theme.text; Layout.fillWidth: true }
                    ActionButton { objectName: "historyRestoreView"; text: "Restore view…"; onClicked: window.chooseRestoreView(); ToolTip.text: "Open an exported analysis view (*_view.json)"; ToolTip.visible: hovered }
                    ActionButton {
                        objectName: "historyExportView"; text: "Export PNG + CSV…"; enabled: backend.exportReady
                        highlighted: true
                        onClicked: backend.hasMap ? exportMenu.open() : window.saveAs("both")
                        ToolTip.text: "Save the displayed plot, CSV data and a restorable analysis view"; ToolTip.visible: hovered
                        Menu {
                            id: exportMenu
                            MenuItem { objectName: "historyExportHeatmapItem"; text: "Heatmap PNG + CSV…"; onTriggered: window.saveAs("heatmap") }
                            MenuItem { objectName: "historyExportCutItem"; text: "Measured cut PNG + CSV…"; enabled: backend.cutReady; onTriggered: window.saveAs("cut") }
                            MenuItem { objectName: "historyExportBothItem"; text: "Heatmap + cut PNG + CSV…"; enabled: backend.cutReady; onTriggered: window.saveAs("both") }
                        }
                    }
                }
                Flow {
                    objectName: "historySettings"
                    Layout.fillWidth: true; Layout.preferredHeight: implicitHeight
                    spacing: 8
                    Row {
                    spacing: 5
                    Label { text: "X axis"; anchors.verticalCenter: parent.verticalCenter }
                    SelectBox {
                        width: 150; model: backend.xChoices
                        enabled: !backend.busy && model.length > 0
                        displayText: currentIndex >= 0 ? backend.axisLabel(backend.state.x) : backend.busy ? "Checking data…" : "Select files"
                        currentIndex: model.indexOf(backend.state.x); onActivated: backend.setOption("x", currentText)
                        Accessible.name: "X coordinate"
                        delegate: ItemDelegate { required property var modelData; width: parent.width; text: backend.axisLabel(modelData) }
                    }
                    }
                    Row {
                        spacing: 5
                        Label { text: "Signal"; anchors.verticalCenter: parent.verticalCenter }
                        SelectBox {
                            objectName: "historySignal"
                            width: 200; model: backend.signalChoices
                            enabled: !backend.busy && model.length > 0
                            currentIndex: model.indexOf(backend.state.signal)
                            displayText: currentIndex >= 0 ? currentText : backend.busy ? "Checking data…" : backend.selectedCount ? "No compatible signal" : "Select files"
                            onActivated: backend.setOption("signal", currentText)
                            Accessible.name: "Signal"
                            ToolTip.text: backend.choiceNote; ToolTip.visible: hovered
                        }
                    }
                    Row {
                        spacing: 5
                        Label { text: "Direction"; anchors.verticalCenter: parent.verticalCenter }
                        SelectBox { width: 115; model: ["All", "forward", "backward", "unknown"]; currentIndex: Math.max(0, model.indexOf(backend.state.direction)); onActivated: backend.setOption("direction", currentText); Accessible.name: "Direction" }
                    }
                    Row {
                        spacing: 5
                        Label { text: backend.hasMap ? "Cut scale" : "Normalize"; anchors.verticalCenter: parent.verticalCenter }
                        SelectBox { width: 110; model: ["Raw", "Max |Y|", "0–1"]; currentIndex: ["raw", "max_abs", "min_max"].indexOf(backend.state.normalization); onActivated: backend.setOption("normalization", ["raw", "max_abs", "min_max"][currentIndex]); Accessible.name: "Normalization" }
                    }
                    ActionButton { objectName: "historyRanges"; text: backend.hasMap ? "Axis / color ranges…" : "Axis ranges…"; enabled: backend.exportReady; onClicked: window.rangesOpen = true }
                }
                RowLayout {
                    Layout.fillWidth: true
                    Label {
                        objectName: "historyCoverageSummary"
                        text: backend.coverageSummary
                        color: Theme.text; font.bold: backend.selectedCount > 0
                        Layout.fillWidth: true; wrapMode: Text.Wrap
                        Accessible.name: text
                    }
                    ActionButton {
                        objectName: "historyCoverageButton"
                        text: "File details…"; enabled: backend.selectedCount > 0 && !backend.busy
                        onClicked: coverageDialog.open()
                        ToolTip.text: "Show plotted files and why other checked files were skipped"
                        ToolTip.visible: hovered
                    }
                }
                Label { text: (backend.selectedCount > 0 ? "Selected conditions: " : "") + backend.selectionSummary; color: Theme.muted; Layout.fillWidth: true; wrapMode: Text.Wrap; maximumLineCount: 2; elide: Text.ElideRight; ToolTip.text: text; ToolTip.visible: summaryHover.hovered; HoverHandler { id: summaryHover } }
                ColumnLayout {
                    visible: backend.currentType === "map_2d"
                    Layout.fillWidth: true
                    Layout.preferredHeight: visible ? implicitHeight : 0
                    ColumnLayout {
                        id: mapControls; Layout.fillWidth: true; spacing: 4
                        Flow {
                            Layout.fillWidth: true; Layout.preferredHeight: implicitHeight; spacing: 5
                            Row {
                            spacing: 5
                            Label { text: "Map source"; font.bold: true; anchors.verticalCenter: parent.verticalCenter }
                            SelectBox {
                                width: Math.min(260, comparison.width - 160); model: backend.mapSources; textRole: "label"; valueRole: "path"
                                currentIndex: { var sources = model; var selected = backend.state.primary_path; for (var i = 0; i < sources.length; ++i) if (sources[i].path === selected) return i; return 0 }
                                onActivated: backend.setOption("primary_path", currentValue); Accessible.name: "Map source file"
                                ToolTip.text: currentText; ToolTip.visible: hovered
                            }
                            }
                            Row {
                            spacing: 5
                            Label { text: "Y axis"; anchors.verticalCenter: parent.verticalCenter }
                            SelectBox { width: 130; model: backend.yChoices; enabled: !backend.busy && backend.xChoices.length > 0; currentIndex: model.indexOf(backend.state.map_y); onActivated: backend.setOption("map_y", currentText); Accessible.name: "Map Y coordinate" }
                            }
                            StateCheckBox { text: "Infer axes"; checked: backend.state.auto_axes; onClicked: backend.setOption("auto_axes", checked) }
                        }
                        Flow {
                            visible: backend.state.map_y !== "None (1D)"; Layout.fillWidth: true; Layout.preferredHeight: visible ? implicitHeight : 0; spacing: 5
                            Row {
                            spacing: 5
                            Label { text: "Measured cut"; font.bold: true; anchors.verticalCenter: parent.verticalCenter }
                            SelectBox { width: 120; model: backend.passes; currentIndex: Math.max(0, model.indexOf(backend.state.pass)); onActivated: backend.setOption("pass", currentText); Accessible.name: "Row or pass" }
                            }
                            Row {
                            spacing: 5
                            Label { text: "Fixed axis"; anchors.verticalCenter: parent.verticalCenter }
                            SelectBox { width: 95; model: [backend.state.x, backend.state.map_y]; currentIndex: Math.max(0, model.indexOf(backend.state.fixed_axis)); onActivated: backend.setOption("fixed_axis", currentText); Accessible.name: "Fixed cut axis" }
                            }
                            Row {
                            spacing: 5
                            Label { text: "Value"; anchors.verticalCenter: parent.verticalCenter }
                            HistoryCoordinate { width: 155; backend: window.backend }
                            }
                        }
                    }
                }
                TabBar {
                    id: plotTabs; objectName: "historyPlotTabs"; visible: backend.hasMap && window.narrowPlots
                    Layout.fillWidth: true
                    NavigationTabButton { objectName: "historyHeatmapTab"; text: "Heatmap · " + backend.state.signal }
                    NavigationTabButton { objectName: "historyCutTab"; text: "Measured cut · " + backend.state.fixed_axis + " = " + backend.cutCoordinateLabel; ToolTip.text: backend.state.fixed_axis + " = " + backend.cutCoordinateExact + " · " + backend.traceCount + " displayed traces"; ToolTip.visible: hovered }
                }
                Item {
                    Layout.fillWidth: true; Layout.fillHeight: true; Layout.minimumHeight: 180
                    RowLayout {
                        anchors.fill: parent; spacing: 10
                        ColumnLayout {
                        visible: !backend.hasMap || !window.narrowPlots || plotTabs.currentIndex === 0
                        Layout.fillWidth: true; Layout.fillHeight: true; Layout.preferredWidth: 1
                        spacing: 6
                        Label { objectName: "historyHeatmapTitle"; visible: backend.hasMap && !window.narrowPlots; text: "Heatmap — " + backend.state.signal; font.bold: true; Layout.fillWidth: true; color: Theme.text; elide: Text.ElideRight }
                        PlotPane {
                            objectName: "historyMainPlot"; darkMode: backend.dark
                            visible: !backend.hasMap || !window.narrowPlots || plotTabs.currentIndex === 0
                            Layout.fillWidth: true; Layout.fillHeight: true
                            plotModel: backend.plotModel; showAxisSelector: false; showChannelSelector: false; showLayoutSelector: false
                            showExport: false; exportEnabled: backend.exportReady; showEmptyState: false
                        }
                        }
                        ColumnLayout {
                        visible: backend.hasMap && (!window.narrowPlots || plotTabs.currentIndex === 1)
                        Layout.fillWidth: true; Layout.fillHeight: true; Layout.preferredWidth: 1
                        spacing: 6
                        Label { objectName: "historyCutTitle"; visible: !window.narrowPlots; text: "Measured cut — " + backend.state.fixed_axis + " = " + backend.cutCoordinateLabel; font.bold: true; Layout.fillWidth: true; color: Theme.text; elide: Text.ElideRight; ToolTip.text: backend.state.fixed_axis + " = " + backend.cutCoordinateExact; ToolTip.visible: cutTitleHover.hovered; HoverHandler { id: cutTitleHover } }
                        PlotPane {
                            objectName: "historyCutPlot"; darkMode: backend.dark
                            visible: backend.hasMap && (!window.narrowPlots || plotTabs.currentIndex === 1)
                            Layout.fillWidth: true; Layout.fillHeight: true
                            plotModel: backend.cutPlotModel; showAxisSelector: false; showChannelSelector: false; showLayoutSelector: false
                            showExport: false; exportEnabled: backend.cutReady
                            emptyText: backend.cutEmptyText
                        }
                        }
                    }
                    Rectangle {
                        visible: backend.emptyState.title.length > 0 && !backend.busy && (!backend.exportReady || backend.selectedCount === 0)
                        anchors.fill: parent; color: Theme.surface; radius: 6; border.color: Theme.border
                        ColumnLayout {
                            anchors.centerIn: parent; width: Math.min(parent.width - 40, 520); spacing: 12
                            Label { text: backend.emptyState.title; font.bold: true; font.pixelSize: 20; color: Theme.text; Layout.fillWidth: true; wrapMode: Text.Wrap; horizontalAlignment: Text.AlignHCenter }
                            Label { text: backend.emptyState.detail; color: Theme.muted; Layout.fillWidth: true; wrapMode: Text.Wrap; horizontalAlignment: Text.AlignHCenter }
                            ActionButton { Layout.alignment: Qt.AlignHCenter; visible: backend.emptyState.action.length > 0; text: backend.emptyState.action === "filters" ? "Clear filters" : "Browse folder…"; onClicked: backend.emptyState.action === "filters" ? backend.clearFilters() : window.chooseFolder() }
                        }
                    }
                    Rectangle {
                        visible: backend.busy; anchors.top: parent.top; anchors.horizontalCenter: parent.horizontalCenter
                        width: Math.min(parent.width - 20, 510); height: 48; color: Theme.surface; radius: 6; border.color: Theme.accent
                        RowLayout {
                            anchors.fill: parent; anchors.margins: 7
                            BusyIndicator { running: backend.busy; implicitWidth: 30; implicitHeight: 30 }
                            Label { text: backend.traceCount > 0 || backend.hasMap ? "Updating selection… Previous plot remains visible." : "Loading selected files and display settings…"; color: Theme.text; Layout.fillWidth: true; wrapMode: Text.Wrap }
                        }
                    }
                }
                }
            }
        }
        RowLayout {
            Layout.fillWidth: true
            Label { Layout.fillWidth: true; text: backend.statusDetails.replace(/\n/g, " · "); elide: Text.ElideRight; color: Theme.muted; font.pixelSize: 12 }
            ActionButton { text: "Details (" + backend.statusDetails.split("\n").length + ")…"; visible: backend.statusDetails.length > 0; onClicked: detailsDialog.open() }
        }
    }
}
