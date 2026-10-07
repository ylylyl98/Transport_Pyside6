"""Edit sample heater bands without changing instrument setup."""
from PySide6 import QtWidgets

from app.sample_heater_ranges import validate_heater_ranges
from app.ui.widgets.safe_combo import SafeComboBox


class SampleHeaterRangeDialog(QtWidgets.QDialog):
    def __init__(self, bands, on_save, parent=None):
        super().__init__(parent)
        self._on_save = on_save
        self.setWindowTitle("Automatic sample heater ranges")
        self.resize(520, 380)
        layout = QtWidgets.QVBoxLayout(self)
        self.use_instrument = QtWidgets.QCheckBox("Use commissioned LS335 Zone table (read only)")
        self.use_instrument.setChecked(not bands)
        layout.addWidget(self.use_instrument)
        note = QtWidgets.QLabel(
            "Custom bands select the first upper bound that covers the target. "
            "Enter limits established for your sample heater. Saving changes the application configuration; "
            "the instrument is commanded only by Apply temperature.")
        note.setWordWrap(True)
        layout.addWidget(note)
        self.table = QtWidgets.QTableWidget(0, 2)
        self.table.setHorizontalHeaderLabels(["Upper temperature (K)", "Heater range"])
        self.table.horizontalHeader().setSectionResizeMode(QtWidgets.QHeaderView.ResizeMode.Stretch)
        self.table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectionBehavior.SelectRows)
        layout.addWidget(self.table, 1)
        buttons = QtWidgets.QHBoxLayout()
        self.add_button = QtWidgets.QPushButton("Add band")
        self.remove_button = QtWidgets.QPushButton("Remove band")
        self.add_button.clicked.connect(lambda: self.add_band())
        self.remove_button.clicked.connect(self.remove_band)
        buttons.addWidget(self.add_button)
        buttons.addWidget(self.remove_button)
        buttons.addStretch()
        layout.addLayout(buttons)
        self.error_label = QtWidgets.QLabel()
        self.error_label.setWordWrap(True)
        layout.addWidget(self.error_label)
        actions = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.StandardButton.Save | QtWidgets.QDialogButtonBox.StandardButton.Cancel)
        actions.accepted.connect(self.save)
        actions.rejected.connect(self.reject)
        layout.addWidget(actions)
        for band in bands:
            self.add_band(band)
        self.use_instrument.toggled.connect(self.update_editing)
        self.update_editing()

    def add_band(self, band=None):
        if self.table.rowCount() >= 10:
            return
        row = self.table.rowCount()
        self.table.insertRow(row)
        band = band or {}
        self.table.setItem(row, 0, QtWidgets.QTableWidgetItem(str(band.get("upper_temperature_k", ""))))
        ranges = SafeComboBox()
        for label, value in (("OFF", 0), ("Low / voltage ON", 1), ("Medium", 2), ("High", 3)):
            ranges.addItem(label, value)
        ranges.setCurrentIndex(ranges.findData(band.get("heater_range", 0)))
        self.table.setCellWidget(row, 1, ranges)
        self.update_editing()

    def remove_band(self):
        row = self.table.currentRow()
        if row < 0:
            row = self.table.rowCount() - 1
        if row >= 0:
            self.table.removeRow(row)
        self.update_editing()

    def update_editing(self, *_):
        custom = not self.use_instrument.isChecked()
        self.table.setEnabled(custom)
        self.add_button.setEnabled(custom and self.table.rowCount() < 10)
        self.remove_button.setEnabled(custom and self.table.rowCount() > 0)

    def selected_bands(self):
        if self.use_instrument.isChecked():
            return []
        return validate_heater_ranges([
            {"upper_temperature_k": self.table.item(row, 0).text(),
             "heater_range": self.table.cellWidget(row, 1).currentData()}
            for row in range(self.table.rowCount())])

    def save(self):
        try:
            self._on_save(self.selected_bands())
        except Exception as exc:
            self.error_label.setText(str(exc))
            return
        self.accept()
