"""ME31-XDXX0400 四通道温度采集上位机。"""

from __future__ import annotations

import sys

__all__ = ["main"]


def main() -> None:
    from PySide6.QtWidgets import QApplication

    from .gui import MainWindow

    app = QApplication(sys.argv)
    app.setApplicationName("temperature-acq")
    window = MainWindow()
    window.show()
    sys.exit(app.exec())
