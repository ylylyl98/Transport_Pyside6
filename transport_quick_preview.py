"""Hardware-blocked Qt Quick preview with isolated settings and synthetic plots."""
from pathlib import Path
import argparse
import sys
import tempfile

ROOT = Path(__file__).resolve().parent


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, add_help=False)
    parser.add_argument("--child", action="store_true")
    parser.add_argument("--state-directory", type=Path)
    parser.add_argument("--offscreen", action="store_true")
    options, args = parser.parse_known_args(argv)
    if not options.child:
        # The supervisor owns no Qt objects, controllers, or hardware sessions.
        import subprocess
        with tempfile.TemporaryDirectory(prefix="transport-quick-preview-") as directory:
            command = [sys.executable, "-B", str(Path(__file__).resolve()), "--child", "--state-directory", directory, *args]
            if options.offscreen:
                command.append("--offscreen")
            while True:
                result = subprocess.call(command, cwd=ROOT)
                if result != 75:
                    return result
                for flag in ("--mode", "--panel"):
                    if flag in command:
                        index = command.index(flag)
                        del command[index:index + 2]
    if options.state_directory is None:
        raise SystemExit("Preview child requires isolated settings")
    from app.offline_sandbox import OfflineSandbox
    sandbox = OfflineSandbox(ROOT, preview=True, state_directory=options.state_directory,
                             offscreen=options.offscreen or "--screenshot" in args or "--check" in args)
    sandbox.install_hardware_guards()
    from app.quick.application import main as run
    return run(["--preview", *args])


if __name__ == "__main__":
    raise SystemExit(main())
