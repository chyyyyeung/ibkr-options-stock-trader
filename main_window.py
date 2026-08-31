"""Main window layout — assembles all widgets."""

import threading
from datetime import datetime

from PyQt5.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QSplitter, QTabWidget, QMessageBox, QStatusBar,
    QPushButton, QLabel, QApplication, QSizePolicy, QMenu, QAction,
    QScrollArea, QFrame,
)
from PyQt5.QtCore import Qt, QTimer, pyqtSignal, QEvent, QSettings, QRect

from config import (
    COLOR_BG, COLOR_BG_DARK, COLOR_BG_PANEL, COLOR_TEXT,
    COLOR_BORDER, COLOR_ACCENT, COLOR_GREEN, COLOR_RED,
    SPX_SESSION_GTH_START, SPX_SESSION_GTH_END,
    SPX_SESSION_RTH_START, SPX_SESSION_RTH_END,
    DATA_CONNECTION_ERROR_CODES,
    THEME_NAME, UI_RADIUS, save_theme_name,
)
from config import FUTURES_SPECS, FUTURES_MAX_EXPIRIES, FUTURES_REQUIRE_BRACKET
from models import OptionInfo, OrderAction, OrderType, TradingMode
from ibkr_engine import IBKREngine
from paper_engine import PaperEngine
from conditional_orders import ConditionalOrderManager
from watchlist import WatchListManager
from sound_alerts import play_fill
from widgets.symbol_bar import SymbolBar
from widgets.option_chain import OptionChainWidget
from widgets.price_ladder import PriceLadder
from widgets.position_panel import PositionPanel
from widgets.order_panel import OrderPanel
from widgets.account_bar import AccountBar
from widgets.option_calculator import OptionCalculator
from widgets.strategy_window import StrategyPanel
from widgets.watch_panel import WatchPanel
from widgets.ui_util import disable_ime
# ChartWindow is imported lazily (first chart open) — it pulls in
# numpy + pyqtgraph (~25MB), which shouldn't load at startup


DARK_STYLESHEET = f"""
    QMainWindow, QWidget {{
        background-color: {COLOR_BG};
        color: {COLOR_TEXT};
    }}
    QTableWidget {{
        background-color: {COLOR_BG_DARK};
        alternate-background-color: {COLOR_BG};
        color: {COLOR_TEXT};
        gridline-color: {COLOR_BORDER};
        border: 1px solid {COLOR_BORDER};
        selection-background-color: {COLOR_BG_PANEL};
    }}
    QTableWidget::item {{
        padding: 2px 4px;
    }}
    QHeaderView::section {{
        background-color: {COLOR_BG_PANEL};
        color: {COLOR_TEXT};
        border: 1px solid {COLOR_BORDER};
        padding: 4px;
        font-weight: bold;
    }}
    QTabWidget::pane {{
        border: 1px solid {COLOR_BORDER};
        background-color: {COLOR_BG_DARK};
    }}
    QTabBar::tab {{
        background-color: {COLOR_BG_DARK};
        color: {COLOR_TEXT};
        padding: 6px 12px;
        border: 1px solid {COLOR_BORDER};
        border-bottom: none;
        margin-right: 2px;
    }}
    QTabBar::tab:selected {{
        background-color: {COLOR_BG_PANEL};
        color: {COLOR_ACCENT};
        font-weight: bold;
    }}
    QLineEdit {{
        background-color: {COLOR_BG_DARK};
        color: {COLOR_TEXT};
        border: 1px solid {COLOR_BORDER};
        padding: 4px 8px;
        border-radius: {UI_RADIUS};
    }}
    QComboBox {{
        background-color: {COLOR_BG_DARK};
        color: {COLOR_TEXT};
        border: 1px solid {COLOR_BORDER};
        padding: 4px 8px;
        border-radius: {UI_RADIUS};
    }}
    QComboBox::drop-down {{
        border: none;
    }}
    QComboBox QAbstractItemView {{
        background-color: {COLOR_BG_DARK};
        color: {COLOR_TEXT};
        selection-background-color: {COLOR_BG_PANEL};
    }}
    QSpinBox {{
        background-color: {COLOR_BG_DARK};
        color: {COLOR_TEXT};
        border: 1px solid {COLOR_BORDER};
        padding: 4px;
        border-radius: {UI_RADIUS};
    }}
    QLabel {{
        color: {COLOR_TEXT};
    }}
    QSplitter::handle {{
        background-color: {COLOR_BORDER};
    }}
    QScrollArea {{
        border: none;
    }}
    QScrollBar:vertical {{
        background-color: {COLOR_BG_DARK};
        width: 10px;
    }}
    QScrollBar::handle:vertical {{
        background-color: {COLOR_BORDER};
        border-radius: 4px;
        min-height: 20px;
    }}
    QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
        height: 0;
    }}
    QStatusBar {{
        background-color: {COLOR_BG_DARK};
        color: {COLOR_TEXT};
    }}
"""

# 科幻主题追加规则: 选中 Tab 顶部电光青描边、输入框聚焦发光边、
# 表头字距拉开 —— 只叠加视觉, 不动任何布局/交互
if THEME_NAME == "scifi":
    DARK_STYLESHEET += f"""
    QTabBar::tab {{
        border-top: 2px solid transparent;
    }}
    QTabBar::tab:selected {{
        border-top: 2px solid {COLOR_ACCENT};
        background-color: {COLOR_BG_DARK};
    }}
    QLineEdit:focus, QComboBox:focus, QSpinBox:focus {{
        border: 1px solid {COLOR_ACCENT};
    }}
    QHeaderView::section {{
        letter-spacing: 1px;
        border: none;
        border-bottom: 1px solid {COLOR_ACCENT};
        border-right: 1px solid {COLOR_BORDER};
    }}
    QToolTip {{
        background-color: {COLOR_BG_PANEL};
        color: {COLOR_TEXT};
        border: 1px solid {COLOR_ACCENT};
    }}
    QScrollBar::handle:vertical {{
        background-color: {COLOR_BG_PANEL};
        border: 1px solid {COLOR_BORDER};
        border-radius: 0;
    }}
    QSplitter::handle {{
        background-color: {COLOR_BG_DARK};
    }}
"""


