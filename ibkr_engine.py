"""IBKR API Engine — EWrapper/EClient + order management.

Reuses connection patterns from tradebot/ibkr_paper_trader.py,
adds order placement/cancellation and Qt signal bridge.
"""

import json
import os
import time
import threading
from datetime import datetime

from ibapi.client import EClient
from ibapi.wrapper import EWrapper
from ibapi.contract import Contract
from ibapi.order import Order
from ibapi.execution import ExecutionFilter

from PyQt5.QtCore import QObject, pyqtSignal, QTimer, QThread, QMetaObject, Qt

from config import (
    IBKR_HOST, IBKR_PAPER_PORT, IBKR_LIVE_PORT,
    IBKR_GW_PAPER_PORT, IBKR_GW_LIVE_PORT, USE_GATEWAY,
    IBKR_CLIENT_ID, MARKET_DATA_TYPE, IGNORED_ERROR_CODES,
    DATA_CONNECTION_ERROR_CODES,
    COMMISSION_PER_CONTRACT, COMMISSION_MIN, DEPTH_ROWS,
    INDEX_SYMBOLS, FUTURES_SPECS,
)
from models import (
    OptionInfo, OrderInfo, PositionInfo, PortfolioPosition,
    OrderAction, OrderStatus, OrderType, TradingMode,
)
from trade_stats import TradeStats


# Known PM-settled weekly option trading classes for index symbols, used as
# a fallback when the option chain hasn't been loaded yet (e.g. a contract
# typed directly into the price ladder). These are the actively-quoted
# intraday/0DTE classes.
_INDEX_WEEKLY_CLASS = {
    "SPX": "SPXW",
    "XSP": "XSP",
    "RUT": "RUTW",
    "NDX": "NDXP",
}


# ── Qt Signal Bridge ─────────────────────────────────────────────────

class IBKRSignalBridge(QObject):
    """Thread-safe bridge: IBKR reader thread -> Qt GUI thread."""

    tick_updated = pyqtSignal(str, float, float, float)  # key, bid, ask, last
    chain_ready = pyqtSignal(list, list)                 # expirations, strikes
    order_status_changed = pyqtSignal(int, str, float, float, float)  # orderId, status, filled, remaining, avgPrice
    execution_received = pyqtSignal(int, str, float, float)  # orderId, action, qty, price
    position_changed = pyqtSignal()
    connected = pyqtSignal()
    disconnected = pyqtSignal()
    error_received = pyqtSignal(int, int, str)  # reqId, code, msg
    contract_detail_received = pyqtSignal(int, object)  # reqId, contractDetails

    # Symbol search results: list of (symbol, secType, description) tuples
    symbol_search_results = pyqtSignal(list)

    # New signals for account, portfolio, depth, pnl
    account_summary_updated = pyqtSignal(str, str, str, str)  # tag, value, currency, account
    account_summary_end = pyqtSignal()
    # Per-currency cash balances (reqAccountSummary "$LEDGER:ALL")
    currency_balance_updated = pyqtSignal(str, float)  # currency, cash
    currency_balances_end = pyqtSignal()
    portfolio_position_received = pyqtSignal(object)  # PortfolioPosition
    portfolio_positions_end = pyqtSignal()
    pnl_updated = pyqtSignal(float, float, float)  # dailyPnL, unrealizedPnL, realizedPnL
    daily_commission_updated = pyqtSignal(float)   # 今日累计手续费 (USD)
    computed_daily_pnl = pyqtSignal(float, float)  # 自算今日总盈亏(已扣费), 今日手续费
    depth_updated = pyqtSignal(int, int, int, int, float, int)  # reqId, position, operation, side, price, size

    # Historical data signals
    historical_bars_ready = pyqtSignal(int, list)   # reqId, list[dict]
    historical_bar_update = pyqtSignal(int, dict)   # reqId, bar dict (streaming)

    # Open order restore (from TWS on reconnect)
    # orderId, OptionInfo, action_str, qty, price, order_type_str, status_str
    open_order_received = pyqtSignal(int, object, str, int, float, str, str)

    # Order rejected/cancelled by IBKR (not user-requested): orderId, code, msg
    order_rejected = pyqtSignal(int, int, str)

    # Per-position PnL (reqPnLSingle): conId, pos, dailyPnL, unrealizedPnL, value
    pnl_single_updated = pyqtSignal(int, float, float, float, float)

    # 已平仓交易统计 (笔数/胜率/盈亏比) — snapshot dict
    trade_stats_updated = pyqtSignal(object)


# ── IBKR API App ─────────────────────────────────────────────────────

# **进程级** reqId 计数器 —— 绝不能跟着 IBKRApp 实例复位。
#
# connect() 每次 (含重连、实盘↔模拟切换、326 换 clientId 重试) 都新建一个
# IBKRApp。计数器若随实例回到 1000, 而 Gateway 仍持有上一次连接 (同 clientId)
# 的行情订阅, 新的 reqMktData 就会撞上已在用的 tickerId, IBKR 直接回
# 「322 Duplicate ticker id」把这次订阅**拒掉** —— 那条行情线永远不推数据,
# 点价梯就永远停在最后一次报价上。这正是"某些标的点价交易卡住"的根因。
_REQ_ID_LOCK = threading.Lock()
_REQ_ID_SEQ = 1000


