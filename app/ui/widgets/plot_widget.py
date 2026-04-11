from __future__ import annotations

from PyQt6 import QtCore, QtWidgets
from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg as FigureCanvas
from matplotlib.figure import Figure


class PlotWidget(QtWidgets.QWidget):
    y_axis_changed = QtCore.pyqtSignal(str)
    plot_mode_changed = QtCore.pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.fig = Figure(figsize=(5, 3))
        self.canvas = FigureCanvas(self.fig)
        self.ax = self.fig.add_subplot(111)
        self.axes = [self.ax]
        self._y_axis_options: list[str] = []
        self._selected_y_axis = ""
        self._plot_modes = ["Single Plot", "4-Channel Compare"]
        self._selected_plot_mode = "Single Plot"
        self._compare_channels: list[str] = []

        lay = QtWidgets.QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(4)

        header = QtWidgets.QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)
        self.btn_plot_mode = QtWidgets.QToolButton()
        self.btn_plot_mode.setPopupMode(QtWidgets.QToolButton.ToolButtonPopupMode.InstantPopup)
        self.btn_plot_mode.setProperty("role", "status-detail")
        self.btn_plot_mode.show()
        self.plot_mode_menu = QtWidgets.QMenu(self)
        self.btn_plot_mode.setMenu(self.plot_mode_menu)
        header.addWidget(self.btn_plot_mode, 0, QtCore.Qt.AlignmentFlag.AlignLeft)
        header.addStretch(1)
        self.btn_y_axis = QtWidgets.QToolButton()
        self.btn_y_axis.setPopupMode(QtWidgets.QToolButton.ToolButtonPopupMode.InstantPopup)
        self.btn_y_axis.setProperty("role", "status-detail")
        self.btn_y_axis.hide()
        self.y_axis_menu = QtWidgets.QMenu(self)
        self.btn_y_axis.setMenu(self.y_axis_menu)
        header.addWidget(self.btn_y_axis, 0, QtCore.Qt.AlignmentFlag.AlignRight)
        lay.addLayout(header)
        lay.addWidget(self.canvas)
        self.set_plot_mode_options(self._plot_modes, self._selected_plot_mode)
        self.clear()

    def set_y_axis_options(self, options: list[str], selected: str):
        self._y_axis_options = list(options)
        self.y_axis_menu.clear()
        for option in self._y_axis_options:
            action = self.y_axis_menu.addAction(option)
            action.setCheckable(True)
            action.setChecked(option == selected)
            action.triggered.connect(lambda checked=False, value=option: self._emit_y_axis_changed(value))
        self.set_selected_y_axis(selected)
        self.btn_y_axis.setVisible(bool(options))

    def set_selected_y_axis(self, selected: str):
        self._selected_y_axis = selected
        self.btn_y_axis.setText(f"Y: {selected}")
        for action in self.y_axis_menu.actions():
            action.setChecked(action.text() == selected)

    def _emit_y_axis_changed(self, selected: str):
        if selected != self._selected_y_axis:
            self.set_selected_y_axis(selected)
            self.y_axis_changed.emit(selected)

    def set_plot_mode_options(self, options: list[str], selected: str):
        self._plot_modes = list(options)
        self.plot_mode_menu.clear()
        for option in self._plot_modes:
            action = self.plot_mode_menu.addAction(option)
            action.setCheckable(True)
            action.setChecked(option == selected)
            action.triggered.connect(lambda checked=False, value=option: self._emit_plot_mode_changed(value))
        self.set_selected_plot_mode(selected)

    def set_selected_plot_mode(self, selected: str):
        self._selected_plot_mode = selected
        self.btn_plot_mode.setText(f"View: {selected}")
        for action in self.plot_mode_menu.actions():
            action.setChecked(action.text() == selected)
        self._rebuild_axes()

    def _emit_plot_mode_changed(self, selected: str):
        if selected != self._selected_plot_mode:
            self.set_selected_plot_mode(selected)
            self.plot_mode_changed.emit(selected)

    def current_plot_mode(self) -> str:
        return self._selected_plot_mode

    def set_compare_channels(self, channels: list[str]):
        self._compare_channels = list(channels)
        self._rebuild_axes()

    def compare_channels(self) -> list[str]:
        return list(self._compare_channels)

    def get_axes(self):
        return list(self.axes)

    def _rebuild_axes(self):
        self.fig.clear()
        if self._selected_plot_mode == "4-Channel Compare" and self._compare_channels:
            built = self.fig.subplots(len(self._compare_channels), 1, sharex=True)
            if hasattr(built, "ravel"):
                self.axes = list(built.ravel())
            elif isinstance(built, (list, tuple)):
                self.axes = list(built)
            else:
                self.axes = [built]
            self.fig.subplots_adjust(left=0.12, right=0.97, top=0.97, bottom=0.09, hspace=0.12)
        else:
            self.axes = [self.fig.add_subplot(111)]
            self.fig.subplots_adjust(left=0.12, right=0.97, top=0.95, bottom=0.12)
        self.ax = self.axes[0]
        for axis in self.axes:
            axis.grid(True)
        self.canvas.draw_idle()

    def clear(self):
        self._rebuild_axes()
        for axis in self.axes:
            axis.clear()
            axis.grid(True)
        self.canvas.draw_idle()
