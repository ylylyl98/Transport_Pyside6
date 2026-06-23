from __future__ import annotations

from typing import List, Optional

from PyQt6 import QtCore, QtGui, QtWidgets
from PyQt6.QtCore import Qt

from app.run_output import output_blocking_reason
from app.ui.widgets.plot_widget import PlotWidget
from app.ui.widgets.run_panel import RunPanel


class _PreviewTextEdit(QtWidgets.QTextEdit):
    """Read-only, borderless, auto-height preview widget for filename/path display.

    Uses WrapAnywhere so long filenames with underscores (no spaces) still wrap
    instead of overflowing the panel.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setReadOnly(True)
        self.setFrameStyle(0)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setWordWrapMode(QtGui.QTextOption.WrapMode.WrapAnywhere)
        self.setSizePolicy(
            QtWidgets.QSizePolicy.Policy.Expanding,
            QtWidgets.QSizePolicy.Policy.Preferred,
        )
        self.setStyleSheet(
            "QTextEdit { color: #6B7280; font-size: 9pt; background: transparent;"
            " border: none; margin: 0px; padding: 0px; }"
        )
        self.document().contentsChanged.connect(self.updateGeometry)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self.updateGeometry()

    def sizeHint(self) -> QtCore.QSize:
        w = self.viewport().width()
        if w > 0:
            self.document().setTextWidth(w)
        h = int(self.document().size().height()) + 2
        return QtCore.QSize(max(w, 60), max(h, 16))

    def minimumSizeHint(self) -> QtCore.QSize:
        return self.sizeHint()


class BaseMeasurementTab(QtWidgets.QWidget):
    def __init__(self, start_text: str, plot_xlabel: str, plot_ylabel: str, status_names: List[str], parent=None):
        super().__init__(parent)
        self._status_names = status_names
        self._active_log_path = ""
        self._build_base_ui(start_text)
        self.plot.ax.set_xlabel(plot_xlabel)
        self.plot.ax.set_ylabel(plot_ylabel)

    def _build_base_ui(self, start_text: str):
        main_layout = QtWidgets.QHBoxLayout(self)
        main_layout.setContentsMargins(8, 8, 8, 8)

        self.main_splitter = QtWidgets.QSplitter(Qt.Orientation.Horizontal)
        self.main_splitter.setChildrenCollapsible(False)

        self.control_scroll = QtWidgets.QScrollArea()
        self.control_scroll.setWidgetResizable(True)
        self.control_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.control_widget = QtWidgets.QWidget()
        self.control_widget.setSizePolicy(
            QtWidgets.QSizePolicy.Policy.Ignored,
            QtWidgets.QSizePolicy.Policy.Preferred,
        )
        self.control_layout = QtWidgets.QVBoxLayout(self.control_widget)
        self.control_layout.setSpacing(8)
        self.control_layout.setContentsMargins(8, 8, 8, 8)
        self.control_scroll.setWidget(self.control_widget)
        self.control_scroll.setMinimumWidth(300)
        self.control_scroll.setMaximumWidth(480)

        self._build_control_panel(self.control_layout)
        self.control_layout.addStretch()

        right_widget = QtWidgets.QWidget()
        right_layout = QtWidgets.QVBoxLayout(right_widget)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(8)

        self.plot = PlotWidget()
        self.plot.setMinimumHeight(300)
        self.log = QtWidgets.QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMinimumHeight(80)
        self.run_panel = RunPanel(start_text)
        self.btn_start = self.run_panel.btn_start
        self.btn_stop = self.run_panel.btn_stop
        self.progress = self.run_panel.progress
        self.lbl_status = self.run_panel.lbl_status

        self.plot_splitter = QtWidgets.QSplitter(Qt.Orientation.Vertical)
        self.plot_splitter.setChildrenCollapsible(False)
        self.plot_splitter.addWidget(self.plot)
        self.plot_splitter.addWidget(self.log)
        self.plot_splitter.setStretchFactor(0, 3)
        self.plot_splitter.setStretchFactor(1, 1)

        right_layout.addWidget(self.plot_splitter, 1)
        right_layout.addWidget(self.run_panel)
        self.main_splitter.addWidget(self.control_scroll)
        self.main_splitter.addWidget(right_widget)
        self.main_splitter.setStretchFactor(1, 1)
        self.main_splitter.setSizes([360, 900])
        main_layout.addWidget(self.main_splitter)

    def _build_control_panel(self, ctl_layout: QtWidgets.QVBoxLayout):
        raise NotImplementedError

    def set_status(self, message: str, state: str, detail: str = ""):
        self.run_panel.set_status_text(message, state, detail)

    def set_progress(self, value: float):
        self.run_panel.set_progress_fraction(value)

    def set_device_status(self, name: str, state: str, detail: Optional[str] = None):
        self.status_panel.set_status(name, state, detail)

    def _add_output_preview_section(self, ctl_layout: QtWidgets.QVBoxLayout):
        self.lbl_filename_preview = _PreviewTextEdit()
        self.lbl_path_preview = _PreviewTextEdit()
        self.lbl_metadata_preview = _PreviewTextEdit()
        self.lbl_log_preview = _PreviewTextEdit()

        self.lbl_output_warning = QtWidgets.QLabel()
        self.lbl_output_warning.setWordWrap(True)
        self.lbl_output_warning.setProperty("role", "warning-hint")
        self.lbl_output_warning.setMinimumWidth(0)
        self.lbl_output_warning.setSizePolicy(
            QtWidgets.QSizePolicy.Policy.Ignored,
            QtWidgets.QSizePolicy.Policy.Preferred,
        )

        wrap = QtWidgets.QWidget()
        vbox = QtWidgets.QVBoxLayout(wrap)
        vbox.setContentsMargins(8, 4, 8, 4)
        vbox.setSpacing(2)

        rows = [
            ("Final CSV:", self.lbl_filename_preview),
            ("Output Folder:", self.lbl_path_preview),
            ("Metadata:", self.lbl_metadata_preview),
            ("Run Log:", self.lbl_log_preview),
            ("Output Warning:", self.lbl_output_warning),
        ]
        for key_text, val_lbl in rows:
            row = QtWidgets.QWidget()
            hbox = QtWidgets.QHBoxLayout(row)
            hbox.setContentsMargins(0, 0, 0, 0)
            hbox.setSpacing(6)
            key_lbl = QtWidgets.QLabel(key_text)
            key_lbl.setProperty("role", "hint")
            key_lbl.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignTop)
            key_lbl.setSizePolicy(
                QtWidgets.QSizePolicy.Policy.Preferred,
                QtWidgets.QSizePolicy.Policy.Preferred,
            )
            hbox.addWidget(key_lbl)
            hbox.addWidget(val_lbl, 1)
            vbox.addWidget(row)
            if val_lbl is self.lbl_output_warning:
                self._output_warning_row = row
                row.hide()

        ctl_layout.addWidget(wrap)

    def set_output_preview_text(self, planned, warning: str = ""):
        ds = planned.display_stem
        self.lbl_filename_preview.setPlainText(ds + ".csv")
        self.lbl_filename_preview.setToolTip(planned.csv_path)
        self.lbl_path_preview.setPlainText(planned.output_dir)
        self.lbl_path_preview.setToolTip(planned.output_dir)
        self.lbl_metadata_preview.setPlainText(ds + "_metadata.json")
        self.lbl_metadata_preview.setToolTip(planned.metadata_path)
        self.lbl_log_preview.setPlainText(ds + "_run_log.txt")
        self.lbl_log_preview.setToolTip(planned.log_path)
        self.lbl_output_warning.setText(warning)
        self._output_warning_row.setVisible(bool(warning))

    def validate_output_ready(self, save) -> bool:
        planned = getattr(self, "_planned_output", None)
        if planned is None:
            return True
        reason = output_blocking_reason(planned, save)
        if reason:
            QtWidgets.QMessageBox.warning(self, "Output Not Ready", reason)
            return False
        return True

    def begin_run_logging(self, planned, measurement_name: str):
        log_path = planned.log_path if planned is not None else ""
        self._active_log_path = ""
        if not log_path:
            return
        import datetime
        import os

        os.makedirs(os.path.dirname(log_path), exist_ok=True)
        with open(log_path, "x", encoding="utf-8") as f:
            f.write(f"Run: {measurement_name}\n")
            f.write(f"Started: {datetime.datetime.now().isoformat(timespec='seconds')}\n")
            f.write(f"CSV: {planned.csv_path}\n")
            f.write(f"Metadata: {planned.metadata_path}\n\n")
        self._active_log_path = log_path

    def append_log(self, message: str):
        self.log.appendPlainText(message)
        if not self._active_log_path:
            return
        try:
            with open(self._active_log_path, "a", encoding="utf-8") as f:
                f.write(str(message).rstrip() + "\n")
        except Exception:
            pass

    def end_run_logging(self, status: str, detail: str = ""):
        if not self._active_log_path:
            return
        import datetime

        try:
            with open(self._active_log_path, "a", encoding="utf-8") as f:
                f.write(f"\nStatus: {status}\n")
                if detail:
                    f.write(f"Detail: {detail}\n")
                f.write(f"Ended: {datetime.datetime.now().isoformat(timespec='seconds')}\n")
        except Exception:
            pass
