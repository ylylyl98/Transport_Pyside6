"""Offline UI behavior: geometry, current channels, and mode-specific layout memory."""
import os
import unittest
from unittest.mock import patch

if os.environ.get('TRANSPORT_OFFLINE_VERIFICATION') != '1':
    raise unittest.SkipTest('Use verify_offline.py to isolate hardware and settings')

from PySide6 import QtCore, QtWidgets
from app.settings import get_app_settings
from app.ui.main_window import MainWindow
from app.ui.widgets.plot_widget import PlotWidget


SINGLE = 'Single Plot'
COMPARE = '4-Channel Compare'
POSITIONS = {'I_drag_X': (0, 0), 'I_drag_Y': (1, 0),
             'I_drive_X': (0, 1), 'I_drive_Y': (1, 1)}


def record(index=0):
    return dict(x=float(index), index=index, vtg=float(index), vbg=0., vds=float(index),
                doping=float(index), efield=0., B_measured_T=float(index), direction='forward',
                raw_X=1., raw_Y=2., raw_drive_X=3., raw_drive_Y=4., raw_DC=5.,
                I_drag_X=1e-9, I_drag_Y=-2e-9, I_drive_X=3e-6, I_drive_Y=-4e-6,
                Ids_X=1e-9, Ids_Y=-2e-9, Ids_DC=5e-8)


class PlotChannelMenuTests(unittest.TestCase):
    def test_single_channel_menu_is_only_shown_in_single_plot(self):
        plot = PlotWidget()
        self.addCleanup(plot.close)
        plot.set_y_axis_options(['Ids_DC', 'I_drag_X', 'I_drive_X'], 'Ids_DC')
        self.assertFalse(plot.btn_y_axis.isHidden())
        plot.set_selected_plot_mode(COMPARE)
        self.assertTrue(plot.btn_y_axis.isHidden())
        plot.set_y_axis_options(['Ids_DC', 'I_drag_X'], 'Ids_DC')
        self.assertTrue(plot.btn_y_axis.isHidden())
        plot.set_selected_plot_mode(SINGLE)
        self.assertFalse(plot.btn_y_axis.isHidden())
        self.assertEqual(plot._selected_y_axis, 'Ids_DC')


