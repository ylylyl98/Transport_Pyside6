"""Four-panel orders are isolated across saved instrument flavors."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

if os.environ.get('TRANSPORT_OFFLINE_VERIFICATION') != '1':
    raise unittest.SkipTest('Use verify_offline.py')

from PySide6 import QtCore
from app.quick.application import ApplicationBackend
from app.quick.plot import PlotModel


class ChannelPreferencesTests(unittest.TestCase):
    def test_flavor_load_resets_old_order_but_partial_update_preserves_it(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = QtCore.QSettings(str(Path(directory) / 'settings.ini'), QtCore.QSettings.IniFormat)
            plot = PlotModel()
            self.addCleanup(lambda: plot._timer.stop())
            backend = SimpleNamespace(settings=settings, _plots=[plot], _plot_flavors=['ordinary'],
                                      _loading_plot_preferences=False, _plot_key=lambda mode: 'gate_scan')
            custom = ['Ids_Y', 'Ids_X', 'Ids_DC', 'Ids_Keithley']
            settings.setValue('quick/plots/gate_scan/ordinary', json.dumps({'layout':'four', 'channelOrder':custom}))
            settings.setValue('quick/plots/gate_scan/drag_drive', json.dumps({'layout':'four'}))
            ApplicationBackend._load_plot_preferences(backend, 0)
            self.assertEqual(plot.channels(), custom)
            plot.applyPreferences({'xAxis':'Vbg'})
            self.assertEqual(plot.channels(), custom)
            plot.applyPreferences({'channelOrder':None})
            self.assertIsNone(plot._channel_order)
            ApplicationBackend._load_plot_preferences(backend, 0)
            backend._plot_flavors[0] = 'drag_drive'
            ApplicationBackend._load_plot_preferences(backend, 0)
            self.assertIsNone(plot._channel_order)
            plot.beginRun({'x_axis':'Vtg', 'channels':['I_drag_X','I_drive_X','I_drag_Y','I_drive_Y']})
            self.assertEqual(plot.channels(), ['I_drag_X','I_drive_X','I_drag_Y','I_drive_Y'])
            backend._plot_flavors[0] = 'ordinary'
            ApplicationBackend._load_plot_preferences(backend, 0)
            self.assertEqual(plot.channels(), custom)
