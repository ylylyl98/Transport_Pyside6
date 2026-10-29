import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

ColumnLayout {
    id: pane
    objectName: "instrumentPane"
    required property var appBackend
    property int section: 0
    property var visitedSections: [true, false, false, false, false, false, false, false]
    onSectionChanged: {
        if (!visitedSections[section]) {
            const visited = visitedSections.slice()
            visited[section] = true
            visitedSections = visited
        }
    }
    property var instruments: appBackend.instruments
    property var magnet: appBackend.magnet
    property var lockin: section === 4 ? appBackend.driveLockin : appBackend.lockin
    // Display workflow order without changing the stable section IDs used by loaders.
    readonly property var pages: [
        {title: "Connections & protection", section: 1},
        {title: "Sample & files", section: 5},
        {title: "Signal chain", section: 6},
        {title: "Manual controls", section: 0},
        {title: "Magnet & temperature", section: 2},
        {title: "Drag / primary lock-in", section: 3},
        {title: "Drive lock-in", section: 4},
        {title: "Runtime settings", section: 7}
    ]
    spacing: 10
    ComboBox {
        Layout.fillWidth: true
        objectName: "instrumentPageSelector"
        model: pane.pages
        textRole: "title"; valueRole: "section"
        currentIndex: pane.pages.findIndex(page => page.section === pane.section)
        wheelEnabled: false
        onActivated: pane.section = currentValue
        Accessible.name: "Instrument panel"
    }
    Label {
        objectName: "instrumentStatus"
        text: pane.section === 2 ? pane.magnet.message : pane.section === 3 || pane.section === 4 ? pane.lockin.message : pane.instruments.message
        color: Theme.muted; wrapMode: Text.Wrap; Layout.fillWidth: true
    }
    StackLayout {
        currentIndex: pane.section
        Layout.fillWidth: true; Layout.fillHeight: true
        Loader {
            objectName: "instrumentLoader0"
            Layout.fillWidth: true; Layout.fillHeight: true
            // Keep visited pages alive so edits, dialogs and scroll positions survive navigation.
            active: pane.visitedSections[0]
            sourceComponent: Component {
                ScrollView {
                    id: manualScroll; objectName: "instrumentScroller0"
                    clip: true; contentWidth: availableWidth; ScrollBar.horizontal.policy: ScrollBar.AlwaysOff
                    ManualInstrumentSection { width: manualScroll.availableWidth; instruments: pane.instruments }
                }
            }
        }
        Loader {
            objectName: "instrumentLoader1"
            Layout.fillWidth: true; Layout.fillHeight: true
            // Keep visited pages alive so edits, dialogs and scroll positions survive navigation.
            active: pane.visitedSections[1]
            sourceComponent: Component {
                ScrollView {
                    id: connectionScroll; objectName: "instrumentScroller1"
                    clip: true; contentWidth: availableWidth; ScrollBar.horizontal.policy: ScrollBar.AlwaysOff
                    ConnectionInstrumentSection { width: connectionScroll.availableWidth; instruments: pane.instruments }
                }
            }
        }
        Loader {
            objectName: "instrumentLoader2"
            Layout.fillWidth: true; Layout.fillHeight: true
            // Keep visited pages alive so edits, dialogs and scroll positions survive navigation.
            active: pane.visitedSections[2]
            sourceComponent: Component {
                ScrollView {
                    id: magnetScroll; objectName: "instrumentScroller2"
                    clip: true; contentWidth: availableWidth; ScrollBar.horizontal.policy: ScrollBar.AlwaysOff
                    MagnetInstrumentSection { width: magnetScroll.availableWidth; magnet: pane.magnet }
                }
            }
        }
        Loader {
            objectName: "instrumentLoader3"
            Layout.fillWidth: true; Layout.fillHeight: true
            // Keep visited pages alive so edits, dialogs and scroll positions survive navigation.
            active: pane.visitedSections[3]
            sourceComponent: Component {
                ScrollView {
                    id: primaryScroll; objectName: "instrumentScroller3"
                    clip: true; contentWidth: availableWidth; ScrollBar.horizontal.policy: ScrollBar.AlwaysOff
                    LockinInstrumentSection { width: primaryScroll.availableWidth; lockin: appBackend.lockin; measurementActive: appBackend.measurement.active }
                }
            }
        }
        Loader {
            objectName: "instrumentLoader4"
            Layout.fillWidth: true; Layout.fillHeight: true
            // Keep visited pages alive so edits, dialogs and scroll positions survive navigation.
            active: pane.visitedSections[4]
            sourceComponent: Component {
                ScrollView {
                    id: driveScroll; objectName: "instrumentScroller4"
                    clip: true; contentWidth: availableWidth; ScrollBar.horizontal.policy: ScrollBar.AlwaysOff
                    LockinInstrumentSection { width: driveScroll.availableWidth; lockin: appBackend.driveLockin; drive: true; measurementActive: appBackend.measurement.active }
                }
            }
        }
        Loader {
            objectName: "instrumentLoader5"
            Layout.fillWidth: true; Layout.fillHeight: true
            // Keep visited pages alive so edits, dialogs and scroll positions survive navigation.
            active: pane.visitedSections[5]
            sourceComponent: Component {
                ScrollView {
                    id: sampleScroll; objectName: "instrumentScroller5"
                    clip: true; contentWidth: availableWidth; ScrollBar.horizontal.policy: ScrollBar.AlwaysOff
                    ColumnLayout {
                        width: sampleScroll.availableWidth
                        spacing: 10
                        Label { text: "Choose the operator, sample and data folder before setting up a measurement. Files are saved under Data folder / Operator / Device."; color: Theme.muted; wrapMode: Text.Wrap; Layout.fillWidth: true }
                        Repeater {
                            // Descriptor refreshes must keep editors and the Tab focus chain alive.
                            model: pane.instruments.sampleFields.length
                            delegate: FieldEditor {
                                required property int index
                                readonly property var modelData: pane.instruments.sampleFields[index] || ({})
                                formField: modelData; locked: pane.instruments.busy
                                trailingActionText: modelData.key === "base" ? "Browse…" : ""
                                trailingActionEnabled: !pane.instruments.busy
                                onTrailingActionTriggered: appBackend.chooseFolderRequested()
                                onCommitted: value => pane.instruments.setValue(modelData.key, value)
                            }
                        }
                    }
                }
            }
        }
        Loader {
            objectName: "instrumentLoader6"
            Layout.fillWidth: true; Layout.fillHeight: true
            // Keep visited pages alive so edits, dialogs and scroll positions survive navigation.
            active: pane.visitedSections[6]
            sourceComponent: Component {
                ScrollView {
                    id: signalScroll; objectName: "instrumentScroller6"
                    clip: true; contentWidth: availableWidth; ScrollBar.horizontal.policy: ScrollBar.AlwaysOff
                    ColumnLayout {
                        width: signalScroll.availableWidth
                        spacing: 10
                        Label { text: "Set the signal conversion, then read settings in the lock-in pages to verify calibration."; color: Theme.muted; wrapMode: Text.Wrap; Layout.fillWidth: true }
                        FieldForm { fields: pane.instruments.signalFields; target: pane.instruments; locked: pane.instruments.busy; Layout.fillWidth: true }
                        Label { text: "Verified instrument readback"; font.bold: true; color: Theme.text }
                        Label { text: Object.keys(pane.instruments.calibrationStatus).map(k => k + ": " + pane.instruments.calibrationStatus[k]).join("\n") || "Connect and refresh the lock-in panels to verify calibration."; color: Theme.muted; wrapMode: Text.Wrap; Layout.fillWidth: true }
                        Label { text: pane.instruments.acVoltageEstimate; color: Theme.muted; wrapMode: Text.Wrap; Layout.fillWidth: true }
                    }
                }
            }
        }
        Loader {
            objectName: "instrumentLoader7"
            Layout.fillWidth: true; Layout.fillHeight: true
            // Keep visited pages alive so edits, dialogs and scroll positions survive navigation.
            active: pane.visitedSections[7]
            sourceComponent: Component {
                ScrollView {
                    id: runtimeScroll; objectName: "instrumentScroller7"
                    clip: true; contentWidth: availableWidth; ScrollBar.horizontal.policy: ScrollBar.AlwaysOff
                    FieldForm { width: runtimeScroll.availableWidth; fields: appBackend.runtime.fields; target: appBackend.runtime }
                }
            }
        }
    }
}
