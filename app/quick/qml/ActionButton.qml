import QtQuick
import QtQuick.Controls as Controls

Controls.Button {
    id: button
    FontMetrics { id: metrics; font: button.font }
    implicitHeight: Theme.controlHeight(Math.max(metrics.height, implicitContentHeight))
    topPadding: Theme.controlVerticalPadding
    bottomPadding: Theme.controlVerticalPadding
    topInset: 0
    bottomInset: 0
    baselineOffset: (height - metrics.height) / 2 + metrics.ascent
    property string tone: "neutral"
    readonly property bool emphasized: highlighted || checked || tone === "primary"
    readonly property color foreground: Theme.actionText(enabled, emphasized, tone)
    palette.buttonText: foreground
    palette.disabled.buttonText: Theme.disabledText
    palette.brightText: foreground
    background: ActionSurface {
        implicitWidth: 80; implicitHeight: 24
        control: button
        tone: button.tone
        emphasized: button.emphasized
    }
}
