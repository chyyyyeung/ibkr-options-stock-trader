"""本地条件单管理器 — 止盈/止损 (到价才发限价单到 IBKR)。

「本地条件单」在到价前**不发到 IBKR**:本程序每 0.5 秒读现价,触发后才提交一张限价单。
好处:规避 IBKR「同合约不能双向挂单」(错误 201);坏处:**只在程序运行时监控**,
关程序 / 崩溃 / 断网就不会触发(已持久化到 conditional_orders.json,重连后恢复监控)。

原生 STP LMT(服务器端)不归本管理器,由 MainWindow 直接调 engine.place_stop_limit_order。
"""

import os
import json
import time

from PyQt5.QtCore import QObject, pyqtSignal, QTimer

from models import ConditionalOrder, OptionInfo


_STATE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "conditional_orders.json")
_RETRY_COOLDOWN_S = 5.0   # 触发后下单失败的重试间隔


def _current_price(tick: dict) -> float:
    """现价: 优先 last, 退回中价/单边。"""
    last = tick.get("last", 0) or 0
    if last > 0:
        return last
    bid = tick.get("bid", 0) or 0
    ask = tick.get("ask", 0) or 0
    if bid > 0 and ask > 0:
        return (bid + ask) / 2
    return bid or ask or 0.0


