from __future__ import annotations

import sys

from PyQt6.QtWidgets import QApplication

from app.ui.main_window import MainWindow, launch_in_notebook


if __name__ == "__main__":
    app = QApplication(sys.argv)
    w = MainWindow()
    w.show()
    sys.exit(app.exec())
