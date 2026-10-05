from __future__ import annotations

import sys

from PySide6 import QtWidgets
from PySide6.QtCore import Qt

from app.app_identity import APP_NAME, configure_qapp, set_windows_app_id
from app.device_manager import DeviceManager
from app.engine.gate_scan_field_batch import GateScanFieldBatch
from app.engine.bfield_transport_controller import BFieldTransportController
from app.settings import get_app_settings
from controllers.attodry2100_controller import AttoDRY2100Controller
from controllers.magnet_controller import MagnetController
from controllers.lakeshore335_controller import LakeShore335Controller
from app.thermal_safety import ThermalSafetyEvaluator
from app.signal_chain import SignalChainSnapshot
from app.ui.dock import ConnDock
from app.ui.instrument_workspace import InstrumentWorkspace
from app.ui.measurement_workflow import MeasurementWorkflow
from app.ui.lockin_panel import LockinPanel
from app.ui.magnet_panel import MagnetPanel
from app.ui.style import APP_STYLE
from utils.config import cfg
from app.ui.tabs.cosweep_tab import CoSweepTab
from app.ui.tabs.dual_gate_tab import DualGateTab
from app.ui.tabs.gate_scan_tab import GateScanTab
from app.ui.tabs.bfield_gate_scan_tab import BFieldGateScanTab
from app.ui.tabs.bfield_transport_tab import BFieldTransportTab
from app.ui.tabs.photocurrent_tab import PhotocurrentTab
from app.ui.sample_temperature_bar import SampleTemperatureBar
from app.curve_history import device_history_folder
from app.ui.history_viewer_launcher import HistoryViewerLauncher
from app.ui.png_export_process import PngExportProcess
from app.ui.measurement_png_coordinator import MeasurementPngCoordinator


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
        self.workspace_menu = QtWidgets.QMenuBar()
        self.view_menu = self.workspace_menu.addMenu("View")

        # One owner per physical magnet system for the lifetime of the window.
        # Controllers start worker threads but do not open hardware until the
        # operator passes the panel's commissioning review gate.
        self.magnet1000 = MagnetController(parent=self)
        self.magnet2100 = AttoDRY2100Controller(config=cfg.attodry2100, parent=self)
        self._experiment_2100_field = None
        self._experiment_2100_temperature = None
        self.magnet2100.snapshot_updated.connect(lambda value: setattr(self, "_experiment_2100_field", value))
        self.magnet2100.temperature_updated.connect(lambda value: setattr(self, "_experiment_2100_temperature", value))
        # LS335 is a read-only external monitor dedicated to the APS100
        # B-field workflow; it is deliberately not shared with the 2100 path.
        self.lakeshore335 = LakeShore335Controller(parent=self)
        self.thermal_safety = ThermalSafetyEvaluator(cfg.lakeshore335)
        self.magnet_panel = MagnetPanel(self.magnet1000, self.magnet2100, self, self.lakeshore335, self.thermal_safety)
        self.conn_dock = ConnDock()
        self.conn_dock.load_settings()
        self.conn_dock.stop_requested.connect(self.on_emergency_stop)

        self.save_root = self.conn_dock.save_root
        self.connections = self.conn_dock.conns
        self.device_manager = DeviceManager(self.connections)
        self.conn_dock.set_device_manager(self.device_manager)
        self.refresh_models_from_ui()

        self.lockin_panel = LockinPanel(self.device_manager)
        self.drive_lockin_panel = LockinPanel(self.device_manager, session_name="lockin_drive")
        self.drive_lockin_panel.stop_sweep_requested.connect(self._stop_active_sweep_for_lockin_settings)
        self.conn_dock.bind_lockin_panels(self.lockin_panel, self.drive_lockin_panel)
        self.lockin_panel.stop_sweep_requested.connect(self._stop_active_sweep_for_lockin_settings)
        self.instrument_workspace = InstrumentWorkspace(
            self.conn_dock, self.magnet_panel, self.lockin_panel, self.device_manager, self, drive_lockin=self.drive_lockin_panel
        )
        self.instrument_dock = QtWidgets.QDockWidget("Instruments", self)
        self.instrument_dock.setObjectName("instrumentWorkspace")
        self.instrument_dock.setWidget(self.instrument_workspace)
        self.instrument_dock.setMinimumWidth(370)
        self.instrument_dock.setAllowedAreas(Qt.DockWidgetArea.LeftDockWidgetArea)
        self.instrument_dock.setFeatures(QtWidgets.QDockWidget.DockWidgetFeature.DockWidgetClosable)
        self.addDockWidget(Qt.DockWidgetArea.LeftDockWidgetArea, self.instrument_dock)
        self.view_menu.addAction(self.instrument_dock.toggleViewAction())
        self.command_bar = QtWidgets.QToolBar("Workspace", self)
        self.command_bar.setMovable(False)
        self.command_bar.addAction(self.instrument_dock.toggleViewAction())
        self.command_bar.addAction("Devices...", self.instrument_workspace.show_devices)
        self.command_bar.addAction("Sample / Files...", self.instrument_workspace.show_experiment)
        self.command_bar.addAction("Signal chain...", self.instrument_workspace.show_signal_chain)
        self.active_run_button = QtWidgets.QPushButton("No active measurement")
        self.active_run_button.setMaximumWidth(270)
        self.active_run_button.setEnabled(False)
        self.active_run_button.clicked.connect(self._show_active_measurement)
        self.command_bar.addWidget(self.active_run_button)
        spacer = QtWidgets.QWidget()
        spacer.setSizePolicy(QtWidgets.QSizePolicy.Policy.Expanding, QtWidgets.QSizePolicy.Policy.Preferred)
        self.command_bar.addWidget(spacer)
        # Move the existing button: its original stop_requested routing stays intact.
        self.conn_dock.layout().removeWidget(self.conn_dock.btn_stop)
        self.conn_dock.btn_stop.setText("STOP ALL / ZERO VOLTAGES")
        self.conn_dock.btn_stop.setToolTip("Stop measurements, request safe voltage ramps to 0 V, and pause magnets. This does not mean zero magnetic field.")
        # A permanent header keeps Stop outside toolbar overflow and away from
        # the native caption buttons and resizing edges. Hideable tools follow it.
        self.workspace_header = QtWidgets.QWidget(self)
        header_layout = QtWidgets.QVBoxLayout(self.workspace_header)
        header_layout.setContentsMargins(0, 0, 0, 0)
        header_layout.setSpacing(0)
        header_layout.addWidget(self.workspace_menu)
        self.stop_strip = QtWidgets.QWidget()
        stop_layout = QtWidgets.QHBoxLayout(self.stop_strip)
        stop_layout.setContentsMargins(24, 12, 24, 12)
        stop_layout.setSpacing(0)
        stop_layout.addWidget(self.conn_dock.btn_stop)
        stop_layout.addSpacing(32)
        stop_layout.addWidget(self.command_bar, 1)
        header_layout.addWidget(self.stop_strip)
        self.setMenuWidget(self.workspace_header)
        self.conn_dock.btn_stop.setAutoDefault(False)
        self.conn_dock.btn_stop.setAccessibleName("Stop all measurements and ramp voltages to zero")

        self.tabs = QtWidgets.QTabWidget()
        self.sample_temperature_bar = SampleTemperatureBar(self.lakeshore335, cfg.lakeshore335, self)
        central = QtWidgets.QWidget()
        central_layout = QtWidgets.QVBoxLayout(central)
        central_layout.setContentsMargins(0, 0, 0, 0)
        central_layout.setSpacing(0)
        central_layout.addWidget(self.sample_temperature_bar)
        central_layout.addWidget(self.tabs, 1)
        self.setCentralWidget(central)
        self.tab_dual = DualGateTab(self.save_root, self.connections, self.device_manager, get_global_rates_callable=self.conn_dock.get_rates, get_signal_chain_callable=self.signal_chain_snapshot)
        self.tab_cosweep = CoSweepTab(self.save_root, self.connections, self.device_manager, get_global_rates_callable=self.conn_dock.get_rates, get_ao_items_callable=self.tab_dual.get_ao_items_if_available, get_signal_chain_callable=self.signal_chain_snapshot)
        self.tab_gate_scan = GateScanTab(self.save_root, self.connections, self.device_manager, get_global_rates_callable=self.conn_dock.get_rates, get_ao_items_callable=self.tab_dual.get_ao_items_if_available, get_signal_chain_callable=self.signal_chain_snapshot)
        self.tab_bfield_gate_scan = BFieldGateScanTab(self.save_root, self.connections, self.device_manager, get_global_rates_callable=self.conn_dock.get_rates, get_ao_items_callable=self.tab_dual.get_ao_items_if_available, get_signal_chain_callable=self.signal_chain_snapshot)
        self.tab_bfield_transport = BFieldTransportTab(self.save_root, self.connections, self.device_manager, get_global_rates_callable=self.conn_dock.get_rates, get_signal_chain_callable=self.signal_chain_snapshot)
        self.bfield_transport_controller = BFieldTransportController(
            self.magnet1000, self.tab_bfield_transport, self.device_manager,
            thermal_safety=self.thermal_safety, parent=self,
        )
        self.tab_bfield_transport.set_execution_controller(self.bfield_transport_controller)
        self.bfield_transport_controller.lakeshore_controller = self.lakeshore335
        # If closing the window has to wait for an APS100 Persistent
        # acknowledgement after a successful Driven transport, retry the
        # close event automatically once that safety transition completes.
        self.bfield_transport_controller.shutdown_ready.connect(self.close)
        self.tab_photocurrent = PhotocurrentTab(self.save_root, self.connections, self.device_manager, get_global_rates_callable=self.conn_dock.get_rates, get_ao_items_callable=self.tab_dual.get_ao_items_if_available, get_signal_chain_callable=self.signal_chain_snapshot)
        self.gate_scan_field_batch = GateScanFieldBatch(
            self.magnet1000,
            self.tab_bfield_gate_scan,
            self,
            selected_backend_callable=lambda: self.magnet_panel._backend,
            thermal_safety=self.thermal_safety,
            lakeshore_controller=self.lakeshore335,
        )
        self.lakeshore335.snapshot_updated.connect(self.gate_scan_field_batch.on_lakeshore_snapshot)
        self.lakeshore335.snapshot_updated.connect(self.bfield_transport_controller.on_lakeshore_snapshot)
        self.lakeshore335.snapshot_updated.connect(self.tab_bfield_transport.refresh_hardware_readiness)
        self.lakeshore335.disconnected.connect(self._clear_lakeshore_thermal)
        self.lakeshore335.disconnected.connect(self.gate_scan_field_batch.on_lakeshore_disconnected)
        self.lakeshore335.disconnected.connect(self.bfield_transport_controller.on_lakeshore_disconnected)
        self.lakeshore335.disconnected.connect(self.tab_bfield_transport.refresh_hardware_readiness)
        self.lakeshore335.snapshot_updated.connect(self.magnet_panel._on_lakeshore_snapshot)
        self.lakeshore335.snapshot_updated.connect(self.tab_bfield_gate_scan.set_temperature_safety)
        self.lakeshore335.fault.connect(lambda message: self.tab_bfield_gate_scan.set_temperature_safety(None, message))
        self.lakeshore335.fault.connect(self.bfield_transport_controller._on_fault)
        self.tab_bfield_gate_scan.set_field_batch_orchestrator(self.gate_scan_field_batch)
        self.magnet_panel.backend_changed.connect(
            self.tab_bfield_gate_scan.set_batch_magnet_context
        )
        self.magnet_panel.backend_changed.connect(self.sample_temperature_bar.set_backend)
        self.sample_temperature_bar.set_backend(self.magnet_panel._backend)
        self.sample_temperature_bar.readiness_changed.connect(self._sync_temperature_start_gate)
        self._sync_temperature_start_gate(self.sample_temperature_bar.is_ready())
        self.tab_bfield_gate_scan.set_batch_magnet_context(self.magnet_panel._backend)
        self.tabs.addTab(self.tab_dual, "Vds Sweep")
        self.tabs.addTab(self.tab_gate_scan, "Gate Scan")
        self.tabs.addTab(self.tab_bfield_gate_scan, "B-field Gate Scan")
        self.tabs.addTab(self.tab_bfield_transport, "B-field Sweep")
        self.tabs.addTab(self.tab_cosweep, "2D Map")
        self.tabs.addTab(self.tab_photocurrent, "Photocurrent")
        self.tab_curve_compare = HistoryViewerLauncher(
            lambda: device_history_folder(self.conn_dock.to_models()[1]), self
        )
        self._png_close_pending = False
        self.tab_curve_compare.shutdown_ready.connect(self.close)
        self.tabs.addTab(self.tab_curve_compare, "Curve Compare")
        self.tabs.currentChanged.connect(self._on_workspace_page_changed)
        self._bind_save_preview_updates()
        self.conn_dock.signal_chain_changed.connect(self._on_signal_chain_changed)
        self.lockin_panel.settings_changed.connect(self._on_signal_chain_changed)
        self._bind_plot_mode_settings()
        self._load_plot_mode_settings()
        self.png_exporter = PngExportProcess(self)
        self.png_exporter.completed.connect(self._png_export_completed)
        self.tab_curve_compare.bind_exporter(self.png_exporter)
        self.png_coordinator = MeasurementPngCoordinator(
            self._measurement_tabs(), self.png_exporter, self.tab_curve_compare.auto_png_check.isChecked, self
        )
        for tab in self._measurement_tabs():
            tab.refine_parameter_presentation()
            tab.workflow = MeasurementWorkflow(tab)
            tab.run_panel.running_changed.connect(self._update_active_measurement)
            tab.run_panel.status_changed.connect(self._update_active_measurement)
            tab.run_panel.running_changed.connect(self._refresh_history_after_run)
            tab.run_panel.status_changed.connect(self._refresh_history_after_run)
        self.gate_scan_field_batch.state_changed.connect(self._update_active_measurement)
        self.gate_scan_field_batch.finished.connect(self._update_active_measurement)
        self.gate_scan_field_batch.stopped.connect(self._update_active_measurement)
        self.gate_scan_field_batch.error.connect(self._update_active_measurement)
        self.resizeDocks([self.instrument_dock], [370], Qt.Orientation.Horizontal)
        self._update_active_measurement()

    def _active_measurement_tabs(self):
        return [tab for tab in self._measurement_tabs()
                if tab.run_panel.operation_active()
                or (tab is self.tab_bfield_gate_scan and self.gate_scan_field_batch.active)]

    def _on_workspace_page_changed(self, _index):
        if self.tabs.currentWidget() is self.tab_curve_compare:
            self.tab_curve_compare.open_viewer()

    def _refresh_history_after_run(self, *_):
        if not any(tab.run_panel.operation_active() for tab in self._measurement_tabs()):
            self.tab_curve_compare.refresh()

    def _png_export_completed(self, result):
        if result.get("error"):
            message = "PNG export failed: " + result["error"]
        elif result.get("warnings"):
            message = f"Saved {len(result.get('outputs', []))} PNGs. " + " · ".join(result["warnings"])
        elif result.get("outputs"):
            message = f"Saved {len(result['outputs'])} PNGs in plots."
        else:
            message = ""
        self.tab_curve_compare.png_status_label.setText(message)

    def _update_active_measurement(self, *_args):
        active = self._active_measurement_tabs()
        names = [self.tabs.tabText(self.tabs.indexOf(tab)) + " — " + tab.run_panel.lbl_phase.text() for tab in active]
        self.active_run_button.setText("Running: " + ", ".join(names) if names else "No active measurement")
        self.active_run_button.setToolTip(self.active_run_button.text())
        self.active_run_button.setEnabled(bool(active))

    def _show_active_measurement(self):
        active = self._active_measurement_tabs()
        if active:
            self.tabs.setCurrentWidget(active[0])

    def _sync_temperature_start_gate(self, ready):
        """Gate every measurement start only for an opted-in 1000 run."""
        blocked = bool(
            self.magnet_panel._backend == "1000"
            and self.sample_temperature_bar.wait_check.isChecked()
            and not ready
        )
        for tab in self._measurement_tabs():
            run_panel = getattr(tab, "run_panel", None)
            if run_panel is not None:
                run_panel.set_start_blocked("sample_temperature", blocked)

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
        self.connections.gate1_max_voltage_v = c.gate1_max_voltage_v
        self.connections.gate2_max_voltage_v = c.gate2_max_voltage_v
        self.connections.gate3_max_voltage_v = c.gate3_max_voltage_v
        self.connections.gate1_current_compliance_a = c.gate1_current_compliance_a
        self.connections.gate2_current_compliance_a = c.gate2_current_compliance_a
        self.connections.gate3_current_compliance_a = c.gate3_current_compliance_a
        self.connections.daq_dev = c.daq_dev
        self.connections.mono = c.mono
        self.connections.lockin = c.lockin
        self.connections.drive_lockin = c.drive_lockin
        self.connections.drag_drive_enabled = c.drag_drive_enabled
        self.device_manager.sync_addresses()
        if getattr(self, "tab_cosweep", None) is not None:
            self.tab_cosweep._update_sweep_summary()

    def _clear_lakeshore_thermal(self):
        self.thermal_safety.latest_snapshot = None
        self.thermal_safety.evaluate(None)

    def _bind_save_preview_updates(self):
        for widget in (self.conn_dock.ed_user, self.conn_dock.ed_device_id, self.conn_dock.ed_base):
            widget.textChanged.connect(self._on_save_settings_edited)
        tab = getattr(self, "tab_cosweep", None)
        if tab is not None:
            # Read draft settings for the estimate only. sync_addresses can close
            # live sessions, so hardware text edits must not invoke it here.
            tab.get_timing_context = lambda: self.conn_dock.to_models()[:2]
            for widget in (self.conn_dock.cbo_g1, self.conn_dock.cbo_g2,
                           self.conn_dock.cbo_g3, self.conn_dock.cbo_daq,
                           self.conn_dock.cbo_lockin, self.conn_dock.cbo_g1_mode,
                           self.conn_dock.cbo_g2_mode, self.conn_dock.cbo_g3_mode):
                widget.currentTextChanged.connect(tab._update_sweep_summary)

    def _on_save_settings_edited(self):
        self.refresh_models_from_ui()
        if getattr(self, "tab_curve_compare", None) is not None:
            self.tab_curve_compare.folder_changed()
        for tab in self._measurement_tabs():
            if hasattr(tab, "refresh_output_preview"):
                tab.refresh_output_preview()
            if hasattr(tab, "workflow"):
                tab.workflow.refresh()

    def signal_chain_snapshot(self) -> SignalChainSnapshot:
        values = self.conn_dock.signal_chain_values()
        self.conn_dock.update_ac_voltage_estimate(self.lockin_panel.sp_sine_out.value())
        lockin_connected = self.device_manager.is_connected("lockin")
        return SignalChainSnapshot(
            drag_drive=values.get("drag_drive"),
            ac_contact=str(values.get("ac_contact", "")),
            ac_voltage_ratio=values.get("ac_voltage_ratio"),
            lockin_settings=self.lockin_panel.settings_snapshot(),
            frequency_hz=float(self.lockin_panel.sp_frequency.value()),
            lockin_sensitivity_v=float(values["lockin_sensitivity_v"]),
            preamp_sensitivity_a=float(values["preamp_sensitivity_a"]),
            frequency_source="connected lock-in" if lockin_connected else "saved/manual",
            lockin_sensitivity_source=str(values["lockin_sensitivity_source"]),
            preamp_sensitivity_source=str(values["preamp_sensitivity_source"]),
        )

    def experiment_context_snapshot(self):
        """Only copy existing telemetry; never poll hardware to build metadata."""
        from dataclasses import asdict, is_dataclass
        from copy import deepcopy
        import time
        from app.experiment_metadata import timestamp

        context = {"captured_at": timestamp(), "source": "cached application telemetry",
                   "scope": "run-start conditions; not per-point telemetry",
                   "magnet_backend": getattr(self.magnet_panel, "_backend", None),
                   "device_states": dict(self.device_manager.states)}
        snapshots = {
            "magnet1000": getattr(self.magnet1000, "latest_snapshot", None),
            "magnet2100": self._experiment_2100_field,
            "temperature2100": self._experiment_2100_temperature,
            "temperature": getattr(self.thermal_safety, "latest_snapshot", None),
        }
        for key, snapshot in snapshots.items():
            if snapshot is None:
                context[key] = {"available": False}
                continue
            values = asdict(snapshot) if is_dataclass(snapshot) else deepcopy(snapshot) if isinstance(snapshot, dict) else None
            sample_time = getattr(snapshot, "monotonic_s", None)
            context[key] = {"available": values is not None, "values": values,
                            "age_s": max(0.0, time.monotonic() - sample_time) if sample_time is not None else None}
        return context

    def _on_signal_chain_changed(self):
        mode_changed = False
        controls = getattr(getattr(self, 'conn_dock', None), 'drag_drive_settings', None)
        if controls is not None:
            enabled = controls.configuration()['enabled']
            mode_changed = self.connections.drag_drive_enabled != enabled
            if mode_changed:
                self._save_plot_mode_settings()
            self.connections.drag_drive_enabled = enabled
            self.conn_dock._update_reconnect_indicators()
        workspace = getattr(self, 'instrument_workspace', None)
        if workspace is not None:
            workspace.refresh_summary()
        for tab in tuple(
            tab for tab in (
                getattr(self, "tab_dual", None), getattr(self, "tab_gate_scan", None),
                getattr(self, "tab_bfield_gate_scan", None), getattr(self, "tab_cosweep", None),
                getattr(self, "tab_bfield_transport", None),
                getattr(self, "tab_photocurrent", None),
            ) if tab is not None
        ):
            if mode_changed and hasattr(tab, "_update_plot_axis_choices"):
                tab._update_plot_axis_choices()
            tab.refresh_output_preview()
        if mode_changed:
            self._load_plot_mode_settings()

    def _stop_active_sweep_for_lockin_settings(self):
        for tab in tuple(
            tab for tab in (
                getattr(self, "tab_dual", None), getattr(self, "tab_gate_scan", None),
                getattr(self, "tab_bfield_gate_scan", None), getattr(self, "tab_cosweep", None),
                getattr(self, "tab_bfield_transport", None),
                getattr(self, "tab_photocurrent", None),
            ) if tab is not None
        ):
            if getattr(tab, "worker", None) is not None or getattr(tab, "_locked", False):
                tab.stop_run()
                return

    def closeEvent(self, event):
        prepare_lockin = getattr(getattr(self, "lockin_panel", None), "prepare_shutdown", None)
        if callable(prepare_lockin) and not prepare_lockin():
            event.ignore()
            return
        prepare_drive = getattr(getattr(self, "drive_lockin_panel", None), "prepare_shutdown", None)
        if callable(prepare_drive) and not prepare_drive():
            event.ignore()
            return
        if self._png_close_pending:
            if self.tab_curve_compare.prepare_shutdown():
                event.accept()
            else:
                event.ignore()
            return
        self._save_plot_mode_settings()
        for tab in self._measurement_tabs():
            if hasattr(tab, "save_tab_settings"):
                tab.save_tab_settings()
        self.lockin_panel.save_panel_settings()
        self.conn_dock.save_settings()
        # Shutdown is deliberately explicit and ordered.  The 2100 controller
        # reports whether its owner confirmed Stop/close; never claim a safe
        # shutdown when that acknowledgement is unavailable.
        self.gate_scan_field_batch.stop()
        if getattr(self, "bfield_transport_controller", None) is not None:
            controller = self.bfield_transport_controller
            if not controller.prepare_shutdown():
                # Keep the window and APS/device ownership in place until the
                # asynchronous pause, persistent-mode, and RATE/limit restore
                # acknowledgements have completed.  This also covers a
                # completed Driven run whose heater is still intentionally ON.
                event.ignore()
                return
        if getattr(self, "tab_bfield_transport", None) is not None:
            self.tab_bfield_transport.stop_run()
        self.magnet1000.shutdown()
        self.lakeshore335.shutdown()
        safe_2100 = self.magnet2100.shutdown()
        if safe_2100 is False:
            QtWidgets.QMessageBox.critical(
                self,
                "Magnet shutdown not confirmed",
                "The attoDRY2100 controller could not confirm a safe shutdown. "
                "Keep the application open and use STOP / PAUSE or retry shutdown.",
            )
            event.ignore()
            return
        try:
            self.device_manager.shutdown()
        except Exception as ex:
            QtWidgets.QMessageBox.critical(
                self,
                "Keithley shutdown not confirmed",
                "The application could not confirm that every voltage-source Keithley reached 0 V. "
                "The outputs were left ON and the instrument sessions were preserved.\n\n"
                f"{ex}",
            )
            event.ignore()
            return
        self.png_coordinator.shutdown()
        self.png_exporter.shutdown()
        self._png_close_pending = True
        if self.tab_curve_compare.prepare_shutdown():
            event.accept()
        else:
            event.ignore()

    def _plot_mode_tabs(self):
        return {
            key: tab for key, tab in (
                ("vds_sweep", getattr(self, "tab_dual", None)),
                ("gate_scan", getattr(self, "tab_gate_scan", None)),
                ("bfield_gate_scan", getattr(self, "tab_bfield_gate_scan", None)),
                ("bfield_transport", getattr(self, "tab_bfield_transport", None)),
                ("map_2d", getattr(self, "tab_cosweep", None)),
                ("photocurrent", getattr(self, "tab_photocurrent", None)),
            ) if tab is not None
        }

    def _measurement_tabs(self):
        """Return available tabs (also keeps lightweight test doubles valid)."""
        return tuple(
            tab for tab in (
                getattr(self, "tab_dual", None),
                getattr(self, "tab_gate_scan", None),
                getattr(self, "tab_bfield_gate_scan", None),
                getattr(self, "tab_bfield_transport", None),
                getattr(self, "tab_cosweep", None),
                getattr(self, "tab_photocurrent", None),
            ) if tab is not None
        )

    def _bind_plot_mode_settings(self):
        for _key, tab in self._plot_mode_tabs().items():
            tab.plot.plot_mode_changed.connect(lambda _mode, current_tab=tab: self._save_single_plot_mode(current_tab))

    def _load_plot_mode_settings(self):
        settings = get_app_settings()
        self._plot_measurement_mode = 'drag_drive' if self.connections.drag_drive_enabled else 'ordinary'
        for key, tab in self._plot_mode_tabs().items():
            # Only ordinary mode inherits the pre-dual-mode preference.
            default = ('4-Channel Compare' if self._plot_measurement_mode == 'drag_drive'
                       else str(settings.value(f'plot_mode/{key}', 'Single Plot')))
            saved_mode = str(settings.value(f'plot_mode/{key}/{self._plot_measurement_mode}', default))
            if saved_mode not in ("Single Plot", "4-Channel Compare"):
                saved_mode = '4-Channel Compare' if self._plot_measurement_mode == 'drag_drive' else 'Single Plot'
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
                mode = getattr(self, '_plot_measurement_mode', 'ordinary')
                settings.setValue(f'plot_mode/{key}/{mode}', tab.plot.current_plot_mode())
                settings.sync()
                break

    def on_emergency_stop(self):
        # Emergency stop explicitly requests LS335 sample-heater OFF.  The
        # controller reports confirmation only after RANGE readback.
        if self.magnet_panel._backend == "1000":
            self.lakeshore335.heater_off()
        self.gate_scan_field_batch.stop()
        tabs = list(self._measurement_tabs())
        for tab in tabs:
            worker = getattr(tab, "worker", None)
            if worker is not None:
                worker.request_stop()
            elif hasattr(tab, "stop_run"):
                tab.stop_run()
                tab.log.appendPlainText("!!! EMERGENCY STOP REQUESTED !!!")

        for tab in tabs:
            if hasattr(tab, "run_panel"):
                tab.run_panel.set_running(False)

        daq_channels = self.device_manager.daq_output_channels()
        self.device_manager.emergency_stop()
        # Keep this path synchronous only in the sense of issuing requests;
        # both controllers perform hardware work on their dedicated threads.
        self.magnet1000.pause()
        self.magnet2100.request_stop()

        msg = "Stop signal sent to all workers.\n\n"
        msg += "Safe ramp started:\n"
        msg += "- Keithley outputs G1, G2, and G3 are being ramped toward 0 V where connected.\n"
        if daq_channels:
            msg += "- DAQ AO outputs requested: " + ", ".join(f"ao{channel}" for channel in daq_channels) + "\n"
        else:
            msg += "- No connected DAQ AO outputs were available to request.\n"
        msg += "\nWatch Instruments > Gate / DAQ for safe-ramp completion."
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
