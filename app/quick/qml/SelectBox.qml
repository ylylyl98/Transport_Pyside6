import QtQuick
import QtQuick.Controls as Controls

Controls.ComboBox {
    id: select
    FontMetrics { id: metrics; font: select.font }
    implicitHeight: Theme.controlHeight(metrics.height)
    // Fusion's content item is already a TextField. Keep the single vertical
    // padding inside that field, including when the combo is editable.
    topPadding: 0
    bottomPadding: 0
    topInset: 0
    bottomInset: 0
    baselineOffset: (height - metrics.height) / 2 + metrics.ascent
    Binding { target: select.contentItem; property: "topPadding"; value: Theme.controlVerticalPadding }
    Binding { target: select.contentItem; property: "bottomPadding"; value: Theme.controlVerticalPadding }
    palette.buttonText: enabled ? Theme.text : Theme.disabledText
    palette.disabled.buttonText: Theme.disabledText
    palette.disabled.text: Theme.disabledText
    palette.text: Theme.text
    palette.base: Theme.surface
    palette.highlight: Theme.primary
    palette.highlightedText: Theme.primaryText
    background: ActionSurface {
        implicitWidth: 120; implicitHeight: 24
        control: select
    }
}
