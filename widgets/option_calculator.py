"""期权理论价计算器 (Black-Scholes)。

放在主窗口右下角, 跟随左侧待交易期权 (price ladder 当前选中的合约)。
用 IBKR 推送的 IV + 标的价, 配合行权价与剩余到期时间, 按 Black-Scholes
实时算出期权的「应有价格」, 并与盘口中间价比较 (高估 / 低估)。

刷新由 QTimer 驱动: 既跟随实时行情 (IV / 标的价更新), 也随时间衰减
(剩余到期时间 T 每次重算), 因此理论价会「根据时间实时更新」。

勾掉「跟随实时」即进入手动 what-if 模式, 可任意改 S / IV / 利率 / 剩余天数试算。
"""

from __future__ import annotations

import json
import math
import threading
import urllib.request
from datetime import datetime

from PyQt5.QtCore import Qt, QTimer, pyqtSignal
from PyQt5.QtGui import QFont
from PyQt5.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QGridLayout, QLabel, QDoubleSpinBox,
    QCheckBox, QFrame, QPushButton, QRadioButton, QButtonGroup,
)

from models import OptionInfo
from config import (
    RISK_FREE_RATE, DIVIDEND_YIELD, OPTION_MARKET_CLOSE_ET,
    CALCULATOR_REFRESH_MS, COLOR_BG_PANEL, COLOR_BG_DARK, COLOR_BORDER,
    COLOR_TEXT, COLOR_TEXT_DIM, COLOR_GREEN, COLOR_RED, COLOR_ACCENT,
)


