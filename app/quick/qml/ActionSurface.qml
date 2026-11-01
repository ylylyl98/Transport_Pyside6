import QtQuick

Rectangle {
    id: surface
    required property var control
    property string tone: "neutral"
    property bool emphasized: false
    property bool subtle: false
    color: subtle && control.enabled && !control.hovered && !control.down && !emphasized ? "transparent"
         : Theme.actionFill(control.enabled, emphasized, control.down, control.hovered, tone)
    radius: 4
    border.width: 1
    border.color: !control.enabled ? Theme.disabledBorder : subtle && !control.hovered && !control.down && !emphasized ? "transparent" : emphasized || tone === "danger" ? surface.color : Theme.controlBorder
    Rectangle {
        x: -2; y: -2
        width: parent.width + 4; height: parent.height + 4
        radius: 6
        color: "transparent"
        border.width: 2
        border.color: Theme.focus
        visible: surface.control.enabled && surface.control.activeFocus
    }
}
