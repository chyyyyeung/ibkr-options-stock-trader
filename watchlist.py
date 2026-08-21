"""自选监控 (Watch List) — 把标的/合约加入列表, 实时看价 + 到价警报。

与 conditional_orders.py 同构: 依赖通过 configure() 注入 (get_tick/订阅/退订),
QTimer 0.5s 巡检; 持久化 watchlist.json (gitignore, 个人数据);
启动 resume() 加载时**自动清理过期合约** (期权 expiry < 今日, 期货月份 < 本月);
每个合约可挂**任意多条**到价警报 (每条 = 方向 above/below + 价格), 右键标的追加;
警报为**一次性**: 触发后该条自动移除, 避免反复响。
"""

import os
import json
import time
from dataclasses import dataclass, field

from PyQt5.QtCore import QObject, pyqtSignal, QTimer

from models import OptionInfo
from conditional_orders import _current_price

_STATE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "watchlist.json")
_TICK_MS = 500   # 价格刷新/警报巡检间隔 (用户要求 0.5s)


@dataclass
class Alert:
    """一条到价警报: 现价触及 price 时 (above=涨破≥ / below=跌破≤) 触发一次。

    id 仅在**会话内唯一** (由 WatchListManager 分配), 供面板按 id 定位/编辑/删除;
    不参与持久化 (每次加载重新分配)。
    """
    direction: str          # "above" | "below"
    price: float
    id: int = 0

    def hit(self, price: float) -> bool:
        if self.price <= 0:
            return False
        if self.direction == "above":
            return price >= self.price
        return price <= self.price


@dataclass
class WatchItem:
    """一条自选监控: 合约 + 任意多条到价警报。"""
    option: OptionInfo
    alerts: list = field(default_factory=list)   # list[Alert]

    @property
    def key(self) -> str:
        return self.option.to_ibkr_key()

    def is_expired(self, today: str) -> bool:
        """过期判定: 期权 expiry(YYYYMMDD) < 今日; 期货合约月份 < 本月。"""
        o = self.option
        if o.right in ("C", "P") and len(o.expiry) == 8:
            return o.expiry < today
        if o.right == "FUT" and len(o.expiry) >= 6:
            return o.expiry[:6] < today[:6]
        return False   # 正股不过期

    def to_dict(self) -> dict:
        o = self.option
        return {
            "symbol": o.symbol, "expiry": o.expiry, "strike": o.strike,
            "right": o.right, "con_id": o.con_id,
            "alerts": [{"direction": a.direction, "price": a.price}
                       for a in self.alerts if a.price > 0],
        }

    @staticmethod
    def from_dict(d: dict) -> "WatchItem":
        opt = OptionInfo(
            symbol=d["symbol"], expiry=d.get("expiry", ""),
            strike=d.get("strike", 0.0), right=d.get("right", "C"),
            con_id=d.get("con_id", 0),
        )
        alerts: list = []
        raw = d.get("alerts")
        if isinstance(raw, list):
            for a in raw:
                try:
                    direction = a.get("direction")
                    price = float(a.get("price", 0.0) or 0.0)
                except (AttributeError, TypeError, ValueError):
                    continue
                if direction in ("above", "below") and price > 0:
                    alerts.append(Alert(direction=direction, price=price))
        else:
            # 迁移旧格式 (单 alert_above / alert_below 标量)
            above = float(d.get("alert_above", 0.0) or 0.0)
            below = float(d.get("alert_below", 0.0) or 0.0)
            if above > 0:
                alerts.append(Alert(direction="above", price=above))
            if below > 0:
                alerts.append(Alert(direction="below", price=below))
        return WatchItem(option=opt, alerts=alerts)


