"""Persist harmless window layout only; never restore connections or a running task."""
from PySide6 import QtCore
from app.settings import get_app_settings

PREFIX = "workspace/window/"
STATE_VERSION = 1


def save_workspace(window, settings=None):
    settings = settings or get_app_settings()
    settings.setValue(PREFIX + "geometry", window.saveGeometry())
    settings.setValue(PREFIX + "state", window.saveState(STATE_VERSION))
    settings.setValue(PREFIX + "commands_visible", not window.command_bar.isHidden())
    selected = window.tabs.currentWidget()
    if selected in window._measurement_tabs():
        name = window.tabs.tabText(window.tabs.currentIndex())
    else:
        name = getattr(window, "_last_measurement_page", "Vds Sweep")
    settings.setValue(PREFIX + "measurement_page", name)
    settings.sync()


def restore_workspace(window, settings=None):
    settings = settings or get_app_settings()
    geometry = settings.value(PREFIX + "geometry")
    state = settings.value(PREFIX + "state")
    if isinstance(geometry, (QtCore.QByteArray, bytes)):
        window.restoreGeometry(geometry)
    if isinstance(state, (QtCore.QByteArray, bytes)):
        window.restoreState(state, STATE_VERSION)
    window.command_bar.setVisible(settings.value(PREFIX + "commands_visible", True, type=bool))
    name = str(settings.value(PREFIX + "measurement_page", "Vds Sweep"))
    with QtCore.QSignalBlocker(window.tabs):
        for index in range(window.tabs.count()):
            if window.tabs.tabText(index) == name and window.tabs.widget(index) in window._measurement_tabs():
                window.tabs.setCurrentIndex(index)
                window._last_measurement_page = name
                break


def reset_workspace(window, settings=None):
    settings = settings or get_app_settings()
    settings.remove(PREFIX.rstrip("/"))
    settings.sync()
    window.showNormal()
    window.resize(1400, 860)
    window.instrument_dock.show()
    window.command_bar.show()
    window.resizeDocks([window.instrument_dock], [370], QtCore.Qt.Orientation.Horizontal)
