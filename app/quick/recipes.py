"""Virtualized condition-row model backed by MeasurementService parameters."""
from uuid import uuid4

from PySide6.QtCore import QAbstractListModel, QModelIndex, QObject, Property, Qt, Signal, Slot

from app.quick.parameters import descriptor
from app.quick.sampling import gate_sampling


class CellState(QObject):
    """Stable per-cell binding target; only changed values invalidate QML."""
    changed = Signal()

    def __init__(self, spec, parent):
        super().__init__(parent)
        self._spec = spec

    spec = Property("QVariantMap", lambda self: self._spec, notify=changed)

    def update(self, spec, *, notify=True):
        if spec != self._spec:
            self._spec = spec
            if notify:
                self.changed.emit()


class RecipeModel(QAbstractListModel):
    countChanged = Signal()
    selectedIndexChanged = Signal()
    UidRole = Qt.UserRole + 1
    NumberRole = Qt.UserRole + 2
    CellsRole = Qt.UserRole + 3

    def __init__(self, service, mode, key):
        super().__init__(service)
        self.service, self.mode, self.key = service, mode, key
        self._ids = [uuid4().hex for _ in self.rows]
        self._positions = {uid: i for i, uid in enumerate(self._ids)}
        self._drafts = {}
        self._flushing = False
        self._states = {}
        self._orders = {}

    @property
    def rows(self):
        return self.service._values[self.mode][self.key]

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self._ids)

    count = Property(int, lambda self: len(self._ids), notify=countChanged)
    selectedIndex = Property(int, lambda self: min(max(0, self.service._selected_rows[self.mode].get(self.key, 0)), len(self._ids)-1) if self._ids else -1, notify=selectedIndexChanged)

    def roleNames(self):
        return {self.UidRole: b"uid", self.NumberRole: b"number", self.CellsRole: b"cells"}

    def data(self, index, role=Qt.DisplayRole):
        if not index.isValid() or not 0 <= index.row() < len(self._ids):
            return None
        i = index.row()
        if role == self.UidRole:
            return self._ids[i]
        if role == self.NumberRole:
            return i + 1
        if role == self.CellsRole:
            return self._cell_states(i)
        return None

    def cells(self, i):
        from app.quick.parameters import bias2_keys
        row = self.rows[i]
        values = self.service._values[self.mode]
        overrides = {}
        if self.mode == 3:
            keys = ["enabled", "vtg", "vbg", "vds", "settle_s"]
            overrides["vds"] = {"readOnly": not values["use_vds"]}
        elif self.mode == 5:
            raw = self.service.transportCoordinates == "Raw"
            inputs, calculated = (["vtg", "vbg"], ["doping", "efield"]) if raw else (["doping", "efield"], ["vtg", "vbg"])
            keys = ["enabled", "name", *inputs, "vds", "vds_source"]
            if row["vds_source"] == "NI DAQ AO":
                keys.append("ao_channel")
            keys += ["settle_s", *calculated]
            for key in calculated:
                overrides[key] = {"readOnly": True}
            try:
                from app.gate_transform import derived_to_gates
                vtg, vbg = derived_to_gates(row["doping"], row["efield"], values["ratio"], values["ratio_target"])
                overrides.update(vtg={"value": vtg, "readOnly": not raw}, vbg={"value": vbg, "readOnly": not raw})
            except ValueError as exc:
                overrides.update(vtg={"value": "Unavailable", "readOnly": True, "error": str(exc)},
                                 vbg={"value": "Unavailable", "readOnly": True, "error": str(exc)})
        elif self.key == "regions":
            keys = [f"{axis}_{part}" for axis in ("vtg", "vbg") for part in ("start", "stop", "step")]
        else:
            keys = ["enabled", "name", "mode", "n_points", "sweep_both_ways"]
            if row["mode"] == "Raw":
                for axis in ("vtg", "vbg", "vds"):
                    if axis == "vds" and row["vds_source"] == "None":
                        continue
                    keys.extend([f"raw_{axis}_active", f"raw_{axis}_start", f"raw_{axis}_stop"])
                    label = {"vtg": "Vtg", "vbg": "Vbg", "vds": "Vds"}[axis]
                    overrides[f"raw_{axis}_active"] = {"label": label + " sweep"}
                    overrides[f"raw_{axis}_start"] = {"label": label + " start (V)"}
                    overrides[f"raw_{axis}_stop"] = {"label": label + " stop (V)", "readOnly": not row[f"raw_{axis}_active"]}
                for key, column in (("name", 0), ("mode", 1), ("n_points", 2)):
                    overrides.setdefault(key, {}).update(layoutRow=0, layoutColumn=column)
                overrides["sweep_both_ways"] = {"layoutRow": 1, "layoutColumn": 0, "layoutColumnSpan": 3}
                for axis_index, axis in enumerate(("vtg", "vbg", "vds")):
                    for column, part in enumerate(("active", "start", "stop")):
                        overrides.setdefault(f"raw_{axis}_{part}", {}).update(layoutRow=axis_index + 2, layoutColumn=column)
            else:
                keys += ["derived_axis", "derived_start", "derived_stop", "derived_fixed", "derived_ratio", "derived_ratio_target"]
                axis = row["derived_axis"]
                held = "E-field" if axis == "Doping" else "Doping"
                overrides["derived_axis"] = {"coordinate": axis}
                for part in ("start", "stop"):
                    overrides[f"derived_{part}"] = {"label": f"{axis} {part} (V)", "coordinate": axis}
                overrides["derived_fixed"] = {"label": f"Hold {held} (V)", "coordinate": held}
        if self.key != "regions":
            keys += [key for key in bias2_keys(row) if key in row]
        result = []
        for key in keys:
            item = descriptor(key, row[key], self.mode)
            item.update(readOnly=False, error=self.service._field_errors[self.mode].get((self.key, i, key), ""))
            item.update(overrides.get(key, {}))
            draft = self._drafts.get((self._ids[i], key))
            if draft is not None:
                item["draft"] = draft
            result.append(item)
        if self.mode == 4 or self.key == "regions":
            invalid = any(scope == self.key and index == i and (key == "n_points" or key.startswith(("raw_", "derived_", "vtg_", "vbg_")))
                          for scope, index, key in self.service._field_errors[self.mode])
            warning = invalid
            if invalid:
                text = "Sampling unavailable: correct the highlighted range fields"
            elif self.mode == 4:
                text = gate_sampling(row)
            else:
                lines = []
                for axis in ("vtg", "vbg"):
                    detail, has_warning = self.service._sampling.step(row[axis + "_start"], row[axis + "_stop"], row[axis + "_step"], policy="region")
                    lines.append(axis.replace("vtg", "Vtg").replace("vbg", "Vbg") + " · " + detail)
                    warning = warning or has_warning
                text = "\n".join(lines)
            result[0].update(samplingText=text, samplingWarning=warning)
        return result

    def position(self, uid):
        return self._positions.get(uid, -1)

    @Slot(str)
    def select(self, uid):
        row = self.position(uid)
        if row >= 0 and self.mode == self.service._mode:
            self.service.selectRow(self.key, row)

    @Slot(str, str, str)
    def rememberDraft(self, uid, key, text):
        if self.mode == self.service._mode and not self.service.active and self.position(uid) >= 0:
            self._drafts[(uid, key)] = text
            # The editor already displays this text. Preserve it for a recycled
            # delegate without invalidating any live binding on each keystroke.
            state = self._states.get(uid, {}).get(key)
            if state is not None:
                state.update({**state.spec, "draft": text}, notify=False)

    @Slot(str, str, "QVariant", result=bool)
    def commit(self, uid, key, value):
        row = self.position(uid)
        if row < 0 or self.mode != self.service._mode or self.service.active:
            return False
        cell = next((c for c in self.cells(row) if c["key"] == key), None)
        if cell is None or cell.get("readOnly"):
            return False
        changes_shape = self.mode == 4 and (key in {"mode", "vds_source"} or
            (key.startswith("raw_") and key.endswith("_active")))
        changes_shape = changes_shape or (self.mode == 5 and key == "vds_source") or key == "bias2_mode"
        if changes_shape and value != cell["value"] and not self.flush():
            # Native checkboxes/combos have already changed their visual state.
            # Notify even when the accepted descriptor itself is unchanged.
            state = self._states.get(uid, {}).get(key)
            if state is not None:
                state.changed.emit()
            return False
        if self.service._set_recipe_cell(self.key, row, key, value, notify_model=False):
            self._drafts.pop((uid, key), None)
            self.notify_row(row)
            return True
        return False

    @Slot(result=bool)
    def flush(self):
        if self.mode != self.service._mode:
            return not self._drafts
        if self._flushing:
            return False
        self._flushing = True
        try:
            for (uid, key), value in list(self._drafts.items()):
                if not self.commit(uid, key, value):
                    return False
            return True
        finally:
            self._flushing = False

    def _cell_states(self, row):
        uid = self._ids[row]
        specs = self.cells(row)
        states = self._states.setdefault(uid, {})
        for spec in specs:
            key = spec["key"]
            if key not in states:
                states[key] = CellState(spec, self)
            else:
                states[key].update(spec)
        self._orders[uid] = tuple(spec["key"] for spec in specs)
        return [states[spec["key"]] for spec in specs]

    def notify_row(self, row):
        if not 0 <= row < len(self._ids):
            return
        uid = self._ids[row]
        # A row that has never been displayed has no bindings to notify.
        if uid not in self._states:
            return
        old_order = self._orders[uid]
        self._cell_states(row)
        if self._orders[uid] != old_order:
            idx = self.index(row, 0)
            self.dataChanged.emit(idx, idx, [self.CellsRole])

    def notify_all(self):
        for uid in tuple(self._states):
            row = self.position(uid)
            if row >= 0:
                self.notify_row(row)

    def _discard_states(self, uid):
        for state in self._states.pop(uid, {}).values():
            state.deleteLater()
        self._orders.pop(uid, None)

    def reset_rows(self, rows):
        old_uids = tuple(self._states)
        self.beginResetModel()
        self.service._values[self.mode][self.key] = rows
        self._ids = [uuid4().hex for _ in self.rows]
        self._positions = {uid: i for i, uid in enumerate(self._ids)}
        self._drafts.clear()
        self.endResetModel()
        for uid in old_uids:
            self._discard_states(uid)
        self.countChanged.emit()
        self.selectedIndexChanged.emit()

    def insert(self, index, row):
        self.beginInsertRows(QModelIndex(), index, index)
        self.rows.insert(index, row)
        self._ids.insert(index, uuid4().hex)
        self._positions = {uid: i for i, uid in enumerate(self._ids)}
        self.endInsertRows()
        self.countChanged.emit()
        if index + 1 < len(self._ids):
            self.dataChanged.emit(self.index(index + 1, 0), self.index(len(self._ids)-1, 0), [self.NumberRole])

    def remove(self, index):
        self.beginRemoveRows(QModelIndex(), index, index)
        uid = self._ids.pop(index)
        self.rows.pop(index)
        self._positions = {uid: i for i, uid in enumerate(self._ids)}
        self._drafts = {key: value for key, value in self._drafts.items() if key[0] != uid}
        self.endRemoveRows()
        self._discard_states(uid)
        self.countChanged.emit()
        if index < len(self._ids):
            self.dataChanged.emit(self.index(index, 0), self.index(len(self._ids)-1, 0), [self.NumberRole])
