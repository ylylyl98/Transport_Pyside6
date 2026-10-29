pragma Singleton
import QtQuick

QtObject {
    property bool dark: false
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
}
