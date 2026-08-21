"""Option Chain — T-shaped quote table with expiry tabs + date range filter."""

import threading
from datetime import datetime, timedelta

from momentum_flip import analyze as analyze_es_momentum

from PyQt5.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QTabWidget, QTableWidget,
    QTableWidgetItem, QHeaderView, QAbstractItemView, QLabel,
    QPushButton, QButtonGroup, QSizePolicy,
)
from PyQt5.QtCore import pyqtSignal, Qt, QTimer
from PyQt5.QtGui import QColor, QBrush

from config import (
    COLOR_BG_PANEL, COLOR_BG_DARK, COLOR_TEXT, COLOR_TEXT_DIM,
    COLOR_ATM_HIGHLIGHT, COLOR_GREEN, COLOR_RED, COLOR_ACCENT,
    COLOR_BORDER, COLOR_BUTTON_DISABLED, COLOR_ACCENT_HOVER, COLOR_MY_ORDER,
    MAX_EXPIRY_TABS_PER_RANGE, MAX_SIMULTANEOUS_STREAMS,
)
from models import OptionInfo


# Range filter names in display order
RANGE_NAMES = ["0DTE", "本周", "下周", "本月", "下月", "远月", "全部"]


class OptionChainWidget(QWidget):
    """T-shaped option chain with expiry tabs and date range filter."""

    option_selected = pyqtSignal(object)  # OptionInfo
    chart_requested = pyqtSignal(object)  # OptionInfo — 双击合约: 打开今日 1 分钟图
    # 后台 reqContractDetails 取到某到期日真实行权价后, 回到 GUI 线程填表
    _strikes_ready = pyqtSignal(str, str, object, bool)  # symbol, expiry, strikes, ok
    _momentum_ready = pyqtSignal(object)  # ES 动量翻转 state (compute_flip 结果或 None)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._symbol = ""
        self._all_expirations: list[str] = []  # ALL future expirations
        self._expirations: list[str] = []       # Currently displayed (filtered)
        self._strikes: list[float] = []      # 当前 Tab(到期日)的行权价
        self._all_strikes: list[float] = []  # 全链 union (按到期日取不到时的回退)
        self._options: dict[str, OptionInfo] = {}  # key -> OptionInfo
        self._sub_req_ids: dict[str, int] = {}      # key -> reqId
        self._engine = None
        self._stock_price = 0.0
        self._range_buckets: dict[str, list[str]] = {}  # range_name -> [expirations]
        self._active_range: str = ""
        self._atm_row: int = 0   # ATM 行索引 (切 Tab 时滚到此处, 优先取价)
        # 各到期日**真实**行权价 (reqContractDetails 取, 按到期日缓存)
        self._real_strikes: dict[str, list] = {}
        self._fetching_strikes: set = set()   # 正在后台取的到期日, 防重复请求
        self._strikes_ready.connect(self._on_strikes_ready)
        # ES 动量翻转监测 (60s 后台拉 ES 连续期货 1分钟K线计算; 与品种无关, 只看 ES)
        self._mom_fetching = False
        self._momentum_ready.connect(self._on_momentum_ready)
        self._mom_timer = QTimer(self)
        self._mom_timer.timeout.connect(self._fetch_momentum)
        self._mom_timer.start(60_000)
        # 视口取价去抖: 滚动停止 ~250ms 后只对当前可见行拉一次快照 (省行情线)
        self._snap_timer = QTimer(self)
        self._snap_timer.setSingleShot(True)
        self._snap_timer.timeout.connect(self._request_snapshots)

        # Cached brushes — reused every refresh instead of allocating a new
        # QBrush/QColor per cell per second.
        self._brush_text = QBrush(QColor(COLOR_TEXT))
        self._brush_bid = QBrush(QColor(COLOR_GREEN))
        self._brush_ask = QBrush(QColor(COLOR_RED))

        self._build_ui()

        # Refresh timer
        self._refresh_timer = QTimer()
        self._refresh_timer.timeout.connect(self._refresh_prices)
        self._refresh_timer.start(1000)

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)

        self.title_label = QLabel("期权链")
        self.title_label.setStyleSheet(
            "font-size: 14px; font-weight: bold; padding: 4px;"
        )
        layout.addWidget(self.title_label)

        # ── Range filter bar ──
        self._range_bar = QWidget()
        range_layout = QHBoxLayout(self._range_bar)
        range_layout.setContentsMargins(4, 2, 4, 2)
        range_layout.setSpacing(4)

        self._range_btn_group = QButtonGroup(self)
        self._range_btn_group.setExclusive(True)
        self._range_buttons: dict[str, QPushButton] = {}

        for name in RANGE_NAMES:
            btn = QPushButton(name)
            btn.setCheckable(True)
            btn.setMinimumWidth(50)
            btn.setFixedHeight(26)
            btn.setCursor(Qt.PointingHandCursor)
            self._apply_range_btn_style(btn, selected=False, enabled=True)
            btn.clicked.connect(lambda checked, n=name: self._on_range_clicked(n))
            self._range_btn_group.addButton(btn)
            self._range_buttons[name] = btn
            range_layout.addWidget(btn)

        range_layout.addStretch()

        # ES 动量翻转 (Vordinkkk 法, 1分钟K线): 翻多▲ / 翻空▼ / 持续 / —
        self._momentum_label = QLabel("ES动量 —")
        self._momentum_label.setToolTip(
            "ES (E-mini S&P 500 连续期货) 动量 — Vordinkkk 法 (1分钟K线):\n"
            "翻转: 动量(收盘[t]−收盘[t−10]) 穿越0 且 |导数| ≥ 0.4\n"
            "  翻多▲ = 由空转多; 翻空▼ = 由多转空 (入场信号)\n"
            "趋势/震荡 (5MA 斜率法): 最近5根 5周期均线斜率\n"
            "  同向占比 ≥ 80% → 趋势↑/↓ (价格抖动视为噪音)\n"
            "  否则 → 震荡~ (chop, 无仓不开/谨慎)\n"
            "组合显示: 如「翻多▲ · 趋势↑」(强) / 「翻空▼ · 震荡~」(谨慎)"
        )
        self._momentum_label.setStyleSheet(
            f"color: {COLOR_TEXT_DIM}; font-size: 12px; padding: 0 8px;"
        )
        range_layout.addWidget(self._momentum_label)

        # 今日已平仓交易统计 (刷新报价左侧): 笔数 / 胜率 / 盈亏比 (不列明细)
        self._stats_label = QLabel("笔数 — · 胜率 — · 盈亏比 —")
        self._stats_label.setToolTip(
            "今日已平仓交易统计 (开+平算1笔):\n"
            "· 笔数 = 已平仓回合数\n"
            "· 胜率 = 盈利笔数 / 总笔数\n"
            "· 盈亏比 = 平均盈利 / 平均亏损"
        )
        self._stats_label.setStyleSheet(
            f"color: {COLOR_TEXT_DIM}; font-size: 12px; padding: 0 8px;"
        )
        range_layout.addWidget(self._stats_label)

        # 手动刷新按钮: 拉一次快照报价 (不占常驻行情线, 避免持续订阅压垮 Gateway)
        self._refresh_btn = QPushButton("🔄 刷新报价")
        self._refresh_btn.setFixedHeight(26)
        self._refresh_btn.setCursor(Qt.PointingHandCursor)
        self._refresh_btn.setStyleSheet(f"""
            QPushButton {{
                background-color: {COLOR_ACCENT};
                color: white;
                border: 1px solid {COLOR_ACCENT};
                border-radius: 3px;
                font-weight: bold;
                font-size: 12px;
                padding: 2px 10px;
            }}
            QPushButton:hover {{ background-color: {COLOR_ACCENT_HOVER}; }}
            QPushButton:disabled {{
                background-color: {COLOR_BUTTON_DISABLED}; color: #555555;
                border: 1px solid {COLOR_BORDER};
            }}
        """)
        self._refresh_btn.clicked.connect(self._request_snapshots)
        range_layout.addWidget(self._refresh_btn)

        self._range_bar.hide()  # Hidden until chain is loaded
        layout.addWidget(self._range_bar)

        # ── Tab widget ──
        self.tab_widget = QTabWidget()
        self.tab_widget.currentChanged.connect(self._on_tab_changed)
        self.tab_widget.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        # stretch=1 → 标题与筛选栏之外的纵向空间全部给报价表
        layout.addWidget(self.tab_widget, stretch=1)

    def _apply_range_btn_style(self, btn: QPushButton, selected: bool, enabled: bool):
        if not enabled:
            btn.setEnabled(False)
            btn.setStyleSheet(f"""
                QPushButton {{
                    background-color: {COLOR_BUTTON_DISABLED};
                    color: #555555;
                    border: 1px solid {COLOR_BORDER};
                    border-radius: 3px;
                    font-size: 12px;
                    padding: 2px 6px;
                }}
            """)
        elif selected:
            btn.setEnabled(True)
            btn.setStyleSheet(f"""
                QPushButton {{
                    background-color: {COLOR_ACCENT};
                    color: white;
                    border: 1px solid {COLOR_ACCENT};
                    border-radius: 3px;
                    font-weight: bold;
                    font-size: 12px;
                    padding: 2px 6px;
                }}
            """)
        else:
            btn.setEnabled(True)
            btn.setStyleSheet(f"""
                QPushButton {{
                    background-color: {COLOR_BG_DARK};
                    color: {COLOR_TEXT_DIM};
                    border: 1px solid {COLOR_BORDER};
                    border-radius: 3px;
                    font-size: 12px;
                    padding: 2px 6px;
                }}
                QPushButton:hover {{
                    background-color: {COLOR_BG_PANEL};
                    color: {COLOR_TEXT};
                }}
            """)

    def set_engine(self, engine):
        self._engine = engine
        # 连接后尽快出一次 ES 动量 (不等 60s 首个定时器)
        QTimer.singleShot(2000, self._fetch_momentum)

    # ── ES 动量翻转 ─────────────────────────────────────────────────────

    def _fetch_momentum(self):
        """后台拉 ES 连续期货 1分钟K线 → 算动量翻转 → 信号回 GUI 更新标签。"""
        if self._mom_fetching or self._engine is None:
            return
        if not getattr(self._engine, "is_connected", False):
            return
        if not hasattr(self._engine, "request_es_momentum_bars"):
            return
        self._mom_fetching = True
        eng = self._engine

        def work():
            state = None
            try:
                closes = eng.request_es_momentum_bars()
                if closes:
                    state = analyze_es_momentum(closes)
            except Exception:
                state = None
            self._momentum_ready.emit(state)

        threading.Thread(target=work, daemon=True).start()

    def _on_momentum_ready(self, state):
        """(GUI 线程) 更新 ES 动量标签: 翻转(翻多▲/翻空▼) + 趋势/震荡 regime。
        state=None → 无数据。"""
        self._mom_fetching = False
        if not state:
            self._momentum_label.setText("ES动量 —")
            self._momentum_label.setStyleSheet(
                f"color: {COLOR_TEXT_DIM}; font-size: 12px; padding: 0 8px;"
            )
            return
        # 震荡(chop) 用琥珀色 = 中性警示 (复用 COLOR_MY_ORDER 的主题琥珀,
        # 亮色主题下自动加深保证白底可读)
        amber = COLOR_MY_ORDER

        # 翻转部分 (最actionable, 加粗)
        flip = state.get("flip")
        if flip == "UP":
            flip_html = f"<span style='color:{COLOR_GREEN};font-weight:bold'>翻多▲</span>"
        elif flip == "DOWN":
            flip_html = f"<span style='color:{COLOR_RED};font-weight:bold'>翻空▼</span>"
        else:
            flip_html = ""

        # 趋势/震荡 regime 部分
        regime = state.get("regime")
        rdir = state.get("regime_direction")
        if regime == "TRENDING" and rdir == "UP":
            regime_html = f"<span style='color:{COLOR_GREEN}'>趋势↑</span>"
        elif regime == "TRENDING" and rdir == "DOWN":
            regime_html = f"<span style='color:{COLOR_RED}'>趋势↓</span>"
        elif regime == "CHOPPY":
            regime_html = f"<span style='color:{amber}'>震荡~</span>"
        else:
            regime_html = ""

        parts = [p for p in (flip_html, regime_html) if p]
        if not parts:
            # 回退: 当前动量方向 (无翻转且 regime 不可用)
            sign = state.get("sign", 0)
            if sign > 0:
                parts.append(f"<span style='color:{COLOR_GREEN}'>多</span>")
            elif sign < 0:
                parts.append(f"<span style='color:{COLOR_RED}'>空</span>")
            else:
                parts.append(f"<span style='color:{COLOR_TEXT_DIM}'>平</span>")

        inner = " · ".join(parts)
        self._momentum_label.setText(
            f"<span style='color:{COLOR_TEXT_DIM}'>ES</span> {inner}"
        )
        self._momentum_label.setStyleSheet("font-size: 12px; padding: 0 8px;")

    def set_trade_stats(self, stats: dict):
        """更新「笔数 · 胜率 · 盈亏比」(刷新报价左侧)。stats 为 TradeStats.snapshot()。"""
        if not stats:
            return
        count = int(stats.get("count", 0) or 0)
        if count <= 0:
            self._stats_label.setText("笔数 0 · 胜率 — · 盈亏比 —")
            self._stats_label.setStyleSheet(
                f"color: {COLOR_TEXT_DIM}; font-size: 12px; padding: 0 8px;"
            )
            return
        wr = stats.get("win_rate", 0.0) or 0.0
        plr = stats.get("pl_ratio", None)   # None = 无亏损回合 → ∞
        wr_color = COLOR_GREEN if wr >= 0.5 else COLOR_RED
        if plr is None:
            plr_txt, plr_color = "∞", COLOR_GREEN
        else:
            plr_txt = f"{plr:.2f}"
            plr_color = COLOR_GREEN if plr >= 1.0 else COLOR_RED
        self._stats_label.setText(
            f"<span style='color:{COLOR_TEXT_DIM}'>笔数</span> {count} · "
            f"<span style='color:{COLOR_TEXT_DIM}'>胜率</span> "
            f"<span style='color:{wr_color};font-weight:bold'>{wr * 100:.0f}%</span> · "
            f"<span style='color:{COLOR_TEXT_DIM}'>盈亏比</span> "
            f"<span style='color:{plr_color};font-weight:bold'>{plr_txt}</span>"
        )
        self._stats_label.setStyleSheet("font-size: 12px; padding: 0 8px;")

    def load_chain(self, symbol: str, expirations: list[str], strikes: list[float],
                   stock_price: float = 0):
        """Load option chain data."""
        self._symbol = symbol
        self._stock_price = stock_price
        # 换标的: 清掉按到期日缓存的真实行权价 (不同标的共用同样的周五到期日字符串,
        # 不清会把上一个标的的行权价错配到新标的)。
        self._real_strikes.clear()
        self._fetching_strikes.clear()

        # Filter to future expirations only
        today = datetime.now().strftime("%Y%m%d")
        self._all_expirations = [e for e in expirations if e >= today]

        # 显示**全部**行权价 (能滚到远端如 SPY 695 put); 不再按 ATM±N 裁剪。
        # 行情线占用由「按视口取价」控制 (只对当前滚动可见行拉快照), 而非靠少显示。
        # 各到期日实际行权价不同 → 建表时按到期日取真实行权价 (见 _ensure_table_built),
        # 全链 union 仅作取不到时的回退。
        self._all_strikes = sorted(strikes)
        self._strikes = self._all_strikes
        self._recompute_atm_row()

        print(f"[DEBUG] Option chain: {len(self._all_expirations)} total expirations, "
              f"{len(self._all_strikes)} union strikes, "
              f"ATM row={self._atm_row}, stock_price={stock_price}", flush=True)

        self.title_label.setText(f"期权链 — {symbol}")

        # Categorize expirations into range buckets
        self._categorize_expirations()

        # Update range button states
        self._update_range_buttons()
        self._range_bar.show()

        # Auto-select the first range that has expirations
        auto_range = "全部"
        for name in RANGE_NAMES:
            if self._range_buckets.get(name):
                auto_range = name
                break

        self._apply_range_filter(auto_range)

    def _categorize_expirations(self):
        """Classify each expiration into a range bucket."""
        now = datetime.now()
        today = now.date()

        # End of this week (Sunday)
        days_until_sunday = 6 - today.weekday()  # Monday=0, Sunday=6
        end_of_week = today + timedelta(days=days_until_sunday)

        # Next week
        next_monday = end_of_week + timedelta(days=1)
        next_sunday = next_monday + timedelta(days=6)

        # Current month end
        if now.month == 12:
            this_month_end = today.replace(year=now.year + 1, month=1, day=1) - timedelta(days=1)
        else:
            this_month_end = today.replace(month=now.month + 1, day=1) - timedelta(days=1)

        # Next month
        if now.month == 12:
            next_month_start = today.replace(year=now.year + 1, month=1, day=1)
            next_month_end = today.replace(year=now.year + 1, month=2, day=1) - timedelta(days=1)
        elif now.month == 11:
            next_month_start = today.replace(month=12, day=1)
            next_month_end = today.replace(year=now.year + 1, month=1, day=1) - timedelta(days=1)
        else:
            next_month_start = today.replace(month=now.month + 1, day=1)
            next_month_end = today.replace(month=now.month + 2, day=1) - timedelta(days=1)

        buckets: dict[str, list[str]] = {name: [] for name in RANGE_NAMES}

        for exp_str in self._all_expirations:
            exp_date = datetime.strptime(exp_str, "%Y%m%d").date()

            if exp_date == today:
                buckets["0DTE"].append(exp_str)
            elif exp_date <= end_of_week:
                buckets["本周"].append(exp_str)
            elif next_monday <= exp_date <= next_sunday:
                buckets["下周"].append(exp_str)
            elif exp_date <= this_month_end:
                buckets["本月"].append(exp_str)
            elif next_month_start <= exp_date <= next_month_end:
                buckets["下月"].append(exp_str)
            else:
                buckets["远月"].append(exp_str)

            # "全部" always gets everything
            buckets["全部"].append(exp_str)

        self._range_buckets = buckets

    def _update_range_buttons(self):
        """Enable/disable range buttons based on available expirations."""
        for name, btn in self._range_buttons.items():
            has_data = bool(self._range_buckets.get(name))
            self._apply_range_btn_style(btn, selected=False, enabled=has_data)

    def _on_range_clicked(self, range_name: str):
        self._apply_range_filter(range_name)

    def _apply_range_filter(self, range_name: str):
        """Filter expirations by range and rebuild tabs."""
        self._active_range = range_name

        # Update button styles
        for name, btn in self._range_buttons.items():
            has_data = bool(self._range_buckets.get(name))
            is_selected = (name == range_name)
            self._apply_range_btn_style(btn, selected=is_selected, enabled=has_data)
            if is_selected:
                btn.setChecked(True)

        # Get filtered expirations
        filtered = self._range_buckets.get(range_name, [])
        self._expirations = filtered[:MAX_EXPIRY_TABS_PER_RANGE]

        # Rebuild tabs
        self.tab_widget.blockSignals(True)
        self._unsubscribe_all()
        self.tab_widget.clear()
        self._options.clear()

        for exp in self._expirations:
            display = self._format_expiry(exp)
            table = self._create_table(exp)
            self.tab_widget.addTab(table, display)

        self.tab_widget.blockSignals(False)

        # Load first tab
        if self._expirations:
            self._on_tab_changed(0)

    def select_expiry(self, expiry: str) -> bool:
        """跳到指定到期日的 Tab (双击持仓/监控/委托里的期权时联动)。

        到期日不在当前 range 的可见 Tab 里时, 自动切到第一个包含它的 range
        再选中; 找不到 (已过期/不属于当前标的) 返回 False, 不动当前显示。
        """
        if not expiry or expiry not in self._all_expirations:
            return False
        if expiry in self._expirations:
            self.tab_widget.setCurrentIndex(self._expirations.index(expiry))
            return True
        for name in RANGE_NAMES:
            bucket = self._range_buckets.get(name) or []
            if expiry in bucket[:MAX_EXPIRY_TABS_PER_RANGE]:
                self._apply_range_filter(name)
                self.tab_widget.setCurrentIndex(self._expirations.index(expiry))
                return True
        return False

    def _format_expiry(self, exp: str) -> str:
        """Format expiry for tab display. Shows DTE."""
        today = datetime.now()
        exp_date = datetime.strptime(exp, "%Y%m%d")
        dte = (exp_date - today).days
        if dte < 0:
            dte = 0
        month_day = f"{exp[4:6]}/{exp[6:8]}"
        if dte == 0:
            return f"0DTE {month_day}"
        elif dte == 1:
            return f"1DTE {month_day}"
        else:
            return f"{dte}D {month_day}"

    def _create_table(self, expiry: str) -> QTableWidget:
        """Create the T-shaped table **shell** for one expiry (rows built lazily).

        显示全部行权价后, 单条链可能有几百档 × 多个到期日 Tab。若在加载时一次性
        把每个 Tab 的行都建出来, 主线程会卡顿。故这里只建表头/列, 行留到该 Tab
        首次显示时 (`_on_tab_changed` → `_ensure_table_built`) 再填充。
        """
        headers = [
            "C.Bid", "C.Ask", "C.Last", "C.Vol",
            "Strike",
            "P.Bid", "P.Ask", "P.Last", "P.Vol",
        ]
        table = QTableWidget(0, len(headers))
        table.setHorizontalHeaderLabels(headers)
        table.verticalHeader().setVisible(False)
        table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        table.setSelectionBehavior(QAbstractItemView.SelectRows)
        table.setAlternatingRowColors(False)
        # 表格吃满 Tab 页的空间, 且地板压得很低 → 纵向空间紧张时它自己出滚动条,
        # 而不是把 sizeHint 顶成一个大块去挤压别的模块。
        table.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        table.setMinimumHeight(60)
        table.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        table.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)
        table._expiry = expiry   # 该 Tab 的到期日 (供懒填充/点击用)
        table._built = False     # 行是否已填充

        # Column widths
        header = table.horizontalHeader()
        for i in range(len(headers)):
            if i == 4:  # Strike column
                header.setSectionResizeMode(i, QHeaderView.Fixed)
                table.setColumnWidth(i, 70)
            else:
                header.setSectionResizeMode(i, QHeaderView.Stretch)

        table.cellClicked.connect(lambda r, c: self._on_cell_clicked(table, r, c))
        # 双击 → 该期权今日 1 分钟图 (双击的首次单击已按单击处理, 载入点价梯不冲突)
        table.cellDoubleClicked.connect(
            lambda r, c: self._on_cell_double_clicked(table, r, c)
        )
        # 滚动该到期日表 → 去抖后只对新进入视口的行拉快照 (省行情线)
        table.verticalScrollBar().valueChanged.connect(self._on_table_scrolled)
        return table

    def _ensure_table_built(self, table: QTableWidget):
        """首次显示该到期日 Tab 时填充行权价行 (懒加载)。

        行权价按**该到期日真实存在**的取 —— 已缓存则立即填; 未缓存则显示「加载中」
        并发起后台 reqContractDetails (`_start_strikes_fetch`), 数据回来再填
        (`_on_strikes_ready`)。这样不会用全链 union 拼出该到期日不存在的合约
        (否则整张表空, 如 NVDA 07/10)。
        """
        if getattr(table, "_built", False):
            return
        expiry = getattr(table, "_expiry", "")
        cached = self._real_strikes.get(expiry)
        if cached is not None:
            self._populate_table_rows(table, cached)
        else:
            self._show_loading_placeholder(table)
            self._start_strikes_fetch(expiry)

    def _show_loading_placeholder(self, table: QTableWidget):
        """该到期日真实行权价还在后台拉取时, 显示一行提示。"""
        table.setRowCount(1)
        item = QTableWidgetItem("正在加载该到期日合约…")
        item.setTextAlignment(Qt.AlignCenter)
        item.setForeground(QBrush(QColor(COLOR_TEXT_DIM)))
        table.setItem(0, 4, item)
        table.setRowHeight(0, 28)

    def _start_strikes_fetch(self, expiry: str):
        """后台 reqContractDetails 取该到期日真实行权价 (结果经信号回 GUI 线程)。"""
        if not expiry or expiry in self._fetching_strikes or self._engine is None:
            return
        if not hasattr(self._engine, "request_option_strikes_live"):
            # 旧引擎: 回退到 union, 直接当作就绪 (ok=False)
            self._strikes_ready.emit(self._symbol, expiry, [], False)
            return
        self._fetching_strikes.add(expiry)
        eng, sym = self._engine, self._symbol

        def work():
            try:
                strikes, _tc, ok = eng.request_option_strikes_live(sym, expiry)
            except Exception:
                strikes, ok = [], False
            self._strikes_ready.emit(sym, expiry, strikes or [], bool(ok))

        threading.Thread(target=work, daemon=True).start()

    def _on_strikes_ready(self, symbol: str, expiry: str, strikes: list, ok: bool):
        """(GUI 线程) 后台取到该到期日真实行权价 → 缓存并填当前 Tab。

        - 结果对应的 symbol 已不是当前标的 → 丢弃 (换标的期间的过期结果);
        - ok=True : 用权威行权价 (为空 = 该到期日确无合约 → 表内提示, 不拼 union);
        - ok=False: 超时/旧引擎 → 回退全链 union (至少 ATM 附近能出价)。"""
        self._fetching_strikes.discard(expiry)
        if symbol != self._symbol:
            return  # 过期结果 (已切到别的标的)
        resolved = list(strikes) if ok else list(self._all_strikes)
        self._real_strikes[expiry] = resolved

        idx = self.tab_widget.currentIndex()
        if not (0 <= idx < len(self._expirations)) or self._expirations[idx] != expiry:
            return  # 期间已切走; 数据已缓存, 下次切回即用
        table = self.tab_widget.widget(idx)
        if not isinstance(table, QTableWidget) or getattr(table, "_built", False):
            return
        self._populate_table_rows(table, resolved)
        self._strikes = table._strikes
        self._recompute_atm_row()
        QTimer.singleShot(0, lambda t=table: self._center_and_snapshot(t))

    def _populate_table_rows(self, table: QTableWidget, strikes: list):
        """用给定行权价填充表格行 (建表的实际填充步骤)。"""
        table._strikes = strikes
        expiry = getattr(table, "_expiry", "")

        if not strikes:
            # 该到期日确无合约 (reqContractDetails 返回空) → 提示而非拼假行
            table.setRowCount(1)
            item = QTableWidgetItem("该到期日无可用合约")
            item.setTextAlignment(Qt.AlignCenter)
            item.setForeground(QBrush(QColor(COLOR_TEXT_DIM)))
            table.setItem(0, 4, item)
            table.setRowHeight(0, 28)
            table._built = True
            return

        table.setRowCount(len(strikes))

        for row, strike in enumerate(strikes):
            # Strike column (center)
            strike_str = f"{int(strike)}" if strike == int(strike) else f"{strike:g}"
            item = QTableWidgetItem(strike_str)
            item.setTextAlignment(Qt.AlignCenter)
            item.setData(Qt.UserRole, strike)

            # ATM highlighting
            if self._stock_price > 0:
                if abs(strike - self._stock_price) <= 1.0:
                    item.setBackground(QBrush(QColor(COLOR_ATM_HIGHLIGHT)))

            table.setItem(row, 4, item)

            # Create OptionInfo for Call and Put
            for right in ("C", "P"):
                opt = OptionInfo(
                    symbol=self._symbol,
                    expiry=expiry,
                    strike=strike,
                    right=right,
                )
                self._options[opt.to_ibkr_key()] = opt

            # Initialize price cells
            for col in range(9):
                if col == 4:
                    continue
                item = QTableWidgetItem("—")
                item.setTextAlignment(Qt.AlignCenter)
                item.setForeground(QBrush(QColor(COLOR_TEXT_DIM)))
                table.setItem(row, col, item)

        # Set row height
        for row in range(table.rowCount()):
            table.setRowHeight(row, 28)

        table._built = True

    def _on_table_scrolled(self, _value: int):
        """表格滚动: 重启去抖定时器, 停下后对可见行拉快照。"""
        self._snap_timer.start(250)

    def _on_tab_changed(self, index: int):
        """切到新到期日: 把表滚到最接近现价的行权价居中, 再对可见行拉快照。

        居中**延后**到下一轮事件循环: 刚建/刚切的表此刻 viewport 高度可能还是 0,
        立即 scrollToItem 会无效 → 表停在最上面(最低行权价, 如 0.5)。延后到布局
        完成后再滚, 才能真正居中到平值附近。
        """
        if index < 0 or index >= len(self._expirations):
            return
        table = self.tab_widget.widget(index)
        if not isinstance(table, QTableWidget):
            return
        self._ensure_table_built(table)
        # 已就绪(缓存命中)才居中/取价; 否则等 _on_strikes_ready 数据回来再处理
        if getattr(table, "_built", False):
            self._strikes = getattr(table, "_strikes", self._all_strikes)
            QTimer.singleShot(0, lambda t=table: self._center_and_snapshot(t))

    def _center_and_snapshot(self, table: QTableWidget):
        """布局就绪后: 先把最接近现价的行权价滚到居中, 再对(居中后的)可见行拉快照。"""
        if table is not self.tab_widget.currentWidget():
            return  # 期间又切走了, 放弃
        self._scroll_table_to_atm(table)
        self._request_snapshots()

    def _recompute_atm_row(self):
        """用**最新现价**重算最接近的行权价行 (现价随实时行情更新, 故每次切表都重算)。"""
        if not self._strikes:
            self._atm_row = 0
        elif self._stock_price > 0:
            self._atm_row = min(range(len(self._strikes)),
                                key=lambda i: abs(self._strikes[i] - self._stock_price))
        else:
            self._atm_row = len(self._strikes) // 2

    def _scroll_table_to_atm(self, table: QTableWidget):
        """把表格滚动到最接近现价的行权价行居中, 使默认可见区聚焦平值附近。"""
        self._recompute_atm_row()
        if 0 <= self._atm_row < table.rowCount():
            item = table.item(self._atm_row, 4)
            if item:
                table.scrollToItem(item, QAbstractItemView.PositionAtCenter)

    def _visible_strike_rows(self, table: QTableWidget) -> list[int]:
        """当前视口内可见的行索引 (上下各留几行缓冲, 滚动时预取)。

        首次显示时视口高度可能尚未布局好 (rowAt 返回 -1) → 回退到以 ATM 为中心的
        一个窗口, 保证平值附近先有报价。
        """
        n = table.rowCount()
        if n == 0:
            return []
        vp_h = table.viewport().height()
        top = table.rowAt(0)
        bottom = table.rowAt(max(vp_h - 1, 0))
        if top < 0 or bottom < 0 or vp_h <= 0:
            # 视口未就绪: 以 ATM 为中心取一窗口 (约半个行情线预算的行数)
            half = max(8, MAX_SIMULTANEOUS_STREAMS // 4)
            lo = max(0, self._atm_row - half)
            hi = min(n - 1, self._atm_row + half)
            return list(range(lo, hi + 1))
        buf = 4
        lo = max(0, top - buf)
        hi = min(n - 1, bottom + buf)
        return list(range(lo, hi + 1))

    def _request_snapshots(self):
        """对当前到期日**视口内可见**的行权价拉一次 one-shot 快照报价。

        用完即弃 (IBKR 自动取消), **不占常驻行情线**。只取当前滚动可见的行
        (而非整条链), 这样既能滚到任意远端行权价取价, 又不会因一次性订阅几百档
        压垮 Gateway 行情线上限。快照异步到达, 由 `_refresh_prices` 绘出;
        另排几个 singleShot 让它更快显示。
        """
        if not self._engine or not hasattr(self._engine, "snapshot_option_tick"):
            return
        idx = self.tab_widget.currentIndex()
        if idx < 0 or idx >= len(self._expirations):
            return
        table = self.tab_widget.widget(idx)
        if not isinstance(table, QTableWidget):
            return
        expiry = self._expirations[idx]
        count = 0
        for row in self._visible_strike_rows(table):
            if row >= len(self._strikes):
                continue
            strike = self._strikes[row]
            for right in ("C", "P"):
                if count >= MAX_SIMULTANEOUS_STREAMS:
                    break
                opt = OptionInfo(
                    symbol=self._symbol, expiry=expiry,
                    strike=strike, right=right,
                )
                self._engine.snapshot_option_tick(opt)
                count += 1
        # 快照异步返回, 多排几次重绘让数据尽快出现
        for delay_ms in (300, 800, 1500, 2500):
            QTimer.singleShot(delay_ms, self._refresh_prices)

    def _unsubscribe_all(self):
        """Cancel all current subscriptions."""
        if self._engine:
            for key, req_id in list(self._sub_req_ids.items()):
                self._engine.unsubscribe_tick(req_id)
        self._sub_req_ids.clear()

    def _refresh_prices(self):
        """Update displayed prices from tick data."""
        if not self._engine:
            return

        # Update title with real-time underlying price
        if self._symbol:
            stock_key = f"__stock__{self._symbol}"
            stock_tick = self._engine.get_tick(stock_key)
            price = stock_tick.get("last", 0)
            if price <= 0:
                bid = stock_tick.get("bid", 0)
                ask = stock_tick.get("ask", 0)
                price = (bid + ask) / 2 if bid > 0 and ask > 0 else (bid or ask)
            if price > 0:
                self._stock_price = price
                # 标的 IV (generic tick 106 → tickType 24) 显示在价格右侧; 无则不显示
                iv = stock_tick.get("iv", 0) or 0
                iv_txt = f"  IV {iv * 100:.1f}%" if iv > 0 else ""
                self.title_label.setText(
                    f"期权链 — {self._symbol}  ${price:.2f}{iv_txt}"
                )

        idx = self.tab_widget.currentIndex()
        if idx < 0 or idx >= len(self._expirations):
            return

        table = self.tab_widget.widget(idx)
        if not isinstance(table, QTableWidget):
            return
        if not getattr(table, "_built", False):
            return  # 行尚未懒填充, 无可更新单元格

        expiry = self._expirations[idx]
        strikes = getattr(table, "_strikes", self._strikes)

        for row, strike in enumerate(strikes):
            for right_idx, right in enumerate(("C", "P")):
                key = f"{self._symbol}_{expiry}_{right}_{strike}"
                tick = self._engine.get_tick(key)

                bid = tick.get("bid", 0)
                ask = tick.get("ask", 0)
                last = tick.get("last", 0)
                volume = tick.get("volume", 0)

                if right == "C":
                    cols = (0, 1, 2)  # bid, ask, last
                    vol_col = 3
                else:
                    cols = (5, 6, 7)  # bid, ask, last
                    vol_col = 8

                for ci, val in zip(cols, (bid, ask, last)):
                    item = table.item(row, ci)
                    if item and val > 0:
                        text = f"{val:.2f}"
                        if item.text() != text:   # skip unchanged cells (no repaint)
                            item.setText(text)
                            # Color code: green bid, red ask, default for last.
                            # Only set when text changes — the cached brushes
                            # avoid per-tick QBrush/QColor allocation.
                            if ci in (0, 5):    # bid
                                item.setForeground(self._brush_bid)
                            elif ci in (1, 6):  # ask
                                item.setForeground(self._brush_ask)
                            else:
                                item.setForeground(self._brush_text)

                # Volume column (was never rendered before)
                vol_item = table.item(row, vol_col)
                if vol_item and volume > 0:
                    vtext = str(int(volume))
                    if vol_item.text() != vtext:
                        vol_item.setText(vtext)
                        vol_item.setForeground(self._brush_text)

                # Update the OptionInfo
                opt = self._options.get(key)
                if opt:
                    opt.bid = tick.get("bid", 0)
                    opt.ask = tick.get("ask", 0)
                    opt.last = tick.get("last", 0)

    def _option_at(self, table: QTableWidget, row: int, col: int):
        """按表格 (row, col) 解析出对应 OptionInfo (左侧列=Call, 右侧列=Put)。"""
        strikes = getattr(table, "_strikes", self._strikes)
        if row < 0 or row >= len(strikes):
            return None

        idx = self.tab_widget.currentIndex()
        if idx < 0 or idx >= len(self._expirations):
            return None

        strike = strikes[row]
        expiry = self._expirations[idx]

        # Determine Call or Put based on column
        if col <= 3:
            right = "C"
        elif col >= 5:
            right = "P"
        else:
            right = "C"  # Strike column -> default to Call

        key = f"{self._symbol}_{expiry}_{right}_{strike}"
        return self._options.get(key)

    def _on_cell_clicked(self, table: QTableWidget, row: int, col: int):
        """Click on a cell -> emit option_selected."""
        opt = self._option_at(table, row, col)
        if opt:
            self.option_selected.emit(opt)

    def _on_cell_double_clicked(self, table: QTableWidget, row: int, col: int):
        """双击合约 -> 打开该期权今日 1 分钟图。"""
        opt = self._option_at(table, row, col)
        if opt:
            self.chart_requested.emit(opt)

    def update_stock_price(self, price: float):
        """Update ATM highlighting when stock price changes."""
        self._stock_price = price

    def cleanup(self):
        """Cleanup subscriptions."""
        self._refresh_timer.stop()
        self._mom_timer.stop()
        self._snap_timer.stop()
        self._unsubscribe_all()
