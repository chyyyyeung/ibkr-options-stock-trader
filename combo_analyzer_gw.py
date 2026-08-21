"""期权组合分析器 — Gateway 版入口 (新版本)。

与 combo_analyzer.py 的区别 (同 main_gw.py vs main.py):
  1. 启动前置环境变量 IBKR_USE_GATEWAY=1 → 连 IB Gateway (端口 4002 实盘 /
     4001 模拟) 而非 TWS, 并收紧行情订阅占用 (见 config.py)。
  2. 独立入口文件名 → single_instance 只杀本脚本的旧进程, **不会**影响
     正在运行的 combo_analyzer.py (旧版), 两者可同时存在、对比。
  3. 日志写入 logs/combo_app_gw_YYYY-MM-DD.log, 与旧版日志分开。

环境变量必须在 import config / combo_analyzer 之前设置 —— config.USE_GATEWAY
在模块导入时求值。
"""

import os
import sys
from datetime import datetime

# ── 必须最先设置: 切到 Gateway 端口 + 轻量订阅 ──────────────────────
os.environ["IBKR_USE_GATEWAY"] = "1"

# Under pythonw there is no console — redirect to a SEPARATE gw log so the
# new (Gateway) version's logs don't mix with the old combo_analyzer.py logs.
# 必须在 import combo_analyzer 之前完成, 这样它顶部的日志重定向块会因
# sys.stdout 已非 None 而跳过, 不会再开旧版日志。
if sys.stdout is None or sys.stderr is None:
    _log_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
    os.makedirs(_log_dir, exist_ok=True)
    _log_file = open(
        os.path.join(_log_dir, f"combo_app_gw_{datetime.now():%Y-%m-%d}.log"),
        "a", encoding="utf-8", buffering=1,
    )
    sys.stdout = sys.stderr = _log_file
    print(f"\n──── Combo analyzer (Gateway) started "
          f"{datetime.now():%Y-%m-%d %H:%M:%S} ────")
else:
    os.system("")  # Enable ANSI on Windows (console mode only)

from PyQt5.QtWidgets import QApplication
from PyQt5.QtCore import Qt
from PyQt5.QtGui import QFont, QIcon

from single_instance import kill_previous_instances
from combo_analyzer import ComboAnalyzerWindow, APP_ICON


def main():
    # Kill only leftover instances of THIS script (combo_analyzer_gw.py) — the
    # running combo_analyzer.py (old version) and main*.py are NOT touched.
    kill_previous_instances(__file__)

    QApplication.setAttribute(Qt.AA_EnableHighDpiScaling, True)
    QApplication.setAttribute(Qt.AA_UseHighDpiPixmaps, True)

    app = QApplication(sys.argv)
    from config import FONT_FAMILY, FONT_SIZE
    app.setFont(QFont(FONT_FAMILY, FONT_SIZE))
    if os.path.exists(APP_ICON):
        app.setWindowIcon(QIcon(APP_ICON))

    window = ComboAnalyzerWindow()
    # 标题加 [GW] 标记, 一眼区分新旧两个窗口 (连接后由窗口内部按 USE_GATEWAY 保持)
    window.setWindowTitle(window.windowTitle() + "  [GW 新版]")
    window.show()
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
