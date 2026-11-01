import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

ActionDialog {
    id: dialog
    objectName: "updateDialog"
    required property var updateService
    parent: Overlay.overlay
    anchors.centerIn: parent
    width: Math.min(540, parent.width - 32)
    title: "Software update"
    modal: false
    closePolicy: Popup.CloseOnEscape
    onClosed: updateService.hideDialog()
    Connections {
        target: dialog.updateService
        function onChanged() {
            if (dialog.updateService.dialogVisible && !dialog.visible) dialog.open()
            else if (!dialog.updateService.dialogVisible && dialog.visible) dialog.close()
        }
    }
    contentItem: ColumnLayout {
        spacing: 12
        Label {
            text: "Installed: " + (dialog.updateService.currentVersion || "Source / unversioned build")
                + (dialog.updateService.latestVersion ? "    Latest: " + dialog.updateService.latestVersion : "")
            color: Theme.text
            wrapMode: Text.Wrap
            Layout.fillWidth: true
        }
        Label {
            objectName: "updateMessage"
            text: dialog.updateService.downloadMessage || dialog.updateService.message
            color: dialog.updateService.downloadState === "error" ? Theme.danger : Theme.text
            wrapMode: Text.Wrap
            Layout.fillWidth: true
        }
        MeasurementProgress {
            objectName: "updateProgress"
            Layout.fillWidth: true
            visible: dialog.updateService.checking || ["downloading", "cancelling", "ready", "installing"].indexOf(dialog.updateService.downloadState) >= 0
            indeterminate: dialog.updateService.checking || dialog.updateService.downloadState === "installing"
            value: dialog.updateService.downloadProgress
            tone: dialog.updateService.downloadState === "ready" ? "success" : "primary"
        }
        Label {
            visible: dialog.updateService.totalBytes > 0
            text: (dialog.updateService.downloadedBytes / 1048576).toFixed(1) + " / "
                + (dialog.updateService.totalBytes / 1048576).toFixed(1) + " MB"
            color: Theme.muted
            Layout.fillWidth: true
        }
        Label {
            text: dialog.updateService.downloadState === "ready" && !dialog.updateService.canInstall
                ? "Installation is available after measurements, cleanup and instrument operations finish."
                : "Download runs in the background. Exit and install closes Transport normally, then opens the Windows installer. Settings and measurement data are kept."
            color: Theme.muted
            wrapMode: Text.Wrap
            Layout.fillWidth: true
        }
    }
    footer: Pane {
        padding: 12
        background: Rectangle { color: Theme.background }
        contentItem: Flow {
            spacing: 8
            ActionButton {
                objectName: "updateDownloadInstaller"
                visible: dialog.updateService.available && ["idle", "error"].indexOf(dialog.updateService.downloadState) >= 0
                text: dialog.updateService.downloadState === "error" ? "Retry download" : "Download update"
                tone: "primary"
                enabled: dialog.updateService.canDownload
                onClicked: dialog.updateService.downloadUpdate()
            }
            ActionButton {
                objectName: "updateCancelDownload"
                visible: ["downloading", "cancelling"].indexOf(dialog.updateService.downloadState) >= 0
                text: dialog.updateService.downloadState === "cancelling" ? "Cancelling…" : "Cancel download"
                enabled: dialog.updateService.downloadState === "downloading"
                onClicked: dialog.updateService.cancelDownload()
            }
            ActionButton {
                objectName: "updateInstall"
                visible: ["ready", "installing"].indexOf(dialog.updateService.downloadState) >= 0
                text: "Exit and install"
                tone: "primary"
                enabled: dialog.updateService.canInstall
                onClicked: dialog.updateService.installUpdate()
            }
            ActionButton {
                visible: !dialog.updateService.available && dialog.updateService.downloadState === "idle"
                text: "Check again"
                enabled: !dialog.updateService.checking
                onClicked: dialog.updateService.checkNow()
            }
            ActionButton {
                text: "Later"
                enabled: !dialog.updateService.installPending
                onClicked: dialog.updateService.hideDialog()
            }
            ActionToolButton {
                text: "Release notes"
                visible: dialog.updateService.releaseUrl.length > 0
                onClicked: dialog.updateService.openRelease()
            }
        }
    }
}
