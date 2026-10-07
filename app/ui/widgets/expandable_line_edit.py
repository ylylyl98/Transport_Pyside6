"""Single-value inputs with a wrapped preview and a full-size editor."""
from __future__ import annotations

import math
import html
from PySide6 import QtCore, QtGui, QtWidgets


class _WrappedPreview(QtWidgets.QTextEdit):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setReadOnly(True)
        self.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)
        self.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setWordWrapMode(QtGui.QTextOption.WrapMode.WrapAnywhere)
        self.setSizePolicy(QtWidgets.QSizePolicy.Policy.Ignored, QtWidgets.QSizePolicy.Policy.Fixed)
        self.setMinimumWidth(0)
        self.setStyleSheet("QTextEdit { background: transparent; color: #6B7280; border: none; padding: 0; }")

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.fit_height()

    def fit_height(self):
        self.document().setTextWidth(max(1, self.viewport().width()))
        maximum = self.fontMetrics().lineSpacing() * 5 + 10
        height = min(maximum, max(24, math.ceil(self.document().size().height()) + 4))
        if self.height() != height:
            self.setFixedHeight(height)


class _ValueEditor(QtWidgets.QPlainTextEdit):
    apply_requested = QtCore.Signal()

    def __init__(self, allow_newlines, parent=None):
        super().__init__(parent)
        self.allow_newlines = allow_newlines
        self.setLineWrapMode(QtWidgets.QPlainTextEdit.LineWrapMode.WidgetWidth)
        self.setWordWrapMode(QtGui.QTextOption.WrapMode.WrapAnywhere)
        self.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

    def keyPressEvent(self, event):
        if event.key() in (QtCore.Qt.Key.Key_Return, QtCore.Qt.Key.Key_Enter):
            if not self.allow_newlines or event.modifiers() & QtCore.Qt.KeyboardModifier.ControlModifier:
                self.apply_requested.emit()
                event.accept()
                return
        super().keyPressEvent(event)


class ExpandedValueDialog(QtWidgets.QDialog):
    """Apply one draft atomically; visual wrapping never changes stored text."""
    def __init__(self, field):
        super().__init__(field.window())
        self.field = field
        self.original_value = field.text()
        self.setWindowTitle(field.editor_title)
        self.resize(720, 430)
        layout = QtWidgets.QVBoxLayout(self)
        hint = QtWidgets.QLabel("Text wraps automatically. Ctrl+Enter applies; Esc cancels.")
        hint.setWordWrap(True)
        layout.addWidget(hint)
        self.editor = _ValueEditor(field.allow_newlines)
        self.editor.setPlainText(self.original_value)
        self.editor.setReadOnly(field.isReadOnly())
        cursor = self.editor.textCursor()
        cursor.setPosition(field.cursorPosition())
        self.editor.setTextCursor(cursor)
        self.editor.apply_requested.connect(self.apply_value)
        layout.addWidget(self.editor, 1)
        self.error_label = QtWidgets.QLabel()
        self.error_label.setWordWrap(True)
        self.error_label.setTextFormat(QtCore.Qt.TextFormat.PlainText)
        self.error_label.setProperty("role", "warning-hint")
        layout.addWidget(self.error_label)
        self.buttons = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.StandardButton.Save | QtWidgets.QDialogButtonBox.StandardButton.Cancel)
        self.buttons.accepted.connect(self.apply_value)
        self.buttons.rejected.connect(self.reject)
        self.buttons.button(QtWidgets.QDialogButtonBox.StandardButton.Save).setEnabled(not field.isReadOnly())
        layout.addWidget(self.buttons)
        field.destroyed.connect(self.reject)

    def apply_value(self):
        value = self.editor.toPlainText()
        field = self.field
        if not field.isEnabled() or field.isReadOnly():
            self.error_label.setText("This field is currently locked. Cancel to keep its value.")
            return
        if field.text() != self.original_value:
            self.error_label.setText("The value changed while this editor was open. Cancel and reopen to edit the current value.")
            return
        if not field.allow_newlines and any(char in value for char in ("\n", "\r", "\u2028", "\u2029")):
            self.error_label.setText("This field stores a single value. Remove line breaks; automatic visual wrapping is supported.")
            return
        # QLineEdit counts UTF-16 units; reject oversize drafts rather than truncate.
        if len(value.encode("utf-16-le", errors="surrogatepass")) // 2 > field.maxLength():
            self.error_label.setText(f"This value exceeds the field's {field.maxLength()} character limit.")
            return
        validator = field.validator()
        if validator is not None and validator.validate(value, 0)[0] != QtGui.QValidator.State.Acceptable:
            self.error_label.setText("This value does not match the field's required format.")
            return
        if value != self.original_value:
            field.setText(value)
            field.textEdited.emit(value)
            field.editingFinished.emit()
        self.accept()


