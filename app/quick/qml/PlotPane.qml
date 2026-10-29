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
    property bool exportEnabled: plotModel ? plotModel.pointCount > 0 : false
    property bool showEmptyState: true
    property string emptyText: "No data yet\nAcquired points will appear here."
    readonly property bool compact: width < 760
    readonly property bool small: width < 460
    signal saveRequested()
    padding: 0
    background: Rectangle { color: Theme.surface; radius: 4; border.color: Theme.border }
    ColumnLayout {
        anchors.fill: parent
        spacing: 0
        RowLayout {
            Layout.fillWidth: true
            Layout.margins: 5
            spacing: 2
            ToolButton {
                text: root.small ? qsTr("Reset") : qsTr("Reset view")
                Accessible.name: "Reset plot view"
                onClicked: if (root.plotModel) root.plotModel.home()
                ToolTip.visible: hovered
                ToolTip.text: qsTr("Restore the selected X range policy and automatic Y scaling")
            }
            ToolButton {
                text: root.small ? (root.plotModel && root.plotModel.interactionMode === "zoom" ? "Zoom" : "Pan") : "Pan"
                checkable: true
                checked: root.plotModel ? root.plotModel.interactionMode === "pan" : true
                onClicked: if (root.plotModel) root.plotModel.interactionMode = root.small && root.plotModel.interactionMode === "pan" ? "zoom" : "pan"
                ToolTip.visible: hovered
                ToolTip.text: root.small ? "Toggle pan / rectangle zoom" : "Drag to pan"
            }
            ToolButton {
                visible: !root.small
                text: qsTr("Zoom")
                checkable: true
                checked: root.plotModel ? root.plotModel.interactionMode === "zoom" : false
                onClicked: if (root.plotModel) root.plotModel.interactionMode = "zoom"
                ToolTip.visible: hovered
                ToolTip.text: qsTr("Drag a rectangle. Scroll to zoom at the cursor; middle-drag to pan.")
            }
            ComboBox {
                visible: !root.compact
                Layout.preferredWidth: 136
                model: ["Full sweep", "Acquired data"]
                currentIndex: root.plotModel && root.plotModel.xRangeMode === "Acquired data" ? 1 : 0
                wheelEnabled: false
                Accessible.name: "Plot X range"
                onActivated: if (root.plotModel) root.plotModel.xRangeMode = currentText
            }
            ComboBox {
                visible: !root.compact && root.showAxisSelector && !(root.plotModel && root.plotModel.isHeatmap)
                Layout.preferredWidth: 126
                model: ["Follow Sweep", "Step Index", "Vtg", "Vbg", "Vds", "Doping", "E-field"]
                currentIndex: root.plotModel ? Math.max(0, model.indexOf(root.plotModel.xAxis)) : 0
                wheelEnabled: false
                Accessible.name: "Plot X axis"
                onActivated: if (root.plotModel) root.plotModel.xAxis = currentText
            }
            ComboBox {
                visible: !root.small && root.showChannelSelector && !(root.plotModel && root.plotModel.isHeatmap)
                Layout.preferredWidth: 132
                model: root.plotModel ? root.plotModel.channelOptions : []
                currentIndex: root.plotModel ? Math.max(0, root.plotModel.channelOptions.indexOf(root.plotModel.channel)) : 0
                wheelEnabled: false
                Accessible.name: "Plot current channel"
                onActivated: if (root.plotModel) root.plotModel.channel = currentText
            }
            Item { Layout.fillWidth: true; Layout.minimumWidth: 0 }
            ToolButton {
                text: root.compact ? "Options" : "Plot options"
                Accessible.name: "Plot axes, range, layout and legend"
                ToolTip.visible: hovered
                ToolTip.text: "Choose axes, scaling, channel layout and legend"
                onClicked: plotSettings.open()
            }
            ToolButton {
                visible: root.showExport
                text: qsTr("Export")
                enabled: root.exportEnabled
                ToolTip.visible: hovered
                ToolTip.text: "Save PNG and full-resolution CSV"
                onClicked: root.saveRequested()
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
            }
            Label {
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
            text: (root.plotModel ? root.plotModel.pointCount.toLocaleString() : "0") + (root.small ? " samples · double-click: reset" : " samples · scroll: zoom · drag: pan/rectangle · double-click: reset view")
            color: Theme.muted
            font.pixelSize: 11
            elide: Text.ElideRight
            Layout.fillWidth: true
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
                    ToolButton { text: "Close"; onClicked: plotSettings.close() }
                }
                Label { text: "X range"; color: Theme.muted }
                ComboBox {
                    Layout.fillWidth: true
                    model: ["Full sweep", "Acquired data"]
                    currentIndex: root.plotModel && root.plotModel.xRangeMode === "Acquired data" ? 1 : 0
                    wheelEnabled: false
                    onActivated: if (root.plotModel) root.plotModel.xRangeMode = currentText
                }
                Label { visible: root.showAxisSelector && !(root.plotModel && root.plotModel.isHeatmap); text: "X axis"; color: Theme.muted }
                ComboBox {
                    visible: root.showAxisSelector && !(root.plotModel && root.plotModel.isHeatmap)
                    Layout.fillWidth: true
                    model: ["Follow Sweep", "Step Index", "Vtg", "Vbg", "Vds", "Doping", "E-field"]
                    currentIndex: root.plotModel ? Math.max(0, model.indexOf(root.plotModel.xAxis)) : 0
                    wheelEnabled: false
                    onActivated: if (root.plotModel) root.plotModel.xAxis = currentText
                }
                Label { visible: root.showChannelSelector && !(root.plotModel && root.plotModel.isHeatmap); text: "Current channel"; color: Theme.muted }
                ComboBox {
                    visible: root.showChannelSelector && !(root.plotModel && root.plotModel.isHeatmap)
                    Layout.fillWidth: true
                    model: root.plotModel ? root.plotModel.channelOptions : []
                    currentIndex: root.plotModel ? Math.max(0, model.indexOf(root.plotModel.channel)) : 0
                    wheelEnabled: false
                    onActivated: if (root.plotModel) root.plotModel.channel = currentText
                }
                CheckBox {
                    visible: root.showLayoutSelector && !(root.plotModel && root.plotModel.isHeatmap)
                    text: "Compare four channels"
                    checked: root.plotModel ? root.plotModel.fourChannels : false
                    onClicked: if (root.plotModel) root.plotModel.fourChannels = checked
                }
                ComboBox {
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
