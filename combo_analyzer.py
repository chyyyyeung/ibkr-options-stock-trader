"""期权组合价格分析器 (combo_analyzer.py) — 独立程序, clientId=12.

展示选中标的的多腿期权组合 (蝶式 / 铁鹰 / 垂直价差 / 跨式 / 宽跨 / 日历...) 的:
  • 当前组合净价 (借记 / 贷记) 与各腿最新价
  • 组合价格随时间的历史变化 —— 由各腿历史 K 线 close 按时间戳对齐合成。
    券商通常不直接提供「组合」的历史价, 但可由各腿历史价算出。

入口: pythonw combo_analyzer.py  (或 start_combo.bat)。与期权 GUI / 正股 client 并行,
用独立 clientId=12。
"""

import os
import sys
import json
import time
import threading
from datetime import datetime

# pythonw 无控制台 — 把 print/traceback 重定向到 logs/combo_app_YYYY-MM-DD.log
if sys.stdout is None or sys.stderr is None:
    _log_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
    os.makedirs(_log_dir, exist_ok=True)
    _log_file = open(
        os.path.join(_log_dir, f"combo_app_{datetime.now():%Y-%m-%d}.log"),
        "a", encoding="utf-8", buffering=1,
    )
    sys.stdout = sys.stderr = _log_file
    print(f"\n──── Combo analyzer started {datetime.now():%Y-%m-%d %H:%M:%S} ────")
else:
    os.system("")

from PyQt5.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QGridLayout, QLabel,
    QLineEdit, QPushButton, QComboBox, QDoubleSpinBox, QSpinBox, QTableWidget,
    QTableWidgetItem, QHeaderView, QAbstractItemView, QApplication, QFrame,
    QSplitter, QMessageBox, QCheckBox,
)
from PyQt5.QtCore import Qt, pyqtSignal, QTimer
from PyQt5.QtGui import QFont, QIcon, QColor

from config import (
    IBKR_COMBO_CLIENT_ID, CHART_TIMEFRAMES, DEFAULT_SYMBOLS,
    COLOR_BG, COLOR_BG_DARK, COLOR_BG_PANEL, COLOR_TEXT, COLOR_TEXT_DIM,
    COLOR_GREEN, COLOR_RED, COLOR_ACCENT, COLOR_BORDER,
    IBKR_LIVE_PORT, IBKR_PAPER_PORT, IBKR_GW_LIVE_PORT, IBKR_GW_PAPER_PORT,
    USE_GATEWAY,
)


def _display_port(is_live: bool) -> int:
    """连接端口 (仅用于状态栏显示); 跟随 USE_GATEWAY 在 TWS/Gateway 间切换。"""
    if USE_GATEWAY:
        return IBKR_GW_LIVE_PORT if is_live else IBKR_GW_PAPER_PORT
    return IBKR_LIVE_PORT if is_live else IBKR_PAPER_PORT
from models import TradingMode, OptionInfo, ComboLegInfo
from ibkr_engine import IBKREngine
from single_instance import kill_previous_instances
from widgets.strategy_defs import STRATEGY_REGISTRY, StrategyType
from widgets.combo_pricing import (
    resolved_legs, compute_combo_series, compute_combo_ohlc, leg_sign,
    combo_price_from_prices, auto_assign_strikes,
)

_APP_DIR = os.path.dirname(os.path.abspath(__file__))
APP_ICON = os.path.join(_APP_DIR, "app.ico")

# 组合分析展示顺序 (排除 CUSTOM — 无固定腿)
_STRATEGY_ORDER = [
    StrategyType.LONG_CALL_BUTTERFLY, StrategyType.LONG_PUT_BUTTERFLY,
    StrategyType.IRON_CONDOR, StrategyType.IRON_BUTTERFLY,
    StrategyType.STRADDLE, StrategyType.STRANGLE,
    StrategyType.BULL_CALL_SPREAD, StrategyType.BEAR_PUT_SPREAD,
    StrategyType.BULL_PUT_SPREAD, StrategyType.BEAR_CALL_SPREAD,
    StrategyType.CALENDAR_SPREAD,
]


