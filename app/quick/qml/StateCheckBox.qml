import QtQuick
import QtQuick.Controls as Controls

Controls.CheckBox {
    id: control
    FontMetrics { id: metrics; font: control.font }
    implicitHeight: Math.max(Theme.controlHeight(metrics.height), implicitContentHeight + topPadding + bottomPadding)
    topPadding: Theme.controlVerticalPadding
    bottomPadding: Theme.controlVerticalPadding
    baselineOffset: topPadding + (availableHeight - implicitContentHeight) / 2 + metrics.ascent
    palette.windowText: Theme.text
    palette.disabled.windowText: Theme.disabledText
    indicator: Rectangle {
        implicitWidth: 14; implicitHeight: 14
        x: control.text ? (control.mirrored ? control.width - width - control.rightPadding : control.leftPadding) : control.leftPadding + (control.availableWidth - width) / 2
        y: control.topPadding + (control.availableHeight - height) / 2
        radius: 2
        color: !control.enabled ? Theme.disabledSurface : control.checked ? Theme.primary : control.down ? Theme.controlPressed : control.hovered ? Theme.controlHover : Theme.surface
        border.width: control.activeFocus ? 2 : 1
        border.color: !control.enabled ? Theme.disabledBorder : control.activeFocus ? Theme.focus : control.checked ? Theme.primary : Theme.controlBorder
        Item {
            anchors.fill: parent
            visible: control.checkState === Qt.Checked
            Rectangle {
                x: 2; y: 6; width: 4.5; height: 2
                color: control.enabled ? Theme.primaryText : Theme.disabledText
                antialiasing: true
                transform: Rotation { origin.x: 0; origin.y: 1; angle: 45 }
            }
            Rectangle {
                x: 5; y: 9; width: 8; height: 2
                color: control.enabled ? Theme.primaryText : Theme.disabledText
                antialiasing: true
                transform: Rotation { origin.x: 0; origin.y: 1; angle: -48 }
            }
        }
        Rectangle {
            anchors.centerIn: parent
            width: 8; height: 2
            visible: control.checkState === Qt.PartiallyChecked
            color: control.enabled ? Theme.primaryText : Theme.disabledText
        }
    }
}
