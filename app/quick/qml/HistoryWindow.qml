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
    title: "Transport — Measurement History (Qt Quick)"
    font.family: "Segoe UI"
    font.pointSize: 10
    color: Theme.background
    palette.window: Theme.background
    palette.base: Theme.surface
    palette.windowText: Theme.text
    palette.text: Theme.text
    palette.button: Theme.surface
    palette.buttonText: Theme.text
    palette.highlight: Theme.accent
    palette.placeholderText: Theme.muted
    palette.disabled.buttonText: Theme.muted
    palette.disabled.text: Theme.muted
    palette.disabled.windowText: Theme.muted
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
    Dialog {
        id: rangeDialog; objectName: "historyRangeDialog"
        title: "Axis and color ranges"; modal: true
        width: Math.min(window.width - 64, 640)
        anchors.centerIn: parent
        visible: window.rangesOpen
        onClosed: window.rangesOpen = false
        standardButtons: Dialog.Close
        contentItem: ColumnLayout {
            spacing: 12
            Label { text: "Use Auto to fit the displayed data, or Fixed to enter bounds."; color: Theme.muted; Layout.fillWidth: true; wrapMode: Text.Wrap }
            HistoryRange { backend: window.backend; axis: "x"; label: "Map X"; Layout.fillWidth: true }
            HistoryRange { backend: window.backend; axis: "y"; label: "Map Y"; Layout.fillWidth: true }
            HistoryRange { backend: window.backend; axis: "color"; label: "Signal"; Layout.fillWidth: true }
        }
    }
    Dialog {
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
            Button { text: "Copy all details"; onClicked: backend.copyStatus() }
        }
    }
    ColumnLayout {
        anchors.fill: parent; anchors.margins: 12; spacing: 6
        RowLayout {
            Layout.fillWidth: true
            Label { text: "Measurement History"; font.pixelSize: 23; font.bold: true; color: Theme.text }
            Item { Layout.fillWidth: true }
            BusyIndicator { running: backend.busy; implicitWidth: 28; implicitHeight: 28 }
            Button { objectName: "historyThemeToggle"; text: "Dark theme"; checkable: true; checked: backend.dark; onToggled: if (checked !== backend.dark) backend.setDark(checked); Accessible.name: "Dark theme" }
            Button { text: "Refresh"; onClicked: backend.refresh() }
            Button { text: "Reload analysis"; visible: historyConnected; onClicked: backend.requestReload() }
        }
        RowLayout {
            Layout.fillWidth: true
            Label { text: "Folder"; color: Theme.muted }
            Label { text: backend.folder; color: Theme.muted; elide: Text.ElideMiddle; Layout.fillWidth: true; Accessible.name: "History folder: " + text; ToolTip.text: text; ToolTip.visible: folderHover.hovered; HoverHandler { id: folderHover } }
            Button { text: "Browse folder…"; onClicked: window.chooseFolder() }
            Button { text: "Follow current device"; enabled: !backend.following; onClicked: backend.followCurrent(); ToolTip.text: "Follow the data folder selected in the measurement window"; ToolTip.visible: hovered }
        }
        TabBar {
            Layout.fillWidth: true
            Repeater {
                model: backend.measurementTypes
                TabButton { required property var modelData; text: modelData.label; checked: backend.currentType === modelData.key; onClicked: backend.selectType(modelData.key) }
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
                        Button { objectName: "historyExpandFiles"; text: backend.expanded ? "Compare files" : "Expand list"; onClicked: backend.setOption("expanded", !backend.expanded) }
                    }
                    ComboBox { objectName: "historyDateFilter"; Layout.fillWidth: true; model: backend.dates; currentIndex: Math.max(0, model.indexOf(backend.date)); onActivated: backend.setOption("date", currentText); Accessible.name: "Date filter" }
                    TextField { id: historySearch; objectName: "historySearch"; Layout.fillWidth: true; placeholderText: "Search filename, conditions, status"; text: backend.search; onTextEdited: backend.queueSearch(text); Accessible.name: "Search history"; selectByMouse: true }
                    RowLayout {
                        Layout.fillWidth: true
                        Label { text: fileList.count + " / " + backend.typeFileCount + " visible"; color: Theme.muted; Layout.fillWidth: true }
                        Button { objectName: "historyClearFilters"; text: "Clear filters"; enabled: backend.search.length > 0 || backend.date !== "All dates"; onClicked: backend.clearFilters() }
                    }
                    RowLayout {
                        Label { text: backend.selectedCount > 0 ? backend.selectedCount + " selected" : "Check files to compare"; font.bold: backend.selectedCount > 0; color: Theme.text; Layout.fillWidth: true; ToolTip.text: "Selections stay checked when you filter the list"; ToolTip.visible: selectionHover.hovered; HoverHandler { id: selectionHover } }
                        Button { text: "Clear selection"; enabled: backend.selectedCount > 0; onClicked: backend.clearSelection() }
                    }
                    ListView {
                        id: fileList; Layout.fillWidth: true; Layout.fillHeight: true
                        clip: true; spacing: 4; model: backend.files
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
                                CheckBox { checked: modelData.checked; onToggled: backend.setChecked(modelData.path, checked); Accessible.name: "Compare " + modelData.name }
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
                    Button { objectName: "historyRestoreView"; text: "Restore view…"; onClicked: window.chooseRestoreView(); ToolTip.text: "Open an exported analysis view (*_view.json)"; ToolTip.visible: hovered }
                    Button {
                        objectName: "historyExportView"; text: "Export PNG + CSV…"; enabled: backend.exportReady
                        highlighted: true
                        onClicked: backend.hasMap ? exportMenu.open() : window.saveAs("both")
                        ToolTip.text: "Save the displayed plot, CSV data and a restorable analysis view"; ToolTip.visible: hovered
                        Menu {
                            id: exportMenu
                            MenuItem { text: "Heatmap PNG + CSV…"; onTriggered: window.saveAs("heatmap") }
                            MenuItem { text: "Measured cut PNG + CSV…"; enabled: backend.cutReady; onTriggered: window.saveAs("cut") }
                            MenuItem { text: "Heatmap + cut PNG + CSV…"; enabled: backend.cutReady; onTriggered: window.saveAs("both") }
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
                    ComboBox {
                        width: 150; model: backend.xChoices
                        displayText: backend.axisLabel(backend.state.x)
                        currentIndex: Math.max(0, model.indexOf(backend.state.x)); onActivated: backend.setOption("x", currentText)
                        Accessible.name: "X coordinate"
                        delegate: ItemDelegate { required property var modelData; width: parent.width; text: backend.axisLabel(modelData) }
                    }
                    }
                    Row {
                        spacing: 5
                        Label { text: "Signal"; anchors.verticalCenter: parent.verticalCenter }
                        ComboBox { width: 200; model: backend.signalChoices; currentIndex: Math.max(0, model.indexOf(backend.state.signal)); onActivated: backend.setOption("signal", currentText); Accessible.name: "Signal"; ToolTip.text: currentText; ToolTip.visible: hovered }
                    }
                    Row {
                        spacing: 5
                        Label { text: "Direction"; anchors.verticalCenter: parent.verticalCenter }
                        ComboBox { width: 115; model: ["All", "forward", "backward", "unknown"]; currentIndex: Math.max(0, model.indexOf(backend.state.direction)); onActivated: backend.setOption("direction", currentText); Accessible.name: "Direction" }
                    }
                    Row {
                        spacing: 5
                        Label { text: backend.hasMap ? "Cut scale" : "Normalize"; anchors.verticalCenter: parent.verticalCenter }
                        ComboBox { width: 110; model: ["Raw", "Max |Y|", "0–1"]; currentIndex: ["raw", "max_abs", "min_max"].indexOf(backend.state.normalization); onActivated: backend.setOption("normalization", ["raw", "max_abs", "min_max"][currentIndex]); Accessible.name: "Normalization" }
                    }
                    Button { objectName: "historyRanges"; visible: backend.hasMap; text: "Axis / color ranges…"; onClicked: window.rangesOpen = true }
                }
                Label { text: backend.selectionSummary; color: Theme.muted; Layout.fillWidth: true; wrapMode: Text.Wrap; maximumLineCount: 2; elide: Text.ElideRight; ToolTip.text: text; ToolTip.visible: summaryHover.hovered; HoverHandler { id: summaryHover } }
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
                            ComboBox {
                                width: Math.min(260, comparison.width - 160); model: backend.mapSources; textRole: "label"; valueRole: "path"
                                currentIndex: { var sources = model; var selected = backend.state.primary_path; for (var i = 0; i < sources.length; ++i) if (sources[i].path === selected) return i; return 0 }
                                onActivated: backend.setOption("primary_path", currentValue); Accessible.name: "Map source file"
                                ToolTip.text: currentText; ToolTip.visible: hovered
                            }
                            }
                            Row {
                            spacing: 5
                            Label { text: "Y axis"; anchors.verticalCenter: parent.verticalCenter }
                            ComboBox { width: 130; model: ["None (1D)"].concat(backend.xChoices); currentIndex: Math.max(0, model.indexOf(backend.state.map_y)); onActivated: backend.setOption("map_y", currentText); Accessible.name: "Map Y coordinate" }
                            }
                            CheckBox { text: "Infer axes"; checked: backend.state.auto_axes; onToggled: backend.setOption("auto_axes", checked) }
                        }
                        Flow {
                            visible: backend.state.map_y !== "None (1D)"; Layout.fillWidth: true; Layout.preferredHeight: visible ? implicitHeight : 0; spacing: 5
                            Row {
                            spacing: 5
                            Label { text: "Measured cut"; font.bold: true; anchors.verticalCenter: parent.verticalCenter }
                            ComboBox { width: 120; model: backend.passes; currentIndex: Math.max(0, model.indexOf(backend.state.pass)); onActivated: backend.setOption("pass", currentText); Accessible.name: "Row or pass" }
                            }
                            Row {
                            spacing: 5
                            Label { text: "Fixed axis"; anchors.verticalCenter: parent.verticalCenter }
                            ComboBox { width: 95; model: [backend.state.x, backend.state.map_y]; currentIndex: Math.max(0, model.indexOf(backend.state.fixed_axis)); onActivated: backend.setOption("fixed_axis", currentText); Accessible.name: "Fixed cut axis" }
                            }
                            Row {
                            spacing: 5
                            Label { text: "Value"; anchors.verticalCenter: parent.verticalCenter }
                            ComboBox { width: 155; model: backend.measuredValues; editable: true; currentIndex: model.indexOf(backend.state.fixed_value); editText: backend.state.fixed_value === null ? "" : String(backend.state.fixed_value); onActivated: backend.setOption("fixed_value", currentText); onAccepted: backend.setOption("fixed_value", editText); Accessible.name: "Exact measured cut coordinate" }
                            }
                        }
                    }
                }
                TabBar {
                    id: plotTabs; objectName: "historyPlotTabs"; visible: backend.hasMap && window.narrowPlots
                    Layout.fillWidth: true
                    TabButton { objectName: "historyHeatmapTab"; text: "Heatmap · " + backend.state.signal }
                    TabButton { objectName: "historyCutTab"; text: "Measured cut · " + backend.state.fixed_axis + " = " + backend.state.fixed_value; ToolTip.text: backend.traceCount + " displayed traces"; ToolTip.visible: hovered }
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
                        Label { objectName: "historyCutTitle"; visible: !window.narrowPlots; text: "Measured cut — " + backend.state.fixed_axis + " = " + backend.state.fixed_value; font.bold: true; Layout.fillWidth: true; color: Theme.text; elide: Text.ElideRight; ToolTip.text: text; ToolTip.visible: cutTitleHover.hovered; HoverHandler { id: cutTitleHover } }
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
                            Button { Layout.alignment: Qt.AlignHCenter; visible: backend.emptyState.action.length > 0; text: backend.emptyState.action === "filters" ? "Clear filters" : "Browse folder…"; onClicked: backend.emptyState.action === "filters" ? backend.clearFilters() : window.chooseFolder() }
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
            Button { text: "Details (" + backend.statusDetails.split("\n").length + ")…"; visible: backend.statusDetails.length > 0; onClicked: detailsDialog.open() }
        }
    }
}
