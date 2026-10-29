from __future__ import annotations

import sys


def main(argv=None):
    arguments = list(sys.argv[1:] if argv is None else argv)
    if "--widgets" in arguments:
        arguments.remove("--widgets")
        from PySide6.QtWidgets import QApplication
        from app.app_identity import configure_qapp, set_windows_app_id
        from app.ui.main_window import MainWindow
        set_windows_app_id()
        application = QApplication.instance() or QApplication([sys.argv[0], *arguments])
        configure_qapp(application)
        window = MainWindow()
        window.show()
        return application.exec()
    if "--history" in arguments:
        from app.quick.history_app import main as run
    else:
        from app.quick.application import main as run
    return run(arguments)


def __getattr__(name):
    # Preserve notebook imports without pulling Widgets into the Quick process.
    if name in ("MainWindow", "launch_in_notebook"):
        from app.ui import main_window
        return getattr(main_window, name)
    raise AttributeError(name)


if __name__ == "__main__":
    raise SystemExit(main())