class IBKRApp(EWrapper, EClient):
    """EWrapper + EClient with callbacks for option chain, ticks, orders."""

    def __init__(self, bridge: IBKRSignalBridge):
        EClient.__init__(self, self)
        self.bridge = bridge

        self.connected_event = threading.Event()
        self.client_id_in_use = False  # set on error 326
        self._req_id_lock = threading.Lock()
        self._req_id = 1000
        self._next_order_id = 0
        self._order_id_lock = threading.Lock()

        # Contract details
        self._contract_data: dict[int, dict] = {}

        # Option parameters (reqSecDefOptParams)
        self._opt_data: dict[int, dict] = {}

        # Tick subscriptions: reqId -> option key
        self._tick_req_to_key: dict[int, str] = {}
        self._tick_data: dict[str, dict] = {}  # key -> {bid, ask, last}
        # reqId -> (contract, generic_ticks) 供"未订阅实时行情"时切延迟后重订
        self._tick_req_contract: dict[int, tuple] = {}
        # reqId -> 该**行情线**最后一次收到 tick 的时刻 (判断这条线是否已死;
        # 不能用合约级时间戳, 会被期权链的快照刷新掩盖 — 见 _touch_tick)
        self._tick_req_last: dict[int, float] = {}
        # key -> 因 322 重订的次数 (防止一直撞车时无限重试)
        self._tick_retry: dict[str, int] = {}

        # Active subscriptions for cleanup
        self._active_mkt_data_reqs: set[int] = set()
        # 一次性: 收到"未订阅实时行情"错误后切换为延迟行情 (模拟盘/无期货行情时有用)
        self._delayed_fallback_done: bool = False

        # Market depth tracking
        self._depth_req_id: int | None = None
        self._depth_not_supported: bool = False
        self._depth_is_smart: bool = False

        # Account summary tracking
        self._account_summary_req_id: int | None = None
        # Per-currency cash ledger tracking ("$LEDGER:ALL")
        self._ledger_req_id: int | None = None

        # PnL tracking
        self._pnl_req_id: int | None = None

        # Per-position PnL subscriptions: reqId -> conId
        self._pnl_single_reqs: dict[int, int] = {}

        # Account name. Authoritative source = managedAccounts (sent on connect);
        # accountSummary is only a fallback. Using a non-managed / wrong account
        # code in reqPnL/reqPnLSingle triggers error 321 "Invalid account code".
        self._account_name: str = ""
        self._managed_accounts: list[str] = []
        self._acct_err_warned = False   # error 321 仅提示一次, 不刷屏

        # 自算「今日期权交易总盈亏」(不依赖 IBKR 的 dailyPnL —— 它常为 -- / 未含费)。
        # 今日盈亏 = 今日成交现金流 + 当前持仓市值 − 今日手续费, 全部按 execId/conId 去重、跨日清零。
        #   _exec_cf:  execId -> 现金流 (卖 +price*qty*mult, 买 -price*qty*mult)
        #   _posval:   conId  -> 持仓市值 (来自 reqPnLSingle 的 value, 平仓即移除)
        #   _comm_by_execid: execId -> 手续费 (commissionReport)
        # 对当日开平的仓 (0DTE 日内) 精确; 隔夜持仓卖出当天会略偏 (无昨日买入现金流)。
        self._pnl_day = None
        self._exec_cf: dict[str, float] = {}
        self._posval: dict[int, float] = {}
        self._comm_by_execid: dict[str, float] = {}
        # execId -> 手续费计价货币 (commissionReport.currency)。非美元标的 (如台股)
        # 的佣金按当地货币计 (TWD), 显示前需按持仓汇率折成 USD, 否则「费$40」其实是
        # 40 TWD 被误当成美元。
        self._comm_ccy_by_execid: dict[str, str] = {}
        # 币种 → **账户基础货币** 汇率, 来自 ledger 的 ExchangeRate 标签
        # ($LEDGER:ALL 每种持有货币推一行)。注意方向: 是"→基础货币"而不是"→USD",
        # 换 USD 统一走 IBKREngine.to_usd_rate。
        # **不能**预置 {"USD": 1.0} —— 那等于断言账户基础货币就是 USD, 在非 USD 账户上
        # 会让 to_usd_rate 在 ledger 到达前算出错得离谱的值。宁可先返回"未知"。
        self._fx_rates: dict[str, float] = {}
        # 账户基础货币 (accountSummary 的 NetLiquidation 行带的 currency)
        self._base_currency: str = ""
        # execId -> conId (execDetails 记录), 用于把 commissionReport 的佣金
        # 归到具体合约 → 点价梯/持仓面板显示该合约今日实际已付手续费
        self._exec_conid: dict[str, int] = {}
        # 已平仓交易统计 (笔数/胜率/盈亏比), 由 execDetails 喂入 (含当日历史回放), 每日重置
        self._trade_stats = TradeStats()

        # One-time market data warning flag
        self._mkt_data_warned: bool = False

        # Heartbeat: timestamp of last tick received (for timeout detection)
        self._last_tick_time: float = 0.0

        # Historical data tracking
        self._hist_data: dict[int, dict] = {}

    def next_req_id(self) -> int:
        """全进程单调递增的 reqId (见 _REQ_ID_SEQ 说明: 不随重连复位)。"""
        global _REQ_ID_SEQ
        with _REQ_ID_LOCK:
            _REQ_ID_SEQ += 1
            self._req_id = _REQ_ID_SEQ
            return _REQ_ID_SEQ

    def next_order_id(self) -> int:
        with self._order_id_lock:
            oid = self._next_order_id
            self._next_order_id += 1
            return oid

    def _switch_to_delayed_and_resubscribe(self):
        """收到"未订阅实时行情"后, 一次性切到延迟行情并重订当前所有流式行情线。
        复用各自原 reqId (cancel 后同 id 重订), 以免点价梯/链里记录的 reqId 失效。
        已订阅实时的合约在延迟模式下仍下发实时, 只有无实时的(如模拟盘期货)才转延迟。"""
        if self._delayed_fallback_done:
            return
        self._delayed_fallback_done = True
        try:
            self.reqMarketDataType(3)  # 3 = 延迟 (无实时订阅时给 15 分钟延迟)
        except Exception:
            pass
        for req, ct in list(self._tick_req_contract.items()):
            contract, gticks = ct
            try:
                self.cancelMktData(req)
                self.reqMktData(req, contract, gticks, False, False, [])
            except Exception:
                pass
        print("[INFO] 实时行情未订阅 → 已切换为延迟行情并重订", flush=True)
        self.bridge.error_received.emit(
            -1, 10168, "实时行情未订阅 → 已切换为延迟行情 (15分钟延迟)"
        )

    _MAX_TICK_RETRY = 3

    def _retry_tick_with_new_id(self, req_id: int, why: str):
        """行情订阅被「Duplicate ticker id」拒掉 → 换个全新 reqId 重订同一合约。

        面板持有的旧 reqId 就此作废, 但它们只用它退订 (对已不存在的 id 调
        cancelMktData 只会引出无害的 300, 已单独静默处理), **显示一律走 key**,
        所以数据恢复对所有面板同时生效, 不需要面板配合改动。
        """
        key = self._tick_req_to_key.pop(req_id, None)
        ct = self._tick_req_contract.pop(req_id, None)
        self._active_mkt_data_reqs.discard(req_id)
        self._tick_req_last.pop(req_id, None)
        if key is None or ct is None:
            print(f"[MKTDATA] reqId={req_id} 被拒但已无合约记录, 放弃重订 ({why})",
                  flush=True)
            return

        n = self._tick_retry.get(key, 0) + 1
        if n > self._MAX_TICK_RETRY:
            print(f"[MKTDATA] {key} 连续 {n-1} 次撞 duplicate ticker id, 停止重订",
                  flush=True)
            self.bridge.error_received.emit(
                req_id, 322, f"{key} 行情订阅反复被拒 — 建议重启 Gateway")
            return
        self._tick_retry[key] = n

        contract, gticks = ct
        new_id = self.next_req_id()
        self._tick_req_to_key[new_id] = key
        self._tick_req_contract[new_id] = ct
        self._active_mkt_data_reqs.add(new_id)
        try:
            self.reqMktData(new_id, contract, gticks, False, False, [])
            print(f"[MKTDATA] {key} 行情订阅被拒 (reqId={req_id}: {why}) "
                  f"→ 已改用 reqId={new_id} 重订 (第 {n} 次)", flush=True)
        except Exception as e:
            self._tick_req_to_key.pop(new_id, None)
            self._tick_req_contract.pop(new_id, None)
            self._active_mkt_data_reqs.discard(new_id)
            print(f"[MKTDATA] {key} 换 id 重订失败: {e}", flush=True)

    def resubscribe_all_ticks(self, reason: str = ""):
        """把所有**流式**行情线 cancel 后按原 reqId 重订。

        用于 IBKR 报 1101 ("connectivity restored — DATA LOST"): 此时 TWS/Gateway
        已把之前的行情订阅全部丢弃, 但 API 侧不会有任何回调, 各面板的 reqId 依旧
        "有效"却永远收不到 tick —— 表现就是点价梯/期权链停在断线前那一刻的价格
        (界面像被锁死)。复用原 reqId 重订, 面板记录的 reqId 无需变更。
        """
        n = 0
        for req, ct in list(self._tick_req_contract.items()):
            contract, gticks = ct
            try:
                self.cancelMktData(req)
            except Exception:
                pass
            try:
                self.reqMktData(req, contract, gticks, False, False, [])
                n += 1
            except Exception as e:
                print(f"[RESUB] reqId={req} 重订失败: {e}", flush=True)
        print(f"[RESUB] 已重订 {n} 条行情线 ({reason})", flush=True)
        return n

    # ── Connection ────────────────────────────────────────────────────

    def nextValidId(self, orderId: int):
        self._next_order_id = orderId
        self.connected_event.set()
        self.bridge.connected.emit()

    def connectionClosed(self):
        self.bridge.disconnected.emit()

    def error(self, reqId, errorCode, errorString, advancedOrderRejectJson=""):
        # 326: client id already in use — wake the waiting connect() so the
        # engine can retry with a fallback id instead of timing out
        if errorCode == 326:
            print(f"[CONNECT] clientId in use: {errorString}", flush=True)
            self.client_id_in_use = True
            self.connected_event.set()
            return

        # Log order-related errors (reqId in order ID range) for debugging
        if reqId >= self._next_order_id - 100 and reqId < self._next_order_id + 100:
            print(f"[ORDER ERROR] reqId={reqId} code={errorCode} "
                  f"msg={errorString}", flush=True)
            if advancedOrderRejectJson:
                print(f"[ORDER REJECT] {advancedOrderRejectJson}", flush=True)

        # 1100/1101/1102: 与 IBKR 服务器的连通性。**1101 = 已恢复但行情订阅丢失**,
        # 必须把所有行情线重订, 否则各面板的 reqId 永远收不到 tick —— 点价梯就
        # 停在断线前那一刻的价格不动了 (本次 bug 的主因之一)。
        # 1102 = 已恢复且订阅保留, 1100 = 断开, 都只记日志。
        if errorCode in (1100, 1101, 1102):
            print(f"[CONN] code={errorCode} msg={errorString}", flush=True)
            if errorCode == 1101:
                self.resubscribe_all_ticks("1101 连接恢复但行情订阅丢失")
            self.bridge.error_received.emit(reqId, errorCode, errorString)
            return

        # 322 / 102 「Duplicate ticker id」—— 本次 reqMktData 被**直接拒绝**,
        # 这条线一个 tick 都不会来, 面板于是永远停在最后一次报价上 (AAPL 点价梯
        # 卡死就是这个)。成因: Gateway 还持有上一次连接 (同 clientId) 的同号订阅。
        # 处理: 换一个全新 reqId 把同一个合约重订上。key 不变, 所有面板都是按 key
        # 轮询行情的, 所以数据一恢复, 点价梯/期权链/计算器全部同时恢复。
        if errorCode in (102, 322) and "duplicate ticker" in errorString.lower():
            if reqId in self._tick_req_to_key:
                self._retry_tick_with_new_id(reqId, errorString)
                return

        # 行情线相关的硬错误 —— 这条 reqId 已经死了, IBKR 不会再推任何 tick。
        # 101   = 行情线用尽 (Max number of tickers has been reached)
        # 102   = ticker id 重复
        # 309   = 深度订阅数用尽
        # 10197 = 有其它会话在用同一份行情 (competing live session)
        # 以前这些只在状态栏一闪而过、日志里没有任何痕迹, 面板则一直显示旧价格。
        if errorCode in (101, 102, 309, 10197):
            key = self._tick_req_to_key.pop(reqId, None)
            self._active_mkt_data_reqs.discard(reqId)
            self._tick_req_contract.pop(reqId, None)
            print(f"[MKTDATA] 行情线失效 reqId={reqId} key={key} "
                  f"code={errorCode} msg={errorString} "
                  f"(当前活跃行情线 {len(self._active_mkt_data_reqs)} 条)", flush=True)
            self.bridge.error_received.emit(reqId, errorCode, errorString)
            return

        # 161 / 10147 / 10148: 撤单类无害响应 —— 订单已不可撤 (多半已成交或已撤) 或
        # 找不到。**不是拒单**: 不弹框、不标 ERROR、不写拒单日志 (否则会把刚成交的单
        # 误显示成"已拒绝")。上面那行 [ORDER ERROR] 已在 app 日志留痕供排查。
        if errorCode in (161, 10147, 10148):
            return

        # Market data subscription info — show one-time warning then suppress
        if errorCode == 10167:
            if not self._mkt_data_warned:
                self._mkt_data_warned = True
                print(f"[INFO] {errorString}", flush=True)
                self.bridge.error_received.emit(
                    reqId, errorCode, "行情为延迟数据 (15分钟延迟)"
                )
            return

        # 354 / 10168: 该合约无实时行情订阅 (模拟盘未共享行情, 或期货无行情包)。
        # 一次性切换为**延迟行情**并重订当前所有行情线, 让点价梯/链至少有延迟报价。
        # (引擎已能解析延迟 tick 66/67/68; 已订阅实时的合约仍走实时。)
        if errorCode in (354, 10168) and reqId in self._tick_req_to_key:
            if not self._delayed_fallback_done:
                self._switch_to_delayed_and_resubscribe()
            else:
                # 已切延迟仍报 → 静默 (该合约连延迟数据也没有), 不刷状态栏
                pass
            return

        # 321: "Invalid account code" — reqPnL/reqPnLSingle 用了无效账户。根因已由
        # managedAccounts 取权威账户修正; 万一仍出现 (多账户/子账户), 记下实际用的账户
        # 供排查, 并一次性提示, 不反复刷状态栏。
        if errorCode == 321:
            if not self._acct_err_warned:
                self._acct_err_warned = True
                print(f"[ACCT] 321 invalid account: {errorString} "
                      f"(account={self._account_name}, "
                      f"managed={self._managed_accounts})", flush=True)
                self.bridge.error_received.emit(
                    reqId, errorCode,
                    "账户校验失败(321) — 多账户时请确认主账户; 详见 app 日志"
                )
            return

        if errorCode in IGNORED_ERROR_CODES:
            return

        # Data-connection codes: log + surface to GUI (not silenced)
        if errorCode in DATA_CONNECTION_ERROR_CODES:
            print(f"[DATA CONN] code={errorCode} msg={errorString}", flush=True)
            self.bridge.error_received.emit(reqId, errorCode, errorString)
            return

        # Any error on depth request — silently give up (price ladder
        # falls back to bid/ask from tick data).  Covers 10092 ("deep
        # market data not supported") AND 200 ("no security definition").
        if self._depth_req_id is not None and reqId == self._depth_req_id:
            self._depth_req_id = None
            self._depth_not_supported = True
            return

        # 10189: "tick-by-tick data is not supported" (no live subscription)
        # Silently ignore — reqMktData fallback already active
        if errorCode == 10189:
            return

        # 300: "Can't find EId with tickerId" — cancelMktData on a line that's
        # already gone (e.g. a snapshot IBKR auto-cancelled, or a double
        # cancel). Harmless; clean up tracking and don't spam the status bar.
        if errorCode == 300:
            self._tick_req_to_key.pop(reqId, None)
            self._active_mkt_data_reqs.discard(reqId)
            return

        # Error 200 on a tick/market-data subscription — contract not found.
        # Clean up tracking so dead reqIds don't accumulate, and suppress
        # the status-bar spam (option chain routinely hits strikes with
        # no listed contract).
        if errorCode == 200 and reqId in self._tick_req_to_key:
            self._tick_req_to_key.pop(reqId, None)
            self._active_mkt_data_reqs.discard(reqId)
            return

        # Signal errors to waiting threads (reqContractDetails, etc.)
        for store in (self._contract_data, self._opt_data, self._hist_data):
            if reqId in store:
                store[reqId]["error"] = (errorCode, errorString)
                store[reqId]["event"].set()
                return  # handled by waiting thread, don't also spam bridge

        # 兜底: 其余错误以前只在状态栏一闪而过, 日志里毫无痕迹, 事后无法排查
        # (行情停摆类问题尤其吃亏)。统一落日志; 行情线相关的额外标出 key。
        tick_key = self._tick_req_to_key.get(reqId)
        extra = f" key={tick_key}" if tick_key else ""
        print(f"[API ERROR] reqId={reqId} code={errorCode}{extra} "
              f"msg={errorString}", flush=True)
        self.bridge.error_received.emit(reqId, errorCode, errorString)

    # ── Contract Details ──────────────────────────────────────────────

    def contractDetails(self, reqId, contractDetails):
        if reqId in self._contract_data:
            self._contract_data[reqId]["details"].append(contractDetails)
        self.bridge.contract_detail_received.emit(reqId, contractDetails)

    def contractDetailsEnd(self, reqId):
        if reqId in self._contract_data:
            self._contract_data[reqId]["event"].set()

    # ── Option Parameters ─────────────────────────────────────────────

    def securityDefinitionOptionParameter(
        self, reqId, exchange, underlyingConId, tradingClass,
        multiplier, expirations, strikes,
    ):
        if reqId in self._opt_data:
            self._opt_data[reqId]["params"].append({
                "exchange": exchange,
                "tradingClass": tradingClass,
                "expirations": sorted(expirations),
                "strikes": sorted(strikes),
            })

    def securityDefinitionOptionParameterEnd(self, reqId):
        if reqId in self._opt_data:
            self._opt_data[reqId]["event"].set()

    # ── Tick Data ─────────────────────────────────────────────────────

    def _touch_tick(self, key: str, req_id: int) -> dict:
        """取(或建)该合约的 tick 记录, 盖上合约级/**行情线级**/全局三个时间戳。

        三个粒度各有用处:
          - `d['t']`   合约级: 这个合约有没有新数据 (谁推的都算)。
          - `_tick_req_last[req_id]` **行情线级**: 判断"我自己订的这条线是不是
            死了"必须用它。只看合约级会被**期权链的快照**骗到 —— 快照写的是同一个
            key, 会不断刷新 `d['t']`, 于是点价梯的常驻订阅早就被 IBKR 拒掉/掐掉了,
            合约级时间戳却一直是新的, 停摆检测永远不触发 (实测 AAPL 卡死时
            日志里一条 [LADDER] 都没有, 就是栽在这里)。
          - `_last_tick_time` 全局: 区分"整体断流/收盘"与"单条线死了"。
        """
        d = self._tick_data.setdefault(key, {"bid": 0.0, "ask": 0.0, "last": 0.0})
        now = time.time()
        d["t"] = now
        self._tick_req_last[req_id] = now
        self._last_tick_time = now
        return d

    def tickPrice(self, reqId, tickType, price, attrib):
        if price <= 0 or price != price:
            return

        key = self._tick_req_to_key.get(reqId)
        if key is None:
            return

        d = self._touch_tick(key, reqId)

        if tickType in (1, 66):     # bid / delayed bid
            d["bid"] = float(price)
        elif tickType in (2, 67):   # ask / delayed ask
            d["ask"] = float(price)
        elif tickType in (4, 68):   # last / delayed last
            d["last"] = float(price)

        # Note: no tick_updated emit. The GUI widgets (price ladder, option
        # chain) poll _tick_data via engine.get_tick() on their own refresh
        # timers, so emitting a cross-thread queued signal here would post
        # hundreds of no-op events per second to an empty receiver list.

    def tickSize(self, reqId, tickType, size):
        # Emit volume tick types for tracking
        key = self._tick_req_to_key.get(reqId)
        if key is None:
            return
        d = self._touch_tick(key, reqId)
        if tickType in (0, 69):     # bid size / delayed bid size
            d["bid_size"] = int(size)
        elif tickType in (3, 70):   # ask size / delayed ask size
            d["ask_size"] = int(size)
        elif tickType in (5, 71):   # last size
            d["last_size"] = int(size)
        elif tickType in (8, 74):   # volume
            d["volume"] = int(size)

    def tickString(self, reqId, tickType, value):
        pass

    def tickSnapshotEnd(self, reqId):
        """One-shot snapshot delivered — IBKR已自动取消该订阅, 清掉本地映射。
        tick 数据保留在 _tick_data[key] 供 GUI 轮询显示。"""
        self._tick_req_to_key.pop(reqId, None)
        self._active_mkt_data_reqs.discard(reqId)

    def tickGeneric(self, reqId, tickType, value):
        """通用单值 tick。tickType 24 = 标的「期权隐含波动率」(请求 genericTick 106 时下发,
        即 TWS 的 Implied Vol %, 小数如 0.18=18%)。存入 _tick_data[key]['iv'] 供期权链标题显示。"""
        key = self._tick_req_to_key.get(reqId)
        if key is None:
            return
        if tickType == 24 and value is not None and value == value and value > 0:
            d = self._touch_tick(key, reqId)
            d["iv"] = float(value)

    def tickOptionComputation(
        self, reqId, tickType, tickAttrib, impliedVol, delta, optPrice,
        pvDividend, gamma, vega, theta, undPrice,
    ):
        """IBKR 期权模型计算回调 (IV + Greeks + 标的价)。
        tickType 13 = 模型值 (IB 模型 IV/greeks/undPrice, 最优来源);
        10/11/12 = bid/ask/last 计算值 (作为 IV/undPrice 的回退)。
        结果写入 _tick_data[key], 期权计算器在自己的刷新定时器里轮询。"""
        key = self._tick_req_to_key.get(reqId)
        if key is None:
            return
        d = self._touch_tick(key, reqId)

        def _ok(x):  # 非 None / 非 NaN / 正数 (IBKR 用 NaN 或负值表示无效)
            return x is not None and x == x and x > 0

        def _num(x):  # 非 None / 非 NaN (greeks 可为负, 不要求正)
            return x is not None and x == x

        if tickType == 13:  # 模型值 — 优先覆盖
            if _ok(impliedVol):
                d["iv"] = float(impliedVol)
            if _ok(undPrice):
                d["und_price"] = float(undPrice)
            if _num(delta):
                d["delta"] = float(delta)
            if _num(gamma):
                d["gamma"] = float(gamma)
            if _num(vega):
                d["vega"] = float(vega)
            if _num(theta):
                d["theta"] = float(theta)
        elif tickType in (10, 11, 12):  # bid/ask/last 计算值 — 仅在模型值缺失时回退
            if "iv" not in d and _ok(impliedVol):
                d["iv"] = float(impliedVol)
            if "und_price" not in d and _ok(undPrice):
                d["und_price"] = float(undPrice)

    # ── Historical Data Callbacks ─────────────────────────────────────

    def historicalData(self, reqId, bar):
        """Accumulate bars during initial historical data load."""
        if reqId in self._hist_data:
            self._hist_data[reqId]["bars"].append({
                "date": bar.date,
                "open": bar.open,
                "high": bar.high,
                "low": bar.low,
                "close": bar.close,
                "volume": int(bar.volume),
                "wap": float(bar.average) if bar.average else 0.0,
                "count": int(bar.barCount) if bar.barCount else 0,
            })

    def historicalDataEnd(self, reqId, start, end):
        """All initial bars received — set event for waiting thread."""
        if reqId in self._hist_data:
            self._hist_data[reqId]["event"].set()

    def historicalDataUpdate(self, reqId, bar):
        """Streaming bar update (keepUpToDate=True)."""
        bar_dict = {
            "date": bar.date,
            "open": bar.open,
            "high": bar.high,
            "low": bar.low,
            "close": bar.close,
            "volume": int(bar.volume),
            "wap": float(bar.average) if bar.average else 0.0,
            "count": int(bar.barCount) if bar.barCount else 0,
        }
        self.bridge.historical_bar_update.emit(reqId, bar_dict)

    # ── Order Callbacks ───────────────────────────────────────────────

    def orderStatus(self, orderId, status, filled, remaining,
                    avgFillPrice, permId, parentId, lastFillPrice,
                    clientId, whyHeld, mktCapPrice):
        print(f"[ORDER STATUS] orderId={orderId} status={status} "
              f"filled={filled} remaining={remaining} "
              f"avgFill={avgFillPrice} whyHeld={whyHeld}", flush=True)
        self.bridge.order_status_changed.emit(
            orderId, status, float(filled), float(remaining), float(avgFillPrice)
        )

    def openOrder(self, orderId, contract, order, orderState):
        # Track every instrument supported by the integrated ladder.  This is
        # essential for targeted cancellation after a restart: an open stock or
        # futures order must be present in `_orders` before it can be cancelled
        # individually without falling back to the dangerous global cancel.
        option = self._option_from_contract(contract)
        if option is not None:
            action = OrderAction.BUY if order.action == "BUY" else OrderAction.SELL
            price = order.lmtPrice if order.orderType == "LMT" else 0.0
            order_type = OrderType.LIMIT if order.orderType == "LMT" else OrderType.MARKET
            self.bridge.open_order_received.emit(
                orderId, option, action.value, int(order.totalQuantity),
                price, order_type.value, orderState.status,
            )
        self.bridge.order_status_changed.emit(
            orderId, orderState.status, 0, 0, 0
        )

    @staticmethod
    def _option_from_contract(contract):
        """从 ibapi Contract 构造 OptionInfo (供完成单恢复; 支持 期权/正股/期货/组合)。"""
        st = contract.secType
        if st == "OPT":
            return OptionInfo(
                symbol=contract.symbol,
                expiry=contract.lastTradeDateOrContractMonth,
                strike=contract.strike, right=contract.right,
                con_id=contract.conId,
            )
        if st == "FUT":
            return OptionInfo(
                symbol=contract.symbol,
                expiry=contract.lastTradeDateOrContractMonth,
                strike=0.0, right="FUT", con_id=contract.conId,
            )
        if st == "STK":
            return OptionInfo(symbol=contract.symbol, expiry="", strike=0.0,
                              right="STK", con_id=contract.conId)
        if st == "BAG":
            return OptionInfo(symbol=contract.symbol, expiry="", strike=0.0,
                              right="COMBO", con_id=contract.conId)
        return None

    def completedOrder(self, contract, order, orderState):
        """重启后恢复当日**已完成**(成交/撤销)的委托 —— 复用 `open_order_received`
        进委托面板, `_on_open_order` 会按 `orderState.status` 映射成 已成交/已撤销。
        跨会话的完成单 `orderId` 可能为 0, 用稳定的 `permId` 兜底作主键。"""
        option = self._option_from_contract(contract)
        if option is None:
            return
        action = OrderAction.BUY if order.action == "BUY" else OrderAction.SELL
        price = order.lmtPrice if order.orderType == "LMT" else 0.0
        order_type = OrderType.LIMIT if order.orderType == "LMT" else OrderType.MARKET
        oid = order.orderId or order.permId
        try:
            qty = int(float(order.totalQuantity))
        except (ValueError, TypeError):
            qty = 0
        self.bridge.open_order_received.emit(
            oid, option, action.value, qty, price, order_type.value,
            orderState.status,
        )

    def completedOrdersEnd(self):
        print("[COMPLETED ORDERS] 当日完成单拉取完毕", flush=True)

    def execDetails(self, reqId, contract, execution):
        # 累计今日成交现金流 (卖 +, 买 −; 含合约乘数), 用于自算今日盈亏。历史回放(reqId≥0)
        # 与实时成交(reqId=-1)都计入, 按 execId 去重。
        self._reset_pnl_if_new_day()
        try:
            mult = float(contract.multiplier) if contract.multiplier else 100.0
        except (ValueError, TypeError):
            mult = 100.0
        shares = float(execution.shares)
        price = float(execution.price)
        sign = 1.0 if execution.side == "SLD" else -1.0   # 卖出进现金 / 买入出现金
        is_new_exec = execution.execId not in self._exec_cf   # 防同一 execId 重复计入
        self._exec_cf[execution.execId] = sign * price * shares * mult
        self._emit_computed_daily()

        # 已平仓交易统计 (笔数/胜率/盈亏比): 喂入本笔成交 (历史回放 reqId>=0 也计入 →
        # 覆盖「今日全部」)。按 conId 分合约做回合制 FIFO; 佣金用估算。
        try:
            con_id = int(getattr(contract, "conId", 0) or 0)
        except (ValueError, TypeError):
            con_id = 0
        if con_id:
            self._exec_conid[execution.execId] = con_id
        if con_id and is_new_exec:
            est_comm = max(COMMISSION_PER_CONTRACT * shares, COMMISSION_MIN)
            self._trade_stats.record_fill(
                con_id, execution.side, shares, price, mult, est_comm
            )
            self.bridge.trade_stats_updated.emit(self._trade_stats.snapshot())

        # reqId >= 0 → reqExecutions 的当日历史回放: 不触发成交音/持仓事件, 否则启动时会把
        # 今天每笔成交的提示音全播一遍。实时成交由 IBKR 以 reqId = -1 推送。
        if reqId is not None and reqId >= 0:
            return
        self.bridge.execution_received.emit(
            execution.orderId, execution.side, shares, price,
        )

    def execDetailsEnd(self, reqId):
        # 当日历史成交拉取完毕 → 推一次自算今日盈亏 (即便今天无成交, 也给出 0 基线)
        self._emit_computed_daily()
        self.bridge.trade_stats_updated.emit(self._trade_stats.snapshot())

    def commissionReport(self, commissionReport):
        """累计今日手续费 (按 execId 去重)。每笔成交后到达;`reqExecutions` 连接时也会
        补发当日历史成交的 commissionReport。汇总后并入自算今日盈亏。"""
        commission = float(commissionReport.commission)
        if commission >= 1e9:  # IBKR sends 1.7976931e+308 for unknown
            return
        self._reset_pnl_if_new_day()
        self._comm_by_execid[commissionReport.execId] = commission
        # 记下佣金计价货币 (可能是 TWD/HKD 等), 供持仓面板折算成 USD 显示
        self._comm_ccy_by_execid[commissionReport.execId] = (
            getattr(commissionReport, "currency", "") or ""
        )
        self._emit_computed_daily()

    # ── 自算今日盈亏 helpers ───────────────────────────────────────────

    def _reset_pnl_if_new_day(self):
        today = datetime.now().date()
        if self._pnl_day != today:
            self._pnl_day = today
            self._exec_cf.clear()
            self._posval.clear()
            self._comm_by_execid.clear()
            self._comm_ccy_by_execid.clear()
            self._exec_conid.clear()
            self._trade_stats.reset()   # 交易统计也按日重置 (笔数/胜率/盈亏比)

    def _emit_computed_daily(self):
        """今日盈亏 = 今日成交现金流 + 当前持仓市值 − 今日手续费 (已扣费)。"""
        cashflow = sum(self._exec_cf.values())
        open_val = sum(self._posval.values())
        comm = sum(self._comm_by_execid.values())
        total = cashflow + open_val - comm
        self.bridge.computed_daily_pnl.emit(total, comm)

    # ── Account Summary Callbacks ────────────────────────────────────

    def managedAccounts(self, accountsList: str):
        """IBKR 连接后自动下发的**有效账户代码**列表 (逗号分隔), 无需请求。

        这是 reqPnL/reqPnLSingle 该用的权威账户来源。之前只从 accountSummary 取账户,
        多账户/账户组时会被最后一行覆盖成一个 reqPnL 不接受的代码 → error 321
        "Invalid account code"。这里取第一个有效账户为主账户。"""
        accts = [a.strip() for a in (accountsList or "").split(",") if a.strip()]
        self._managed_accounts = accts
        if accts:
            self._account_name = accts[0]

    def accountSummary(self, reqId, account, tag, value, currency):
        # 仅在 managedAccounts 尚未给出账户时, 才用 summary 行兜底 (避免被多账户/
        # 账户组的某一行覆盖成 reqPnL 不接受的代码 → 321)。
        if not self._account_name:
            self._account_name = account
        # 账户基础货币: NetLiquidation 这行带的 currency 就是它。
        # to_usd_rate 要靠它把 ledger 的"→基础货币"汇率换算成"→USD"。
        if tag == "NetLiquidation" and currency and currency != "BASE":
            self._base_currency = currency
        # Ledger request → per-currency cash balances (separate subscription)
        if reqId == self._ledger_req_id:
            if tag == "CashBalance" and currency and currency != "BASE":
                try:
                    self.bridge.currency_balance_updated.emit(currency, float(value))
                except (ValueError, TypeError):
                    pass
            elif tag == "ExchangeRate" and currency and currency != "BASE":
                # 该货币 → 基础货币(USD) 汇率, 供持仓面板折算非美元标的
                try:
                    self._fx_rates[currency] = float(value)
                except (ValueError, TypeError):
                    pass
            return
        self.bridge.account_summary_updated.emit(tag, value, currency, account)

    def accountSummaryEnd(self, reqId):
        if reqId == self._ledger_req_id:
            self.bridge.currency_balances_end.emit()
            return
        self.bridge.account_summary_end.emit()

    # ── Position Callbacks ────────────────────────────────────────────

    def position(self, account, contract, pos, avgCost):
        pp = PortfolioPosition(
            con_id=contract.conId,
            symbol=contract.symbol,
            sec_type=contract.secType,
            expiry=getattr(contract, 'lastTradeDateOrContractMonth', ''),
            strike=getattr(contract, 'strike', 0.0),
            right=getattr(contract, 'right', ''),
            quantity=float(pos),
            avg_price=float(avgCost),
            currency=contract.currency,
            multiplier=float(contract.multiplier) if contract.multiplier else 1.0,
        )
        # For options, IBKR returns avgCost already multiplied by multiplier
        if pp.sec_type == "OPT" and pp.multiplier > 1:
            pp.avg_price = float(avgCost) / pp.multiplier
        # 平仓 → 从自算盈亏的持仓市值里移除, 避免残留旧市值
        if pos == 0:
            self._posval.pop(contract.conId, None)
            self._emit_computed_daily()
        self.bridge.portfolio_position_received.emit(pp)

    def positionEnd(self):
        self._emit_computed_daily()  # 初始持仓快照完成 → 给出 0 基线 (即便空仓)
        self.bridge.portfolio_positions_end.emit()

    # ── PnL Callbacks ─────────────────────────────────────────────────

    def pnl(self, reqId, dailyPnL, unrealizedPnL, realizedPnL):
        # IBKR 对尚未算出的字段推 DBL_MAX (~1.8e308) → 转 NaN, GUI 保留上一次的好值。
        # 某些账户的 dailyPnL 会长期为 DBL_MAX(不可用), 但 unrealized/realized 有效 →
        # GUI 用 realized+unrealized 兜底算今日盈亏 (见 account_bar.update_daily_pnl)。
        def _clean(v):
            v = float(v)
            return float("nan") if abs(v) > 1e300 else v
        self.bridge.pnl_updated.emit(
            _clean(dailyPnL), _clean(unrealizedPnL), _clean(realizedPnL)
        )

    def pnlSingle(self, reqId, pos, dailyPnL, unrealizedPnL,
                  realizedPnL, value):
        con_id = self._pnl_single_reqs.get(reqId)
        if con_id is None:
            return

        # IBKR 对尚未算出的字段推 DBL_MAX (~1.8e308)。这些字段是「本次无效」,
        # 而非真的 0 —— 转成 NaN, 让 GUI 保留上一次的好值, 避免盈亏闪烁成 0。
        def _clean(v):
            v = float(v)
            return float("nan") if abs(v) > 1e300 else v

        # 持仓市值并入自算今日盈亏 (平仓 pos=0 即移除)
        v_val = float(value)
        if abs(v_val) < 1e300:
            self._reset_pnl_if_new_day()
            if pos == 0:
                self._posval.pop(con_id, None)
            else:
                self._posval[con_id] = v_val
            self._emit_computed_daily()

        self.bridge.pnl_single_updated.emit(
            con_id, float(pos), _clean(dailyPnL),
            _clean(unrealizedPnL), _clean(value),
        )

    # ── Market Depth Callbacks ────────────────────────────────────────

    def updateMktDepth(self, reqId, position, operation, side, price, size):
        self.bridge.depth_updated.emit(
            reqId, position, operation, side, float(price), int(size)
        )

    def updateMktDepthL2(self, reqId, position, marketMaker, operation,
                         side, price, size, isSmartDepth):
        # Treat L2 depth same as L1 for our purposes
        self.bridge.depth_updated.emit(
            reqId, position, operation, side, float(price), int(size)
        )

    # ── Symbol Search Callbacks ─────────────────────────────────────

    def symbolSamples(self, reqId, contractDescriptions):
        results = []
        for cd in contractDescriptions:
            c = cd.contract
            if c.currency == "USD" and c.secType in ("STK", "IND", "ETF"):
                derivs = ", ".join(cd.derivativeSecTypes) if cd.derivativeSecTypes else ""
                desc = f"{c.primaryExchange}"
                if derivs:
                    desc += f" [{derivs}]"
                results.append((c.symbol, c.secType, desc))
        self.bridge.symbol_search_results.emit(results)

    # ── Tick-by-Tick Callbacks ────────────────────────────────────────

    def tickByTickBidAsk(self, reqId, time_, bidPrice, askPrice,
                         bidSize, askSize, tickAttribBidAsk):
        key = self._tick_req_to_key.get(reqId)
        if key is None:
            return
        d = self._touch_tick(key, reqId)
        d["bid"] = float(bidPrice)
        d["ask"] = float(askPrice)
        d["bid_size"] = int(bidSize)
        d["ask_size"] = int(askSize)
        # GUI polls via get_tick(); no cross-thread emit needed (see tickPrice).

    def tickByTickAllLast(self, reqId, tickType, time_, price,
                          size, tickAttribLast, exchange, specialConditions):
        key = self._tick_req_to_key.get(reqId)
        if key is None:
            return
        d = self._touch_tick(key, reqId)
        d["last"] = float(price)
        # GUI polls via get_tick(); no cross-thread emit needed (see tickPrice).


