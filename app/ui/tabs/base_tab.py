from __future__ import annotations

from typing import List, Optional

from PyQt6 import QtWidgets
from PyQt6.QtCore import Qt

from app.ui.widgets.plot_widget import PlotWidget
from app.ui.widgets.run_panel import RunPanel


class BaseMeasurementTab(QtWidgets.QWidget):
    def __init__(self, start_text: str, plot_xlabel: str, plot_ylabel: str, status_names: List[str], parent=None):
        super().__init__(parent)
        self._status_names = status_names
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
        self.control_widget = QtWidgets.QWidget()
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
