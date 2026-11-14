"""Offline tests of live timing and background planning, without instrument I/O."""
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from PySide6.QtCore import QCoreApplication

from app.quick.parameters import defaults
from app.quick.timing import LiveTiming, MeasurementTiming, _EstimateTask


class History:
    def __init__(self, result=None):
        self.result = result
        self.calls = []

    def lookup(self, kind, params, save):
        self.calls.append((kind, params, save))
        return self.result


class Clock:
    now = 0.
    def __call__(self):
        return self.now


class Signal:
    def __init__(self):
        self.calls = []
    def emit(self, *args):
        self.calls.append(args)


class LiveTimingTests(unittest.TestCase):
    def test_learns_after_three_intervals_and_holds_when_stalled(self):
        live = LiveTiming(0.)
        for i in range(1, 4):
            live.observe(i/10, i*2.)
            self.assertIsNone(live.remaining(i*2.))
        live.observe(.4, 8.)
        self.assertAlmostEqual(live.remaining(8., cleanup=3.), 15.)
        self.assertIsNone(live.remaining(24.))
        live.observe(.5, 25.)
        self.assertIsNotNone(live.remaining(25.))

    def test_progress_regression_does_not_revive_stalled_eta(self):
        live = LiveTiming(0.)
        for i in range(1, 5): live.observe(i/10, i*2.)
        self.assertIsNone(live.remaining(24.))
        live.observe(.3, 24.)
        self.assertEqual(live.fraction, .4)
        self.assertEqual(live.completed, 5)
        self.assertEqual(live.last_point, 8.)
        self.assertIsNone(live.remaining(24.))


class MeasurementTimingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QCoreApplication.instance() or QCoreApplication([])

    def pump_until(self, predicate, timeout=3.):
        deadline = time.monotonic() + timeout
        while not predicate() and time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(.005)
        self.app.processEvents()
        self.assertTrue(predicate(), 'background task did not complete')

    def test_historical_result_selected_with_canonical_kind(self):
        values = defaults(0)
        history = History({'seconds': 42., 'note': 'Comparable run', 'source': 'history.csv'})
        signal = Signal()
        save = SimpleNamespace(device_id='test')
        _EstimateTask(7, (0, values, None, save, '1000'), history, signal).run()
        generation, result = signal.calls[0]
        self.assertEqual(generation, 7)
        self.assertEqual(history.calls[0][0], 'vds_sweep')
        self.assertEqual(result['seconds'], 42.)
        self.assertEqual(result['source'], 'Historical measurements')
        self.assertEqual(result['historyPath'], 'history.csv')
        self.assertGreater(result['points'], 0)

    def test_async_generation_rejects_obsolete_result(self):
        started, release = threading.Event(), threading.Event()
        def estimate(mode, values, **kwargs):
            if values['delay'] == 1.:
                started.set()
                if not release.wait(3.): raise RuntimeError('test timeout')
            return dict(seconds=values['delay'], points=10, cleanup_seconds=0., note='model')
        timing = MeasurementTiming()
        timing._history = History()
        save = SimpleNamespace(device_id='test')
        a = defaults(0); a['delay'] = 1.
        b = defaults(0); b['delay'] = 2.
        try:
            with patch('app.quick.timing.estimate_measurement_time', side_effect=estimate):
                timing.request(0, a, None, save, '1000'); timing._timer.stop(); timing._launch_pending()
                self.pump_until(started.is_set)
                timing.request(0, b, None, save, '1000'); timing._timer.stop(); timing._launch_pending()
                release.set()
                self.pump_until(lambda: timing.plan.get('seconds') == 2.)
                self.assertEqual(timing.plan['seconds'], 2.)
                self.assertNotIn('~1 s', timing.summary)
        finally:
            release.set()
            timing._timer.stop(); timing._tick.stop()

    def test_live_lifecycle_and_paused_state(self):
        clock = Clock()
        timing = MeasurementTiming(clock=clock)
        save = SimpleNamespace(device_id='test')
        timing.request(0, defaults(0), None, save, '1000')
        timing._timer.stop()
        timing._complete(timing._generation, dict(seconds=100., points=10, cleanup_seconds=2., note='test', source='Parameter model'))
        self.assertIn('1 min 40 s', timing.summary)
        timing.begin(); timing.set_phase('running')
        for i in range(1, 5):
            clock.now = i*2.
            timing.record({})
        self.assertIn('Remaining:', timing.summary)
        timing.set_phase('running', paused=True)
        self.assertIn('Remaining time pending', timing.summary)
        timing.set_phase('running', paused=False)
        clock.now = 40.
        timing._render()
        self.assertIn('Waiting for the next point', timing.detail)
        timing.finish('finished')
        self.assertIn('Last run: finished', timing.summary)
        self.assertIn('40 s', timing.summary)
        timing._tick.stop()

    def test_service_preview_edit_and_fake_worker_lifecycle(self):
        import tempfile
        from app.models import SaveRoot
        from app.quick.measurements import MeasurementService
        from tests.test_quick_measurements import FakeManager, FakeWorker
        with tempfile.TemporaryDirectory() as directory:
            manager = FakeManager()
            service = MeasurementService(manager, SaveRoot(base=directory),
                selected_backend_callable=lambda: '1000', worker_factories={0: FakeWorker})
            try:
                self.pump_until(lambda: service.timing.plan.get('seconds') is not None)
                baseline = service.timing.plan['seconds']
                service.setValue('delay', '1.5')
                self.pump_until(lambda: service.timing.plan.get('seconds') is not None and
                                service.timing.plan['seconds'] > baseline)
                service.start()
                self.pump_until(lambda: service.worker is not None)
                worker = service.worker
                self.pump_until(lambda: worker.entered.is_set() and service.timing._live.completed >= 1)
                self.assertTrue(service.timing._active)
                self.assertIn('Elapsed:', service.timing.summary)
                worker.finish_cleanup.set()
                self.pump_until(lambda: not service.active)
                self.assertFalse(service.timing._active)
                self.assertIn('Last run:', service.timing.summary)
            finally:
                if service.worker is not None:
                    service.worker.request_stop()
                    service.worker.finish_cleanup.set()
                    self.pump_until(lambda: service.worker_thread is None)
                service.timing._timer.stop(); service.timing._tick.stop()
                service.deleteLater()
                self.app.processEvents()

    def test_continuous_fraction_accounts_for_reverse_and_condition(self):
        clock = Clock()
        timing = MeasurementTiming(clock=clock)
        v = defaults(5)
        v['measurement_mode'] = 'continuous'; v['start_field_t'] = -1.; v['stop_field_t'] = 1.
        v['conditions'].append(dict(v['conditions'][0], name='Con2'))
        timing.request(5, v, None, SimpleNamespace(device_id='test'), '1000')
        timing._timer.stop()
        timing._complete(timing._generation, dict(seconds=100., points=None, cleanup_seconds=0., note='test'))
        timing.begin()
        for i, (field, direction, condition) in enumerate(((-1., 'forward', 1), (1., 'forward', 1),
                                                             (0., 'backward', 1), (-1., 'backward', 1),
                                                             (0., 'forward', 2)), 1):
            clock.now = float(i)
            timing.record(dict(B_measured_T=field, Direction=direction, Condition_index=condition))
        self.assertAlmostEqual(timing._live.fraction, .625)
        timing.finish('finished'); timing._tick.stop()


if __name__ == '__main__':
    unittest.main()