class WatchListManager(QObject):
    """管理自选列表: 订阅行情、0.5s 巡检价格与警报、持久化。"""

    changed = pyqtSignal()                    # 列表结构/警报设置变化 (面板重建)
    ticked = pyqtSignal()                     # 0.5s 心跳 (面板刷新价格)
    alerted = pyqtSignal(object, str, float)  # (WatchItem, "above"/"below", 现价)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._items: dict[str, WatchItem] = {}   # key -> WatchItem
        self._req_ids: dict[str, int] = {}       # key -> tick reqId
        self._next_alert_id = 1                   # 会话内唯一警报 id 计数器
        # 是否已经从磁盘读过一次。_save() 以此为闸: 没读过就不许写盘,
        # 否则一个空的内存列表会把磁盘上的自选整个覆盖掉。
        self._loaded = False
        self._purge_day = ""                      # 上次执行过期清理的日期 (YYYYMMDD)

        # 注入的回调 (默认空操作, configure 后生效)
        self._get_tick = lambda key: {}
        self._subscribe = lambda opt: None
        self._unsubscribe = lambda req_id: None

        # **构造即加载**: 自选是纯本地数据, 不该等连上 Gateway 才出现。
        # 早先只在 resume() (连接成功) 里加载, 于是:
        #   1) 未连接时「监控」标签页是空的 —— 看着就像自选丢了;
        #   2) 此时若再加一条自选, _save() 会把「空列表 + 这一条」写回磁盘,
        #      原有的自选被真正抹掉 —— 这才是"下次打开就没了"的根因。
        self._load()

        self._timer = QTimer()
        self._timer.timeout.connect(self._tick)
        self._timer.start(_TICK_MS)

    def configure(self, get_tick, subscribe, unsubscribe):
        self._get_tick = get_tick
        self._subscribe = subscribe
        self._unsubscribe = unsubscribe

    # ── 查询 ──────────────────────────────────────────────────────────
    def items(self) -> list:
        return list(self._items.values())

    def price_of(self, key: str) -> dict:
        """最新 tick (bid/ask/last), 供面板显示。"""
        return self._get_tick(key) or {}

    # ── 增删 / 警报设置 ──────────────────────────────────────────────
    def add(self, option: OptionInfo) -> bool:
        """加入自选; 已存在返回 False。"""
        item = WatchItem(option=option)
        if item.key in self._items:
            return False
        self._items[item.key] = item
        self._ensure_subscribed(item)
        self._save()
        self.changed.emit()
        return True

    def remove(self, key: str):
        if self._items.pop(key, None) is None:
            return
        self._drop_subscription(key)
        self._save()
        self.changed.emit()

    def _new_alert_id(self) -> int:
        i = self._next_alert_id
        self._next_alert_id += 1
        return i

    def add_alert(self, key: str, direction: str, price: float):
        """给某合约追加一条到价警报; 成功返回新 alert id, 否则 None。"""
        item = self._items.get(key)
        if item is None:
            return None
        try:
            price = max(float(price), 0.0)
        except (TypeError, ValueError):
            return None
        if direction not in ("above", "below") or price <= 0:
            return None
        alert = Alert(direction=direction, price=price, id=self._new_alert_id())
        item.alerts.append(alert)
        self._save()
        self.changed.emit()
        return alert.id

    def update_alert(self, key: str, alert_id: int, price: float):
        """改某条警报价; price <= 0 视为删除该条。"""
        item = self._items.get(key)
        if item is None:
            return
        try:
            price = max(float(price), 0.0)
        except (TypeError, ValueError):
            price = 0.0
        for a in item.alerts:
            if a.id == alert_id:
                if price <= 0:
                    item.alerts.remove(a)
                else:
                    a.price = price
                break
        self._save()
        self.changed.emit()

    def toggle_alert_direction(self, key: str, alert_id: int):
        """切换某条警报方向 above ↔ below。"""
        item = self._items.get(key)
        if item is None:
            return
        for a in item.alerts:
            if a.id == alert_id:
                a.direction = "below" if a.direction == "above" else "above"
                break
        self._save()
        self.changed.emit()

    def remove_alert(self, key: str, alert_id: int):
        """删除某合约的一条警报 (保留合约本身)。"""
        item = self._items.get(key)
        if item is None:
            return
        before = len(item.alerts)
        item.alerts = [a for a in item.alerts if a.id != alert_id]
        if len(item.alerts) != before:
            self._save()
            self.changed.emit()

    # ── 连接生命周期 (MainWindow 调用) ────────────────────────────────
    def resume(self):
        """连接成功后调用: 清理过期合约并订阅行情。

        **不再** clear + 重读磁盘 —— 列表在 __init__ 就已加载好。重读会把
        「连接建立之前用户加进来的自选」连同其警报一起抹掉 (重连/切换实盘模拟
        都会走到这里, 不只是首次连接)。
        """
        if not self._loaded:      # 理论上 __init__ 已加载; 兜底
            self._load()
        self._purge_expired()
        for item in self._items.values():
            self._ensure_subscribed(item)
        self.changed.emit()

    def suspend(self):
        """断开时调用: 退订行情但保留列表 (重连后 resume)。"""
        for key in list(self._req_ids):
            self._drop_subscription(key)

    # ── 巡检 (0.5s) ──────────────────────────────────────────────────
    def _tick(self):
        # 跨日 → 清一次过期合约 (app 连开数日时, 昨天到期的期权应自动移出)
        if self._loaded and self._purge_day != time.strftime("%Y%m%d"):
            self._purge_expired()
        if not self._items:
            return
        fired = False
        for item in list(self._items.values()):
            if not item.alerts:
                continue
            price = _current_price(self._get_tick(item.key))
            if price <= 0:
                continue
            remaining = []
            for a in item.alerts:
                if a.hit(price):
                    sign = "≥" if a.direction == "above" else "≤"
                    print(f"[WATCH] {item.option.display_name} 现价 {price:.2f} "
                          f"{sign} {a.price:.2f} 到价警报", flush=True)
                    self.alerted.emit(item, a.direction, price)  # 一次性: 不放回
                    fired = True
                else:
                    remaining.append(a)
            item.alerts = remaining
        if fired:
            self._save()
            self.changed.emit()   # 警报清零 → 面板重建显示
        self.ticked.emit()

    # ── 行情订阅 ──────────────────────────────────────────────────────
    def _ensure_subscribed(self, item: WatchItem):
        if item.key in self._req_ids:
            return
        try:
            req_id = self._subscribe(item.option)
            if req_id is not None:
                self._req_ids[item.key] = req_id
        except Exception as e:
            print(f"[WATCH] subscribe error: {e}", flush=True)

    def _drop_subscription(self, key: str):
        req_id = self._req_ids.pop(key, None)
        if req_id is not None:
            try:
                self._unsubscribe(req_id)
            except Exception:
                pass

    # ── 持久化 ────────────────────────────────────────────────────────
    def _purge_expired(self) -> int:
        """移除已过期的合约 (期权 expiry < 今日, 期货月份 < 本月)。返回移除条数。

        「删除」与「过期」是自选**仅有**的两个消失理由 —— 其余一律留着。
        除加载时执行外, 每天跨日后也执行一次 (app 常年开着不重启时, 昨天到期
        的期权不该一直挂在监控里)。
        """
        today = time.strftime("%Y%m%d")
        self._purge_day = today
        expired = [k for k, it in self._items.items() if it.is_expired(today)]
        for k in expired:
            self._items.pop(k, None)
            self._drop_subscription(k)
        if expired:
            print(f"[WATCH] 已清理 {len(expired)} 条过期自选", flush=True)
            self._save()
            self.changed.emit()
        return len(expired)

    def _save(self):
        if not self._loaded:
            # 还没读过磁盘就写盘 = 用空列表覆盖用户的自选。宁可不保存。
            print("[WATCH] 尚未加载磁盘自选, 跳过保存 (避免覆盖)", flush=True)
            return
        try:
            with open(_STATE_PATH, "w", encoding="utf-8") as f:
                json.dump([it.to_dict() for it in self._items.values()],
                          f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f"[WATCH] save error: {e}", flush=True)

    def _load(self):
        """从磁盘读入自选。文件不存在 = 空列表 (也算加载成功, 允许后续保存)。

        读取失败 (JSON 损坏/占用) 时**不**置 _loaded —— 让 _save() 保持封锁,
        免得把一个读不出来的文件用空列表覆盖掉, 那才是真的丢数据。
        """
        self._purge_day = time.strftime("%Y%m%d")
        if not os.path.exists(_STATE_PATH):
            self._loaded = True
            return
        try:
            with open(_STATE_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception as e:
            print(f"[WATCH] load error: {e} — 已封锁保存以免覆盖该文件", flush=True)
            return
        self._items.clear()
        dropped = 0
        today = self._purge_day
        for d in data:
            try:
                item = WatchItem.from_dict(d)
            except Exception:
                continue
            if item.is_expired(today):
                dropped += 1   # 过期合约直接丢弃
                continue
            for a in item.alerts:            # 分配会话内唯一 id
                a.id = self._new_alert_id()
            self._items[item.key] = item
        self._loaded = True
        print(f"[WATCH] 已加载 {len(self._items)} 条自选", flush=True)
        if dropped:
            print(f"[WATCH] 已清理 {dropped} 条过期自选", flush=True)
            self._save()

    def cleanup(self):
        self._timer.stop()
        for key in list(self._req_ids):
            self._drop_subscription(key)
