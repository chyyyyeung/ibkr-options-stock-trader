"""IBKR 点价交易 — Entry point."""

import sys
import os
from datetime import datetime

# Under pythonw there is no console — redirect all print/traceback output
# to logs/app_YYYY-MM-DD.log so [ORDER ERROR] etc. are not lost
if sys.stdout is None or sys.stderr is None:
    _log_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
    os.makedirs(_log_dir, exist_ok=True)
    _log_file = open(
        os.path.join(_log_dir, f"app_{datetime.now():%Y-%m-%d}.log"),
        "a", encoding="utf-8", buffering=1,
    )
    sys.stdout = sys.stderr = _log_file
    print(f"\n──── App started {datetime.now():%Y-%m-%d %H:%M:%S} ────")
else:
    # Enable ANSI on Windows (console mode only)
    os.system("")

# 全局崩溃捕获: 未处理异常/Qt致命/硬崩溃都落日志, 槽函数异常不再静默闪退
from crash_handler import install_crash_handler
install_crash_handler(sys.stderr)

from PyQt5.QtWidgets import QApplication
from PyQt5.QtCore import Qt
from PyQt5.QtGui import QFont, QIcon

from single_instance import kill_previous_instances
from main_window import MainWindow

_APP_DIR = os.path.dirname(os.path.abspath(__file__))
APP_ICON = os.path.join(_APP_DIR, "app.ico")


def main():
    # Kill leftover instances of THIS script (frees clientId in TWS);
    # stock_trader.py instances are not touched
    kill_previous_instances(__file__)

    # High DPI support
    QApplication.setAttribute(Qt.AA_EnableHighDpiScaling, True)
    QApplication.setAttribute(Qt.AA_UseHighDpiPixmaps, True)

    app = QApplication(sys.argv)
    from config import FONT_FAMILY, FONT_SIZE
    app.setFont(QFont(FONT_FAMILY, FONT_SIZE))
    if os.path.exists(APP_ICON):
        app.setWindowIcon(QIcon(APP_ICON))

    window = MainWindow()
    window.show()

    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
