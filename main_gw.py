"""IBKR 点价交易 — Gateway 版入口 (新版本, 用于收盘后模拟盘测试)。

与 main.py 的区别:
  1. 启动前置环境变量 IBKR_USE_GATEWAY=1 → 连 IB Gateway (端口 4002 实盘 /
     4001 模拟) 而非 TWS, 并收紧行情订阅占用 (见 config.py)。
  2. 独立入口文件名 → single_instance 只杀掉本脚本的旧进程, **不会**影响
     正在运行的 main.py (旧版), 两者可同时存在、对比。
  3. 日志写入 logs/app_gw_YYYY-MM-DD.log, 与旧版日志分开。

环境变量必须在 import config / main_window 之前设置 —— config.USE_GATEWAY
在模块导入时求值。
"""

import os
import sys
from datetime import datetime

# ── 必须最先设置: 切到 Gateway 端口 + 轻量订阅 ──────────────────────
os.environ["IBKR_USE_GATEWAY"] = "1"

# Under pythonw there is no console — redirect output to a SEPARATE log so
# the new (Gateway) version's logs don't mix with the old main.py logs.
if sys.stdout is None or sys.stderr is None:
    _log_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
    os.makedirs(_log_dir, exist_ok=True)
    _log_file = open(
        os.path.join(_log_dir, f"app_gw_{datetime.now():%Y-%m-%d}.log"),
        "a", encoding="utf-8", buffering=1,
    )
    sys.stdout = sys.stderr = _log_file
    print(f"\n──── App (Gateway) started {datetime.now():%Y-%m-%d %H:%M:%S} ────")
else:
    os.system("")  # Enable ANSI on Windows (console mode only)

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
    # Kill only leftover instances of THIS script (main_gw.py) — the running
    # main.py (old version) and stock_trader.py are NOT touched.
    kill_previous_instances(__file__)

    QApplication.setAttribute(Qt.AA_EnableHighDpiScaling, True)
    QApplication.setAttribute(Qt.AA_UseHighDpiPixmaps, True)

    app = QApplication(sys.argv)
    from config import FONT_FAMILY, FONT_SIZE
    app.setFont(QFont(FONT_FAMILY, FONT_SIZE))
    if os.path.exists(APP_ICON):
        app.setWindowIcon(QIcon(APP_ICON))

    window = MainWindow()
    # 标题加 [GW] 标记, 一眼区分新旧两个窗口
    window.setWindowTitle(window.windowTitle() + "  [GW 新版]")
    window.show()

    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
