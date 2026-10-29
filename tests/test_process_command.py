"""Release process contracts: frozen workers, source compatibility and isolation."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

if os.environ.get("TRANSPORT_OFFLINE_VERIFICATION") != "1":
    raise unittest.SkipTest("Run through verify_offline.py")

from app.offline_sandbox import audit_policy
from app.process_command import worker_command
import transport_UI


class WorkerCommandTests(unittest.TestCase):
    def test_frozen_children_use_console_worker_and_preserve_argument_boundaries(self):
        executable = str(Path(tempfile.gettempdir()) / "Transport release" / "Transport.exe")
        folder = str(Path(tempfile.gettempdir()) / "data with spaces")
        with patch.object(sys, "frozen", True, create=True), patch.object(sys, "executable", executable):
            for mode in ("history", "history-widgets", "png-worker"):
                with self.subTest(mode=mode):
                    command = worker_command(mode, "--folder", folder)
                    self.assertEqual(command, [str(Path(executable).with_name("TransportWorker.exe")),
                                               "--" + mode, "--folder", folder])
            # A worker can itself start the detached renderer from the same bundle.
            with patch.object(sys, "executable", command[0]):
                self.assertEqual(worker_command("png-worker")[0], command[0])

    def test_source_history_retains_unbuffered_python_ipc(self):
        root = Path(__file__).resolve().parents[1]
        with patch.object(sys, "frozen", False, create=True), patch.object(sys, "executable", "pythonw.exe"), patch.object(Path, "exists", return_value=True):
            self.assertEqual(worker_command("history", "--connected"),
                             ["python.exe", "-u", "-B", "-m", "app.quick.history_app", "--connected"])
            self.assertEqual(worker_command("history-widgets", "--connected"),
                             ["python.exe", "-u", str(root / "transport_history_viewer.py"), "--connected"])

    def test_unknown_mode_fails_before_launch(self):
        with self.assertRaises(ValueError):
            worker_command("arbitrary-command")

    def test_frozen_history_launchers_select_worker_without_starting_hardware(self):
        from app.quick.history_launcher import HistoryLauncher
        from app.ui.history_viewer_launcher import HistoryViewerLauncher
        folder = Path(tempfile.gettempdir()) / "history with spaces"
        with patch.object(sys, "frozen", True, create=True):
            for launcher_type, mode in ((HistoryLauncher, "history"),
                                        (HistoryViewerLauncher, "history-widgets")):
                with self.subTest(mode=mode):
                    launcher = launcher_type(lambda: folder)
                    self.addCleanup(launcher.deleteLater)
                    with patch.object(launcher.process, "start") as start:
                        launcher._start_viewer()
                    start.assert_called_once_with()
                    self.assertEqual(Path(launcher.process.program()).name, "TransportWorker.exe")
                    self.assertEqual(launcher.process.arguments(), ["--" + mode, "--folder", str(folder), "--connected"])

    def test_frozen_png_keeps_detached_stdio_and_reload_owner(self):
        from app.ui.png_export_process import PngExportProcess
        queue = PngExportProcess()
        self.addCleanup(queue.shutdown)
        process = Mock(pid=12345)
        with patch.object(sys, "frozen", True, create=True), patch("app.ui.png_export_process.subprocess.Popen", return_value=process) as popen:
            queue._launch_worker(after_pid=42)
        command = popen.call_args.args[0]
        options = popen.call_args.kwargs
        self.assertEqual(command[1:], ["--png-worker", "--spool", str(queue.spool),
                                      "--owner-pid", str(os.getpid()), "--after-pid", "42"])
        self.assertEqual(Path(command[0]).name, "TransportWorker.exe")
        self.assertEqual(options["executable"], command[0])
        self.assertEqual(options["stdin"], subprocess.DEVNULL)
        self.assertEqual(options["stdout"], subprocess.DEVNULL)
        self.assertTrue(options["close_fds"])
        if os.name == "nt":
            self.assertEqual(options["creationflags"], subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP)
        else:
            self.assertTrue(options["start_new_session"])


class EntryRoutingTests(unittest.TestCase):
    def test_worker_and_smoke_routes_consume_only_their_dispatch_argument(self):
        routes = (("--png-worker", "transport_png_exporter"),
                  ("--history-widgets", "transport_history_viewer"),
                  ("--verify-package", "app.package_smoke"))
        for flag, module in routes:
            with self.subTest(flag=flag):
                run = Mock(return_value=17)
                arguments = [flag, "--folder", "data with spaces"]
                with patch.dict(sys.modules, {module: SimpleNamespace(main=run)}):
                    self.assertEqual(transport_UI.main(arguments), 17)
                run.assert_called_once_with(["--folder", "data with spaces"])
                self.assertEqual(arguments[0], flag)

    def test_quick_history_route_retains_existing_arguments(self):
        run = Mock(return_value=0)
        arguments = ["--history", "--folder", "data", "--connected"]
        with patch.dict(sys.modules, {"app.quick.history_app": SimpleNamespace(main=run)}):
            self.assertEqual(transport_UI.main(arguments), 0)
        run.assert_called_once_with(arguments)


class FrozenOfflinePolicyTests(unittest.TestCase):
    def test_only_exact_frozen_renderer_is_permitted(self):
        root = Path(__file__).resolve().parents[1]
        application = str(root / "Transport.exe")
        worker = str(root / "TransportWorker.exe")
        spool = str(Path(tempfile.gettempdir()) / "transport-png-offline-test")
        command = [worker, "--png-worker", "--spool", spool, "--owner-pid", str(os.getpid())]
        with patch.object(sys, "frozen", True, create=True), patch.object(sys, "executable", application):
            policy = audit_policy(root)
            for allowed in (command, command + ["--after-pid", "123"]):
                policy("subprocess.Popen", (worker, allowed, str(root), {}))
                policy("subprocess.Popen", (worker, subprocess.list2cmdline(allowed), str(root), {}))
            rejected = [
                command[:1] + ["--history"] + command[2:],
                command[:1] + [str(root / "transport_png_exporter.py")] + command[2:],
                command + ["--extra"],
                command[:-1] + [str(os.getpid() + 1)],
                command[:3] + [str(root / "transport-png-outside-temp")] + command[4:],
                command[:3] + [str(Path(tempfile.gettempdir()) / "other-prefix")] + command[4:],
                command + ["--after-pid", "not-a-pid"],
                [application] + command[1:],
            ]
            for altered in rejected:
                with self.subTest(command=altered), self.assertRaises(RuntimeError):
                    policy("subprocess.Popen", (worker, altered, str(root), {}))
            for executable, directory in ((application, root), (worker, root.parent),
                                          (str(root.parent / "TransportWorker.exe"), root)):
                with self.subTest(executable=executable, directory=directory), self.assertRaises(RuntimeError):
                    policy("subprocess.Popen", (executable, [executable] + command[1:], str(directory), {}))
            with self.assertRaises(RuntimeError):
                audit_policy(root, preview=True)("subprocess.Popen", (worker, command, str(root), {}))
        with patch.object(sys, "frozen", False, create=True), self.assertRaises(RuntimeError):
            audit_policy(root)("subprocess.Popen", (worker, command, str(root), {}))
