from __future__ import annotations

from typing import Iterable, List

from PyQt6 import QtWidgets

SAVED_PREFIX = "[saved] "


class ResourceComboBox(QtWidgets.QComboBox):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setEditable(True)
        self.setInsertPolicy(QtWidgets.QComboBox.InsertPolicy.NoInsert)

    def _strip_saved_prefix(self, value: str) -> str:
        return value[len(SAVED_PREFIX):] if value.startswith(SAVED_PREFIX) else value

    def current_address(self) -> str:
        return self._strip_saved_prefix(self.currentText().strip())

    def populate(self, items: Iterable[str], saved_value: str = ""):
        current_value = (saved_value or "").strip() or self.current_address()
        clean_items: List[str] = []
        seen = set()
        for item in items:
            item = (item or "").strip()
            if item and item not in seen:
                clean_items.append(item)
                seen.add(item)

        self.blockSignals(True)
        self.clear()

        if clean_items:
            if current_value and current_value not in seen:
                self.addItem(f"{SAVED_PREFIX}{current_value}")
                self.insertSeparator(self.count())
            self.addItems(clean_items)
        elif current_value:
            self.addItem(current_value)

        if current_value:
            if current_value in clean_items:
                self.setCurrentText(current_value)
            elif self.count():
                self.setCurrentIndex(0)
            else:
                self.setEditText(current_value)
        elif clean_items:
            self.setCurrentIndex(0)

        self.blockSignals(False)
