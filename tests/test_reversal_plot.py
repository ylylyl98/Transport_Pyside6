import tempfile
import unittest
from pathlib import Path

import numpy as np

from app.curve_cache import SnapshotCache
from app.curve_history import load_curve
from app.quick.plot import PlotModel


class ReversalPlotTests(unittest.TestCase):
    def test_live_flag_follows_axis_and_channel(self):
        model = PlotModel()
        model.beginRun({'x_axis': 'B-field', 'channels': ['Ids_DC', 'Ids_Keithley']})
        model.appendRecord({'x': 0.1, 'B_Keithley_T': 0.11, 'Ids_DC': 1, 'Ids_Keithley': 2})
        model.appendRecord({'x': 0.2, 'B_Keithley_T': 0.21, 'Ids_DC': 3, 'Ids_Keithley': 4,
                            'Field_reversal_warning': True})
        trace, x, y = next(model.traces('Ids_Keithley'))
        np.testing.assert_allclose(x.values, [0.11, 0.21])
        np.testing.assert_allclose(y.values, [2, 4])
        np.testing.assert_allclose(trace.columns['Field_reversal_warning'].values, [np.nan, 1], equal_nan=True)
        self.assertIn('Readback reversal', model.legendEntries)

    def test_saved_flags_survive_cache_and_older_files(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'data.csv'
            path.write_text('B_measured_T,Ids_DC,Field_reversal_warning,Field_quality\n'
                            '0,1,False,ok\n1,2,True,readback_reversal\n'
                            '2,3,False,readback_reversal_offset\n', encoding='utf-8')
            for cache in (None, SnapshotCache()):
                result = load_curve(path, 'B_measured_T', 'Ids_DC', cache)
                np.testing.assert_array_equal(result.reversal_warning, [False, True, True])
            path.write_text('B_measured_T,Ids_DC\n0,1\n1,2\n', encoding='utf-8')
            np.testing.assert_array_equal(load_curve(path, 'B_measured_T', 'Ids_DC').reversal_warning, [False, False])

    def test_history_flags_are_kept_on_trace(self):
        model = PlotModel()
        model.setTraces([{'x': [1, 2], 'y': [3, 4], 'reversal_warning': [False, True]}])
        trace, _, _ = next(model.traces('Ids_DC'))
        np.testing.assert_array_equal(trace.columns['Field_reversal_warning'].values, [0, 1])
        self.assertIn('Readback reversal', model.legendEntries)
    def test_live_snapshot_exports_warning_markers(self):
        from matplotlib.figure import Figure
        from app.png_export import _draw_reversal_markers
        model = PlotModel()
        model.beginRun({'x_axis': 'B-field', 'channels': ['Ids_DC']})
        model.appendRecord({'x': 0.1, 'Ids_DC': 1})
        model.appendRecord({'x': 0.2, 'Ids_DC': 3, 'Field_reversal_warning': True})
        snapshot = model.exportSnapshot()
        trace = snapshot['traces'][0]
        self.assertFalse(trace['reversal_warning'].flags.writeable)
        model.appendRecord({'x': 0.3, 'Ids_DC': 5})
        self.assertEqual(len(trace['reversal_warning']), 2)
        fig = Figure()
        axis = fig.subplots()
        marker = _draw_reversal_markers(axis, trace, 1)
        self.assertEqual(marker.get_label(), 'Readback reversal')
        np.testing.assert_allclose(marker.get_offsets(), [[0.2, 3]])
        self.assertIsNone(_draw_reversal_markers(axis, {'x': [0], 'y': [1]}, 1))
    def test_overlay_export_labels_warning_once_for_both_channels(self):
        from app.png_export import build_overlay_figure
        model = PlotModel()
        model.beginRun({'x_axis': 'B-field', 'channels': ['Ids_DC', 'Ids_Keithley']})
        model.appendRecord({'x': 0.1, 'B_Keithley_T': 0.11, 'Ids_DC': 1e-9, 'Ids_Keithley': 2e-9,
                            'Field_reversal_warning': True})
        model.setYAxes('Ids_DC', 'Ids_Keithley', 'overlay')
        fig = build_overlay_figure(model.exportSnapshot())
        axis = fig.axes[0]
        self.assertEqual(len(axis.collections), 2)
        labels = [t.get_text() for t in axis.get_legend().get_texts()]
        self.assertEqual(labels.count('Readback reversal'), 1)
        np.testing.assert_allclose(axis.collections[1].get_offsets()[0, 0], .11)