# ── IBKR Engine (high-level interface) ───────────────────────────────

class IBKREngine:
    """High-level engine wrapping IBKRApp for the GUI."""

    CONNECT_TIMEOUT = 10

    def __init__(self):
        self.bridge = IBKRSignalBridge()
        self._app: IBKRApp | None = None
        self._thread: threading.Thread | None = None
        self._connected = False
        self._con_id_cache: dict[str, int] = {}
        # 期权链缓存: SYM -> (交易日 YYYYMMDD, expirations, strikes)。
        # 见 request_option_chain —— 省掉来回切标的时每次 10 秒的 reqSecDefOptParams。
        self._chain_cache: dict[str, tuple] = {}
        # Per-expiry option trading class, learned from request_option_chain.
        # Key "SYMBOL|EXPIRY" -> tradingClass (e.g. "SPX" vs "SPXW").
        # Index options (SPX/XSP/...) are ambiguous without it: reqMktData
        # returns error 200 and the price silently never arrives.
        self._opt_trading_class: dict[str, str] = {}
        # Per-expiry **real** strike list, keyed "SYMBOL|EXPIRY". Resolved from the
        # matching option class (not the global union) so the chain never builds
        # phantom (expiry, strike, class) contracts → error 200 / 空表.
        self._opt_strikes_by_expiry: dict[str, list] = {}
        self._mode = TradingMode.PAPER

        # Order & position tracking
        self._orders: dict[int, OrderInfo] = {}
        self._positions: dict[str, PositionInfo] = {}  # key -> PositionInfo

        # Real IBKR positions streamed from reqPositions() — the authoritative
        # account holdings, including option positions opened in a PRIOR session
        # (before this process started). _positions above only tracks fills seen
        # this session, so without this the 平仓 button can't close a position
        # left over from before a crash/restart. Keyed by position_key.
        self._ibkr_positions: dict[str, PortfolioPosition] = {}
        # 初始持仓快照 (reqPositions → positionEnd) 是否已到 — 条件单触发前
        # 校验持仓以此为前提, 避免刚连接、快照未到时把有仓误判为空仓
        self._positions_synced = False

        # Order IDs the user asked to cancel (suppress reject popup for these)
        self._user_cancel_ids: set[int] = set()

        # Per-position PnL subscriptions: conId -> reqId
        self._pnl_single_by_conid: dict[int, int] = {}

        # API client id (default from config; stock trader overrides).
        # _base_client_id 为"标准 id", 每次连接都从它起算重试, 避免 326 退避后
        # 永久漂移到 20/30...; _client_id 仅记录当前实际用的 id。
        self._base_client_id = IBKR_CLIENT_ID
        self._client_id = IBKR_CLIENT_ID

        # Heartbeat timer (checks reader thread + tick timeout)
        self._heartbeat_timer: QTimer | None = None
        self._tick_timeout_warned = False

        # 心跳定时器必须在 GUI 线程启动 (QTimer 线程约束): connect() 跑在后台
        # 线程, 原先在那里直接建 QTimer → 定时器从未真正运行 (reader 线程死亡/
        # 行情超时监控实际失效), 且每次连接报 QObject::startTimer 警告。
        # 改为 bridge.connected 信号驱动 → 槽在 GUI 线程执行。
        self.bridge.connected.connect(self._start_heartbeat)

        # Connect internal signals
        self.bridge.order_status_changed.connect(self._on_order_status)
        self.bridge.execution_received.connect(self._on_execution)
        self.bridge.open_order_received.connect(self._on_open_order)
        self.bridge.error_received.connect(self._on_order_error)
        self.bridge.portfolio_position_received.connect(self._on_portfolio_position)
        self.bridge.portfolio_positions_end.connect(self._on_positions_synced)

    def _on_positions_synced(self):
        self._positions_synced = True

    @property
    def positions_synced(self) -> bool:
        """初始持仓快照是否已收完 (positionEnd 已到)。"""
        return self._positions_synced

    @property
    def is_connected(self) -> bool:
        return self._connected

    @property
    def mode(self) -> TradingMode:
        return self._mode

    @mode.setter
    def mode(self, value: TradingMode):
        self._mode = value

    @property
    def positions(self) -> dict[str, PositionInfo]:
        """真实引擎下**故意返回空** —— 持仓一律走 IBKR API。

        持仓面板改由 `portfolio_position_received`(reqPositions)+ `reqPnLSingle`
        的真实盈亏渲染(不依赖逐合约行情订阅,Gateway 快照模式下也准);本地按成交
        累加曾导致幻影持仓/数目对不上,已彻底弃用。点价梯持仓摘要用 `get_position()`
        (它对当前合约有实时行情)。模拟引擎 `PaperEngine.positions` 仍用本地撮合持仓。
        """
        return {}

    def get_position(self, option_key: str) -> "PositionInfo | None":
        """按合约 key 取单个持仓 (供点价梯摘要), **以 IBKR API 为准**。
        从 `_ibkr_positions`(reqPositions 真实持仓)构造 PositionInfo;
        `current_price` 由调用方用实时行情填入后再读 net_pnl。"""
        pp = self._ibkr_positions.get(option_key)
        if not pp or pp.sec_type != "OPT" or pp.quantity <= 0:
            return None
        opt = OptionInfo(
            symbol=pp.symbol, expiry=pp.expiry,
            strike=pp.strike, right=pp.right, con_id=pp.con_id,
        )
        return PositionInfo(
            option=opt, quantity=int(pp.quantity), avg_price=pp.avg_price,
        )

    def get_position_commission(self, option_key: str) -> float:
        """该合约**今日**累计已付手续费 (IBKR commissionReport 实际值, 按 execId 去重)。

        供点价梯/持仓面板显示。旧实现读 `get_position().total_commission`, 但真实模式
        的 PositionInfo 由 reqPositions 构造、从不填佣金 → 恒显示 0 (bug)。
        注意口径: 只含**今日**成交的佣金 (IBKR 仅回放当日 execution); 隔夜仓的开仓
        佣金已计入 IBKR avgCost, 不重复出现在这里。"""
        pp = self._ibkr_positions.get(option_key)
        if not pp or not self._app or not pp.con_id:
            return 0.0
        app = self._app
        try:
            # list() 拷贝: reader 线程可能并发写入这两个 dict
            return sum(
                comm for eid, comm in list(app._comm_by_execid.items())
                if app._exec_conid.get(eid) == pp.con_id
            )
        except RuntimeError:
            return 0.0

    def get_fx_rate(self, currency: str) -> float:
        """货币 → USD 汇率。USD 恒 1.0; 汇率未到时返回 0.0 (未知)。

        持仓面板用它把非美元标的的市值/盈亏/价格折算成 USD。
        """
        return self.to_usd_rate(currency)

    def to_usd_rate(self, currency: str) -> float:
        """把 `currency` 计价的金额换成 USD 要乘的系数; 拿不到汇率返回 0.0。

        `$LEDGER:ALL` 的 ExchangeRate 是「该币 → **账户基础货币**」, **不是**
        「该币 → USD」。因此非 USD 基础货币账户不能直接把 ExchangeRate 当作
        →USD 汇率使用。

        两边同时除以 USD 那条汇率, 基础货币就被约掉, 与基础货币是什么无关:

            currency→USD = (currency→base) / (USD→base)
        """
        if not currency or currency == "USD":
            return 1.0
        if not self._app:
            return 0.0
        try:
            rates = self._app._fx_rates
            usd_to_base = rates.get("USD", 0.0)
            if not usd_to_base:
                return 0.0   # ledger 还没到, 老实说不知道, 别瞎折
            if currency == getattr(self._app, "_base_currency", ""):
                cur_to_base = 1.0
            else:
                cur_to_base = rates.get(currency, 0.0)
            if not cur_to_base:
                return 0.0
            return cur_to_base / usd_to_base
        except (AttributeError, RuntimeError, ZeroDivisionError):
            return 0.0

    def get_position_commission_currency(self, option_key: str) -> str:
        """该合约今日佣金的计价货币 (如 "USD"/"TWD")。混合或未知时返回 ""。

        持仓面板据此判断「费$X」里的 X 是否需要按持仓汇率折成 USD ——
        台股佣金按 TWD 计, 不折算会把 40 TWD 显示成 $40。
        """
        pp = self._ibkr_positions.get(option_key)
        if not pp or not self._app or not pp.con_id:
            return ""
        app = self._app
        try:
            ccys = {
                app._comm_ccy_by_execid.get(eid, "")
                for eid, cid in list(app._exec_conid.items())
                if cid == pp.con_id
            }
            ccys.discard("")
            return next(iter(ccys)) if len(ccys) == 1 else ""
        except RuntimeError:
            return ""

    @property
    def orders(self) -> dict[int, OrderInfo]:
        return self._orders

    # ── Connection ────────────────────────────────────────────────────

    def connect(self, mode: TradingMode = TradingMode.PAPER,
                client_id: int | None = None) -> bool:
        """Connect to TWS/Gateway. Returns True on success.
        client_id: override config IBKR_CLIENT_ID (e.g. stock trader uses 11).
        """
        self._mode = mode
        if client_id is not None:
            self._base_client_id = client_id
            self._client_id = client_id
        # 实盘 → live 端口; 两种模拟 (本地 / IBKR模拟盘) → paper 端口。
        # USE_GATEWAY=1 时走 IB Gateway (4002/4001), 否则 TWS (7496/7497)。
        if USE_GATEWAY:
            port = IBKR_GW_LIVE_PORT if mode.is_live_port else IBKR_GW_PAPER_PORT
        else:
            port = IBKR_LIVE_PORT if mode.is_live_port else IBKR_PAPER_PORT

        # Retry with fallback ids on error 326 (zombie connection / double
        # launch). +10 steps avoid colliding with the other GUI's base id.
        base_id = self._base_client_id  # 始终从标准 id 起算, 不被上次 326 退避带偏
        for attempt in range(5):
            cid = base_id + attempt * 10
            self._app = IBKRApp(self.bridge)
            try:
                self._app.connect(IBKR_HOST, port, cid)
            except Exception as e:
                self.bridge.error_received.emit(-1, -1, f"Connection failed: {e}")
                return False

            self._thread = threading.Thread(
                target=self._run_wrapper, args=(self._app,),
                daemon=True, name="ibkr-reader",
            )
            self._thread.start()

            if not self._app.connected_event.wait(timeout=self.CONNECT_TIMEOUT):
                # 套接字连上但 nextValidId 迟迟不回 —— 典型是 Gateway/TWS 内部卡死
                # (JTS 死锁) 或正忙加载持仓。客户端无法自愈, 必须重启 Gateway。
                try:
                    self._app.disconnect()
                except Exception:
                    pass
                self._app = None
                self._connected = False
                self.bridge.error_received.emit(
                    -1, -3,
                    f"连接超时 (端口 {port}): Gateway/TWS 无响应 —— 可能已卡死(JTS死锁)"
                    f"或正忙。请到任务管理器结束 Gateway 进程并重新登录后再连。",
                )
                return False

            if self._app.client_id_in_use:
                print(f"[CONNECT] clientId={cid} occupied, "
                      f"retrying with {cid + 10}", flush=True)
                self.bridge.error_received.emit(
                    -1, 326, f"clientId={cid} 被占用, 尝试 {cid + 10}..."
                )
                # 先把被丢弃的连接从 self._app 摘掉, 这样它的 reader 线程退出时
                # (_run_wrapper) 判定 `app is self._app` 为 False, 不会误发
                # disconnected —— 否则该信号会晚于新连接的 connected 到达 GUI,
                # 表现为"刚连上又断开"。
                discarded = self._app
                self._app = None
                try:
                    discarded.disconnect()
                except Exception:
                    pass
                time.sleep(1.5)  # 给 Gateway 更多时间释放该 clientId
                continue

            self._client_id = cid
            break
        else:
            self.bridge.error_received.emit(
                -1, 326,
                "clientId 全部被占用 — 多半是上次连接未释放(Gateway 可能已卡死/未关干净)。"
                "请结束多余的程序实例, 或直接重启 IB Gateway 后重连。",
            )
            self._connected = False
            return False

        self._connected = True
        self._app.reqMarketDataType(MARKET_DATA_TYPE)

        # Fetch existing open orders (from previous session or TWS-placed)
        self._app.reqOpenOrders()

        # 拉当日已成交 → 触发 commissionReport, 补齐今日累计手续费 (供今日盈亏扣费),
        # 这样重启后也能算上重启前的手续费, 不只本会话。
        try:
            self._app.reqExecutions(self._app.next_req_id(), ExecutionFilter())
        except Exception:
            pass

        # 拉当日**已完成委托**(成交/撤销) → 重启后委托面板也能看到历史委托, 不只当前挂单。
        # 注: IBKR 仅提供「上次服务器重置以来」的完成单 (约当日), 无法取更久历史。
        # apiOnly=False 含 TWS 手动单, 一并显示。
        try:
            self._app.reqCompletedOrders(False)
        except Exception:
            pass

        # 心跳监控由 bridge.connected 信号在 GUI 线程启动 (本方法在后台线程跑,
        # 不能直接建/启 QTimer)

        return True

    def _run_wrapper(self, app: IBKRApp):
        """Wrapper around app.run() that catches reader thread crashes."""
        try:
            app.run()
        except Exception as e:
            print(f"[FATAL] Reader thread crashed: {e}", flush=True)
        finally:
            # Notify GUI only if this app is still the active connection
            # (discarded retry attempts exit silently)
            if app is self._app:
                self._connected = False
                try:
                    self.bridge.disconnected.emit()
                except RuntimeError:
                    pass  # Qt object may already be destroyed

    def _start_heartbeat(self):
        """Start a 10-second timer that checks reader thread + tick freshness."""
        self._stop_heartbeat()
        self._tick_timeout_warned = False
        self._heartbeat_timer = QTimer()
        self._heartbeat_timer.timeout.connect(self._on_heartbeat)
        self._heartbeat_timer.start(10_000)

    def _stop_heartbeat(self):
        """Stop the heartbeat timer (可从任意线程调用)。"""
        timer = self._heartbeat_timer
        self._heartbeat_timer = None
        if timer is None:
            return
        if QThread.currentThread() is timer.thread():
            timer.stop()
        else:
            # QTimer 不能跨线程 stop (killTimer 警告且无效) → 队列到其所属线程
            QMetaObject.invokeMethod(timer, "stop", Qt.QueuedConnection)
        timer.deleteLater()

    def _on_heartbeat(self):
        """Periodic check: reader thread alive? tick data still flowing?"""
        # 1. Check if the reader thread is still alive
        if self._thread is not None and not self._thread.is_alive():
            print("[HEARTBEAT] Reader thread is dead!", flush=True)
            self._connected = False
            self._stop_heartbeat()
            self.bridge.disconnected.emit()
            return

        # 2. Check tick data freshness (only warn if we have active subscriptions)
        if self._app and self._app._active_mkt_data_reqs:
            last_tick = self._app._last_tick_time
            if last_tick > 0:
                elapsed = time.time() - last_tick
                if elapsed > 30:
                    if not self._tick_timeout_warned:
                        self._tick_timeout_warned = True
                        print(f"[HEARTBEAT] No tick data for {elapsed:.0f}s", flush=True)
                        self.bridge.error_received.emit(
                            -1, -2, f"行情数据超时 ({elapsed:.0f}秒无更新)"
                        )
                else:
                    # Tick resumed — clear warning
                    self._tick_timeout_warned = False

    def disconnect(self):
        """Disconnect from TWS."""
        self._stop_heartbeat()
        self.cancel_account_summary()
        self.cancel_currency_balances()
        self.cancel_pnl()
        for con_id in list(self._pnl_single_by_conid):
            self.cancel_pnl_single(con_id)
        self.cancel_positions()
        self.unsubscribe_market_depth()

        if self._app:
            # Cancel all market data subscriptions
            for req_id in list(self._app._active_mkt_data_reqs):
                try:
                    self._app.cancelMktData(req_id)
                except Exception:
                    pass
            self._app._active_mkt_data_reqs.clear()

            try:
                self._app.disconnect()
            except Exception:
                pass
        self._connected = False
        self._positions_synced = False  # 重连后等新快照 (positionEnd) 再信任持仓
        self.bridge.disconnected.emit()

    def reconnect(self, mode: TradingMode) -> bool:
        """Disconnect, clear state, and reconnect to a different port."""
        self.disconnect()
        time.sleep(3.0)  # 给 Gateway/TWS 充分时间释放旧 clientId (热切换 326 主因)
        self._orders.clear()
        self._positions.clear()
        self._ibkr_positions.clear()
        return self.connect(mode)

    # ── Symbol Search ──────────────────────────────────────────────────

    def search_symbols(self, pattern: str):
        """Search for matching symbols via IBKR API (non-blocking)."""
        if not self._app or not self._connected:
            return
        req_id = self._app.next_req_id()
        self._app.reqMatchingSymbols(req_id, pattern)

    # ── Contract Resolution ───────────────────────────────────────────

    def get_con_id(self, symbol: str) -> int:
        """Get conId for a stock/index symbol (cached, blocking)."""
        if symbol in self._con_id_cache:
            return self._con_id_cache[symbol]

        contract = self._make_underlying_contract(symbol)
        req_id = self._app.next_req_id()
        self._app._contract_data[req_id] = {
            "details": [], "event": threading.Event(), "error": None,
        }
        self._app.reqContractDetails(req_id, contract)

        state = self._app._contract_data[req_id]
        if not state["event"].wait(timeout=10):
            raise RuntimeError(f"Timeout getting contract details for {symbol}")
        if state["error"]:
            code, msg = state["error"]
            raise RuntimeError(f"Contract error {symbol}: code={code} {msg}")
        if not state["details"]:
            raise RuntimeError(f"No contract details for {symbol}")

        con_id = state["details"][0].contract.conId
        self._con_id_cache[symbol] = con_id
        self._app._contract_data.pop(req_id, None)
        return con_id

    # ── Option Chain Discovery ────────────────────────────────────────

    def request_option_chain(self, symbol: str,
                             force: bool = False) -> tuple[list[str], list[float]]:
        """Get expirations and strikes for a symbol (blocking).
        Returns (expirations, strikes).

        **按标的+交易日缓存**: 到期日/行权价一天之内基本不变, 但原来每次切标的都要
        重跑一遍 reqSecDefOptParams (最长阻塞 10 秒)。实测 2026-08-11 单日日志里
        AAPL 拉了 10 次、TSLA 11 次 —— 来回切标的时那几秒等待全是白等。
        缓存按日失效 (跨日到期日会滚), `force=True` 可手动刷新。

        注: 命中缓存时 `_opt_strikes_by_expiry` / `_opt_trading_class` 这两张
        按到期日的表已经是上次填好的 (它们本来就跨调用常驻), 直接返回不会丢东西。
        """
        sym_key = symbol.upper()
        today = datetime.now().strftime("%Y%m%d")
        if not force:
            hit = self._chain_cache.get(sym_key)
            if hit and hit[0] == today:
                return hit[1], hit[2]

        con_id = self.get_con_id(symbol)

        req_id = self._app.next_req_id()
        self._app._opt_data[req_id] = {
            "params": [], "event": threading.Event(), "error": None,
        }
        sec_type = "IND" if symbol.upper() in INDEX_SYMBOLS else "STK"
        self._app.reqSecDefOptParams(req_id, symbol, "", sec_type, con_id)

        if not self._app._opt_data[req_id]["event"].wait(timeout=10):
            raise RuntimeError(f"Timeout getting option params for {symbol}")

        params_list = self._app._opt_data[req_id]["params"]
        self._app._opt_data.pop(req_id, None)

        if not params_list:
            raise RuntimeError(f"No option params for {symbol}")

        # Merge expirations and strikes (prefer SMART exchange)
        all_expirations: set[str] = set()
        all_strikes: set[float] = set()
        for p in params_list:
            if p["exchange"] == "SMART":
                all_expirations.update(p["expirations"])
                all_strikes.update(p["strikes"])
        if not all_expirations:
            for p in params_list:
                all_expirations.update(p["expirations"])
                all_strikes.update(p["strikes"])

        # Resolve **per-expiry** strikes + tradingClass from the matching option
        # class — NOT the global union. reqSecDefOptParams returns one entry per
        # (exchange, tradingClass) with that class's own expirations + strikes.
        # The old code unioned everything, so an expiry from class A could be
        # paired with strikes/class from class B → a contract that doesn't exist
        # → error 200 → whole expiry tab empty (e.g. NVDA 07/10). Here each expiry
        # takes the strikes of the class that actually lists it. On overlap (e.g.
        # SPX 3rd Friday lists both SPX & SPXW) prefer the "...W" class (intraday-
        # quoted PM-settled); otherwise the class with the most strikes.
        sym_up = symbol.upper()
        smart_params = [p for p in params_list if p["exchange"] == "SMART"]
        use_params = smart_params if smart_params else params_list

        exp_entries: dict[str, list] = {}  # exp -> [(tradingClass, strikes_list), ...]
        for p in use_params:
            tclass = p.get("tradingClass") or ""
            strikes_p = p["strikes"]
            for exp in p["expirations"]:
                exp_entries.setdefault(exp, []).append((tclass, strikes_p))

        for exp, entries in exp_entries.items():
            w_entries = [e for e in entries if e[0].upper().endswith("W")]
            tclass, strikes_p = max(w_entries or entries, key=lambda e: len(e[1]))
            map_key = f"{sym_up}|{exp}"
            self._opt_strikes_by_expiry[map_key] = sorted(strikes_p)
            if tclass:
                self._opt_trading_class[map_key] = tclass

        expirations = sorted(all_expirations)
        strikes = sorted(all_strikes)
        self._chain_cache[sym_key] = (today, expirations, strikes)
        return expirations, strikes

    def option_strikes_for_expiry(self, symbol: str, expiry: str) -> list:
        """该到期日**真实存在**的行权价 (来自实际列出它的 option class)。

        用于按到期日建期权链, 避免用全链 union 行权价拼出不存在的合约
        (error 200 / 空表)。未加载过该标的链时返回 []，调用方应回退到 union。

        注: 来自 reqSecDefOptParams, 行权价是该 class **跨所有到期日的并集**, 故对
        单一 class 的标的 (如 NVDA) 各到期日相同 —— 仍可能含某到期日不存在的行权价。
        要精确到单个到期日, 用 `request_option_strikes_live` (reqContractDetails)。"""
        return self._opt_strikes_by_expiry.get(f"{symbol.upper()}|{expiry}", [])

    def request_option_strikes_live(self, symbol: str, expiry: str,
                                    timeout: float = 8.0) -> tuple:
        """用 reqContractDetails 取 (symbol, expiry) **该到期日真实存在**的期权合约,
        返回 `(strikes_sorted, trading_class, ok)`。

        这是按到期日精确取行权价的权威途径 —— 解决 reqSecDefOptParams 的并集行权价
        在某些到期日不存在 → reqMktData/下单 error 200 → 整张到期日表空 (如 NVDA 07/10)。
        阻塞 (内部 Event.wait), **调用方须放后台线程**。
          - ok=True  : 请求成功 (strikes 可能为空 = 该到期日确无 SMART 合约);
          - ok=False : 超时/错误 → 调用方可回退到并集行权价。
        同时把权威 tradingClass / 行权价写回缓存, 供下单与点价梯复用。"""
        if not self._app or not self._connected:
            return [], "", False
        c = Contract()
        c.symbol = symbol
        c.secType = "OPT"
        c.exchange = "SMART"
        c.currency = "USD"
        c.lastTradeDateOrContractMonth = expiry
        req_id = self._app.next_req_id()
        self._app._contract_data[req_id] = {
            "details": [], "event": threading.Event(), "error": None,
        }
        try:
            self._app.reqContractDetails(req_id, c)
        except Exception:
            self._app._contract_data.pop(req_id, None)
            return [], "", False
        ok = self._app._contract_data[req_id]["event"].wait(timeout)
        state = self._app._contract_data.pop(req_id, None) or {}
        if not ok:
            return [], "", False
        details = state.get("details") or []
        # 按 tradingClass 分组收集行权价 (优先标准乘数 100; 全非标则不过滤)
        by_class: dict[str, set] = {}
        for cd in details:
            con = cd.contract
            mult = str(getattr(con, "multiplier", "") or "")
            if mult not in ("100", ""):
                continue
            by_class.setdefault(con.tradingClass or "", set()).add(float(con.strike))
        if not by_class:
            for cd in details:
                con = cd.contract
                by_class.setdefault(con.tradingClass or "", set()).add(float(con.strike))
        if not by_class:
            return [], "", True  # 成功但无合约 → 该到期日确实没有
        # 选 class: 优先 "...W" (周/日, 盘中报价), 否则合约数最多者
        classes = list(by_class)
        w = [cl for cl in classes if cl.upper().endswith("W")]
        best = max(w or classes, key=lambda cl: len(by_class[cl]))
        strikes = sorted(by_class[best])
        map_key = f"{symbol.upper()}|{expiry}"
        if best:
            self._opt_trading_class[map_key] = best
        self._opt_strikes_by_expiry[map_key] = strikes
        return strikes, best, True

    def _trading_class_for(self, symbol: str, expiry: str) -> str:
        """Best-known option tradingClass for (symbol, expiry).
        Empty string lets SMART resolve it (fine for single-class names
        like SPY). Falls back to a known weekly class for index symbols
        when the chain hasn't been loaded (e.g. direct entry in the ladder)."""
        sym_up = symbol.upper()
        tclass = self._opt_trading_class.get(f"{sym_up}|{expiry}")
        if tclass:
            return tclass
        if sym_up in INDEX_SYMBOLS:
            return _INDEX_WEEKLY_CLASS.get(sym_up, "")
        return ""

    # ── Market Data Subscription ──────────────────────────────────────

    def _quote_contract(self, option: OptionInfo) -> Contract:
        """按伪合约类型返回行情/下单用的 IBKR Contract。
        right='STK' → 标的, 'FUT' → 期货, 其余 ('C'/'P') → 期权。"""
        if option.right == "STK":
            return self._make_underlying_contract(option.symbol)
        if option.right == "FUT":
            return self._make_futures_contract(option.symbol, option.expiry)
        return self._make_option_contract(
            option.symbol, option.expiry, option.strike, option.right,
            self._trading_class_for(option.symbol, option.expiry),
        )

    def subscribe_option_tick(self, option: OptionInfo) -> int:
        """Subscribe to streaming tick data for an option (or a stock/futures
        pseudo-contract with right='STK'/'FUT'). Returns reqId."""
        contract = self._quote_contract(option)
        req_id = self._app.next_req_id()
        key = option.to_ibkr_key()
        self._app._tick_req_to_key[req_id] = key
        self._app._active_mkt_data_reqs.add(req_id)
        # generic tick 106 = Option Implied Volatility; 确保期权计算器拿到 IV。
        # 模型 greeks 与标的价 (tickOptionComputation tickType 13) 随期权订阅默认下发。
        # 正股/期货伪合约无 IV 概念, 保持空 generic tick。
        generic_ticks = "106" if option.right in ("C", "P") else ""
        self._app._tick_req_contract[req_id] = (contract, generic_ticks)
        self._app.reqMktData(req_id, contract, generic_ticks, False, False, [])
        return req_id

    def snapshot_option_tick(self, option: OptionInfo) -> int:
        """One-shot 快照报价 (snapshot=True): IBKR 推一次 bid/ask/last/vol 后
        自动取消, **不占常驻行情线**。数据落到 _tick_data[key], GUI 轮询显示。
        适合 Gateway 下行情线紧张时按需刷新整条期权链 (用美股快照数据包)。
        返回 reqId。"""
        contract = self._quote_contract(option)
        req_id = self._app.next_req_id()
        key = option.to_ibkr_key()
        self._app._tick_req_to_key[req_id] = key
        self._app._active_mkt_data_reqs.add(req_id)
        # snapshot=True 时 IBKR 不接受 generic tick 列表, 必须留空; 快照含 vol。
        self._app.reqMktData(req_id, contract, "", True, False, [])
        return req_id

    def subscribe_stock_tick(self, symbol: str) -> int:
        """Subscribe to streaming tick data for a stock/ETF. Returns reqId.
        Tick data lands under key '__stock__<symbol>'."""
        contract = self._make_underlying_contract(symbol)
        req_id = self._app.next_req_id()
        key = f"__stock__{symbol}"
        self._app._tick_req_to_key[req_id] = key
        self._app._active_mkt_data_reqs.add(req_id)
        self._app._tick_req_contract[req_id] = (contract, "")
        self._app.reqMktData(req_id, contract, "", False, False, [])
        return req_id

    def subscribe_watch_tick(self, symbol: str) -> int:
        """订阅任意**监控标的**的行情, 数据统一落 `__stock__<SYM>` 键。

        - 股票/ETF/指数 (SPX/NDX 等走 IND): 直接 subscribe_stock_tick;
        - 期货根代码 (ES/NQ 等在 FUTURES_SPECS 中): 立即占用 reqId 返回,
          后台线程解析近月合约后再真正 reqMktData (解析是阻塞的
          reqContractDetails, 不能卡 GUI 线程)。首个 tick 到达前 get_tick
          返回 0, 调用方 (条件单巡检/播种) 对 0 价均已跳过。
        """
        sym = symbol.upper()
        if sym not in FUTURES_SPECS:
            return self.subscribe_stock_tick(sym)
        req_id = self._app.next_req_id()
        key = f"__stock__{sym}"
        self._app._tick_req_to_key[req_id] = key
        self._app._active_mkt_data_reqs.add(req_id)

        def _sub():
            try:
                cons = self.resolve_futures_contracts(sym, max_count=1)
                if not cons:
                    print(f"[WATCH-SUB] {sym} 无可用期货合约", flush=True)
                    return
                c = self._make_futures_contract(sym, cons[0]["expiry"])
                # 解析期间已被退订则放弃
                if req_id not in self._app._active_mkt_data_reqs:
                    return
                self._app._tick_req_contract[req_id] = (c, "")
                self._app.reqMktData(req_id, c, "", False, False, [])
            except Exception as e:
                print(f"[WATCH-SUB] {sym} 期货行情订阅失败: {e}", flush=True)

        threading.Thread(target=_sub, daemon=True,
                         name=f"watch-sub-{sym}").start()
        return req_id

    def unsubscribe_tick(self, req_id: int):
        """Cancel a tick data subscription."""
        key = self._app._tick_req_to_key.pop(req_id, None)
        self._app._tick_req_contract.pop(req_id, None)
        self._app._tick_req_last.pop(req_id, None)
        self._app._active_mkt_data_reqs.discard(req_id)
        if key is not None:
            self._app._tick_retry.pop(key, None)
        try:
            self._app.cancelMktData(req_id)
        except Exception:
            pass

    def get_tick(self, key: str) -> dict:
        """Get latest tick data for a key."""
        if self._app:
            return self._app._tick_data.get(key, {"bid": 0.0, "ask": 0.0, "last": 0.0})
        return {"bid": 0.0, "ask": 0.0, "last": 0.0}

    def tick_age(self, key: str) -> float:
        """该**合约**最近一次 tick 距今秒数 (谁推的都算); 从无数据返回 inf。"""
        if not self._app:
            return float("inf")
        t = self._app._tick_data.get(key, {}).get("t", 0.0)
        return float("inf") if not t else max(0.0, time.time() - t)

    def req_tick_age(self, req_id: int) -> float:
        """该**行情线**最近一次 tick 距今秒数; 从无数据返回 inf。

        判断"我订的这条线是不是死了"必须用它, 而不是 tick_age(key): 期权链的
        一次性快照写的是同一个 key, 会把合约级时间戳一直刷新, 掩盖掉常驻订阅
        早已被拒/被掐的事实。
        """
        if not self._app or req_id is None:
            return float("inf")
        t = self._app._tick_req_last.get(req_id, 0.0)
        return float("inf") if not t else max(0.0, time.time() - t)

    def market_data_age(self) -> float:
        """**任意**合约最近一次 tick 距今秒数。

        与 `tick_age(key)` 对比即可区分两种"不动": 全局也不动 = 整体断流/收盘
        (交给心跳和重连处理); 全局在动而某个 key 不动 = 那条行情线死了, 该重订。
        """
        if not self._app or not self._app._last_tick_time:
            return float("inf")
        return max(0.0, time.time() - self._app._last_tick_time)

    def get_trade_stats(self) -> dict:
        """已平仓交易统计快照 (笔数/胜率/盈亏比)。未连接返回空统计。"""
        if self._app:
            return self._app._trade_stats.snapshot()
        return TradeStats().snapshot()

    # ── Historical Data ─────────────────────────────────────────────

    def request_historical_data(
        self, symbol: str, bar_size: str, duration: str,
        keep_up_to_date: bool = False, timeout: float = 30,
        end_date_time: str = "",
    ) -> tuple[int, list[dict]]:
        """Request historical bars (blocking). Returns (reqId, bars).
        If keep_up_to_date is True, caller must later call cancel_historical_data(reqId).
        end_date_time: "" = now, or "yyyyMMdd HH:mm:ss" for earlier data.
        """
        if not self._app or not self._connected:
            raise RuntimeError("Not connected")

        contract = self._make_underlying_contract(symbol)
        req_id = self._app.next_req_id()
        self._app._hist_data[req_id] = {
            "bars": [], "event": threading.Event(), "error": None,
        }

        self._app.reqHistoricalData(
            reqId=req_id,
            contract=contract,
            endDateTime=end_date_time,
            durationStr=duration,
            barSizeSetting=bar_size,
            whatToShow="TRADES",
            useRTH=0,               # include extended hours
            formatDate=1,           # yyyyMMdd HH:mm:ss format
            keepUpToDate=keep_up_to_date,
            chartOptions=[],
        )

        state = self._app._hist_data[req_id]
        if not state["event"].wait(timeout=timeout):
            # Timeout — cancel and clean up
            try:
                self._app.cancelHistoricalData(req_id)
            except Exception:
                pass
            self._app._hist_data.pop(req_id, None)
            raise RuntimeError(f"Timeout requesting historical data for {symbol}")

        if state["error"]:
            code, msg = state["error"]
            self._app._hist_data.pop(req_id, None)
            raise RuntimeError(f"Historical data error {symbol}: code={code} {msg}")

        bars = state["bars"]
        if not keep_up_to_date:
            self._app._hist_data.pop(req_id, None)
        return req_id, bars

    def cancel_historical_data(self, req_id: int):
        """Cancel a keepUpToDate historical data subscription."""
        if self._app:
            try:
                self._app.cancelHistoricalData(req_id)
            except Exception:
                pass
            self._app._hist_data.pop(req_id, None)

    def request_es_momentum_bars(self, duration: str = "7200 S",
                                 bar_size: str = "1 min",
                                 timeout: float = 15.0) -> list[float]:
        """ES 连续期货 (CONTFUT@CME) 的 close 序列, 供动量翻转监测 (默认近 2 小时 1 分钟)。

        ES 是期货 → 必须用 CONTFUT 合约 (不能走 STK 的 `_make_underlying_contract`)。
        阻塞 (内部 Event.wait), **调用方须放后台线程**。失败/无权限返回 []。"""
        if not self._app or not self._connected:
            return []
        spec = FUTURES_SPECS.get("ES")
        exchange = spec[0] if spec else "CME"
        c = Contract()
        c.symbol = "ES"
        c.secType = "CONTFUT"
        c.exchange = exchange
        c.currency = "USD"
        req_id = self._app.next_req_id()
        self._app._hist_data[req_id] = {
            "bars": [], "event": threading.Event(), "error": None,
        }
        try:
            self._app.reqHistoricalData(
                reqId=req_id, contract=c, endDateTime="", durationStr=duration,
                barSizeSetting=bar_size, whatToShow="TRADES", useRTH=0,
                formatDate=1, keepUpToDate=False, chartOptions=[],
            )
        except Exception:
            self._app._hist_data.pop(req_id, None)
            return []
        ok = self._app._hist_data[req_id]["event"].wait(timeout)
        state = self._app._hist_data.pop(req_id, None) or {}
        if not ok or state.get("error"):
            return []
        return [b["close"] for b in state.get("bars", [])
                if b.get("close", 0)]

    def request_option_historical_data(
        self, symbol: str, expiry: str, strike: float, right: str,
        bar_size: str, duration: str, what_to_show: str = "TRADES",
        timeout: float = 30, end_date_time: str = "",
    ) -> list[dict]:
        """期权合约历史 K 线 (阻塞)。返回 bars: [{date, open, high, low, close, volume, ...}]。
        date 为 epoch 秒字符串 (formatDate=2), 便于多腿按时间戳对齐与绘图。
        what_to_show: TRADES / MIDPOINT / BID / ASK (期权流动性差时 MIDPOINT 更连续)。
        组合分析器为每条腿调用本方法, 再把各腿 close 按时间合成组合价。"""
        if not self._app or not self._connected:
            raise RuntimeError("Not connected")

        contract = self._make_option_contract(
            symbol, expiry, strike, right,
            self._trading_class_for(symbol, expiry),
        )
        req_id = self._app.next_req_id()
        self._app._hist_data[req_id] = {
            "bars": [], "event": threading.Event(), "error": None,
        }
        self._app.reqHistoricalData(
            reqId=req_id,
            contract=contract,
            endDateTime=end_date_time,
            durationStr=duration,
            barSizeSetting=bar_size,
            whatToShow=what_to_show,
            useRTH=0,
            formatDate=2,           # epoch seconds — easy cross-leg alignment
            keepUpToDate=False,
            chartOptions=[],
        )

        state = self._app._hist_data[req_id]
        label = f"{symbol} {expiry} {strike:g}{right}"
        if not state["event"].wait(timeout=timeout):
            try:
                self._app.cancelHistoricalData(req_id)
            except Exception:
                pass
            self._app._hist_data.pop(req_id, None)
            raise RuntimeError(f"Timeout requesting historical data: {label}")

        if state["error"]:
            code, msg = state["error"]
            self._app._hist_data.pop(req_id, None)
            raise RuntimeError(f"Historical data error {label}: code={code} {msg}")

        bars = state["bars"]
        self._app._hist_data.pop(req_id, None)
        return bars

    # ── Account Summary ──────────────────────────────────────────────

    def request_account_summary(self):
        """订阅账户摘要 (流式, 非阻塞)。

        **幂等**: `reqAccountSummary` 是**流式订阅** —— 订一次后 IBKR 会持续推送更新
        (约每 3 分钟或值变化时), 无需重订。`account_bar` 每 3 秒会调用本方法, 原实现
        每次都 `cancel + 重订` → 在 Gateway 上造成严重 churn (EMsgPacer 不停"发请求/发取消"、
        NonAwtClientQueue 任务堆积)。现已订阅 (`_account_summary_req_id` 非空) 则直接返回,
        不再 churn。重连会新建 `IBKRApp` → req_id 自动复位, 故会重新订阅。
        """
        if not self._app or not self._connected:
            return
        if self._app._account_summary_req_id is not None:
            return  # 已订阅, 流式推送中, 不再 churn
        req_id = self._app.next_req_id()
        self._app._account_summary_req_id = req_id
        tags = "NetLiquidation,TotalCashValue,BuyingPower,UnrealizedPnL,RealizedPnL"
        self._app.reqAccountSummary(req_id, "All", tags)

    def resync_account_summary(self):
        """强制 IBKR **立刻**回一份账户摘要快照 (cancel + 重订)。

        `reqAccountSummary` 订上之后 IBKR 只在值变化或约每 3 分钟才推 —— 总资产/
        可用资金/购买力于是长时间纹丝不动。重订会立即触发一次全量推送。
        由 account_bar 按 ACCOUNT_RESYNC_MS 调用 (30 秒一次, 不是每 3 秒, 避免
        当年那种 EMsgPacer / NonAwtClientQueue 堆积)。
        """
        if not self._app or not self._connected:
            return
        self.cancel_account_summary()
        self.request_account_summary()

    def cancel_account_summary(self):
        """Cancel account summary subscription."""
        if self._app and self._app._account_summary_req_id is not None:
            try:
                self._app.cancelAccountSummary(self._app._account_summary_req_id)
            except Exception:
                pass
            self._app._account_summary_req_id = None

    def request_currency_balances(self):
        """Subscribe to per-currency cash balances via "$LEDGER:ALL".

        Streams a CashBalance row per held currency (USD/EUR/...). Separate
        subscription from request_account_summary so the two don't clash.
        """
        if not self._app or not self._connected:
            return
        if self._app._ledger_req_id is not None:
            return  # 已订阅 (流式), 幂等 — 同 request_account_summary, 避免每 3 秒 churn
        req_id = self._app.next_req_id()
        self._app._ledger_req_id = req_id
        self._app.reqAccountSummary(req_id, "All", "$LEDGER:ALL")

    def cancel_currency_balances(self):
        """Cancel the per-currency ledger subscription."""
        if self._app and self._app._ledger_req_id is not None:
            try:
                self._app.cancelAccountSummary(self._app._ledger_req_id)
            except Exception:
                pass
            self._app._ledger_req_id = None

    # ── Positions ─────────────────────────────────────────────────────

    def request_positions(self):
        """Request all portfolio positions (non-blocking)."""
        if not self._app or not self._connected:
            return
        self._app.reqPositions()

    def cancel_positions(self):
        """Cancel positions subscription."""
        if self._app and self._connected:
            try:
                self._app.cancelPositions()
            except Exception:
                pass

    # ── PnL ───────────────────────────────────────────────────────────

    def request_pnl(self, account: str = ""):
        """Subscribe to account daily/unrealized P&L (reqPnL).

        **幂等**: reqPnL 是流式订阅, 订一次后 IBKR 持续推送。`account_summary_end`
        每 3 秒会调一次本方法, 若每次都 cancel+重订, 重订瞬间的初值不稳会让今日/未实现
        盈亏闪成 0。所以已订阅 (`_pnl_req_id` 非空) 时直接返回, 不再 churn。
        """
        if not self._app or not self._connected:
            return
        if self._app._pnl_req_id is not None:
            return  # 已在流式订阅中, 不重复订
        if not account:
            account = self._app._account_name
        if not account:
            print("[PNL] request_pnl skipped: 账户名未就绪", flush=True)
            return  # Need account name first
        req_id = self._app.next_req_id()
        self._app._pnl_req_id = req_id
        print(f"[PNL] reqPnL 订阅: account={account} reqId={req_id}", flush=True)
        self._app.reqPnL(req_id, account, "")

    def cancel_pnl(self):
        """Cancel PnL subscription."""
        if self._app and self._app._pnl_req_id is not None:
            try:
                self._app.cancelPnL(self._app._pnl_req_id)
            except Exception:
                pass
            self._app._pnl_req_id = None

    def request_pnl_single(self, con_id: int) -> int:
        """Subscribe to per-position daily/unrealized PnL (reqPnLSingle).
        Returns reqId, or -1 if not ready (needs account name from summary).
        Does NOT consume a market data line.
        """
        if not self._app or not self._connected:
            return -1
        account = self._app._account_name
        if not account:
            return -1  # Account name not known yet — caller may retry
        if con_id in self._pnl_single_by_conid:
            return self._pnl_single_by_conid[con_id]
        req_id = self._app.next_req_id()
        self._app._pnl_single_reqs[req_id] = con_id
        self._pnl_single_by_conid[con_id] = req_id
        self._app.reqPnLSingle(req_id, account, "", con_id)
        return req_id

    def cancel_pnl_single(self, con_id: int):
        """Cancel a per-position PnL subscription."""
        req_id = self._pnl_single_by_conid.pop(con_id, None)
        if req_id is None or not self._app:
            return
        self._app._pnl_single_reqs.pop(req_id, None)
        try:
            self._app.cancelPnLSingle(req_id)
        except Exception:
            pass

    # ── Market Depth ──────────────────────────────────────────────────

    def subscribe_market_depth(self, option: OptionInfo):
        """Subscribe to market depth for an option.
        Skips silently if depth was already found to be unsupported (error 10092).
        """
        if not self._app or not self._connected:
            return
        # Skip if depth is known to be unsupported for this exchange/data type
        if self._app._depth_not_supported:
            return
        self.unsubscribe_market_depth()
        contract = self._quote_contract(option)
        # 正股走 SMART 聚合盘口; 期权/期货用合约自身交易所盘口 (期货深度按交易所)。
        smart_depth = option.right == "STK"
        req_id = self._app.next_req_id()
        self._app._depth_req_id = req_id
        self._app._depth_is_smart = smart_depth
        try:
            self._app.reqMktDepth(req_id, contract, DEPTH_ROWS, smart_depth, [])
        except Exception:
            pass

    def unsubscribe_market_depth(self):
        """Cancel market depth subscription."""
        if self._app and self._app._depth_req_id is not None:
            try:
                self._app.cancelMktDepth(
                    self._app._depth_req_id, self._app._depth_is_smart
                )
            except Exception:
                pass
            self._app._depth_req_id = None

    # ── Order Management ──────────────────────────────────────────────

    def place_limit_order(self, option: OptionInfo, action: OrderAction,
                          quantity: int, price: float,
                          outside_rth: bool = False) -> int:
        """Place a limit order. Returns orderId.
        outside_rth: allow execution during GTH/Curb sessions (盘外交易).
        """
        contract = self._make_option_contract(
            option.symbol, option.expiry, option.strike, option.right,
            self._trading_class_for(option.symbol, option.expiry),
        )

        order = Order()
        order.action = action.value
        order.orderType = "LMT"
        order.totalQuantity = quantity
        order.lmtPrice = price
        order.eTradeOnly = False
        order.firmQuoteOnly = False
        order.tif = "DAY"
        order.outsideRth = outside_rth

        order_id = self._app.next_order_id()

        # Track order
        commission = max(COMMISSION_PER_CONTRACT * quantity, COMMISSION_MIN)
        order_info = OrderInfo(
            order_id=order_id,
            option=option,
            action=action,
            quantity=quantity,
            limit_price=price,
            order_type=OrderType.LIMIT,
            commission=commission,
        )
        self._orders[order_id] = order_info

        print(f"[ORDER] Placing LMT {action.value} {quantity}x "
              f"{option.display_name} @ {price:.2f} "
              f"outsideRth={outside_rth} orderId={order_id}", flush=True)
        self._app.placeOrder(order_id, contract, order)
        self.bridge.order_status_changed.emit(
            order_id, OrderStatus.PENDING.value, 0, float(quantity), 0
        )
        return order_id

    def place_market_order(self, option: OptionInfo, action: OrderAction,
                           quantity: int,
                           outside_rth: bool = False) -> int:
        """Place a market order. Returns orderId.
        outside_rth: allow execution during GTH/Curb sessions (盘外交易).
        """
        contract = self._make_option_contract(
            option.symbol, option.expiry, option.strike, option.right,
            self._trading_class_for(option.symbol, option.expiry),
        )

        order = Order()
        order.action = action.value
        order.orderType = "MKT"
        order.totalQuantity = quantity
        order.eTradeOnly = False
        order.firmQuoteOnly = False
        order.tif = "DAY"
        order.outsideRth = outside_rth

        order_id = self._app.next_order_id()

        commission = max(COMMISSION_PER_CONTRACT * quantity, COMMISSION_MIN)
        # Use mid price as reference for limit_price field (display only)
        ref_price = option.mid if option.mid > 0 else option.last
        order_info = OrderInfo(
            order_id=order_id,
            option=option,
            action=action,
            quantity=quantity,
            limit_price=ref_price,
            order_type=OrderType.MARKET,
            commission=commission,
        )
        self._orders[order_id] = order_info

        print(f"[ORDER] Placing MKT {action.value} {quantity}x "
              f"{option.display_name} outsideRth={outside_rth} "
              f"orderId={order_id}", flush=True)
        self._app.placeOrder(order_id, contract, order)
        self.bridge.order_status_changed.emit(
            order_id, OrderStatus.PENDING.value, 0, float(quantity), 0
        )
        return order_id

    def place_stock_order(self, symbol: str, action: OrderAction,
                          quantity: int, price: float = 0.0,
                          order_type: OrderType = OrderType.LIMIT,
                          outside_rth: bool = False) -> int:
        """Place a stock/ETF order (LMT or MKT). Returns orderId.
        Tracked with a pseudo OptionInfo (right='STK')."""
        from config import STOCK_COMMISSION_PER_SHARE, STOCK_COMMISSION_MIN

        contract = self._make_underlying_contract(symbol)

        order = Order()
        order.action = action.value
        order.orderType = order_type.value
        order.totalQuantity = quantity
        if order_type == OrderType.LIMIT:
            order.lmtPrice = price
        order.eTradeOnly = False
        order.firmQuoteOnly = False
        order.tif = "DAY"
        order.outsideRth = outside_rth

        order_id = self._app.next_order_id()

        commission = max(
            STOCK_COMMISSION_PER_SHARE * quantity, STOCK_COMMISSION_MIN
        )
        pseudo = OptionInfo(symbol=symbol, expiry="", strike=0.0, right="STK")
        self._orders[order_id] = OrderInfo(
            order_id=order_id,
            option=pseudo,
            action=action,
            quantity=quantity,
            limit_price=price,
            order_type=order_type,
            commission=commission,
        )

        print(f"[STOCK ORDER] {order_type.value} {action.value} {quantity}x "
              f"{symbol} @ {price:.2f} outsideRth={outside_rth} "
              f"orderId={order_id}", flush=True)
        self._app.placeOrder(order_id, contract, order)
        self.bridge.order_status_changed.emit(
            order_id, OrderStatus.PENDING.value, 0, float(quantity), 0
        )
        return order_id

    def place_futures_order(self, symbol: str, expiry: str, action: OrderAction,
                            quantity: int, price: float = 0.0,
                            order_type: OrderType = OrderType.LIMIT,
                            outside_rth: bool = False) -> int:
        """Place a futures order (LMT or MKT). Returns orderId.
        Tracked with a pseudo OptionInfo (right='FUT', expiry=合约月份)."""
        from config import (
            FUTURES_COMMISSION_PER_CONTRACT, FUTURES_COMMISSION_MIN,
        )

        contract = self._make_futures_contract(symbol, expiry)

        order = Order()
        order.action = action.value
        order.orderType = order_type.value
        order.totalQuantity = quantity
        if order_type == OrderType.LIMIT:
            order.lmtPrice = price
        order.eTradeOnly = False
        order.firmQuoteOnly = False
        order.tif = "DAY"
        order.outsideRth = outside_rth

        order_id = self._app.next_order_id()

        commission = max(
            FUTURES_COMMISSION_PER_CONTRACT * quantity, FUTURES_COMMISSION_MIN
        )
        pseudo = OptionInfo(symbol=symbol, expiry=expiry, strike=0.0, right="FUT")
        self._orders[order_id] = OrderInfo(
            order_id=order_id,
            option=pseudo,
            action=action,
            quantity=quantity,
            limit_price=price,
            order_type=order_type,
            commission=commission,
        )

        print(f"[FUT ORDER] {order_type.value} {action.value} {quantity}x "
              f"{symbol} {expiry} @ {price:.2f} outsideRth={outside_rth} "
              f"orderId={order_id}", flush=True)
        self._app.placeOrder(order_id, contract, order)
        self.bridge.order_status_changed.emit(
            order_id, OrderStatus.PENDING.value, 0, float(quantity), 0
        )
        return order_id

    def _est_commission(self, option: OptionInfo, quantity: int) -> float:
        """按品种估算佣金 (仅本地显示)。"""
        from config import (
            STOCK_COMMISSION_PER_SHARE, STOCK_COMMISSION_MIN,
            FUTURES_COMMISSION_PER_CONTRACT, FUTURES_COMMISSION_MIN,
        )
        if option.right == "STK":
            return max(STOCK_COMMISSION_PER_SHARE * quantity, STOCK_COMMISSION_MIN)
        if option.right == "FUT":
            return max(FUTURES_COMMISSION_PER_CONTRACT * quantity, FUTURES_COMMISSION_MIN)
        return max(COMMISSION_PER_CONTRACT * quantity, COMMISSION_MIN)

    def place_stop_limit_order(self, option: OptionInfo, action: OrderAction,
                               quantity: int, stop_price: float,
                               limit_price: float,
                               outside_rth: bool = False) -> int:
        """IBKR **原生 STP LMT**(止损限价)单: 现价触及 stop_price 后, 以 limit_price 挂限价单。
        secType 按 option.right 路由(期权/正股/期货)。用 GTC(跨日有效)。返回 orderId。
        注: 原生单是「已挂在 IBKR」的活动单, 同合约反向已有挂单时仍受 201 限制。"""
        contract = self._quote_contract(option)

        order = Order()
        order.action = action.value
        order.orderType = "STP LMT"
        order.totalQuantity = quantity
        order.auxPrice = stop_price      # 触发价 (stop trigger)
        order.lmtPrice = limit_price     # 触发后挂的限价
        order.eTradeOnly = False
        order.firmQuoteOnly = False
        order.tif = "GTC"                # 止损单跨日有效, 不随当日收盘失效
        order.outsideRth = outside_rth

        order_id = self._app.next_order_id()
        self._orders[order_id] = OrderInfo(
            order_id=order_id, option=option, action=action,
            quantity=quantity, limit_price=limit_price,
            order_type=OrderType.LIMIT,
            commission=self._est_commission(option, quantity),
        )
        print(f"[STP LMT] {action.value} {quantity}x {option.display_name} "
              f"stop={stop_price:.2f} lmt={limit_price:.2f} "
              f"outsideRth={outside_rth} orderId={order_id}", flush=True)
        self._app.placeOrder(order_id, contract, order)
        self.bridge.order_status_changed.emit(
            order_id, OrderStatus.PENDING.value, 0, float(quantity), 0
        )
        return order_id

    def cancel_order(self, order_id: int):
        """Cancel an order."""
        print(f"[ORDER] Cancelling orderId={order_id}", flush=True)
        self._user_cancel_ids.add(order_id)
        self._app.cancelOrder(order_id)

    def cancel_orders_for_option(self, option: OptionInfo) -> int:
        """Cancel only pending orders for one exact ladder instrument.

        Both BUY and SELL orders are included.  Matching uses IBKR conId when
        available and the stable ladder key otherwise, so another contract can
        never be swept in.  Returns the number of cancellation requests sent.
        """
        target_key = option.to_ibkr_key()
        order_ids = [
            order_id
            for order_id, tracked in list(self._orders.items())
            if tracked.status in (OrderStatus.PENDING, OrderStatus.SUBMITTED)
            and tracked.option.same_instrument_as(option)
        ]
        print(
            f"[ORDER] Cancelling {len(order_ids)} order(s) for "
            f"{option.display_name} key={target_key}",
            flush=True,
        )
        for order_id in order_ids:
            self.cancel_order(order_id)
        return len(order_ids)

    def cancel_all_orders(self):
        """Cancel all open orders (including those placed in prior sessions)."""
        self._user_cancel_ids.update(
            order_id for order_id, order in self._orders.items()
            if order.status in (OrderStatus.PENDING, OrderStatus.SUBMITTED)
        )
        self._app.reqGlobalCancel()

    def close_position(self, option: OptionInfo,
                       outside_rth: bool = False) -> int:
        """Close entire position with a market sell order. Returns orderId or -1."""
        key = option.to_ibkr_key()
        qty = self.get_position_qty(key)
        if qty <= 0:
            self.bridge.error_received.emit(-1, -1, "无持仓可平")
            return -1
        return self.place_market_order(option, OrderAction.SELL, qty,
                                       outside_rth=outside_rth)

    def place_forex_order(self, base: str, quote: str, action: str, amount: float):
        """Place a forex conversion order on IDEALPRO."""
        if not self._app or not self._connected:
            self.bridge.error_received.emit(-1, -1, "未连接")
            return

        contract = Contract()
        contract.symbol = base
        contract.secType = "CASH"
        contract.exchange = "IDEALPRO"
        contract.currency = quote

        order = Order()
        order.action = action  # "BUY" or "SELL"
        order.orderType = "MKT"
        order.totalQuantity = amount
        order.eTradeOnly = ""
        order.firmQuoteOnly = ""

        order_id = self._app.next_order_id()
        self._app.placeOrder(order_id, contract, order)
        self.bridge.error_received.emit(
            -1, 0, f"换汇订单已提交: {action} {amount} {base}.{quote}"
        )

    # ── Order/Execution Callbacks ─────────────────────────────────────

    def _on_open_order(self, order_id: int, option: OptionInfo,
                       action_str: str, quantity: int, price: float,
                       order_type_str: str, status_str: str):
        """Restore an order from TWS (prior session or TWS-placed)."""
        if order_id in self._orders:
            return  # Already tracked

        action = OrderAction.BUY if action_str == "BUY" else OrderAction.SELL
        order_type = OrderType.LIMIT if order_type_str == "LMT" else OrderType.MARKET
        order_info = OrderInfo(
            order_id=order_id,
            option=option,
            action=action,
            quantity=quantity,
            limit_price=price,
            order_type=order_type,
        )
        # Map status
        sl = status_str.lower()
        if "fill" in sl:
            order_info.status = OrderStatus.FILLED
        elif "cancel" in sl:
            order_info.status = OrderStatus.CANCELLED
        elif "submit" in sl:
            order_info.status = OrderStatus.SUBMITTED
        elif "inactive" in sl:
            order_info.status = OrderStatus.ERROR
        else:
            order_info.status = OrderStatus.PENDING

        self._orders[order_id] = order_info
        print(f"[ORDER RESTORE] id={order_id} {action_str} {quantity}x "
              f"{option.display_name} @ {price:.2f} status={status_str}",
              flush=True)

    # Order-event warnings — 订单还活着, 不是拒单
    ORDER_WARN_CODES = {399}

    @classmethod
    def _is_order_warning(cls, code: int) -> bool:
        """IBKR 把 2100-2200 整段划为 System/Warning messages —— 订单没被拒。

        真正的拒单在 100-4xx (201 rejected / 202 cancelled / 203 / 321...) 和 10xxx。
        原来只白名单了 2109, 漏了 **2161**(监管限价封顶提示): 2026-08-10 那张
        IBM C320/C325 组合单因此被标成「已拒绝」、弹了拒单框、写进拒单日志 ——
        而它其实好好地**成交了** (紧接着 status=Filled avgFill=0.01)。
        用户看到的"组合单交易不了", 有一半是这个假拒单造成的。
        整段放行, 免得下次又漏一个 21xx。
        """
        return code in cls.ORDER_WARN_CODES or 2100 <= code < 2200

    def _on_order_error(self, req_id: int, code: int, msg: str):
        """Detect IBKR order rejections and surface the reason prominently.

        IBKR reports order errors with reqId == orderId.  Codes 201/202/103
        etc. mean the order was rejected or cancelled server-side — without
        this, the only trace is a console line and a transient status-bar
        message that is easy to miss.
        """
        order = self._orders.get(req_id)
        if order is None:
            return
        if self._is_order_warning(code):
            return  # warning only — status bar already shows it
        if code == 202 and req_id in self._user_cancel_ids:
            self._user_cancel_ids.discard(req_id)
            return  # normal user-requested cancel, not a rejection

        order.status = OrderStatus.ERROR
        order.error_msg = f"[{code}] {msg}"
        print(f"[ORDER REJECTED] orderId={req_id} code={code} msg={msg}",
              flush=True)
        self._log_rejection(order, code, msg)
        self.bridge.order_rejected.emit(req_id, code, msg)

    # Rejection log directory: ibkr_trader/logs/
    LOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")

    def _log_rejection(self, order: OrderInfo, code: int, msg: str):
        """Append rejection details to logs/order_rejects_YYYY-MM-DD.jsonl.

        One JSON object per line with full context (order, quote at the
        moment of rejection, current position, other tracked orders) so the
        failure can be analyzed offline.
        """
        try:
            os.makedirs(self.LOG_DIR, exist_ok=True)
            now = datetime.now()
            path = os.path.join(
                self.LOG_DIR, f"order_rejects_{now:%Y-%m-%d}.jsonl"
            )
            key = order.option.to_ibkr_key()
            tick = self.get_tick(key)
            record = {
                "time": now.isoformat(timespec="seconds"),
                "mode": self._mode.value,
                "order_id": order.order_id,
                "contract": order.option.display_name,
                "con_id": order.option.con_id,
                "action": order.action.value,
                "quantity": order.quantity,
                "order_type": order.order_type.value,
                "limit_price": order.limit_price,
                "create_time": order.create_time.isoformat(timespec="seconds"),
                "reject_code": code,
                "reject_msg": msg,
                "quote": {
                    "bid": tick.get("bid", 0.0),
                    "ask": tick.get("ask", 0.0),
                    "last": tick.get("last", 0.0),
                },
                "position_qty": self.get_position_qty(key),
                "other_orders": [
                    {
                        "id": o.order_id,
                        "contract": o.option.display_name,
                        "action": o.action.value,
                        "qty": o.quantity,
                        "price": o.limit_price,
                        "status": o.status.value,
                    }
                    for o in self._orders.values()
                    if o.order_id != order.order_id
                ],
            }
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
            print(f"[LOG] Rejection logged to {path}", flush=True)
        except Exception as e:
            # Logging must never break order handling
            print(f"[LOG] Failed to write rejection log: {e}", flush=True)

    def _on_order_status(self, order_id: int, status: str,
                         filled: float, remaining: float, avg_price: float):
        """Handle order status update from IBKR."""
        order = self._orders.get(order_id)
        if order is None:
            return

        status_lower = status.lower()
        if "fill" in status_lower:
            order.status = OrderStatus.FILLED
            order.filled_qty = int(filled)
            order.filled_price = avg_price
            order.fill_time = datetime.now()
        elif "cancel" in status_lower:
            # Keep ERROR (rejection reason already recorded) — the follow-up
            # "Cancelled" status from IBKR must not mask the rejection
            if order.status != OrderStatus.ERROR:
                order.status = OrderStatus.CANCELLED
        elif "submit" in status_lower:
            order.status = OrderStatus.SUBMITTED
        elif "pending" in status_lower:
            order.status = OrderStatus.PENDING
        elif "error" in status_lower or "inactive" in status_lower:
            order.status = OrderStatus.ERROR

    def _on_portfolio_position(self, pp: PortfolioPosition):
        """Track the real IBKR option holdings streamed by reqPositions().

        These are the authoritative account positions — crucially they include
        option positions opened in a prior session (before this process
        started). _positions only knows about fills seen this session, so this
        is what lets close_position() / get_position_qty() act on a holding
        left over from before a crash/restart.

        正股/期货持仓也一并缓存 (键用点价梯的伪合约键 `__stock__SYM` /
        `__fut__SYM_YYYYMM`), 使整合后的点价梯在「正股/期货」模式下的
        持仓数量、市价平仓按钮也能从 reqPositions 取数 (与期权一致)。
        """
        key = self._ladder_key_for(pp)
        if key is None:
            return
        if abs(pp.quantity) > 0:
            self._ibkr_positions[key] = pp
        else:
            self._ibkr_positions.pop(key, None)

    @staticmethod
    def _ladder_key_for(pp: PortfolioPosition) -> "str | None":
        """把 reqPositions 的持仓映射到点价梯/下单用的 key (= OptionInfo.to_ibkr_key)。
        期权用 SYM_exp_right_strike; 正股/ETF 用 __stock__SYM; 期货用
        __fut__SYM_YYYYMM。其它类型 (CASH 等) 返回 None 不缓存。"""
        st = pp.sec_type
        if st == "OPT":
            return f"{pp.symbol}_{pp.expiry}_{pp.right}_{pp.strike}"
        if st in ("STK", "ETF"):
            return f"__stock__{pp.symbol}"
        if st == "FUT":
            mon = pp.expiry[:6] if pp.expiry else ""
            return f"__fut__{pp.symbol}_{mon}"
        return None

    def _on_execution(self, order_id: int, side: str,
                      qty: float, price: float):
        """成交回报 —— **不再在本地累加持仓**。

        持仓的唯一真相来自 IBKR API (reqPositions → `_ibkr_positions`,见
        `positions` 属性)。本地按成交累加曾导致撤单竞态/重复回报下出现幻影持仓、
        数目对不上。这里只发 `position_changed` 触发界面刷新 —— 真实数量随后由
        `position()` 回调推来 (亚秒级)。
        """
        self.bridge.position_changed.emit()

    # ── Contract Helpers ──────────────────────────────────────────────

    @staticmethod
    def _make_underlying_contract(symbol: str) -> Contract:
        """Create a contract for the underlying (stock or index)."""
        c = Contract()
        c.symbol = symbol
        c.currency = "USD"
        if symbol.upper() in INDEX_SYMBOLS:
            c.secType = "IND"
            c.exchange = "CBOE"
        else:
            c.secType = "STK"
            c.exchange = "SMART"
            c.primaryExchange = "ARCA"
        return c

    @staticmethod
    def _make_stock_contract(symbol: str) -> Contract:
        """Legacy — use _make_underlying_contract instead."""
        return IBKREngine._make_underlying_contract(symbol)

    @staticmethod
    def _make_option_contract(symbol: str, expiry: str,
                              strike: float, right: str,
                              trading_class: str = "") -> Contract:
        c = Contract()
        c.symbol = symbol
        c.secType = "OPT"
        c.exchange = "SMART"
        c.currency = "USD"
        c.lastTradeDateOrContractMonth = expiry
        c.strike = strike
        c.right = right
        c.multiplier = "100"
        # tradingClass disambiguates symbols with multiple option classes
        # (e.g. SPX monthly vs SPXW weekly). Without it index options come
        # back ambiguous (error 200) and never stream a price.
        if trading_class:
            c.tradingClass = trading_class
        return c

    @staticmethod
    def _make_futures_contract(symbol: str, expiry: str = "") -> Contract:
        """期货合约。symbol 为根代码 (ES/NQ/CL...), expiry 为合约月份
        (YYYYMM 或 YYYYMMDD; 留空则匹配该根代码全部到期 → 供解析近月用)。
        交易所/乘数取自 FUTURES_SPECS。"""
        c = Contract()
        c.symbol = symbol
        c.secType = "FUT"
        c.currency = "USD"
        spec = FUTURES_SPECS.get(symbol.upper())
        # 只设交易所即可唯一定位标准期货 (各根代码在其交易所乘数唯一);
        # 不设 multiplier 以免与 IBKR 的字符串表示不完全一致 → 空结果。
        c.exchange = spec[0] if spec else "SMART"
        if expiry:
            c.lastTradeDateOrContractMonth = expiry
        return c

    def resolve_futures_contracts(self, symbol: str, max_count: int = 5) -> list:
        """解析某期货根代码的近月起若干个合约 (近月 + 之后的季月/月度), 阻塞。
        返回 [{'expiry': 'YYYYMMDD', 'con_id': int, 'local_symbol': str,
        'multiplier': float}], 按到期升序, 已滤掉过期合约。
        复用 reqContractDetails + threading.Event 模式。"""
        if not self._app or not self._connected:
            raise RuntimeError("Not connected")
        contract = self._make_futures_contract(symbol)
        req_id = self._app.next_req_id()
        self._app._contract_data[req_id] = {
            "details": [], "event": threading.Event(), "error": None,
        }
        self._app.reqContractDetails(req_id, contract)

        state = self._app._contract_data[req_id]
        if not state["event"].wait(timeout=10):
            self._app._contract_data.pop(req_id, None)
            raise RuntimeError(f"解析期货合约超时: {symbol}")
        if state["error"]:
            code, msg = state["error"]
            self._app._contract_data.pop(req_id, None)
            raise RuntimeError(f"期货合约错误 {symbol}: code={code} {msg}")
        details = state["details"]
        self._app._contract_data.pop(req_id, None)

        today = datetime.now().strftime("%Y%m%d")
        out = []
        for d in details:
            con = d.contract
            exp = con.lastTradeDateOrContractMonth or ""
            # 归一到 YYYYMMDD 末日比较 (YYYYMM 视为该月末, 近似用月+末日不必精确,
            # 用 月份>=本月 判定即可避免漏掉本月内尚未到期的合约)
            exp8 = exp if len(exp) >= 8 else (exp + "31")[:8]
            if exp8 < today:
                continue
            try:
                mult = float(con.multiplier) if con.multiplier else 1.0
            except (ValueError, TypeError):
                mult = 1.0
            out.append({
                "expiry": exp,
                "con_id": con.conId,
                "local_symbol": con.localSymbol,
                "multiplier": mult,
            })
        out.sort(key=lambda x: x["expiry"])
        return out[:max_count]

    # ── Combo / Spread Orders ────────────────────────────────────────

    def resolve_option_con_id(self, symbol: str, expiry: str,
                               strike: float, right: str) -> int:
        """Resolve conId for a specific option contract (blocking).
        Uses reqContractDetails + threading.Event, same pattern as get_con_id.
        """
        contract = self._make_option_contract(
            symbol, expiry, strike, right,
            self._trading_class_for(symbol, expiry),
        )
        req_id = self._app.next_req_id()
        self._app._contract_data[req_id] = {
            "details": [], "event": threading.Event(), "error": None,
        }
        self._app.reqContractDetails(req_id, contract)

        state = self._app._contract_data[req_id]
        if not state["event"].wait(timeout=10):
            self._app._contract_data.pop(req_id, None)
            raise RuntimeError(
                f"Timeout resolving conId: {symbol} {expiry} {right} {strike}"
            )
        if state["error"]:
            code, msg = state["error"]
            self._app._contract_data.pop(req_id, None)
            raise RuntimeError(
                f"Contract error {symbol} {expiry} {right} {strike}: "
                f"code={code} {msg}"
            )
        if not state["details"]:
            self._app._contract_data.pop(req_id, None)
            raise RuntimeError(
                f"No contract found: {symbol} {expiry} {right} {strike}"
            )

        con_id = state["details"][0].contract.conId
        self._app._contract_data.pop(req_id, None)
        return con_id

    def place_combo_order(self, symbol: str, legs: list,
                          action: str, quantity: int,
                          limit_price: float,
                          outside_rth: bool = False,
                          market: bool = False,
                          non_guaranteed: bool = False) -> int:
        """Place a BAG (combo) order for a multi-leg strategy.

        Args:
            symbol: Underlying symbol (e.g. "SPY")
            legs: List of ComboLegInfo objects (con_id must be resolved)
            action: "BUY" or "SELL"
            quantity: Number of combo units
            limit_price: Net limit price for the combo (market=True 时忽略)
            outside_rth: Allow execution outside regular trading hours
            market: True = 市价组合单 (一键平仓用)

        Returns:
            orderId
        """
        from ibapi.contract import ComboLeg
        from ibapi.tag_value import TagValue

        if not self._app or not self._connected:
            self.bridge.error_received.emit(-1, -1, "未连接")
            return -1

        # Build BAG contract
        contract = Contract()
        contract.symbol = symbol
        contract.secType = "BAG"
        contract.currency = "USD"
        contract.exchange = "SMART"

        combo_legs = []
        for leg in legs:
            cl = ComboLeg()
            cl.conId = leg.con_id
            cl.ratio = leg.ratio
            cl.action = leg.action
            cl.exchange = leg.exchange or "SMART"
            combo_legs.append(cl)
        contract.comboLegs = combo_legs

        # Build order
        order = Order()
        order.action = action
        order.orderType = "MKT" if market else "LMT"
        order.totalQuantity = quantity
        order.lmtPrice = 0.0 if market else limit_price
        order.eTradeOnly = False
        order.firmQuoteOnly = False
        order.tif = "DAY"
        order.outsideRth = outside_rth
        # NonGuaranteed=1 = 允许 SMART 把各腿**拆开分别成交**。代价是 IBKR 会按
        # "每条腿各自独立"算保证金 —— 卖出的那条腿被当成**裸卖**, 于是价差单被
        # 按裸空的保证金拒掉 (实测: SPX 7045/7050 价差报
        # "GROSS POSITION VALUE [121184.60] MUST NOT EXCEED NLV [1219.18] × 30",
        # IBM C320/C325 那次则是 "UNCOVERED OPTION POSITION")。
        # TWS 原生组合单默认是**保证成交(guaranteed)**, 按价差整体算保证金, 所以
        # 同一个价差在 TWS 能下、在这里下不了。默认改回 guaranteed, 需要拆腿时
        # 由界面上的勾选显式打开。
        if non_guaranteed:
            order.smartComboRoutingParams = [TagValue("NonGuaranteed", "1")]

        order_id = self._app.next_order_id()

        # Track with synthetic OptionInfo (right="COMBO")
        from models import ComboLegInfo
        total_contracts = sum(leg.ratio for leg in legs)
        commission = max(
            COMMISSION_PER_CONTRACT * total_contracts * quantity,
            COMMISSION_MIN,
        )
        option = OptionInfo(
            symbol=symbol,
            expiry="",
            strike=0.0,
            right="COMBO",
        )
        order_info = OrderInfo(
            order_id=order_id,
            option=option,
            action=OrderAction.BUY if action == "BUY" else OrderAction.SELL,
            quantity=quantity,
            limit_price=0.0 if market else limit_price,
            order_type=OrderType.MARKET if market else OrderType.LIMIT,
            commission=commission,
        )
        self._orders[order_id] = order_info

        leg_desc = " + ".join(
            f"{l.action} {l.ratio}x {l.right}{l.strike}" for l in legs
        )
        price_desc = "MKT" if market else f"@ {limit_price:.2f}"
        print(f"[COMBO ORDER] {action} {quantity}x {symbol} "
              f"[{leg_desc}] {price_desc} "
              f"outsideRth={outside_rth} orderId={order_id}", flush=True)

        self._app.placeOrder(order_id, contract, order)
        self.bridge.order_status_changed.emit(
            order_id, OrderStatus.PENDING.value, 0, float(quantity), 0
        )
        return order_id

    def get_position_qty(self, option_key: str) -> int:
        """持仓数量 **以 IBKR API (reqPositions) 为准**。

        只读 `_ibkr_positions`(真实账户持仓),不再用本地成交跟踪 —— 后者会因
        撤单竞态/重复回报产生幻影、数目对不上。平仓/撤单类无害响应不影响此处。
        """
        pp = self._ibkr_positions.get(option_key)
        return int(pp.quantity) if pp and pp.quantity > 0 else 0
