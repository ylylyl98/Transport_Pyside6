import QtQuick
import QtQuick.Controls

// Share navigation states, while rendering ampersands literally rather than as mnemonics.
TabButton {
    id: control
    FontMetrics { id: metrics; font: control.font }
    topPadding: Theme.controlVerticalPadding
    bottomPadding: Theme.controlVerticalPadding
    topInset: 0
    bottomInset: 0
    horizontalPadding: 12
    implicitWidth: Math.max(implicitContentWidth + leftPadding + rightPadding, 64)
    implicitHeight: Theme.controlHeight(Math.max(metrics.height, implicitContentHeight))
    baselineOffset: (height - metrics.height) / 2 + metrics.ascent
    Accessible.name: text
    contentItem: Text {
        text: control.text
        font: control.font
        color: !control.enabled ? Theme.disabledText : control.checked ? Theme.primaryText : Theme.text
        horizontalAlignment: Text.AlignHCenter
        verticalAlignment: Text.AlignVCenter
    }
    background: Rectangle {
        color: Theme.actionFill(control.enabled, control.checked, control.down, control.hovered, "neutral")
        border.width: control.activeFocus ? 2 : 1
        border.color: !control.enabled ? Theme.disabledBorder : control.activeFocus ? Theme.focus : control.checked ? Theme.primary : Theme.controlBorder
        radius: 3
        Rectangle {
            anchors.left: parent.left; anchors.right: parent.right; anchors.bottom: parent.bottom
            height: 3
            visible: control.checked
            color: control.enabled ? Theme.primaryText : Theme.disabledText
        }
    }
}
