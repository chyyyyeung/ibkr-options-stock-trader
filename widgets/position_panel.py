"""Position panel — displays current holdings with P/L.

Supports option positions (from engine) and IBKR portfolio positions
(stocks/ETFs via portfolio_position_received signal).
"""

import math

from PyQt5.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QTableWidget, QTableWidgetItem,
    QHeaderView, QAbstractItemView, QLabel, QComboBox, QPushButton, QMessageBox,
)
from PyQt5.QtCore import pyqtSignal, Qt, QTimer
from PyQt5.QtGui import QColor, QBrush

from config import COLOR_GREEN, COLOR_RED, COLOR_TEXT, COLOR_TEXT_DIM, COLOR_ACCENT, COLOR_BG_DARK, COLOR_BORDER
from models import PositionInfo, PortfolioPosition, OptionInfo


class PositionPanel(QWidget):
    """Displays current positions with real-time P/L."""

    position_clicked = pyqtSignal(object)  # OptionInfo — double-click to open ladder
    # 一键平仓: list[dict] — 每项 {"option": OptionInfo, "qty": 有符号数量, "name": str}
    # qty > 0 = 多头 (市价卖出平掉); qty < 0 = 空头 (市价买回平掉)。
    close_all_requested = pyqtSignal(list)
    # 账户级未实现盈亏 (全部 API 持仓汇总, USD, **不受面板筛选影响**) → 账户栏。
    # 部分账户的 reqPnL 会推 unrealizedPnL=0.0 (假值, 见 on_pnl_single 注释),
    # 账户栏的「未实现盈亏」和总资产外推都改用这里的汇总值。
    portfolio_unrealized_changed = pyqtSignal(float)

    def __init__(self, parent=None, default_filter: str = "期权"):
        super().__init__(parent)
        self._engine = None

        # IBKR portfolio positions (stocks, ETFs, options from reqPositions)
        self._portfolio_positions: dict[str, PortfolioPosition] = {}

        # Default filter: main app shows options only (user preference);
        # the stock trader client passes "正股/ETF"
        self._current_filter = default_filter

        # Cached brushes — avoid allocating a QBrush/QColor per cell per second
        self._brush_cache: dict[str, QBrush] = {}

        self._build_ui()

        self._refresh_timer = QTimer()
        self._refresh_timer.timeout.connect(self._refresh)
        self._refresh_timer.start(1000)

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        # Top bar: title + filter
        top_layout = QHBoxLayout()

        self.title = QLabel("持仓")
        self.title.setStyleSheet("font-size: 13px; font-weight: bold; padding: 4px;")
        top_layout.addWidget(self.title)

        top_layout.addStretch()

        # Filter combo
        self.filter_combo = QComboBox()
        self.filter_combo.addItems(["全部", "期权", "正股/ETF", "期货"])
        self.filter_combo.setCurrentText(self._current_filter)
        self.filter_combo.setFixedWidth(100)
        self.filter_combo.currentTextChanged.connect(self._on_filter_changed)
        self.filter_combo.setStyleSheet(f"""
            QComboBox {{
                background-color: {COLOR_BG_DARK};
                color: {COLOR_TEXT};
                border: 1px solid {COLOR_BORDER};
                padding: 2px 6px;
                border-radius: 3px;
                font-size: 11px;
            }}
        """)
        top_layout.addWidget(self.filter_combo)

        # 一键平仓 — 期权与正股各一个 (两类的平仓路由不同, 也避免误把另一类一起清掉)
        self.close_all_opt_btn = self._make_close_all_btn(
            "一键平仓期权", "市价平掉当前全部期权持仓 (多头卖出 / 空头买回)"
        )
        self.close_all_opt_btn.clicked.connect(lambda: self._on_close_all("OPT"))
        top_layout.addWidget(self.close_all_opt_btn)

        self.close_all_stk_btn = self._make_close_all_btn(
            "一键平仓正股", "市价平掉当前全部正股/ETF持仓 (多头卖出 / 空头买回)"
        )
        self.close_all_stk_btn.clicked.connect(lambda: self._on_close_all("STK"))
        top_layout.addWidget(self.close_all_stk_btn)

        layout.addLayout(top_layout)

        headers = ["类型", "合约", "数量", "均价", "市价", "市值", "今日盈亏", "盈亏(含费)", "盈亏%"]
        self.table = QTableWidget(0, len(headers))
        self.table.setHorizontalHeaderLabels(headers)
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)

        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.Stretch)
        for i in range(2, len(headers)):
            header.setSectionResizeMode(i, QHeaderView.ResizeToContents)

        self.table.doubleClicked.connect(self._on_double_click)
        layout.addWidget(self.table)

        # Summary
        self.summary_label = QLabel("总盈亏: $0.00")
        self.summary_label.setStyleSheet(f"color: {COLOR_TEXT_DIM}; padding: 4px;")
        layout.addWidget(self.summary_label)

    def _make_close_all_btn(self, text: str, tip: str) -> QPushButton:
        btn = QPushButton(text)
        btn.setCursor(Qt.PointingHandCursor)
        btn.setToolTip(tip)
        btn.setStyleSheet(f"""
            QPushButton {{
                background-color: {COLOR_BG_DARK};
                color: {COLOR_RED};
                border: 1px solid {COLOR_RED};
                padding: 2px 8px;
                border-radius: 3px;
                font-size: 11px;
            }}
            QPushButton:hover {{ background-color: {COLOR_RED}; color: #ffffff; }}
        """)
        return btn

    def set_engine(self, engine):
        self._engine = engine

    # ── 一键平仓 ──────────────────────────────────────────────────────

    @staticmethod
    def _pp_to_option(pp: PortfolioPosition) -> OptionInfo | None:
        """把 IBKR 持仓 (reqPositions) 转成下单用的 OptionInfo; 不支持的品种返回 None。"""
        if pp.sec_type == "OPT":
            return OptionInfo(symbol=pp.symbol, expiry=pp.expiry,
                              strike=pp.strike, right=pp.right, con_id=pp.con_id)
        if pp.sec_type in ("STK", "ETF"):
            return OptionInfo(symbol=pp.symbol, expiry="", strike=0.0,
                              right="STK", con_id=pp.con_id)
        if pp.sec_type == "FUT":
            return OptionInfo(symbol=pp.symbol, expiry=pp.expiry, strike=0.0,
                              right="FUT", con_id=pp.con_id)
        return None

    def _collect_closable(self, kind: str) -> list[dict]:
        """收集某一类的全部可平持仓。kind: "OPT" = 期权, "STK" = 正股/ETF。

        两个来源合并去重 (与表格 _refresh 同口径): 引擎本地跟踪的持仓 +
        IBKR reqPositions 报来的持仓 (含上一会话留下的、本地没跟踪的仓)。
        数量保留**符号**: 正=多头(卖出平), 负=空头(买回平); 碎股取整后为 0 的跳过
        (市价单数量必须是整数, 分红再投产生的 <1 股无法用普通市价单平掉)。
        """
        items: list[dict] = []
        seen: set[str] = set()

        if self._engine is not None:
            for key, pos in self._engine.positions.items():
                if kind != "OPT" or pos.option.right not in ("C", "P"):
                    continue
                qty = int(pos.quantity)
                if qty == 0:
                    continue
                items.append({"option": pos.option, "qty": qty,
                              "name": pos.option.display_name})
                seen.add(key)

        want = ("OPT",) if kind == "OPT" else ("STK", "ETF")
        for key, pp in self._portfolio_positions.items():
            if key in seen or pp.sec_type not in want:
                continue
            opt = self._pp_to_option(pp)
            if opt is None:
                continue
            qty = int(pp.quantity)
            if qty == 0:
                continue  # 碎股 (<1 股) — 市价单下不出去, 跳过
            items.append({"option": opt, "qty": qty, "name": pp.display_name})
            seen.add(key)

        return items

    def _on_close_all(self, kind: str):
        """一键平仓: 弹窗列出将被平掉的持仓, 确认后交由主窗口逐笔下市价单。"""
        label = "期权" if kind == "OPT" else "正股/ETF"
        if self._engine is None:
            QMessageBox.information(self, "一键平仓", "尚未连接, 无法平仓。")
            return

        items = self._collect_closable(kind)
        if not items:
            QMessageBox.information(self, "一键平仓", f"当前没有{label}持仓。")
            return

        lines = []
        for it in items:
            qty = it["qty"]
            act = "市价卖出" if qty > 0 else "市价买回"
            lines.append(f"  • {it['name']}    {act} {abs(qty)}")
        detail = "\n".join(lines)

        box = QMessageBox(self)
        box.setIcon(QMessageBox.Warning)
        box.setWindowTitle(f"⚠ 一键平仓{label}")
        # 纯文本渲染: QMessageBox 会对含 HTML 标签的文本走富文本, 这里全是普通字符,
        # 显式设成 PlainText 以免合约名里的符号被当成标记。
        box.setTextFormat(Qt.PlainText)
        box.setText(
            f"即将以「市价」平掉全部 {len(items)} 笔{label}持仓:\n\n"
            f"{detail}\n\n"
            "市价单会立即成交, 成交价不受控制; 流动性差的合约可能滑点很大。\n"
            "确认继续?"
        )
        box.setStandardButtons(QMessageBox.Yes | QMessageBox.No)
        box.setDefaultButton(QMessageBox.No)   # 默认「否」, 避免回车误触
        box.button(QMessageBox.Yes).setText("确认平仓")
        box.button(QMessageBox.No).setText("取消")
        if box.exec_() != QMessageBox.Yes:
            return

        self.close_all_requested.emit(items)

    def on_portfolio_position(self, pos: PortfolioPosition):
        """Handle portfolio_position_received signal from IBKR."""
        key = pos.position_key
        if abs(pos.quantity) > 0:
            # Keep PnL data already received for this position
            old = self._portfolio_positions.get(key)
            if old is not None and old.has_pnl_data:
                pos.daily_pnl = old.daily_pnl
                pos.unrealized_pnl = old.unrealized_pnl
                pos.market_price = old.market_price
                pos.market_value = old.market_value
                pos.has_pnl_data = True
                pos.has_daily_data = old.has_daily_data
                pos.pnl_is_derived = old.pnl_is_derived
            self._portfolio_positions[key] = pos
            self._subscribe_pnl_single(pos)
        else:
            if self._engine and hasattr(self._engine, "cancel_pnl_single"):
                self._engine.cancel_pnl_single(pos.con_id)
            self._portfolio_positions.pop(key, None)

    def _subscribe_pnl_single(self, pos: PortfolioPosition):
        """Subscribe per-position PnL (daily + unrealized + market value).
        Returns silently if engine not ready (retried from _refresh)."""
        if self._engine and hasattr(self._engine, "request_pnl_single"):
            self._engine.request_pnl_single(pos.con_id)

    def on_pnl_single(self, con_id: int, qty: float, daily_pnl: float,
                      unrealized_pnl: float, value: float):
        """Handle reqPnLSingle update — fill in market data for the position.
        NaN 字段表示 IBKR 本次未提供该值 (原 DBL_MAX), 跳过以保留上一次的好值,
        避免未实现盈亏 / 今日盈亏偶尔闪烁成 0。"""
        for pp in self._portfolio_positions.values():
            if pp.con_id == con_id:
                got_any = False
                if not math.isnan(daily_pnl):
                    pp.daily_pnl = daily_pnl
                    pp.has_daily_data = True
                    got_any = True
                if not math.isnan(unrealized_pnl):
                    pp.unrealized_pnl = unrealized_pnl
                    pp.pnl_is_derived = False
                    got_any = True
                if not math.isnan(value):
                    # 市值有效即算「数据已到」。以前 got_any 只认 daily/unrealized,
                    # 而 IBKR 对部分账户这两项会长期推 DBL_MAX (伴随 error 2150
                    # "Invalid position trade derived value", 已被 IGNORED_ERROR_CODES
                    # 静默吞掉) → has_pnl_data 永远 False → 持仓面板的
                    # 市价/市值/今日盈亏/盈亏/P L% 五列全部锁死在 "--"。
                    got_any = True
                    # reqPnLSingle 的 value/unrealized 可能按标的**当地货币**计
                    # (如台股为 TWD)。market_value / market_price 均存**本币原值**,
                    # 折算 USD 交给显示层 (× 账户 ledger 汇率)。
                    pp.market_value = value
                    # IBKR 没给浮动盈亏时自算: 浮盈亏 ≡ 市值 − 成本, 成本 =
                    # 均价 × 数量 × 乘数 (reqPositions 的 avgCost 已含手续费, 与
                    # IBKR 自己的口径一致)。
                    # 空头 qty<0 时 value/cost 同为负, 相减依然正确。
                    if math.isnan(unrealized_pnl):
                        cost = pp.avg_price * pp.quantity * pp.multiplier
                        if cost:
                            pp.unrealized_pnl = value - cost
                            pp.pnl_is_derived = True
                    if pp.quantity and pp.multiplier:
                        if pp.currency == "USD":
                            pp.market_price = abs(
                                value / (pp.quantity * pp.multiplier)
                            )
                        else:
                            # 由本币市值反推本币现价 (比值抵消数量/乘数, 结果精确):
                            #   mkt_local = avg_local × 市值 / 成本
                            #             = avg_local × value / (value − unrealized)
                            cost_local = value - pp.unrealized_pnl
                            if abs(cost_local) > 1e-9 and pp.avg_price > 0:
                                pp.market_price = abs(
                                    pp.avg_price * value / cost_local
                                )
                if got_any:
                    pp.has_pnl_data = True
                break

    def on_portfolio_positions_end(self):
        """Called when position snapshot is complete."""
        pass  # Positions already accumulated via on_portfolio_position

    def _on_filter_changed(self, text: str):
        self._current_filter = text

    def set_filter(self, text: str):
        """外部 (切换交易品种时) 设置持仓筛选, 同步下拉显示。"""
        if text not in ("全部", "期权", "正股/ETF", "期货"):
            return
        self._current_filter = text
        self.filter_combo.blockSignals(True)
        self.filter_combo.setCurrentText(text)
        self.filter_combo.blockSignals(False)

    def _refresh(self):
        if not self._engine:
            return

        # Merge engine positions (options tracked locally) + IBKR portfolio positions
        rows_data = []
        seen_keys = set()

        # Engine option positions (Paper or Live tracked positions)
        for key, pos in self._engine.positions.items():
            # Update current price from tick data
            tick = self._engine.get_tick(key)
            last = tick.get("last", 0)
            bid = tick.get("bid", 0)
            ask = tick.get("ask", 0)
            if last > 0:
                pos.current_price = last
            elif bid > 0 and ask > 0:
                pos.current_price = (bid + ask) / 2
            elif bid > 0:
                pos.current_price = bid

            row = {
                "type": "期权",
                "sec_type": "OPT",
                "name": pos.option.display_name,
                "qty": pos.quantity,
                "avg_price": pos.avg_price,
                "current_price": pos.current_price,
                "market_value": pos.market_value,
                # 本地撮合的都是美股期权, 本币即 USD → 汇率 1.0
                "currency": "USD",
                "fx_rate": 1.0,
                "pnl": pos.net_pnl,
                "pnl_pct": pos.net_pnl_pct,
                "commission": pos.total_commission,
                "commission_ccy": "USD",
                "commission_fx": 1.0,
                "daily": None,  # engine-tracked options: no daily PnL feed
                "has_pnl": True,  # 本地撮合持仓盈亏即时可算
                "option": pos.option,
                "key": key,
            }
            rows_data.append(row)
            seen_keys.add(key)

        # IBKR portfolio positions (stocks, ETFs, possibly options)
        for key, pp in self._portfolio_positions.items():
            if key in seen_keys:
                continue  # Avoid double-counting options

            # Retry PnL subscription (account name may arrive after positions)
            if not pp.has_pnl_data:
                self._subscribe_pnl_single(pp)

            # 本币 → USD 汇率 (账户 ledger 的 ExchangeRate)。reqPnLSingle 的市值/盈亏
            # 可能按**当地货币**计, 需乘此汇率才是 USD。未到则 0.0 → 显示层暂显本币。
            pp.fx_rate = (
                self._engine.get_fx_rate(pp.currency)
                if hasattr(self._engine, "get_fx_rate")
                else (1.0 if pp.currency == "USD" else 0.0)
            )

            comm = (
                self._engine.get_position_commission(key)
                if hasattr(self._engine, "get_position_commission") else 0
            )
            comm_ccy = (
                self._engine.get_position_commission_currency(key)
                if hasattr(self._engine, "get_position_commission_currency") else ""
            )
            # 佣金按其自身计价货币折算 (通常与持仓同币)
            comm_fx = (
                self._engine.get_fx_rate(comm_ccy)
                if (comm_ccy and hasattr(self._engine, "get_fx_rate")) else 1.0
            )

            row = {
                "type": pp.instrument_type,
                "sec_type": pp.sec_type,
                "name": pp.display_name,
                # 不能 int() 取整: 正股碎股 (分红再投等产生的 <1 股) 会显示成
                # 数量 0 但市值仍在; 保留原始小数, 渲染时整数才去掉小数位
                "qty": pp.quantity,
                # 一律存**本币**原值 + 汇率, 由显示层统一折算成 USD (汇率未到则显本币)
                "avg_price": pp.avg_price,          # 本币 (如 TWD)
                "current_price": pp.market_price,   # 本币 (合成)
                "market_value": pp.market_value,    # 本币
                "currency": pp.currency,
                "fx_rate": pp.fx_rate,              # 本币 → USD
                "pnl": pp.unrealized_pnl,           # 本币
                "pnl_pct": pp.pnl_pct,              # 无量纲, 与币种无关
                "commission": comm,                 # 本币 (comm_ccy)
                "commission_ccy": comm_ccy or pp.currency,
                "commission_fx": comm_fx,
                # 今日盈亏单独用 has_daily_data 把门 —— IBKR 的 dailyPnL 常年无效,
                # 跟着 has_pnl_data 走会把它显示成误导性的 $0.00 而非 "--"
                "daily": pp.daily_pnl if pp.has_daily_data else None,  # 本币
                "has_pnl": pp.has_pnl_data,  # API 持仓: reqPnLSingle 到达前显示"--"而非 0
                "option": None,
                "key": key,
            }
            rows_data.append(row)

        # Apply filter
        if self._current_filter == "期权":
            rows_data = [r for r in rows_data if r["sec_type"] == "OPT"]
        elif self._current_filter == "正股/ETF":
            rows_data = [r for r in rows_data if r["sec_type"] in ("STK", "ETF")]
        elif self._current_filter == "期货":
            rows_data = [r for r in rows_data if r["sec_type"] == "FUT"]

        self.table.setRowCount(len(rows_data))
        total_pnl = 0.0

        for row_idx, data in enumerate(rows_data):
            # Type
            self._set_cell(row_idx, 0, data["type"], COLOR_ACCENT)

            # Contract name
            self._set_cell(row_idx, 1, data["name"], COLOR_TEXT)

            # Quantity (整数正常显示; 碎股保留小数, 如 0.6656)
            qty = data["qty"]
            qty_text = (str(int(qty)) if float(qty).is_integer()
                        else f"{qty:.4f}".rstrip("0").rstrip("."))
            self._set_cell(row_idx, 2, qty_text, COLOR_TEXT)

            # 币种与汇率: 一律折算成 USD 显示 (整行统一美元, 免得 均价 TWD 而市值
            # 冠 $、前后对不上)。汇率来自账户 ledger; 非美元且汇率未到时退显本币。
            cur_code = data.get("currency", "USD")
            fx = data.get("fx_rate", 1.0 if cur_code == "USD" else 0.0)
            to_usd = (cur_code == "USD") or (fx and fx > 0)

            # 均价 — 折算 USD; 本币原价与汇率放 tooltip 备查
            avg_text = self._fmt_money(data["avg_price"], cur_code, fx)
            if cur_code != "USD" and to_usd:
                avg_tip = (f"本币成本: {data['avg_price']:,.2f} {cur_code}"
                           f"\n汇率: 1 {cur_code} ≈ ${fx:.4f}")
            elif cur_code != "USD":
                avg_tip = "等待账户汇率 (ExchangeRate) 以折算美元…"
            else:
                avg_tip = ""
            self._set_cell(row_idx, 3, avg_text, COLOR_TEXT, tooltip=avg_tip)

            # 盈亏数据未到达前 — 显示 "--" 而非误导性的 $0.00 (含 API 期权持仓在
            # reqPnLSingle 到达前; 以及正股/ETF 行情未到时)。
            # 非美元标的以本币市值为就绪信号 (有市值即可折算)。
            price_ready = (
                data["current_price"] > 0
                or (cur_code != "USD" and bool(data.get("market_value")))
            )
            if not data.get("has_pnl", True) or (
                data["sec_type"] != "OPT" and not price_ready
            ):
                for col in (4, 5, 6, 7, 8):
                    self._set_cell(row_idx, col, "--", COLOR_TEXT_DIM)
                self.table.setRowHeight(row_idx, 28)
                continue

            # 市价 — 折算 USD; 本币现价放 tooltip
            px_text = self._fmt_money(data["current_price"], cur_code, fx)
            if cur_code != "USD" and to_usd:
                px_tip = (f"本币现价: {data['current_price']:,.2f} {cur_code}"
                          f"\n汇率: 1 {cur_code} ≈ ${fx:.4f}")
            else:
                px_tip = ""
            self._set_cell(row_idx, 4, px_text, COLOR_TEXT, tooltip=px_tip)

            # 市值 — 折算 USD (本币市值 × 汇率)
            mv_text = self._fmt_money(data["market_value"], cur_code, fx)
            mv_tip = (f"本币市值: {data['market_value']:,.2f} {cur_code}"
                      if cur_code != "USD" and to_usd else "")
            self._set_cell(row_idx, 5, mv_text, COLOR_TEXT, tooltip=mv_tip)

            # 今日盈亏 — 折算 USD (from reqPnLSingle; 本地期权无此项)
            daily = data.get("daily")
            if daily is None:
                self._set_cell(row_idx, 6, "--", COLOR_TEXT_DIM)
            else:
                d_color = COLOR_GREEN if daily >= 0 else COLOR_RED
                # IBKR 的 dailyPnL 是**按合约**统计的当日总盈亏, 含今天已经平掉的
                # 部分 —— 所以刚建的仓也可能显示一个很大的数 (实测 SPX 7750C:
                # 今日 +205.20, 而当前这笔的浮动只有 -1.54, 差额来自同一行权价
                # 今天早些时候的来回)。把构成写进 tooltip, 免得被读成"这笔赚了这么多"。
                #   dailyPnL = 当前浮动 + 该合约今日已实现   (无隔夜仓时精确成立)
                realized_today = daily - data["pnl"]
                if abs(realized_today) >= 0.005:
                    daily_tip = (
                        f"IBKR 今日盈亏 (按**合约**统计, 含今天已平仓部分):\n"
                        f"  当前持仓浮动: "
                        f"{self._fmt_money(data['pnl'], cur_code, fx, signed=True)}\n"
                        f"  该合约今日已实现: "
                        f"{self._fmt_money(realized_today, cur_code, fx, signed=True)}\n"
                        f"  合计: {self._fmt_money(daily, cur_code, fx, signed=True)}\n\n"
                        f"隔夜持仓时该拆分为近似值 (浮动以均价计, 非昨收)。"
                    )
                else:
                    daily_tip = (
                        "IBKR 今日盈亏 (按合约统计, 含今天已平仓部分)。\n"
                        "该合约今日无已实现盈亏, 故等于当前持仓浮动。"
                    )
                self._set_cell(row_idx, 6,
                               self._fmt_money(daily, cur_code, fx, signed=True),
                               d_color, tooltip=daily_tip)

            # 盈亏(含费) — 折算 USD。佣金按其自身币种/汇率折算 (台股佣金按 TWD 计)
            pnl = data["pnl"]
            pnl_color = COLOR_GREEN if pnl >= 0 else COLOR_RED
            pnl_str = self._fmt_money(pnl, cur_code, fx, signed=True)
            comm = data.get("commission", 0)
            if comm and comm > 0:
                comm_ccy = data.get("commission_ccy", cur_code)
                comm_fx = data.get("commission_fx", fx)
                pnl_str += f" (费{self._fmt_money(comm, comm_ccy, comm_fx)})"
            self._set_cell(row_idx, 7, pnl_str, pnl_color)
            # 汇总总盈亏: 只累加能折算成 USD 的 (汇率未到的非美元行暂不计入)
            if to_usd:
                total_pnl += pnl * (1.0 if cur_code == "USD" else fx)

            # P/L %
            pct = data["pnl_pct"]
            pct_color = COLOR_GREEN if pct >= 0 else COLOR_RED
            self._set_cell(row_idx, 8, f"{pct:+.1f}%", pct_color)

            self.table.setRowHeight(row_idx, 28)

        # Summary (总盈亏统一按 USD 汇总)
        pnl_color = COLOR_GREEN if total_pnl >= 0 else COLOR_RED
        pnl_str = f"+${total_pnl:,.2f}" if total_pnl >= 0 else f"-${abs(total_pnl):,.2f}"
        self.summary_label.setText(f"总盈亏: {pnl_str}")
        self.summary_label.setStyleSheet(f"color: {pnl_color}; padding: 4px; font-weight: bold;")

        count = len(rows_data)
        self.title.setText(f"持仓 ({count})")

        # 账户级未实现盈亏汇总 → 账户栏。刻意**不用**上面的 total_pnl: 那个是按
        # 面板筛选 (期权/正股/期货) 之后的行算的, 一筛选账户栏就会跟着少一半。
        acct_unrl = 0.0
        acct_ok = False
        for pp in self._portfolio_positions.values():
            if not pp.has_pnl_data:
                continue
            fx = 1.0 if pp.currency == "USD" else pp.fx_rate
            if not fx:
                continue  # 汇率未到 (非美元标的), 该行暂不计入, 免得汇总跳数
            acct_unrl += pp.unrealized_pnl * fx
            acct_ok = True
        if acct_ok or not self._portfolio_positions:
            # 空仓时也要发 0.0, 否则平完仓账户栏还挂着上一笔浮盈
            self.portfolio_unrealized_changed.emit(acct_unrl)

    @staticmethod
    def _fmt_money(native_val: float, cur_code: str, fx: float, signed: bool = False) -> str:
        """把**本币**金额按显示规则格式化。

        能折算 USD (USD 标的, 或非美元但已拿到汇率) → 显示 `$X`;
        汇率未到 → 退回显示本币 `X CCY`, 避免把台币数字冠上美元符号。
        signed=True 时带 +/- 号 (盈亏列用)。
        """
        if cur_code == "USD":
            usd = native_val
        elif fx and fx > 0:
            usd = native_val * fx
        else:
            # 汇率未知 → 显示本币原值 + 货币代码
            body = f"{native_val:,.2f} {cur_code}"
            return (("+" + body) if native_val >= 0 else ("-" + f"{abs(native_val):,.2f} {cur_code}")) if signed else body
        if signed:
            return f"+${usd:,.2f}" if usd >= 0 else f"-${abs(usd):,.2f}"
        return f"${usd:,.2f}"

    def _brush(self, color: str) -> QBrush:
        b = self._brush_cache.get(color)
        if b is None:
            b = QBrush(QColor(color))
            self._brush_cache[color] = b
        return b

    def _set_cell(self, row, col, text, color, tooltip=None):
        item = self.table.item(row, col)
        if item is None:
            item = QTableWidgetItem()
            item.setTextAlignment(Qt.AlignCenter)
            self.table.setItem(row, col, item)
        # Skip redundant setText (avoids needless cell repaint for static
        # columns like name/qty/avg that don't change between ticks).
        if item.text() != text:
            item.setText(text)
        item.setForeground(self._brush(color))
        # tooltip: 非美元标的把本币原价/汇率放这里 (None = 不改动, "" = 清空)
        if tooltip is not None and item.toolTip() != tooltip:
            item.setToolTip(tooltip)

    def _on_double_click(self, index):
        if not self._engine:
            return
        row = index.row()

        # Rebuild same data to find the option at this row
        rows_data = []
        for key, pos in self._engine.positions.items():
            rows_data.append({"option": pos.option, "sec_type": "OPT", "key": key})

        for key, pp in self._portfolio_positions.items():
            if key not in {r["key"] for r in rows_data}:
                # Build an OptionInfo so double-clicking an option held from a
                # prior session (only known via reqPositions) opens the ladder
                # and lets the user close it. Stocks/ETFs stay None (handled
                # by the dedicated stock trader).
                opt = None
                if pp.sec_type == "OPT":
                    opt = OptionInfo(
                        symbol=pp.symbol, expiry=pp.expiry,
                        strike=pp.strike, right=pp.right, con_id=pp.con_id,
                    )
                elif pp.sec_type in ("STK", "ETF"):
                    opt = OptionInfo(symbol=pp.symbol, expiry="",
                                     strike=0.0, right="STK", con_id=pp.con_id)
                elif pp.sec_type == "FUT":
                    opt = OptionInfo(symbol=pp.symbol, expiry=pp.expiry,
                                     strike=0.0, right="FUT", con_id=pp.con_id)
                rows_data.append({"option": opt, "sec_type": pp.sec_type, "key": key})

        # Apply same filter
        if self._current_filter == "期权":
            rows_data = [r for r in rows_data if r["sec_type"] == "OPT"]
        elif self._current_filter == "正股/ETF":
            rows_data = [r for r in rows_data if r["sec_type"] in ("STK", "ETF")]
        elif self._current_filter == "期货":
            rows_data = [r for r in rows_data if r["sec_type"] == "FUT"]

        if row < len(rows_data):
            opt = rows_data[row].get("option")
            if opt:
                self.position_clicked.emit(opt)

    def cleanup(self):
        self._refresh_timer.stop()
