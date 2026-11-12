import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import Transport.Plot 1.0

Pane {
    id: root
    property var plotModel: null
    property bool darkMode: false
    property bool showAxisSelector: true
    property bool showChannelSelector: true
    property bool showLayoutSelector: true
    property bool showExport: true
    property bool showRangeSelector: true
    property string pointNoun: "samples"
    property string footerText: ""
    property bool exportEnabled: plotModel ? plotModel.pointCount > 0 : false
    property bool showEmptyState: true
    property string emptyText: "No data yet\nAcquired points will appear here."
    readonly property bool compact: width < 760
    readonly property bool small: width < 460
    readonly property bool directAxes: showLayoutSelector && plotModel && !plotModel.isHeatmap
    signal saveRequested()
    padding: 0
    background: Rectangle { color: Theme.surface; radius: 4; border.color: Theme.border }
    ColumnLayout {
        anchors.fill: parent
        spacing: 0
        Flow {
            id: plotToolbar
            objectName: "plotToolbar"
            Layout.fillWidth: true
            Layout.margins: 5
            Layout.preferredHeight: implicitHeight
            spacing: 6
            Row {
            id: navigationGroup
            objectName: "plotNavigationControls"
            spacing: 2
            ActionToolButton {
                text: root.small ? qsTr("Reset") : qsTr("Reset view")
                Accessible.name: "Reset plot view"
                onClicked: if (root.plotModel) root.plotModel.home()
                ToolTip.visible: hovered
                ToolTip.text: qsTr("Restore the selected X range policy and automatic Y scaling")
            }
            ActionToolButton {
                text: root.small ? (root.plotModel && root.plotModel.interactionMode === "zoom" ? "Zoom" : "Pan") : "Pan"
                checkable: true
                checked: root.plotModel ? root.plotModel.interactionMode === "pan" : true
                onClicked: if (root.plotModel) root.plotModel.interactionMode = root.small && root.plotModel.interactionMode === "pan" ? "zoom" : "pan"
                ToolTip.visible: hovered
                ToolTip.text: root.small ? "Toggle pan / rectangle zoom" : "Drag to pan"
            }
            ActionToolButton {
                visible: !root.small
                text: qsTr("Zoom")
                checkable: true
                checked: root.plotModel ? root.plotModel.interactionMode === "zoom" : false
                onClicked: if (root.plotModel) root.plotModel.interactionMode = "zoom"
                ToolTip.visible: hovered
                ToolTip.text: qsTr("Drag a rectangle. Scroll to zoom at the cursor; middle-drag to pan.")
            }
            SelectBox {
                visible: !root.compact && root.showRangeSelector
                width: 136
                model: ["Full sweep", "Acquired data"]
                currentIndex: root.plotModel && root.plotModel.xRangeMode === "Acquired data" ? 1 : 0
                wheelEnabled: false
                Accessible.name: "Plot X range"
                onActivated: if (root.plotModel) root.plotModel.xRangeMode = currentText
            }
            SelectBox {
                visible: !root.directAxes && !root.compact && root.showAxisSelector && !(root.plotModel && root.plotModel.isHeatmap)
                width: 126
                model: ["Follow Sweep", "Step Index", "Vtg", "Vbg", "Vds", "Doping", "E-field"]
                currentIndex: root.plotModel ? Math.max(0, model.indexOf(root.plotModel.xAxis)) : 0
                wheelEnabled: false
                Accessible.name: "Plot X axis"
                onActivated: if (root.plotModel) root.plotModel.xAxis = currentText
            }
            SelectBox {
                visible: !root.directAxes && !root.small && root.showChannelSelector && root.plotModel && !root.plotModel.isHeatmap && root.plotModel.layout === "single"
                width: 132
                model: root.plotModel ? root.plotModel.channelOptions : []
                currentIndex: root.plotModel ? Math.max(0, root.plotModel.channelOptions.indexOf(root.plotModel.channel)) : 0
                wheelEnabled: false
                Accessible.name: "Plot current channel"
                onActivated: if (root.plotModel) root.plotModel.channel = currentText
            }
            }
            Row {
                id: plotCountGroup
                objectName: "plotLayoutControls"
                visible: root.directAxes
                spacing: 2
                Repeater {
                    model: [{count: 1, label: "1 plot"}, {count: 2, label: "2 plots"}, {count: 4, label: "4 plots"}]
                    delegate: ActionToolButton {
                        required property var modelData
                        objectName: "plotCount" + modelData.count
                        text: modelData.label
                        checkable: true
                        checked: root.plotModel ? root.plotModel.plotCount === modelData.count : false
                        enabled: modelData.count !== 2 || (root.plotModel && root.plotModel.channelOptions.length >= 2)
                        onClicked: if (root.plotModel) root.plotModel.setPlotCount(modelData.count)
                        Accessible.name: "Plot layout: " + modelData.label
                    }
                }
            }
            Row {
            id: plotActionsGroup
            objectName: "plotExtraControls"
            spacing: 2
            ActionToolButton {
                text: root.compact ? "Options" : "Plot options"
                Accessible.name: "Plot axes, range, layout and legend"
                ToolTip.visible: hovered
                ToolTip.text: "Plot settings and full legend"
                onClicked: plotSettings.open()
            }
            ActionToolButton {
                visible: root.showExport
                text: qsTr("Export")
                enabled: root.exportEnabled
                ToolTip.visible: hovered
                ToolTip.text: "Save PNG and full-resolution CSV"
                onClicked: root.saveRequested()
            }
            }
            Label {
                id: plotHint
                objectName: "plotToolbarStatus"
                visible: root.directAxes
                width: Math.max(Math.min(150, plotToolbar.width), plotToolbar.width - navigationGroup.width - plotCountGroup.width - plotActionsGroup.width - 3 * plotToolbar.spacing)
                height: Math.max(24, plotCountGroup.height)
                text: root.plotModel && root.plotModel.layout === "overlay" ? root.plotModel.overlayStatus
                    : root.plotModel && root.plotModel.layout === "two" ? "Shared X · independent Y" : "Click an axis title to edit"
                color: Theme.muted
                font.pointSize: Theme.captionPointSize
                verticalAlignment: Text.AlignVCenter
                elide: Text.ElideRight
                ToolTip.text: text + " · Click an axis title to edit"
                ToolTip.visible: plotHintHover.hovered
                HoverHandler { id: plotHintHover }
            }
        }
        Item {
            Layout.fillWidth: true
            Layout.fillHeight: true
            Layout.minimumHeight: 80
            clip: true
            PlotItem {
                id: plot
                objectName: "nativePlot"
                anchors.fill: parent
                model: root.plotModel
                darkMode: root.darkMode
                editableXAxis: root.showAxisSelector && !(root.plotModel && root.plotModel.isHeatmap)
                editableYAxis: root.showChannelSelector && !(root.plotModel && root.plotModel.isHeatmap)
            }
            Repeater {
                model: plot.axisTitles
                delegate: ActionToolButton {
                    id: axisTitle
                    required property var modelData
                    objectName: "axisTitle_" + modelData.role + "_" + modelData.panel
                    text: modelData.text
                    readonly property color titleColor: modelData.color === "first" ? (root.darkMode ? "#8fbaff" : "#2978d5")
                        : modelData.color === "second" ? (root.darkMode ? "#ffbd78" : "#cf650b")
                        : hovered ? Theme.accent : Theme.muted
                    width: Math.max(30, Math.min(implicitWidth, modelData.extent))
                    height: 24
                    x: modelData.cx - width / 2
                    y: modelData.cy - height / 2
                    rotation: modelData.rotation
                    subtle: true
                    font.pointSize: Theme.captionPointSize
                    contentItem: Item {
                        implicitWidth: titleText.implicitWidth + 13
                        implicitHeight: titleText.implicitHeight
                        Text {
                            id: titleText
                            anchors.fill: parent; anchors.rightMargin: 13
                            text: axisTitle.text; font: axisTitle.font; color: axisTitle.titleColor
                            elide: Text.ElideRight
                            horizontalAlignment: Text.AlignHCenter; verticalAlignment: Text.AlignVCenter
                        }
                        Canvas {
                            anchors.right: parent.right; anchors.verticalCenter: parent.verticalCenter
                            width: 8; height: 4
                            property color ink: axisTitle.titleColor
                            onInkChanged: requestPaint()
                            onPaint: {
                                const context = getContext("2d")
                                context.clearRect(0, 0, width, height)
                                context.fillStyle = ink
                                context.beginPath(); context.moveTo(0, 0); context.lineTo(width, 0); context.lineTo(width / 2, height); context.closePath(); context.fill()
                            }
                        }
                    }
                    onClicked: root.editAxis(modelData.role, modelData.channel, axisTitle)
                    Keys.onReturnPressed: root.editAxis(modelData.role, modelData.channel, axisTitle)
                    Keys.onEnterPressed: root.editAxis(modelData.role, modelData.channel, axisTitle)
                    Accessible.name: "Configure " + (modelData.role === "x" ? "X" : "Y") + " axis: " + modelData.text
                    ToolTip.text: modelData.text + " · click to choose axis settings"
                    ToolTip.visible: hovered && !axisSettings.visible
                }
            }
            Label {
                objectName: "plotEmptyText"
                visible: root.showEmptyState && (!root.plotModel || root.plotModel.pointCount === 0)
                anchors.centerIn: parent
                width: Math.max(100, parent.width - 140)
                padding: 16
                text: root.emptyText
                horizontalAlignment: Text.AlignHCenter
                wrapMode: Text.Wrap
                color: Theme.muted
                background: Rectangle { color: Theme.surface; radius: 4 }
            }
        }
        Label {
            Layout.leftMargin: 12
            Layout.bottomMargin: 4
            text: root.footerText || ((root.plotModel ? root.plotModel.pointCount.toLocaleString() : "0") + " " + root.pointNoun + (root.small ? " · double-click: reset" : " · scroll: zoom · drag: pan/rectangle · double-click: reset view"))
            color: Theme.muted
            font.pointSize: Theme.captionPointSize
            elide: Text.ElideRight
            Layout.fillWidth: true
        }
    }
    function editAxis(role, channel, titleItem) {
        axisSettings.axisRole = role
        axisSettings.panelChannel = channel
        axisSettings.anchorPoint = titleItem.mapToItem(Overlay.overlay, titleItem.width / 2, titleItem.height / 2)
        axisSettings.open()
    }
    Popup {
        id: axisSettings
        objectName: "plotAxisPopup"
        parent: Overlay.overlay
        property string axisRole: "x"
        property string panelChannel: ""
        property point anchorPoint: Qt.point(0, 0)
        readonly property bool isX: axisRole === "x"
        property var choices: []
        readonly property var availableChoices: {
            if (!root.plotModel) return []
            if (isX) return ["Follow Sweep", "Step Index", "Vtg", "Vbg", "Vds", "Doping", "E-field"]
            const occupied = root.plotModel.plotCount > 1 || root.plotModel.layout === "overlay" ? root.plotModel.channelsForDisplay : []
            return root.plotModel.channelOptions.filter(function(name) { return name === panelChannel || occupied.indexOf(name) < 0 })
        }
        onAvailableChoicesChanged: {
            if (JSON.stringify(choices) !== JSON.stringify(availableChoices)) choices = availableChoices
        }
        readonly property string currentChoice: isX && root.plotModel ? root.plotModel.xAxis : panelChannel
        width: Math.min(320, parent.width - 24)
        height: Math.min(parent.height - 24, axisColumn.implicitHeight + 24)
        padding: 12
        focus: true
        closePolicy: Popup.CloseOnEscape | Popup.CloseOnPressOutside
        background: Rectangle { color: Theme.surface; border.color: Theme.controlBorder; radius: 5 }
        onOpened: Qt.callLater(function() {
            placeNearTitle()
            const option = axisOptions.itemAt(Math.max(0, choices.indexOf(currentChoice)))
            if (option) option.forceActiveFocus()
        })
        onHeightChanged: if (visible) placeNearTitle()
        onWidthChanged: if (visible) placeNearTitle()
        Connections {
            target: axisSettings.parent
            function onWidthChanged() { if (axisSettings.visible) axisSettings.placeNearTitle() }
            function onHeightChanged() { if (axisSettings.visible) axisSettings.placeNearTitle() }
        }
        function placeNearTitle() {
            x = Math.max(8, Math.min(anchorPoint.x + 12, parent.width - width - 8))
            y = Math.max(8, Math.min(anchorPoint.y + 12, parent.height - height - 8))
        }
        function choose(value) {
            if (!root.plotModel) return
            if (isX) root.plotModel.xAxis = value
            else root.plotModel.setPanelChannel(panelChannel, value)
            close()
        }
        contentItem: ScrollView {
            clip: true
            contentWidth: availableWidth
            ColumnLayout {
                id: axisColumn
                width: parent.width
                spacing: 4
                RowLayout {
                    Layout.fillWidth: true
                    Label { text: axisSettings.isX ? "X axis" : axisSettings.axisRole === "y2" ? "Right Y axis" : "Y axis"; font.bold: true; color: Theme.text; Layout.fillWidth: true }
                    ActionToolButton { text: "Close"; onClicked: axisSettings.close() }
                }
                Repeater {
                    id: axisOptions
                    model: axisSettings.choices
                    delegate: ItemDelegate {
                        required property string modelData
                        required property int index
                        objectName: "axisChoice_" + modelData
                        Layout.fillWidth: true
                        text: modelData
                        highlighted: modelData === axisSettings.currentChoice
                        onClicked: axisSettings.choose(modelData)
                        Keys.onReturnPressed: axisSettings.choose(modelData)
                        Keys.onEnterPressed: axisSettings.choose(modelData)
                        Keys.onDownPressed: { const next = axisOptions.itemAt(index + 1); if (next) next.forceActiveFocus() }
                        Keys.onUpPressed: { const previous = axisOptions.itemAt(index - 1); if (previous) previous.forceActiveFocus() }
                    }
                }
                Label { visible: axisSettings.isX && root.showRangeSelector; text: "X range"; color: Theme.muted }
                SelectBox {
                    visible: axisSettings.isX && root.showRangeSelector
                    Layout.fillWidth: true
                    model: ["Full sweep", "Acquired data"]
                    currentIndex: root.plotModel && root.plotModel.xRangeMode === "Acquired data" ? 1 : 0
                    onActivated: function(index) { if (root.plotModel) root.plotModel.xRangeMode = model[index] }
                }
                Label { visible: !axisSettings.isX && root.plotModel && root.plotModel.plotCount === 1; text: "Second Y signal"; color: Theme.muted }
                SelectBox {
                    objectName: "axisTitleSecondY"
                    visible: !axisSettings.isX && root.plotModel && root.plotModel.plotCount === 1
                    Layout.fillWidth: true
                    model: root.plotModel ? ["None"].concat(root.plotModel.channelOptions.filter(function(name) { return name !== root.plotModel.channel })) : ["None"]
                    currentIndex: root.plotModel && root.plotModel.layout === "overlay" ? model.indexOf(root.plotModel.overlayChannels[1]) : 0
                    onActivated: function(index) { if (root.plotModel) root.plotModel.setYAxes(root.plotModel.channel, index === 0 ? "" : model[index]) }
                    Accessible.name: "Second Y signal; None for one signal"
                }
                Label { visible: !axisSettings.isX && root.plotModel && root.plotModel.layout === "overlay"; text: "Y axes"; color: Theme.muted }
                SelectBox {
                    objectName: "axisTitleYMode"
                    visible: !axisSettings.isX && root.plotModel && root.plotModel.layout === "overlay"
                    Layout.fillWidth: true
                    model: ["Shared Y axis", "Independent left / right Y axes"]
                    currentIndex: root.plotModel && root.plotModel.overlayAxis === "dual" ? 1 : 0
                    onActivated: function(index) { if (root.plotModel) root.plotModel.overlayAxis = index === 1 ? "dual" : "shared" }
                    Accessible.name: "Overlay Y axes"
                }
            }
        }
    }
    Popup {
        id: plotSettings
        objectName: "plotOptionsPopup"
        parent: Overlay.overlay
        anchors.centerIn: parent
        width: Math.min(420, parent.width - 32)
        height: Math.min(parent.height - 40, settingsColumn.implicitHeight + 24)
        modal: true
        padding: 12
        background: Rectangle { color: Theme.surface; border.color: Theme.border; radius: 5 }
        contentItem: ScrollView {
            clip: true
            contentWidth: availableWidth
            ColumnLayout {
                id: settingsColumn
                width: parent.width
                spacing: 8
                RowLayout {
                    Layout.fillWidth: true
                    Label { text: "Plot options"; font.bold: true; color: Theme.text; Layout.fillWidth: true }
                    ActionToolButton { text: "Close"; onClicked: plotSettings.close() }
                }
                Label { visible: root.showRangeSelector; text: "X range"; color: Theme.muted }
                SelectBox {
                    visible: root.showRangeSelector
                    Layout.fillWidth: true
                    Accessible.name: "Plot X range"
                    model: ["Full sweep", "Acquired data"]
                    currentIndex: root.plotModel && root.plotModel.xRangeMode === "Acquired data" ? 1 : 0
                    wheelEnabled: false
                    onActivated: if (root.plotModel) root.plotModel.xRangeMode = currentText
                }
                Label { visible: !root.directAxes && root.showAxisSelector && !(root.plotModel && root.plotModel.isHeatmap); text: "X axis"; color: Theme.muted }
                SelectBox {
                    visible: !root.directAxes && root.showAxisSelector && !(root.plotModel && root.plotModel.isHeatmap)
                    Layout.fillWidth: true
                    Accessible.name: "Plot X axis"
                    model: ["Follow Sweep", "Step Index", "Vtg", "Vbg", "Vds", "Doping", "E-field"]
                    currentIndex: root.plotModel ? Math.max(0, model.indexOf(root.plotModel.xAxis)) : 0
                    wheelEnabled: false
                    onActivated: if (root.plotModel) root.plotModel.xAxis = currentText
                }
                Label { visible: !root.directAxes && root.showChannelSelector && root.plotModel && !root.plotModel.isHeatmap && root.plotModel.layout === "single"; text: "Current channel"; color: Theme.muted }
                SelectBox {
                    visible: !root.directAxes && root.showChannelSelector && root.plotModel && !root.plotModel.isHeatmap && root.plotModel.layout === "single"
                    Layout.fillWidth: true
                    Accessible.name: "Plot current channel"
                    model: root.plotModel ? root.plotModel.channelOptions : []
                    currentIndex: root.plotModel ? Math.max(0, model.indexOf(root.plotModel.channel)) : 0
                    wheelEnabled: false
                    onActivated: if (root.plotModel) root.plotModel.channel = currentText
                }
                Label { visible: !root.directAxes && root.plotModel && root.plotModel.layout === "overlay" && !root.plotModel.isHeatmap; text: "Overlay Y axes"; color: Theme.muted }
                SelectBox {
                    objectName: "plotOverlayAxes"
                    visible: !root.directAxes && root.plotModel && root.plotModel.layout === "overlay" && !root.plotModel.isHeatmap
                    Layout.fillWidth: true
                    model: ["Shared Y axis", "Independent left / right Y axes"]
                    currentIndex: root.plotModel && root.plotModel.overlayAxis === "dual" ? 1 : 0
                    onActivated: if (root.plotModel) root.plotModel.overlayAxis = currentIndex === 1 ? "dual" : "shared"
                    Accessible.name: "Overlay Y axes"
                }
                SelectBox {
                    visible: root.plotModel ? root.plotModel.isHeatmap : false
                    Layout.fillWidth: true
                    model: root.plotModel ? root.plotModel.colorMapOptions : []
                    currentIndex: root.plotModel ? Math.max(0, model.indexOf(root.plotModel.colorMap)) : 0
                    wheelEnabled: false
                    Accessible.name: "Heatmap color map"
                    onActivated: if (root.plotModel) root.plotModel.setColorMap(currentText)
                }
                Label { text: "Traces"; visible: legendList.count > 0; color: Theme.muted }
                Repeater {
                    id: legendList
                    objectName: "plotLegend"
                    // The popup is normally closed during acquisition/history
                    // refreshes; avoid constructing every hidden trace label.
                    model: plotSettings.visible && root.plotModel ? root.plotModel.legendEntries : []
                    delegate: Label {
                        required property var modelData
                        Layout.fillWidth: true
                        text: modelData
                        color: Theme.text
                        wrapMode: Text.WrapAnywhere
                    }
                }
            }
        }
    }
}