class MainWindow(QMainWindow):
    """Main application window."""

    _search_validated = pyqtSignal(object)  # OptionInfo — validated search result
    _futures_resolved = pyqtSignal(object)  # (symbol, contracts) — futures resolved off-thread

    def __init__(self):
        super().__init__()
        self.setWindowTitle("IBKR 点价交易")
        # 最小尺寸随屏幕收缩 → 小屏/高 DPI 缩放下窗口仍能放进可用区域。
        # 硬写 900x600 在 1707x1067 (2560x1600 @150%) 这类逻辑分辨率下会顶满,
        # 导致各模块被挤到最小高度 (期权链只剩一行)。实际大小由 _restore_layout 定。
        avail = self._available_rect()
        self.setMinimumSize(
            min(760, max(480, avail.width() - 40)),
            min(520, max(400, avail.height() - 60)),
        )
        self._fit_to_screen(save=False)

        # 记忆窗口大小与各 splitter 分割位置 (跨会话持久化)
        self._settings = QSettings("MoneyTrader", "ibkr_options_gui")

        # Engines
        self.ibkr_engine = IBKREngine()
        self.paper_engine = PaperEngine(self.ibkr_engine)
        self._active_engine = self.paper_engine  # Default to paper

        # 本地条件单 (止盈/止损) 管理器 —— 回调用 self._active_engine (随模式切换)
        self.cond_manager = ConditionalOrderManager(self)
        self.cond_manager.configure(
            get_tick=lambda key: self._active_engine.get_tick(key),
            place=lambda opt, action, lmt, qty, outside, market: self._place_order(
                opt, OrderAction(action),
                OrderType.MARKET if market else OrderType.LIMIT, lmt, qty, outside),
            subscribe=lambda opt: self._active_engine.subscribe_option_tick(opt),
            unsubscribe=lambda req_id: self._active_engine.unsubscribe_tick(req_id),
            subscribe_under=lambda symbol: self._active_engine.subscribe_watch_tick(symbol),
            get_position_qty=lambda key: self._active_engine.get_position_qty(key),
            positions_ready=lambda: self._active_engine.positions_synced,
        )

        # 自选监控 (watch list): 实时看价 + 到价警报, 持久化 watchlist.json
        self.watch_manager = WatchListManager(self)
        self.watch_manager.configure(
            get_tick=lambda key: self._active_engine.get_tick(key),
            subscribe=lambda opt: self._active_engine.subscribe_option_tick(opt),
            unsubscribe=lambda req_id: self._active_engine.unsubscribe_tick(req_id),
        )

        self._current_symbol = "SPY"
        self._current_option: OptionInfo | None = None
        self._instrument = "OPT"   # "OPT"(默认) / "STK" / "FUT"
        self._future_expiries: list = []  # 当前期货标的的合约月份 [{expiry,con_id,...}]
        self._chart_windows: list = []  # list[ChartWindow]
        # 期权 1 分图**单实例**: 双击新合约自动关掉旧合约的图 (停止其轮询取数)
        self._option_chart = None  # OptionChartWindow | None
        # 限价开多后, 待成交回报再挂的止盈/止损: {entry_order_id: {option,qty,bracket,outside}}
        self._pending_buy_brackets: dict[int, dict] = {}

        # Detachable price ladder state
        self._ladder_detached = False
        self._ladder_window: QMainWindow | None = None
        self._embedded_chart = None  # ChartWindow | None

        self._build_ui()
        self._connect_signals()

        # 关掉所有输入框的输入法挂载 —— 搜狗拼音一挂上来就弹 SoPY_Status 空窗
        # 并卡 1-3 秒 (双击监控切合约时必现)。详见 widgets/ui_util.disable_ime。
        # 必须放在 _build_ui 之后: 只对已经建好的控件生效。
        disable_ime(self)

        self.setStyleSheet(DARK_STYLESHEET)
        self.statusBar().showMessage("就绪 — 点击「连接」开始")

        # Session indicator timer (updates every 10 seconds)
        self._session_timer = QTimer()
        self._session_timer.timeout.connect(self._update_session_indicator)
        self._session_timer.start(10_000)
        self._update_session_indicator()

        # 恢复上次的窗口大小与各分割位置 (若有)
        self._restore_layout()

    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        main_layout = QVBoxLayout(central)
        main_layout.setContentsMargins(4, 4, 4, 4)
        main_layout.setSpacing(4)

        # ── Top bar ──
        top_bar_layout = QHBoxLayout()

        self.symbol_bar = SymbolBar()
        top_bar_layout.addWidget(self.symbol_bar, stretch=1)

        self._chart_btn = QPushButton("K线图")
        self._chart_btn.setFixedHeight(30)
        self._chart_btn.setStyleSheet(
            f"QPushButton {{ background-color: {COLOR_BG_PANEL}; color: {COLOR_ACCENT}; "
            f"border: 1px solid {COLOR_BORDER}; padding: 2px 12px; border-radius: 3px; "
            f"font-weight: bold; }}"
            f"QPushButton:hover {{ background-color: {COLOR_ACCENT}; color: {COLOR_BG}; }}"
        )
        self._chart_btn.clicked.connect(self._on_open_chart)
        top_bar_layout.addWidget(self._chart_btn)

        # ── 布局菜单: 适应屏幕 / 重置布局 / 期权链最大化 ──
        # 记忆的分割位置一旦存坏 (或换了分辨率更小的屏), 就会一直复现;
        # 这里给一个一键恢复的出口, 免得只能去删注册表。
        self._layout_btn = QPushButton("布局 ▾")
        self._layout_btn.setFixedHeight(30)
        self._layout_btn.setStyleSheet(self._chart_btn.styleSheet())
        self._layout_btn.setToolTip(
            "适应屏幕 — 窗口缩放到当前屏幕并按比例重排各模块\n"
            "重置布局 — 丢弃记忆的窗口大小/分割位置, 恢复出厂默认\n"
            "期权链最大化 — 把纵向空间尽量让给期权链"
        )
        layout_menu = QMenu(self)
        act_fit = QAction("适应屏幕", self)
        act_fit.setShortcut("Ctrl+0")
        act_fit.triggered.connect(self._on_fit_to_screen)
        act_reset = QAction("重置布局 (恢复默认)", self)
        act_reset.triggered.connect(self._on_reset_layout)
        act_max_chain = QAction("期权链最大化", self)
        act_max_chain.setShortcut("Ctrl+1")
        act_max_chain.triggered.connect(self._on_maximize_chain)
        for a in (act_fit, act_max_chain, act_reset):
            layout_menu.addAction(a)
            self.addAction(a)  # 让快捷键在无菜单弹出时也生效
        layout_menu.insertSeparator(act_reset)
        self._layout_btn.setMenu(layout_menu)
        top_bar_layout.addWidget(self._layout_btn)

        # Session indicator (shows current market session for SPX options)
        self._session_label = QLabel("--")
        self._session_label.setFixedHeight(30)
        self._session_label.setStyleSheet(
            f"color: {COLOR_TEXT}; background-color: {COLOR_BG_PANEL}; "
            f"border: 1px solid {COLOR_BORDER}; padding: 2px 10px; "
            f"border-radius: 3px; font-size: 12px; font-weight: bold;"
        )
        self._session_label.setToolTip(
            "SPX 期权交易时段 (ET)\n"
            "GTH 夜盘: 20:15 - 09:15\n"
            "RTH 正常盘: 09:30 - 16:15"
        )
        top_bar_layout.addWidget(self._session_label)

        main_layout.addLayout(top_bar_layout)

        # ── Account bar ──
        self.account_bar = AccountBar()
        main_layout.addWidget(self.account_bar)

        # ── Main content: vertical splitter ──
        self.main_splitter = QSplitter(Qt.Vertical)

        # Top: Option chain
        self.option_chain = OptionChainWidget()
        # 期权链是主视图 → 优先吸收纵向空间, 且给一个「至少看得见几行」的地板,
        # 免得下方点价梯的固定高度控件把它挤成一行。
        self.option_chain.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.option_chain.setMinimumHeight(self._px(150))
        self.main_splitter.addWidget(self.option_chain)

        # Bottom: horizontal splitter (price ladder | position/order panels)
        self.bottom_splitter = QSplitter(Qt.Horizontal)

        # Left: Price ladder
        self.price_ladder = PriceLadder()
        self.bottom_splitter.addWidget(self.price_ladder)

        # Right: Position + Order + Watch tabs (top) + 期权理论价计算器 (bottom-right corner)
        self.right_tabs = QTabWidget()
        self.position_panel = PositionPanel()
        self.order_panel = OrderPanel()
        self.watch_panel = WatchPanel()
        self.watch_panel.set_manager(self.watch_manager)
        self.right_tabs.addTab(self.position_panel, "持仓")
        self.right_tabs.addTab(self.order_panel, "委托")
        self.right_tabs.addTab(self.watch_panel, "监控")

        self.right_splitter = QSplitter(Qt.Vertical)
        self.right_splitter.addWidget(self.right_tabs)
        self.calculator = OptionCalculator()
        # 计算器内容比较高 (两列 + 多行输入)。放进滚动区后:
        #   · 空间够 → 和以前一样铺满 (setWidgetResizable);
        #   · 空间不够 → 出滚动条, 用滚轮看全, 而不是把内容截断/顶掉别的模块。
        self._calc_scroll = QScrollArea()
        self._calc_scroll.setWidget(self.calculator)
        self._calc_scroll.setWidgetResizable(True)
        self._calc_scroll.setFrameShape(QFrame.NoFrame)
        self._calc_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self._calc_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        # 滚动区自身的地板要小, 否则它又变成一个撑住布局的固定块
        self._calc_scroll.setMinimumHeight(self._px(90))
        self.right_splitter.addWidget(self._calc_scroll)
        self.right_splitter.setSizes([520, 300])
        # 右侧竖向: 持仓/委托 Tab 主要吸收增长, 计算器小幅跟随
        self.right_splitter.setStretchFactor(0, 5)
        self.right_splitter.setStretchFactor(1, 2)
        self.right_splitter.setChildrenCollapsible(False)
        self.bottom_splitter.addWidget(self.right_splitter)

        self.bottom_splitter.setSizes([380, 500])
        # 下方横向: 点价梯与右侧面板按 4:5 比例联动缩放
        self.bottom_splitter.setStretchFactor(0, 4)
        self.bottom_splitter.setStretchFactor(1, 5)
        self.bottom_splitter.setChildrenCollapsible(False)
        self.main_splitter.addWidget(self.bottom_splitter)

        # 主竖向: 期权链略优先 (3:2) —— 它是主视图, 且行数多才有用
        self.main_splitter.setStretchFactor(0, 3)
        self.main_splitter.setStretchFactor(1, 2)
        self.main_splitter.setChildrenCollapsible(False)

        # 分隔条加宽 → 好拖。三个 splitter 统一手感。
        for sp in (self.main_splitter, self.bottom_splitter, self.right_splitter):
            sp.setHandleWidth(self._px(6))
            sp.setOpaqueResize(True)

        # ── 中央: 顶层 Tab —「单腿点价」(现有点价梯) /「多腿组合」(策略组合) ──
        # 两个模块相互独立: 单腿点价用左侧点价梯; 多腿组合是嵌入的策略生成器。
        self.center_tabs = QTabWidget()

        single_leg_tab = QWidget()
        single_leg_layout = QVBoxLayout(single_leg_tab)
        single_leg_layout.setContentsMargins(0, 0, 0, 0)
        single_leg_layout.addWidget(self.main_splitter)
        self.center_tabs.addTab(single_leg_tab, "单腿点价")

        # 多腿组合面板 (懒加载期权链: 首次切到该 Tab 才拉)
        self.strategy_panel = StrategyPanel(symbol=self._current_symbol)
        self.center_tabs.addTab(self.strategy_panel, "多腿组合")

        # stretch=1 → 中央 Tab 吃掉顶栏 (30px) 与账户栏 (56px) 之外的全部纵向空间
        main_layout.addWidget(self.center_tabs, stretch=1)

    def _connect_signals(self):
        # Symbol bar
        self.symbol_bar.connect_clicked.connect(self._on_connect)
        self.symbol_bar.disconnect_clicked.connect(self._on_disconnect)
        self.symbol_bar.symbol_changed.connect(self._on_symbol_changed)
        self.symbol_bar.theme_changed.connect(self._on_theme_changed)
        self.symbol_bar.mode_changed.connect(self._on_mode_changed)
        self.symbol_bar.reconnect_requested.connect(self._on_reconnect_requested)
        self.symbol_bar.instrument_changed.connect(self._on_instrument_changed)
        self.symbol_bar.future_expiry_changed.connect(self._on_future_expiry_changed)

        # Option chain -> price ladder
        self.option_chain.option_selected.connect(self._on_option_selected)
        # Option chain 双击 -> 该期权今日 1 分钟图
        self.option_chain.chart_requested.connect(self._on_open_option_chart)

        # Price ladder -> order (limit orders from price clicks)
        self.price_ladder.order_requested.connect(self._on_order_requested)

        # Price ladder -> market orders
        self.price_ladder.market_order_requested.connect(self._on_market_order_requested)

        # Price ladder -> close position
        self.price_ladder.close_position_requested.connect(self._on_close_position_requested)

        # Price ladder -> 只撤当前合约挂单（绝不调用 IBKR 全局撤单）
        self.price_ladder.cancel_symbol_requested.connect(
            self._on_cancel_symbol_requested
        )

        # Price ladder -> detach
        self.price_ladder.detach_requested.connect(self._on_detach_ladder)

        # Price ladder -> 条件单 (止盈/止损)
        self.price_ladder.conditional_requested.connect(self._on_conditional_requested)
        self.price_ladder.conditional_cancel_requested.connect(self._on_conditional_cancel)
        self.cond_manager.changed.connect(self._refresh_conditionals)
        self.cond_manager.triggered.connect(self._on_conditional_triggered)
        self.cond_manager.failed.connect(self._on_conditional_failed)
        self.cond_manager.voided.connect(self._on_conditional_voided)

        # 自选监控
        self.price_ladder.watch_requested.connect(self._on_watch_add)
        self.watch_manager.alerted.connect(self._on_watch_alerted)
        # 点价梯换合约后刷新该合约的条件单显示
        self.price_ladder.option_loaded.connect(self._refresh_conditionals)

        # Price ladder -> contract search
        self.price_ladder.contract_searched.connect(self._on_contract_searched)
        self._search_validated.connect(self._load_validated_contract)
        self._futures_resolved.connect(self._apply_futures_contracts)

        # Position panel -> open ladder
        self.position_panel.position_clicked.connect(self._on_option_selected)

        # Position panel -> 一键平仓 (面板已弹窗确认过, 这里只负责逐笔下单)
        self.position_panel.close_all_requested.connect(self._on_close_all_positions)

        # Position panel -> 账户栏: 账户级未实现盈亏 (持仓汇总)。IBKR 的 reqPnL /
        # reqPnLSingle 在部分账户上不给有效浮盈亏 (error 2150), 账户栏改吃这个汇总值。
        self.position_panel.portfolio_unrealized_changed.connect(
            self.account_bar.on_portfolio_unrealized
        )

        # 双击委托/交易记录的合约 -> 跳到该标的并加载到点价梯
        self.order_panel.option_selected.connect(self._on_option_selected)

        # 双击监控 (watch list) 的合约名 -> 同样跳到点价梯/期权链
        self.watch_panel.option_selected.connect(self._on_option_selected)

        # Order panel -> cancel
        self.order_panel.cancel_requested.connect(self._on_cancel_order)
        self.order_panel.cancel_all_requested.connect(
            self._on_cancel_all_orders_requested
        )

        # 中央 Tab 切换 -> 进入「多腿组合」时懒加载期权链
        self.center_tabs.currentChanged.connect(self._on_center_tab_changed)

        # IBKR engine signals
        self.ibkr_engine.bridge.connected.connect(self._on_connected)
        self.ibkr_engine.bridge.disconnected.connect(self._on_disconnected)
        self.ibkr_engine.bridge.error_received.connect(self._on_error)
        self.ibkr_engine.bridge.order_rejected.connect(self._on_order_rejected)
        self.ibkr_engine.bridge.pnl_single_updated.connect(
            self.position_panel.on_pnl_single
        )

        # IBKR account/portfolio signals
        self.ibkr_engine.bridge.account_summary_updated.connect(self.account_bar.update_account)
        self.ibkr_engine.bridge.pnl_updated.connect(self.account_bar.update_daily_pnl)
        self.ibkr_engine.bridge.computed_daily_pnl.connect(
            self.account_bar.on_computed_daily
        )
        self.ibkr_engine.bridge.portfolio_position_received.connect(
            self.position_panel.on_portfolio_position
        )
        self.ibkr_engine.bridge.portfolio_positions_end.connect(
            self.position_panel.on_portfolio_positions_end
        )
        self.ibkr_engine.bridge.account_summary_end.connect(self._on_account_summary_end)
        self.ibkr_engine.bridge.currency_balance_updated.connect(
            self.account_bar.on_currency_balance
        )
        # 真正成交 → 提示音 (仅真实引擎; 本地模拟不响)
        self.ibkr_engine.bridge.execution_received.connect(self._on_fill_sound)
        # 期货限价开多成交 → 自动挂上止盈/止损 (两个引擎都监听, 按当前模式生效)
        self.ibkr_engine.bridge.execution_received.connect(self._on_exec_arm_bracket)
        # 已平仓交易统计 (笔数/胜率/盈亏比) → 期权链刷新报价左侧。
        # 两个引擎都连着 (模拟也用真实引擎取行情), 故只显示**当前活动引擎**的统计,
        # 避免模拟模式下显示真实账户的当日成交统计。
        self.ibkr_engine.bridge.trade_stats_updated.connect(
            lambda s: self._on_trade_stats(s, self.ibkr_engine)
        )

        # Paper engine signals
        self.paper_engine.bridge.error_received.connect(self._on_error)
        self.paper_engine.bridge.execution_received.connect(self._on_exec_arm_bracket)
        self.paper_engine.bridge.trade_stats_updated.connect(
            lambda s: self._on_trade_stats(s, self.paper_engine)
        )
        self.paper_engine.bridge.account_summary_updated.connect(self.account_bar.update_account)
        self.paper_engine.bridge.pnl_updated.connect(self.account_bar.update_daily_pnl)
        self.paper_engine.bridge.computed_daily_pnl.connect(
            self.account_bar.on_computed_daily
        )
        self.paper_engine.bridge.currency_balance_updated.connect(
            self.account_bar.on_currency_balance
        )

    # ── Connection ────────────────────────────────────────────────────

    def _on_connect(self):
        mode = self.symbol_bar.get_mode()
        self.statusBar().showMessage(f"正在连接 ({mode.label})...")

        # Connect in background thread
        def do_connect():
            success = self.ibkr_engine.connect(mode)
            if not success:
                self.ibkr_engine.bridge.error_received.emit(
                    -1, -1, "连接失败 — 请确认 Gateway/TWS 已登录。若刚才还能用、"
                    "现在连不上, 多半是 Gateway 卡死(JTS死锁), 请重启 Gateway 后再连。"
                )

        threading.Thread(target=do_connect, daemon=True).start()

    def _on_disconnect(self):
        self.account_bar.stop()
        self.option_chain.cleanup()
        self.ibkr_engine.disconnect()

    def _on_connected(self):
        # 结束"切换中"状态 → 重新启用模式下拉 + 标的输入框 (热切换后必须复位,
        # 否则切到模拟/实盘后这俩控件一直禁用, 无法再改标的/再切回)。
        self.symbol_bar.set_switching(False)
        mode = self.ibkr_engine.mode
        # 本地模拟走 PaperEngine; IBKR模拟盘 / 实盘都走真实 IBKR 引擎 (真实发单)
        if mode.uses_ibkr_engine:
            self._active_engine = self.ibkr_engine
        else:
            self._active_engine = self.paper_engine

        self.symbol_bar.set_connected(True, mode)
        self.symbol_bar.set_engine(self.ibkr_engine)
        self.option_chain.set_engine(self._active_engine)
        self.price_ladder.set_engine(self._active_engine)
        self.position_panel.set_engine(self._active_engine)
        self.order_panel.set_engine(self._active_engine)
        self.account_bar.set_engine(self._active_engine)
        self.calculator.set_engine(self._active_engine)
        self.strategy_panel.set_engine(self._active_engine)
        # 交易统计跟随当前引擎 (真实引擎连接后会回放当日成交 → 自动推 today 统计)
        self.option_chain.set_trade_stats(self._active_engine.get_trade_stats())
        # 若当前正停在「多腿组合」Tab, 立即加载期权链
        if self.center_tabs.currentWidget() is self.strategy_panel:
            self.strategy_panel.ensure_loaded()

        self.statusBar().setStyleSheet("")
        self.statusBar().showMessage(f"已连接 ({mode.label})")

        # Request account data
        self._active_engine.request_account_summary()
        self.ibkr_engine.request_positions()
        self.account_bar.start()

        # 恢复本地条件单 (从磁盘) 并重新订阅各合约行情
        self.cond_manager.resume()
        # 恢复自选监控 (加载时自动清理过期合约) 并订阅行情
        self.watch_manager.resume()

        # 按当前交易品种加载 (期权链 / 正股伪合约 / 期货合约)
        self._load_current_instrument()

    def _on_disconnected(self):
        # 复位"切换中"状态, 避免切换失败后控件卡在禁用
        self.symbol_bar.set_switching(False)
        self.symbol_bar.set_connected(False)
        self.account_bar.stop()
        # 退订条件单行情 (保留条件单本身, 重连后 resume)
        self.cond_manager.suspend()
        self.watch_manager.suspend()
        self.statusBar().setStyleSheet(
            f"QStatusBar {{ color: {COLOR_RED}; }}"
        )
        self.statusBar().showMessage("已断开连接")

    def _on_fill_sound(self, order_id: int, side: str, qty: float, price: float):
        """真正成交回报 → 播放提示音 (后台线程, 不阻塞 GUI)。"""
        play_fill(side)

    def _on_trade_stats(self, snapshot: dict, source):
        """交易统计更新 → 仅显示当前活动引擎的 (模拟/实盘各自一套)。"""
        if source is self._active_engine:
            self.option_chain.set_trade_stats(snapshot)

    def _on_account_summary_end(self):
        """After first account summary, request PnL (needs account name)."""
        self._active_engine.request_pnl()

    # ── Symbol / Mode ─────────────────────────────────────────────────

    def _on_theme_changed(self, name: str):
        """顶栏「主题」切换: 保存选择 → 询问是否立即重启 (主题重启后生效)。

        206 处内联样式在各 widget 构建时就用 f-string 固化了颜色值,
        运行中无法整体重绘 —— 故采用保存 + 重启方案。"""
        if name == THEME_NAME:
            return
        try:
            save_theme_name(name)
        except Exception as e:
            QMessageBox.warning(self, "主题", f"保存主题设置失败: {e}")
            return
        resp = QMessageBox.question(
            self, "切换主题",
            "主题将在重启后生效。是否立即重启程序?\n"
            "(重启会断开当前连接; 挂单在 IBKR 服务器上不受影响)",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.Yes,
        )
        if resp == QMessageBox.Yes:
            self._restart_app()

    def _restart_app(self):
        """自重启: 先正常 close (保存窗口布局/断开连接), 再拉起新进程。

        新进程入口与当前一致 (main.py / main_gw.py); 其 kill_previous_instances
        只按脚本名杀旧进程, 此时旧进程已在退出中, 无竞态。"""
        import os
        import subprocess
        import sys
        script = os.path.abspath(sys.argv[0])
        self.close()
        try:
            subprocess.Popen(
                [sys.executable, script],
                cwd=os.path.dirname(script),
                close_fds=True,
            )
        except Exception as e:
            print(f"[THEME] 重启失败: {e}", flush=True)

    def _on_symbol_changed(self, symbol: str):
        self._current_symbol = symbol
        # 多腿组合面板跟随标的 (重置已加载状态; 若该 Tab 正显示则重载)
        self.strategy_panel.set_symbol(symbol)
        if self.ibkr_engine.is_connected:
            self._load_current_instrument()

    def _on_center_tab_changed(self, index: int):
        """切到「多腿组合」Tab → 懒加载期权链 (已连接且未加载时)。"""
        if (self.center_tabs.widget(index) is self.strategy_panel
                and self.ibkr_engine.is_connected):
            self.strategy_panel.set_engine(self._active_engine)
            self.strategy_panel.ensure_loaded()

    # ── Instrument type (期权/正股/期货) ────────────────────────────────

    def _on_instrument_changed(self, kind: str):
        """顶栏「类型」切换: 期权 / 正股 / 期货。"""
        if kind == self._instrument:
            return
        self._instrument = kind
        # 期权链仅在期权模式显示
        self.option_chain.setVisible(kind == "OPT")
        # 持仓筛选随品种切换
        self.position_panel.set_filter(
            {"OPT": "期权", "STK": "正股/ETF", "FUT": "期货"}[kind]
        )
        # 清空期货合约月份缓存 (重新解析)
        if kind != "FUT":
            self._future_expiries = []
        # 切到期货且当前标的不是期货根代码 → 默认填一个 (ES), 避免提示"不在列表"
        if kind == "FUT" and self._current_symbol.upper() not in FUTURES_SPECS:
            default_fut = "ES" if "ES" in FUTURES_SPECS else next(iter(FUTURES_SPECS))
            self._current_symbol = default_fut
            self.symbol_bar.set_symbol(default_fut)
        if self.ibkr_engine.is_connected:
            self._load_current_instrument()

    def _load_current_instrument(self):
        """按当前 _instrument 加载: 期权链 / 正股伪合约 / 期货合约到点价梯。"""
        sym = self._current_symbol
        if self._instrument == "OPT":
            self._load_option_chain(sym)
        elif self._instrument == "STK":
            self._load_stock_into_ladder(sym)
        elif self._instrument == "FUT":
            self._load_futures(sym)

    def _load_stock_into_ladder(self, symbol: str):
        """正股: 直接用伪合约 (right='STK') 载入点价梯 (复用 stock_trader 模式)。"""
        from config import INDEX_SYMBOLS
        if symbol in INDEX_SYMBOLS:
            self.statusBar().showMessage(f"{symbol} 是指数, 不能直接交易正股")
            return
        pseudo = OptionInfo(symbol=symbol, expiry="", strike=0.0, right="STK")
        self.price_ladder.set_option(pseudo)
        self.calculator.set_option(pseudo)
        self.symbol_bar.set_current_option(pseudo.display_name)
        self.statusBar().showMessage(f"正股: 已加载 {symbol}")

    def _load_futures(self, symbol: str):
        """期货: 后台解析近月起若干合约 → 填充月份下拉 → 默认载入近月。"""
        symbol = symbol.upper()
        if symbol not in FUTURES_SPECS:
            avail = ", ".join(FUTURES_SPECS.keys())
            self.statusBar().showMessage(
                f"{symbol} 不在内置期货列表; 可用: {avail}"
            )
            self.symbol_bar.populate_future_expiries([])
            return

        self.statusBar().showMessage(f"解析 {symbol} 期货合约...")

        def do_resolve():
            try:
                contracts = self.ibkr_engine.resolve_futures_contracts(
                    symbol, max_count=FUTURES_MAX_EXPIRIES
                )
                if not contracts:
                    self.ibkr_engine.bridge.error_received.emit(
                        -1, -1, f"{symbol} 无可用期货合约"
                    )
                    return
                # 跳回 GUI 线程填充下拉并载入近月
                self._futures_resolved.emit((symbol, contracts))
            except Exception as e:
                self.ibkr_engine.bridge.error_received.emit(-1, -1, f"期货解析失败: {e}")

        threading.Thread(target=do_resolve, daemon=True).start()

    def _apply_futures_contracts(self, payload):
        """在 GUI 线程: 填充期货月份下拉, 默认载入近月。payload=(symbol, contracts)。"""
        symbol, contracts = payload
        # 用户可能在解析期间又切走了品种/标的 — 丢弃过期结果
        if self._instrument != "FUT" or symbol != self._current_symbol.upper():
            return
        self._future_expiries = contracts
        items = []
        for i, c in enumerate(contracts):
            exp = c["expiry"]
            mon = exp[:6] if len(exp) >= 6 else exp
            mon_disp = f"{mon[:4]}-{mon[4:6]}" if len(mon) >= 6 else mon
            tag = " (近月)" if i == 0 else (" (季月)" if i == 1 else "")
            items.append((f"{mon_disp}{tag}", exp))
        self.symbol_bar.populate_future_expiries(items)
        # 默认载入近月
        self._load_future_contract(symbol, contracts[0]["expiry"])

    def _load_future_contract(self, symbol: str, expiry: str):
        """把指定期货合约月份载入点价梯。"""
        con_id = 0
        for c in self._future_expiries:
            if c["expiry"] == expiry:
                con_id = c.get("con_id", 0)
                break
        pseudo = OptionInfo(symbol=symbol, expiry=expiry, strike=0.0,
                            right="FUT", con_id=con_id)
        self.price_ladder.set_option(pseudo)
        self.calculator.set_option(pseudo)
        self.symbol_bar.set_current_option(pseudo.display_name)
        self.statusBar().showMessage(f"期货: 已加载 {pseudo.display_name}")

    def _on_future_expiry_changed(self, expiry: str):
        """用户切换期货合约月份下拉。"""
        if self._instrument == "FUT" and expiry:
            self._load_future_contract(self._current_symbol, expiry)

    def _on_mode_changed(self, mode_value: str):
        """Handle mode change before connection."""
        mode = TradingMode(mode_value)
        if self.ibkr_engine.is_connected:
            if mode.uses_ibkr_engine:
                self._active_engine = self.ibkr_engine
            else:
                self._active_engine = self.paper_engine
            self.option_chain.set_engine(self._active_engine)
            self.price_ladder.set_engine(self._active_engine)
            self.position_panel.set_engine(self._active_engine)
            self.order_panel.set_engine(self._active_engine)
            self.account_bar.set_engine(self._active_engine)
            self.calculator.set_engine(self._active_engine)
            self.strategy_panel.set_engine(self._active_engine)
            self.option_chain.set_trade_stats(self._active_engine.get_trade_stats())

    def _on_reconnect_requested(self, mode_value: str):
        """Handle hot switch: disconnect and reconnect to different port."""
        mode = TradingMode(mode_value)
        self.statusBar().showMessage(f"切换到 {mode.label} 模式...")
        self.symbol_bar.set_switching(True)
        self.account_bar.stop()

        def do_reconnect():
            success = self.ibkr_engine.reconnect(mode)
            if not success:
                self.ibkr_engine.bridge.error_received.emit(
                    -1, -1, f"切到 {mode.label} 失败 — 若 Gateway 刚才还能用现在连不上, "
                    "多半是 Gateway 卡死(JTS死锁)或旧连接未释放, 请重启 Gateway 后再连。"
                )
                self.ibkr_engine.bridge.disconnected.emit()

        threading.Thread(target=do_reconnect, daemon=True).start()

    # ── Option Chain Loading ──────────────────────────────────────────

    def _load_option_chain(self, symbol: str, jump_expiry: str = ""):
        """加载标的期权链; jump_expiry 非空时, 链加载完成后自动选中该到期日 Tab
        (双击期权持仓/监控/委托跳转用; 正股/期货不传)。"""
        self.statusBar().showMessage(f"加载 {symbol} 期权链...")

        def do_load():
            try:
                print(f"[DEBUG] Loading option chain for {symbol}...", flush=True)
                expirations, strikes = self.ibkr_engine.request_option_chain(symbol)
                print(f"[DEBUG] Got {len(expirations)} expirations, {len(strikes)} strikes", flush=True)

                if not expirations or not strikes:
                    self.ibkr_engine.bridge.error_received.emit(
                        -1, -1, f"{symbol} 期权链为空"
                    )
                    return

                # Get stock price via a one-shot tick subscription
                stock_price = self._fetch_stock_price(symbol)
                print(f"[DEBUG] Stock price for {symbol}: {stock_price}", flush=True)

                # Update UI on main thread via signal
                self.ibkr_engine.bridge.chain_ready.emit(expirations, strikes)
                # Store for the callback
                self._pending_chain = (symbol, expirations, strikes, stock_price)
            except Exception as e:
                print(f"[DEBUG] Option chain error: {e}", flush=True)
                self.ibkr_engine.bridge.error_received.emit(-1, -1, str(e))

        # Connect chain_ready to update (one-shot)
        try:
            self.ibkr_engine.bridge.chain_ready.disconnect()
        except TypeError:
            pass

        def on_chain_ready(expirations, strikes):
            data = getattr(self, '_pending_chain', None)
            if data:
                sym, exps, stks, price = data
                print(f"[DEBUG] on_chain_ready: {sym}, price={price}, "
                      f"{len(exps)} exp, {len(stks)} strikes", flush=True)
                self.option_chain.load_chain(sym, exps, stks, stock_price=price)
                if jump_expiry and sym == symbol:
                    self.option_chain.select_expiry(jump_expiry)
                self.statusBar().showMessage(
                    f"{sym} 期权链已加载: {len(exps)} 个到期日, "
                    f"{len(stks)} 个行权价 (股价=${price:.2f})"
                )
            try:
                self.ibkr_engine.bridge.chain_ready.disconnect(on_chain_ready)
            except TypeError:
                pass

        self.ibkr_engine.bridge.chain_ready.connect(on_chain_ready)
        threading.Thread(target=do_load, daemon=True).start()

    def _fetch_stock_price(self, symbol: str) -> float:
        """Subscribe to underlying stock price and return initial value.

        The subscription stays alive so the option chain title can display
        a continuously-updating price.  The tick data key is
        ``__stock__{symbol}`` inside ``app._tick_data``.
        """
        import time

        app = self.ibkr_engine._app
        key = f"__stock__{symbol}"

        # Cancel previous underlying subscription if any
        old_req = getattr(self, '_stock_price_req_id', None)
        if old_req is not None:
            try:
                app.cancelMktData(old_req)
            except Exception:
                pass
            app._active_mkt_data_reqs.discard(old_req)

        contract = IBKREngine._make_underlying_contract(symbol)
        req_id = app.next_req_id()
        self._stock_price_req_id = req_id
        self._stock_price_key = key
        app._tick_req_to_key[req_id] = key
        # 保留该标的上一次的报价, 不要清零 —— 来回切标的时清零会让下面那个循环
        # 重新空等最多 5 秒 (期权链要等它返回才渲染)。旧价几秒内足够当初值,
        # 新 tick 一到就覆盖。首次订阅才建空表。
        app._tick_data.setdefault(key, {"bid": 0.0, "ask": 0.0, "last": 0.0})
        app._active_mkt_data_reqs.add(req_id)
        # generic tick 106 = Option Implied Volatility → 标的 IV (tickGeneric tickType 24),
        # 显示在期权链标题价格右侧。指数(SPX 等)可能不下发, 缺则标题不显示 IV。
        app.reqMktData(req_id, contract, "106", False, False, [])

        # Wait up to 5 seconds for initial price
        for _ in range(50):
            time.sleep(0.1)
            d = app._tick_data.get(key, {})
            if d.get("last", 0) > 0 or d.get("bid", 0) > 0:
                break

        # Return initial price (subscription stays alive)
        d = app._tick_data.get(key, {})
        last = d.get("last", 0)
        bid = d.get("bid", 0)
        ask = d.get("ask", 0)
        if last > 0:
            return last
        if bid > 0 and ask > 0:
            return (bid + ask) / 2
        if bid > 0:
            return bid
        if ask > 0:
            return ask
        return 0.0

    # ── Option Selected ───────────────────────────────────────────────

    def _set_instrument_ui(self, kind: str):
        """同步「类型」下拉 + 期权链可见性 + 持仓筛选 (不触发重新加载)。"""
        if self._instrument != kind:
            self._instrument = kind
            self.symbol_bar.set_instrument(kind)
            self.option_chain.setVisible(kind == "OPT")
            self.position_panel.set_filter(
                {"OPT": "期权", "STK": "正股/ETF", "FUT": "期货"}[kind]
            )

    def _on_option_selected(self, option: OptionInfo):
        self._current_option = option
        right = getattr(option, "right", "")
        sym = getattr(option, "symbol", "")

        # 正股 / 期货合约 (多来自双击对应持仓): 切到该品种模式并直接载入点价梯,
        # 不加载期权链
        if right in ("STK", "FUT"):
            if sym:
                self._current_symbol = sym
                self.symbol_bar.set_symbol(sym)
            self._set_instrument_ui(right)
            self.price_ladder.set_option(option)
            self.calculator.set_option(option)
            self.symbol_bar.set_current_option(option.display_name)
            self.statusBar().showMessage(f"已选择: {option.display_name}")
            return

        # 期权 (C/P): 若属于另一个标的则切换标的并重载期权链 (并切回期权模式)
        opt_expiry = getattr(option, "expiry", "") if right in ("C", "P") else ""
        if sym and right != "COMBO" and (sym != self._current_symbol
                                         or self._instrument != "OPT"):
            self._current_symbol = sym
            self.symbol_bar.set_symbol(sym)
            self._set_instrument_ui("OPT")
            if self.ibkr_engine.is_connected:
                # 链加载完成后自动跳到该期权的到期日 Tab
                self._load_option_chain(sym, jump_expiry=opt_expiry)
        elif opt_expiry:
            # 同标的且期权链已在: 直接跳到该合约的到期日 Tab
            self.option_chain.select_expiry(opt_expiry)
        # 加载到点价梯 + 计算器 (点价交易界面就在左侧, 一直可见)
        self.price_ladder.set_option(option)
        self.calculator.set_option(option)
        self.symbol_bar.set_current_option(option.display_name)
        self.statusBar().showMessage(f"已选择: {option.display_name}")

    # ── Contract Search (from price ladder search bar) ────────────────

    def _on_contract_searched(self, option: OptionInfo):
        """Handle contract search — validate contract exists before loading."""
        if not self.ibkr_engine.is_connected:
            self.statusBar().showMessage("未连接 — 无法搜索合约")
            return

        self.statusBar().showMessage(f"验证合约: {option.display_name}...")
        self.price_ladder.contract_label.setText(f"验证中: {option.display_name}...")

        def do_validate():
            try:
                contract = IBKREngine._make_option_contract(
                    option.symbol, option.expiry, option.strike, option.right
                )
                app = self.ibkr_engine._app
                req_id = app.next_req_id()
                app._contract_data[req_id] = {
                    "details": [], "event": threading.Event(), "error": None,
                }
                app.reqContractDetails(req_id, contract)

                state = app._contract_data[req_id]
                if not state["event"].wait(timeout=5):
                    self.ibkr_engine.bridge.error_received.emit(
                        -1, -1, f"合约验证超时: {option.display_name}"
                    )
                    app._contract_data.pop(req_id, None)
                    return

                if state["error"] or not state["details"]:
                    self.ibkr_engine.bridge.error_received.emit(
                        -1, -1, f"合约不存在: {option.display_name}"
                    )
                    app._contract_data.pop(req_id, None)
                    return

                app._contract_data.pop(req_id, None)
                # Valid — notify GUI thread
                self._search_validated.emit(option)

            except Exception as e:
                self.ibkr_engine.bridge.error_received.emit(-1, -1, f"搜索错误: {e}")

        threading.Thread(target=do_validate, daemon=True).start()

    def _load_validated_contract(self, option: OptionInfo):
        """Load a validated searched contract into the price ladder."""
        self._current_option = option

        # Subscribe to tick data
        if self._active_engine and self.ibkr_engine.is_connected:
            key = option.to_ibkr_key()
            if not any(k == key for k in self.ibkr_engine._app._tick_req_to_key.values()):
                self._active_engine.subscribe_option_tick(option)

        self.price_ladder.set_option(option)
        self.calculator.set_option(option)
        self.symbol_bar.set_current_option(option.display_name)
        self.statusBar().showMessage(f"已加载: {option.display_name}")

    # ── Order Handling ────────────────────────────────────────────────

    def _unit_for(self, option: OptionInfo) -> str:
        """下单数量单位: 期货=手, 正股=股, 期权=张。"""
        return {"FUT": "手", "STK": "股"}.get(option.right, "张")

    def _place_order(self, option: OptionInfo, action: OrderAction,
                     order_type: OrderType, price: float, qty: int,
                     outside_rth: bool) -> int:
        """按品种路由下单: 期权 / 正股 / 期货, 返回 orderId。"""
        eng = self._active_engine
        if option.right in ("C", "P"):
            if order_type == OrderType.LIMIT:
                return eng.place_limit_order(option, action, qty, price,
                                             outside_rth=outside_rth)
            return eng.place_market_order(option, action, qty,
                                          outside_rth=outside_rth)
        if option.right == "STK":
            return eng.place_stock_order(option.symbol, action, qty, price,
                                         order_type=order_type,
                                         outside_rth=outside_rth)
        if option.right == "FUT":
            return eng.place_futures_order(option.symbol, option.expiry, action,
                                           qty, price, order_type=order_type,
                                           outside_rth=outside_rth)
        return -1

    def _confirm_sell_to_open(self, option: OptionInfo, qty: int) -> bool:
        """卖出数量超过实际持仓 (= 会开出空头) → **直接拦截**, 返回 True=放行。

        点价梯停在未持仓合约时误点「市价卖出」可能意外开出裸空腿。
        2026-07-10 起从「弹确认框可放行」改为**无条件拦截**
        (只弹提示框说明原因, 无放行选项)。
        (市价平仓按钮/条件单有各自的持仓校验; 多腿组合走 combo 下单不经此处,
        价差空腿属有意为之, 不受影响。)
        """
        eng = self._active_engine
        if not getattr(eng, "positions_synced", True):
            held = 0
            note = "\n(持仓快照尚未就绪, 按 0 持仓对待)"
        else:
            held = eng.get_position_qty(option.to_ibkr_key())
            note = ""
        if qty <= held:
            return True
        QMessageBox.warning(
            self, "已拦截: 不允许卖空",
            f"{option.display_name}\n当前持仓 {held}{self._unit_for(option)}, "
            f"卖出 {qty}{self._unit_for(option)} 会开出 "
            f"{qty - held}{self._unit_for(option)} 空头。{note}\n\n"
            f"本程序不允许卖空, 该卖单已拦截。\n"
            f"(想平仓请核对合约行权价/到期日; 价差空腿请用多腿组合下单)",
        )
        return False

    def _on_order_requested(self, option: OptionInfo, action_str: str, price: float):
        action = OrderAction.BUY if action_str == "BUY" else OrderAction.SELL

        # 买入是否需附带止盈/止损 (期货强制 / 期权勾「随买入单附带」)。None=拦截, {}=无需挂
        bracket = self._resolve_buy_bracket(option, action)
        if bracket is None:
            return

        qty = self.price_ladder.get_quantity()
        outside_rth = self.price_ladder.get_outside_rth()

        if action == OrderAction.SELL and not self._confirm_sell_to_open(option, qty):
            self.statusBar().showMessage("已拦截: 卖出数量超过持仓 (不允许卖空)")
            return

        order_id = self._place_order(option, action, OrderType.LIMIT, price, qty,
                                     outside_rth)
        if order_id > 0:
            self.price_ladder.reset_quantity()   # 每笔提交后数量复位 1
            action_text = "买入" if action == OrderAction.BUY else "卖出"
            rth_tag = " [盘外]" if outside_rth else ""
            msg = (f"已提交: {action_text} {qty}{self._unit_for(option)} "
                   f"{option.display_name} @ ${price:.2f}{rth_tag}")
            if action == OrderAction.SELL:
                msg += self._cancel_conds_on_manual_sell(option)
            # 限价开多: 等成交回报后再挂止盈/止损 (避免挂单未成交时误触发开出反向单)
            if bracket:
                self._pending_buy_brackets[order_id] = {
                    "option": option, "qty": qty,
                    "bracket": bracket, "outside": outside_rth,
                }
                msg += " — 成交后自动挂止盈/止损"
            self.statusBar().showMessage(msg)
            # Switch to order tab
            self.right_tabs.setCurrentIndex(1)

    def _on_market_order_requested(self, option: OptionInfo, action_str: str):
        """Handle market order from price ladder action buttons."""
        action = OrderAction.BUY if action_str == "BUY" else OrderAction.SELL

        # 买入是否需附带止盈/止损 (期货强制 / 期权勾「随买入单附带」)。None=拦截, {}=无需挂
        bracket = self._resolve_buy_bracket(option, action)
        if bracket is None:
            return

        qty = self.price_ladder.get_quantity()
        outside_rth = self.price_ladder.get_outside_rth()

        if action == OrderAction.SELL and not self._confirm_sell_to_open(option, qty):
            self.statusBar().showMessage("已拦截: 卖出数量超过持仓 (不允许卖空)")
            return

        order_id = self._place_order(option, action, OrderType.MARKET, 0.0, qty,
                                     outside_rth)
        if order_id > 0:
            self.price_ladder.reset_quantity()   # 每笔提交后数量复位 1
            action_text = "市价买入" if action == OrderAction.BUY else "市价卖出"
            rth_tag = " [盘外]" if outside_rth else ""
            sell_note = (self._cancel_conds_on_manual_sell(option)
                         if action == OrderAction.SELL else "")
            self.statusBar().showMessage(
                f"已提交: {action_text} {qty}{self._unit_for(option)} "
                f"{option.display_name}{rth_tag}{sell_note}"
            )
            # 市价开多附带条件单:
            #  - 绝对价(期权): 触发价已知, 成交即时 → 立刻挂;
            #  - 点数(期货): 需成交价作基准 → 等成交回报再挂 (_on_exec_arm_bracket)。
            if bracket:
                if bracket.get("by_points"):
                    self._pending_buy_brackets[order_id] = {
                        "option": option, "qty": qty,
                        "bracket": bracket, "outside": outside_rth,
                    }
                else:
                    self._arm_buy_bracket(option, qty, bracket, outside_rth)
            self.right_tabs.setCurrentIndex(1)

    # ── 买入附带止盈/止损 (bracket) ────────────────────────────────────

    @staticmethod
    def _trigger_price(by_points: bool, kind: str, value: float,
                       base: float) -> float:
        """把条件单输入换成**绝对触发价**。

        - ``by_points=False``: value 本身就是绝对触发价, 直接返回;
        - ``by_points=True`` (期货): value 是点数, 止盈=base+点, 止损=base−点
          (base = 买入成交价 / 持仓均价)。
        """
        if not by_points:
            return value
        return round(base + value, 4) if kind == "TP" else round(base - value, 4)

    def _resolve_buy_bracket(self, option: OptionInfo, action: OrderAction):
        """买入(BUY)时是否需附带止盈/止损条件单。

        - **期货开多**: 强制带 (`FUTURES_REQUIRE_BRACKET`), 止盈+止损都需设好;
        - **期权/正股**: 勾选点价梯「随买入单附带」才挂, 至少一条腿即可。

        返回值:
          - ``{}``  → 放行, 无需挂 (非 BUY / 既不强制也未勾附带);
          - ``dict``→ 放行, 且按此配置挂 ({tp_on,tp_price,sl_on,sl_price,native});
          - ``None``→ 已拦截 (调用方须中止下单)。
        """
        if action != OrderAction.BUY:
            return {}
        force = (option.right == "FUT" and FUTURES_REQUIRE_BRACKET)
        attach = self.price_ladder.attach_to_buy()
        if not force and not attach:
            return {}
        bracket = self.price_ladder.get_bracket(require_both=force)
        if bracket is None:
            self.price_ladder.open_cond_panel()
            if force:
                self.statusBar().showMessage("已拦截: 期货开仓必须先设置止盈 + 止损")
                QMessageBox.warning(
                    self, "期货需带止盈止损",
                    "期货开仓必须同时设置「止盈」和「止损」才能下单。\n\n"
                    "请在点价梯下方「条件单」面板勾选 ☑止盈 与 ☑止损 并填写有效触发价, "
                    "然后再点买入。",
                )
            else:
                self.statusBar().showMessage("已拦截: 勾了「随买入单附带」却未设置止盈/止损")
                QMessageBox.warning(
                    self, "需设置止盈/止损",
                    "已勾选「随买入单附带」, 但未设置有效的止盈/止损。\n\n"
                    "请在「条件单」面板勾选止盈或止损并填写触发价, "
                    "或取消「随买入单附带」后再下单。",
                )
            return None
        # 绝对价模式下, 两条腿都在时止盈价须高于止损价 (开多: 止盈在上、止损在下)。
        # 期货「点数」模式两者都是正偏移, 止盈触发价=base+点 永远 > 止损触发价=base−点, 无需此检查。
        if (not bracket.get("by_points") and bracket["tp_on"] and bracket["sl_on"]
                and bracket["tp_price"] <= bracket["sl_price"]):
            self.price_ladder.open_cond_panel()
            self.statusBar().showMessage("已拦截: 止盈价须高于止损价")
            QMessageBox.warning(
                self, "止盈/止损价不合理",
                f"开多: 止盈价 (${bracket['tp_price']:.2f}) 应高于 "
                f"止损价 (${bracket['sl_price']:.2f})。\n请检查触发价。",
            )
            return None
        return bracket

    def _arm_buy_bracket(self, option: OptionInfo, qty: int,
                         bracket: dict, outside: bool, base: float = 0.0):
        """给刚开的多仓挂上止盈/止损 (qty = 开仓数量, 可只挂其中一条腿)。

        `base` = 入场价基准, 期货「点数」模式用它把 +点/-点 换算成绝对触发价
        (止盈=base+点, 止损=base−点); 绝对价模式忽略 base。

        本地模式: 由 ConditionalOrderManager 监控现价, 到价发 SELL 限价单;
        原生模式: 止盈 = SELL LMT, 止损 = SELL STP LMT (服务器端)。
        """
        if not self.ibkr_engine.is_connected:
            self.statusBar().showMessage("未连接 — 无法挂止盈/止损")
            return
        by_points = bracket.get("by_points", False)
        if by_points and base <= 0:
            self.statusBar().showMessage("无入场价基准 — 无法按点数换算止盈/止损")
            return
        native = bracket.get("native", False)
        msgs = []
        placed_native = False
        if bracket.get("tp_on"):
            tp = self._trigger_price(by_points, "TP", bracket["tp_price"], base)
            off = f" (+{bracket['tp_price']:g}点)" if by_points else ""
            if tp > 0:
                if native:
                    oid = self._place_order(option, OrderAction.SELL,
                                            OrderType.LIMIT, tp, qty, outside)
                    if oid > 0:
                        msgs.append(f"止盈(原生限价@{tp:.2f}{off})")
                        placed_native = True
                else:
                    self.cond_manager.arm(option, "TP", tp, tp, qty, outside)
                    msgs.append(f"止盈(本地≥{tp:.2f}{off})")
        if bracket.get("sl_on"):
            sl = self._trigger_price(by_points, "SL", bracket["sl_price"], base)
            off = f" (-{bracket['sl_price']:g}点)" if by_points else ""
            if sl > 0:
                if native:
                    oid2 = self._active_engine.place_stop_limit_order(
                        option, OrderAction.SELL, qty, sl, sl, outside_rth=outside)
                    if oid2 > 0:
                        msgs.append(f"止损(原生STP LMT@{sl:.2f}{off})")
                        placed_native = True
                else:
                    self.cond_manager.arm(option, "SL", sl, sl, qty, outside)
                    msgs.append(f"止损(本地≤{sl:.2f}{off})")
        if msgs:
            tag = "" if native else " (本地: 仅程序运行时监控)"
            self.statusBar().showMessage(
                f"已挂(同{qty}{self._unit_for(option)}): {' + '.join(msgs)}{tag}"
            )
            if placed_native:
                self.right_tabs.setCurrentIndex(1)
        self._refresh_conditionals()

    def _on_exec_arm_bracket(self, order_id: int, side: str, qty: float,
                             price: float):
        """开多成交回报 → 挂上待挂的止盈/止损 (首次成交即挂, 然后移除)。
        成交价 `price` 作为期货「点数」换算的入场价基准。"""
        pending = self._pending_buy_brackets.pop(order_id, None)
        if pending is None:
            return
        self._arm_buy_bracket(
            pending["option"], pending["qty"],
            pending["bracket"], pending["outside"], base=price,
        )

    def _on_close_position_requested(self, option: OptionInfo):
        """Handle close position from price ladder (期权/正股/期货)。"""
        outside_rth = self.price_ladder.get_outside_rth()

        if option.right in ("C", "P"):
            order_id = self._active_engine.close_position(
                option, outside_rth=outside_rth
            )
        else:
            # 正股/期货: 用市价反向单平掉 reqPositions 报来的持仓数量
            qty = self._active_engine.get_position_qty(option.to_ibkr_key())
            if qty <= 0:
                self.statusBar().showMessage(f"无 {option.display_name} 多头持仓可平")
                return
            order_id = self._place_order(option, OrderAction.SELL,
                                         OrderType.MARKET, 0.0, qty, outside_rth)
        if order_id > 0:
            self.price_ladder.reset_quantity()   # 每笔提交后数量复位 1
            rth_tag = " [盘外]" if outside_rth else ""
            note = self._cancel_conds_on_manual_sell(option)
            self.statusBar().showMessage(
                f"已提交平仓: {option.display_name}{rth_tag}{note}")
            self.right_tabs.setCurrentIndex(1)

    def _on_close_all_positions(self, items: list):
        """一键平仓 —— 逐笔下市价单平掉列表里的持仓。

        确认弹窗已由持仓面板出过 (列出了每一笔), 到这里就是执行。
        多头卖出 / 空头买回按 qty 符号决定; 每笔独立下单, 单笔失败不影响其余,
        逐笔结果写日志, 最后在状态栏汇总成功/失败笔数。
        """
        if not items:
            return
        outside_rth = self.price_ladder.get_outside_rth()
        ok, failed = 0, []
        for it in items:
            option = it["option"]
            qty = int(it["qty"])
            action = OrderAction.SELL if qty > 0 else OrderAction.BUY
            try:
                order_id = self._place_order(option, action, OrderType.MARKET,
                                             0.0, abs(qty), outside_rth)
            except Exception as e:
                order_id = -1
                print(f"[CLOSE ALL] {it['name']} 下单异常: {e}", flush=True)
            if order_id > 0:
                ok += 1
                print(f"[CLOSE ALL] {it['name']} {action.value} {abs(qty)} "
                      f"→ orderId={order_id}", flush=True)
                # 该合约挂着的本地条件单 (止盈/止损) 随手动平仓一起作废
                self._cancel_conds_on_manual_sell(option)
            else:
                failed.append(it["name"])
                print(f"[CLOSE ALL] {it['name']} 下单失败", flush=True)

        rth_tag = " [盘外]" if outside_rth else ""
        msg = f"一键平仓: 已提交 {ok}/{len(items)} 笔市价单{rth_tag}"
        if failed:
            msg += f" — 失败: {', '.join(failed)}"
            self.statusBar().setStyleSheet(f"QStatusBar {{ color: {COLOR_RED}; }}")
        else:
            self.statusBar().setStyleSheet("")
        self.statusBar().showMessage(msg)
        self.right_tabs.setCurrentIndex(1)   # 跳到委托页看成交回报

    def _on_cancel_symbol_requested(self, option: OptionInfo):
        """点价梯撤单：只逐笔取消当前合约，绝不波及其他标的。"""
        count = self._active_engine.cancel_orders_for_option(option)
        if count:
            self.statusBar().showMessage(
                f"已请求取消 {option.display_name} 的 {count} 笔挂单；其他标的不受影响"
            )
        else:
            self.statusBar().showMessage(f"{option.display_name} 当前没有可撤挂单")

    def _on_cancel_all_orders_requested(self):
        """委托面板全局撤单：确认由 OrderPanel 完成。"""
        self._active_engine.cancel_all_orders()
        self.statusBar().showMessage("已请求取消全部标的的所有委托")

    def _on_cancel_order(self, order_id: int):
        self._active_engine.cancel_order(order_id)
        self.statusBar().showMessage(f"已请求撤单: #{order_id}")

    # ── 条件单 (止盈/止损) ─────────────────────────────────────────────

    def _on_conditional_requested(self, req: dict):
        """点价梯「挂条件单」(对**当前持仓**): 按勾选挂止盈/止损 (本地 或 IBKR 原生)。

        期货用「点数」表示, 以**当前持仓均价**为基准换算绝对触发价 (无持仓则拦截)。
        """
        opt = getattr(self.price_ladder, "_option", None)
        if opt is None:
            self.statusBar().showMessage("未选合约 — 无法挂条件单")
            return
        if not self.ibkr_engine.is_connected:
            self.statusBar().showMessage("未连接 — 无法挂条件单")
            return

        by_points = req.get("by_points", False)
        base = 0.0
        if by_points:
            pos = (self._active_engine.get_position(opt.to_ibkr_key())
                   if self._active_engine else None)
            base = getattr(pos, "avg_price", 0.0) if pos else 0.0
            if base <= 0:
                self.statusBar().showMessage(
                    "无持仓均价 — 期货按点数挂需先持仓 (或用「随买入单附带」在开仓时按成交价挂)"
                )
                QMessageBox.warning(
                    self, "需持仓均价",
                    "期货「条件单」用点数(相对持仓均价)表示, 但当前无持仓均价可用。\n\n"
                    "请先持有该期货仓位后再挂, 或勾「随买入单附带」在开仓时按成交价自动挂。",
                )
                return

        native = req["native"]
        qty = req["qty"]
        outside = req["outside_rth"]
        msgs = []
        if req["tp_on"]:
            tp = self._trigger_price(by_points, "TP", req["tp_price"], base)
            off = f" (+{req['tp_price']:g}点)" if by_points else ""
            if native:
                # 止盈 = 高于市价的卖出限价, 原生即普通 SELL LMT(到价成交)
                oid = self._place_order(opt, OrderAction.SELL, OrderType.LIMIT,
                                        tp, qty, outside)
                if oid > 0:
                    msgs.append(f"止盈(原生限价@{tp:.2f}{off})")
            else:
                self.cond_manager.arm(opt, "TP", tp, tp, qty, outside)
                msgs.append(f"止盈(本地≥{tp:.2f}{off})")
        if req["sl_on"]:
            sl = self._trigger_price(by_points, "SL", req["sl_price"], base)
            off = f" (-{req['sl_price']:g}点)" if by_points else ""
            if native:
                oid = self._active_engine.place_stop_limit_order(
                    opt, OrderAction.SELL, qty, sl, sl, outside_rth=outside)
                if oid > 0:
                    msgs.append(f"止损(原生STP LMT@{sl:.2f}{off})")
            else:
                self.cond_manager.arm(opt, "SL", sl, sl, qty, outside)
                msgs.append(f"止损(本地≤{sl:.2f}{off})")
        # 标的价触发 → 市价卖出 (本地监控标的价, 仅期权适用)。
        # 监控标的默认期权自己的标的, 可指定其它 (如 SPY 期权盯 SPX/ES)
        if req.get("ul_on") and opt.right in ("C", "P"):
            ul = round(req.get("ul_price", 0.0), 2)
            direction = req.get("ul_dir", "UP")
            watch_sym = (req.get("ul_sym") or "").upper()
            if watch_sym == opt.symbol.upper():
                watch_sym = ""   # 选了自己 = 默认
            arrow = "≥" if direction == "UP" else "≤"
            self.cond_manager.arm(opt, "UL", ul, 0.0, qty, outside,
                                  watch="UNDER", direction=direction, market=True,
                                  watch_symbol=watch_sym)
            msgs.append(f"{watch_sym or '标的'}{arrow}{ul:.2f}→市价卖{qty}")
        if msgs:
            tag = "" if native else " (本地: 仅程序运行时监控)"
            self.statusBar().showMessage("已挂条件单: " + " + ".join(msgs) + tag)
            if native:
                self.right_tabs.setCurrentIndex(1)
        self._refresh_conditionals()

    def _on_conditional_cancel(self, cond_id: int):
        self.cond_manager.cancel(cond_id)
        self.statusBar().showMessage(f"已取消本地条件单 #{cond_id}")

    def _cancel_conds_on_manual_sell(self, option: OptionInfo) -> str:
        """手动卖出后自动取消该合约的**全部本地条件单** (用户要求 2026-07-10)。

        卖出后不清, 残留的止盈/止损/标的价条件单会在之后重新买入同合约时,
        按旧触发价把新仓位卖掉。返回追加到状态栏的提示 ("" = 本无条件单)。
        条件单自身触发的卖出不走本方法 (只清手动卖出)。
        """
        key = option.to_ibkr_key()
        n = len(self.cond_manager.for_key(key))
        if n == 0:
            return ""
        self.cond_manager.cancel_for_key(key)
        self._refresh_conditionals()
        print(f"[COND] 手动卖出 {option.display_name} → 自动取消该合约 "
              f"{n} 条本地条件单", flush=True)
        return f"; 已自动取消该合约 {n} 条条件单"

    def _refresh_conditionals(self):
        """把当前点价梯合约的本地条件单推给点价梯显示。"""
        opt = getattr(self.price_ladder, "_option", None)
        conds = self.cond_manager.for_key(opt.to_ibkr_key()) if opt else []
        self.price_ladder.set_conditionals(conds)

    def _on_conditional_triggered(self, cond, order_id: int):
        play_fill("SLD")
        self.statusBar().showMessage(
            f"⚡ 条件单触发: {cond.kind_label} 卖出 {cond.quantity} "
            f"{cond.option.display_name} @ {cond.limit_price:.2f} → 已下单 #{order_id}"
        )
        self.right_tabs.setCurrentIndex(1)
        self._refresh_conditionals()

    def _on_conditional_failed(self, cond, msg: str):
        self.statusBar().setStyleSheet(f"QStatusBar {{ color: #ff9800; }}")
        self.statusBar().showMessage(f"条件单 {cond.kind_label} 触发但{msg}")

    def _on_conditional_voided(self, cond, reason: str):
        self.statusBar().showMessage(
            f"条件单 #{cond.cond_id} {cond.kind_label} "
            f"({cond.option.display_name}) 已自动作废: {reason}"
        )

    # ── 自选监控 (watch list) ─────────────────────────────────────────

    def _on_watch_add(self, option: OptionInfo):
        if not self.ibkr_engine.is_connected:
            self.statusBar().showMessage("未连接 — 无法加自选")
            return
        if self.watch_manager.add(option):
            self.statusBar().showMessage(
                f"已加入自选监控: {option.display_name} (「监控」页可设到价警报)")
            self.right_tabs.setCurrentWidget(self.watch_panel)
        else:
            self.statusBar().showMessage(f"{option.display_name} 已在自选列表中")

    def _on_watch_alerted(self, item, direction: str, price: float):
        sign = "≥ 高于" if direction == "above" else "≤ 低于"
        self.statusBar().showMessage(
            f"⚠ 到价警报: {item.option.display_name} 现价 {price:.2f} {sign}警报价")

    # ── Chart Window ─────────────────────────────────────────────────

    def _on_open_chart(self):
        """Open a K-line chart window for the current symbol (lazy import)."""
        if not self.ibkr_engine.is_connected:
            QMessageBox.warning(self, "未连接", "请先连接到 IBKR")
            return

        from widgets.chart_window import ChartWindow

        chart = ChartWindow(
            engine=self.ibkr_engine,
            symbol=self._current_symbol,
            parent=None,  # independent window
        )
        self._chart_windows.append(chart)
        chart.destroyed.connect(lambda: self._chart_windows.remove(chart)
                                if chart in self._chart_windows else None)
        chart.show_and_load()

    def _on_open_option_chart(self, option):
        """期权链双击某合约 → 打开该期权**今日 1 分钟图** (轻量独立窗口, 懒导入)。

        **单实例**: 同时只保留一个期权分时图 —— 双击新合约先关掉旧合约的窗口
        (close → closeEvent 停轮询定时器, WA_DeleteOnClose 销毁释放);
        双击**同一**合约则把已开的窗口带到前台, 不重开。"""
        if not self.ibkr_engine.is_connected:
            QMessageBox.warning(self, "未连接", "请先连接到 IBKR")
            return

        old = self._option_chart
        if old is not None:
            try:
                if old._option.to_ibkr_key() == option.to_ibkr_key():
                    old.raise_()
                    old.activateWindow()
                    return
                # 换合约**就地复用**这个窗口, 不再 close + new。原来每切一次都要销毁
                # 重建整个窗口 (两个 pyqtgraph PlotWidget + 全套 UI), 任务栏还会闪出
                # 一个新窗口 —— 就是那个"每次切换都弹出来的加载窗口"。
                old.set_option(option)
                old.raise_()
                old.activateWindow()
                return
            except RuntimeError:
                pass  # C++ 对象已销毁 (窗口早被用户关掉), 直接开新的
            self._option_chart = None

        from widgets.option_chart_window import OptionChartWindow

        chart = OptionChartWindow(
            engine=self.ibkr_engine,   # 行情/历史数据一律走真实引擎 (模拟模式亦然)
            option=option,
            parent=None,               # independent window
        )
        self._option_chart = chart
        self._chart_windows.append(chart)
        chart.destroyed.connect(lambda: self._on_option_chart_destroyed(chart))
        chart.show_and_load()

    def _on_option_chart_destroyed(self, chart):
        """期权分时图销毁 (关闭/被新合约替换) → 从跟踪列表与单实例引用中移除。"""
        if chart in self._chart_windows:
            self._chart_windows.remove(chart)
        if self._option_chart is chart:
            self._option_chart = None

    # ── Detachable Price Ladder ──────────────────────────────────────

    def _on_detach_ladder(self):
        """Pop out price ladder into a standalone window; replace its spot with a chart."""
        if self._ladder_detached:
            return

        self._ladder_detached = True
        self.price_ladder.detach_btn.setText("已弹出")
        self.price_ladder.detach_btn.setEnabled(False)

        # Remove price ladder from splitter (keep the widget alive)
        self.price_ladder.setParent(None)

        # Create standalone window for the price ladder
        self._ladder_window = QMainWindow(None)
        self._ladder_window.setWindowTitle("点价交易")
        # 弹出窗也要放得进屏幕 (旧的写死 600 高在矮屏上会超出可用区)
        _avail = self._available_rect()
        self._ladder_window.setMinimumSize(
            min(self._px(420), max(320, _avail.width() - 40)),
            min(self._px(600), max(400, _avail.height() - 60)),
        )
        self._ladder_window.resize(
            min(self._px(440), _avail.width() - self._px(20)),
            min(self._px(800), _avail.height() - self._px(20)),
        )
        self._ladder_window.setCentralWidget(self.price_ladder)
        self._ladder_window.setStyleSheet(DARK_STYLESHEET)
        self._ladder_window.installEventFilter(self)

        # Create an embedded chart in the vacated splitter spot
        if self.ibkr_engine.is_connected:
            from widgets.chart_window import ChartWindow
            self._embedded_chart = ChartWindow(
                engine=self.ibkr_engine,
                symbol=self._current_symbol,
                parent=None,
            )
            # Embed ChartWindow directly in splitter (QMainWindow is a QWidget)
            self._embedded_chart.setWindowFlags(Qt.Widget)
            self.bottom_splitter.insertWidget(0, self._embedded_chart)
            self._embedded_chart.show_and_load()
        else:
            # No connection — show placeholder
            placeholder = QLabel("连接 IBKR 后显示K线图")
            placeholder.setAlignment(Qt.AlignCenter)
            placeholder.setStyleSheet(
                f"color: {COLOR_TEXT}; font-size: 14px; background-color: {COLOR_BG_DARK};"
            )
            placeholder.setObjectName("chart_placeholder")
            self.bottom_splitter.insertWidget(0, placeholder)

        # 图表占左侧 (比点价梯宽一点), 按当前实际宽度取比例
        _tw = max(self.bottom_splitter.width(), self._px(700))
        _chart_w = max(int(_tw * 0.55), self._px(320))
        self.bottom_splitter.setSizes([_chart_w, max(_tw - _chart_w, self._px(280))])
        # insertWidget 会把新 index0 的 stretch 重置, 重新设回比例联动
        self.bottom_splitter.setStretchFactor(0, 4)
        self.bottom_splitter.setStretchFactor(1, 5)
        self._ladder_window.show()

    def _on_reattach_ladder(self):
        """Return the price ladder to its original splitter position and remove the chart."""
        if not self._ladder_detached:
            return

        # Clean up embedded chart
        if self._embedded_chart:
            self._embedded_chart.cleanup()
            self._embedded_chart.setParent(None)
            self._embedded_chart.deleteLater()
            self._embedded_chart = None
        else:
            # Remove placeholder if present
            for i in range(self.bottom_splitter.count()):
                w = self.bottom_splitter.widget(i)
                if w and w.objectName() == "chart_placeholder":
                    w.setParent(None)
                    w.deleteLater()
                    break

        # Reparent price ladder back into the splitter
        self.price_ladder.setParent(None)
        self.bottom_splitter.insertWidget(0, self.price_ladder)
        _tw = max(self.bottom_splitter.width(), self._px(700))
        _ladder_w = max(int(_tw * 4 / 9), self._px(380))
        _ladder_w = min(_ladder_w, max(_tw - self._px(280), self._px(380)))
        self.bottom_splitter.setSizes([_ladder_w, max(_tw - _ladder_w, self._px(280))])
        # 重新设回 stretch (insertWidget 重置了 index0 的 stretch factor)
        self.bottom_splitter.setStretchFactor(0, 4)
        self.bottom_splitter.setStretchFactor(1, 5)

        # Reset state
        self.price_ladder.detach_btn.setText("弹出")
        self.price_ladder.detach_btn.setEnabled(True)
        self._ladder_detached = False

        if self._ladder_window:
            self._ladder_window.removeEventFilter(self)
            self._ladder_window.deleteLater()
            self._ladder_window = None

    def eventFilter(self, obj, event):
        """Catch the ladder window being closed to trigger reattach."""
        if obj is self._ladder_window and event.type() == QEvent.Close:
            self._on_reattach_ladder()
            return True  # Consume the close event (we handle cleanup)
        return super().eventFilter(obj, event)

    # ── Session Indicator ────────────────────────────────────────────

    def _update_session_indicator(self):
        """Update the session status label based on current ET time."""
        try:
            import zoneinfo
            et = datetime.now(zoneinfo.ZoneInfo("America/New_York"))
        except Exception:
            # Fallback: assume local time is ET (close enough for display)
            et = datetime.now()

        h, m = et.hour, et.minute
        t = h * 60 + m  # minutes since midnight

        gth_start = SPX_SESSION_GTH_START[0] * 60 + SPX_SESSION_GTH_START[1]  # 20:15 = 1215
        gth_end = SPX_SESSION_GTH_END[0] * 60 + SPX_SESSION_GTH_END[1]        # 09:15 = 555
        rth_start = SPX_SESSION_RTH_START[0] * 60 + SPX_SESSION_RTH_START[1]  # 09:30 = 570
        rth_end = SPX_SESSION_RTH_END[0] * 60 + SPX_SESSION_RTH_END[1]        # 16:15 = 975

        weekday = et.weekday()  # 0=Mon ... 6=Sun
        is_weekend = weekday >= 5

        if is_weekend:
            session_text = "休市"
            session_color = COLOR_RED
        elif t >= rth_start and t < rth_end:
            session_text = "RTH 正常盘"
            session_color = COLOR_GREEN
        elif t >= gth_start or t < gth_end:
            session_text = "GTH 夜盘"
            session_color = COLOR_ACCENT
        elif t >= gth_end and t < rth_start:
            session_text = "盘前过渡"
            session_color = "#ff9800"
        elif t >= rth_end and t < gth_start:
            session_text = "盘后"
            session_color = "#ff9800"
        else:
            session_text = "休市"
            session_color = COLOR_RED

        time_str = et.strftime("%H:%M ET")
        self._session_label.setText(f"{session_text} {time_str}")
        self._session_label.setStyleSheet(
            f"color: {session_color}; background-color: {COLOR_BG_PANEL}; "
            f"border: 1px solid {COLOR_BORDER}; padding: 2px 10px; "
            f"border-radius: 3px; font-size: 12px; font-weight: bold;"
        )

    # ── Error Handling ────────────────────────────────────────────────

    def _on_error(self, req_id: int, code: int, msg: str):
        # Data connection errors — show specific warning in status bar
        if code in DATA_CONNECTION_ERROR_CODES:
            if code in (2104, 2106):
                # 连接正常/已恢复
                self.statusBar().showMessage("行情数据连接正常")
                self.statusBar().setStyleSheet("")
            elif code in (2107, 2108):
                # farm「inactive but should be available upon demand」=
                # 空闲待命 (取数据时自动重连), 是 IBKR 正常状态, 不是故障 → 不标红
                self.statusBar().showMessage(f"行情farm空闲 [{code}] (按需自动重连, 正常)")
                self.statusBar().setStyleSheet("")
            else:
                # 仅 2100 / 2103 / 2105 是真正的连接断开 → 标红告警
                self.statusBar().showMessage(f"⚠ 行情数据连接异常 [{code}]: {msg}")
                self.statusBar().setStyleSheet(
                    f"QStatusBar {{ color: {COLOR_RED}; }}"
                )
            return

        # Heartbeat tick timeout (code=-2, synthetic from heartbeat)
        if code == -2:
            self.statusBar().showMessage(f"⚠ {msg}")
            self.statusBar().setStyleSheet(
                f"QStatusBar {{ color: #ff9800; }}"  # orange warning
            )
            return

        # General errors — reset status bar style
        self.statusBar().setStyleSheet("")
        self.statusBar().showMessage(f"错误 [{code}]: {msg}")

        # Reset "验证中" state in price ladder on search error
        if "验证中" in self.price_ladder.contract_label.text():
            self.price_ladder.contract_label.setText("选择期权以开始")

    def _on_order_rejected(self, order_id: int, code: int, msg: str):
        """Order rejected/cancelled by IBKR — show the reason prominently."""
        order = self.ibkr_engine.orders.get(order_id)
        desc = ""
        if order:
            desc = (f"{order.display_action} {order.quantity}张 "
                    f"{order.option.display_name} @ ${order.limit_price:.2f}\n\n")

        self.statusBar().setStyleSheet(f"QStatusBar {{ color: {COLOR_RED}; }}")
        self.statusBar().showMessage(f"⚠ 订单 #{order_id} 被拒绝 [{code}]: {msg}")

        # Non-modal popup — doesn't block further trading clicks
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Warning)
        box.setWindowTitle(f"订单被拒绝 #{order_id}")
        box.setText(f"{desc}IBKR 拒绝原因 [{code}]:\n{msg}")
        box.setWindowModality(Qt.NonModal)
        box.setAttribute(Qt.WA_DeleteOnClose)
        box.show()

    # ── Cleanup ───────────────────────────────────────────────────────

    # ── Layout persistence ────────────────────────────────────────────

    def _layout_splitters(self):
        """(name, splitter) 对, 用于保存/恢复各分割位置。"""
        return (
            ("main", self.main_splitter),
            ("bottom", self.bottom_splitter),
            ("right", self.right_splitter),
        )

    # ── 屏幕自适应基础设施 ──────────────────────────────────────────

    def _px(self, value: int) -> int:
        """按屏幕 DPI 缩放一个基准像素值 (基准 = 96 DPI)。

        开了 AA_EnableHighDpiScaling 时 Qt 已按逻辑坐标工作, 这里通常返回原值;
        未开或整数倍缩放场景下仍能把「地板/手柄宽」按比例放大。
        """
        try:
            screen = self.screen() if hasattr(self, "screen") else None
            dpi = screen.logicalDotsPerInch() if screen else 96.0
        except Exception:
            dpi = 96.0
        scale = max(1.0, min(dpi / 96.0, 3.0))
        return int(round(value * scale))

    def _available_rect(self) -> QRect:
        """当前窗口所在屏幕的**可用**区域 (已扣掉任务栏)。"""
        try:
            screen = None
            if self.windowHandle() is not None:
                screen = self.windowHandle().screen()
            if screen is None:
                cursor_screen = QApplication.screenAt(self.pos()) if self.pos() else None
                screen = cursor_screen or QApplication.primaryScreen()
            if screen is not None:
                return screen.availableGeometry()
        except Exception:
            pass
        return QRect(0, 0, 1280, 800)

    def _fit_to_screen(self, save: bool = True):
        """把窗口缩放到当前屏幕可用区域内并居中。

        目标尺寸取「理想大小」与「屏幕可用区」的较小者 → 永远不会超出屏幕,
        也不会在大屏上被钉死在 1400x900。
        """
        avail = self._available_rect()
        w = min(1400, avail.width() - self._px(20))
        h = min(900, avail.height() - self._px(20))
        w = max(w, self.minimumWidth())
        h = max(h, self.minimumHeight())
        self.resize(w, h)
        # 居中到该屏幕的可用区
        x = avail.x() + max(0, (avail.width() - w) // 2)
        y = avail.y() + max(0, (avail.height() - h) // 2)
        self.move(x, y)
        if save:
            self._apply_default_splitter_sizes()

    def _apply_default_splitter_sizes(self):
        """按当前实际高度/宽度**按比例**重排三个 splitter。

        取代原来写死的 setSizes([400,400]) / [380,500] / [520,300] ——
        写死的绝对像素在小屏 (或高 DPI 缩放后的小逻辑分辨率) 上会让
        期权链只剩一行。
        """
        # 主竖向: 期权链 : 下方 = 3 : 2, 但期权链至少留够看 ~8 行
        total_h = max(self.main_splitter.height(), self._px(400))
        chain_h = max(int(total_h * 0.55), self._px(240))
        chain_h = min(chain_h, total_h - self._px(220))  # 给下方留活路
        chain_h = max(chain_h, self._px(150))
        self.main_splitter.setSizes([chain_h, max(total_h - chain_h, self._px(150))])

        # 下方横向: 点价梯 : 右侧面板 ≈ 4 : 5, 点价梯至少 380 (表头固定宽度之和)
        total_w = max(self.bottom_splitter.width(), self._px(700))
        ladder_w = max(int(total_w * 4 / 9), self._px(380))
        ladder_w = min(ladder_w, max(total_w - self._px(280), self._px(380)))
        self.bottom_splitter.setSizes([ladder_w, max(total_w - ladder_w, self._px(280))])

        # 右侧竖向: 持仓/委托 Tab : 计算器 ≈ 5 : 3 (计算器已可滚动, 挤一点没关系)
        total_rh = max(self.right_splitter.height(), self._px(400))
        tabs_h = max(int(total_rh * 0.6), self._px(200))
        tabs_h = min(tabs_h, max(total_rh - self._px(150), self._px(200)))
        self.right_splitter.setSizes([tabs_h, max(total_rh - tabs_h, self._px(90))])

    def _splitter_state_is_sane(self, name: str, splitter: QSplitter, sizes) -> bool:
        """恢复出来的分割位置是否还「能看」。

        存坏的状态 (某一格被压到只剩一行) 会被 restoreState 永久复现 ——
        用户拖窗口也救不回来, 因为 restoreState 覆盖了 setSizes 默认值。
        这里对明显不合理的状态直接丢弃, 回落到按比例的默认布局。
        """
        if not sizes or len(sizes) < 2:
            return False
        if any(s <= 0 for s in sizes):   # 有面板被完全折叠
            return False
        floors = {
            "main": (self._px(150), self._px(150)),
            "bottom": (self._px(300), self._px(240)),
            "right": (self._px(140), self._px(70)),
        }
        floor = floors.get(name)
        if floor is None:
            return True
        return all(s >= f for s, f in zip(sizes, floor))

    def _restore_layout(self):
        """恢复上次会话的窗口几何与各 splitter 分割位置 (首次运行则用默认值)。

        恢复前先做两道体检, 任一不过就回落到「适应屏幕 + 比例布局」:
          1. 几何是否还落在当前某块屏幕内 (换了小屏/改了缩放 → 旧几何作废);
          2. 各分割位置是否把某个模块压到了不可用的高度。
        """
        geo = self._settings.value("geometry")
        restored_geo = False
        if geo is not None:
            restored_geo = self.restoreGeometry(geo)
        if restored_geo and not self._geometry_fits_screen():
            restored_geo = False
        if not restored_geo:
            self._fit_to_screen(save=False)

        # splitter 尺寸依赖于窗口的最终大小 → 等布局跑完再校验/落位
        QTimer.singleShot(0, self._restore_splitters)

    def _geometry_fits_screen(self) -> bool:
        """恢复出来的窗口是否仍然大体落在某块屏幕的可用区内。"""
        if self.isMaximized() or self.isFullScreen():
            return True
        frame = self.frameGeometry()
        for screen in QApplication.screens():
            avail = screen.availableGeometry()
            if not avail.intersects(frame):
                continue
            # 标题栏要够得着 (能拖动), 且窗口不能明显大于该屏
            visible = avail.intersected(frame)
            title_visible = visible.height() >= self._px(28) and visible.width() >= self._px(120)
            fits = (frame.width() <= avail.width() + self._px(8)
                    and frame.height() <= avail.height() + self._px(8))
            if title_visible and fits:
                return True
        return False

    def _restore_splitters(self):
        """校验并应用记忆的分割位置; 不合理则用按比例的默认布局。"""
        self._apply_default_splitter_sizes()   # 先铺一个合理底板
        for name, splitter in self._layout_splitters():
            state = self._settings.value(f"splitter/{name}")
            if state is None:
                continue
            before = splitter.sizes()
            if not splitter.restoreState(state):
                continue
            if not self._splitter_state_is_sane(name, splitter, splitter.sizes()):
                splitter.setSizes(before)   # 状态存坏了 → 退回默认比例
                self._settings.remove(f"splitter/{name}")

    def _save_layout(self):
        """保存当前窗口几何与各 splitter 分割位置。

        只保存**通过体检**的分割位置 —— 否则一次意外的压扁会被永久记住。
        """
        self._settings.setValue("geometry", self.saveGeometry())
        for name, splitter in self._layout_splitters():
            if self._splitter_state_is_sane(name, splitter, splitter.sizes()):
                self._settings.setValue(f"splitter/{name}", splitter.saveState())
            else:
                self._settings.remove(f"splitter/{name}")

    # ── 布局菜单动作 ────────────────────────────────────────────────

    def _on_fit_to_screen(self):
        """窗口缩放到当前屏幕 + 各模块按比例重排。"""
        if self.isMaximized() or self.isFullScreen():
            self.showNormal()
        self._fit_to_screen(save=False)
        self._apply_default_splitter_sizes()
        self.statusBar().showMessage("布局已适应当前屏幕", 3000)

    def _on_reset_layout(self):
        """丢弃记忆的窗口大小/分割位置, 恢复出厂默认。"""
        for name, _ in self._layout_splitters():
            self._settings.remove(f"splitter/{name}")
        self._settings.remove("geometry")
        self._settings.sync()
        if self.isMaximized() or self.isFullScreen():
            self.showNormal()
        self._fit_to_screen(save=False)
        self._apply_default_splitter_sizes()
        self.statusBar().showMessage("布局已重置为默认 (记忆的分割位置已清除)", 4000)

    def _on_maximize_chain(self):
        """把纵向空间尽量让给期权链 (下方压到各自地板)。"""
        total = max(self.main_splitter.height(), self._px(400))
        bottom = self._px(220)
        self.main_splitter.setSizes([max(total - bottom, self._px(150)), bottom])
        self.statusBar().showMessage("期权链已最大化 (Ctrl+0 恢复比例布局)", 3000)

    def closeEvent(self, event):
        # Stop session timer
        self._session_timer.stop()

        # Reattach ladder if detached (cleans up embedded chart too)
        if self._ladder_detached:
            self._on_reattach_ladder()

        # Persist window size + splitter positions (ladder is reattached by now)
        self._save_layout()

        # Close all chart windows
        for chart in list(self._chart_windows):
            chart.cleanup()
            chart.close()
        self._chart_windows.clear()

        # 多腿组合面板 (退订各腿行情 + 停止刷新定时器)
        self.strategy_panel.cleanup()

        self.cond_manager.cleanup()
        self.watch_manager.cleanup()
        self.watch_panel.cleanup()
        self.account_bar.cleanup()
        self.option_chain.cleanup()
        self.price_ladder.cleanup()
        self.position_panel.cleanup()
        self.order_panel.cleanup()
        self.calculator.cleanup()
        self.ibkr_engine.disconnect()
        event.accept()
