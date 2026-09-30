import unittest
from app.workers.cosweep_timing import LiveCoSweepTiming


class LiveCoSweepTimingTests(unittest.TestCase):
    def test_initial_estimate_and_cleanup_reserve(self):
        eta = LiveCoSweepTiming(100, 205, 5)
        self.assertEqual(eta.remaining_seconds, 205)
        self.assertEqual(eta.total_seconds, 205)

    def test_startup_not_treated_as_steady_point_cost(self):
        eta = LiveCoSweepTiming(100, 205, 5)
        eta.update(1, 60)
        self.assertEqual(eta.remaining_seconds, 203)
        for point in range(2, 7):
            eta.update(point, 60 + (point - 1) * 3)
        self.assertAlmostEqual(eta.remaining_seconds, 94 * 3 + 5)
        self.assertAlmostEqual(eta.total_seconds, 75 + 94 * 3 + 5)

    def test_recent_window_adapts_without_replaying_entire_run(self):
        eta = LiveCoSweepTiming(100, 205, 5)
        elapsed = 0
        for point in range(1, 41):
            elapsed += 2 if point <= 20 else 4
            eta.update(point, elapsed)
        self.assertAlmostEqual(eta.remaining_seconds, 60 * 4 + 5)
        self.assertLessEqual(len(eta.intervals), 20)

    def test_active_elapsed_excludes_pause_and_duplicate_updates(self):
        eta = LiveCoSweepTiming(10, 25, 5)
        eta.update(1, 2)
        eta.update(2, 4)
        before = eta.remaining_seconds
        eta.update(2, 4)  # pause/resume without completed point
        self.assertEqual(eta.remaining_seconds, before)
        eta.update(3, 6)
        self.assertEqual(eta.remaining_seconds, 19)

    def test_last_point_keeps_cleanup_until_explicit_completion(self):
        eta = LiveCoSweepTiming(1, 7, 5)
        eta.update(1, 2)
        self.assertEqual(eta.remaining_seconds, 5)
        eta.finish(8)
        self.assertEqual(eta.remaining_seconds, 0)
        self.assertEqual(eta.total_seconds, 8)


class WorkerTimingTests(unittest.TestCase):
    def test_worker_excludes_actual_pause_wait(self):
        from unittest.mock import patch
        from app.models import CoParams, Connections, SaveRoot
        from app.workers.cosweep import CoSweepWorker
        worker = CoSweepWorker(CoParams(), SaveRoot(), Connections())
        worker._timing_start = 0
        worker.request_pause(True)
        clock = [10.0]
        samples = []
        worker.timing_updated.connect(samples.append)
        def resume(_seconds):
            clock[0] += 100
            worker.request_pause(False)
        with patch("app.workers.cosweep.time.monotonic", side_effect=lambda: clock[0]), patch("app.workers.base.time.sleep", side_effect=resume):
            worker.check_abort_pause()
            clock[0] += 2
            worker._timing_completed = 1
            worker._emit_timing("sampling")
        self.assertEqual([sample["phase"] for sample in samples], ["paused", "sampling", "sampling"])
        self.assertEqual([sample["elapsed"] for sample in samples], [10, 10, 12])
