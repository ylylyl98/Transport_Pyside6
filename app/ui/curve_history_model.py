"""Qt model/view adapters for read-only measurement history."""
from PySide6 import QtCore
from app.curve_history import MEASUREMENT_TYPES


class HistoryModel(QtCore.QAbstractTableModel):
    selection_changed = QtCore.Signal()
    HEADERS = ("Compare", "Time", "Scan", "Conditions", "Status", "File")

    def __init__(self, parent=None):
        super().__init__(parent)
        self.records = []
        self.checked = set()

    def rowCount(self, parent=QtCore.QModelIndex()):
        return 0 if parent.isValid() else len(self.records)

    def columnCount(self, parent=QtCore.QModelIndex()):
        return 0 if parent.isValid() else len(self.HEADERS)

    def headerData(self, section, orientation, role=QtCore.Qt.ItemDataRole.DisplayRole):
        if orientation == QtCore.Qt.Orientation.Horizontal and role == QtCore.Qt.ItemDataRole.DisplayRole:
            return self.HEADERS[section]
        return super().headerData(section, orientation, role)

    def data(self, index, role=QtCore.Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        record = self.records[index.row()]
        if role == QtCore.Qt.ItemDataRole.CheckStateRole and index.column() == 0:
            return QtCore.Qt.CheckState.Checked if str(record.path) in self.checked else QtCore.Qt.CheckState.Unchecked
        if role == QtCore.Qt.ItemDataRole.ToolTipRole:
            return f"{record.path}\n{record.conditions}\nStatus: {record.status}"
        if role == QtCore.Qt.ItemDataRole.DisplayRole:
            names = MEASUREMENT_TYPES
            stamp = record.created_at.replace("T", " ")[:19]
            title = f"{stamp}\n{record.path.name}\n{record.conditions}\nStatus: {record.status}"
            return (title, stamp, names.get(record.measurement, record.measurement), record.conditions, record.status, record.path.name)[index.column()]
        return None

    def flags(self, index):
        flags = super().flags(index)
        if index.isValid() and index.column() == 0:
            flags |= QtCore.Qt.ItemFlag.ItemIsUserCheckable
        return flags

    def setData(self, index, value, role=QtCore.Qt.ItemDataRole.EditRole):
        if not index.isValid() or index.column() != 0 or role != QtCore.Qt.ItemDataRole.CheckStateRole:
            return False
        path = str(self.records[index.row()].path)
        if value in (QtCore.Qt.CheckState.Checked, QtCore.Qt.CheckState.Checked.value):
            self.checked.add(path)
        else:
            self.checked.discard(path)
        self.dataChanged.emit(index, index, [role])
        self.selection_changed.emit()
        return True

    def replace(self, records):
        if len(records) == len(self.records) and all(old.path == new.path for old, new in zip(self.records, records)):
            previous = self.records
            self.records = records
            for row, (old, new) in enumerate(zip(previous, records)):
                # A growing CSV changes its fingerprint, not its displayed
                # text. Keep indexes, scroll position and cached row geometry.
                if (old.created_at, old.measurement, old.conditions, old.status) != (new.created_at, new.measurement, new.conditions, new.status):
                    self.dataChanged.emit(self.index(row, 0), self.index(row, len(self.HEADERS) - 1),
                                          [QtCore.Qt.ItemDataRole.DisplayRole, QtCore.Qt.ItemDataRole.ToolTipRole])
            return
        self.beginResetModel()
        self.records = records
        self.endResetModel()


class HistoryFilter(QtCore.QSortFilterProxyModel):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.date = "All dates"
        self.kind = "vds_sweep"
        self.search = ""

    def filterAcceptsRow(self, row, parent):
        record = self.sourceModel().records[row]
        wanted_date = QtCore.QDate.currentDate().toString("yyyy-MM-dd") if self.date == "Today" else self.date
        if self.date != "All dates" and record.date != wanted_date:
            return False
        if record.measurement != self.kind:
            return False
        haystack = f"{record.path.name} {record.conditions} {record.status} {record.created_at}".casefold()
        return self.search.casefold() in haystack
