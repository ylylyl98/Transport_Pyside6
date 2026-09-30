"""Presentation-only preflight: inspect widgets and cached state, never poll devices."""
from PySide6 import QtCore, QtWidgets
from app.ui.widgets.collapsible_section import CollapsibleSection


class _FeedbackLabel(QtWidgets.QLabel):
    """Reserve wrapped text height inside the plot/run-panel splitter."""
    def setText(self, text):
        super().setText(text)
        self._fit_height()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._fit_height()

    def _fit_height(self):
        self.setMinimumHeight(max(0, self.heightForWidth(max(1, self.width()))))


def parameter_issues(tab):
    """Return (field, message) pairs for existing rules safe to check while editing.

    Execution-time validation remains authoritative, including frozen recipes,
    calibration, protection limits, and output creation.
    """
    issues = []
    if hasattr(tab, "bfield_fields"):
        # The series runs frozen conditions, not the current parameter editor.
        from app.engine.gate_scan_field_batch import GateScanFieldBatch
        try:
            GateScanFieldBatch.parse_fields(tab.bfield_fields.text())
        except ValueError as exc:
            issues.append((tab.bfield_fields, str(exc)))
        if not any(condition.enabled for condition in tab._conditions):
            issues.append((tab.condition_table, "Enable at least one saved scan condition."))
    elif hasattr(tab, "rad_mode_raw"):
        if tab.rad_mode_raw.isChecked():
            if not any(getattr(tab, "chk_raw_" + axis + "_active").isChecked() for axis in ("vtg", "vbg", "vds")):
                issues.append((tab.chk_raw_vtg_active, "Select at least one active sweep variable."))
        elif abs(tab.sp_ratio.value()) < 1e-12:
            issues.append((tab.sp_ratio, "Derived trajectory requires a non-zero ratio."))
        else:
            try:
                low, high = tab._derived_axis_bounds()
                for field in (tab.sp_derived_start, tab.sp_derived_stop):
                    if not low - 1e-9 <= field.value() <= high + 1e-9:
                        issues.append((field, f"Use a value between {low:g} and {high:g} for these gate limits."))
            except ValueError as exc:
                issues.append((tab.sp_ratio, str(exc)))
    elif hasattr(tab, "cbo_sweep_dim"):
        if getattr(tab, "_precision_error", ""):
            issues.append((tab._axis_controls(tab.cbo_fast.currentText())[3], tab._precision_error))
        if tab._is_derived() and not tab._is_2d_map():
            issues.append((tab.cbo_sweep_dim, "Doping/E-field coordinates require a 2D map."))
        if tab._is_derived() and abs(tab.sp_ratio.value()) < 1e-12:
            issues.append((tab.sp_ratio, "Doping/E-field sweeps require a non-zero ratio."))
        if tab._is_2d_map() and tab.cbo_slow.currentText() in ("None", tab.cbo_fast.currentText()):
            issues.append((tab.cbo_slow, "Choose two different axes for a 2D map."))
        for axis in tab._swept_axes():
            field = tab._axis_controls(axis)[3]
            if abs(field.value()) < 1e-9:
                issues.append((field, f"{axis} Step must be non-zero for a swept axis."))
    elif hasattr(tab, "sp_rate") and hasattr(tab, "condition_table"):
        from app.engine.bfield_transport_sweep import validate_setup
        try:
            validate_setup(tab.sp_start.value(), tab.sp_stop.value(), tab.sp_rate.value())
        except ValueError as exc:
            issues.append((tab.sp_rate, str(exc)))
    return issues


