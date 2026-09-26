"""续墨 —— 应用入口。"""

import sys

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import QApplication

import theme as T
from ui import Window


def main() -> int:
    QApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
    )
    app = QApplication(sys.argv)
    app.setApplicationName("续墨")
    app.setApplicationDisplayName("续墨")

    base = QFont("Microsoft YaHei UI", 9)
    base.setHintingPreference(QFont.HintingPreference.PreferFullHinting)
    app.setFont(base)
    app.setStyleSheet(T.build_qss())

    win = Window()
    win.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