class KeyboardOnlySpinBox(QDoubleSpinBox):
    """只允许**键盘**改值的数值框。

    鼠标滚轮/拖拽在这里是纯粹的误操作来源: 滚动整个计算器面板时指针恰好掠过
    某个输入框, 标的价/IV 就被悄悄改掉了, 而理论价会跟着变 —— 看上去像算错了。
    这里把滚轮事件直接放行给父级 (面板照常滚动, 值不变), 并且不接受滚轮焦点;
    上下调节按钮本来就已隐藏 (NoButtons)。键入数字、↑↓、PgUp/PgDn 均不受影响。
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        # 只能点击/Tab 获得焦点, 滚轮不会先把焦点抢过来再改值
        self.setFocusPolicy(Qt.StrongFocus)

    def wheelEvent(self, event):
        event.ignore()  # 不消费 → 交给外层滚动区域, 值保持不变


# ── Black-Scholes ───────────────────────────────────────────────────────

def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def black_scholes_price(S, K, T, r, sigma, right, q=0.0):
    """Black-Scholes-Merton 欧式期权理论价。right: 'C'/'P'。无效输入返回 0。"""
    if S <= 0 or K <= 0 or sigma <= 0 or T <= 0:
        return 0.0
    sqrt_t = math.sqrt(T)
    d1 = (math.log(S / K) + (r - q + 0.5 * sigma * sigma) * T) / (sigma * sqrt_t)
    d2 = d1 - sigma * sqrt_t
    if right == "C":
        return (S * math.exp(-q * T) * _norm_cdf(d1)
                - K * math.exp(-r * T) * _norm_cdf(d2))
    return (K * math.exp(-r * T) * _norm_cdf(-d2)
            - S * math.exp(-q * T) * _norm_cdf(-d1))


def solve_underlying_for_price(target, K, T, r, sigma, right, q=0.0):
    """反向求解: 给定目标期权价 target 与 (K, T, r, sigma), 求所需标的价 S。

    BS 价格对 S 单调 (Call 递增 / Put 递减), 用括弧二分法稳健求解。
    无解时返回 0.0:
      - 输入非法 (target/K/sigma/T <= 0);
      - Call 目标价低于内在价值下界几乎为 0 仍可解, 但极端值括不住时返回 0;
      - Put 目标价 >= K*exp(-rT) (需 S<=0 才能达到) → 无解。
    """
    if target <= 0 or K <= 0 or sigma <= 0 or T <= 0:
        return 0.0

    def f(S):
        return black_scholes_price(S, K, T, r, sigma, right, q)

    lo, hi = 1e-6, max(K, target) * 2.0 + 1.0

    if right == "C":
        # 价格随 S 递增: 扩大上界直到 f(hi) >= target
        for _ in range(128):
            if f(hi) >= target:
                break
            hi *= 2.0
        else:
            return 0.0
        for _ in range(200):
            mid = (lo + hi) / 2.0
            if f(mid) < target:
                lo = mid
            else:
                hi = mid
        return (lo + hi) / 2.0

    # Put: 价格随 S 递减, 在 S→0 时取最大 K*exp(-rT)
    if target >= K * math.exp(-r * T):
        return 0.0  # 目标价过高, 需 S<=0 才能达到 — 无解
    for _ in range(128):
        if f(hi) <= target:
            break
        hi *= 2.0
    else:
        return 0.0
    for _ in range(200):
        mid = (lo + hi) / 2.0
        if f(mid) > target:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2.0


def years_to_expiry(expiry: str) -> float:
    """从现在到 expiry(YYYYMMDD) 当日 16:00 ET 收盘的年化剩余时间 (年)。已过期返回 0。"""
    if not expiry or len(expiry) != 8:
        return 0.0
    try:
        y, m, d = int(expiry[:4]), int(expiry[4:6]), int(expiry[6:8])
    except ValueError:
        return 0.0
    try:
        import zoneinfo
        tz = zoneinfo.ZoneInfo("America/New_York")
        now = datetime.now(tz)
        expiry_dt = datetime(y, m, d, OPTION_MARKET_CLOSE_ET, 0, 0, tzinfo=tz)
    except Exception:
        now = datetime.now()
        expiry_dt = datetime(y, m, d, OPTION_MARKET_CLOSE_ET, 0, 0)
    secs = (expiry_dt - now).total_seconds()
    return secs / (365.0 * 24.0 * 3600.0) if secs > 0 else 0.0


# ── Widget ──────────────────────────────────────────────────────────────

class OptionCalculator(QWidget):
    """期权理论价计算器面板。"""

    # 2 年期收益率经后台线程从 Yahoo 拉取, 用信号回 GUI 线程更新标签 (值为 % ; <0 表失败)
    _rate2y_ready = pyqtSignal(float)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._engine = None
        self._option: OptionInfo | None = None
        self._mid = 0.0          # 最新盘口中间价 (用于比较)
        self._greeks: dict = {}  # 最新模型 greeks (展示用)
        self._solver_seeded = False  # 右列是否已用实时值初始化 (每次换合约重置)
        self._index_req_ids: list[int] = []   # 大盘指数条订阅 (SPY/SPX/VIX)
        self._indices_subscribed = False      # 当前引擎是否已订阅指数行情
        self._fetching_2y = False             # 2 年期 Yahoo 拉取进行中 (防并发重入)

        self._build_ui()

        self._rate2y_ready.connect(self._on_rate2y_ready)
        # 启动后尽早拉一次 2 年期 (Yahoo, 不依赖 IBKR 连接), 之后随 _update_rates 每 30s 刷新
        QTimer.singleShot(2000, self._fetch_2y_yield)

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._refresh)
        self._timer.start(CALCULATOR_REFRESH_MS)

        # 美债收益率刷新慢 (30s): 利率变动慢, 无需跟随计算器高频刷新
        self._rate_timer = QTimer(self)
        self._rate_timer.timeout.connect(self._update_rates)
        self._rate_timer.start(30_000)

    # ── UI ──────────────────────────────────────────────────────────────

    def _build_ui(self):
        self.setStyleSheet(f"""
            QWidget {{ background-color: {COLOR_BG_PANEL}; color: {COLOR_TEXT}; }}
            QLabel {{ background: transparent; }}
            QDoubleSpinBox {{
                background-color: {COLOR_BG_DARK}; color: {COLOR_TEXT};
                border: 1px solid {COLOR_BORDER}; border-radius: 3px;
                padding: 2px 4px;
            }}
            QDoubleSpinBox:read-only {{ color: {COLOR_TEXT_DIM}; }}
        """)
        root = QVBoxLayout(self)
        root.setContentsMargins(8, 6, 8, 6)
        root.setSpacing(6)

        self._title = QLabel("期权理论价计算器")
        self._title.setStyleSheet(
            f"color: {COLOR_ACCENT}; font-weight: bold; font-size: 13px;"
        )
        root.addWidget(self._title)

        # 两列布局: 左 = 实时理论价计算器, 右 = 反向求解标的价 (what-if)
        columns = QHBoxLayout()
        columns.setSpacing(10)
        left_col = QVBoxLayout()
        left_col.setSpacing(6)
        right_col = QVBoxLayout()
        right_col.setSpacing(6)

        vsep = QFrame()
        vsep.setFrameShape(QFrame.VLine)
        vsep.setStyleSheet(f"color: {COLOR_BORDER};")

        columns.addLayout(left_col, 1)
        columns.addWidget(vsep)
        columns.addLayout(right_col, 1)
        root.addLayout(columns, 1)

        self._build_left_column(left_col)
        self._build_right_column(right_col)

        self._build_index_bar(root)

        self._apply_follow_state(True)
        self._show_placeholder()

    def _build_index_bar(self, root: QVBoxLayout):
        """计算器下方: 实时大盘指数条。
        左侧 SPY / SPX 现价 + 换算关系 (SPX ≈ SPY×10); 右侧 VIX。
        数据来自 __stock__{SPY,SPX,VIX} 行情, 连接后由刷新定时器自动订阅。"""
        sep = QFrame()
        sep.setFrameShape(QFrame.HLine)
        sep.setStyleSheet(f"color: {COLOR_BORDER};")
        root.addWidget(sep)

        bar = QHBoxLayout()
        bar.setSpacing(10)

        # 左: SPY / SPX 现价 + 换算关系
        self._spy_label = QLabel("SPY —")
        self._spx_label = QLabel("SPX —")
        for lab in (self._spy_label, self._spx_label):
            lab.setStyleSheet(
                f"color: {COLOR_TEXT}; font-size: 13px; font-weight: bold;"
            )
        self._conv_label = QLabel("")
        self._conv_label.setStyleSheet(f"color: {COLOR_TEXT_DIM}; font-size: 10px;")
        bar.addWidget(self._spy_label)
        bar.addWidget(self._spx_label)
        bar.addWidget(self._conv_label)
        bar.addStretch(1)

        # 右: VIX (按水平着色: <15 平静绿 / >=25 恐慌红 / 其余中性)
        vix_cap = QLabel("VIX")
        vix_cap.setStyleSheet(f"color: {COLOR_TEXT_DIM}; font-size: 11px;")
        self._vix_label = QLabel("—")
        self._vix_label.setStyleSheet(
            f"color: {COLOR_ACCENT}; font-size: 15px; font-weight: bold;"
        )
        bar.addWidget(vix_cap)
        bar.addWidget(self._vix_label)

        root.addLayout(bar)

        # 第二行: 美债收益率 (2年 / 5年 / 10年)。刷新慢 (30s), 利率变动慢无需高频。
        # 2年走 Yahoo `2YY=F` (延迟行情, 标注「延迟」); 5年/10年走 IBKR CBOE 指数。
        rate_bar = QHBoxLayout()
        rate_bar.setSpacing(10)
        rate_cap = QLabel("美债")
        rate_cap.setStyleSheet(f"color: {COLOR_TEXT_DIM}; font-size: 11px;")
        rate_bar.addWidget(rate_cap)
        self._rate_labels: dict[str, QLabel] = {}

        # 2 年期 (Yahoo, 延迟) — 放在最前作短端档位
        cap2y = QLabel("2年")
        cap2y.setStyleSheet(f"color: {COLOR_TEXT_DIM}; font-size: 11px;")
        self._rate2y_label = QLabel("—")
        self._rate2y_label.setStyleSheet(
            f"color: {COLOR_TEXT}; font-size: 13px; font-weight: bold;"
        )
        self._rate2y_label.setToolTip("2 年期美债收益率 · Yahoo 2YY=F · 延迟行情")
        delay_tag = QLabel("延迟")
        delay_tag.setStyleSheet(f"color: {COLOR_TEXT_DIM}; font-size: 9px;")
        delay_tag.setToolTip("Yahoo Finance 延迟数据 (非实时)")
        rate_bar.addWidget(cap2y)
        rate_bar.addWidget(self._rate2y_label)
        rate_bar.addWidget(delay_tag)

        # 5年 / 10年 (IBKR CBOE 指数)
        for label, sym, _scale in self._RATE_SYMBOLS:
            cap = QLabel(label)
            cap.setStyleSheet(f"color: {COLOR_TEXT_DIM}; font-size: 11px;")
            val = QLabel("—")
            val.setStyleSheet(
                f"color: {COLOR_TEXT}; font-size: 13px; font-weight: bold;"
            )
            self._rate_labels[sym] = val
            rate_bar.addWidget(cap)
            rate_bar.addWidget(val)
        rate_bar.addStretch(1)
        root.addLayout(rate_bar)

    @staticmethod
    def _make_spin(decimals, maximum, step, suffix="", on_change=None):
        # 键盘专用: 滚轮/拖拽不改值 (见 KeyboardOnlySpinBox)
        sp = KeyboardOnlySpinBox()
        sp.setDecimals(decimals)
        sp.setRange(0.0, maximum)
        sp.setSingleStep(step)
        if suffix:
            sp.setSuffix(suffix)
        sp.setButtonSymbols(QDoubleSpinBox.NoButtons)
        if on_change is not None:
            sp.valueChanged.connect(on_change)
        return sp

    def _build_left_column(self, col: QVBoxLayout):
        """左列: 与原计算器一致 — 实时跟随, 正向算理论价。"""
        head = QLabel("正向 · 理论价")
        head.setStyleSheet(f"color: {COLOR_TEXT_DIM}; font-size: 11px; font-weight: bold;")
        col.addWidget(head)

        grid = QGridLayout()
        grid.setHorizontalSpacing(8)
        grid.setVerticalSpacing(4)

        self._s_spin = self._make_spin(2, 1_000_000.0, 0.5, on_change=self._recompute)
        self._k_spin = self._make_spin(2, 1_000_000.0, 1.0, on_change=self._recompute)
        self._iv_spin = self._make_spin(2, 1000.0, 1.0, " %", on_change=self._recompute)
        self._r_spin = self._make_spin(2, 100.0, 0.25, " %", on_change=self._recompute)
        self._days_spin = self._make_spin(4, 3650.0, 1.0, " 天", on_change=self._recompute)
        self._r_spin.setValue(RISK_FREE_RATE * 100.0)

        rows = [
            ("标的价 S", self._s_spin),
            ("行权价 K", self._k_spin),
            ("隐含波动率 IV", self._iv_spin),
            ("无风险利率 r", self._r_spin),
            ("剩余到期", self._days_spin),
        ]
        for i, (label, spin) in enumerate(rows):
            lab = QLabel(label)
            lab.setStyleSheet(f"color: {COLOR_TEXT_DIM};")
            grid.addWidget(lab, i, 0)
            grid.addWidget(spin, i, 1)
        grid.setColumnStretch(1, 1)
        col.addLayout(grid)

        self._follow_chk = QCheckBox("跟随实时行情与时间 (取消可手动试算)")
        self._follow_chk.setChecked(True)
        self._follow_chk.setStyleSheet("color: %s;" % COLOR_TEXT_DIM)
        self._follow_chk.toggled.connect(self._on_follow_toggled)
        col.addWidget(self._follow_chk)

        sep = QFrame()
        sep.setFrameShape(QFrame.HLine)
        sep.setStyleSheet(f"color: {COLOR_BORDER};")
        col.addWidget(sep)

        out = QGridLayout()
        out.setHorizontalSpacing(8)
        out.setVerticalSpacing(3)

        lab_theo = QLabel("理论价")
        lab_theo.setStyleSheet(f"color: {COLOR_TEXT_DIM};")
        self._theo_label = QLabel("—")
        self._theo_label.setStyleSheet(
            f"color: {COLOR_ACCENT}; font-weight: bold; font-size: 20px;"
        )
        out.addWidget(lab_theo, 0, 0)
        out.addWidget(self._theo_label, 0, 1, Qt.AlignRight)

        lab_mid = QLabel("盘口中间价")
        lab_mid.setStyleSheet(f"color: {COLOR_TEXT_DIM};")
        self._mid_label = QLabel("—")
        self._mid_label.setStyleSheet("font-size: 13px;")
        out.addWidget(lab_mid, 1, 0)
        out.addWidget(self._mid_label, 1, 1, Qt.AlignRight)

        lab_val = QLabel("估值")
        lab_val.setStyleSheet(f"color: {COLOR_TEXT_DIM};")
        self._valuation_label = QLabel("—")
        self._valuation_label.setStyleSheet("font-size: 13px; font-weight: bold;")
        out.addWidget(lab_val, 2, 0)
        out.addWidget(self._valuation_label, 2, 1, Qt.AlignRight)
        out.setColumnStretch(1, 1)
        col.addLayout(out)

        self._greeks_label = QLabel("")
        self._greeks_label.setStyleSheet(f"color: {COLOR_TEXT_DIM}; font-size: 11px;")
        self._greeks_label.setWordWrap(True)
        col.addWidget(self._greeks_label)

        col.addStretch(1)

    def _build_right_column(self, col: QVBoxLayout):
        """右列: what-if 试算, 两个方向可切换:
           A「期权价→标的价」: 由目标期权价反推所需标的价 (原功能);
           B「标的价→期权价」: 由假设标的价正算该价位下的期权价 (新增)。
        共享一组参数 (K/IV/r/到期), 仅可变输入与输出标题随模式切换。"""
        head = QLabel("反向 · 试算")
        head.setStyleSheet(f"color: {COLOR_ACCENT}; font-size: 11px; font-weight: bold;")
        col.addWidget(head)

        # 模式切换 (两个方向)
        self._mode_group = QButtonGroup(self)
        self._mode_solve_s = QRadioButton("期权价→标的价")
        self._mode_solve_price = QRadioButton("标的价→期权价")
        self._mode_solve_s.setChecked(True)
        mode_row = QHBoxLayout()
        mode_row.setSpacing(6)
        for rb in (self._mode_solve_s, self._mode_solve_price):
            rb.setStyleSheet(f"color: {COLOR_TEXT_DIM}; font-size: 10px;")
            self._mode_group.addButton(rb)
            mode_row.addWidget(rb)
        mode_row.addStretch(1)
        col.addLayout(mode_row)
        self._mode_solve_s.toggled.connect(self._on_solver_mode_changed)

        self._solver_hint = QLabel("")
        self._solver_hint.setStyleSheet(f"color: {COLOR_TEXT_DIM}; font-size: 10px;")
        self._solver_hint.setWordWrap(True)
        col.addWidget(self._solver_hint)

        grid = QGridLayout()
        grid.setHorizontalSpacing(8)
        grid.setVerticalSpacing(4)

        # 可变输入: 目标期权价 (模式A) / 假设标的价 (模式B) —— 两者叠放同一格, 按模式只显示一个
        self._target_spin = self._make_spin(2, 1_000_000.0, 0.05, on_change=self._solve)
        self._under_spin = self._make_spin(2, 1_000_000.0, 0.5, on_change=self._solve)
        self._target_in_label = QLabel("目标期权价")
        self._under_in_label = QLabel("假设标的价")
        for lab in (self._target_in_label, self._under_in_label):
            lab.setStyleSheet(f"color: {COLOR_TEXT_DIM};")
        grid.addWidget(self._target_in_label, 0, 0)
        grid.addWidget(self._under_in_label, 0, 0)
        grid.addWidget(self._target_spin, 0, 1)
        grid.addWidget(self._under_spin, 0, 1)

        # 共享参数
        self._wk_spin = self._make_spin(2, 1_000_000.0, 1.0, on_change=self._solve)
        self._wiv_spin = self._make_spin(2, 1000.0, 1.0, " %", on_change=self._solve)
        self._wr_spin = self._make_spin(2, 100.0, 0.25, " %", on_change=self._solve)
        self._wdays_spin = self._make_spin(4, 3650.0, 0.5, " 天", on_change=self._solve)
        self._wr_spin.setValue(RISK_FREE_RATE * 100.0)
        params = [
            ("行权价 K", self._wk_spin),
            ("隐含波动率 IV", self._wiv_spin),
            ("无风险利率 r", self._wr_spin),
            ("剩余到期", self._wdays_spin),
        ]
        for i, (label, spin) in enumerate(params, start=1):
            lab = QLabel(label)
            lab.setStyleSheet(f"color: {COLOR_TEXT_DIM};")
            grid.addWidget(lab, i, 0)
            grid.addWidget(spin, i, 1)
        grid.setColumnStretch(1, 1)
        col.addLayout(grid)

        self._sync_btn = QPushButton("↺ 用实时值填充")
        self._sync_btn.setCursor(Qt.PointingHandCursor)
        self._sync_btn.setStyleSheet(f"""
            QPushButton {{
                background-color: {COLOR_BG_DARK}; color: {COLOR_TEXT_DIM};
                border: 1px solid {COLOR_BORDER}; border-radius: 3px; padding: 3px;
                font-size: 11px;
            }}
            QPushButton:hover {{ color: {COLOR_TEXT}; border-color: {COLOR_ACCENT}; }}
        """)
        self._sync_btn.clicked.connect(self._seed_solver_from_live)
        col.addWidget(self._sync_btn)

        sep = QFrame()
        sep.setFrameShape(QFrame.HLine)
        sep.setStyleSheet(f"color: {COLOR_BORDER};")
        col.addWidget(sep)

        out = QGridLayout()
        out.setHorizontalSpacing(8)
        out.setVerticalSpacing(3)

        # 输出标题随模式切换 (A: 所需标的价/当前标的/需变动; B: 期权价/盘口中间价/相对盘口)
        self._out_main_title = QLabel("所需标的价")
        self._out_main_title.setStyleSheet(f"color: {COLOR_TEXT_DIM};")
        self._out_main_value = QLabel("—")
        self._out_main_value.setStyleSheet(
            f"color: {COLOR_ACCENT}; font-weight: bold; font-size: 20px;"
        )
        out.addWidget(self._out_main_title, 0, 0)
        out.addWidget(self._out_main_value, 0, 1, Qt.AlignRight)

        self._out_ref_title = QLabel("当前标的")
        self._out_ref_title.setStyleSheet(f"color: {COLOR_TEXT_DIM};")
        self._out_ref_value = QLabel("—")
        self._out_ref_value.setStyleSheet("font-size: 13px;")
        out.addWidget(self._out_ref_title, 1, 0)
        out.addWidget(self._out_ref_value, 1, 1, Qt.AlignRight)

        self._out_cmp_title = QLabel("需变动")
        self._out_cmp_title.setStyleSheet(f"color: {COLOR_TEXT_DIM};")
        self._out_cmp_value = QLabel("—")
        self._out_cmp_value.setStyleSheet("font-size: 13px; font-weight: bold;")
        out.addWidget(self._out_cmp_title, 2, 0)
        out.addWidget(self._out_cmp_value, 2, 1, Qt.AlignRight)
        out.setColumnStretch(1, 1)
        col.addLayout(out)

        col.addStretch(1)

        # 初始化模式相关的可见性 / 标题 / 提示
        self._on_solver_mode_changed()

    # ── Wiring ───────────────────────────────────────────────────────────

    def set_engine(self, engine):
        # 换引擎 (连接/重连/模拟↔实盘切换) → 旧 reqId 失效, 重新订阅指数行情
        if engine is not self._engine:
            self._indices_subscribed = False
            self._index_req_ids = []
        self._engine = engine

    def set_option(self, option: OptionInfo):
        """主窗口选中新期权时调用。"""
        self._option = option
        self._solver_seeded = False  # 换合约 → 右列重新用实时值播种
        is_opt = option is not None and option.right in ("C", "P")
        self._title.setText(
            f"期权理论价计算器 — {option.display_name}" if option else "期权理论价计算器"
        )
        if not is_opt:
            # 正股伪合约无理论价概念
            self._set_inputs_enabled(False)
            self._theo_label.setText("—")
            self._mid_label.setText("—")
            self._valuation_label.setText("仅期权适用")
            self._valuation_label.setStyleSheet(
                f"color: {COLOR_TEXT_DIM}; font-size: 13px;"
            )
            self._greeks_label.setText("")
            self._out_main_value.setText("—")
            self._out_ref_value.setText("—")
            self._out_cmp_value.setText("仅期权适用")
            self._out_cmp_value.setStyleSheet(f"color: {COLOR_TEXT_DIM}; font-size: 13px;")
            return
        self._set_inputs_enabled(True)
        self._apply_follow_state(self._follow_chk.isChecked())
        block = self._block_spins(True)
        self._k_spin.setValue(option.strike)
        self._wk_spin.setValue(option.strike)
        self._block_spins(block)
        self._refresh()

    # ── Refresh loop ─────────────────────────────────────────────────────

    def _refresh(self):
        """定时刷新: 拉取实时 IV/标的价/盘口, 重算剩余时间, 计算理论价。"""
        # 大盘指数条独立于是否选中期权, 始终刷新
        self._update_indices()

        if self._option is None or self._option.right not in ("C", "P"):
            return

        tick = {}
        if self._engine is not None:
            tick = self._engine.get_tick(self._option.to_ibkr_key()) or {}

        self._mid = self._market_mid(tick)
        self._greeks = {
            "delta": tick.get("delta"), "gamma": tick.get("gamma"),
            "vega": tick.get("vega"), "theta": tick.get("theta"),
        }

        if self._follow_chk.isChecked():
            und = self._live_underlying(tick)
            iv = tick.get("iv", 0.0) or 0.0
            block = self._block_spins(True)
            if und > 0:
                self._s_spin.setValue(und)
            if iv > 0:
                self._iv_spin.setValue(iv * 100.0)
            self._days_spin.setValue(years_to_expiry(self._option.expiry) * 365.0)
            self._block_spins(block)

        # 右列首次拿到有效实时数据时, 自动播种一次 (之后由用户自由编辑)
        if not self._solver_seeded and (tick.get("iv", 0.0) or 0.0) > 0:
            self._seed_solver_from_live()

        self._recompute()
        self._solve()

    # ── 大盘指数条 (SPY / SPX / VIX) ─────────────────────────────────────

    _INDEX_BAR_SYMBOLS = ("SPY", "SPX", "VIX")
    # 美债收益率档位 (走 IBKR CBOE 指数的部分): (显示标签, CBOE 指数代码, 显示换算 scale)。
    # TNX/FVX 指数=收益率×10 → ×0.1 还原为百分比。
    # 短端 2 年期: CBOE 无标准 2 年收益率指数, 改从 Yahoo `2YY=F` (CBOE 2 年期收益率期货,
    # 值即收益率%) 后台拉取, 单列处理 (见 _fetch_2y_yield), 故不在此元组内。
    _RATE_SYMBOLS = (("5年", "FVX", 0.1), ("10年", "TNX", 0.1))
    # Yahoo Finance 2 年期收益率期货 (延迟行情) 的 chart 接口
    _YAHOO_2Y_URL = "https://query1.finance.yahoo.com/v8/finance/chart/2YY=F?interval=1d&range=1d"

    def _ensure_index_subscriptions(self):
        """连接后订阅 SPY/SPX/VIX + 美债收益率(FVX/TNX) 行情 (每个引擎仅订阅一次)。
        均为 CBOE 指数, 若账户无指数行情权限则相应字段保持「—」。
        2 年期不在此列 (走 Yahoo, 见 _fetch_2y_yield)。"""
        if self._indices_subscribed or self._engine is None:
            return
        if not getattr(self._engine, "is_connected", False):
            return
        sub = getattr(self._engine, "subscribe_stock_tick", None)
        if sub is None:
            return
        rate_syms = tuple(s for _, s, _ in self._RATE_SYMBOLS)
        for sym in self._INDEX_BAR_SYMBOLS + rate_syms:
            try:
                self._index_req_ids.append(sub(sym))
            except Exception:
                pass
        self._indices_subscribed = True
        # 30s 定时器首帧要等 30s; 连接后先排两次快速更新, 让利率行尽早出值
        QTimer.singleShot(3000, self._update_rates)
        QTimer.singleShot(8000, self._update_rates)

    def _index_price(self, symbol: str) -> float:
        """指数现价: 优先 last, 回退 bid/ask 中间价。无行情返回 0。"""
        if self._engine is None:
            return 0.0
        t = self._engine.get_tick(f"__stock__{symbol}") or {}
        last = t.get("last", 0.0) or 0.0
        if last > 0:
            return last
        bid = t.get("bid", 0.0) or 0.0
        ask = t.get("ask", 0.0) or 0.0
        if bid > 0 and ask > 0:
            return (bid + ask) / 2.0
        return bid or ask or 0.0

    def _update_indices(self):
        """刷新底部指数条: SPY/SPX 现价 + 换算关系, 右侧 VIX。"""
        self._ensure_index_subscriptions()
        if self._engine is None:
            return

        spy = self._index_price("SPY")
        spx = self._index_price("SPX")
        vix = self._index_price("VIX")

        self._spy_label.setText(f"SPY {spy:.2f}" if spy > 0 else "SPY —")
        self._spx_label.setText(f"SPX {spx:.2f}" if spx > 0 else "SPX —")

        # 换算关系: SPX ≈ SPY×10 (SPY 约为标普指数的 1/10)
        if spy > 0 and spx > 0:
            self._conv_label.setText(
                f"SPX≈SPY×10  (SPY×10={spy * 10:.1f}, 实测 {spx / spy:.2f}×)"
            )
        elif spy > 0:
            self._conv_label.setText(f"SPY×10≈{spy * 10:.1f}")
        else:
            self._conv_label.setText("")

        if vix > 0:
            if vix < 15:
                color = COLOR_GREEN      # 平静
            elif vix >= 25:
                color = COLOR_RED        # 恐慌
            else:
                color = COLOR_ACCENT     # 中性
            self._vix_label.setText(f"{vix:.2f}")
            self._vix_label.setStyleSheet(
                f"color: {color}; font-size: 15px; font-weight: bold;"
            )
        else:
            self._vix_label.setText("—")
            self._vix_label.setStyleSheet(
                f"color: {COLOR_TEXT_DIM}; font-size: 15px; font-weight: bold;"
            )

    def _update_rates(self):
        """刷新美债收益率行 (2年/5年/10年), 30s 一次。无行情/无权限则显示「—」。
        2年走 Yahoo (后台线程); 5年/10年走 IBKR CBOE 指数 (TNX/FVX=收益率×10 → 按 scale 还原)。"""
        # 2 年期: Yahoo 延迟行情, 不依赖 IBKR 连接, 每次刷新都后台拉一次
        self._fetch_2y_yield()
        if self._engine is None or not hasattr(self, "_rate_labels"):
            return
        for _label, sym, scale in self._RATE_SYMBOLS:
            lab = self._rate_labels.get(sym)
            if lab is None:
                continue
            raw = self._index_price(sym)
            if raw > 0:
                lab.setText(f"{raw * scale:.3f}%")
                lab.setStyleSheet(
                    f"color: {COLOR_TEXT}; font-size: 13px; font-weight: bold;"
                )
            else:
                lab.setText("—")
                lab.setStyleSheet(
                    f"color: {COLOR_TEXT_DIM}; font-size: 13px; font-weight: bold;"
                )

    def _fetch_2y_yield(self):
        """后台拉取 2 年期美债收益率 (Yahoo `2YY=F`, 延迟行情)。
        在 daemon 线程发 HTTP, 结果经 _rate2y_ready 信号回 GUI 线程; 失败发 <0。"""
        if self._fetching_2y:
            return
        self._fetching_2y = True

        def work():
            val = -1.0
            try:
                req = urllib.request.Request(
                    self._YAHOO_2Y_URL, headers={"User-Agent": "Mozilla/5.0"}
                )
                with urllib.request.urlopen(req, timeout=6) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                meta = data["chart"]["result"][0]["meta"]
                price = meta.get("regularMarketPrice")
                if price is not None and price > 0:
                    val = float(price)   # 2YY=F 即收益率%, 无需换算
            except Exception:
                val = -1.0
            self._rate2y_ready.emit(val)

        threading.Thread(target=work, daemon=True).start()

    def _on_rate2y_ready(self, value: float):
        """(GUI 线程) 收到 2 年期收益率 → 更新标签。value<0 表拉取失败, 保留上次显示。"""
        self._fetching_2y = False
        if not hasattr(self, "_rate2y_label"):
            return
        if value > 0:
            self._rate2y_label.setText(f"{value:.3f}%")
            self._rate2y_label.setStyleSheet(
                f"color: {COLOR_TEXT}; font-size: 13px; font-weight: bold;"
            )
        elif self._rate2y_label.text() == "—":
            # 仍无有效值 (首拉失败): 保持「—」
            self._rate2y_label.setStyleSheet(
                f"color: {COLOR_TEXT_DIM}; font-size: 13px; font-weight: bold;"
            )

    def _live_underlying(self, opt_tick: dict) -> float:
        """标的实时价。优先用高频的标的 tick (__stock__SYM, 随每笔成交跳动),
        回退到期权模型计算值 undPrice (更新较慢, 故理论价此前显得迟钝)。"""
        if self._engine is not None:
            und_tick = self._engine.get_tick(f"__stock__{self._option.symbol}") or {}
            last = und_tick.get("last", 0.0) or 0.0
            bid = und_tick.get("bid", 0.0) or 0.0
            ask = und_tick.get("ask", 0.0) or 0.0
            if last > 0:
                return last
            if bid > 0 and ask > 0:
                return (bid + ask) / 2.0
        return opt_tick.get("und_price", 0.0) or 0.0

    def _recompute(self):
        if self._option is None or self._option.right not in ("C", "P"):
            return
        S = self._s_spin.value()
        K = self._k_spin.value()
        sigma = self._iv_spin.value() / 100.0
        r = self._r_spin.value() / 100.0
        T = max(self._days_spin.value(), 0.0) / 365.0

        # 盘口中间价
        if self._mid > 0:
            self._mid_label.setText(f"${self._mid:.2f}")
        else:
            self._mid_label.setText("—")

        # 缺少必要输入时给出明确提示, 而非误导性的 0
        if sigma <= 0:
            self._theo_label.setText("—")
            self._valuation_label.setText("等待 IV 行情…")
            self._valuation_label.setStyleSheet(
                f"color: {COLOR_TEXT_DIM}; font-size: 13px;"
            )
            self._update_greeks_label()
            return
        if T <= 0:
            self._theo_label.setText(f"${max(self._intrinsic(S, K), 0):.2f}")
            self._valuation_label.setText("已到期 (仅内在价值)")
            self._valuation_label.setStyleSheet(
                f"color: {COLOR_TEXT_DIM}; font-size: 13px;"
            )
            self._update_greeks_label()
            return

        theo = black_scholes_price(
            S, K, T, r, sigma, self._option.right, DIVIDEND_YIELD
        )
        self._theo_label.setText(f"${theo:.2f}")

        # 估值: 理论价 vs 盘口中间价
        if self._mid > 0 and theo > 0:
            diff = self._mid - theo            # 市场 - 理论
            pct = diff / theo * 100.0
            if diff > 0:   # 市场价高于理论价 → 偏贵 (高估)
                self._valuation_label.setText(f"偏贵 +${diff:.2f} ({pct:+.1f}%)")
                self._valuation_label.setStyleSheet(
                    f"color: {COLOR_RED}; font-size: 13px; font-weight: bold;"
                )
            else:          # 市场价低于理论价 → 偏便宜 (低估)
                self._valuation_label.setText(f"偏便宜 ${diff:.2f} ({pct:+.1f}%)")
                self._valuation_label.setStyleSheet(
                    f"color: {COLOR_GREEN}; font-size: 13px; font-weight: bold;"
                )
        else:
            self._valuation_label.setText("无盘口可比")
            self._valuation_label.setStyleSheet(
                f"color: {COLOR_TEXT_DIM}; font-size: 13px;"
            )

        self._update_greeks_label()

    # ── Reverse solver (right column) ─────────────────────────────────────

    def _on_solver_mode_changed(self, *args):
        """切换试算方向: 调整可变输入的可见性 + 输出标题 + 提示, 并重算。"""
        mode_b = self._mode_solve_price.isChecked()  # True = 标的价→期权价
        self._target_in_label.setVisible(not mode_b)
        self._target_spin.setVisible(not mode_b)
        self._under_in_label.setVisible(mode_b)
        self._under_spin.setVisible(mode_b)
        if mode_b:
            self._solver_hint.setText("设定假设标的价与到期, 算该价位下的期权价")
            self._out_main_title.setText("期权价")
            self._out_ref_title.setText("盘口中间价")
            self._out_cmp_title.setText("相对盘口")
        else:
            self._solver_hint.setText("设定目标期权价与到期, 反推标的需到的价位")
            self._out_main_title.setText("所需标的价")
            self._out_ref_title.setText("当前标的")
            self._out_cmp_title.setText("需变动")
        self._solve()

    def _seed_solver_from_live(self):
        """用左列实时值播种右列: K/IV/r/到期天数; 目标期权价取盘口中间价、
        假设标的价取当前标的, 两个方向都有合理初值。"""
        if self._option is None or self._option.right not in ("C", "P"):
            return
        block = self._block_solver_spins(True)
        self._wk_spin.setValue(self._k_spin.value() or self._option.strike)
        iv = self._iv_spin.value()
        if iv > 0:
            self._wiv_spin.setValue(iv)
        self._wr_spin.setValue(self._r_spin.value())
        self._wdays_spin.setValue(self._days_spin.value())
        if self._mid > 0:
            self._target_spin.setValue(self._mid)
        elif self._theo_value() > 0:
            self._target_spin.setValue(self._theo_value())
        und = self._current_underlying()
        if und > 0:
            self._under_spin.setValue(und)
        self._block_solver_spins(block)
        self._solver_seeded = True
        self._solve()

    def _current_underlying(self) -> float:
        """当前标的实时价 (供右列两个方向共用)。"""
        if self._engine is None or self._option is None:
            return 0.0
        return self._live_underlying(
            self._engine.get_tick(self._option.to_ibkr_key()) or {}
        )

    def _theo_value(self) -> float:
        """左列当前理论价 (数值)，供播种目标价时回退使用。"""
        try:
            txt = self._theo_label.text().lstrip("$")
            return float(txt)
        except (ValueError, AttributeError):
            return 0.0

    def _solve(self):
        """按当前模式分派: A 反推标的价 / B 正算期权价。"""
        if self._option is None or self._option.right not in ("C", "P"):
            return
        if self._mode_solve_price.isChecked():
            self._solve_price()
        else:
            self._solve_underlying()

    def _solve_underlying(self):
        """模式A: 由目标期权价 + (K,IV,r,T) 求所需标的价 S, 并对比当前标的。"""
        target = self._target_spin.value()
        K = self._wk_spin.value()
        sigma = self._wiv_spin.value() / 100.0
        r = self._wr_spin.value() / 100.0
        T = max(self._wdays_spin.value(), 0.0) / 365.0

        # 当前标的价 (用最近一次实时值)
        cur = self._current_underlying()
        self._out_ref_value.setText(f"${cur:.2f}" if cur > 0 else "—")

        if target <= 0 or sigma <= 0 or T <= 0:
            self._out_main_value.setText("—")
            msg = "等待 IV 行情…" if sigma <= 0 else (
                "已到期" if T <= 0 else "设定目标价")
            self._out_cmp_value.setText(msg)
            self._out_cmp_value.setStyleSheet(f"color: {COLOR_TEXT_DIM}; font-size: 13px;")
            return

        solved = solve_underlying_for_price(
            target, K, T, r, sigma, self._option.right, DIVIDEND_YIELD
        )
        if solved <= 0:
            self._out_main_value.setText("无解")
            self._out_cmp_value.setText("目标价超出可达范围")
            self._out_cmp_value.setStyleSheet(f"color: {COLOR_TEXT_DIM}; font-size: 13px;")
            return

        self._out_main_value.setText(f"${solved:.2f}")

        if cur > 0:
            diff = solved - cur
            pct = diff / cur * 100.0
            # CALL 需上涨 / PUT 需下跌为利好方向; 这里直接显示标的需变动方向
            up = diff >= 0
            arrow = "↑" if up else "↓"
            color = COLOR_GREEN if up else COLOR_RED
            self._out_cmp_value.setText(f"{arrow} ${abs(diff):.2f} ({pct:+.1f}%)")
            self._out_cmp_value.setStyleSheet(
                f"color: {color}; font-size: 13px; font-weight: bold;"
            )
        else:
            self._out_cmp_value.setText("无当前标的可比")
            self._out_cmp_value.setStyleSheet(f"color: {COLOR_TEXT_DIM}; font-size: 13px;")

    def _solve_price(self):
        """模式B: 由假设标的价 + (K,IV,r,T) 正算期权价, 并对比当前盘口中间价。"""
        S = self._under_spin.value()
        K = self._wk_spin.value()
        sigma = self._wiv_spin.value() / 100.0
        r = self._wr_spin.value() / 100.0
        T = max(self._wdays_spin.value(), 0.0) / 365.0

        # 参考: 当前盘口中间价 (期权现价)
        self._out_ref_value.setText(f"${self._mid:.2f}" if self._mid > 0 else "—")

        if S <= 0 or sigma <= 0:
            self._out_main_value.setText("—")
            self._out_cmp_value.setText("等待 IV 行情…" if sigma <= 0 else "设定标的价")
            self._out_cmp_value.setStyleSheet(f"color: {COLOR_TEXT_DIM}; font-size: 13px;")
            return
        if T <= 0:
            price = max(self._intrinsic(S, K), 0.0)
            self._out_main_value.setText(f"${price:.2f}")
            self._out_cmp_value.setText("已到期 (仅内在价值)")
            self._out_cmp_value.setStyleSheet(f"color: {COLOR_TEXT_DIM}; font-size: 13px;")
            return

        price = black_scholes_price(
            S, K, T, r, sigma, self._option.right, DIVIDEND_YIELD
        )
        self._out_main_value.setText(f"${price:.2f}")

        # 相对当前盘口: 若标的到该价位, 期权较现价的盈亏方向
        if self._mid > 0 and price > 0:
            diff = price - self._mid
            pct = diff / self._mid * 100.0
            up = diff >= 0
            arrow = "↑" if up else "↓"
            color = COLOR_GREEN if up else COLOR_RED
            self._out_cmp_value.setText(f"{arrow} ${abs(diff):.2f} ({pct:+.1f}%)")
            self._out_cmp_value.setStyleSheet(
                f"color: {color}; font-size: 13px; font-weight: bold;"
            )
        else:
            self._out_cmp_value.setText("无盘口可比")
            self._out_cmp_value.setStyleSheet(f"color: {COLOR_TEXT_DIM}; font-size: 13px;")

    def _block_solver_spins(self, block: bool) -> bool:
        prev = self._target_spin.signalsBlocked()
        for sp in (self._target_spin, self._under_spin, self._wk_spin,
                   self._wiv_spin, self._wr_spin, self._wdays_spin):
            sp.blockSignals(block)
        return prev

    # ── Helpers ──────────────────────────────────────────────────────────

    def _intrinsic(self, S, K):
        if self._option.right == "C":
            return S - K
        return K - S

    def _market_mid(self, tick: dict) -> float:
        bid = tick.get("bid", 0.0) or 0.0
        ask = tick.get("ask", 0.0) or 0.0
        last = tick.get("last", 0.0) or 0.0
        if bid > 0 and ask > 0:
            return (bid + ask) / 2.0
        return last

    def _update_greeks_label(self):
        g = self._greeks
        parts = []
        if g.get("delta") is not None:
            parts.append(f"Δ {g['delta']:.3f}")
        if g.get("gamma") is not None:
            parts.append(f"Γ {g['gamma']:.4f}")
        if g.get("theta") is not None:
            parts.append(f"Θ {g['theta']:.3f}")
        if g.get("vega") is not None:
            parts.append(f"V {g['vega']:.3f}")
        self._greeks_label.setText("  ".join(parts) if parts else "")

    def _show_placeholder(self):
        self._theo_label.setText("—")
        self._mid_label.setText("—")
        self._valuation_label.setText("选择左侧期权后计算")
        self._valuation_label.setStyleSheet(
            f"color: {COLOR_TEXT_DIM}; font-size: 13px;"
        )
        self._out_main_value.setText("—")
        self._out_ref_value.setText("—")
        self._out_cmp_value.setText("选择期权后求解")
        self._out_cmp_value.setStyleSheet(f"color: {COLOR_TEXT_DIM}; font-size: 13px;")

    def _block_spins(self, block: bool) -> bool:
        """统一阻塞/恢复所有输入框信号; 返回 s_spin 之前的阻塞状态以便还原。"""
        prev = self._s_spin.signalsBlocked()
        for sp in (self._s_spin, self._k_spin, self._iv_spin,
                   self._r_spin, self._days_spin):
            sp.blockSignals(block)
        return prev

    def _set_inputs_enabled(self, enabled: bool):
        for sp in (self._s_spin, self._k_spin, self._iv_spin,
                   self._r_spin, self._days_spin,
                   self._target_spin, self._under_spin, self._wk_spin,
                   self._wiv_spin, self._wr_spin, self._wdays_spin):
            sp.setEnabled(enabled)
        self._follow_chk.setEnabled(enabled)
        self._sync_btn.setEnabled(enabled)
        self._mode_solve_s.setEnabled(enabled)
        self._mode_solve_price.setEnabled(enabled)

    def _on_follow_toggled(self, checked: bool):
        self._apply_follow_state(checked)
        self._refresh()

    def _apply_follow_state(self, following: bool):
        """跟随实时模式下, 由行情驱动的字段设为只读 (利率始终可改)。"""
        for sp in (self._s_spin, self._iv_spin, self._days_spin, self._k_spin):
            sp.setReadOnly(following)

    def cleanup(self):
        self._timer.stop()
        self._rate_timer.stop()
        # 退订指数行情线 (SPY/SPX/VIX + 美债 FVX/TNX; 2 年走 Yahoo 无需退订)
        unsub = getattr(self._engine, "unsubscribe_tick", None) if self._engine else None
        if unsub:
            for rid in self._index_req_ids:
                try:
                    unsub(rid)
                except Exception:
                    pass
        self._index_req_ids = []
