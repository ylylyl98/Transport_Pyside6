from __future__ import annotations

import sys

from PyQt6 import QtWidgets
from PyQt6.QtCore import Qt

from app.app_identity import APP_NAME, configure_qapp, set_windows_app_id
from app.device_manager import DeviceManager
from app.settings import get_app_settings
from app.ui.dock import ConnDock
from app.ui.lockin_panel import LockinPanel
from app.ui.style import APP_STYLE
from app.ui.tabs.cosweep_tab import CoSweepTab
from app.ui.tabs.dual_gate_tab import DualGateTab
from app.ui.tabs.gate_scan_tab import GateScanTab
from app.ui.tabs.photocurrent_tab import PhotocurrentTab


class MainWindow(QtWidgets.QMainWindow):
    def __init__(self):
        super().__init__()
        app = QtWidgets.QApplication.instance()
        if app is not None:
            app.setStyleSheet(APP_STYLE)

        self.setWindowTitle(APP_NAME)
        app = QtWidgets.QApplication.instance()
        if app is not None and not app.windowIcon().isNull():
            self.setWindowIcon(app.windowIcon())
        self.resize(1400, 860)
        self.view_menu = self.menuBar().addMenu("View")

        self.conn_dock = ConnDock()
        self.conn_dock.load_settings()
        self.conn_dock.stop_requested.connect(self.on_emergency_stop)

        self.instrument_dock = QtWidgets.QDockWidget("Instrument Setup", self)
        self.instrument_scroll = QtWidgets.QScrollArea()
        self.instrument_scroll.setWidgetResizable(True)
        self.instrument_scroll.setSizeAdjustPolicy(QtWidgets.QAbstractScrollArea.SizeAdjustPolicy.AdjustIgnored)
        self.instrument_scroll.setSizePolicy(
            QtWidgets.QSizePolicy.Policy.Preferred,
            QtWidgets.QSizePolicy.Policy.Ignored,
        )
        self.instrument_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.instrument_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.instrument_scroll.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)
        self.instrument_scroll.setMinimumHeight(0)
        self.conn_dock.setMinimumHeight(0)
        self.instrument_scroll.setWidget(self.conn_dock)
        self.instrument_dock.setWidget(self.instrument_scroll)
        self.instrument_dock.setMinimumWidth(240)
        self.instrument_dock.setMinimumHeight(0)
        self.instrument_dock.setFeatures(
            QtWidgets.QDockWidget.DockWidgetFeature.DockWidgetClosable
            | QtWidgets.QDockWidget.DockWidgetFeature.DockWidgetMovable
        )
        self.addDockWidget(Qt.DockWidgetArea.LeftDockWidgetArea, self.instrument_dock)
        self.view_menu.addAction(self.instrument_dock.toggleViewAction())

        self.save_root = self.conn_dock.save_root
        self.connections = self.conn_dock.conns
        self.device_manager = DeviceManager(self.connections)
        self.conn_dock.set_device_manager(self.device_manager)
        self.refresh_models_from_ui()

        self.lockin_panel = LockinPanel(self.device_manager)
        self.lockin_panel.sensitivity_read.connect(self.conn_dock.set_lockin_sensitivity_from_sr830)
        self.lockin_dock = QtWidgets.QDockWidget("SR830 Lock-in", self)
        self.lockin_scroll = QtWidgets.QScrollArea()
        self.lockin_scroll.setWidgetResizable(True)
        self.lockin_scroll.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)
        self.lockin_scroll.setWidget(self.lockin_panel)
        self.lockin_dock.setWidget(self.lockin_scroll)
        self.lockin_dock.setMinimumWidth(360)
        self.lockin_dock.setFeatures(
            QtWidgets.QDockWidget.DockWidgetFeature.DockWidgetClosable
            | QtWidgets.QDockWidget.DockWidgetFeature.DockWidgetMovable
            | QtWidgets.QDockWidget.DockWidgetFeature.DockWidgetFloatable
        )
        self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, self.lockin_dock)
        self.view_menu.addAction(self.lockin_dock.toggleViewAction())

        self.tabs = QtWidgets.QTabWidget()
        self.setCentralWidget(self.tabs)
        self.tab_dual = DualGateTab(self.save_root, self.connections, self.device_manager, get_global_rates_callable=self.conn_dock.get_rates)
        self.tab_cosweep = CoSweepTab(self.save_root, self.connections, self.device_manager, get_global_rates_callable=self.conn_dock.get_rates, get_ao_items_callable=self.tab_dual.get_ao_items_if_available)
        self.tab_gate_scan = GateScanTab(self.save_root, self.connections, self.device_manager, get_global_rates_callable=self.conn_dock.get_rates, get_ao_items_callable=self.tab_dual.get_ao_items_if_available)
        self.tab_photocurrent = PhotocurrentTab(self.save_root, self.connections, self.device_manager, get_global_rates_callable=self.conn_dock.get_rates, get_ao_items_callable=self.tab_dual.get_ao_items_if_available)
        self.tabs.addTab(self.tab_dual, "Vds Sweep")
        self.tabs.addTab(self.tab_gate_scan, "Gate Scan")
        self.tabs.addTab(self.tab_cosweep, "2D Map")
        self.tabs.addTab(self.tab_photocurrent, "Photocurrent")
        self._bind_save_preview_updates()
        self._bind_plot_mode_settings()
        self._load_plot_mode_settings()

    def refresh_models_from_ui(self):
        c, s, _ = self.conn_dock.to_models()
        self.save_root.user = s.user
        self.save_root.device_id = s.device_id
        self.save_root.base = s.base
        self.connections.gate1 = c.gate1
        self.connections.gate2 = c.gate2
        self.connections.gate3 = c.gate3
        self.connections.gate1_mode = c.gate1_mode
        self.connections.gate2_mode = c.gate2_mode
        self.connections.gate3_mode = c.gate3_mode
        self.connections.daq_dev = c.daq_dev
        self.connections.mono = c.mono
        self.connections.lockin = c.lockin
        self.device_manager.sync_addresses()

    def _bind_save_preview_updates(self):
        for widget in (self.conn_dock.ed_user, self.conn_dock.ed_device_id, self.conn_dock.ed_base):
            widget.textChanged.connect(self._on_save_settings_edited)

    def _on_save_settings_edited(self):
        self.refresh_models_from_ui()
        for tab in (self.tab_dual, self.tab_gate_scan, self.tab_cosweep, self.tab_photocurrent):
            if hasattr(tab, "refresh_output_preview"):
                tab.refresh_output_preview()

    def closeEvent(self, event):
        self._save_plot_mode_settings()
        for tab in (self.tab_dual, self.tab_gate_scan, self.tab_cosweep, self.tab_photocurrent):
            if hasattr(tab, "save_tab_settings"):
                tab.save_tab_settings()
        self.lockin_panel.save_panel_settings()
        self.conn_dock.save_settings()
        self.device_manager.shutdown()
        event.accept()

    def _plot_mode_tabs(self):
        return {
            "vds_sweep": self.tab_dual,
            "gate_scan": self.tab_gate_scan,
            "map_2d": self.tab_cosweep,
            "photocurrent": self.tab_photocurrent,
        }

    def _bind_plot_mode_settings(self):
        for _key, tab in self._plot_mode_tabs().items():
            tab.plot.plot_mode_changed.connect(lambda _mode, current_tab=tab: self._save_single_plot_mode(current_tab))

    def _load_plot_mode_settings(self):
        settings = get_app_settings()
        for key, tab in self._plot_mode_tabs().items():
            saved_mode = str(settings.value(f"plot_mode/{key}", "Single Plot"))
            if saved_mode not in ("Single Plot", "4-Channel Compare"):
                saved_mode = "Single Plot"
            tab.plot.set_selected_plot_mode(saved_mode)
            if hasattr(tab, "_redraw_plot"):
                tab._redraw_plot()

    def _save_plot_mode_settings(self):
        settings = get_app_settings()
        for _key, tab in self._plot_mode_tabs().items():
            self._save_single_plot_mode(tab, settings)
        settings.sync()

    def _save_single_plot_mode(self, tab, settings=None):
        settings = settings or get_app_settings()
        for key, candidate in self._plot_mode_tabs().items():
            if candidate is tab:
                settings.setValue(f"plot_mode/{key}", tab.plot.current_plot_mode())
                settings.sync()
                break

    def on_emergency_stop(self):
        tabs = [self.tab_dual, self.tab_gate_scan, self.tab_cosweep, self.tab_photocurrent]
        for tab in tabs:
            if tab.worker:
                tab.worker.request_stop()
                tab.log.appendPlainText("!!! EMERGENCY STOP REQUESTED !!!")

        for tab in tabs:
            if hasattr(tab, "run_panel"):
                tab.run_panel.set_running(False)

        daq_channels = self.device_manager.daq_output_channels()
        self.device_manager.emergency_stop()

        msg = "Stop signal sent to all workers.\n\n"
        msg += "Safe ramp started:\n"
        msg += "- Keithley outputs G1, G2, and G3 are being ramped toward 0 V where connected.\n"
        if daq_channels:
            msg += "- DAQ AO outputs requested: " + ", ".join(f"ao{channel}" for channel in daq_channels) + "\n"
        else:
            msg += "- No connected DAQ AO outputs were available to request.\n"
        msg += "\nWatch Instrument Setup status for safe-ramp completion."
        QtWidgets.QMessageBox.critical(self, "Emergency Stop", msg)


def launch_in_notebook(show: bool = True) -> MainWindow:
    set_windows_app_id()
    app = QtWidgets.QApplication.instance()
    if app is None:
        app = QtWidgets.QApplication(sys.argv)
    configure_qapp(app)
    w = MainWindow()
    if show:
        w.show()
    return w
