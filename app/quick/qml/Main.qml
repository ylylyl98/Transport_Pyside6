import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import QtQuick.Dialogs

ApplicationWindow {
    id: window
    property var backend: appBackend
    function savedBool(key, fallback) {
        let value = backend.preference(key, fallback)
        return value === true || value === 1 || value === "true"
    }
    property bool showInstruments: savedBool("instrumentsOpenV2", false)
    property bool pinInstruments: savedBool("pinInstruments", false)
    property bool showParameters: savedBool("parameters", true)
    property bool showLog: false
    property bool showSweepPreview: false
    onShowSweepPreviewChanged: backend.measurement.setSweepPreviewVisible(showSweepPreview)
    function openSweepPreview() {
        measurementMode.focusCurrent()
        backend.measurement.requestSweepPreview()
        showSweepPreview = true
        if (narrow) narrowPage = 1
    }
    Connections {
        target: window.backend.measurement
        function onChanged() { if (window.backend.measurement.active) window.showSweepPreview = false }
    }
    onShowLogChanged: if (showLog && narrow) narrowPage = 1
    property int narrowPage: 0
    readonly property bool canDockInstruments: width >= (showParameters ? 1320 : 960)
    readonly property bool dockInstruments: pinInstruments && canDockInstruments
    readonly property bool narrow: width < 1050
    onNarrowChanged: if (narrow && showLog) narrowPage = 1
    property real parameterWidth: Math.max(360, Math.min(720, Number(backend.preference("parameterWidth", 520))))
    property real instrumentWidth: Math.max(360, Math.min(560, Number(backend.preference("instrumentWidth", 400))))
    property var scrollPositions: ({})
    property int scrollMode: 0
    function openInstrumentPage(section) {
        instruments.section = section
        showInstruments = true
    }
    function revealSetupItem(item) {
        Qt.callLater(() => {
            const flick = parameterScroll.contentItem
            const y = item.mapToItem(flick.contentItem, 0, 0).y
            flick.contentY = Math.max(0, Math.min(Math.max(0, flick.contentHeight - flick.height), y - 8))
        })
    }
    Component.onCompleted: scrollMode = backend.measurement.mode
    property real pendingScrollY: 0
    width: Math.min(Screen.desktopAvailableWidth, Math.max(800, Number(backend.preference("width", 1480))))
    height: Math.min(Screen.desktopAvailableHeight - 48, Math.max(600, Number(backend.preference("height", 900))))
    minimumWidth: 800
    minimumHeight: 600
    visible: true
    title: backend.offline ? "Transport — OFFLINE preview" : "Transport Measurement"
    font.family: "Segoe UI"
    font.pointSize: backend.fontSize
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
    palette.disabled.button: Theme.disabledSurface
    palette.disabled.buttonText: Theme.disabledText
    palette.disabled.text: Theme.disabledText
    palette.disabled.windowText: Theme.disabledText
    onClosing: close => {
        backend.savePreference("width", width)
        backend.savePreference("height", height)
        backend.savePreference("instrumentsOpenV2", showInstruments)
        backend.savePreference("pinInstruments", pinInstruments)
        backend.savePreference("parameters", showParameters)
        backend.savePreference("parameterWidth", parameterRegion.visible && !narrow ? parameterRegion.width : parameterWidth)
        backend.savePreference("instrumentWidth", instrumentDock.visible ? instrumentDock.width : instrumentWidth)
        if (!backend.closeAccepted) {
            close.accepted = false
            backend.requestClose()
        }
    }
    Binding { target: Theme; property: "dark"; value: window.backend.dark }
    Binding { target: Theme; property: "basePointSize"; value: window.backend.fontSize }
    Shortcut { sequence: "Ctrl+L"; onActivated: window.showLog = !window.showLog }
    Shortcut { sequence: "Ctrl+Alt+H"; onActivated: window.backend.history.open() }
    Shortcut { sequence: "Ctrl+Shift+R"; enabled: window.backend.offline; onActivated: { measurementMode.focusCurrent(); window.backend.rebuildPreview() } }
    header: ToolBar {
        background: Rectangle { color: Theme.surface; border.color: Theme.border }
        ColumnLayout {
            anchors.fill: parent
            spacing: 0
            RowLayout {
                Layout.fillWidth: true
                Layout.margins: 8
                spacing: 8
                ColumnLayout {
                    spacing: 2
                    ActionButton {
                        id: stopAll
                        tone: "danger"
                        objectName: "stopAll"
                        text: window.width < 1000 ? "STOP / ZERO VOLTAGES" : "STOP ALL / ZERO VOLTAGES"
                        font.bold: true
                        Accessible.name: "Stop all measurements and ramp voltages to zero"
                        Accessible.description: "Requests safe voltage ramps and magnet pause. Magnetic field is not zeroed."
                        ToolTip.visible: hovered
                        ToolTip.text: "Requests safe voltage ramps and magnet pause. Does not zero magnetic field."
                        onClicked: window.backend.emergencyStop()
                    }
                    Label {
                        objectName: "stopFieldNotice"
                        text: "Magnetic field is not zeroed"
                        color: Theme.muted
                        font.pointSize: Theme.captionPointSize
                    }
                }
                Label {
                    objectName: "measurementPhase"
                    text: window.backend.measurement.phase === "idle" ? "Idle" : window.backend.measurement.phase
                    color: Theme.progressTone(window.backend.measurement.phase) === "danger" ? Theme.danger
                        : Theme.progressTone(window.backend.measurement.phase) === "success" ? Theme.progressSuccess
                        : Theme.progressTone(window.backend.measurement.phase) === "warning" ? Theme.warning
                        : window.backend.measurement.active ? Theme.accent : Theme.muted
                    Layout.fillWidth: true; Layout.alignment: Qt.AlignTop
                    Layout.preferredHeight: stopAll.height
                    verticalAlignment: Text.AlignVCenter; elide: Text.ElideRight
                }
                ActionButton { objectName: "openInstruments"; Layout.alignment: Qt.AlignTop; text: "Instruments"; checkable: true; checked: window.showInstruments; onClicked: window.showInstruments = checked }
                ActionButton {
                    objectName: "openAnalysis"
                    Layout.alignment: Qt.AlignTop
                    text: window.backend.history.opening ? "Opening…" : window.backend.history.launchFailed ? "Retry Analysis" : "Analysis"
                    enabled: !window.backend.history.busy && !window.backend.closing
                    Accessible.name: "Open Analysis"
                    ToolTip.visible: hovered
                    ToolTip.text: "Open saved measurements in a separate read-only Analysis window (Ctrl+Alt+H)"
                    onClicked: window.backend.history.open()
                }
                ActionToolButton {
                    objectName: "openViewMenu"
                    Layout.alignment: Qt.AlignTop
                    text: "View"
                    onClicked: viewMenu.open()
                    Menu {
                        id: viewMenu
                        MenuItem { text: "Dark theme"; checkable: true; checked: window.backend.dark; onTriggered: window.backend.dark = checked }
                        MenuItem { text: "Show parameters"; checkable: true; checked: window.showParameters; onTriggered: { window.showParameters = checked; window.narrowPage = 0 } }
                        MenuItem { text: "Pin instruments beside the plot"; checkable: true; checked: window.pinInstruments; onTriggered: window.pinInstruments = checked }
                        MenuItem { text: "Show log (Ctrl+L)"; checkable: true; checked: window.showLog; onTriggered: window.showLog = checked }
                        MenuItem { text: "Save channel PNGs after measurements"; checkable: true; checked: window.backend.autoPng; onTriggered: window.backend.autoPng = checked }
                        MenuSeparator {}
                        MenuItem { text: "Reload analysis"; onTriggered: window.backend.history.reload() }
                        MenuItem { text: "Runtime settings"; onTriggered: window.openInstrumentPage(7) }
                        MenuSeparator {}
                        MenuItem {
                            objectName: "checkForUpdates"
                            text: window.backend.updates.checking ? "Checking for updates…" : "Check for updates"
                            enabled: !window.backend.updates.checking && !window.backend.closing
                            onTriggered: window.backend.updates.checkNow()
                        }
                        MenuItem {
                            objectName: "automaticUpdates"
                            text: "Check for updates automatically"
                            checkable: true
                            checked: window.backend.updates.autoCheck
                            onTriggered: window.backend.updates.setAutoCheck(checked)
                        }
                        MenuItem {
                            text: window.backend.updates.message
                            enabled: false
                            visible: window.backend.updates.message.length > 0
                        }
                        MenuItem { text: "Rebuild offline preview (Ctrl+Shift+R)"; visible: window.backend.offline; onTriggered: window.backend.rebuildPreview() }
                    }
                }
            }
            Label {
                objectName: "analysisLaunchStatus"
                visible: window.backend.history.opening || window.backend.history.launchFailed
                Layout.fillWidth: true
                Layout.leftMargin: 8; Layout.rightMargin: 8
                text: window.backend.history.message
                color: window.backend.history.launchFailed ? Theme.danger : Theme.muted
                elide: Text.ElideRight
                ToolTip.visible: analysisStatusHover.hovered
                ToolTip.text: text
                HoverHandler { id: analysisStatusHover }
                Accessible.name: text
            }
            RowLayout {
                Layout.fillWidth: true
                Layout.leftMargin: 8; Layout.rightMargin: 8; Layout.bottomMargin: 6
                spacing: 8
                ActionToolButton {
                    objectName: "openConnections"
                    text: "Connections"
                    ToolTip.visible: hovered
                    ToolTip.text: "Connect instruments and check source protection"
                    onClicked: window.openInstrumentPage(1)
                }
                ActionToolButton {
                    objectName: "openSampleFiles"
                    text: "Sample && files"
                    Accessible.name: "Sample and files"
                    ToolTip.visible: hovered
                    ToolTip.text: "Set the operator, sample and measurement data folder"
                    onClicked: window.openInstrumentPage(5)
                }
                ColumnLayout {
                    Layout.fillWidth: true
                    Layout.minimumWidth: 0
                    spacing: 2
                    Label {
                        objectName: "dataDestination"
                        text: "Save to: " + window.backend.dataFolderPath
                        color: Theme.text; elide: Text.ElideMiddle
                        Layout.fillWidth: true; Layout.minimumWidth: 0
                        Accessible.name: text
                        ToolTip.visible: folderHover.hovered
                        ToolTip.text: text
                        HoverHandler { id: folderHover }
                    }
                    Label {
                        text: window.backend.magnet.summary + "  ·  " + window.backend.instruments.devices.filter(d => d.connected).length + " instruments connected"
                        color: Theme.muted; elide: Text.ElideRight
                        Layout.fillWidth: true; Layout.minimumWidth: 0
                        ToolTip.visible: statusHover.hovered
                        ToolTip.text: window.backend.magnet.summary + "\n" + window.backend.instruments.devices.map(d => d.label + ": " + d.state).join(" · ")
                        HoverHandler { id: statusHover }
                    }
                }
            }
            RowLayout {
                Layout.fillWidth: true
                Layout.leftMargin: 8; Layout.rightMargin: 8; Layout.bottomMargin: 6
                spacing: 4
                readonly property bool overflowing: measurementMode.naturalWidth > width + 1
                ActionToolButton {
                    objectName: "scrollMeasurementTabsLeft"
                    text: "Previous"
                    visible: parent.overflowing
                    enabled: measurementMode.enabled && measurementTabsView.contentX > 0.5
                    Accessible.name: "Scroll measurement tabs left"
                    onClicked: measurementTabsView.contentX = Math.max(0, measurementTabsView.contentX - measurementTabsView.width * 0.7)
                }
                TabBar {
                    id: measurementMode
                    objectName: "measurementMode"
                    Layout.fillWidth: true
                    Layout.minimumWidth: 0
                    Layout.preferredWidth: 0
                    clip: true
                    enabled: !window.backend.measurement.active && !window.backend.closing
                    Accessible.name: "Measurement mode"
                    background: Rectangle { color: Theme.surface }
                    property bool navigationReady: false
                    readonly property real naturalWidth: {
                        let total = 0
                        for (let i = 0; i < count; ++i) total += itemAt(i).implicitWidth
                        return total + Math.max(0, count - 1) * measurementTabsView.spacing
                    }
                    function revealCurrent() {
                        measurementTabsView.positionViewAtIndex(currentIndex, ListView.Contain)
                    }
                    function focusCurrent() {
                        const tab = itemAt(window.backend.measurement.mode)
                        if (tab) tab.forceActiveFocus()
                        else forceActiveFocus()
                    }
                    Component.onCompleted: {
                        setCurrentIndex(window.backend.measurement.mode)
                        navigationReady = true
                        Qt.callLater(revealCurrent)
                    }
                    onCurrentIndexChanged: {
                        if (!navigationReady) return
                        if (enabled && currentIndex !== window.backend.measurement.mode)
                            window.backend.measurement.mode = currentIndex
                        // A rejected request must never leave a different tab selected.
                        if (currentIndex !== window.backend.measurement.mode)
                            setCurrentIndex(window.backend.measurement.mode)
                        Qt.callLater(revealCurrent)
                    }
                    Connections {
                        target: window.backend.measurement
                        function onModeChanged() {
                            measurementMode.setCurrentIndex(window.backend.measurement.mode)
                            Qt.callLater(measurementMode.revealCurrent)
                        }
                    }
                    contentItem: ListView {
                        id: measurementTabsView
                        objectName: "measurementTabsView"
                        model: measurementMode.contentModel
                        currentIndex: measurementMode.currentIndex
                        orientation: ListView.Horizontal
                        boundsBehavior: Flickable.StopAtBounds
                        spacing: 4
                        clip: true
                        onWidthChanged: Qt.callLater(measurementMode.revealCurrent)
                        onContentWidthChanged: Qt.callLater(measurementMode.revealCurrent)
                    }
                    Repeater {
                        model: window.backend.measurement.modes
                        NavigationTabButton {
                            required property int index
                            required property var modelData
                            objectName: "measurementTab" + index
                            width: implicitWidth
                            text: modelData.title
                        }
                    }
                }
                ActionToolButton {
                    objectName: "scrollMeasurementTabsRight"
                    text: "Next"
                    visible: parent.overflowing
                    enabled: measurementMode.enabled && measurementTabsView.contentX + measurementTabsView.width < measurementTabsView.contentWidth - 0.5
                    Accessible.name: "Scroll measurement tabs right"
                    onClicked: measurementTabsView.contentX = Math.min(measurementTabsView.contentWidth - measurementTabsView.width, measurementTabsView.contentX + measurementTabsView.width * 0.7)
                }
            }
        }
    }
    Drawer {
        id: instrumentDrawer
        objectName: "instrumentDrawer"
        width: Math.min(500, window.width - 60)
        y: window.header.height
        height: window.height - window.header.height - measurementControls.height
        edge: Qt.RightEdge
        visible: window.showInstruments && !window.dockInstruments
        modal: false
        dim: false
        closePolicy: Popup.CloseOnEscape
        onClosed: if (!window.dockInstruments) window.showInstruments = false
        background: Rectangle { color: Theme.background; border.color: Theme.border }
    }
    ColumnLayout {
        anchors.fill: parent
        anchors.margins: 8
        spacing: 6
        Rectangle {
            objectName: "updateBanner"
            visible: window.backend.updates.available && !window.backend.closing
            Layout.fillWidth: true
            implicitHeight: updateRow.implicitHeight + 16
            color: Theme.surface
            border.color: Theme.accent
            radius: 4
            RowLayout {
                id: updateRow
                anchors.fill: parent
                anchors.margins: 8
                spacing: 8
                Label {
                    text: "Transport " + window.backend.updates.latestVersion + " is available. Update after measurements finish."
                    color: Theme.text
                    wrapMode: Text.Wrap
                    Layout.fillWidth: true
                    Accessible.name: text
                }
                ActionButton {
                    objectName: "downloadUpdate"
                    text: "Download update"
                    onClicked: window.backend.updates.openRelease()
                }
                ActionToolButton {
                    objectName: "dismissUpdate"
                    text: "Dismiss"
                    onClicked: window.backend.updates.ignoreLatest()
                }
            }
        }
        RowLayout {
            visible: window.narrow && window.showParameters
            Layout.fillWidth: true
            ActionButton { text: "Parameters"; checkable: true; checked: window.narrowPage === 0; onClicked: window.narrowPage = 0 }
            ActionButton { text: "Live plot"; checkable: true; checked: window.narrowPage === 1; onClicked: window.narrowPage = 1 }
            Item { Layout.fillWidth: true }
        }
        SplitView {
            id: workspace
            Layout.fillWidth: true
            Layout.fillHeight: true
            orientation: Qt.Horizontal
            handle: Rectangle {
                implicitWidth: 8
                color: SplitHandle.pressed ? Theme.selection : Theme.background
                Rectangle {
                    anchors.centerIn: parent
                    width: 2; height: Math.min(40, parent.height)
                    color: parent.SplitHandle.hovered || parent.SplitHandle.pressed ? Theme.accent : Theme.border
                }
            }
            Pane {
                id: parameterRegion
                enabled: !window.backend.closing
                objectName: "measurementSetupRegion"
                visible: window.showParameters && (!window.narrow || window.narrowPage === 0)
                SplitView.fillWidth: window.narrow
                SplitView.minimumWidth: window.narrow ? 0 : (window.backend.measurement.mode >= 2 ? 420 : 360)
                SplitView.preferredWidth: window.parameterWidth
                SplitView.maximumWidth: window.narrow ? 10000 : 720
                padding: 8
                background: Rectangle { color: Theme.background; border.color: Theme.border; radius: 4 }
                ColumnLayout {
                    anchors.fill: parent
                    spacing: 6
                    RowLayout {
                        id: parameterHeader
                        Layout.fillWidth: true
                        Label { text: "Measurement setup"; font.bold: true; font.pointSize: Theme.titlePointSize; color: Theme.text; Layout.fillWidth: true }
                        ActionToolButton {
                            objectName: "hideParameters"
                            text: "Hide"
                            Accessible.name: "Hide measurement setup"
                            ToolTip.visible: hovered; ToolTip.text: "Give more space to the live plot"
                            onClicked: window.showParameters = false
                        }
                    }
                    Rectangle { Layout.fillWidth: true; implicitHeight: 1; color: Theme.border }
                    ScrollView {
                        id: parameterScroll
                        objectName: "parameterScroll"
                        Layout.fillWidth: true; Layout.fillHeight: true
                        clip: true
                        contentWidth: availableWidth
                        ScrollBar.horizontal.policy: ScrollBar.AlwaysOff
                        MeasurementPane {
                            id: measurementPane
                            objectName: "measurementPane"
                            width: parameterScroll.availableWidth - 14
                            measurement: window.backend.measurement
                            onRevealRequested: item => window.revealSetupItem(item)
                            onSweepPreviewRequested: window.openSweepPreview()
                        }
                    }
                }
            }
            ColumnLayout {
                id: plotColumn
                objectName: "liveDataRegion"
                visible: !window.narrow || !window.showParameters || window.narrowPage === 1
                SplitView.fillWidth: true
                SplitView.minimumWidth: window.narrow ? 0 : 400
                spacing: 6
                RowLayout {
                    enabled: !window.backend.closing
                    Layout.fillWidth: true
                    Layout.leftMargin: 8; Layout.rightMargin: 8
                    Layout.minimumHeight: parameterHeader.implicitHeight + 16
                    NavigationTabButton {
                        objectName: "liveDataTab"
                        text: "Live data"
                        checked: !window.showSweepPreview
                        onClicked: window.showSweepPreview = false
                    }
                    NavigationTabButton {
                        objectName: "sweepPreviewTab"
                        text: "Sweep preview"
                        checked: window.showSweepPreview
                        enabled: !window.backend.measurement.active && !window.backend.closing
                        onClicked: window.openSweepPreview()
                    }
                    Item { Layout.fillWidth: true }
                    ActionToolButton {
                        objectName: "refreshSweepPreview"
                        text: "Refresh"
                        visible: window.showSweepPreview
                        enabled: !window.backend.measurement.active && !window.backend.measurement.sweepPreview.busy && !window.backend.closing
                        onClicked: window.openSweepPreview()
                    }
                    ActionToolButton {
                        objectName: "showParameters"
                        visible: !window.showParameters
                        text: "Show setup"
                        onClicked: { window.showParameters = true; window.narrowPage = 0 }
                    }
                }
                Label {
                    objectName: "sweepPreviewStatus"
                    visible: window.showSweepPreview
                    Layout.fillWidth: true
                    Layout.leftMargin: 8; Layout.rightMargin: 8
                    wrapMode: Text.Wrap
                    text: {
                        const preview = window.backend.measurement.sweepPreview
                        if (preview.busy) return "Building sweep preview… No instrument outputs are applied."
                        if (preview.error) return "Preview unavailable: " + preview.error
                        if (preview.stale) return "Updating preview for the current page and settings…"
                        return preview.summary + (window.backend.measurement.mode === 4 ? " Selected condition only; repeated at each B-field target." : "")
                    }
                    color: window.backend.measurement.sweepPreview.error ? Theme.danger : window.backend.measurement.sweepPreview.stale ? Theme.warning : Theme.muted
                    font.pointSize: Theme.captionPointSize
                }
                PlotPane {
                    objectName: "livePlotPane"
                    enabled: !window.backend.closing
                    Layout.fillWidth: true
                    Layout.fillHeight: true
                    plotModel: window.showSweepPreview ? window.backend.measurement.sweepPreview.plotModel : window.backend.plotModel
                    showAxisSelector: !window.showSweepPreview
                    showChannelSelector: !window.showSweepPreview
                    showLayoutSelector: !window.showSweepPreview
                    showRangeSelector: !window.showSweepPreview
                    showExport: !window.showSweepPreview
                    footerText: window.showSweepPreview ? "Sweep preview · scroll: zoom · drag: pan/rectangle · double-click: reset view" : ""
                    darkMode: window.backend.dark
                    emptyText: window.showSweepPreview ? "Sweep range preview\nThe preview follows the current page and committed settings." : window.backend.offline ? "Offline preview\nAcquisition is disabled. Open Analysis to explore saved data." : "No data yet\nSet connections, sample and parameters, then start measurement."
                    onSaveRequested: {
                        plotFileDialog.currentFolder = window.backend.exportFolderUrl
                        plotFileDialog.selectedFile = window.backend.suggestedPlotUrl
                        plotFileDialog.open()
                    }
                }
                Pane {
                    objectName: "measurementLogRegion"
                    visible: window.showLog
                    Layout.fillWidth: true
                    Layout.preferredHeight: Math.min(180, workspace.height * 0.32)
                    padding: 8
                    background: Rectangle { color: Theme.surface; border.color: Theme.border; radius: 4 }
                    ColumnLayout {
                        anchors.fill: parent
                        spacing: 4
                        RowLayout {
                            Layout.fillWidth: true
                            Label { text: "Measurement log"; font.bold: true; color: Theme.text; Layout.fillWidth: true }
                            ActionToolButton { objectName: "closeMeasurementLog"; text: "Close"; Accessible.name: "Close measurement log"; onClicked: window.showLog = false }
                        }
                        ScrollView {
                            Layout.fillWidth: true; Layout.fillHeight: true
                            clip: true; contentWidth: availableWidth
                            ScrollBar.horizontal.policy: ScrollBar.AlwaysOff
                            TextArea {
                                text: window.backend.measurement.logText
                                readOnly: true
                                selectByMouse: true
                                Accessible.name: "Measurement log"
                                font.family: "Consolas"
                                wrapMode: TextEdit.Wrap
                                color: Theme.text
                                background: null
                            }
                        }
                    }
                }
                Item {
                    id: plotControlsHost
                    objectName: "plotControlsHost"
                    visible: !window.narrow
                    Layout.fillWidth: true
                    Layout.preferredHeight: visible ? measurementControls.implicitHeight : 0
                    Layout.minimumHeight: visible ? measurementControls.implicitHeight : 0
                }
            }
            Item {
                id: instrumentDock
                enabled: !window.backend.closing
                objectName: "instrumentDock"
                visible: window.showInstruments && window.dockInstruments
                SplitView.minimumWidth: 360
                SplitView.preferredWidth: window.instrumentWidth
                SplitView.maximumWidth: 560
                Pane {
                    id: instrumentHost
                    objectName: "instrumentRegion"
                    parent: window.dockInstruments ? instrumentDock : instrumentDrawer.contentItem
                    anchors.fill: parent
                    padding: 8
                    background: Rectangle { color: Theme.background; border.color: Theme.border; radius: 4 }
                    ColumnLayout {
                        anchors.fill: parent
                        spacing: 6
                        RowLayout {
                            Layout.fillWidth: true
                            Label { text: "Instruments"; font.bold: true; color: Theme.text; Layout.fillWidth: true }
                            ActionToolButton {
                                objectName: "pinInstrumentPanel"
                                text: window.pinInstruments ? "Pinned" : "Pin"
                                checkable: true; checked: window.pinInstruments
                                Accessible.name: "Pin instruments beside the plot"
                                ToolTip.visible: hovered
                                ToolTip.text: window.canDockInstruments ? "Keep instruments beside the live plot" : "Dock automatically when the window has enough room"
                                onClicked: window.pinInstruments = checked
                            }
                            ActionToolButton {
                                objectName: "closeInstrumentPanel"
                                text: "Close"
                                Accessible.name: "Close instrument panel"
                                onClicked: window.showInstruments = false
                            }
                        }
                        Rectangle { Layout.fillWidth: true; implicitHeight: 1; color: Theme.border }
                        InstrumentPane {
                            id: instruments
                            Layout.fillWidth: true; Layout.fillHeight: true
                            appBackend: window.backend
                        }
                    }
                }
            }
        }
        Item {
            id: narrowControlsHost
            objectName: "narrowControlsHost"
            visible: window.narrow
            Layout.fillWidth: true
            Layout.preferredHeight: visible ? measurementControls.implicitHeight : 0
            Layout.minimumHeight: visible ? measurementControls.implicitHeight : 0
        }
    }
    Pane {
        id: measurementControls
        objectName: "measurementControlsRegion"
        parent: window.narrow ? narrowControlsHost : plotControlsHost
        anchors.fill: parent
        implicitHeight: measurementControlsColumn.implicitHeight + topPadding + bottomPadding
        padding: 8
        background: Rectangle { color: Theme.surface; border.color: Theme.border }
        ColumnLayout {
            id: measurementControlsColumn
            anchors.fill: parent
            spacing: 5
            Label {
                text: window.backend.measurement.message === "Ready" && window.backend.measurement.phase === "idle" ? "Idle" : window.backend.measurement.message
                color: window.backend.measurement.phase === "error" ? Theme.danger : Theme.text
                wrapMode: Text.Wrap
                maximumLineCount: 2
                elide: Text.ElideRight
                Layout.fillWidth: true
                ToolTip.visible: runHover.hovered
                ToolTip.text: text
                HoverHandler { id: runHover }
            }
            Flow {
                Layout.fillWidth: true
                Layout.preferredHeight: implicitHeight
                spacing: 4
                ActionButton { objectName: "startMeasurement"; text: "Start measurement"; highlighted: true; enabled: window.backend.measurement.canStart && !window.backend.offline && !window.backend.closing; onClicked: { measurementMode.focusCurrent(); window.backend.measurement.start() } }
                ActionButton {
                    objectName: "locateMeasurementError"
                    text: "Locate error"
                    visible: window.backend.measurement.validationIssues.length > 0
                    Accessible.description: "Expand the first invalid section and focus the field that needs correction"
                    onClicked: {
                        window.showParameters = true
                        window.narrowPage = 0
                        measurementPane.revealIssue(window.backend.measurement.validationIssues[0])
                    }
                }
                ActionButton { objectName: "stopMeasurement"; text: "Stop measurement"; tone: "danger"; enabled: window.backend.measurement.canStop; onClicked: window.backend.measurement.stop() }
                ActionButton { text: "Continue after check"; visible: window.backend.measurement.continuationRequired; enabled: window.backend.measurement.canContinue; ToolTip.visible: hovered; ToolTip.text: window.backend.measurement.continuationReason; onClicked: window.backend.measurement.continueAfterTemperatureCheck() }
                ActionToolButton { objectName: "showStatusDetails"; text: "Status details"; ToolTip.visible: hovered; ToolTip.text: "Read or copy the full measurement and application messages"; onClicked: statusDetails.open() }
                ActionButton { text: "Log"; checkable: true; checked: window.showLog; ToolTip.visible: hovered; ToolTip.text: "Show the measurement log (Ctrl+L)"; onClicked: window.showLog = checked }
            }
            MeasurementProgress { objectName: "measurementProgress"; Layout.fillWidth: true; Layout.minimumWidth: 40; from: 0; to: 1; tone: Theme.progressTone(window.backend.measurement.phase); value: window.backend.measurement.progress; Accessible.name: "Measurement progress" }
            Label {
                text: window.backend.message
                color: window.backend.offline ? Theme.warning : Theme.muted
                wrapMode: Text.Wrap
                maximumLineCount: 1
                elide: Text.ElideRight
                Layout.fillWidth: true
                ToolTip.visible: messageHover.hovered
                ToolTip.text: text
                HoverHandler { id: messageHover }
            }
        }
    }
    Popup {
        id: statusDetails
        objectName: "statusDetailsPopup"
        parent: Overlay.overlay
        anchors.centerIn: parent
        width: Math.min(680, window.width - 40)
        height: Math.min(380, window.height - window.header.height - measurementControls.height - 24)
        modal: false
        focus: true
        padding: 12
        closePolicy: Popup.CloseOnEscape | Popup.CloseOnPressOutside
        background: Rectangle { color: Theme.surface; border.color: Theme.border; radius: 4 }
        contentItem: ColumnLayout {
            RowLayout {
                Layout.fillWidth: true
                Label { text: "Status details"; font.bold: true; color: Theme.text; Layout.fillWidth: true }
                ActionToolButton { text: "Close"; onClicked: statusDetails.close() }
            }
            ScrollView {
                Layout.fillWidth: true; Layout.fillHeight: true
                clip: true; contentWidth: availableWidth
                ScrollBar.horizontal.policy: ScrollBar.AlwaysOff
                TextArea {
                    objectName: "statusDetailsText"
                    text: "Measurement\n" + window.backend.measurement.message + "\n\nApplication\n" + window.backend.message
                    readOnly: true; selectByMouse: true; wrapMode: TextEdit.Wrap
                    color: Theme.text; background: null
                }
            }
        }
    }
    FolderDialog {
        id: folderDialog
        title: "Choose measurement data folder"
        onAccepted: window.backend.setDataFolder(selectedFolder)
    }
    FileDialog {
        id: plotFileDialog
        title: "Export plot — PNG and full CSV"
        fileMode: FileDialog.SaveFile
        nameFilters: ["PNG plot (*.png)"]
        defaultSuffix: "png"
        onAccepted: window.backend.exportPlot(selectedFile)
    }
    ActionDialog {
        id: confirmation
        objectName: "confirmationDialog"
        property string token: ""
        property string body: ""
        anchors.centerIn: parent
        width: Math.min(window.width - 48, 680)
        height: Math.min(window.height - 48, confirmationText.implicitHeight + 130)
        modal: true
        closePolicy: Popup.NoAutoClose
        standardButtons: Dialog.Yes | Dialog.No
        contentItem: ScrollView {
            objectName: "confirmationScroll"
            clip: true
            contentWidth: availableWidth
            ScrollBar.horizontal.policy: ScrollBar.AlwaysOff
            TextArea {
                id: confirmationText
                text: confirmation.body
                readOnly: true
                selectByMouse: true
                wrapMode: Text.Wrap
                color: Theme.text
                background: null
            }
        }
        onAccepted: window.backend.answerConfirmation(token, true)
        onRejected: window.backend.answerConfirmation(token, false)
    }
    Connections {
        target: window.backend.measurement
        function onModeChanged() {
            window.scrollPositions[window.scrollMode] = parameterScroll.contentItem.contentY
            window.scrollMode = window.backend.measurement.mode
            window.pendingScrollY = window.scrollPositions[window.scrollMode] || 0
            restoreParameterScroll.restart()
        }
    }
    Timer {
        id: restoreParameterScroll
        interval: 50
        onTriggered: parameterScroll.contentItem.contentY = Math.min(window.pendingScrollY, Math.max(0, parameterScroll.contentItem.contentHeight - parameterScroll.availableHeight))
    }
    Connections {
        target: window.backend
        function onChooseFolderRequested() { folderDialog.currentFolder = window.backend.dataFolderUrl; folderDialog.open() }
        function onConfirmationRequested(token, title, body) {
            confirmation.token = token; confirmation.title = title; confirmation.body = body
            confirmation.open()
            confirmation.standardButton(Dialog.No).forceActiveFocus()
        }
    }
}
