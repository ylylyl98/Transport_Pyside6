from __future__ import annotations

from contextlib import contextmanager
from functools import wraps

from PySide6 import QtCore, QtWidgets
from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg as FigureCanvas, NavigationToolbar2QT
from matplotlib.figure import Figure
from app.plot_ranges import FULL_SWEEP, ACQUIRED_DATA, finite_bounds, padded_limits


def preserve_plot_view(method):
    """Keep user zoom and apply the selected X policy after artist updates."""
    @wraps(method)
    def wrapped(self, *args, **kwargs):
        with self.plot.redraw():
            return method(self, *args, **kwargs)
    return wrapped


class _PlotToolbar(NavigationToolbar2QT):
    toolitems = tuple(item for item in NavigationToolbar2QT.toolitems
                     if item[0] in {"Home", "Back", "Forward", "Pan", "Zoom", "Save"})

    def home(self, *args):
        self.parent().reset_view()


class PlotWidget(QtWidgets.QWidget):
    y_axis_changed = QtCore.Signal(str)
    plot_mode_changed = QtCore.Signal(str)

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
        self._compare_grid = False
        self._x_range_mode = FULL_SWEEP
        self._planned_x_ranges = {}
        self._x_axis_key = "x"
        self._manual_xlim = None
        self._manual_ylims = {}
        self._view_update_depth = 0
        self._view_callbacks = []

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
        self.btn_x_range = QtWidgets.QToolButton()
        self.btn_x_range.setPopupMode(QtWidgets.QToolButton.ToolButtonPopupMode.InstantPopup)
        self.btn_x_range.setProperty("role", "status-detail")
        self.x_range_menu = QtWidgets.QMenu(self)
        self.btn_x_range.setMenu(self.x_range_menu)
        for mode in (FULL_SWEEP, ACQUIRED_DATA):
            action = self.x_range_menu.addAction(mode)
            action.setCheckable(True)
            action.triggered.connect(lambda checked=False, value=mode: self.set_x_range_mode(value))
        self.btn_x_range.setToolTip("Full sweep shows the complete planned X range. Acquired data follows recorded points. Home resets zoom.")
        header.addWidget(self.btn_x_range)
        self._update_x_range_menu()
        header.addStretch(1)
        self.btn_y_axis = QtWidgets.QToolButton()
        self.btn_y_axis.setPopupMode(QtWidgets.QToolButton.ToolButtonPopupMode.InstantPopup)
        self.btn_y_axis.setProperty("role", "status-detail")
        self.btn_y_axis.hide()
        self.y_axis_menu = QtWidgets.QMenu(self)
        self.btn_y_axis.setMenu(self.y_axis_menu)
        header.addWidget(self.btn_y_axis, 0, QtCore.Qt.AlignmentFlag.AlignRight)
        lay.addLayout(header)
        self.toolbar = _PlotToolbar(self.canvas, self)
        lay.addWidget(self.toolbar)
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
        self.btn_y_axis.setVisible(bool(options) and self._selected_plot_mode == 'Single Plot')

    def set_selected_y_axis(self, selected: str):
        if selected != self._selected_y_axis:
            self._manual_ylims.clear()
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
        changed = selected != self._selected_plot_mode
        self._selected_plot_mode = selected
        self.btn_y_axis.setVisible(bool(self._y_axis_options) and selected == 'Single Plot')
        self.btn_plot_mode.setText(f"View: {selected}")
        for action in self.plot_mode_menu.actions():
            action.setChecked(action.text() == selected)
        if changed:
            self._rebuild_axes()

    def _emit_plot_mode_changed(self, selected: str):
        if selected != self._selected_plot_mode:
            self.set_selected_plot_mode(selected)
            self.plot_mode_changed.emit(selected)

    def current_plot_mode(self) -> str:
        return self._selected_plot_mode

    def set_compare_channels(self, channels: list[str], *, grid: bool = False):
        if self._compare_channels == list(channels) and self._compare_grid == grid:
            return
        self._compare_channels = list(channels)
        self._compare_grid = grid
        self._rebuild_axes()

    def compare_channels(self) -> list[str]:
        return list(self._compare_channels)

    def get_axes(self):
        return list(self.axes)

    def bottom_axes(self):
        """Axes that need an X label, including both columns of the dual grid."""
        return [axis for axis in self.axes if axis.get_subplotspec().is_last_row()]

    def format_compare_axes(self):
        if self._selected_plot_mode != '4-Channel Compare':
            return
        for axis, channel in zip(self.axes, self._compare_channels):
            if self._compare_grid:
                axis.set_title(channel.removeprefix('I_').replace('_', ' ').title(), fontsize=10)
            axis.tick_params(axis='x', labelbottom=axis.get_subplotspec().is_last_row())

    def _rebuild_axes(self):
        with self.redraw():
            self._create_axes()
            self._manual_ylims.clear()
        if hasattr(self, "toolbar"):
            self.toolbar.update()
        self.canvas.draw_idle()

    def _create_axes(self):
        self.fig.clear()
        if self._selected_plot_mode == "4-Channel Compare" and self._compare_channels:
            if self._compare_grid and len(self._compare_channels) == 4:
                built = self.fig.subplots(2, 2, sharex=True, sharey=False)
                # Channels are Drag X/Y, Drive X/Y: preserve column order.
                self.axes = [built[0, 0], built[1, 0], built[0, 1], built[1, 1]]
                self.fig.subplots_adjust(left=.13, right=.97, top=.91, bottom=.12,
                                         hspace=.32, wspace=.46)
            else:
                built = self.fig.subplots(len(self._compare_channels), 1, sharex=True)
                self.axes = list(built.ravel()) if hasattr(built, 'ravel') else [built]
                self.fig.subplots_adjust(left=.12, right=.97, top=.97, bottom=.09, hspace=.12)
        else:
            self.axes = [self.fig.add_subplot(111)]
            self.fig.subplots_adjust(left=0.12, right=0.97, top=0.95, bottom=0.12)
        self.ax = self.axes[0]
        for axis in self.axes:
            axis.grid(True)
        self.format_compare_axes()

    def clear(self, *, reset_plan=False):
        if reset_plan:
            self._planned_x_ranges.clear()
            self._manual_xlim = None
            self._manual_ylims.clear()
        self._rebuild_axes()
        self.canvas.draw_idle()

    def _update_x_range_menu(self):
        self.btn_x_range.setText("X range: " + self._x_range_mode)
        for action in self.x_range_menu.actions():
            action.setChecked(action.text() == self._x_range_mode)

    def set_x_range_mode(self, mode):
        if mode not in {FULL_SWEEP, ACQUIRED_DATA}:
            raise ValueError("Unknown plot X range mode")
        self._x_range_mode = mode
        self._update_x_range_menu()
        self.reset_view()

    def set_planned_ranges(self, ranges, *, axis=None):
        self._planned_x_ranges = {key: tuple(sorted(bounds)) for key, bounds in ranges.items()}
        if axis is not None:
            self._x_axis_key = axis
        self.reset_view()

    def set_x_axis_key(self, axis):
        if axis != self._x_axis_key:
            self._x_axis_key = axis
            self._manual_xlim = None

    def reset_view(self):
        self._manual_xlim = None
        self._manual_ylims.clear()
        with self.redraw():
            pass
        self.toolbar.update()
        self.toolbar.push_current()
        self.canvas.draw_idle()

    @contextmanager
    def redraw(self):
        self._view_update_depth += 1
        try:
            yield
        finally:
            try:
                if self._view_update_depth == 1:
                    self._apply_view_limits()
                    # Axes.clear replaces its callback registry.
                    self._bind_view_callbacks()
            finally:
                self._view_update_depth -= 1

    def _apply_view_limits(self):
        bounds = []
        for axis in self.axes:
            for line in axis.lines:
                extent = finite_bounds(line.get_xdata())
                if extent is not None:
                    bounds.extend(extent)
        measured = finite_bounds(bounds)
        planned = self._planned_x_ranges.get(self._x_axis_key) if self._x_range_mode == FULL_SWEEP else None
        x_limits = self._manual_xlim
        if x_limits is None:
            if planned is not None:
                x_limits = padded_limits(planned)
                # Keep the planned view stable through small readback deviations.
                if measured is not None and (measured[0] < x_limits[0] or measured[1] > x_limits[1]):
                    x_limits = padded_limits(finite_bounds((*planned, *measured)))
            elif measured is not None:
                x_limits = padded_limits(measured)
        for index, axis in enumerate(self.axes):
            axis.relim()
            axis.set_autoscalex_on(True)
            axis.set_autoscaley_on(True)
            axis.autoscale_view()
            if x_limits is not None:
                axis.set_xlim(x_limits)
            if index in self._manual_ylims:
                axis.set_ylim(self._manual_ylims[index])

    def _bind_view_callbacks(self):
        for registry, x_id, y_id in self._view_callbacks:
            registry.disconnect(x_id)
            registry.disconnect(y_id)
        self._view_callbacks = []
        for index, axis in enumerate(self.axes):
            registry = axis.callbacks
            x_id = registry.connect("xlim_changed", self._capture_xlim)
            y_id = registry.connect("ylim_changed", lambda changed, i=index: self._capture_ylim(changed, i))
            self._view_callbacks.append((registry, x_id, y_id))

    def _capture_xlim(self, axis):
        if not self._view_update_depth:
            self._manual_xlim = axis.get_xlim()

    def _capture_ylim(self, axis, index):
        if not self._view_update_depth:
            self._manual_ylims[index] = axis.get_ylim()
