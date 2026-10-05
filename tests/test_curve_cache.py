import unittest
import numpy as np
from app.curve_cache import SnapshotCache


class SnapshotCacheTests(unittest.TestCase):
    def test_same_snapshot_reused_and_changed_fingerprint_reloaded(self):
        cache = SnapshotCache(max_bytes=1024)
        first = cache.get(("file", 1), lambda: np.array([1., 2.]))
        second = cache.get(("file", 1), lambda: self.fail("Unchanged snapshot was reread"))
        changed = cache.get(("file", 2), lambda: np.array([1., 2., 3.]))
        self.assertIs(first, second)
        self.assertEqual(changed.tolist(), [1, 2, 3])

    def test_memory_limit_evicts_oldest_snapshot(self):
        cache = SnapshotCache(max_bytes=32)
        cache.get("old", lambda: np.array([1., 2., 3.]))
        cache.get("new", lambda: np.array([4., 5., 6.]))
        reloaded = cache.get("old", lambda: np.array([10., 20., 30.]))
        self.assertEqual(reloaded.tolist(), [10, 20, 30])
        self.assertLessEqual(cache.bytes_used, 32)


if __name__ == "__main__":
    unittest.main()
