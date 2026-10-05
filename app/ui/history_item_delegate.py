"""Wrap history text while retaining Qt's native row and checkbox rendering."""
from functools import lru_cache

from PySide6 import QtCore, QtGui, QtWidgets


@lru_cache(maxsize=2048)
def _wrapped_text(text, font_description, width):
    font = QtGui.QFont()
    font.fromString(font_description)
    lines = []
    # QStyledItemDelegate normalizes model newlines to Unicode line separators.
    for paragraph in text.replace("\u2028", "\n").split("\n"):
        if not paragraph:
            lines.append("")
            continue
        # Zero-width break opportunities keep filename components together.
        # An individual component can still wrap when wider than the sidebar.
        paragraph = paragraph.replace("_", "_\u200b")
        encoded = paragraph.encode("utf-16-le")
        layout = QtGui.QTextLayout(paragraph, font)
        options = QtGui.QTextOption()
        options.setWrapMode(QtGui.QTextOption.WrapMode.WrapAtWordBoundaryOrAnywhere)
        layout.setTextOption(options)
        layout.beginLayout()
        while True:
            line = layout.createLine()
            if not line.isValid():
                break
            line.setLineWidth(width)
            # QTextLayout positions use UTF-16 units, including for emoji.
            start, end = line.textStart(), line.textStart() + line.textLength()
            lines.append(encoded[start * 2:end * 2].decode("utf-16-le").replace("\u200b", ""))
        layout.endLayout()
    return "\u2028".join(lines)


class HistoryItemDelegate(QtWidgets.QStyledItemDelegate):
    def initStyleOption(self, option, index):
        super().initStyleOption(option, index)
        view = self.parent()
        style = view.style()
        geometry = QtWidgets.QStyleOptionViewItem(option)
        width = max(1, view.viewport().width() - 2 * view.spacing())
        geometry.rect = QtCore.QRect(0, 0, width, max(1, option.fontMetrics.height()))
        text_rect = style.subElementRect(QtWidgets.QStyle.SubElement.SE_ItemViewItemText, geometry, view)
        margin = style.pixelMetric(QtWidgets.QStyle.PixelMetric.PM_FocusFrameHMargin, option, view) + 1
        text_width = max(1, text_rect.width() - 2 * margin)
        option.text = _wrapped_text(option.text, option.font.toString(), text_width)
        option.textElideMode = QtCore.Qt.TextElideMode.ElideNone
        # sizeHint and paint both use these explicit line breaks. The native
        # delegate still owns selected/disabled/focus states and checkbox input.
        option.features &= ~QtWidgets.QStyleOptionViewItem.ViewItemFeature.WrapText