class DragDrivePlotTests(unittest.TestCase):
    def setUp(self):
        self.app = QtWidgets.QApplication.instance()
        settings = get_app_settings()
        self.saved = {k: settings.value(k) for k in settings.allKeys()
                      if k.startswith(('plot_mode/', 'drag_drive/'))}
        settings.remove('plot_mode')
        settings.remove('drag_drive')
        settings.sync()
        self.addCleanup(self.restore_settings)
        self.open_window()
        self.addCleanup(self.close_window)

    def open_window(self):
        with patch('app.ui.dock.ConnDock._start_scan'):
            self.window = MainWindow()

    def close_window(self):
        window, self.window = self.window, None
        if window is None:
            return
        window.close()
        window.deleteLater()
        self.app.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)
        self.app.processEvents()

    def restore_settings(self):
        settings = get_app_settings()
        settings.remove('plot_mode')
        settings.remove('drag_drive')
        for key, value in self.saved.items():
            settings.setValue(key, value)
        settings.sync()

    def enable_dual(self, enabled=True):
        self.window.conn_dock.drag_drive_settings.cbo_mode.setCurrentIndex(int(enabled))

    def assert_grid(self, plot):
        self.assertEqual(plot.current_plot_mode(), COMPARE)
        self.assertEqual(len(plot.get_axes()), 4)
        for axis, channel in zip(plot.get_axes(), plot.compare_channels()):
            spec = axis.get_subplotspec()
            self.assertEqual(spec.get_gridspec().get_geometry(), (2, 2))
            self.assertEqual((spec.rowspan.start, spec.colspan.start), POSITIONS[channel])
        axes = plot.get_axes()
        for other in axes[1:]:
            self.assertTrue(axes[0].get_shared_x_axes().joined(axes[0], other))
            self.assertFalse(axes[0].get_shared_y_axes().joined(axes[0], other))
        axes[2].set_xlim(-2., 3.)
        for axis in axes:
            self.assertEqual(axis.get_xlim(), (-2., 3.))
        previous = [a.get_ylim() for a in axes[1:]]
        axes[0].set_ylim(-8e-9, 8e-9)
        self.assertEqual([a.get_ylim() for a in axes[1:]], previous)

    def add_record(self, tab, item):
        if tab is self.window.tab_bfield_transport:
            controller = self.window.bfield_transport_controller
            controller._results.append(item)
            controller.refresh_plot(force=True)
        else:
            tab.on_point_data(item)

    def test_first_enable_uses_requested_grid_in_every_measurement(self):
        for tab in self.window._measurement_tabs():
            self.assertEqual(tab.plot.current_plot_mode(), SINGLE)
        self.enable_dual()
        for key, tab in self.window._plot_mode_tabs().items():
            with self.subTest(tab=key):
                self.assert_grid(tab.plot)

    def test_updates_plot_converted_currents_without_replacing_axes(self):
        self.enable_dual()
        for key, tab in self.window._plot_mode_tabs().items():
            with self.subTest(tab=key):
                axes = tuple(tab.plot.get_axes())
                for index in range(2):
                    self.add_record(tab, record(index))
                self.assertEqual(tuple(tab.plot.get_axes()), axes)
                self.assert_grid(tab.plot)
                for axis, channel in zip(axes, tab.plot.compare_channels()):
                    self.assertEqual(list(axis.lines[0].get_ydata()), [record()[channel]] * 2)
                    self.assertIn('(A)', axis.get_ylabel())
                    self.assertEqual(axis.get_title(), channel.removeprefix('I_').replace('_', ' ').title())
                    if axis.get_subplotspec().is_last_row():
                        self.assertTrue(axis.get_xlabel())
                # Ordinary signal-chain/UI refresh must also leave the axes intact.
                tab._update_plot_axis_choices()
                self.assertEqual(tuple(tab.plot.get_axes()), axes)

    def test_single_plot_retains_dc_and_survives_samples_and_settings_edits(self):
        self.enable_dual()
        for key, tab in self.window._plot_mode_tabs().items():
            with self.subTest(tab=key):
                tab.plot._emit_plot_mode_changed(SINGLE)
                tab.plot._emit_y_axis_changed('Ids_DC')
                axes = tuple(tab.plot.get_axes())
                self.assertIn('Ids_DC', tab.plot._y_axis_options)
                self.add_record(tab, record())
                self.add_record(tab, record(1))
                self.window._on_signal_chain_changed()
                self.assertEqual(tab.plot.current_plot_mode(), SINGLE)
                self.assertEqual(tuple(tab.plot.get_axes()), axes)
                self.assertEqual(list(axes[0].lines[0].get_ydata()), [5e-8, 5e-8])

    def test_preferences_are_separate_by_measurement_mode_and_survive_reopen(self):
        tab = self.window.tab_gate_scan
        tab.plot._emit_plot_mode_changed(COMPARE)
        self.enable_dual()
        self.assertEqual(tab.plot.current_plot_mode(), COMPARE)
        tab.plot._emit_plot_mode_changed(SINGLE)
        self.enable_dual(False)
        self.assertEqual(tab.plot.current_plot_mode(), COMPARE)
        self.assertEqual(self.window.tab_dual.plot.current_plot_mode(), SINGLE)
        self.enable_dual()
        self.assertEqual(tab.plot.current_plot_mode(), SINGLE)
        self.assertEqual(self.window.tab_dual.plot.current_plot_mode(), COMPARE)
        self.close_window()
        self.open_window()
        self.assertEqual(self.window.tab_gate_scan.plot.current_plot_mode(), SINGLE)
        self.assert_grid(self.window.tab_dual.plot)
        self.enable_dual(False)
        self.assertEqual(self.window.tab_gate_scan.plot.current_plot_mode(), COMPARE)
        self.assertEqual(self.window.tab_dual.plot.current_plot_mode(), SINGLE)

    def test_legacy_preference_applies_only_to_ordinary_mode(self):
        settings = get_app_settings()
        settings.setValue('plot_mode/gate_scan', COMPARE)
        settings.sync()
        self.window._load_plot_mode_settings()
        self.assertEqual(self.window.tab_gate_scan.plot.current_plot_mode(), COMPARE)
        self.enable_dual()
        self.window.tab_gate_scan.plot._emit_plot_mode_changed(SINGLE)
        self.enable_dual(False)
        self.assertEqual(self.window.tab_gate_scan.plot.current_plot_mode(), COMPARE)


if __name__ == '__main__':
    unittest.main()
