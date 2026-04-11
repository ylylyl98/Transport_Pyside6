from __future__ import annotations

from PyQt6 import QtCore

from app.constants import SETTINGS_APP, SETTINGS_ORG


def get_app_settings() -> QtCore.QSettings:
    return QtCore.QSettings(QtCore.QSettings.Format.IniFormat, QtCore.QSettings.Scope.UserScope, SETTINGS_ORG, SETTINGS_APP)
