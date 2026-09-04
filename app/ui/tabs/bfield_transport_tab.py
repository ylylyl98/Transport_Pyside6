"""Continuous B-field transport sweep editor.

This tab intentionally has its own recipe and rate controls.  Gate Scan's
trajectory editor remains available for its original workflow; transport rows
are fixed Doping/E-field operating points held while APS100 traverses B.
"""

from __future__ import annotations

from copy import deepcopy
import json

from PyQt6 import QtCore, QtWidgets

from app.engine.bfield_transport_sweep import (
    COIL_CONSTANT_T_PER_A,
    MAX_DRIVEN_FIELD_T,
    MAX_RATE_A_PER_S,
    MAX_RATE_T_PER_MIN,
    COOLDOWN_POLICIES,
    COOLDOWN_POLICY_LABELS,
    estimate_transport_times,
    normalize_cooldown_policy,
    BFieldTransportSafetyError,
    rate_t_per_min_to_a_per_s,
    validate_setup,
)
from app.gate_transform import derived_to_gates
from app.gate_transform import RATIO_TARGET_VBG, RATIO_TARGET_VTG, ratio_formula_text, normalize_ratio_target
from app.models import BFieldTransportCondition, BFieldTransportParams, Connections, SaveRoot
from app.run_output import build_planned_output
from app.settings import get_app_settings
from app.ui.helpers import set_standard_input_height, style_form_layout
from app.ui.tabs.base_tab import BaseMeasurementTab
from app.ui.widgets.safe_combo import SafeComboBox
from app.ui.widgets.safe_spinbox import SafeDoubleSpinBox, SafeSpinBox
from app.ui.widgets.status_panel import SectionHeader, StatusPanel
from utils.config import cfg


