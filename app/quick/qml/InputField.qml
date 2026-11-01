import QtQuick
import QtQuick.Controls as Controls

Controls.TextField {
    id: input
    FontMetrics { id: metrics; font: input.font }
    implicitHeight: Theme.controlHeight(metrics.height)
    topPadding: Theme.controlVerticalPadding
    bottomPadding: Theme.controlVerticalPadding
    topInset: 0
    bottomInset: 0
    verticalAlignment: TextInput.AlignVCenter
    property bool invalid: false
    color: enabled ? Theme.text : Theme.disabledText
    placeholderTextColor: Theme.muted
    selectionColor: Theme.primary
    selectedTextColor: Theme.primaryText
    background: Rectangle {
        implicitWidth: 120; implicitHeight: 24
        radius: 3
        color: !input.enabled ? Theme.disabledSurface : input.readOnly ? Theme.readOnlyFill : Theme.surface
        border.width: input.activeFocus ? 2 : 1
        border.color: !input.enabled ? Theme.disabledBorder : input.invalid ? Theme.danger : input.activeFocus ? Theme.focus : Theme.controlBorder
    }
}