class ComboAnalyzerWindow(QMainWindow):
    """期权组合价格分析器主窗口。"""

    # 工作线程 → GUI 线程
    _combo_ready = pyqtSignal(object)   # dict 结果
    _combo_error = pyqtSignal(str)
    _chain_ready = pyqtSignal(object)   # dict: expirations, strikes, price
    _chain_error = pyqtSignal(str)
    _status = pyqtSignal(str)
    _order_placed = pyqtSignal(int, object, str)  # order_id, pending-dict, mode
    _today_ready = pyqtSignal(object)   # dict: ohlc 合并K线结果

    def __init__(self):
        super().__init__()
        self.setWindowTitle("IBKR 期权组合分析器")
        self.setMinimumSize(960, 640)
        self.resize(1280, 860)

        self.engine = IBKREngine()
        self._expirations: list[str] = []
        self._strikes: list[float] = []
        self._underlying_price = 0.0
        self._strike_spins: dict[str, QDoubleSpinBox] = {}
        self._expiry_combos: dict[str, QComboBox] = {}
        self._plot_widget = None       # 延迟创建 (pyqtgraph)
        self._curve = None
        self._candle = None            # 合并 K 线 (CandlestickItem)
        self._bottom_axis = None

        # 实时录制当日组合价 (用各腿实时盘口合成, 不依赖历史行情权限)
        self._recording = False
        self._record_series: list[dict] = []
        self._record_legs: list[dict] = []
        self._record_symbol = ""

        # 组合持仓 (原子组, 只整组平仓/加仓; 持久化以便重启恢复 —— IBKR 不保留组合分组)
        self._groups_file = os.path.join(_APP_DIR, "combo_positions.json")
        self._groups: list[dict] = self._load_groups()
        self._pending: dict[int, dict] = {}   # order_id -> {group, mode}
        self._subscribed_keys: set[str] = set()
        self._group_seq = max((g.get("id", 0) for g in self._groups), default=0)

        self._build_ui()

        self.engine.bridge.connected.connect(self._on_connected)
        self.engine.bridge.disconnected.connect(self._on_disconnected)
        self.engine.bridge.error_received.connect(self._on_engine_error)
        self.engine.bridge.order_status_changed.connect(self._on_order_status)
        self.engine.bridge.order_rejected.connect(self._on_order_rejected)
        self._combo_ready.connect(self._on_combo_ready)
        self._combo_error.connect(self._on_combo_error)
        self._chain_ready.connect(self._on_chain_ready)
        self._chain_error.connect(self._on_chain_error)
        self._status.connect(lambda m: self.statusBar().showMessage(m))
        self._order_placed.connect(self._on_order_placed)
        self._today_ready.connect(self._on_today_ready)

        self._rebuild_params()
        self._refresh_positions_table()

        # 实时刷新: 当前组合各腿最新价 + 已有组合持仓的现价/盈亏
        self._pos_timer = QTimer(self)
        self._pos_timer.timeout.connect(self._periodic_refresh)
        self._pos_timer.start(1000)

        # 当日组合价录制定时器 (默认停止)
        self._record_timer = QTimer(self)
        self._record_timer.timeout.connect(self._record_sample)

        self.statusBar().showMessage("就绪 — 点击「连接」后加载期权链")

    # ── UI ───────────────────────────────────────────────────────────────

    def _build_ui(self):
        self.setStyleSheet(f"""
            QMainWindow, QWidget {{ background-color: {COLOR_BG}; color: {COLOR_TEXT}; }}
            QLineEdit, QComboBox, QDoubleSpinBox, QSpinBox {{
                background-color: {COLOR_BG_DARK}; color: {COLOR_TEXT};
                border: 1px solid {COLOR_BORDER}; border-radius: 3px; padding: 3px 6px;
            }}
            QPushButton {{
                background-color: {COLOR_BG_PANEL}; color: {COLOR_ACCENT};
                border: 1px solid {COLOR_BORDER}; border-radius: 3px;
                padding: 4px 14px; font-weight: bold;
            }}
            QPushButton:hover {{ background-color: {COLOR_ACCENT}; color: {COLOR_BG}; }}
            QPushButton:disabled {{ color: {COLOR_TEXT_DIM}; }}
            QTableWidget {{
                background-color: {COLOR_BG_DARK}; gridline-color: {COLOR_BORDER};
                border: 1px solid {COLOR_BORDER};
            }}
            QHeaderView::section {{
                background-color: {COLOR_BG_PANEL}; color: {COLOR_TEXT};
                border: none; padding: 4px;
            }}
            QLabel {{ background: transparent; }}
        """)

        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(6)

        # ── 顶栏: 标的 + 连接 ──
        top = QHBoxLayout()
        top.addWidget(QLabel("标的:"))
        self.symbol_input = QComboBox()
        self.symbol_input.setEditable(True)
        self.symbol_input.addItems(DEFAULT_SYMBOLS)
        self.symbol_input.setCurrentText("SPY")
        self.symbol_input.setMinimumWidth(110)
        top.addWidget(self.symbol_input)

        # 模式: 组合下单只能走真实 IBKR 引擎 (本地模拟不支持组合), 故仅 IBKR模拟盘 / 实盘。
        # 默认 IBKR模拟盘 (7497), 测试下单不动真钱。
        top.addWidget(QLabel("模式:"))
        self.mode_combo = QComboBox()
        for mode in (TradingMode.IBKR_PAPER, TradingMode.LIVE):
            self.mode_combo.addItem(mode.label, mode.value)
        self.mode_combo.setMinimumWidth(110)
        top.addWidget(self.mode_combo)

        self.connect_btn = QPushButton("连接")
        self.connect_btn.clicked.connect(self._on_connect_clicked)
        top.addWidget(self.connect_btn)

        self.load_chain_btn = QPushButton("加载期权链")
        self.load_chain_btn.clicked.connect(self._on_load_chain)
        self.load_chain_btn.setEnabled(False)
        top.addWidget(self.load_chain_btn)

        self.underlying_label = QLabel("标的价: --")
        self.underlying_label.setStyleSheet(f"color: {COLOR_ACCENT}; font-weight: bold;")
        top.addWidget(self.underlying_label)
        top.addStretch(1)
        root.addLayout(top)

        # ── 控制行: 策略 / 到期 / 中心 / 翼展 / 周期 / 数据类型 ──
        ctrl = QHBoxLayout()
        ctrl.addWidget(QLabel("策略:"))
        self.strategy_combo = QComboBox()
        for st in _STRATEGY_ORDER:
            self.strategy_combo.addItem(STRATEGY_REGISTRY[st].display_name, st)
        self.strategy_combo.setMinimumWidth(220)
        self.strategy_combo.currentIndexChanged.connect(self._rebuild_params)
        ctrl.addWidget(self.strategy_combo)

        ctrl.addWidget(QLabel("中心行权价:"))
        self.center_spin = QDoubleSpinBox()
        self.center_spin.setRange(0, 1_000_000)
        self.center_spin.setDecimals(2)
        self.center_spin.setSingleStep(1.0)
        ctrl.addWidget(self.center_spin)

        ctrl.addWidget(QLabel("翼展(档):"))
        self.width_spin = QSpinBox()
        self.width_spin.setRange(1, 50)
        self.width_spin.setValue(1)
        ctrl.addWidget(self.width_spin)

        self.autofill_btn = QPushButton("自动填行权价")
        self.autofill_btn.clicked.connect(self._autofill_strikes)
        ctrl.addWidget(self.autofill_btn)
        ctrl.addStretch(1)
        root.addLayout(ctrl)

        ctrl2 = QHBoxLayout()
        ctrl2.addWidget(QLabel("周期:"))
        self.timeframe_combo = QComboBox()
        for label in CHART_TIMEFRAMES:
            self.timeframe_combo.addItem(label)
        self.timeframe_combo.setCurrentText("5分钟")
        ctrl2.addWidget(self.timeframe_combo)

        ctrl2.addWidget(QLabel("数据:"))
        self.what_combo = QComboBox()
        self.what_combo.addItems(["TRADES", "MIDPOINT", "BID", "ASK"])
        self.what_combo.setToolTip("TRADES=成交价(可能稀疏); MIDPOINT=买卖中价(更连续)")
        ctrl2.addWidget(self.what_combo)

        self.compute_btn = QPushButton("计算组合历史价")
        self.compute_btn.clicked.connect(self._on_compute)
        self.compute_btn.setEnabled(False)
        ctrl2.addWidget(self.compute_btn)

        self.record_btn = QPushButton("▶ 录制当日")
        self.record_btn.setToolTip(
            "用各腿实时盘口中价每隔几秒合成组合净价, 累积成当日曲线。\n"
            "不依赖历史行情权限 — 只要有实时行情即可。"
        )
        self.record_btn.clicked.connect(self._toggle_record)
        ctrl2.addWidget(self.record_btn)

        self.today_btn = QPushButton("组合K线(蜡烛)")
        self.today_btn.setToolTip(
            "按上方「周期」拉各腿 OHLC, 合并成组合的蜡烛图(1分/5分/1时/2时/4时/日线...)。\n"
            "时间跨度随周期(如 5分钟≈1周、1小时≈1月、日线≈1年), 不再只限当日。\n"
            "滚轮缩放 / 拖动平移 / 右键菜单可自动缩放。需期权历史权限。"
        )
        self.today_btn.clicked.connect(self._on_today_kline)
        self.today_btn.setEnabled(False)
        ctrl2.addWidget(self.today_btn)
        ctrl2.addStretch(1)
        root.addLayout(ctrl2)

        # ── 各 strike_param / expiry_param 输入 (随策略动态重建) ──
        self.params_frame = QFrame()
        self.params_layout = QGridLayout(self.params_frame)
        self.params_layout.setContentsMargins(0, 0, 0, 0)
        self.params_layout.setHorizontalSpacing(10)
        root.addWidget(self.params_frame)

        # ── 主体: 左侧腿表 + 概要, 右侧图表 ──
        splitter = QSplitter(Qt.Horizontal)

        left = QWidget()
        left_l = QVBoxLayout(left)
        left_l.setContentsMargins(0, 0, 0, 0)

        self.summary_label = QLabel("当前组合净价: --")
        self.summary_label.setStyleSheet("font-size: 15px; font-weight: bold;")
        self.summary_label.setWordWrap(True)
        left_l.addWidget(self.summary_label)

        self.legs_table = QTableWidget(0, 5)
        self.legs_table.setHorizontalHeaderLabels(["方向", "类型", "行权价", "比例", "最新价"])
        self.legs_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.legs_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.legs_table.verticalHeader().setVisible(False)
        left_l.addWidget(self.legs_table)

        # ── 交易行: 整组开仓 (原生 BAG 组合单, 原子成交) ──
        trade = QHBoxLayout()
        trade.addWidget(QLabel("数量:"))
        self.qty_spin = QSpinBox()
        self.qty_spin.setRange(1, 1000)
        self.qty_spin.setValue(1)
        trade.addWidget(self.qty_spin)
        trade.addWidget(QLabel("净限价:"))
        self.limit_spin = QDoubleSpinBox()
        self.limit_spin.setRange(-100000, 100000)
        self.limit_spin.setDecimals(2)
        self.limit_spin.setSingleStep(0.05)
        self.limit_spin.setToolTip("组合净价限价 (借记为正, 贷记为负)")
        trade.addWidget(self.limit_spin)
        self.rth_check = QCheckBox("含盘前后")
        trade.addWidget(self.rth_check)
        self.open_buy_btn = QPushButton("买入开仓")
        self.open_buy_btn.clicked.connect(lambda: self._on_open_combo("BUY"))
        self.open_buy_btn.setEnabled(False)
        trade.addWidget(self.open_buy_btn)
        self.open_sell_btn = QPushButton("卖出开仓")
        self.open_sell_btn.clicked.connect(lambda: self._on_open_combo("SELL"))
        self.open_sell_btn.setEnabled(False)
        trade.addWidget(self.open_sell_btn)
        left_l.addLayout(trade)

        pos_title = QLabel("组合持仓 (整组平仓 / 加仓, 不可单腿)")
        pos_title.setStyleSheet(f"color: {COLOR_ACCENT}; font-weight: bold; margin-top: 4px;")
        left_l.addWidget(pos_title)
        self.pos_table = QTableWidget(0, 7)
        self.pos_table.setHorizontalHeaderLabels(
            ["组合", "方向", "数量", "开仓净价", "现净价", "盈亏($)", "操作"]
        )
        self.pos_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.pos_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.pos_table.verticalHeader().setVisible(False)
        left_l.addWidget(self.pos_table)
        splitter.addWidget(left)

        self.chart_holder = QWidget()
        self.chart_layout = QVBoxLayout(self.chart_holder)
        self.chart_layout.setContentsMargins(0, 0, 0, 0)
        self._chart_placeholder = QLabel("连接并计算后, 这里显示组合价格随时间的变化")
        self._chart_placeholder.setAlignment(Qt.AlignCenter)
        self._chart_placeholder.setStyleSheet(f"color: {COLOR_TEXT_DIM};")
        self.chart_layout.addWidget(self._chart_placeholder)
        splitter.addWidget(self.chart_holder)

        splitter.setSizes([420, 860])
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 6)
        root.addWidget(splitter, stretch=1)

    # ── 动态参数输入 ──────────────────────────────────────────────────────

    def _current_template(self):
        return STRATEGY_REGISTRY[self.strategy_combo.currentData()]

    def _rebuild_params(self):
        """根据当前策略重建 strike_param / expiry_param 输入控件。"""
        while self.params_layout.count():
            item = self.params_layout.takeAt(0)
            w = item.widget()
            if w:
                w.deleteLater()
        self._strike_spins.clear()
        self._expiry_combos.clear()

        tmpl = self._current_template()
        col = 0
        for param in tmpl.expiry_params:
            self.params_layout.addWidget(QLabel(f"{param}:"), 0, col)
            combo = QComboBox()
            combo.setMinimumWidth(120)
            for exp in self._expirations:
                combo.addItem(self._fmt_expiry(exp), exp)
            self._expiry_combos[param] = combo
            self.params_layout.addWidget(combo, 0, col + 1)
            col += 2

        for param in tmpl.strike_params:
            self.params_layout.addWidget(QLabel(f"{param}:"), 1, col % 8)
            spin = QDoubleSpinBox()
            spin.setRange(0, 1_000_000)
            spin.setDecimals(2)
            spin.setSingleStep(self._strike_step())
            self._strike_spins[param] = spin
            self.params_layout.addWidget(spin, 1, (col % 8) + 1)
            col += 2

        self._update_legs_table(prices=None)
        if self._strikes:
            self._autofill_strikes()

    def _strike_step(self) -> float:
        if len(self._strikes) >= 2:
            diffs = sorted(round(b - a, 4)
                           for a, b in zip(self._strikes, self._strikes[1:]) if b > a)
            if diffs:
                return diffs[len(diffs) // 2]
        return 1.0

    def _autofill_strikes(self):
        tmpl = self._current_template()
        center = self.center_spin.value() or self._underlying_price
        assign = auto_assign_strikes(
            tmpl, self._strikes, center, self.width_spin.value()
        )
        for param, spin in self._strike_spins.items():
            if param in assign:
                spin.setValue(assign[param])
        self._update_legs_table(prices=None)

    # ── 腿表 ──────────────────────────────────────────────────────────────

    def _gather_legs(self):
        tmpl = self._current_template()
        strikes_by_param = {p: s.value() for p, s in self._strike_spins.items()}
        expiries_by_param = {p: c.currentData() for p, c in self._expiry_combos.items()}
        return resolved_legs(tmpl, strikes_by_param, expiries_by_param)

    def _update_legs_table(self, prices):
        legs = self._gather_legs()
        self.legs_table.setRowCount(len(legs))
        for i, leg in enumerate(legs):
            buy = leg["action"].upper() == "BUY"
            dir_item = QTableWidgetItem("买入" if buy else "卖出")
            dir_item.setForeground(QColor(COLOR_GREEN if buy else COLOR_RED))
            self.legs_table.setItem(i, 0, dir_item)
            self.legs_table.setItem(i, 1, QTableWidgetItem("Call" if leg["right"] == "C" else "Put"))
            strike = leg["strike"]
            self.legs_table.setItem(i, 2, QTableWidgetItem(f"{strike:g}" if strike else "--"))
            self.legs_table.setItem(i, 3, QTableWidgetItem(f"x{leg['ratio']}"))
            if prices is not None and i < len(prices):
                self.legs_table.setItem(i, 4, QTableWidgetItem(f"{prices[i]:.2f}"))
            else:
                self.legs_table.setItem(i, 4, QTableWidgetItem("--"))

    # ── 连接 ──────────────────────────────────────────────────────────────

    def _on_connect_clicked(self):
        if self.engine.is_connected:
            self.engine.disconnect()
            return
        mode = TradingMode(self.mode_combo.currentData())
        if mode is TradingMode.LIVE:
            if QMessageBox.question(
                self, "切换到实盘",
                "确认连接【实盘】(端口 7496, 真实资金)?\n组合下单将动用真钱。",
                QMessageBox.Ok | QMessageBox.Cancel,
            ) != QMessageBox.Ok:
                return
        self._mode = mode
        self.connect_btn.setEnabled(False)
        self.mode_combo.setEnabled(False)
        self.statusBar().showMessage(f"连接中... ({mode.label})")

        def do_connect():
            try:
                ok = self.engine.connect(mode, client_id=IBKR_COMBO_CLIENT_ID)
                if not ok:
                    port = _display_port(mode.is_live_port)
                    self._status.emit(f"连接失败 — 确认 {mode.label} TWS/Gateway 已登录并监听 {port}")
                    self._combo_error.emit("")  # 仅用于复位按钮
            except Exception as e:
                self._status.emit(f"连接异常: {e}")
                self._combo_error.emit("")

        threading.Thread(target=do_connect, daemon=True).start()

    def _on_connected(self):
        self.connect_btn.setText("断开")
        self.connect_btn.setEnabled(True)
        self.load_chain_btn.setEnabled(True)
        self.open_buy_btn.setEnabled(True)
        self.open_sell_btn.setEnabled(True)
        # 恢复已有组合持仓的各腿行情订阅 (重启后实时盈亏可用)
        for g in self._groups:
            self._subscribe_group_legs(g)
        label = getattr(self, "_mode", TradingMode.IBKR_PAPER).label
        port = _display_port(getattr(self, "_mode", TradingMode.IBKR_PAPER).is_live_port)
        gw = " [GW 新版]" if USE_GATEWAY else ""
        self.setWindowTitle(f"IBKR 期权组合分析器 — {label}{gw}")
        self.statusBar().showMessage(
            f"已连接 {label} (端口 {port}, clientId=12) — 点击「加载期权链」"
        )

    def _on_disconnected(self):
        self.connect_btn.setText("连接")
        self.connect_btn.setEnabled(True)
        self.mode_combo.setEnabled(True)
        self.load_chain_btn.setEnabled(False)
        self.compute_btn.setEnabled(False)
        self.today_btn.setEnabled(False)
        self.open_buy_btn.setEnabled(False)
        self.open_sell_btn.setEnabled(False)
        self.statusBar().showMessage("已断开")

    def _on_engine_error(self, req_id, code, msg):
        # 仅在状态栏提示, 不打断 (数据连接码等无害信息很多)
        if code not in (2104, 2106, 2158, 2107, 2108, 2119):
            self.statusBar().showMessage(f"[{code}] {msg}")

    # ── 加载期权链 ────────────────────────────────────────────────────────

    def _on_load_chain(self):
        if not self.engine.is_connected:
            self.statusBar().showMessage("未连接")
            return
        symbol = self.symbol_input.currentText().strip().upper()
        if not symbol:
            return
        self.load_chain_btn.setEnabled(False)
        self.statusBar().showMessage(f"加载 {symbol} 期权链...")

        def do_load():
            try:
                expirations, strikes = self.engine.request_option_chain(symbol)
                price = 0.0
                try:
                    con_id = self.engine.get_con_id(symbol)  # 触发标的解析
                except Exception:
                    con_id = 0
                self._chain_ready.emit({
                    "symbol": symbol,
                    "expirations": expirations,
                    "strikes": strikes,
                })
            except Exception as e:
                self._chain_error.emit(str(e))

        threading.Thread(target=do_load, daemon=True).start()

    def _on_chain_ready(self, data):
        self._expirations = list(data["expirations"])
        self._strikes = sorted(data["strikes"])
        self.load_chain_btn.setEnabled(True)
        self.compute_btn.setEnabled(True)
        self.today_btn.setEnabled(True)
        if self._strikes and self.center_spin.value() == 0:
            # 默认中心取行权价网格中位数 (近似 ATM)
            self.center_spin.setValue(self._strikes[len(self._strikes) // 2])
        self.center_spin.setSingleStep(self._strike_step())
        self._rebuild_params()
        self.statusBar().showMessage(
            f"{data['symbol']} 期权链已加载: {len(self._expirations)} 个到期, "
            f"{len(self._strikes)} 个行权价"
        )

    def _on_chain_error(self, msg):
        self.load_chain_btn.setEnabled(True)
        self.statusBar().showMessage(f"加载期权链失败: {msg}")

    # ── 计算组合历史价 ────────────────────────────────────────────────────

    def _on_compute(self):
        if not self.engine.is_connected:
            self.statusBar().showMessage("未连接")
            return
        legs = self._gather_legs()
        if not legs or any(not leg["strike"] for leg in legs):
            self.statusBar().showMessage("请先填好各腿行权价 (可点「自动填行权价」)")
            return
        symbol = self.symbol_input.currentText().strip().upper()
        bar_size, duration, _ = CHART_TIMEFRAMES[self.timeframe_combo.currentText()]
        what = self.what_combo.currentText()

        self.compute_btn.setEnabled(False)
        self.statusBar().showMessage("拉取各腿历史数据并合成组合价...")

        def do_compute():
            try:
                cache: dict = {}
                leg_bars = []
                latest = []
                for leg in legs:
                    key = (leg["expiry"], leg["strike"], leg["right"])
                    if key not in cache:
                        cache[key] = self.engine.request_option_historical_data(
                            symbol, leg["expiry"], leg["strike"], leg["right"],
                            bar_size, duration, what_to_show=what, timeout=30,
                        )
                    bars = cache[key]
                    leg_bars.append(bars)
                    latest.append(bars[-1]["close"] if bars else 0.0)
                signed = [leg_sign(l["action"]) * l["ratio"] for l in legs]
                series = compute_combo_series(signed, leg_bars)
                net = combo_price_from_prices(legs, latest)
                self._combo_ready.emit({
                    "series": series, "legs": legs, "latest": latest,
                    "net": net, "what": what,
                    "timeframe": self.timeframe_combo.currentText(),
                })
            except Exception as e:
                self._combo_error.emit(str(e))

        threading.Thread(target=do_compute, daemon=True).start()

    def _on_combo_ready(self, data):
        self.compute_btn.setEnabled(True)
        legs = data["legs"]
        self._update_legs_table(prices=data["latest"])

        net = data["net"]
        kind = "净借记 (买入成本)" if net > 0 else "净贷记 (收取权利金)"
        color = COLOR_RED if net > 0 else COLOR_GREEN
        self.summary_label.setText(
            f"当前组合净价: <span style='color:{color}'>${abs(net):.2f}</span> / 组  ({kind})"
        )
        # 预填开仓净限价 (四舍五入到分)
        self.limit_spin.setValue(round(net, 2))

        series = data["series"]
        if not series:
            self.statusBar().showMessage(
                "各腿历史数据没有共同的时间戳 — 试试 MIDPOINT 数据或更长周期"
            )
            self._plot_series([])
            return
        self._plot_series(series)
        first = datetime.fromtimestamp(float(series[0]["date"]))
        last = datetime.fromtimestamp(float(series[-1]["date"]))
        self.statusBar().showMessage(
            f"组合历史价: {len(series)} 根 ({data['timeframe']}, {data['what']}) "
            f"{first:%m-%d %H:%M} → {last:%m-%d %H:%M}"
        )

    def _on_combo_error(self, msg):
        self.compute_btn.setEnabled(self.engine.is_connected)
        self.today_btn.setEnabled(self.engine.is_connected)
        self.connect_btn.setEnabled(True)
        if not self.engine.is_connected:
            self.mode_combo.setEnabled(True)   # 连接失败时恢复模式选择
        if msg:
            self.statusBar().showMessage(f"计算失败: {msg}")

    # ── 今日合并 K 线 (各腿当日 OHLC 合并成组合蜡烛图) ────────────────────

    def _on_today_kline(self):
        if not self.engine.is_connected:
            self.statusBar().showMessage("未连接")
            return
        legs = self._gather_legs()
        if not legs or any(not l["strike"] or not l["expiry"] for l in legs):
            self.statusBar().showMessage("请先填好各腿行权价与到期 (可点「自动填行权价」)")
            return
        symbol = self.symbol_input.currentText().strip().upper()
        # 按所选周期取 bar_size + 对应时间跨度 (duration), 不再写死「1 D」当日,
        # 这样 1分/5分/1时/2时/4时 等都能给出多天的组合蜡烛图。
        tf = self.timeframe_combo.currentText()
        bar_size, duration, _ = CHART_TIMEFRAMES[tf]
        what = self.what_combo.currentText()
        self.today_btn.setEnabled(False)
        self.statusBar().showMessage(f"拉取各腿 K 线并合并 ({tf}, 跨度 {duration})...")

        def worker():
            try:
                cache, leg_bars = {}, []
                for l in legs:
                    key = (l["expiry"], l["strike"], l["right"])
                    if key not in cache:
                        cache[key] = self.engine.request_option_historical_data(
                            symbol, l["expiry"], l["strike"], l["right"],
                            bar_size, duration, what_to_show=what, timeout=30,
                        )
                    leg_bars.append(cache[key])
                signed = [leg_sign(l["action"]) * l["ratio"] for l in legs]
                ohlc = compute_combo_ohlc(signed, leg_bars)
                self._today_ready.emit({
                    "ohlc": ohlc, "bar_size": bar_size, "what": what, "tf": tf,
                })
            except Exception as e:
                self._combo_error.emit(str(e))

        threading.Thread(target=worker, daemon=True).start()

    def _on_today_ready(self, data):
        self.today_btn.setEnabled(self.engine.is_connected)
        ohlc = data["ohlc"]
        if not ohlc:
            self.statusBar().showMessage(
                "各腿 K 线无共同时间戳 — 试 MIDPOINT 数据或更大周期"
            )
            self._plot_candles([])
            return
        self._plot_candles(ohlc)
        first = datetime.fromtimestamp(float(ohlc[0]["date"]))
        last = datetime.fromtimestamp(float(ohlc[-1]["date"]))
        tf = data.get("tf", data["bar_size"])
        # 跨度超过 1 天则带日期, 当日内只显示时分
        span_days = (last - first).days
        fmt = "%m-%d %H:%M" if span_days >= 1 else "%H:%M"
        self.summary_label.setText(
            f"组合 K 线: {len(ohlc)} 根 (收盘净价 ${ohlc[-1]['close']:.2f})"
        )
        self.statusBar().showMessage(
            f"组合K线: {len(ohlc)} 根 ({tf}, {data['what']}) "
            f"{first:{fmt}} → {last:{fmt}} — 滚轮缩放 / 拖动平移"
        )

    # ── 实时录制当日组合价 (零历史权限) ──────────────────────────────────

    def _toggle_record(self):
        if self._recording:
            self._record_timer.stop()
            self._recording = False
            self.record_btn.setText("▶ 录制当日")
            self.statusBar().showMessage(
                f"已停止录制 — 当日已采集 {len(self._record_series)} 个点"
            )
            return
        if not self.engine.is_connected:
            self.statusBar().showMessage("未连接")
            return
        legs = self._gather_legs()
        if not legs or any(not l["strike"] or not l["expiry"] for l in legs):
            self.statusBar().showMessage("请先填好各腿行权价与到期 (可点「自动填行权价」)")
            return
        self._record_symbol = self.symbol_input.currentText().strip().upper()
        self._record_legs = [dict(l) for l in legs]
        self._record_series = []
        # 订阅各腿实时行情
        for l in self._record_legs:
            opt = OptionInfo(symbol=self._record_symbol, expiry=l["expiry"],
                             strike=l["strike"], right=l["right"])
            key = opt.to_ibkr_key()
            if key not in self._subscribed_keys:
                try:
                    self.engine.subscribe_option_tick(opt)
                    self._subscribed_keys.add(key)
                except Exception:
                    pass
        self._recording = True
        self.record_btn.setText("■ 停止录制")
        self.statusBar().showMessage("录制当日组合价中... (每 2 秒采样一次)")
        self._record_timer.start(2000)

    def _record_sample(self):
        """采样一次: 各腿实时盘口中价 → 组合净价, 追加并实时画出。"""
        prices = []
        for l in self._record_legs:
            opt = OptionInfo(symbol=self._record_symbol, expiry=l["expiry"],
                             strike=l["strike"], right=l["right"])
            tick = self.engine.get_tick(opt.to_ibkr_key()) or {}
            bid = tick.get("bid", 0) or 0
            ask = tick.get("ask", 0) or 0
            last = tick.get("last", 0) or 0
            mid = (bid + ask) / 2 if bid > 0 and ask > 0 else last
            if mid <= 0:
                return  # 某腿暂无报价 — 跳过本次采样
            prices.append(mid)
        net = combo_price_from_prices(self._record_legs, prices)
        self._record_series.append({"date": str(time.time()), "price": net})
        self._plot_series(self._record_series)
        kind = "净借记" if net > 0 else "净贷记"
        color = COLOR_RED if net > 0 else COLOR_GREEN
        self.summary_label.setText(
            f"当日实时组合净价: <span style='color:{color}'>${abs(net):.2f}</span> / 组 "
            f"({kind}, {len(self._record_series)} 点)"
        )

    # ── 绘图 (pyqtgraph 延迟导入) ─────────────────────────────────────────

    def _ensure_plot(self):
        if self._plot_widget is not None:
            return
        import pyqtgraph as pg
        from widgets.candlestick_item import CandlestickItem
        pg.setConfigOptions(antialias=True)
        self._chart_placeholder.hide()
        # 索引 x 轴 + 自定义时间刻度 (蜡烛图与折线共用; pyqtgraph 默认滚轮缩放/拖动平移)
        self._bottom_axis = pg.AxisItem(orientation="bottom")
        self._plot_widget = pg.PlotWidget(axisItems={"bottom": self._bottom_axis})
        self._plot_widget.setBackground(COLOR_BG_DARK)
        self._plot_widget.showGrid(x=True, y=True, alpha=0.2)
        self._plot_widget.setLabel("left", "组合净价 ($/组)")
        self._plot_widget.addLine(y=0, pen=pg.mkPen(COLOR_TEXT_DIM, style=Qt.DashLine))
        self._curve = self._plot_widget.plot(
            [], [], pen=pg.mkPen(COLOR_ACCENT, width=2)
        )
        self._candle = CandlestickItem(COLOR_GREEN, COLOR_RED)
        self._plot_widget.addItem(self._candle)
        self.chart_layout.addWidget(self._plot_widget)

    def _time_ticks(self, dates, n_labels=8):
        """由 epoch 字符串列表生成索引轴的 [(idx, 'MM-DD HH:MM')] 刻度。"""
        n = len(dates)
        if n == 0:
            return []
        step = max(1, n // n_labels)
        ticks = []
        for i in range(0, n, step):
            try:
                t = datetime.fromtimestamp(float(dates[i]))
                ticks.append((i, t.strftime("%m-%d %H:%M")))
            except (TypeError, ValueError):
                ticks.append((i, str(dates[i])))
        return ticks

    def _plot_series(self, series):
        """收盘价折线 (录制/历史线), 索引 x + 时间刻度。"""
        self._ensure_plot()
        self._candle.set_data([])
        self._curve.setData(list(range(len(series))), [p["price"] for p in series])
        self._bottom_axis.setTicks([self._time_ticks([p["date"] for p in series])])
        if series:
            self._plot_widget.enableAutoRange()

    def _plot_candles(self, ohlc):
        """组合合并 K 线 (蜡烛), 索引 x + 时间刻度。"""
        self._ensure_plot()
        self._curve.setData([], [])
        data = [{"date_idx": i, "open": b["open"], "high": b["high"],
                 "low": b["low"], "close": b["close"]} for i, b in enumerate(ohlc)]
        self._candle.set_data(data)
        self._bottom_axis.setTicks([self._time_ticks([b["date"] for b in ohlc])])
        if data:
            self._plot_widget.enableAutoRange()

    # ── 组合交易 (原生 BAG, 原子成交; 只整组平仓/加仓) ────────────────────

    def _on_open_combo(self, action: str):
        """整组开仓。action='BUY' 按模板买入组合, 'SELL' 卖出 (反向) 组合。"""
        if not self.engine.is_connected:
            self.statusBar().showMessage("未连接")
            return
        legs = self._gather_legs()
        if not legs or any(not l["strike"] or not l["expiry"] for l in legs):
            self.statusBar().showMessage("请先填好各腿行权价与到期 (可点「自动填行权价」)")
            return
        symbol = self.symbol_input.currentText().strip().upper()
        qty = self.qty_spin.value()
        limit = round(self.limit_spin.value(), 2)
        # 组合定义净价以 limit 为准 (= 用户确认的成交净价)
        group = {
            "id": self._next_group_id(),
            "symbol": symbol,
            "strategy": self._current_template().display_name,
            "direction": action,
            "legs": [dict(l) for l in legs],
            "entry_net": limit,
            "quantity": qty,
            "open_time": f"{datetime.now():%Y-%m-%d %H:%M:%S}",
        }
        verb = "买入" if action == "BUY" else "卖出"
        if QMessageBox.question(
            self, "确认开仓",
            f"{verb}开仓 {qty} 组\n{group['strategy']}\n净限价 ${limit:.2f}\n\n"
            f"组合将作为一个整体持仓 (只能整组平仓/加仓)。",
            QMessageBox.Ok | QMessageBox.Cancel,
        ) != QMessageBox.Ok:
            return
        self._place_combo(symbol, legs, action, qty, limit,
                          {"mode": "open", "group": group})

    def _place_combo(self, symbol, legs, action, qty, limit, pending):
        """后台解析各腿 conId 并下 BAG 单。"""
        outside = self.rth_check.isChecked()
        self.open_buy_btn.setEnabled(False)
        self.open_sell_btn.setEnabled(False)
        self.statusBar().showMessage("解析合约并提交组合单...")

        def worker():
            try:
                combo_legs = []
                for l in legs:
                    con_id = self.engine.resolve_option_con_id(
                        symbol, l["expiry"], l["strike"], l["right"]
                    )
                    if not con_id:
                        raise RuntimeError(
                            f"无法解析合约 {symbol} {l['expiry']} {l['strike']:g}{l['right']}"
                        )
                    combo_legs.append(ComboLegInfo(
                        con_id=con_id, symbol=symbol, expiry=l["expiry"],
                        strike=l["strike"], right=l["right"], action=l["action"],
                        ratio=l["ratio"],
                    ))
                order_id = self.engine.place_combo_order(
                    symbol, combo_legs, action, qty, limit, outside_rth=outside
                )
                if order_id and order_id > 0:
                    self._order_placed.emit(order_id, pending, pending["mode"])
                else:
                    self._combo_error.emit("组合下单失败 (orderId 无效)")
            except Exception as e:
                self._combo_error.emit(str(e))

        threading.Thread(target=worker, daemon=True).start()

    def _on_order_placed(self, order_id, pending, mode):
        self._pending[order_id] = pending
        self.open_buy_btn.setEnabled(self.engine.is_connected)
        self.open_sell_btn.setEnabled(self.engine.is_connected)
        self.statusBar().showMessage(f"组合单已提交 (#{order_id}, {mode}) — 等待成交")

    def _on_order_status(self, order_id, status, filled, remaining, avg_fill):
        pending = self._pending.get(order_id)
        if pending is None:
            return
        # 组合 BAG 整单成交 (remaining==0) 才落定持仓
        if str(status).lower() != "filled" and not (filled > 0 and remaining == 0):
            return
        self._pending.pop(order_id, None)
        mode = pending["mode"]
        if mode == "open":
            g = pending["group"]
            self._groups.append(g)
            self._subscribe_group_legs(g)
            self.statusBar().showMessage(f"组合已建仓: {g['strategy']} x{g['quantity']}")
        elif mode == "close":
            gid = pending["group_id"]
            self._groups = [g for g in self._groups if g["id"] != gid]
            self.statusBar().showMessage("组合已整组平仓")
        elif mode == "add":
            gid = pending["group_id"]
            for g in self._groups:
                if g["id"] == gid:
                    add_q = pending["qty"]
                    add_net = pending["net"]
                    old_q = g["quantity"]
                    g["entry_net"] = round(
                        (g["entry_net"] * old_q + add_net * add_q) / (old_q + add_q), 4
                    )
                    g["quantity"] = old_q + add_q
                    break
            self.statusBar().showMessage("组合已加仓")
        self._save_groups()
        self._refresh_positions_table()

    def _on_order_rejected(self, order_id, code, msg):
        if order_id in self._pending:
            self._pending.pop(order_id, None)
            self.open_buy_btn.setEnabled(self.engine.is_connected)
            self.open_sell_btn.setEnabled(self.engine.is_connected)
            QMessageBox.warning(self, "组合单被拒", f"[{code}] {msg}")
            self.statusBar().showMessage(f"组合单被拒 [{code}]: {msg}")

    def _close_group(self, group_id):
        g = next((x for x in self._groups if x["id"] == group_id), None)
        if g is None or not self.engine.is_connected:
            return
        reverse = "SELL" if g["direction"] == "BUY" else "BUY"
        live = self._group_net_live(g)
        limit = round(live if live is not None else g["entry_net"], 2)
        if QMessageBox.question(
            self, "确认整组平仓",
            f"整组平仓 {g['strategy']} x{g['quantity']}\n反向 {reverse} @ 净 ${limit:.2f}",
            QMessageBox.Ok | QMessageBox.Cancel,
        ) != QMessageBox.Ok:
            return
        self._place_combo(g["symbol"], g["legs"], reverse, g["quantity"], limit,
                          {"mode": "close", "group_id": group_id})

    def _add_group(self, group_id):
        g = next((x for x in self._groups if x["id"] == group_id), None)
        if g is None or not self.engine.is_connected:
            return
        add_q = self.qty_spin.value()
        live = self._group_net_live(g)
        limit = round(live if live is not None else g["entry_net"], 2)
        if QMessageBox.question(
            self, "确认加仓",
            f"加仓 {g['strategy']} +{add_q} 组\n同向 {g['direction']} @ 净 ${limit:.2f}",
            QMessageBox.Ok | QMessageBox.Cancel,
        ) != QMessageBox.Ok:
            return
        self._place_combo(g["symbol"], g["legs"], g["direction"], add_q, limit,
                          {"mode": "add", "group_id": group_id, "qty": add_q, "net": limit})

    # ── 组合持仓: 实时净价/盈亏 + 表格 ────────────────────────────────────

    def _subscribe_group_legs(self, group):
        if not self.engine.is_connected:
            return
        for l in group["legs"]:
            opt = OptionInfo(symbol=group["symbol"], expiry=l["expiry"],
                             strike=l["strike"], right=l["right"])
            key = opt.to_ibkr_key()
            if key not in self._subscribed_keys:
                try:
                    self.engine.subscribe_option_tick(opt)
                    self._subscribed_keys.add(key)
                except Exception:
                    pass

    def _group_net_live(self, group):
        """由各腿实时盘口中价合成组合现净价 (组合定义口径)。缺价返回 None。"""
        if not self.engine.is_connected:
            return None
        total = 0.0
        for l in group["legs"]:
            opt = OptionInfo(symbol=group["symbol"], expiry=l["expiry"],
                             strike=l["strike"], right=l["right"])
            tick = self.engine.get_tick(opt.to_ibkr_key()) or {}
            bid = tick.get("bid", 0) or 0
            ask = tick.get("ask", 0) or 0
            last = tick.get("last", 0) or 0
            mid = (bid + ask) / 2 if bid > 0 and ask > 0 else last
            if mid <= 0:
                return None
            total += leg_sign(l["action"]) * l["ratio"] * mid
        return total

    def _group_pnl(self, group, live_net):
        """方向相关盈亏 ($): BUY 组合 (current-entry), SELL 组合 (entry-current)。"""
        if live_net is None:
            return None
        sign = 1.0 if group["direction"] == "BUY" else -1.0
        return sign * (live_net - group["entry_net"]) * group["quantity"] * 100.0

    def _refresh_positions_table(self):
        self.pos_table.setRowCount(len(self._groups))
        for i, g in enumerate(self._groups):
            self.pos_table.setItem(i, 0, QTableWidgetItem(g["strategy"]))
            dir_item = QTableWidgetItem("买入" if g["direction"] == "BUY" else "卖出")
            dir_item.setForeground(QColor(COLOR_GREEN if g["direction"] == "BUY" else COLOR_RED))
            self.pos_table.setItem(i, 1, dir_item)
            self.pos_table.setItem(i, 2, QTableWidgetItem(str(g["quantity"])))
            self.pos_table.setItem(i, 3, QTableWidgetItem(f"{g['entry_net']:.2f}"))
            self.pos_table.setItem(i, 4, QTableWidgetItem("--"))
            self.pos_table.setItem(i, 5, QTableWidgetItem("--"))

            cell = QWidget()
            cl = QHBoxLayout(cell)
            cl.setContentsMargins(2, 1, 2, 1)
            cl.setSpacing(4)
            close_btn = QPushButton("平仓")
            close_btn.clicked.connect(lambda _, gid=g["id"]: self._close_group(gid))
            add_btn = QPushButton("加仓")
            add_btn.clicked.connect(lambda _, gid=g["id"]: self._add_group(gid))
            cl.addWidget(close_btn)
            cl.addWidget(add_btn)
            self.pos_table.setCellWidget(i, 6, cell)
        self._refresh_positions_live()

    def _periodic_refresh(self):
        self._refresh_legs_live()
        self._refresh_positions_live()

    def _refresh_legs_live(self):
        """连接后, 持续把当前组合各腿的实时盘口中价填进腿表「最新价」列,
        并实时显示组合净价 —— 不必先点「计算」或「录制」。"""
        if self._recording or not self.engine.is_connected:
            return  # 录制时由 _record_sample 负责
        legs = self._gather_legs()
        if not legs or any(not l["strike"] or not l["expiry"] for l in legs):
            return
        symbol = self.symbol_input.currentText().strip().upper()
        prices, complete = [], True
        for i, l in enumerate(legs):
            opt = OptionInfo(symbol=symbol, expiry=l["expiry"],
                             strike=l["strike"], right=l["right"])
            key = opt.to_ibkr_key()
            if key not in self._subscribed_keys:
                try:
                    self.engine.subscribe_option_tick(opt)
                    self._subscribed_keys.add(key)
                except Exception:
                    pass
            tick = self.engine.get_tick(key) or {}
            bid = tick.get("bid", 0) or 0
            ask = tick.get("ask", 0) or 0
            last = tick.get("last", 0) or 0
            mid = (bid + ask) / 2 if bid > 0 and ask > 0 else last
            prices.append(mid)
            if i < self.legs_table.rowCount():
                self.legs_table.setItem(
                    i, 4, QTableWidgetItem(f"{mid:.2f}" if mid > 0 else "等待行情…")
                )
            if mid <= 0:
                complete = False
        if complete:
            net = combo_price_from_prices(legs, prices)
            kind = "净借记 (买入成本)" if net > 0 else "净贷记 (收取权利金)"
            color = COLOR_RED if net > 0 else COLOR_GREEN
            self.summary_label.setText(
                f"当前组合净价: <span style='color:{color}'>${abs(net):.2f}</span> / 组  ({kind})"
            )
            if self.limit_spin.value() == 0:
                self.limit_spin.setValue(round(net, 2))

    def _refresh_positions_live(self):
        for i, g in enumerate(self._groups):
            if i >= self.pos_table.rowCount():
                break
            live = self._group_net_live(g)
            pnl = self._group_pnl(g, live)
            net_item = QTableWidgetItem(f"{live:.2f}" if live is not None else "--")
            self.pos_table.setItem(i, 4, net_item)
            if pnl is None:
                self.pos_table.setItem(i, 5, QTableWidgetItem("--"))
            else:
                it = QTableWidgetItem(f"{pnl:+.2f}")
                it.setForeground(QColor(COLOR_GREEN if pnl >= 0 else COLOR_RED))
                self.pos_table.setItem(i, 5, it)

    # ── 持久化 (组合分组 IBKR 不保留, 自行存盘) ───────────────────────────

    def _next_group_id(self):
        self._group_seq += 1
        return self._group_seq

    def _load_groups(self):
        try:
            with open(self._groups_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, list) else []
        except (FileNotFoundError, ValueError, OSError):
            return []

    def _save_groups(self):
        try:
            with open(self._groups_file, "w", encoding="utf-8") as f:
                json.dump(self._groups, f, ensure_ascii=False, indent=2)
        except OSError as e:
            print(f"[combo] save groups failed: {e}", flush=True)

    # ── 辅助 ──────────────────────────────────────────────────────────────

    @staticmethod
    def _fmt_expiry(exp: str) -> str:
        if len(exp) == 8:
            return f"{exp[2:4]}-{exp[4:6]}-{exp[6:8]}"
        return exp

    def closeEvent(self, event):
        self._pos_timer.stop()
        self._record_timer.stop()
        self._save_groups()
        if self.engine.is_connected:
            self.engine.disconnect()
        event.accept()


def main():
    kill_previous_instances(__file__)
    QApplication.setAttribute(Qt.AA_EnableHighDpiScaling, True)
    QApplication.setAttribute(Qt.AA_UseHighDpiPixmaps, True)
    app = QApplication(sys.argv)
    from config import FONT_FAMILY, FONT_SIZE
    app.setFont(QFont(FONT_FAMILY, FONT_SIZE))
    if os.path.exists(APP_ICON):
        app.setWindowIcon(QIcon(APP_ICON))
    window = ComboAnalyzerWindow()
    window.show()
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
