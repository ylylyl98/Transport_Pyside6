"""History selection isolation, worker cancellation and immutable export tests."""
import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

if os.environ.get('TRANSPORT_OFFLINE_VERIFICATION') != '1':
    raise unittest.SkipTest('Use verify_offline.py')

import numpy as np
from PySide6 import QtCore

from app.analysis_csv_export import csv_bytes
from app.curve_cache import SnapshotCache
from app.curve_history import discover_history
from app.quick.history import HistoryBackend, _defaults, _read_display


class FakeExporter(QtCore.QObject):
    completed = QtCore.Signal(object)

    def __init__(self):
        super().__init__()
        self.pool = QtCore.QThreadPool(self)
        self.jobs = []

    @property
    def busy(self):
        return bool(self.jobs)

    def submit_snapshot(self, snapshot, output=None, cut_only=False, **kwargs):
        self.jobs.append((snapshot, output, cut_only, kwargs))
        return str(len(self.jobs))

    def shutdown(self):
        pass


class QuickHistoryTests(unittest.TestCase):
    def setUp(self):
        self.scratch = tempfile.TemporaryDirectory()
        self.root = Path(self.scratch.name)
        self.addCleanup(self.scratch.cleanup)
        self.exporter = FakeExporter()
        self.settings = QtCore.QSettings(str(self.root / "settings.ini"), QtCore.QSettings.Format.IniFormat)
        self.backend = HistoryBackend(self.root, settings=self.settings, exporter=self.exporter, autostart=False)
        self.addCleanup(self.backend.shutdown)

    def write(self, name="vds_sweep.csv", kind="vds_sweep", text=None):
        path = self.root / name
        path.write_text(text or "Vds,Ids_DC,Direction\nV,A,\n0,-2,forward\n1,4,forward\n", encoding="utf-8")
        path.with_name(path.stem + "_metadata.json").write_text(json.dumps({"measurement": kind, "created_at": "2026-10-26T12:30:00", "status": "finished"}), encoding="utf-8")
        return path

    def drain(self, predicate=None):
        end = time.monotonic() + 5
        while time.monotonic() < end:
            QtCore.QCoreApplication.processEvents()
            if predicate() if predicate else not self.backend.busy:
                return
            time.sleep(.005)
        self.fail("History worker did not settle: " + self.backend.status)

    def test_six_types_are_isolated_filters_keep_checked_paths(self):
        first = self.write()
        second = self.write("gate.csv", "gate_scan", "Doping,Ids_DC,Direction\nV,A,\n0,3,forward\n")
        self.backend.refresh()
        self.drain()
        self.assertEqual(len(self.backend.measurementTypes), 6)
        self.assertEqual([r["path"] for r in self.backend.files], [str(first)])
        self.backend.setChecked(str(first), True)
        self.drain()
        self.backend.setOption("search", "absent")
        self.assertEqual(self.backend.files, [])
        self.assertEqual(len(self.backend.export_snapshot()["records"]), 1)
        self.backend.selectType("gate_scan")
        self.drain()
        self.assertFalse(self.backend.exportReady)
        self.backend.setOption("search", "")
        self.assertEqual(self.backend.files[0]["path"], str(second))
        self.assertFalse(self.backend.files[0]["checked"])
        self.backend.selectType("vds_sweep")
        self.drain()
        self.assertTrue(self.backend.files[0]["checked"])

    def test_refresh_reuses_header_and_numeric_cache_without_resetting_scroll(self):
        path = self.write()
        self.backend.refresh()
        self.drain()
        self.backend.setChecked(str(path), True)
        self.drain()
        self.backend.setOption("scroll", 91)
        with patch("app.curve_history._header", side_effect=AssertionError("Unchanged CSV reread")):
            self.backend.refresh()
            self.drain()
            self.backend.setOption("normalization", "max_abs")
            self.drain()
        self.assertEqual(self.backend.scrollPosition, 91)
        self.assertGreater(self.backend._cache.bytes_used, 0)
        np.testing.assert_equal(self.backend.export_snapshot()["traces"][0]["y"], [-.5, 1])

    def test_search_reuses_display_metadata_and_refresh_updates_sidecar_conditions(self):
        path = self.write()
        self.backend.refresh()
        self.drain()
        with patch.object(HistoryBackend, '_file_summary', side_effect=AssertionError('Search reformatted metadata')):
            self.backend.setOption('search', 'vds')
            self.backend.setOption('search', '')
            self.backend.setChecked(str(path), True)
            self.drain()
        sidecar = path.with_name(path.stem + '_metadata.json')
        metadata = json.loads(sidecar.read_text(encoding='utf-8'))
        metadata['params'] = {'vtg_set': 7.5}
        sidecar.write_text(json.dumps(metadata), encoding='utf-8')
        self.backend.refresh()
        self.drain()
        self.assertIn('7.5', self.backend.files[0]['conditions'])

    def test_unchanged_automatic_refresh_is_quiet_and_reuses_display_rows(self):
        path = self.write()
        self.backend.refresh()
        self.drain()
        self.backend.setChecked(str(path), True)
        self.drain()
        snapshot = self.backend._snapshot
        notifications = []
        self.backend.changed.connect(lambda: notifications.append('changed'))
        self.backend.busyChanged.connect(lambda: notifications.append('busy'))
        self.backend.filesChanged.connect(lambda: notifications.append('files'))
        with patch.object(HistoryBackend, '_file_summary', side_effect=AssertionError('Unchanged row formatted')), \
                patch.object(self.backend, '_filter', side_effect=AssertionError('Unchanged list rebuilt')), \
                patch('app.quick.history._read_display', side_effect=AssertionError('Unchanged plot rebuilt')):
            self.backend._automatic_refresh()
            self.assertTrue(self.backend.exportReady)
            self.drain(lambda: not self.backend._index_active)
        self.assertEqual(notifications, [])
        self.assertIs(self.backend._snapshot, snapshot)
        self.assertFalse(self.backend._timer.isActive())

    def test_search_burst_coalesces_and_synchronous_clear_cancels_pending_filter(self):
        self.write()
        self.backend.refresh()
        self.drain()
        searches, changes = [], []
        self.backend.searchChanged.connect(lambda: searches.append(self.backend.search))
        self.backend.changed.connect(lambda: changes.append(True))
        with patch.object(self.backend, '_filter', wraps=self.backend._filter) as filtering:
            for text in ('n', 'no', 'no-match'):
                self.backend.queueSearch(text)
            self.assertEqual(self.backend.search, 'no-match')
            self.assertEqual(filtering.call_count, 0)
            self.assertEqual(searches, ['n', 'no', 'no-match'])
            self.assertEqual(changes, [])
            self.assertTrue(self.backend._search_timer.isActive())
            # Exercise the timeout directly so the coalescing assertion does
            # not depend on machine speed or arbitrary event-loop sleeps.
            self.backend._apply_search()
            self.assertEqual(filtering.call_count, 1)
            self.assertEqual(changes, [True])
            self.assertEqual(self.backend.files, [])
            stored = json.loads(self.settings.value(self.backend._key()))
            self.assertEqual(stored['search'], 'no-match')
            self.backend.queueSearch('pending')
            self.backend.clearFilters()
            self.assertFalse(self.backend._search_timer.isActive())
            self.assertEqual(self.backend.search, '')
            self.assertEqual(searches[-1], '')
            self.assertEqual(len(self.backend.files), 1)
            self.backend.queueSearch('pending again')
            self.backend.setOption('search', 'vds')
            self.assertFalse(self.backend._search_timer.isActive())
            self.assertEqual(self.backend.search, 'vds')
            self.assertEqual(searches[-1], 'vds')
            self.assertEqual(len(self.backend.files), 1)

    def test_pending_search_is_saved_by_session_and_cannot_leak_to_new_folder(self):
        self.write()
        self.backend.refresh()
        self.drain()
        self.backend.queueSearch('no-match')
        session = self.backend.capture_session()
        self.assertEqual(session['search'], 'no-match')
        self.assertFalse(self.backend._search_timer.isActive())
        self.assertEqual(self.backend.files, [])
        self.backend.queueSearch('another pending value')
        folder = self.root / 'another-device'
        folder.mkdir()
        self.backend.setFolder(str(folder))
        self.drain()
        self.assertFalse(self.backend._search_timer.isActive())
        self.assertEqual(self.backend.search, '')
        self.assertEqual(json.loads(self.settings.value(self.backend._key(), '{}')).get('search', ''), '')

    def test_search_notifications_follow_folder_and_session_restoration(self):
        self.write()
        self.backend.refresh()
        self.drain()
        searches = []
        self.backend.searchChanged.connect(lambda: searches.append(self.backend.search))
        self.backend.setOption('search', 'saved')
        session = self.backend.capture_session()
        self.backend.setFolder(str(self.root / 'another-device'))
        self.drain()
        self.assertEqual(searches[-1], '')
        self.backend.setFolder(str(self.root))
        self.drain()
        self.assertEqual(searches[-1], 'saved')
        self.backend.clearFilters()
        self.backend.restore_session(session)
        self.drain()
        self.assertEqual(searches[-1], 'saved')
        self.assertEqual(self.backend.search, 'saved')

    def test_same_folder_refresh_applies_pending_search_without_waiting_for_timer(self):
        self.write()
        self.backend.refresh()
        self.drain()
        self.backend.queueSearch('no-match')
        self.backend.setFolder(str(self.root))
        self.drain()
        self.assertEqual(self.backend.files, [])
        self.assertFalse(self.backend._search_timer.isActive())
        self.assertEqual(json.loads(self.settings.value(self.backend._key()))['search'], 'no-match')

    def test_unselected_growth_does_not_replot_but_selected_sidecar_and_removal_do(self):
        selected = self.write()
        other = self.write('other.csv')
        self.backend.refresh()
        self.drain()
        self.backend.setChecked(str(selected), True)
        self.drain()
        snapshot = self.backend._snapshot
        with patch('app.quick.history._read_display', wraps=_read_display) as reads:
            with other.open('a', encoding='utf-8') as stream:
                stream.write('2,8,forward\n')
            self.backend.refresh()
            self.drain()
            self.assertEqual(reads.call_count, 0)
            self.assertIs(self.backend._snapshot, snapshot)
            sidecar = selected.with_name(selected.stem + '_metadata.json')
            metadata = json.loads(sidecar.read_text(encoding='utf-8'))
            metadata['params'] = {'vtg_set': 7.5}
            sidecar.write_text(json.dumps(metadata), encoding='utf-8')
            self.backend.refresh()
            self.drain()
            self.assertEqual(reads.call_count, 1)
            row = next(row for row in self.backend.files if row['path'] == str(selected))
            self.assertIn('7.5', row['conditions'])
            with selected.open('a', encoding='utf-8') as stream:
                stream.write('2,12,forward\n')
            self.backend.refresh()
            self.drain()
            self.assertEqual(reads.call_count, 2)
            self.assertEqual(self.backend._snapshot['traces'][0]['y'][-1], 12)
            selected.unlink()
            self.backend.refresh()
            self.drain()
            self.assertEqual(reads.call_count, 3)
            self.assertEqual(self.backend._snapshot['records'], [])

    def test_selected_file_reclassified_by_sidecar_clears_previous_type_plot(self):
        path = self.write()
        self.backend.refresh()
        self.drain()
        self.backend.setChecked(str(path), True)
        self.drain()
        sidecar = path.with_name(path.stem + '_metadata.json')
        metadata = json.loads(sidecar.read_text(encoding='utf-8'))
        metadata['measurement'] = 'gate_scan'
        sidecar.write_text(json.dumps(metadata), encoding='utf-8')
        self.backend._automatic_refresh()
        self.drain(lambda: not self.backend._index_active and not self.backend.busy)
        self.assertEqual(self.backend.files, [])
        self.assertEqual(self.backend._snapshot['records'], [])
        self.assertFalse(self.backend.exportReady)

    def test_slow_automatic_poll_waits_for_completion_and_explicit_folder_request_survives(self):
        path = self.write()
        self.backend.refresh()
        self.drain()
        self.backend.setChecked(str(path), True)
        self.drain()
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        from app.quick.history import _index_display

        def gated_index(*args):
            entered.set()
            if not release.wait(3):
                raise RuntimeError('Test did not release history index')
            return _index_display(*args)

        self.backend._auto_refresh = True
        self.backend._timer.setInterval(20)
        with patch('app.quick.history._index_display', side_effect=gated_index):
            self.backend._automatic_refresh()
            self.assertTrue(entered.wait(2))
            self.assertFalse(self.backend._timer.isActive())
            self.assertFalse(self.backend.busy)
            self.assertTrue(self.backend.exportReady)
            self.backend._automatic_refresh()
            self.assertFalse(self.backend._index_pending)
            folder = self.root / 'next'
            folder.mkdir()
            next_file = folder / 'vds_sweep_next.csv'
            next_file.write_text('Vds,Ids_DC,Direction\nV,A,\n0,3,forward\n', encoding='utf-8')
            self.backend.setFolder(str(folder))
            self.assertTrue(self.backend._index_pending)
            self.assertTrue(self.backend.busy)
            release.set()
            self.drain()
            self.assertEqual([row['path'] for row in self.backend.files], [str(next_file)])
            self.assertTrue(self.backend._timer.isActive())
        self.backend._timer.stop()

    def test_pan_updates_ranges_without_rebuilding_history_and_saves_latest_view(self):
        path = self.write()
        self.backend.refresh()
        self.drain()
        self.backend.setChecked(str(path), True)
        self.drain()
        changed, ranges = [], []
        self.backend.changed.connect(lambda: changed.append(True))
        self.backend.rangeChanged.connect(lambda: ranges.append(True))
        for i in range(30):
            self.backend.plotModel.set_limits('Ids_DC', (i, i+2), (-2, 5))
        self.assertEqual(changed, [])
        self.assertEqual(len(ranges), 30)
        self.assertEqual(self.backend.rangeState['x']['bounds'], [29., 31.])
        # Capturing a session before the persistence timer fires must include
        # the final viewport, including immediately before reload/close.
        session = self.backend.capture_session()
        self.assertEqual(session['views']['vds_sweep']['xlim'], [29., 31.])
        self.assertEqual(session['limits']['xlim'], [29., 31.])
        stored = json.loads(self.settings.value(self.backend._key()))
        self.assertEqual(stored['views']['vds_sweep']['xlim'], [29., 31.])

    def test_new_type_uses_its_own_auto_bounds_then_recalls_saved_pan(self):
        vds = self.write()
        gate = self.write('gate.csv', 'gate_scan', 'Doping,Ids_DC,Direction\nV,A,\n100,1000,forward\n101,2000,forward\n')
        self.backend.refresh()
        self.drain()
        self.backend.setChecked(str(vds), True)
        self.drain()
        self.backend.plotModel.set_limits('Ids_DC', (0, 1), (-2, 5))
        self.backend.selectType('gate_scan')
        self.backend.setChecked(str(gate), True)
        self.drain()
        auto = self.backend.capture_view()
        self.assertFalse(auto['manual_x'])
        self.assertFalse(auto['manual_y'][0])
        self.assertLess(auto['xlim'][0], 100)
        self.assertGreater(auto['xlim'][1], 101)
        self.assertLess(auto['ylim'][0], 1000)
        self.assertGreater(auto['ylim'][1], 2000)
        self.backend.plotModel.set_limits('Ids_DC', (100.2, 100.8), (1200, 1800))
        self.backend.selectType('vds_sweep')
        self.drain()
        self.assertEqual(self.backend.plotModel.limits(), ((0., 1.), (-2., 5.)))
        self.backend.selectType('gate_scan')
        self.drain()
        self.assertEqual(self.backend.export_snapshot()['view']['xlim'], [100.2, 100.8])

    def test_pan_during_same_type_worker_refresh_keeps_latest_bounds(self):
        path = self.write()
        self.backend.refresh()
        self.drain()
        self.backend.setChecked(str(path), True)
        self.drain()
        # Reproduce a result prepared before the user pans, published after it.
        result = _read_display(self.backend._selected(), dict(self.backend.state), 'vds_sweep', self.backend._cache)
        self.backend.plotModel.set_limits('Ids_DC', (.2, .8), (-1, 3))
        self.backend._publish(*result)
        self.assertEqual(self.backend.plotModel.limits(), ((.2, .8), (-1., 3.)))

    def test_missing_folder_warning_survives_empty_plot_publication(self):
        self.backend.setFolder(str(self.root / "missing"))
        self.drain()
        self.assertEqual(self.backend.emptyState["title"], "Folder not found")
        self.assertTrue(self.backend._index_warnings)
        self.assertIn(self.backend._index_warnings[0], self.backend.statusDetails)
        self.assertFalse(self.backend.exportReady)

    def test_filter_and_selection_counts_clear_without_losing_hidden_selection(self):
        path = self.write()
        self.backend.refresh()
        self.drain()
        self.assertEqual(self.backend.emptyState["title"], "Select files to compare")
        self.backend.setChecked(str(path), True)
        self.drain()
        self.backend.setOption("search", "no-match")
        self.assertEqual(self.backend.selectedCount, 1)
        self.assertEqual(self.backend.typeFileCount, 1)
        self.assertEqual(self.backend.files, [])
        self.assertEqual(self.backend.emptyState["action"], "filters")
        self.backend.clearFilters()
        self.assertEqual(len(self.backend.files), 1)
        self.assertTrue(self.backend.files[0]["checked"])
        self.backend.clearSelection()
        self.drain()
        self.assertEqual(self.backend.selectedCount, 0)
        self.assertFalse(self.backend.exportReady)

    def test_export_default_names_suffix_and_busy_readiness(self):
        path = self.write()
        self.backend.refresh()
        self.drain()
        self.backend.setChecked(str(path), True)
        self.drain()
        proposed = Path(QtCore.QUrl(self.backend.suggestedExportUrl("both")).toLocalFile())
        self.assertEqual(proposed.parent, self.root)
        self.assertEqual(proposed.name, "vds_sweep.png")
        self.backend._plot_active = True
        self.assertFalse(self.backend.exportReady)
        self.backend._plot_active = False
        self.backend.exportView("both", str(self.root / "custom"))
        self.assertEqual(self.exporter.jobs[-1][1].suffix, ".png")
        self.assertIn("1 selected files", self.backend.selectionSummary)
        self.assertEqual(self.backend.axisLabel("B_measured_T"), "Measured B (T)")

    def test_old_selection_result_cannot_publish_after_newer_revision(self):
        self.write()
        self.backend._generation, self.backend._revision = 3, 5
        self.backend._plot_active = True
        with patch.object(self.backend, "_publish") as publish:
            self.backend._finished("plot", (3, 4), (None, None, None), "")
            publish.assert_not_called()
        self.backend._index_active = True
        self.backend._finished("index", 2, ([], []), "stale error")
        self.assertNotIn("stale error", self.backend.status)

    def test_pending_revision_coalesces_and_latest_choice_wins(self):
        path = self.write()
        self.backend.refresh()
        self.drain()
        self.backend.setChecked(str(path), True)
        self.backend.setOption("normalization", "max_abs")
        self.backend.setOption("normalization", "min_max")
        self.drain()
        snapshot = self.backend.export_snapshot()
        self.assertEqual(snapshot["view"]["normalization"], "min_max")
        np.testing.assert_equal(snapshot["traces"][0]["y"], [0, 1])

    def test_export_uses_displayed_snapshot_even_after_source_changes(self):
        path = self.write()
        self.backend.refresh()
        self.drain()
        self.backend.setChecked(str(path), True)
        self.drain()
        before = self.backend.export_snapshot()
        expected = csv_bytes(before, "curve")
        path.write_text("Vds,Ids_DC,Direction\nV,A,\n0,999,forward\n", encoding="utf-8")
        self.backend.exportView("both", "")
        saved = self.exporter.jobs[0][0]
        self.assertEqual(csv_bytes(saved, "curve"), expected)
        self.assertFalse(saved["traces"][0]["y"].flags.writeable)
        self.assertIsInstance(saved["view"]["ylim"][0], (float, int))

    def test_map_ratio_exact_cuts_and_normalization_preserve_physical_heatmap(self):
        self.write("map.csv", "map_2d", "Vtg,Vbg,I_drag_X,I_drive_X\nV,V,A,A\n0,1,2,2\n1,1,8,2\n0,2,4,2\n1,2,16,2\n")
        records, _ = discover_history(self.root)
        state = {**_defaults("map_2d"), "signal": "Drag ratio X", "normalization": "max_abs", "auto_axes": False, "fixed_axis": "Vbg", "fixed_value": 1}
        snapshot, traces, primary = _read_display(records, state, "map_2d", SnapshotCache())
        np.testing.assert_equal(primary.z, [[1, 4], [2, 8]])
        np.testing.assert_equal(traces[0].y, [.25, 1])
        state["fixed_value"] = 1.1
        absent, _, _ = _read_display(records, state, "map_2d", SnapshotCache())
        self.assertEqual(absent["traces"], [])
        self.assertIn("no measured", " ".join(absent["warnings"]))
        self.assertEqual(snapshot["map"]["signal_unit"], "")

    def test_absent_map_cut_preserves_heatmap_export_and_lists_measured_values(self):
        path = self.write("map.csv", "map_2d", "Vtg,Vbg,Ids_DC\nV,V,A\n0,1,2\n1,1,4\n0,2,3\n1,2,8\n")
        self.backend.refresh()
        self.drain()
        self.backend.selectType("map_2d")
        self.backend.setChecked(str(path), True)
        self.drain()
        self.assertTrue(self.backend.cutReady)
        self.backend.setOption("fixed_axis", "Vbg")
        self.backend.setOption("fixed_value", 1.1)
        self.drain()
        self.assertTrue(self.backend.exportReady)
        self.assertFalse(self.backend.cutReady)
        self.assertIn("Available Vbg: 1, 2", self.backend.cutEmptyText)
        self.assertTrue(self.backend.suggestedExportUrl("heatmap").endswith("map_heatmap.png"))

    def test_one_axis_map_automatically_loads_as_curve(self):
        path = self.write("single.csv", "sweep_1d")
        sidecar = path.with_name(path.stem + "_metadata.json")
        meta = json.loads(sidecar.read_text(encoding="utf-8"))
        meta["params"] = {"axis_fast": "Vds", "axis_slow": "None"}
        sidecar.write_text(json.dumps(meta), encoding="utf-8")
        records, _ = discover_history(self.root)
        snapshot, traces, primary = _read_display(records, _defaults("map_2d"), "map_2d", SnapshotCache())
        self.assertIsNone(primary)
        self.assertEqual(snapshot["view"]["map_y"], "None (1D)")
        self.assertEqual(snapshot["x_name"], "Vds")
        self.assertEqual(len(traces), 1)

    def test_session_and_settings_restore_selection_and_directory_following(self):
        path = self.write()
        self.backend.refresh()
        self.drain()
        self.backend.setChecked(str(path), True)
        self.drain()
        self.backend.setOption("normalization", "max_abs")
        self.backend.setOption("scroll", 33)
        self.drain()
        session = self.backend.capture_session()
        clone = HistoryBackend(self.root, settings=self.settings, exporter=FakeExporter(), autostart=False)
        self.addCleanup(clone.shutdown)
        self.assertEqual(clone.state["normalization"], "max_abs")
        self.assertIn(str(path), clone._checked["vds_sweep"])
        self.assertEqual(session["scroll"], 33)
        different = self.root / "another-device"
        clone.handleCommand({"command": "folder", "folder": str(different)})
        clone.restore_session(session)
        self.assertEqual(clone.folder, str(different))
        self.assertEqual(clone._checked["vds_sweep"], set())

    def test_range_preferences_and_reload_wait_for_accepted_export(self):
        path = self.write()
        self.backend.refresh()
        self.drain()
        self.backend.setChecked(str(path), True)
        self.drain()
        self.backend.setRange("x", False, "0.1", "0.8")
        view = self.backend.capture_view()
        self.assertEqual(view["xlim"], [.1, .8])
        self.assertTrue(view["manual_x"])
        self.backend.setRange("x", True, "", "")
        self.assertFalse(self.backend.capture_view()["manual_x"])
        self.backend.exportView("both", "")
        self.assertTrue(self.backend.can_close())  # normal close drains detached work
        self.backend.handleCommand({"command": "reload"})
        self.assertFalse(self.backend.can_close())  # reload also drains the old renderer
        self.exporter.jobs.clear()
        self.assertTrue(self.backend.can_close())

    def test_invalid_ranges_keep_last_valid_view_and_clear_only_corrected_axis(self):
        self.backend.setRange("x", False, "-1", "1")
        self.backend.setRange("y", False, "-2", "2")
        before = self.backend.capture_view()
        self.backend.setRange("x", False, "3", "2")
        self.backend.setRange("y", False, "nan", "2")
        self.assertEqual(self.backend.capture_view(), before)
        self.assertIn("increasing", self.backend.rangeState["x"]["error"])
        self.assertTrue(self.backend.rangeState["y"]["error"])
        self.backend.setRange("x", False, "-3", "3")
        self.assertEqual(self.backend.rangeState["x"]["error"], "")
        self.assertTrue(self.backend.rangeState["y"]["error"])
        self.backend.setRange("y", True, "", "")
        self.assertEqual(self.backend.rangeState["y"]["error"], "")
        self.assertEqual(self.backend.capture_view()["xlim"], [-3, 3])

    def test_dialog_folder_falls_back_to_recent_existing_directory(self):
        recent = self.root / "exports"
        recent.mkdir()
        self.settings.setValue("quick_history/export_folder", str(recent))
        self.backend._folder = self.root / "missing"
        chosen = Path(QtCore.QUrl(self.backend.dialogFolderUrl()).toLocalFile())
        self.assertEqual(chosen, recent)

    def test_theme_uses_and_persists_shared_quick_preference(self):
        self.assertFalse(self.backend.dark)
        self.backend.setDark(True)
        self.assertTrue(self.settings.value("quick/theme/dark", False, type=bool))
        clone = HistoryBackend(self.root, settings=self.settings, exporter=FakeExporter(), autostart=False)
        self.addCleanup(clone.shutdown)
        self.assertTrue(clone.dark)
        clone.setDark(False)
        self.assertFalse(self.settings.value("quick/theme/dark", True, type=bool))

    def test_qml_window_loads_without_errors_and_exposes_native_plot(self):
        from PySide6 import QtQml, QtQuickControls2
        from app.quick.plot import register_types
        QtQuickControls2.QQuickStyle.setStyle("Fusion")
        path = self.write()
        self.backend.refresh()
        self.drain()
        self.backend.setChecked(str(path), True)
        self.drain()
        register_types()
        engine = QtQml.QQmlApplicationEngine()
        warnings = []
        engine.warnings.connect(lambda errors: warnings.extend(str(e) for e in errors))
        engine.rootContext().setContextProperty("historyBackend", self.backend)
        engine.rootContext().setContextProperty("historyConnected", False)
        source = Path(__file__).resolve().parents[1] / "app" / "quick" / "qml" / "HistoryWindow.qml"
        engine.load(QtCore.QUrl.fromLocalFile(str(source)))
        self.assertEqual(len(engine.rootObjects()), 1, "\n".join(warnings))
        window = engine.rootObjects()[0]
        QtCore.QCoreApplication.processEvents()
        self.assertIsNotNone(engine.rootObjects()[0].findChild(QtCore.QObject, "nativePlot"))
        self.backend.setDark(True)
        QtCore.QCoreApplication.processEvents()
        theme_toggle = window.findChild(QtCore.QObject, "historyThemeToggle")
        self.assertTrue(theme_toggle.property("checked"))
        for name in ("historyMainPlot", "historyCutPlot"):
            pane = window.findChild(QtCore.QObject, name)
            self.assertTrue(pane.property("darkMode"))
        self.backend.setDark(False)
        self.assertEqual(warnings, [])
        # Let the coalesced paint timer publish the latest curves.
        end = time.monotonic() + .1
        while time.monotonic() < end:
            QtCore.QCoreApplication.processEvents()
            time.sleep(.005)
        image = engine.rootObjects()[0].grabWindow()
        if not image.isNull():
            destination = Path(__file__).resolve().parents[1] / "tmp" / "verification" / "quick_history.png"
            destination.parent.mkdir(parents=True, exist_ok=True)
            image.save(str(destination))
        map_path = self.write("map.csv", "map_2d", "Vtg,Vbg,Ids_DC\nV,V,A\n0,1,2\n1,1,4\n0,2,3\n1,2,8\n")
        self.backend.refresh()
        self.drain()
        self.backend.selectType("map_2d")
        self.backend.setChecked(str(map_path), True)
        self.drain()
        end = time.monotonic() + .1
        while time.monotonic() < end:
            QtCore.QCoreApplication.processEvents()
            time.sleep(.005)
        self.assertTrue(self.backend.hasMap)
        self.assertEqual(warnings, [])
        window = engine.rootObjects()[0]
        window.setWidth(1100)
        window.setHeight(700)
        QtCore.QCoreApplication.processEvents()
        tabs = window.findChild(QtCore.QObject, "historyPlotTabs")
        self.assertTrue(tabs.property("visible"))
        canvas = window.findChild(QtCore.QObject, "nativePlot")
        self.assertGreater(canvas.property("height"), 150)
        tabs.setProperty("currentIndex", 1)
        QtCore.QCoreApplication.processEvents()
        self.backend.setRange("x", False, "-2", "2")
        lower = window.findChild(QtCore.QObject, "historyRange_x_lower")
        lower.setProperty("text", "3")
        QtCore.QMetaObject.invokeMethod(lower, "editingFinished")
        self.assertEqual(lower.property("text"), "3")
        self.assertTrue(self.backend.rangeState["x"]["error"])
        self.assertEqual(self.backend.capture_view()["map_xlim"], [-2, 2])
        lower.setProperty("text", "-1")
        QtCore.QMetaObject.invokeMethod(lower, "editingFinished")
        self.assertEqual(self.backend.rangeState["x"]["error"], "")
        self.assertEqual(warnings, [])
        image = engine.rootObjects()[0].grabWindow()
        if not image.isNull():
            image.save(str(destination.with_name("quick_history_map.png")))
        engine.rootObjects()[0].setProperty("visible", False)
        del engine

    def test_launcher_reload_keeps_closed_viewer_closed_and_uses_quick_entry(self):
        from app.quick.history_launcher import HistoryLauncher
        launcher = HistoryLauncher(lambda: self.root, offline=True)
        self.addCleanup(launcher.shutdown)
        launcher.reload()
        self.assertFalse(launcher.busy)
        self.assertEqual(launcher.process.state(), QtCore.QProcess.ProcessState.NotRunning)
        with patch.object(launcher.process, "start"):
            launcher.open()
        self.assertEqual(launcher.process.arguments()[3:5], ["app.quick.history_app", "--folder"])
        self.assertTrue(launcher.prepare_shutdown())

    def test_standalone_process_ready_and_clean_close_use_existing_ipc(self):
        import sys
        process = QtCore.QProcess()
        process.setWorkingDirectory(str(Path(__file__).resolve().parents[1]))
        # Configure isolated child settings before importing the real entry.
        script = ("import sys; from PySide6 import QtCore; "
                  "QtCore.QSettings.setPath(QtCore.QSettings.IniFormat,QtCore.QSettings.UserScope,sys.argv[1]); "
                  "from app.quick.history_app import main; "
                  "sys.exit(main(['--folder',sys.argv[1],'--connected']))")
        process.start(sys.executable, ["-u", "-B", "-c", script, str(self.root)])
        self.addCleanup(lambda: process.kill() if process.state() != QtCore.QProcess.NotRunning else None)
        self.assertTrue(process.waitForStarted(3000))
        output = bytearray()
        end = time.monotonic() + 10
        messages = []
        while time.monotonic() < end:
            process.waitForReadyRead(100)
            output.extend(bytes(process.readAllStandardOutput()))
            while b"\n" in output:
                line, _, remainder = output.partition(b"\n")
                output = bytearray(remainder)
                messages.append(json.loads(line))
            if any(m.get("event") == "ready" for m in messages):
                break
        self.assertTrue(any(m.get("event") == "ready" for m in messages), bytes(process.readAllStandardError()).decode(errors="replace"))
        ready = next(m for m in messages if m.get("event") == "ready")
        self.assertEqual(ready["hardware_modules"], [])
        self.assertFalse(ready["widgets_loaded"])
        process.write(b'{"command":"close"}\n')
        process.closeWriteChannel()
        finished = process.waitForFinished(5000)
        output.extend(bytes(process.readAllStandardOutput()))
        self.assertTrue(finished, output.decode(errors="replace") + bytes(process.readAllStandardError()).decode(errors="replace"))
        self.assertIn(b'"event": "close_ready"', output)
        self.assertEqual(process.exitCode(), 0)


if __name__ == "__main__":
    unittest.main()