class BFieldTransportTab(BaseMeasurementTab):
    SETTINGS_PREFIX = "tabs/bfield_transport"

    def __init__(self, save: SaveRoot, conns: Connections, device_manager, **_kwargs):
        self.save = save
        self.conns = conns
        self.device_manager = device_manager
        self.get_global_rates = _kwargs.get("get_global_rates_callable") or (lambda: (1e7, 100.0))
        self.get_signal_chain = _kwargs.get("get_signal_chain_callable") or (lambda: None)
        self.params = BFieldTransportParams()
        self.conditions = [BFieldTransportCondition()]
        self._locked = False
        self._execution_controller = None
        super().__init__("START B-FIELD SWEEP", "B-field (T)", "Ids (A)", ["g1", "g2", "g3", "daq"])
        self._load_settings()
        self._refresh_conditions()
        self.btn_start.clicked.connect(self.start_run)
        self.btn_stop.clicked.connect(self.stop_run)

    @staticmethod
    def _spin(value=0.0, minimum=-1000.0, maximum=1000.0, decimals=6):
        spin = SafeDoubleSpinBox()
        spin.setRange(minimum, maximum)
        spin.setDecimals(decimals)
        spin.setValue(value)
        set_standard_input_height(spin)
        return spin

    def _build_control_panel(self, ctl_layout):
        ctl_layout.addWidget(SectionHeader("B-field trajectory"))
        group = QtWidgets.QGroupBox("Driven-mode sweep")
        form = QtWidgets.QFormLayout(group)
        style_form_layout(form)
        self.sp_start = self._spin(-0.5, -MAX_DRIVEN_FIELD_T, MAX_DRIVEN_FIELD_T)
        self.sp_stop = self._spin(0.5, -MAX_DRIVEN_FIELD_T, MAX_DRIVEN_FIELD_T)
        self.sp_rate = self._spin(0.1, 1e-6, MAX_RATE_T_PER_MIN, 6)
        self.sp_rate_a = QtWidgets.QLabel()
        self.sp_rate_t_s = QtWidgets.QLabel()
        self.chk_round_trip = QtWidgets.QCheckBox("Round trip: start → stop → start")
        self.chk_round_trip.setChecked(True)
        form.addRow("Start (T):", self.sp_start)
        form.addRow("Stop (T):", self.sp_stop)
        form.addRow("Sweep rate (T/min):", self.sp_rate)
        form.addRow("APS100 rate:", self.sp_rate_a)
        form.addRow("Field rate:", self.sp_rate_t_s)
        form.addRow("", self.chk_round_trip)
        self.lbl_rate_limit = QtWidgets.QLabel(
            f"Hard limit: {MAX_RATE_T_PER_MIN:.8f} T/min ({MAX_RATE_A_PER_S:.5f} A/s); "
            f"driven field envelope ±{MAX_DRIVEN_FIELD_T:g} T"
        )
        self.lbl_rate_limit.setWordWrap(True)
        self.lbl_rate_limit.setProperty("role", "hint")
        form.addRow("", self.lbl_rate_limit)
        self.cbo_cooldown_policy = SafeComboBox()
        for policy in COOLDOWN_POLICIES:
            self.cbo_cooldown_policy.addItem(COOLDOWN_POLICY_LABELS[policy], policy)
        self.cbo_cooldown_policy.setToolTip(
            "Adaptive pauses only when Lake Shore permission requires recovery; "
            "Stay driven holds the heater on across rows; Persistent after every row "
            "performs an acknowledged persistent cooldown between rows."
        )
        form.addRow("Between-row thermal policy:", self.cbo_cooldown_policy)
        self.sp_ratio = SafeDoubleSpinBox()
        self.sp_ratio.setRange(-1e4, 1e4)
        self.sp_ratio.setDecimals(4)
        self.sp_ratio.setValue(self.params.ratio)
        self.cbo_ratio_target = SafeComboBox()
        self.cbo_ratio_target.addItem("Top gate: r × Vtg", RATIO_TARGET_VTG)
        self.cbo_ratio_target.addItem("Bottom gate: r × Vbg", RATIO_TARGET_VBG)
        self.lbl_ratio_formula = QtWidgets.QLabel()
        self.lbl_ratio_formula.setWordWrap(True)
        self.lbl_ratio_formula.setProperty("role", "hint")
        form.addRow("Gate weighting ratio, r:", self.sp_ratio)
        form.addRow("Multiply by r:", self.cbo_ratio_target)
        form.addRow("Active equations:", self.lbl_ratio_formula)
        ctl_layout.addWidget(group)

        ctl_layout.addWidget(SectionHeader("Gate conditions for each B-field sweep"))
        intro = QtWidgets.QLabel(
            "Sweep the magnetic field while measuring current at fixed doping and E-field conditions. "
            "Each row fixes both doping and E-field for one complete trajectory; "
            "Vtg/Vbg are calculated from the global gate weighting ratio."
        )
        intro.setWordWrap(True)
        intro.setProperty("role", "hint")
        ctl_layout.addWidget(intro)
        self.condition_table = QtWidgets.QTableWidget(0, 9)
        self.condition_table.setHorizontalHeaderLabels(
            ["", "Name", "Doping", "E-field", "Vds", "Source", "AO", "Vtg", "Vbg"]
        )
        self.condition_table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectionBehavior.SelectRows)
        self.condition_table.setSelectionMode(QtWidgets.QAbstractItemView.SelectionMode.SingleSelection)
        self.condition_table.setMinimumHeight(150)
        self.condition_table.setMaximumHeight(330)
        self.condition_table.verticalHeader().setVisible(False)
        self.condition_table.horizontalHeader().setStretchLastSection(True)
        self.condition_table.itemChanged.connect(self._condition_edited)
        ctl_layout.addWidget(self.condition_table)
        row = QtWidgets.QHBoxLayout()
        self.btn_condition_add = QtWidgets.QPushButton("Add")
        self.btn_condition_update = QtWidgets.QPushButton("Update selected")
        self.btn_condition_duplicate = QtWidgets.QPushButton("Duplicate")
        self.btn_condition_remove = QtWidgets.QPushButton("Remove")
        self.btn_condition_up = QtWidgets.QPushButton("↑")
        self.btn_condition_down = QtWidgets.QPushButton("↓")
        for button in (self.btn_condition_add, self.btn_condition_update, self.btn_condition_duplicate, self.btn_condition_remove, self.btn_condition_up, self.btn_condition_down):
            row.addWidget(button)
        ctl_layout.addLayout(row)
        self.btn_condition_add.clicked.connect(self._add_condition)
        self.btn_condition_update.clicked.connect(self._update_selected)
        self.btn_condition_duplicate.clicked.connect(self._duplicate_condition)
        self.btn_condition_remove.clicked.connect(self._remove_condition)
        self.btn_condition_up.clicked.connect(lambda: self._move_condition(-1))
        self.btn_condition_down.clicked.connect(lambda: self._move_condition(1))

        ctl_layout.addWidget(SectionHeader("Acquisition / output"))
        acq = QtWidgets.QGroupBox("Acquisition")
        acq_form = QtWidgets.QFormLayout(acq)
        style_form_layout(acq_form)
        self.sp_delay = self._spin(0.1, 0.0, 60.0, 3)
        self.sp_averages = SafeSpinBox()
        self.sp_averages.setRange(1, 10000)
        self.sp_averages.setValue(1)
        self.ed_base = QtWidgets.QLineEdit(self.params.base_name)
        acq_form.addRow("Delay (s):", self.sp_delay)
        acq_form.addRow("Averages:", self.sp_averages)
        acq_form.addRow("Filename stem:", self.ed_base)
        ctl_layout.addWidget(acq)
        self.lbl_preview = QtWidgets.QLabel()
        self.lbl_preview.setWordWrap(True)
        self.lbl_preview.setProperty("role", "hint")
        ctl_layout.addWidget(self.lbl_preview)
        self.lbl_time_estimate = QtWidgets.QLabel()
        self.lbl_time_estimate.setWordWrap(True)
        self.lbl_time_estimate.setProperty("role", "hint")
        ctl_layout.addWidget(self.lbl_time_estimate)

        self.lbl_connection_hint = QtWidgets.QLabel("APS100 and measurement sessions must be connected before starting.")
        self.lbl_connection_hint.setWordWrap(True)
        self.lbl_connection_hint.setProperty("role", "hint")
        ctl_layout.addWidget(self.lbl_connection_hint)
        self.status_panel = StatusPanel(["g1", "g2", "g3", "daq"])
        ctl_layout.addWidget(self.status_panel)
        self.sp_start.valueChanged.connect(self._refresh_rate_preview)
        self.sp_stop.valueChanged.connect(self._refresh_rate_preview)
        self.sp_rate.valueChanged.connect(self._refresh_rate_preview)
        self.sp_ratio.valueChanged.connect(self._refresh_ratio_preview)
        self.cbo_ratio_target.currentIndexChanged.connect(self._refresh_ratio_preview)
        self.cbo_cooldown_policy.currentIndexChanged.connect(self._refresh_rate_preview)
        self._refresh_rate_preview()
        self._refresh_ratio_preview()

    def _load_settings(self):
        settings = get_app_settings()
        settings.beginGroup(self.SETTINGS_PREFIX)
        self.sp_start.setValue(float(settings.value("start_field_t", self.params.start_field_t)))
        self.sp_stop.setValue(float(settings.value("stop_field_t", self.params.stop_field_t)))
        self.sp_rate.setValue(float(settings.value("rate_t_per_min", self.params.rate_t_per_min)))
        has_global_ratio = settings.contains("ratio")
        if has_global_ratio:
            self.sp_ratio.setValue(float(settings.value("ratio", self.params.ratio)))
        try:
            global_target = normalize_ratio_target(str(settings.value("ratio_target", self.params.ratio_target)))
        except ValueError:
            global_target = self.params.ratio_target
        self.cbo_ratio_target.setCurrentIndex(max(0, self.cbo_ratio_target.findData(global_target)))
        self.chk_round_trip.setChecked(settings.value("round_trip", True, type=bool))
        try:
            policy = normalize_cooldown_policy(str(settings.value("cooldown_policy", "adaptive")))
        except ValueError:
            policy = "adaptive"
        self.cbo_cooldown_policy.setCurrentIndex(max(0, self.cbo_cooldown_policy.findData(policy)))
        self.sp_delay.setValue(float(settings.value("delay_s", self.params.acquisition_delay_s)))
        self.sp_averages.setValue(int(settings.value("averages", self.params.averages)))
        self.ed_base.setText(str(settings.value("base_name", self.params.base_name)))
        raw_conditions = settings.value("conditions", "")
        if raw_conditions:
            try:
                values = json.loads(str(raw_conditions))
                loaded = [BFieldTransportCondition(**dict(item)) for item in values]
                if loaded:
                    self.conditions = loaded
                    # Migrate legacy per-row ratio fields once.  A persisted
                    # global value wins; otherwise the first row is the only
                    # permitted source and all rows are normalized to it.
                    if not has_global_ratio:
                        self.sp_ratio.setValue(float(loaded[0].ratio))
                        try:
                            target = normalize_ratio_target(loaded[0].ratio_target)
                            self.cbo_ratio_target.setCurrentIndex(max(0, self.cbo_ratio_target.findData(target)))
                        except ValueError:
                            pass
            except (TypeError, ValueError, json.JSONDecodeError):
                pass
        settings.endGroup()

    def _save_settings(self):
        settings = get_app_settings()
        settings.beginGroup(self.SETTINGS_PREFIX)
        settings.setValue("start_field_t", self.sp_start.value())
        settings.setValue("stop_field_t", self.sp_stop.value())
        settings.setValue("rate_t_per_min", self.sp_rate.value())
        settings.setValue("ratio", self.sp_ratio.value())
        settings.setValue("ratio_target", self.cbo_ratio_target.currentData() or RATIO_TARGET_VBG)
        settings.setValue("round_trip", self.chk_round_trip.isChecked())
        settings.setValue("cooldown_policy", self.cbo_cooldown_policy.currentData() or "adaptive")
        settings.setValue("delay_s", self.sp_delay.value())
        settings.setValue("averages", self.sp_averages.value())
        settings.setValue("base_name", self.ed_base.text())
        settings.setValue("conditions", json.dumps([condition.__dict__ for condition in self.conditions]))
        settings.endGroup()

    @staticmethod
    def _condition_values(condition):
        condition.refresh_gates()
        return ["✓" if condition.enabled else "", condition.name, f"{condition.doping:g}", f"{condition.efield:g}", f"{condition.vds:g}", condition.vds_source, str(condition.ao_channel), f"{condition.vtg:g}", f"{condition.vbg:g}"]

    def _apply_global_ratio(self):
        ratio = float(self.sp_ratio.value())
        target = normalize_ratio_target(self.cbo_ratio_target.currentData() or RATIO_TARGET_VBG)
        for condition in self.conditions:
            condition.ratio = ratio
            condition.ratio_target = target
            condition.refresh_gates()

    def _refresh_ratio_preview(self, *_args):
        try:
            target = normalize_ratio_target(self.cbo_ratio_target.currentData() or RATIO_TARGET_VBG)
            ratio = float(self.sp_ratio.value())
            formula = ratio_formula_text(target).replace("r", f"{ratio:g}")
            self.lbl_ratio_formula.setText(formula)
            self._apply_global_ratio()
            if hasattr(self, "condition_table"):
                self._refresh_conditions()
        except Exception as exc:
            self.lbl_ratio_formula.setText(f"Invalid ratio: {exc}")

    def _refresh_conditions(self):
        self._apply_global_ratio()
        self.condition_table.blockSignals(True)
        try:
            self.condition_table.setRowCount(len(self.conditions))
            for row, condition in enumerate(self.conditions):
                for column, value in enumerate(self._condition_values(condition)):
                    item = QtWidgets.QTableWidgetItem(value)
                    if column == 0:
                        item.setFlags(item.flags() | QtCore.Qt.ItemFlag.ItemIsUserCheckable)
                        item.setCheckState(QtCore.Qt.CheckState.Checked if condition.enabled else QtCore.Qt.CheckState.Unchecked)
                    if column in (7, 8):
                        item.setFlags(item.flags() & ~QtCore.Qt.ItemFlag.ItemIsEditable)
                    if column == 0:
                        item.setTextAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
                    self.condition_table.setItem(row, column, item)
        finally:
            self.condition_table.blockSignals(False)
        self._update_condition_buttons()
        self.refresh_output_preview()

    def _condition_from_row(self, row):
        def text(col):
            item = self.condition_table.item(row, col)
            return item.text().strip() if item else ""
        try:
            condition = BFieldTransportCondition(
                name=text(1) or f"Condition {row + 1}", doping=float(text(2)), efield=float(text(3)),
                ratio=self.sp_ratio.value(), ratio_target=self.cbo_ratio_target.currentData() or RATIO_TARGET_VBG,
                vds=float(text(4)), vds_source=text(5) or "Keithley 2400", ao_channel=int(text(6) or 0),
                enabled=(self.condition_table.item(row, 0).checkState() == QtCore.Qt.CheckState.Checked),
            )
            condition.refresh_gates()
            return condition
        except (TypeError, ValueError) as exc:
            raise BFieldTransportSafetyError(f"Condition row {row + 1}: {exc}") from exc

    def _collect_conditions(self):
        return [self._condition_from_row(row) for row in range(self.condition_table.rowCount())]

    def _condition_edited(self, item):
        if item.column() in (2, 3, 4, 5):
            try:
                condition = self._condition_from_row(item.row())
                self.condition_table.blockSignals(True)
                self.condition_table.item(item.row(), 7).setText(f"{condition.vtg:g}")
                self.condition_table.item(item.row(), 8).setText(f"{condition.vbg:g}")
                self.condition_table.blockSignals(False)
            except Exception:
                pass
        self._update_condition_buttons()

    def _selected_row(self):
        selected = self.condition_table.selectionModel().selectedRows()
        return selected[0].row() if selected else -1

    def _update_condition_buttons(self):
        row = self._selected_row()
        enabled = row >= 0
        for button in (self.btn_condition_update, self.btn_condition_duplicate, self.btn_condition_remove, self.btn_condition_up, self.btn_condition_down):
            button.setEnabled(enabled)
        self.btn_condition_remove.setEnabled(len(self.conditions) > 1 and enabled)

    def _add_condition(self):
        self.conditions.append(BFieldTransportCondition(name=f"Condition {len(self.conditions) + 1}"))
        self._refresh_conditions()
        self.condition_table.selectRow(len(self.conditions) - 1)

    def _update_selected(self):
        row = self._selected_row()
        if row >= 0:
            try:
                self.conditions[row] = self._condition_from_row(row)
                self._refresh_conditions()
                self.condition_table.selectRow(row)
            except ValueError as exc:
                self.lbl_preview.setText(str(exc))

    def _duplicate_condition(self):
        row = self._selected_row()
        if row < 0:
            return
        self.conditions.insert(row + 1, deepcopy(self.conditions[row]))
        self.conditions[row + 1].name += " copy"
        self._refresh_conditions()
        self.condition_table.selectRow(row + 1)

    def _remove_condition(self):
        row = self._selected_row()
        if len(self.conditions) > 1 and row >= 0:
            self.conditions.pop(row)
            self._refresh_conditions()
            self.condition_table.selectRow(min(row, len(self.conditions) - 1))

    def _move_condition(self, delta):
        row = self._selected_row()
        target = row + int(delta)
        if not (0 <= row < len(self.conditions) and 0 <= target < len(self.conditions)):
            return
        self.conditions[row], self.conditions[target] = self.conditions[target], self.conditions[row]
        self._refresh_conditions()
        self.condition_table.selectRow(target)

    def _refresh_rate_preview(self, *_args):
        try:
            value = rate_t_per_min_to_a_per_s(self.sp_rate.value())
            self.sp_rate_a.setText(f"{value:.7f} A/s")
            self.sp_rate_t_s.setText(f"{self.sp_rate.value() / 60.0:.8f} T/s")
            validate_setup(self.sp_start.value(), self.sp_stop.value(), self.sp_rate.value())
            planned = build_planned_output(
                self.save, "bfield_transport", self.ed_base.text().strip() or "bfield_transport",
                run_id="preview", create_dir=False,
            )
            enabled = sum(1 for condition in self.conditions if condition.enabled)
            self.lbl_preview.setText(
                "Ready: " + ("round trip" if self.chk_round_trip.isChecked() else "one-way")
                + f" {self.sp_start.value():+.4g} → {self.sp_stop.value():+.4g} T; "
                + f"{enabled} enabled condition(s); output preview: {planned.output_dir}"
            )
            self.lbl_preview.setProperty("role", "hint")
            enabled_conditions = [condition for condition in self.conditions if condition.enabled]
            estimate = estimate_transport_times(
                self.sp_start.value(), self.sp_stop.value(), self.sp_rate.value(), len(enabled_conditions),
                round_trip=self.chk_round_trip.isChecked(),
                bias_settle_s=max((condition.settle_s for condition in enabled_conditions), default=0.5),
                cooldown_policy=self.cbo_cooldown_policy.currentData() or "adaptive",
                heater_cool_s=getattr(cfg.magnet, "heater_cool_s", None),
                recovery_dwell_s=getattr(cfg.lakeshore335, "required_stable_recovery_dwell_s", None),
                heater_warm_s=getattr(cfg.magnet, "heater_warm_s", None),
            )
            thermal = (
                f"; fixed thermal overhead {estimate['fixed_thermal_extra_s']:.1f} s"
                + (f"; field return/positioning {estimate['field_positioning_extra_s']:.1f} s" if estimate["field_positioning_extra_s"] else "")
                + (" + variable/unknown recovery" if estimate["thermal_recovery_variable"] else "")
            ) if (self.cbo_cooldown_policy.currentData() == "persistent_each_row" and len(enabled_conditions) > 1) else (
                "; adaptive thermal recovery is variable/extra" if estimate["thermal_recovery_variable"] else ""
            )
            self.lbl_time_estimate.setText(
                f"Estimate: {estimate['sweep_s_per_row']:.1f} s sweep/row; "
                f"{estimate['batch_s']:.1f} s deterministic batch{thermal}."
            )
        except Exception as exc:
            self.lbl_preview.setText(f"Invalid sweep: {exc}")
            self.lbl_preview.setProperty("role", "warning-hint")
            if hasattr(self, "lbl_time_estimate"):
                self.lbl_time_estimate.setText("")

    def collect_params(self):
        self.conditions = self._collect_conditions()
        self._apply_global_ratio()
        self.params = BFieldTransportParams(
            base_name=self.ed_base.text().strip() or "bfield_transport",
            start_field_t=self.sp_start.value(), stop_field_t=self.sp_stop.value(),
            rate_t_per_min=self.sp_rate.value(), round_trip=self.chk_round_trip.isChecked(),
            ratio=float(self.sp_ratio.value()),
            ratio_target=normalize_ratio_target(self.cbo_ratio_target.currentData() or RATIO_TARGET_VBG),
            cooldown_policy=normalize_cooldown_policy(self.cbo_cooldown_policy.currentData() or "adaptive"),
            acquisition_delay_s=self.sp_delay.value(), averages=self.sp_averages.value(),
            conditions=deepcopy(self.conditions),
        )
        self._save_settings()
        return deepcopy(self.params)

    def validate_setup(self):
        params = self.collect_params()
        result = validate_setup(params.start_field_t, params.stop_field_t, params.rate_t_per_min)
        if not any(condition.enabled for condition in params.conditions):
            raise BFieldTransportSafetyError("Enable at least one fixed transport condition")
        return result

    def refresh_output_preview(self):
        """Refresh the lightweight preview used by MainWindow save updates."""
        if hasattr(self, "lbl_preview"):
            self._refresh_rate_preview()

    def set_sweep_locked(self, locked: bool):
        self._locked = bool(locked)
        for widget in (self.sp_start, self.sp_stop, self.sp_rate, self.chk_round_trip, self.condition_table,
                       self.btn_condition_add, self.btn_condition_update, self.btn_condition_duplicate,
                       self.btn_condition_remove, self.btn_condition_up, self.btn_condition_down):
            widget.setEnabled(not self._locked)
        self.btn_stop.setEnabled(self._locked)

    def start_run(self):
        """Validate and freeze the recipe before a transport controller runs it."""
        if self._locked:
            return False
        try:
            validation = self.validate_setup()
        except Exception as exc:
            self.lbl_preview.setText(f"Cannot start: {exc}")
            self.lbl_preview.setProperty("role", "warning-hint")
            return False
        if self._execution_controller is not None:
            return bool(self._execution_controller.start())
        self.set_sweep_locked(True)
        self.lbl_preview.setText(
            f"Validated: {validation['rate_a_per_s']:.7f} A/s; "
            "transport controller is ready to execute the frozen recipe."
        )
        return True

    def stop_run(self):
        if self._execution_controller is not None and self._execution_controller.active:
            self._execution_controller.stop()
            return
        self.set_sweep_locked(False)
        self.lbl_preview.setText("Transport sweep stopped; recipe remains available for restart.")

    def set_execution_controller(self, controller):
        self._execution_controller = controller
        controller.error.connect(lambda message: self.lbl_preview.setText(f"Transport error: {message}"))
        controller.state_changed.connect(lambda phase, detail: self.lbl_preview.setText(f"{phase}: {detail}"))
        controller.finished.connect(lambda: self.lbl_preview.setText("B-field Sweep complete"))
        controller.stopped.connect(lambda message: self.lbl_preview.setText(str(message)))
