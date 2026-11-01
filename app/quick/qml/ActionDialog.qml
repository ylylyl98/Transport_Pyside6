import QtQuick
import QtQuick.Controls as Controls

Controls.Dialog {
    palette.window: Theme.background
    palette.windowText: Theme.text
    palette.mid: Theme.controlBorder
    footer: ActionDialogButtonBox { visible: count > 0 }
}
