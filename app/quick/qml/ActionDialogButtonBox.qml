import QtQuick
import QtQuick.Controls as Controls

Controls.DialogButtonBox {
    delegate: ActionButton {
        tone: Controls.DialogButtonBox.buttonRole === Controls.DialogButtonBox.DestructiveRole ? "danger"
            : Controls.DialogButtonBox.buttonRole === Controls.DialogButtonBox.AcceptRole
              || Controls.DialogButtonBox.buttonRole === Controls.DialogButtonBox.YesRole
              || Controls.DialogButtonBox.buttonRole === Controls.DialogButtonBox.ApplyRole ? "primary" : "neutral"
    }
}
