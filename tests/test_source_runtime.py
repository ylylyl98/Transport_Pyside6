import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from app import source_runtime


class SourceRuntimeTests(unittest.TestCase):
    def test_names_are_stable_for_case_and_trailing_separator(self):
        with tempfile.TemporaryDirectory(prefix="测试 space ") as root:
            self.assertEqual(source_runtime.root_hash(root), source_runtime.root_hash(root.upper() + os.sep))
            self.assertTrue(source_runtime.names(root)["running"].endswith(".Running"))

    def test_helper_modes_skip_instance(self):
        for flag in ("--preview", "--history", "--history-widgets", "--png-worker", "--verify-package", "--check"):
            self.assertTrue(source_runtime.should_skip([flag]))

    @unittest.skipUnless(sys.platform == "win32", "Windows kernel objects")
    def test_running_mutex_and_activation_across_processes(self):
        with tempfile.TemporaryDirectory(prefix="测试 space ") as root:
            runtime = source_runtime.SourceRuntime(root)
            self.assertTrue(runtime.begin())
            self.addCleanup(runtime.close)
            code = ("from app.source_runtime import SourceRuntime; import sys; "
                    "print(SourceRuntime(sys.argv[1]).begin())")
            from PySide6.QtCore import QProcess
            process = QProcess()
            process.start(sys.executable, ["-B", "-c", code, root])
            self.assertTrue(process.waitForFinished(10000))
            self.assertEqual(process.exitCode(), 0, bytes(process.readAllStandardError()).decode())
            self.assertEqual(bytes(process.readAllStandardOutput()).decode().strip(), "False")
            self.assertTrue(runtime.kernel.wait(runtime.activate, 0))
            self.assertFalse(runtime.kernel.wait(runtime.activate, 0))

    @unittest.skipUnless(sys.platform == "win32", "Windows kernel objects")
    def test_busy_launch_exits_without_waiting_and_signals(self):
        from PySide6.QtCore import QProcess
        with tempfile.TemporaryDirectory(prefix="测试 space ") as root:
            runtime = source_runtime.SourceRuntime(root)
            k = runtime.kernel
            launch = k.mutex(runtime.names["launch"])
            event = k.event(runtime.names["activate"])
            self.assertTrue(k.wait(launch, 0))
            try:
                code = "from app.source_runtime import SourceRuntime; import sys; print(SourceRuntime(sys.argv[1]).begin())"
                process = QProcess()
                process.start(sys.executable, ["-B", "-c", code, root])
                self.assertTrue(process.waitForFinished(5000))
                self.assertEqual(process.exitCode(), 0, bytes(process.readAllStandardError()).decode())
                self.assertEqual(bytes(process.readAllStandardOutput()).decode().strip(), "False")
                self.assertTrue(k.wait(event, 0))
            finally:
                k.k.ReleaseMutex(launch)
                k.k.CloseHandle(launch)
                k.k.CloseHandle(event)


if __name__ == "__main__": unittest.main()
