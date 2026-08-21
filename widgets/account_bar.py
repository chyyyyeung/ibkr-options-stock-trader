"""Account summary bar — displays portfolio value, cash, buying power, P&L."""

import math
from datetime import datetime

from PyQt5.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QFrame,
)
from PyQt5.QtCore import QTimer

from config import (
    COLOR_BG_DARK, COLOR_BG_PANEL, COLOR_TEXT, COLOR_TEXT_DIM,
    COLOR_GREEN, COLOR_RED, COLOR_ACCENT, COLOR_BORDER,
    ACCOUNT_REFRESH_MS, ACCOUNT_RESYNC_MS,
)


class AccountBar(QWidget):
    """Horizontal bar showing account summary and a live US-Eastern clock."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._engine = None

        # Account data
        self._net_liquidation = 0.0
        self._total_cash = 0.0
        self._buying_power = 0.0
        self._unrealized_pnl = 0.0
        self._realized_pnl = 0.0
        self._daily_pnl = 0.0
        self._account_name = ""
        # reqPnL 流是否已给出未实现盈亏。一旦给出, 账户摘要里(每3秒重订、常推0/陈旧值)
        # 的 UnrealizedPnL 就不再覆盖显示 —— 避免未实现盈亏闪 0。
        self._unrealized_from_stream = False
        # 持仓面板汇总出来的账户级未实现盈亏 (USD)。None = 还没汇总过。
        # **优先级高于 reqPnL 流**: 部分账户的 reqPnL 会推 unrealizedPnL=0.0
        # (伴随 error 2150, 见 position_panel.on_pnl_single), 是"看着有效其实是假"
        # 的值, 会把真实浮盈亏盖成 0.00, 并让总资产外推冻住。
        self._portfolio_unrealized = None
        self._realized_seen = False     # reqPnL 是否给过有效 realizedPnL
        # 今日盈亏改用引擎自算值 (今日成交现金流 + 持仓市值 − 手续费), 不依赖 IBKR dailyPnL
        # (其常为 -- / 未含费)。computed_daily_pnl 信号送来 (已扣费总额, 今日手续费)。
        self._daily_computed = None     # 自算今日盈亏 (已扣费)
        self._today_commission = 0.0    # 今日累计手续费 (仅用于标注)
        # reqPnL 流是否给出过**有效的 dailyPnL**。给出过就不再用 已实现+未实现 兜底:
        # 两者口径不同 (dailyPnL 较昨收; 兜底把隔夜仓的历史浮亏也算进"今日"),
        # dailyPnL 偶尔推 DBL_MAX(NaN) 时若切去兜底, 显示会在两个数之间跳。
        self._daily_from_stream = False

        # ── 总资产实时外推 ────────────────────────────────────────────
        # IBKR 的 NetLiquidation 只在值变化或约每 3 分钟才推一次, 直接显示就是
        # "半天不动"。而 reqPnL 是随行情逐笔推的 —— 净值的日内变化**就等于**今日
        # 盈亏的变化 (存取款除外)。于是: 显示值 = 最近一次真值快照 + 这之后的盈亏增量,
        # 每来一笔 PnL 就重算一次, 每次 IBKR 推来真值再自动校准归零误差。
        self._nl_base = 0.0            # 最近一次 IBKR 真值 NetLiquidation
        self._nl_base_pnl = None       # 取到该真值时的今日盈亏 (外推基准)
        self._nl_pnl_source = ""       # 基准所用口径: "stream"(dailyPnL) / "fallback"
        self._nl_synced_at = ""        # 上次真值校准时刻 (显示在 tooltip 里)

        self._build_ui()

        # Periodic refresh to re-request account summary
        self._refresh_timer = QTimer()
        self._refresh_timer.timeout.connect(self._refresh)
        self._refresh_timer.setInterval(ACCOUNT_REFRESH_MS)

        # 定期强制重订账户摘要, 把 IBKR 的 ~3 分钟推送节奏压到 ACCOUNT_RESYNC_MS。
        # 受益的主要是**不能外推**的可用资金/购买力 (总资产已由 PnL 流实时外推)。
        self._resync_timer = QTimer()
        self._resync_timer.timeout.connect(self._resync)
        self._resync_timer.setInterval(ACCOUNT_RESYNC_MS)

        # 美东时间实时时钟 (每秒刷新, 独立于连接, 始终运行)
        self._clock_timer = QTimer()
        self._clock_timer.timeout.connect(self._update_clock)
        self._clock_timer.start(1000)
        self._update_clock()

    def _build_ui(self):
        # 两行布局: 窄屏下单行排不下会截断, 故把账户名与时钟下移到第二行,
        # 第一行只放资金摘要。币种余额条已移除 (按需可经 on_currency_balance 重新接回)。
        self.setFixedHeight(56)
        self.setStyleSheet(f"""
            QWidget {{
                background-color: {COLOR_BG_DARK};
                border-bottom: 1px solid {COLOR_BORDER};
            }}
        """)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(12, 2, 12, 2)
        outer.setSpacing(2)

        # ── 第一行: 资金摘要 (总资产 / 可用 / 购买力 / 未实现 / 今日盈亏 / 手续费) ──
        row1 = QHBoxLayout()
        row1.setSpacing(20)

        # Net liquidation
        self.net_liq_label = QLabel("总资产: --")
        self.net_liq_label.setStyleSheet(f"color: {COLOR_TEXT}; font-size: 12px; font-weight: bold; border: none;")
        row1.addWidget(self.net_liq_label)

        row1.addWidget(self._make_sep())

        # Total cash
        self.cash_label = QLabel("可用资金: --")
        self.cash_label.setStyleSheet(f"color: {COLOR_TEXT}; font-size: 12px; border: none;")
        row1.addWidget(self.cash_label)

        row1.addWidget(self._make_sep())

        # Buying power
        self.bp_label = QLabel("购买力: --")
        self.bp_label.setStyleSheet(f"color: {COLOR_TEXT}; font-size: 12px; border: none;")
        row1.addWidget(self.bp_label)

        row1.addWidget(self._make_sep())

        # Unrealized P&L
        self.unrealized_label = QLabel("未实现盈亏: --")
        self.unrealized_label.setStyleSheet(f"color: {COLOR_TEXT_DIM}; font-size: 12px; border: none;")
        row1.addWidget(self.unrealized_label)

        row1.addWidget(self._make_sep())

        # Daily P&L
        self.daily_pnl_label = QLabel("今日盈亏: --")
        self.daily_pnl_label.setStyleSheet(f"color: {COLOR_TEXT_DIM}; font-size: 12px; border: none;")
        self.daily_pnl_label.setToolTip(
            "IBKR dailyPnL: 较昨日收盘的今日盈亏 (已含手续费)。\n"
            "含隔夜仓今日的价格变动, 但不含其今日之前的浮盈亏 —\n"
            "所以可能 ≠ 已实现+未实现 (后者按开仓成本算)。"
        )
        row1.addWidget(self.daily_pnl_label)

        row1.addWidget(self._make_sep())

        # 今日总手续费 — 来自 computed_daily_pnl 信号的手续费分量
        # (真实模式: IBKR commissionReport 按 execId 去重、日内累计、跨日清零;
        #  模拟模式: 各笔成交估算佣金累计)。
        self.comm_label = QLabel("今日手续费: --")
        self.comm_label.setStyleSheet(
            f"color: {COLOR_TEXT_DIM}; font-size: 12px; border: none;"
        )
        self.comm_label.setToolTip("今日累计手续费 (round-trip 双边均计)")
        row1.addWidget(self.comm_label)

        row1.addStretch()
        outer.addLayout(row1)

        # ── 第二行: 账户名 (左) + 美东时钟 (右) ──
        row2 = QHBoxLayout()
        row2.setSpacing(20)

        # Account label
        self.account_label = QLabel("账户: --")
        self.account_label.setStyleSheet(f"color: {COLOR_TEXT_DIM}; font-size: 12px; border: none;")
        row2.addWidget(self.account_label)

        row2.addStretch()

        # 美东时间实时时钟 (替代原「换汇」按钮)
        self.clock_label = QLabel("🕐 --:--:--")
        self.clock_label.setStyleSheet(
            f"color: {COLOR_ACCENT}; font-size: 13px; font-weight: bold; "
            f"border: none; font-family: 'Consolas', 'Menlo', monospace;"
        )
        self.clock_label.setToolTip("美东时间 (America/New_York)")
        row2.addWidget(self.clock_label)

        outer.addLayout(row2)

    def _make_sep(self) -> QFrame:
        sep = QFrame()
        sep.setFrameShape(QFrame.VLine)
        sep.setStyleSheet(f"color: {COLOR_BORDER}; border: none; background-color: {COLOR_BORDER};")
        sep.setFixedWidth(1)
        sep.setFixedHeight(20)
        return sep

    def set_engine(self, engine):
        self._engine = engine

    def start(self):
        """Start periodic refresh."""
        self._refresh_timer.start()
        self._resync_timer.start()

    def stop(self):
        """Stop periodic refresh."""
        self._refresh_timer.stop()
        self._resync_timer.stop()
        # 换账户/断开 → 外推基准作废, 否则新账户会拿旧净值当基准
        self._nl_base = 0.0
        self._nl_base_pnl = None
        self._nl_pnl_source = ""
        self._nl_synced_at = ""
        # 让重连后账户摘要的未实现盈亏可再次作初始回退, 直到新的 reqPnL 流接管。
        self._unrealized_from_stream = False
        self._daily_from_stream = False
        self._portfolio_unrealized = None
        self._realized_seen = False
        # 重连会重新拉取当日成交/手续费重算, 先清零避免叠加旧会话的值。
        self._today_commission = 0.0
        self._daily_computed = None
        # 断开/切换账户时把盈亏与净值显示清回 "--",避免实盘↔模拟切换时残留上一个账户的数字
        # (今日盈亏/净值/现金等按各自账户独立显示)。
        for lab, txt in (
            (self.daily_pnl_label, "今日盈亏: --"),
            (self.unrealized_label, "未实现盈亏: --"),
            (self.net_liq_label, "总资产: --"),
            (self.cash_label, "可用资金: --"),
            (self.bp_label, "购买力: --"),
            (self.comm_label, "今日手续费: --"),
        ):
            lab.setText(txt)
            lab.setStyleSheet(f"color: {COLOR_TEXT_DIM}; font-size: 12px; border: none;")

    def on_currency_balance(self, currency: str, cash: float):
        """各币种现金余额槽 — 币种显示条已从账户栏移除, 这里保留空槽以免改动
        主窗口的信号接线 (currency_balance_updated → 此槽)。数据仍在引擎侧流动,
        将来要恢复显示时在此重新渲染即可。"""
        return

    def update_account(self, tag: str, value: str, currency: str, account: str):
        """Handle account_summary_updated signal."""
        self._account_name = account
        self.account_label.setText(f"账户: {account}")
        self.account_label.setStyleSheet(f"color: {COLOR_TEXT}; font-size: 12px; border: none;")

        try:
            val = float(value)
        except (ValueError, TypeError):
            return

        # 账户摘要按**账户基础货币**推送,统一折成 USD 再显示。
        # 汇率没到就显示原币,避免给非 USD 数值误加美元符号。
        usd = self._to_usd(val, currency)

        if tag == "NetLiquidation":
            if usd is None:
                self.net_liq_label.setText(f"总资产: {self._fmt_native(val, currency)}")
                self.net_liq_label.setStyleSheet(
                    f"color: {COLOR_TEXT}; font-size: 12px; font-weight: bold; border: none;"
                )
                self.net_liq_label.setToolTip("等待账户汇率 (ExchangeRate) 以折算美元…")
                return
            # IBKR 真值到达 → 重设外推基准 (把这段时间累积的外推误差清零)。
            # 基准必须是 **USD** —— 外推加的是 USD 口径的今日盈亏, 混单位会算歪。
            self._net_liquidation = usd
            self._nl_base = usd
            self._nl_base_pnl = None   # 下一笔 PnL 会把它设成当前值
            self._nl_synced_at = datetime.now().strftime("%H:%M:%S")
            self._render_net_liq(usd, live=False)
        elif tag == "TotalCashValue":
            self._total_cash = usd if usd is not None else val
            self.cash_label.setText(
                f"可用资金: {f'${usd:,.2f}' if usd is not None else self._fmt_native(val, currency)}"
            )
        elif tag == "BuyingPower":
            self._buying_power = usd if usd is not None else val
            self.bp_label.setText(
                f"购买力: {f'${usd:,.2f}' if usd is not None else self._fmt_native(val, currency)}"
            )
        elif tag == "UnrealizedPnL":
            # 仅作初始回退: reqPnL 流一旦接管就不再用账户摘要的值 (它每3秒重订、
            # 常推 0 或陈旧值, 会把好值闪没)。
            if not self._unrealized_from_stream:
                self._unrealized_pnl = val
                color = COLOR_GREEN if val >= 0 else COLOR_RED
                sign = "+" if val >= 0 else ""
                self.unrealized_label.setText(f"未实现盈亏: {sign}${val:,.2f}")
                self.unrealized_label.setStyleSheet(
                    f"color: {color}; font-size: 12px; font-weight: bold; border: none;"
                )
        elif tag == "RealizedPnL":
            self._realized_pnl = val

    def on_computed_daily(self, total: float, commission: float):
        """今日盈亏部分已弃用 (自算在成交/持仓与账户不匹配时会严重出错, 曾误显示巨额盈利,
        今日盈亏改回直接用 IBKR 的 dailyPnL, 由 update_daily_pnl 驱动)。
        但**手续费分量仍可信** (真实模式来自 IBKR commissionReport 按 execId 去重、日内
        累计、跨日清零), 用它驱动右上角「今日手续费」显示。"""
        try:
            comm = float(commission)
        except (ValueError, TypeError):
            return
        if math.isnan(comm) or abs(comm) > 1e300:
            return
        self._today_commission = comm
        self.comm_label.setText(f"今日手续费: ${comm:,.2f}")
        self.comm_label.setStyleSheet(
            f"color: {COLOR_TEXT}; font-size: 12px; font-weight: bold; border: none;"
        )

    def update_daily_pnl(self, daily: float, unrealized: float, realized: float):
        """Handle pnl_updated signal. 今日盈亏 = IBKR reqPnL 的 dailyPnL (较昨收, 含费);
        dailyPnL 不可用(DBL_MAX→NaN)且**本会话从未有效过**时才用 已实现+未实现 兜底
        (IBKR 的 realizedPnL 已含手续费)。一旦 dailyPnL 有效过, NaN 时保留上一次
        dailyPnL 好值、不再切兜底 —— 兜底口径不同 (它把隔夜仓在今日之前的浮亏
        也算进"今日"), 两口径混用会让今日盈亏在两个相差很大的数之间跳。"""
        if not math.isnan(realized):
            # reqPnL 的 realizedPnL 是兜底口径的另一半 (部分账户摘要不推
            # RealizedPnL 这个 tag), 记下来供 on_portfolio_unrealized 用。
            self._realized_pnl = realized
            self._realized_seen = True
        eff_daily = daily
        source = "stream"
        if not math.isnan(daily):
            self._daily_from_stream = True
        elif not self._daily_from_stream and not math.isnan(realized):
            # 兜底口径的浮盈亏分量优先取持仓汇总: 部分账户的 reqPnL unrealized 会恒为
            # 0.0, 用它兜底会让 eff_daily 只在成交时才变 → 总资产外推形同冻结, 表现
            # 为"总资产半天不动"。持仓汇总跟着 reqPnLSingle 的实时市值走, 每秒都在变。
            if self._portfolio_unrealized is not None:
                unrl = self._portfolio_unrealized
            else:
                unrl = 0.0 if math.isnan(unrealized) else unrealized
            eff_daily = realized + unrl
            source = "fallback"
        # 用同一口径的今日盈亏增量把总资产外推到当下 (见 _recompute_live_net_liq)
        self._recompute_live_net_liq(eff_daily, source)
        if not math.isnan(eff_daily):
            self._daily_pnl = eff_daily
            color = COLOR_GREEN if eff_daily >= 0 else COLOR_RED
            sign = "+" if eff_daily >= 0 else ""
            self.daily_pnl_label.setText(f"今日盈亏: {sign}${eff_daily:,.2f}")
            self.daily_pnl_label.setStyleSheet(
                f"color: {color}; font-size: 12px; font-weight: bold; border: none;"
            )

        # Also update unrealized from PnL stream (权威来源, 接管后账户摘要不再覆盖)。
        # 但持仓汇总 (portfolio) 优先级更高 —— reqPnL 可能推 unrealizedPnL=0.0
        # 这种"看着有效其实是假"的值, 会把真实浮盈亏盖成 0.00。
        if self._portfolio_unrealized is not None:
            pass
        elif not math.isnan(unrealized):
            self._unrealized_from_stream = True
            self._unrealized_pnl = unrealized
            u_color = COLOR_GREEN if unrealized >= 0 else COLOR_RED
            u_sign = "+" if unrealized >= 0 else ""
            self.unrealized_label.setText(f"未实现盈亏: {u_sign}${unrealized:,.2f}")
            self.unrealized_label.setStyleSheet(
                f"color: {u_color}; font-size: 12px; font-weight: bold; border: none;"
            )

    # 汇率没到时的原币显示 (别顶着 $ 显示欧元数)
    _CCY_SYMBOL = {"USD": "$", "EUR": "€", "GBP": "£", "JPY": "¥", "HKD": "HK$"}

    @classmethod
    def _fmt_native(cls, val: float, currency: str) -> str:
        sym = cls._CCY_SYMBOL.get(currency or "")
        return f"{sym}{val:,.2f}" if sym else f"{val:,.2f} {currency or '?'}"

    def _to_usd(self, val: float, currency: str):
        """把账户摘要的金额折成 USD; 汇率未知返回 None (由调用方显示原币)。"""
        if not currency or currency == "USD":
            return val
        rate = 0.0
        if self._engine is not None:
            fn = getattr(self._engine, "to_usd_rate", None)
            if fn:
                try:
                    rate = fn(currency)
                except Exception:
                    rate = 0.0
        return val * rate if rate else None

    def on_portfolio_unrealized(self, total: float):
        """持仓面板汇总出来的账户级未实现盈亏 (USD, 全部 API 持仓, 不受面板筛选影响)。

        当 IBKR 的 reqPnL 推 unrealizedPnL=0.0、reqPnLSingle 推 DBL_MAX
        (伴随 error 2150 "Invalid position trade derived value") 时,这是可靠的浮盈亏来源。
        详见 position_panel.on_pnl_single。持仓面板改成用「市值 − 成本」
        自算后, 这里就有了跟着行情逐秒变的浮盈亏, 顺带把总资产外推也救活。
        """
        if math.isnan(total) or abs(total) > 1e300:
            return
        self._portfolio_unrealized = total
        self._unrealized_pnl = total
        color = COLOR_GREEN if total >= 0 else COLOR_RED
        sign = "+" if total >= 0 else ""
        self.unrealized_label.setText(f"未实现盈亏: {sign}${total:,.2f}")
        self.unrealized_label.setStyleSheet(
            f"color: {color}; font-size: 12px; font-weight: bold; border: none;"
        )
        # dailyPnL 有效时以它为准 (口径更正), 否则用 已实现+未实现 兜底并把总资产
        # 外推到当下 —— 这条路径是"总资产实时跳动"的实际驱动力。
        if self._daily_from_stream or not self._realized_seen:
            return
        eff_daily = self._realized_pnl + total
        self._daily_pnl = eff_daily
        d_color = COLOR_GREEN if eff_daily >= 0 else COLOR_RED
        d_sign = "+" if eff_daily >= 0 else ""
        self.daily_pnl_label.setText(f"今日盈亏: {d_sign}${eff_daily:,.2f}")
        self.daily_pnl_label.setStyleSheet(
            f"color: {d_color}; font-size: 12px; font-weight: bold; border: none;"
        )
        self._recompute_live_net_liq(eff_daily, "fallback")

    def _render_net_liq(self, val: float, live: bool):
        """画总资产。live=True 表示这是两次 IBKR 真值之间的实时外推值。"""
        self.net_liq_label.setText(f"总资产: ${val:,.2f}")
        self.net_liq_label.setStyleSheet(
            f"color: {COLOR_TEXT}; font-size: 12px; font-weight: bold; border: none;"
        )
        if live:
            self.net_liq_label.setToolTip(
                f"实时值 = IBKR 最近一次净值快照 + 此后的今日盈亏变动。\n"
                f"IBKR 的净值本身只在值变化或约每 3 分钟推一次, 中间用盈亏流外推,\n"
                f"每次真值到达自动校准 (上次校准 {self._nl_synced_at})。"
            )
        else:
            self.net_liq_label.setToolTip(
                f"IBKR 账户摘要真值 (校准于 {self._nl_synced_at})"
            )

    def _recompute_live_net_liq(self, eff_daily: float, source: str):
        """用今日盈亏的增量把总资产外推到当下。

        净值的日内变化恒等于今日盈亏的变化 (存取款除外), 而 reqPnL 是随行情逐笔
        推送的 —— 所以 `真值快照 + (当前盈亏 − 快照时盈亏)` 就是一个跟着行情跳动的
        净值, 每次 IBKR 推来真值 NetLiquidation 时重设基准, 误差不会累积。

        dailyPnL 与"已实现+未实现"兜底两个口径的**绝对值**不同 (兜底把隔夜仓的历史
        浮亏也算进今日), 但只要基准和当前取自同一口径, **增量**都是对的; 口径中途
        切换时重设基准, 避免净值跳一大格。
        """
        if self._nl_base <= 0 or math.isnan(eff_daily):
            return
        if self._nl_base_pnl is None or source != self._nl_pnl_source:
            # 刚校准过 / 口径变了 → 以当前为新基准, 本次不外推 (不产生跳变)。
            # 口径切换时把**已经外推到的净值**固化成新基准, 否则这段增量会被丢掉,
            # 下一笔 PnL 会让总资产往回跳一格。
            if source != self._nl_pnl_source and self._net_liquidation > 0:
                self._nl_base = self._net_liquidation
            self._nl_base_pnl = eff_daily
            self._nl_pnl_source = source
            return
        live = self._nl_base + (eff_daily - self._nl_base_pnl)
        self._net_liquidation = live
        self._render_net_liq(live, live=True)

    def _refresh(self):
        """保活: 账户摘要/币种余额的订阅 (幂等, 已订阅时是空操作)。"""
        if self._engine:
            self._engine.request_account_summary()
            if hasattr(self._engine, "request_currency_balances"):
                self._engine.request_currency_balances()

    def _resync(self):
        """强制 IBKR 立刻回一份账户摘要快照 —— 主要为了让**可用资金/购买力**
        跟上 (这两个没法像总资产那样用盈亏流外推)。"""
        resync = getattr(self._engine, "resync_account_summary", None) if self._engine else None
        if resync:
            resync()

    def _update_clock(self):
        """刷新美东时间显示 (每秒)。无 tz 数据时回退本地时间。"""
        try:
            import zoneinfo
            et = datetime.now(zoneinfo.ZoneInfo("America/New_York"))
            tz = et.tzname() or "ET"
        except Exception:
            et = datetime.now()
            tz = "本地"
        self.clock_label.setText(f"🕐 美东 {et:%m-%d %H:%M:%S} {tz}")

    def cleanup(self):
        self._refresh_timer.stop()
        self._clock_timer.stop()
