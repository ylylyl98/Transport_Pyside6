"""Reload presentation data without rebuilding widgets or hardware owners."""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import re

from PySide6 import QtCore, QtGui, QtWidgets

from app.ui.style import read_app_style


@dataclass(frozen=True)
class StyleReloadResult:
    ok: bool
    error: str = ""


@contextmanager
def _style_errors():
    messages = []

    def collect(kind, context, message):
        if "could not parse" in message.lower() or "unknown property" in message.lower():
            messages.append(message)
        elif previous is not None:
            previous(kind, context, message)

    previous = QtCore.qInstallMessageHandler(collect)
    try:
        yield messages
    finally:
        QtCore.qInstallMessageHandler(previous)


def _raise_style_errors(messages):
    if messages:
        raise ValueError("; ".join(dict.fromkeys(messages)))


def _validate_style(sheet: str) -> None:
    if not sheet.strip():
        raise ValueError("The style sheet is empty")
    # Qt can silently ignore a truncated final rule. Reject unfinished writes
    # before asking its parser to validate the complete sheet.
    # Preserve offsets so each declaration body can also be validated with
    # its original quoted values, independently of its selector/state.
    syntax = re.sub(r'/\*.*?\*/|"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'',
                    lambda match: " " * len(match.group()), sheet, flags=re.S)
    if syntax.count("{") != syntax.count("}") or "/*" in syntax or "*/" in syntax or '"' in syntax or "'" in syntax:
        raise ValueError("The style sheet contains an unfinished rule or comment")
    rules = list(re.finditer(r"([^{}]+)\{([^{}]*)\}", syntax))
    if not rules or re.sub(r"[^{}]+\{[^{}]*\}", "", syntax).strip():
        raise ValueError("The style sheet contains an invalid rule")
    for rule in rules:
        # Qt also silently drops malformed declarations such as `color red`.
        # Ignore semicolons inside url()/gradient() values when checking them.
        declarations = re.split(r";(?![^()]*\))", rule.group(2))
        if any(item.strip() and not re.match(r"\s*[\w-]+\s*:", item) for item in declarations):
            raise ValueError("Each style declaration needs a property and colon")
    probe = QtWidgets.QWidget()
    try:
        with _style_errors() as messages:
            probe.setStyleSheet(sheet)
            probe.ensurePolished()
            # Qt evaluates declarations lazily for matching widgets. A bare
            # QWidget misses rules for buttons, dynamic properties and hover.
            # Validate every body as an unconditional rule as well.
            for rule in rules:
                body = sheet[rule.start(2):rule.end(2)]
                probe.setStyleSheet("QWidget {" + body + "}")
                probe.ensurePolished()
        _raise_style_errors(messages)
    finally:
        # Deferred deletion leaves the probe in QApplication.allWidgets until
        # the next event-loop turn. Do not let its rejected local rules leak
        # into a subsequent application-wide polish/validation.
        probe.setStyleSheet("")
        probe.deleteLater()


def apply_app_style(app=None, *, path=None, font_size_pt=10) -> StyleReloadResult:
    """Read and validate first; on failure leave the current style untouched."""
    app = app or QtWidgets.QApplication.instance()
    if app is None:
        return StyleReloadResult(False, "A QApplication is required")
    if QtCore.QThread.currentThread() != app.thread():
        return StyleReloadResult(False, "Reload UI style on the Qt GUI thread")
    try:
        sheet = read_app_style(path, font_size_pt=font_size_pt)
        _validate_style(sheet)
    except (OSError, UnicodeError, ValueError, TypeError, OverflowError) as exc:
        return StyleReloadResult(False, str(exc))
    previous_sheet = app.styleSheet()
    try:
        with _style_errors() as messages:
            app.setStyleSheet(sheet)
            for widget in app.allWidgets():
                widget.ensurePolished()
        _raise_style_errors(messages)
    except Exception as exc:
        # Keep a final transaction boundary for errors discovered only while
        # polishing the actual widget tree. Restore the exact live sheet.
        app.setStyleSheet(previous_sheet)
        return StyleReloadResult(False, str(exc))
    return StyleReloadResult(True)


def install_style_reload(window, menu, *, shortcut="Ctrl+Alt+R", font_size_provider=None):
    """Add an explicit reload action; font_size_provider is evaluated per click."""
    action = QtGui.QAction("Reload UI style", window)
    action.setObjectName("reloadUiStyle")
    action.setShortcut(QtGui.QKeySequence(shortcut))
    action.setToolTip("Apply app/ui/app.qss without restarting Transport")

    def reload_style():
        try:
            size = font_size_provider() if font_size_provider is not None else 10
            result = apply_app_style(font_size_pt=size)
        except Exception as exc:
            result = StyleReloadResult(False, str(exc))
        window.statusBar().showMessage(
            "UI style reloaded" if result.ok else "UI style unchanged: " + result.error,
            6000 if result.ok else 15000,
        )

    action.triggered.connect(reload_style)
    menu.addAction(action)
    return action