class ExpandableLineEdit(QtWidgets.QLineEdit):
    """Preserves QLineEdit APIs/signals used by recipes, settings and previews."""
    def __init__(self, text="", parent=None, *, title="Edit full value", allow_newlines=False, directory=False):
        super().__init__(text, parent)
        self.editor_title = title
        self.allow_newlines = allow_newlines
        self._field_widget = None
        self._preview = None
        self._purpose_tooltip = ""
        self._dialog = None
        icon = self.style().standardIcon(QtWidgets.QStyle.StandardPixmap.SP_TitleBarMaxButton)
        self.expand_action = self.addAction(icon, QtWidgets.QLineEdit.ActionPosition.TrailingPosition)
        self.expand_action.setToolTip("Expand to edit the full value (Alt+Enter)")
        self.expand_action.triggered.connect(self.open_editor)
        self._icon_count = 1
        self.browse_action = None
        if directory:
            icon = self.style().standardIcon(QtWidgets.QStyle.StandardPixmap.SP_DirOpenIcon)
            self.browse_action = self.addAction(icon, QtWidgets.QLineEdit.ActionPosition.LeadingPosition)
            self.browse_action.setToolTip("Choose a folder")
            self.browse_action.triggered.connect(self._browse_directory)
            self._icon_count += 1
        self.textChanged.connect(self._update_preview)

    def field_widget(self):
        """Place this input and its overflow preview in an existing form/row."""
        if self._field_widget is None:
            self._field_widget = QtWidgets.QWidget()
            self._field_widget.setMinimumWidth(0)
            self._field_widget.setSizePolicy(QtWidgets.QSizePolicy.Policy.Expanding, QtWidgets.QSizePolicy.Policy.Preferred)
            layout = QtWidgets.QVBoxLayout(self._field_widget)
            layout.setContentsMargins(0, 0, 0, 0)
            layout.setSpacing(2)
            layout.addWidget(self)
            self._preview = _WrappedPreview()
            self._preview.setAccessibleName(self.editor_title + " — full value preview")
            layout.addWidget(self._preview)
            self._update_preview()
        return self._field_widget

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._update_preview()

    def setToolTip(self, text):
        self._purpose_tooltip = str(text)
        self._update_preview()

    def setText(self, text):
        super().setText(text)
        # Settings restore can block signals on the original QLineEdit.
        self._update_preview()

    def _update_preview(self, *_):
        value = self.text() if self.echoMode() == QtWidgets.QLineEdit.EchoMode.Normal else ""
        tip = "\n".join(filter(None, (
            self._purpose_tooltip, "Expand to view/edit the full value (Alt+Enter)", value)))
        QtWidgets.QLineEdit.setToolTip(self, "<p>" + html.escape(tip).replace("\n", "<br>") + "</p>")
        if self._preview is None:
            return
        available = max(0, self.width() - self._icon_count * 24 - 16)
        overflow = "\n" in self.text() or self.fontMetrics().horizontalAdvance(self.displayText()) > available
        if self._preview.toPlainText() != value:
            self._preview.setPlainText(value)
        self._preview.setVisible(bool(value) and overflow)
        if overflow:
            self._preview.fit_height()

    def keyPressEvent(self, event):
        if (event.key() in (QtCore.Qt.Key.Key_Return, QtCore.Qt.Key.Key_Enter)
                and event.modifiers() & QtCore.Qt.KeyboardModifier.AltModifier):
            self.open_editor()
            event.accept()
            return
        super().keyPressEvent(event)

    def open_editor(self):
        if not self.isEnabled():
            return
        if self._dialog is not None:
            self._dialog.raise_()
            self._dialog.activateWindow()
            return
        dialog = ExpandedValueDialog(self)
        self._dialog = dialog
        try:
            dialog.exec()
        finally:
            self._dialog = None
            dialog.deleteLater()

    def _browse_directory(self):
        if not self.isEnabled() or self.isReadOnly():
            return
        original = self.text()
        directory = QtWidgets.QFileDialog.getExistingDirectory(self.window(), "Choose data root", original)
        if (directory and directory != original and self.text() == original
                and self.isEnabled() and not self.isReadOnly()):
            self.setText(directory)
            self.textEdited.emit(directory)
            self.editingFinished.emit()
