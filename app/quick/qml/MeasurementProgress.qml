import QtQuick
import QtQuick.Controls as Controls

Controls.ProgressBar {
    id: control
    FontMetrics { id: metrics; font: control.font }
    implicitHeight: Theme.controlHeight(metrics.height)
    topPadding: 0
    bottomPadding: 0
    topInset: 0
    bottomInset: 0
    property string tone: "primary"
    readonly property string progressText: indeterminate ? "Working…" : Math.round(position * 100) + "%"
    readonly property color fillColor: !enabled ? Theme.disabledText : tone === "danger" ? Theme.progressDanger
        : tone === "success" ? Theme.progressSuccess : tone === "warning" ? Theme.progressWarning : Theme.progressFill
    readonly property color fillTextColor: !enabled ? Theme.disabledSurface : Theme.progressText
    background: Rectangle {
        implicitWidth: 120; implicitHeight: 24
        radius: 4
        color: Theme.progressTrack
        border.color: control.enabled ? Theme.controlBorder : Theme.disabledBorder
    }
    contentItem: Item {
        id: track
        implicitWidth: 120; implicitHeight: 24
        clip: true
        Text {
            anchors.fill: parent
            text: control.progressText
            font: control.font
            color: Theme.text
            horizontalAlignment: Text.AlignHCenter
            verticalAlignment: Text.AlignVCenter
            Accessible.ignored: true
        }
        Rectangle {
            id: fill
            width: control.indeterminate ? track.width * 0.32 : control.position * track.width
            height: track.height
            x: control.indeterminate ? 0 : control.mirrored ? track.width - width : 0
            color: control.fillColor
            radius: 3
            clip: true
            Text {
                x: -fill.x
                width: track.width; height: track.height
                text: control.progressText
                font: control.font
                color: control.fillTextColor
                horizontalAlignment: Text.AlignHCenter
                verticalAlignment: Text.AlignVCenter
                Accessible.ignored: true
            }
            NumberAnimation on x {
                running: control.indeterminate && control.visible
                from: control.mirrored ? track.width : -fill.width
                to: control.mirrored ? -fill.width : track.width
                duration: 1100
                loops: Animation.Infinite
            }
        }
    }
}
