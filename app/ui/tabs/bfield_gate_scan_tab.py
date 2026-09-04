"""Dedicated, multi-condition persistent B-field Gate Scan tab."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime

from PyQt6 import QtCore, QtWidgets

from app.engine.gate_scan_field_batch import GateScanFieldBatch
from app.gate_scan_summary import format_gate_scan_condition
from app.models import GateScanCondition
from app.ui.tabs.gate_scan_tab import GateScanTab
from app.ui.widgets.collapsible_section import CollapsibleSection
from app.ui.widgets.status_panel import SectionHeader


class BFieldGateScanTab(GateScanTab):
    """Gate Scan editor plus an ordered B-field/condition series.

    The inherited editor is the single source of truth for all Gate Scan
    coordinate choices (raw Vtg/Vbg/Vds and derived Doping/E-field).  A
    condition is a frozen snapshot of that editor, so each row can be edited
    independently without duplicating trajectory validation or worker logic.
    """

    SETTINGS_PREFIX = "tabs/bfield_gate_scan"

    def __init__(self, save, conns, device_manager, get_global_rates_callable=None,
                 get_ao_items_callable=None, get_signal_chain_callable=None):
        self._conditions: list[GateScanCondition] = []
        self._selected_condition = -1
        self._bfield_orchestrator: GateScanFieldBatch | None = None
        self._bfield_backend = "1000"
        self._loading_condition = False
        super().__init__(
            save, conns, device_manager,
            get_global_rates_callable=get_global_rates_callable,
            get_ao_items_callable=get_ao_items_callable,
            get_signal_chain_callable=get_signal_chain_callable,
            include_field_batch=False,
            start_text="START B-FIELD GATE SCAN",
        )
        # Capture the settings loaded by GateScanTab as the first editable row.
        self.collect_params()
        self._conditions = [GateScanCondition("Condition 1", deepcopy(self.p))]
        self._selected_condition = 0
        self._refresh_condition_table()
        self._update_series_buttons()
        # Keep the long-running series history useful without allowing an
        # accidental multi-hour scan to grow the UI document without bound.
        self.log.setMaximumBlockCount(5000)

    def set_temperature_safety(self, snapshot=None, error_message=""):
        """Render a compact external LS335 safety banner.

        This label is informational; the batch evaluator remains authoritative.
        """
        if not hasattr(self, "bfield_temperature_banner"):
            return
        if error_message:
            self.bfield_temperature_banner.setText(f"Lake Shore 335: MONITOR_FAULT — {error_message}")
            return
        if snapshot is None:
            self.bfield_temperature_banner.setText("Lake Shore 335: unavailable")
            return
        sample = getattr(snapshot, "sample_temperature_k", None)
        reservoir = getattr(snapshot, "reservoir_temperature_k", None)
        state = "DISARMED"
        permission = "NO"
        orchestrator = getattr(self, "_bfield_orchestrator", None)
        evaluator = getattr(orchestrator, "thermal_safety", None)
        if evaluator is not None:
            decision = evaluator.evaluate(snapshot)
            state, permission = decision.state.value, ("YES" if decision.magnet_permission else "NO")
        self.bfield_temperature_banner.setText(
            f"Lake Shore 335: sample {sample if sample is not None else '—'} K, "
            f"reservoir {reservoir if reservoir is not None else '—'} K | {state} | permission {permission}"
        )

    def _build_control_panel(self, ctl_layout):
        super()._build_control_panel(ctl_layout)
        content = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(content)
        layout.setContentsMargins(8, 4, 8, 4)
        layout.setSpacing(5)

        intro = QtWidgets.QLabel(
            "Save one or more Gate Scan conditions, then run each condition "
            "at the B fields below in the listed order."
        )
        intro.setWordWrap(True)
        intro.setProperty("role", "hint")
        layout.addWidget(intro)

        field_row = QtWidgets.QHBoxLayout()
        field_row.addWidget(QtWidgets.QLabel("B fields (T):"))
        self.bfield_fields = QtWidgets.QLineEdit()
        self.bfield_fields.setPlaceholderText("-2, -0.5, 0, 0.125 or 1:-1:-1")
        self.bfield_fields.setToolTip(
            "Enter comma/newline-separated fields, or Python-style ranges "
            "start:stop:step (stop is exclusive)."
        )
        field_row.addWidget(self.bfield_fields, 1)
        layout.addLayout(field_row)

        self.bfield_help = QtWidgets.QLabel(
            "Values may be mixed. Range syntax: start:stop:step; stop is exclusive."
        )
        self.bfield_help.setWordWrap(True)
        self.bfield_help.setProperty("role", "hint")
        layout.addWidget(self.bfield_help)
        self.bfield_temperature_banner = QtWidgets.QLabel("Lake Shore 335: unavailable")
        self.bfield_temperature_banner.setWordWrap(True)
        self.bfield_temperature_banner.setProperty("role", "hint")
        layout.addWidget(self.bfield_temperature_banner)
        self.bfield_preview = QtWidgets.QLabel()
        self.lbl_bfield_preview = self.bfield_preview
        self.bfield_preview.setWordWrap(False)
        self.bfield_preview.setTextInteractionFlags(
            QtCore.Qt.TextInteractionFlag.TextSelectableByMouse
        )
        self.bfield_preview.setToolTip(
            "The expanded ordered fields will appear here after you enter them."
        )
        self.bfield_preview.setProperty("role", "hint")
        self.bfield_preview.setSizePolicy(
            QtWidgets.QSizePolicy.Policy.Ignored,
            QtWidgets.QSizePolicy.Policy.Preferred,
        )
        layout.addWidget(self.bfield_preview)

        self.condition_name = QtWidgets.QLineEdit("Condition 1")
        self.condition_name.setPlaceholderText("Condition name")
        self.condition_table = QtWidgets.QTableWidget(0, 3)
        self.condition_table.setHorizontalHeaderLabels(["#", "Condition", "Saved trajectory"])
        self.condition_table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectionBehavior.SelectRows)
        self.condition_table.setSelectionMode(QtWidgets.QAbstractItemView.SelectionMode.SingleSelection)
        self.condition_table.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
        self.condition_table.setMinimumHeight(120)
        self.condition_table.setMaximumHeight(280)
        self.condition_table.setWordWrap(True)
        self.condition_table.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.condition_table.setTextElideMode(QtCore.Qt.TextElideMode.ElideNone)
        self.condition_table.horizontalHeader().setStretchLastSection(True)
        self.condition_table.horizontalHeader().setSectionResizeMode(0, QtWidgets.QHeaderView.ResizeMode.ResizeToContents)
        self.condition_table.horizontalHeader().setSectionResizeMode(1, QtWidgets.QHeaderView.ResizeMode.ResizeToContents)
        self.condition_table.verticalHeader().setVisible(False)
        self.condition_table.verticalHeader().setSectionResizeMode(QtWidgets.QHeaderView.ResizeMode.ResizeToContents)
        layout.addWidget(self.condition_table)

        details_group = QtWidgets.QGroupBox("Selected condition details")
        details_layout = QtWidgets.QVBoxLayout(details_group)
        details_layout.setContentsMargins(8, 6, 8, 6)
        self.condition_details = QtWidgets.QLabel("Select a saved condition to inspect its complete recipe.")
        self.condition_details.setWordWrap(True)
        self.condition_details.setTextInteractionFlags(QtCore.Qt.TextInteractionFlag.TextSelectableByMouse)
        self.condition_details.setProperty("role", "hint")
        self.condition_details.setMinimumWidth(0)
        self.condition_details.setSizePolicy(QtWidgets.QSizePolicy.Policy.Ignored, QtWidgets.QSizePolicy.Policy.Preferred)
        # Alias kept explicit for callers that prefer the label naming style.
        self.lbl_condition_details = self.condition_details
        details_layout.addWidget(self.condition_details)
        layout.addWidget(details_group)

        name_row = QtWidgets.QHBoxLayout()
        name_row.addWidget(QtWidgets.QLabel("Selected name:"))
        name_row.addWidget(self.condition_name, 1)
        layout.addLayout(name_row)

        buttons = QtWidgets.QHBoxLayout()
        self.condition_add = QtWidgets.QPushButton("Add current")
        self.condition_update = QtWidgets.QPushButton("Update selected")
        self.condition_duplicate = QtWidgets.QPushButton("Duplicate")
        self.condition_remove = QtWidgets.QPushButton("Remove")
        for button in (self.condition_add, self.condition_update, self.condition_duplicate, self.condition_remove):
            buttons.addWidget(button)
        layout.addLayout(buttons)

        self.bfield_status = QtWidgets.QLabel("Add at least one condition and enter ordered B fields.")
        self.bfield_status.setWordWrap(True)
        self.bfield_status.setProperty("role", "hint")
        layout.addWidget(self.bfield_status)
        self.condition_edit_status = QtWidgets.QLabel()
        self.condition_edit_status.setWordWrap(True)
        self.condition_edit_status.setProperty("role", "hint")
        self.condition_edit_status.setMinimumWidth(0)
        self.lbl_condition_edit_status = self.condition_edit_status
        layout.addWidget(self.condition_edit_status)
        self.bfield_section = CollapsibleSection("B-field Gate Scan Series", content, expanded=True)

        index = ctl_layout.indexOf(self.lbl_connection_hint)
        ctl_layout.insertWidget(index if index >= 0 else ctl_layout.count(), self.bfield_section)
        self.condition_table.itemSelectionChanged.connect(self._condition_selected)
        self.condition_add.clicked.connect(self._add_condition)
        self.condition_update.clicked.connect(self._update_condition)
        self.condition_duplicate.clicked.connect(self._duplicate_condition)
        self.condition_remove.clicked.connect(self._remove_condition)

    def _wire(self):
        super()._wire()
        self.bfield_fields.textChanged.connect(self._update_bfield_preview)
        self._update_bfield_preview()
        self.btn_start.clicked.disconnect(self.start_run)
        self.btn_stop.clicked.disconnect(self.stop_run)
        self.btn_start.clicked.connect(self.start_series)
        self.btn_stop.clicked.connect(self.stop_series)
        # A frozen condition remains authoritative until Update selected is
        # pressed, but the UI should immediately reveal editor divergence.
        editor_widgets = (
            self.ed_base, self.cbo_source, self.cbo_x, self.cbo_y,
            self.condition_name,
            self.rad_mode_raw, self.rad_mode_derived, self.chk_sweep_bidirectional,
            self.chk_raw_vtg_active, self.chk_raw_vbg_active, self.chk_raw_vds_active,
            self.sp_raw_vtg_start, self.sp_raw_vtg_stop, self.sp_raw_vbg_start, self.sp_raw_vbg_stop,
            self.sp_raw_vds_start, self.sp_raw_vds_stop, self.sp_ratio, self.cbo_ratio_target,
            self.rad_sweep_doping, self.rad_sweep_efield, self.sp_derived_start, self.sp_derived_stop,
            self.sp_derived_fixed, self.btn_derived_vbias_fixed, self.btn_derived_vbias_swept,
            self.sp_derived_vds_fixed, self.sp_derived_vds_start, self.sp_derived_vds_stop,
            self.sp_n_points, self.sp_delay, self.sp_nsamp,
        )
        for widget in editor_widgets:
            if isinstance(widget, QtWidgets.QComboBox):
                signal = widget.currentTextChanged
            elif isinstance(widget, QtWidgets.QLineEdit):
                signal = widget.textChanged
            elif isinstance(widget, QtWidgets.QAbstractButton):
                signal = widget.toggled
            else:
                signal = getattr(widget, "valueChanged", None)
            if signal is not None:
                signal.connect(self._update_condition_edit_state)

    def _load_tab_settings(self):
        """Load inherited and B-field settings, then refresh the preview.

        BaseMeasurementTab deliberately blocks widget signals while applying
        settings.  That prevents the line edit's ``textChanged`` signal from
        updating the preview during restore, so refresh explicitly after the
        load completes rather than relying on signal timing.
        """
        super()._load_tab_settings()
        self._update_bfield_preview()

    def _settings_widgets(self):
        widgets = super()._settings_widgets()
        # The B-field series is a tab-level input, so persist it alongside the
        # inherited Gate Scan editor settings when the tab is reopened.
        if hasattr(self, "bfield_fields"):
            widgets.append(("bfield_fields", self.bfield_fields))
        return widgets

    @staticmethod
    def _format_bfield_value(value: float) -> str:
        return format(float(value), ".12g")

    def _update_bfield_preview(self, *_args):
        """Render the same parsed ordered fields that execution will use."""
        if not hasattr(self, "bfield_preview"):
            return
        try:
            fields = GateScanFieldBatch.parse_fields(self.bfield_fields.text())
        except ValueError as exc:
            self.bfield_preview.setText(f"Invalid B-field series: {exc}")
            self.bfield_preview.setToolTip(str(exc))
            self.bfield_preview.setProperty("role", "warning-hint")
            return

        formatted = [self._format_bfield_value(value) for value in fields]
        if len(formatted) <= 12:
            compact = ", ".join(formatted)
        else:
            compact = ", ".join(formatted[:5] + ["…"] + formatted[-3:])
        self.bfield_preview.setText(
            f"Preview ({len(fields)} field{'s' if len(fields) != 1 else ''}): {compact}"
        )
        full = ", ".join(formatted)
        self.bfield_preview.setToolTip(
            f"Expanded ordered B fields ({len(fields)}): {full}"
        )
        self.bfield_preview.setProperty("role", "hint")

    def _condition_summary(self, params):
        return format_gate_scan_condition(params)

    def _condition_details(self, params):
        return format_gate_scan_condition(params, details=True)

    @staticmethod
    def _condition_fingerprint(params):
        """Compare only recipe values, excluding generated output paths."""
        from dataclasses import fields
        return tuple((field.name, getattr(params, field.name)) for field in fields(params)
                     if field.name not in {"output_csv_path", "output_metadata_path", "output_log_path"})

    def _editor_is_modified(self) -> bool:
        if not (0 <= self._selected_condition < len(self._conditions)) or self._loading_condition:
            return False
        self.collect_params()
        return (
            self.condition_name.text().strip() != self._conditions[self._selected_condition].name
            or self._condition_fingerprint(self.p) != self._condition_fingerprint(self._conditions[self._selected_condition].params)
        )

    def _update_condition_edit_state(self, *_args):
        if self._loading_condition or not hasattr(self, "condition_edit_status"):
            return
        modified = self._editor_is_modified()
        name = self._conditions[self._selected_condition].name if 0 <= self._selected_condition < len(self._conditions) else ""
        self.condition_edit_status.setText(
            f"Editing saved condition: {name}" + (" — Modified" if modified else "")
        )
        if 0 <= self._selected_condition < self.condition_table.rowCount():
            item = self.condition_table.item(self._selected_condition, 1)
            if item is not None:
                base_name = self._conditions[self._selected_condition].name
                item.setText(base_name + (" (Modified)" if modified else ""))

    def _refresh_condition_table(self):
        self.condition_table.setRowCount(len(self._conditions))
        for row, condition in enumerate(self._conditions):
            params = condition.params
            values = (str(row + 1), condition.name, self._condition_summary(params))
            for col, value in enumerate(values):
                self.condition_table.setItem(row, col, QtWidgets.QTableWidgetItem(value))
            self.condition_table.item(row, 2).setToolTip(self._condition_details(params))
        if self._conditions and 0 <= self._selected_condition < len(self._conditions):
            self.condition_table.selectRow(self._selected_condition)
        self._update_series_buttons()
        if self._conditions and 0 <= self._selected_condition < len(self._conditions):
            self.condition_details.setText(self._condition_details(self._conditions[self._selected_condition].params))
            self._update_condition_edit_state()

    def _update_series_buttons(self):
        has = bool(self._conditions)
        selected = has and 0 <= self._selected_condition < len(self._conditions)
        self.condition_update.setEnabled(selected)
        self.condition_duplicate.setEnabled(selected)
        self.condition_remove.setEnabled(len(self._conditions) > 1 and selected)

    def _condition_selected(self):
        rows = self.condition_table.selectionModel().selectedRows()
        if not rows:
            return
        row = rows[0].row()
        if not 0 <= row < len(self._conditions):
            return
        previous = self._selected_condition
        if previous != row and 0 <= previous < self.condition_table.rowCount():
            # Switching conditions intentionally discards unsaved editor
            # values; remove the stale marker from the row being left.
            previous_item = self.condition_table.item(previous, 1)
            if previous_item is not None:
                previous_item.setText(self._conditions[previous].name)
        self._selected_condition = row
        condition = self._conditions[row]
        self._loading_condition = True
        try:
            self.condition_name.setText(condition.name)
            self._set_ui_from_params(condition.params)
        finally:
            self._loading_condition = False
        self.condition_details.setText(self._condition_details(condition.params))
        self.condition_edit_status.setText(f"Editing saved condition: {condition.name}")
        self._update_series_buttons()

    def _add_condition(self):
        self.collect_params()
        name = self.condition_name.text().strip() or f"Condition {len(self._conditions) + 1}"
        self._conditions.append(GateScanCondition(name, deepcopy(self.p)))
        self._selected_condition = len(self._conditions) - 1
        self._refresh_condition_table()

    def _update_condition(self):
        if not 0 <= self._selected_condition < len(self._conditions):
            return
        self.collect_params()
        name = self.condition_name.text().strip() or f"Condition {self._selected_condition + 1}"
        self._conditions[self._selected_condition] = GateScanCondition(name, deepcopy(self.p))
        self._refresh_condition_table()
        self.condition_edit_status.setText(f"Editing saved condition: {name}")

    def _duplicate_condition(self):
        if not 0 <= self._selected_condition < len(self._conditions):
            return
        source = self._conditions[self._selected_condition]
        self._conditions.insert(self._selected_condition + 1, GateScanCondition(
            source.name + " copy", deepcopy(source.params), source.enabled,
        ))
        self._selected_condition += 1
        self._refresh_condition_table()

    def _remove_condition(self):
        if len(self._conditions) <= 1 or not 0 <= self._selected_condition < len(self._conditions):
            return
        self._conditions.pop(self._selected_condition)
        self._selected_condition = min(self._selected_condition, len(self._conditions) - 1)
        self._refresh_condition_table()

    def _set_ui_from_params(self, params):
        """Load a condition snapshot into the inherited Gate Scan editor."""
        controls = (
            (self.ed_base, params.base_name), (self.cbo_source, "Keithley 2400" if params.vds_source == "Keithley 2400" else f"NI DAQ ao{params.ao_channel}"),
            (self.cbo_x, params.plot_x_axis), (self.cbo_y, params.plot_choice),
            (self.rad_mode_raw, params.mode == "Raw"), (self.rad_mode_derived, params.mode != "Raw"),
            (self.chk_sweep_bidirectional, params.sweep_both_ways),
            (self.chk_raw_vtg_active, params.raw_vtg_active), (self.sp_raw_vtg_start, params.raw_vtg_start), (self.sp_raw_vtg_stop, params.raw_vtg_stop),
            (self.chk_raw_vbg_active, params.raw_vbg_active), (self.sp_raw_vbg_start, params.raw_vbg_start), (self.sp_raw_vbg_stop, params.raw_vbg_stop),
            (self.chk_raw_vds_active, params.raw_vds_active), (self.sp_raw_vds_start, params.raw_vds_start), (self.sp_raw_vds_stop, params.raw_vds_stop),
            (self.sp_ratio, params.derived_ratio), (self.sp_derived_start, params.derived_start), (self.sp_derived_stop, params.derived_stop), (self.sp_derived_fixed, params.derived_fixed),
            (self.sp_derived_vds_fixed, params.derived_vds_fixed), (self.sp_derived_vds_start, params.derived_vds_start), (self.sp_derived_vds_stop, params.derived_vds_stop),
            (self.sp_n_points, params.n_points), (self.sp_delay, params.delay), (self.sp_nsamp, params.n_sample),
        )
        for widget, value in controls:
            if isinstance(widget, QtWidgets.QAbstractButton):
                widget.setChecked(bool(value))
            elif isinstance(widget, QtWidgets.QComboBox):
                widget.setCurrentText(str(value))
            elif isinstance(widget, QtWidgets.QLineEdit):
                widget.setText(str(value))
            else:
                widget.setValue(value)
        # Vds source determines the available plot channels; restore the
        # stored y channel only after rebuilding that option list.
        self._update_plot_axis_choices()
        self.cbo_x.setCurrentText(str(params.plot_x_axis))
        self.cbo_y.setCurrentText(str(params.plot_choice))
        self.cbo_ratio_target.setCurrentIndex(0 if params.derived_ratio_target == "Vbg" else 1)
        self.rad_sweep_doping.setChecked(params.derived_axis == "Doping")
        self.rad_sweep_efield.setChecked(params.derived_axis != "Doping")
        self.btn_derived_vbias_fixed.setChecked(params.derived_vds_mode == "Fixed")
        self.btn_derived_vbias_swept.setChecked(params.derived_vds_mode != "Fixed")
        self.p = deepcopy(params)
        self._update_mode_ui()
        self.refresh_output_preview()

    def capture_field_batch_requests(self):
        if self._editor_is_modified():
            self.bfield_status.setText(
                "Unsaved editor changes are not part of this series. "
                "Press Update selected to save them before starting."
            )
        calibration = self.verified_run_calibration()
        if calibration is None:
            raise ValueError("Signal-chain verification failed")
        if not self._conditions:
            raise ValueError("Add at least one scan condition")
        requests = []
        for condition in self._conditions:
            if not condition.enabled:
                continue
            params = deepcopy(condition.params)
            required = ["daq", "g1", "g2"]
            if params.vds_source == "Keithley 2400":
                required.append("g3")
            requests.append((params, tuple(required), calibration, condition.name))
        required_all = tuple(dict.fromkeys(name for _params, required, _cal, _name in requests for name in required))
        missing = [name for name in required_all if not self.device_manager.is_connected(name)]
        if missing:
            raise ValueError("Connect required devices before starting: " + ", ".join(missing).upper())
        return requests

    def set_field_batch_condition(self, params):
        # Keep the inherited validation and worker setup pointed at the same
        # frozen condition that the orchestrator selected.  This is important
        # when consecutive rows switch between Raw and Doping/E-field modes.
        self._set_ui_from_params(params)
        self.p = deepcopy(params)
        self._batch_params = deepcopy(params)

    @staticmethod
    def validate_field_batch_request(params):
        """Validate a frozen condition without depending on current widgets."""
        if params.mode == "Raw":
            if not any((params.raw_vtg_active, params.raw_vbg_active, params.raw_vds_active)):
                raise ValueError("Raw trajectory needs at least one active variable")
        elif abs(float(params.derived_ratio)) < 1e-12:
            raise ValueError("Derived trajectory requires a non-zero ratio")

    def _apply_batch_lock(self):
        """Lock every editor while the reserved series owns the instruments."""
        if not hasattr(self, "control_widget"):
            return
        for widget in self.control_widget.findChildren(QtWidgets.QWidget):
            if widget is self.btn_stop:
                continue
            if isinstance(widget, (QtWidgets.QAbstractButton, QtWidgets.QComboBox,
                                   QtWidgets.QLineEdit, QtWidgets.QAbstractSpinBox,
                                   QtWidgets.QTableWidget)):
                widget.setEnabled(not self._batch_locked)
        self.run_panel.set_start_available(
            not self._batch_locked and self.worker_thread is None
        )
        self.btn_stop.setEnabled(self._batch_locked or self.worker is not None)

    def _required_devices(self):
        if self._batch_devices_claimed and self._batch_params is not None:
            required = ["daq", "g1", "g2"]
            if self._batch_params.vds_source == "Keithley 2400":
                required.append("g3")
            return required
        return super()._required_devices()

    def set_field_batch_orchestrator(self, orchestrator):
        self._bfield_orchestrator = orchestrator
        orchestrator.set_review_validator(self._batch_review_valid)
        orchestrator.state_changed.connect(self._on_batch_state_changed)
        orchestrator.progress_changed.connect(self._on_batch_progress_changed)
        orchestrator.error.connect(self._on_batch_error)
        orchestrator.finished.connect(self._on_batch_finished)
        orchestrator.stopped.connect(self._on_batch_stopped)
        if hasattr(orchestrator, "activity"):
            orchestrator.activity.connect(self._on_batch_activity)

    def _batch_context_text(self) -> str:
        orchestrator = self._bfield_orchestrator
        context = getattr(orchestrator, "event_context", {}) if orchestrator else {}
        parts = []
        condition_index = context.get("condition_index")
        if condition_index is not None:
            condition = f"condition {condition_index}"
            if context.get("condition_name"):
                condition += f" ({context['condition_name']})"
            parts.append(condition)
        target = context.get("target_t")
        if target is not None:
            parts.append(f"target {float(target):+.6f} T")
        if context.get("job_count"):
            parts.append(f"job {context.get('job_index')}/{context['job_count']}")
        return f" [{', '.join(parts)}]" if parts else ""

    def _append_series_display(self, message: str, category: str = "INFO"):
        stamp = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %z")
        self.log.appendPlainText(f"{stamp} [{category}] {str(message)}{self._batch_context_text()}")
        self.log.verticalScrollBar().setValue(self.log.verticalScrollBar().maximum())

    def _on_batch_state_changed(self, phase: str, detail: str):
        self.bfield_status.setText(f"{phase}: {detail}")
        self._append_series_display(f"{phase}: {detail}", "PHASE")

    def _on_batch_progress_changed(self, index: int, count: int, detail: str):
        self.bfield_status.setText(f"{detail} — step {min(index + 1, count)}/{count}")
        self._append_series_display(
            f"{detail} — step {min(index + 1, count)}/{count}", "PROGRESS"
        )

    def _on_batch_error(self, message: str):
        self.bfield_status.setText(f"Error: {message}")
        self._append_series_display(str(message), "ERROR")

    def _on_batch_finished(self):
        self.bfield_status.setText("B-field series complete")
        self._append_series_display("B-field series complete", "COMPLETE")

    def _on_batch_stopped(self, message: str):
        self.bfield_status.setText(f"Stopped: {message}")
        self._append_series_display(str(message), "STOP")

    def _on_batch_activity(self, event):
        """Render magnet and structured batch events without overwriting history."""
        if not isinstance(event, dict):
            self._append_series_display(str(event), "INFO")
            return
        if event.get("ui_duplicate"):
            return
        timestamp = str(
            event.get("timestamp")
            or datetime.now().astimezone().isoformat(timespec="seconds")
        )
        category = str(event.get("category") or "INFO")
        message = str(event.get("message") or "")
        context = event.get("context") or {}
        parts = []
        if context.get("condition_index") is not None:
            condition = f"condition {context['condition_index']}"
            if context.get("condition_name"):
                condition += f" ({context['condition_name']})"
            parts.append(condition)
        if context.get("target_t") is not None:
            parts.append(f"target {float(context['target_t']):+.6f} T")
        if context.get("job_count"):
            parts.append(f"job {context.get('job_index')}/{context['job_count']}")
        suffix = f" [{', '.join(parts)}]" if parts else ""
        self.log.appendPlainText(f"{timestamp} [{category}] {message}{suffix}")
        self.log.verticalScrollBar().setValue(self.log.verticalScrollBar().maximum())

    def set_batch_magnet_context(self, backend_name):
        self._bfield_backend = str(backend_name or "1000")
        self._update_connection_hint()

    def start_series(self):
        # A B-field series must be reproducible: never run an older frozen
        # recipe while the selected editor visibly contains a newer one.
        if self._editor_is_modified():
            self.bfield_status.setText(
                "Cannot start: the selected condition is Modified. "
                "Press Update selected (or switch to a saved condition) before starting."
            )
            return
        if self._bfield_orchestrator is None:
            self.bfield_status.setText("B-field controller is unavailable")
            return
        if self._bfield_backend != "1000":
            self.bfield_status.setText("Select attoDRY1000 (APS100) in Magnet Control")
            return
        self.bfield_status.setText("Starting B-field series…")
        self._bfield_orchestrator.start(self.bfield_fields.text())

    def stop_series(self):
        if self._bfield_orchestrator is not None and self._bfield_orchestrator.active:
            self._bfield_orchestrator.stop()
        elif self.worker is not None:
            self.stop_run()
