from __future__ import annotations

import sys

from PyQt6.QtWidgets import QApplication

from app.app_identity import configure_qapp, set_windows_app_id
from app.ui.main_window import MainWindow, launch_in_notebook


if __name__ == "__main__":
    set_windows_app_id()
    app = QApplication(sys.argv)
    configure_qapp(app)
    w = MainWindow()
    w.show()
    sys.exit(app.exec())
