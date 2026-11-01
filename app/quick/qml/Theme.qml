pragma Singleton
import QtQuick

QtObject {
    property bool dark: false
    property real basePointSize: 10
    readonly property real captionPointSize: Math.max(8, basePointSize - 1)
    readonly property real titlePointSize: basePointSize + 1
    readonly property int controlMinimumHeight: 28
    readonly property int controlVerticalPadding: 5
    function controlHeight(contentHeight) {
        return Math.max(controlMinimumHeight, Math.ceil(contentHeight) + controlVerticalPadding * 2)
    }
    readonly property color background: dark ? "#141923" : "#f3f5f8"
    readonly property color surface: dark ? "#1d2532" : "#ffffff"
    readonly property color text: dark ? "#edf2f8" : "#17283d"
    readonly property color muted: dark ? "#a8b7c9" : "#53677e"
    readonly property color border: dark ? "#42516a" : "#c5d0dd"
    readonly property color accent: dark ? "#8fbaff" : "#165dc3"
    readonly property color danger: dark ? "#ffb4ab" : "#a91c22"
    readonly property color warning: dark ? "#ffce85" : "#8a4a00"
    readonly property color dangerButton: dark ? "#a82b32" : "#b62129"
    readonly property color dangerPressed: "#7e1519"
    readonly property color dangerText: "#ffffff"
    readonly property color selection: dark ? "#283e5e" : "#e7f0fe"
    // Interactive surfaces are separate from structural dividers and text accents.
    readonly property color controlSurface: dark ? "#2b3d55" : "#e6edf7"
    readonly property color controlHover: dark ? "#385575" : "#d3e3f8"
    readonly property color controlPressed: dark ? "#466c94" : "#bed4f1"
    readonly property color controlBorder: dark ? "#8ca6c8" : "#526b8d"
    readonly property color readOnlyFill: dark ? "#202b3b" : "#edf2f8"
    readonly property color disabledSurface: dark ? "#242f40" : "#e4e9f0"
    readonly property color disabledText: dark ? "#a6b3c7" : "#596779"
    readonly property color disabledBorder: dark ? "#52657d" : "#9cabbf"
    readonly property color primary: dark ? "#8dbaff" : "#155bbb"
    readonly property color primaryHover: dark ? "#a6caff" : "#0f4fa7"
    readonly property color primaryPressed: dark ? "#75a9f3" : "#0b418d"
    readonly property color primaryText: dark ? "#10253f" : "#ffffff"
    readonly property color focus: dark ? "#ffd58a" : "#123e78"
    readonly property color dangerHover: "#c52e39"
    readonly property color progressTrack: dark ? "#25354a" : "#dce5f0"
    readonly property color progressFill: primary
    readonly property color progressText: primaryText
    readonly property color progressSuccess: dark ? "#75d5a4" : "#1c6b43"
    readonly property color progressWarning: warning
    readonly property color progressDanger: dark ? "#ff8c97" : "#b62129"
    // Coordinate identity is independent of acquisition status and errors.
    readonly property color coordinateDoping: dark ? "#d4b4ff" : "#7040a0"
    readonly property color coordinateEfield: dark ? "#80d5cc" : "#00695f"
    readonly property color coordinateVtg: dark ? "#8fbaff" : "#165dc3"
    readonly property color coordinateVbg: dark ? "#d9c392" : "#72551f"
    readonly property color coordinateVds: dark ? "#b9c7de" : "#475974"

    function coordinateColor(coordinate) {
        if (coordinate === "Doping") return coordinateDoping
        if (coordinate === "E-field") return coordinateEfield
        if (coordinate === "Vtg") return coordinateVtg
        if (coordinate === "Vbg") return coordinateVbg
        if (coordinate === "Vds") return coordinateVds
        return text
    }

    function progressTone(phase) {
        if (phase === "error" || phase === "failed") return "danger"
        if (phase === "complete" || phase === "finished") return "success"
        if (phase === "cleanup" || phase === "stopping" || phase === "stopped" || phase === "paused") return "warning"
        return "primary"
    }

    function actionFill(enabled, emphasized, down, hovered, tone) {
        if (!enabled) return disabledSurface
        if (tone === "danger") return down ? dangerPressed : hovered ? dangerHover : dangerButton
        if (emphasized) return down ? primaryPressed : hovered ? primaryHover : primary
        return down ? controlPressed : hovered ? controlHover : controlSurface
    }
    function actionText(enabled, emphasized, tone) {
        return !enabled ? disabledText : tone === "danger" ? dangerText : emphasized ? primaryText : text
    }
}