class ConditionalOrderManager(QObject):
    """监控本地条件单, 到价触发下单。所有外部依赖通过 configure() 注入,
    与具体引擎/品种路由解耦 (MainWindow 提供 place 回调按品种发单)。"""

    changed = pyqtSignal()                 # 列表变化 (UI 刷新)
    triggered = pyqtSignal(object, int)    # (ConditionalOrder, order_id) 已触发并下单
    failed = pyqtSignal(object, str)       # (ConditionalOrder, msg) 触发后下单失败
    voided = pyqtSignal(object, str)       # (ConditionalOrder, reason) 自动作废 (过期/仓位已平)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._conds: dict[int, ConditionalOrder] = {}
        self._req_ids: dict[int, int] = {}        # cond_id -> tick reqId
        self._cooldown: dict[int, float] = {}     # cond_id -> 下次可重试时间
        self._next_id = 1

        # 注入的回调 (默认空操作, configure 后生效)
        self._get_tick = lambda key: {}
        self._place = lambda opt, action, lmt, qty, outside, market: -1
        self._subscribe = lambda opt: None
        self._subscribe_under = lambda symbol: None
        self._unsubscribe = lambda req_id: None
        self._get_position_qty = None          # key -> 实际持仓数量 (API 真相)
        self._positions_ready = lambda: True   # 初始持仓快照是否已到

        self._timer = QTimer()
        self._timer.timeout.connect(self._check)
        self._timer.start(200)   # 巡检间隔 (用户要求 0.2s, 2026-07-10)

    # ── 依赖注入 ──────────────────────────────────────────────────────
    def configure(self, get_tick, place, subscribe, unsubscribe, subscribe_under=None,
                  get_position_qty=None, positions_ready=None):
        self._get_tick = get_tick
        self._place = place
        self._subscribe = subscribe
        self._unsubscribe = unsubscribe
        if subscribe_under is not None:
            self._subscribe_under = subscribe_under
        if get_position_qty is not None:
            self._get_position_qty = get_position_qty
        if positions_ready is not None:
            self._positions_ready = positions_ready

    # ── 查询 ──────────────────────────────────────────────────────────
    def all(self) -> list:
        return list(self._conds.values())

    def for_key(self, key: str) -> list:
        return [c for c in self._conds.values() if c.key == key]

    # ── 武装 / 取消 ───────────────────────────────────────────────────
    def arm(self, option: OptionInfo, kind: str, trigger_price: float,
            limit_price: float, quantity: int, outside_rth: bool,
            watch: str = "SELF", direction: str = "",
            market: bool = False, watch_symbol: str = "") -> ConditionalOrder:
        """新增一个本地条件单并开始监控。

        kind: 'TP'(止盈)/'SL'(止损)/'UL'(标的价触发)。
        watch: 'SELF'(监控期权自身价) / 'UNDER'(监控标的价)。
        direction: 'UP'(>=) / 'DOWN'(<=); 空则按 kind 推导。
        market: True=触发后发市价单 (忽略 limit_price)。
        watch_symbol: watch='UNDER' 时监控的标的代码 ("" = 期权自己的标的;
                      可指定其它, 如 SPY 期权盯 SPX/ES)。
        """
        cond = ConditionalOrder(
            cond_id=self._next_id, option=option, kind=kind, action="SELL",
            trigger_price=float(trigger_price), limit_price=float(limit_price),
            quantity=int(quantity), native=False, outside_rth=bool(outside_rth),
            watch=watch, direction=direction, market=bool(market),
            watch_symbol=(watch_symbol or "").upper(),
        )
        self._next_id += 1
        # 去重: 同合约+同类型+同监控对象+同方向+同触发价的旧条件单 → 用新单替换
        # (曾出现完全相同的两条并存, 触发时对同一持仓连发两张卖单)
        for old in [c for c in self._conds.values()
                    if c.key == cond.key and c.kind == cond.kind
                    and c.watch == cond.watch
                    and c.watch_symbol == cond.watch_symbol
                    and c._trigger_dir() == cond._trigger_dir()
                    and abs(c.trigger_price - cond.trigger_price) < 1e-9]:
            self._conds.pop(old.cond_id, None)
            self._drop_subscription(old.cond_id)
            self._cooldown.pop(old.cond_id, None)
        self._conds[cond.cond_id] = cond
        self._ensure_subscribed(cond)
        self._save()
        self.changed.emit()
        return cond

    def cancel(self, cond_id: int):
        cond = self._conds.pop(cond_id, None)
        if cond is None:
            return
        self._drop_subscription(cond_id)
        self._cooldown.pop(cond_id, None)
        self._save()
        self.changed.emit()

    def cancel_for_key(self, key: str):
        for cid in [c.cond_id for c in self.for_key(key)]:
            self._conds.pop(cid, None)
            self._drop_subscription(cid)
            self._cooldown.pop(cid, None)
        self._save()
        self.changed.emit()

    def clear_all(self, persist: bool = True):
        for cid in list(self._req_ids):
            self._drop_subscription(cid)
        self._conds.clear()
        self._cooldown.clear()
        if persist:
            self._save()
        self.changed.emit()

    # ── 连接生命周期 (MainWindow 调用) ────────────────────────────────
    def resume(self):
        """连接成功后调用: 从磁盘恢复条件单并重新订阅各合约行情。"""
        self._load()
        for cond in self._conds.values():
            self._ensure_subscribed(cond)
        self.changed.emit()

    def suspend(self):
        """断开时调用: 退订行情但**保留**条件单 (重连后 resume)。"""
        for cid in list(self._req_ids):
            self._drop_subscription(cid)

    # ── 监控循环 ──────────────────────────────────────────────────────
    def _check(self):
        if not self._conds:
            return
        now = time.time()
        today = time.strftime("%Y%m%d")
        for cond in list(self._conds.values()):
            if cond.is_expired(today):
                self._void(cond, "合约已到期")
                continue
            if self._cooldown.get(cond.cond_id, 0) > now:
                continue
            price = _current_price(self._get_tick(cond.watch_key))
            if price <= 0 or not cond.is_triggered(price):
                continue
            # 触发 → 先按 API 持仓核对数量 (防止手动平仓后仍卖出 → 开出裸空被拒)
            if self._get_position_qty is not None:
                if not self._positions_ready():
                    # 初始持仓快照未到 (刚连接的头几秒) → 无法核对, 稍后重查
                    self._cooldown[cond.cond_id] = now + 1.0
                    continue
                try:
                    held = int(self._get_position_qty(cond.key))
                except Exception:
                    held = cond.quantity  # 查询异常不拦截, 按原数量下单
                if held <= 0:
                    self._void(cond, "仓位已平, 自动作废")
                    continue
                if held < cond.quantity:
                    print(f"[COND] #{cond.cond_id} 数量 {cond.quantity} 超过"
                          f"实际持仓 {held}, 按持仓量卖出", flush=True)
                    cond.quantity = held
            # 下单
            try:
                order_id = self._place(
                    cond.option, cond.action, cond.limit_price,
                    cond.quantity, cond.outside_rth, cond.market,
                )
            except Exception as e:
                order_id = -1
                print(f"[COND] place error: {e}", flush=True)
            if order_id and order_id > 0:
                otype = "市价" if cond.market else f"限{cond.limit_price:.2f}"
                print(f"[COND] {cond.kind_label} 触发 @ {price:.2f} → 下单 "
                      f"{cond.option.display_name} {otype} "
                      f"id={order_id}", flush=True)
                self._conds.pop(cond.cond_id, None)
                self._drop_subscription(cond.cond_id)
                self._cooldown.pop(cond.cond_id, None)
                self._save()
                self.triggered.emit(cond, order_id)
                self.changed.emit()
            else:
                # 下单失败 (如资金不足/反向单 201) → 冷却后重试, 不丢失止损
                self._cooldown[cond.cond_id] = now + _RETRY_COOLDOWN_S
                self.failed.emit(cond, "下单失败, 稍后重试 (检查资金/反向挂单/连接)")

    def _void(self, cond: ConditionalOrder, reason: str):
        """自动作废一个条件单 (合约过期 / 仓位已平), 移除并通知 UI。"""
        self._conds.pop(cond.cond_id, None)
        self._drop_subscription(cond.cond_id)
        self._cooldown.pop(cond.cond_id, None)
        self._save()
        print(f"[COND] #{cond.cond_id} {cond.kind_label} "
              f"{cond.option.display_name} 作废: {reason}", flush=True)
        self.voided.emit(cond, reason)
        self.changed.emit()

    # ── 行情订阅 ──────────────────────────────────────────────────────
    def _ensure_subscribed(self, cond: ConditionalOrder):
        if cond.cond_id in self._req_ids:
            return
        try:
            # 标的触发看标的行情 (默认期权自己的标的, 可指定其它), 否则看期权自身
            if cond.watch == "UNDER":
                req_id = self._subscribe_under(
                    cond.watch_symbol or cond.option.symbol)
            else:
                req_id = self._subscribe(cond.option)
            if req_id is not None:
                self._req_ids[cond.cond_id] = req_id
        except Exception as e:
            print(f"[COND] subscribe error: {e}", flush=True)

    def _drop_subscription(self, cond_id: int):
        req_id = self._req_ids.pop(cond_id, None)
        if req_id is not None:
            try:
                self._unsubscribe(req_id)
            except Exception:
                pass

    # ── 持久化 ────────────────────────────────────────────────────────
    def _save(self):
        try:
            with open(_STATE_PATH, "w", encoding="utf-8") as f:
                json.dump([c.to_dict() for c in self._conds.values()],
                          f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f"[COND] save error: {e}", flush=True)

    def _load(self):
        if not os.path.exists(_STATE_PATH):
            return
        try:
            with open(_STATE_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception as e:
            print(f"[COND] load error: {e}", flush=True)
            return
        self._conds.clear()
        max_id = 0
        dropped = 0
        today = time.strftime("%Y%m%d")
        for d in data:
            try:
                cond = ConditionalOrder.from_dict(d)
            except Exception:
                continue
            max_id = max(max_id, cond.cond_id)
            if cond.is_expired(today):
                dropped += 1   # 过期合约的条件单直接丢弃, 不再恢复监控
                continue
            self._conds[cond.cond_id] = cond
        self._next_id = max(self._next_id, max_id + 1)
        if dropped:
            print(f"[COND] 已清理 {dropped} 条过期条件单", flush=True)
            self._save()

    def cleanup(self):
        self._timer.stop()
        for cid in list(self._req_ids):
            self._drop_subscription(cid)