class MeasurementWorkflow(QtCore.QObject):
    """Keep actionable preflight feedback next to Start, even with panels hidden."""
    def __init__(self, tab):
        super().__init__(tab)
        self.tab = tab
        self._marked = {}
        self._inline = {}
        self._issues = []
        self._refreshing = False
        # Existing tab logic continues updating this cached hint; render it once
        # beside Start instead of repeating it in the parameter column.
        tab.lbl_connection_hint.hide()
        self.panel = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(self.panel)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        self.summary = _FeedbackLabel()
        self.summary.setWordWrap(True)
        self.summary.setTextFormat(QtCore.Qt.TextFormat.PlainText)
        self.summary.setSizePolicy(QtWidgets.QSizePolicy.Policy.Ignored, QtWidgets.QSizePolicy.Policy.Preferred)
        summary_policy = self.summary.sizePolicy()
        summary_policy.setHeightForWidth(True)
        self.summary.setSizePolicy(summary_policy)
        self.summary.setProperty("role", "warning-hint")
        layout.addWidget(self.summary)
        actions = QtWidgets.QHBoxLayout()
        self.review_button = QtWidgets.QPushButton("Review inputs")
        self.review_button.clicked.connect(self.review_inputs)
        actions.addWidget(self.review_button)
        self.setup_buttons = {}
        for text, method in (("Devices...", "show_devices"), ("Sample / Files...", "show_experiment")):
            button = QtWidgets.QPushButton(text)
            self.setup_buttons[method] = button
            button.clicked.connect(lambda _checked=False, name=method: getattr(tab.window().instrument_workspace, name)())
            actions.addWidget(button)
        actions.addStretch(1)
        layout.addLayout(actions)
        tab.run_panel.layout().insertWidget(0, self.panel)
        self.timer = QtCore.QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.setInterval(0)
        self.timer.timeout.connect(self.refresh)
        for widget in tab.control_widget.findChildren(QtWidgets.QWidget):
            if isinstance(widget, (QtWidgets.QDoubleSpinBox, QtWidgets.QSpinBox)):
                widget.valueChanged.connect(self.schedule_refresh)
            elif isinstance(widget, QtWidgets.QLineEdit):
                widget.textChanged.connect(self.schedule_refresh)
            elif isinstance(widget, QtWidgets.QComboBox):
                widget.currentIndexChanged.connect(self.schedule_refresh)
            elif isinstance(widget, QtWidgets.QAbstractButton) and widget.isCheckable():
                widget.toggled.connect(self.schedule_refresh)
            elif isinstance(widget, QtWidgets.QTableWidget):
                widget.itemChanged.connect(self.schedule_refresh)
                widget.model().rowsInserted.connect(self.schedule_refresh)
                widget.model().rowsRemoved.connect(self.schedule_refresh)
        tab.device_manager.status_changed.connect(self.schedule_refresh)
        tab.device_manager.resources_changed.connect(self.schedule_refresh)
        tab.device_manager.operation_changed.connect(self.schedule_refresh)
        tab.device_manager.protection_changed.connect(self.schedule_refresh)
        tab.device_manager.gate_currents_read.connect(self.schedule_refresh)
        tab.run_panel.running_changed.connect(self.schedule_refresh)
        tab.run_panel.status_changed.connect(self.schedule_refresh)
        tab.run_panel.readiness_changed.connect(self.schedule_refresh)
        self.refresh()

    def schedule_refresh(self, *_args):
        self.timer.start()

    def refresh(self, *_args):
        if self._refreshing:
            return
        self._refreshing = True
        try:
            self._refresh()
        finally:
            self._refreshing = False

    def _refresh(self):
        tab = self.tab
        running = tab.run_panel.operation_active() or bool(getattr(tab, "_batch_locked", False))
        self.panel.setVisible(not running)
        if running:
            return
        for field, (tooltip, description) in self._marked.items():
            field.setProperty("invalid", False)
            field.setToolTip(tooltip)
            field.setAccessibleDescription(description)
            field.style().unpolish(field)
            field.style().polish(field)
        self._marked.clear()
        for label in self._inline.values():
            label.hide()
        self._issues = parameter_issues(tab)
        messages = []
        for field, message in self._issues:
            if field not in self._marked:
                self._marked[field] = (field.toolTip(), field.accessibleDescription())
            field.setProperty("invalid", True)
            field.setToolTip(message)
            field.setAccessibleDescription(message)
            field.style().unpolish(field)
            field.style().polish(field)
            self.show_inline(field, message)
            messages.append(message)
        save = tab.save
        missing = [label for attr, label in (("user", "Operator"), ("device_id", "Device ID"), ("base", "Data root"))
                   if not str(getattr(save, attr, "") or "").strip()]
        if missing:
            messages.append("Sample / Files: fill in " + ", ".join(missing) + ".")
        # Use the existing preview warning; do not call output_blocking_reason,
        # which creates folders, nor any execution-time/hardware validator here.
        warning = "" if hasattr(tab, "bfield_fields") else tab.lbl_output_warning.text().strip()
        if warning and not missing:
            messages.append(warning)
        busy = tab.device_manager.is_busy()
        if busy:
            messages.append("Wait for the current device operation to finish.")
        tab.run_panel.set_start_blocked("workflow-inputs", bool(self._issues or missing or warning or busy))
        hint = getattr(tab, "lbl_connection_hint", None)
        hardware_issue = hint is not None and (not tab.run_panel._start_available or "required before start" in hint.text().lower())
        if hardware_issue:
            messages.append(hint.text().split(" Optional")[0])
        if "sample_temperature" in tab.run_panel._external_start_blocks:
            messages.append("Wait for the sample temperature to become stable.")
        other_blocks = tab.run_panel._external_start_blocks - {"workflow-inputs", "sample_temperature"}
        if other_blocks:
            messages.append("Start held: " + ", ".join(sorted(other_blocks)))
        self.summary.setText("\n".join(dict.fromkeys(messages)) if messages
                             else "Inputs checked. Hardware and safety checks run again at Start.")
        self.review_button.setEnabled(bool(self._issues))
        self.review_button.setVisible(bool(self._issues))
        self.setup_buttons["show_devices"].setVisible(bool(busy or hardware_issue))
        self.setup_buttons["show_experiment"].setVisible(bool(missing or warning))
        self.panel.setVisible(bool(messages))

    def show_inline(self, field, message):
        if field not in self._inline:
            parent = field.parentWidget()
            layout = parent.layout() if parent else None
            if not isinstance(layout, QtWidgets.QFormLayout):
                return  # Grid/table controls use the persistent review summary.
            row, role = layout.getWidgetPosition(field)
            if row < 0:
                return
            label = QtWidgets.QLabel()
            label.setWordWrap(True)
            label.setTextFormat(QtCore.Qt.TextFormat.PlainText)
            label.setProperty("role", "warning-hint")
            layout.insertRow(row + 1, label)
            self._inline[field] = label
        self._inline[field].setText(message)
        self._inline[field].show()

    def review_inputs(self):
        if not self._issues:
            return
        tab = self.tab
        field = self._issues[0][0]
        tab.parameters_button.setChecked(True)
        parent = field.parentWidget()
        while parent is not None and parent is not tab:
            if isinstance(parent, CollapsibleSection):
                parent.set_expanded(True)
            parent = parent.parentWidget()
        field.setFocus(QtCore.Qt.FocusReason.OtherFocusReason)
        QtCore.QTimer.singleShot(0, lambda: tab.control_scroll.ensureWidgetVisible(field))
