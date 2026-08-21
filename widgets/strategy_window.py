"""Strategy Builder — multi-leg option combo orders.

`StrategyPanel` is an embeddable QWidget used as the「多腿组合」tab inside the
main window (independent of the single-leg price ladder). `StrategyWindow` is a
thin QMainWindow wrapper kept for standalone / backward-compatible use.
"""

import os
import json
import time
import threading

from PyQt5.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QGridLayout,
    QLabel, QComboBox, QSpinBox, QPushButton, QDoubleSpinBox,
    QTableWidget, QTableWidgetItem, QHeaderView, QCheckBox,
    QMessageBox, QGroupBox,
)
from PyQt5.QtCore import Qt, QTimer, pyqtSignal
from PyQt5.QtGui import QFont

from config import (
    COLOR_BG, COLOR_BG_DARK, COLOR_BG_PANEL, COLOR_TEXT,
    COLOR_BORDER, COLOR_ACCENT, COLOR_GREEN, COLOR_RED,
    COLOR_BUY, COLOR_SELL, COMMISSION_PER_CONTRACT, COMMISSION_MIN,
    COLOR_TEXT_DIM, COLOR_BUTTON_DISABLED, COLOR_ACCENT_HOVER,
)
from models import OptionInfo, ComboLegInfo

from widgets.strategy_defs import (
    StrategyType, LegTemplate, StrategyTemplate, STRATEGY_REGISTRY,
)
from widgets.ui_util import disable_ime


WINDOW_STYLESHEET = f"""
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
    QHeaderView::section {{
        background-color: {COLOR_BG_PANEL};
        color: {COLOR_TEXT};
        border: 1px solid {COLOR_BORDER};
        padding: 4px;
        font-weight: bold;
    }}
    QComboBox, QSpinBox, QDoubleSpinBox, QLineEdit {{
        background-color: {COLOR_BG_DARK};
        color: {COLOR_TEXT};
        border: 1px solid {COLOR_BORDER};
        padding: 4px 8px;
        border-radius: 3px;
    }}
    QComboBox::drop-down {{ border: none; }}
    QComboBox QAbstractItemView {{
        background-color: {COLOR_BG_DARK};
        color: {COLOR_TEXT};
        selection-background-color: {COLOR_BG_PANEL};
    }}
    QLabel {{ color: {COLOR_TEXT}; }}
    QGroupBox {{
        color: {COLOR_ACCENT};
        border: 1px solid {COLOR_BORDER};
        border-radius: 4px;
        margin-top: 8px;
        padding-top: 16px;
        font-weight: bold;
    }}
    QGroupBox::title {{
        subcontrol-origin: margin;
        left: 10px;
        padding: 0 4px;
    }}
    QCheckBox {{ color: {COLOR_TEXT}; }}
    QCheckBox::indicator {{
        width: 16px; height: 16px;
        border: 1px solid {COLOR_BORDER};
        border-radius: 3px;
        background-color: {COLOR_BG_DARK};
    }}
    QCheckBox::indicator:checked {{
        background-color: {COLOR_ACCENT};
    }}
"""


class StrategyPanel(QWidget):
    """Multi-leg option strategy builder and order placer (embeddable widget)."""

    def __init__(self, engine=None, symbol: str = "SPY", parent=None):
        super().__init__(parent)
        self._engine = engine
        self._symbol = symbol

        # Data
        self._expirations: list[str] = []
        self._strikes: list[float] = []
        self._legs: list[ComboLegInfo] = []
        self._tick_req_ids: list[int] = []
        self._refresh_timer: QTimer | None = None
        self._loaded = False  # 是否已加载期权链 (懒加载: 首次进入该 Tab 才拉)

        # 已开组合记录 (供一键平仓): 持久化 strategy_combos.json (gitignore)
        self._open_combos: list[dict] = []
        self._combos_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "strategy_combos.json")

        self._build_ui()
        disable_ime(self)   # 见 ui_util: 别让搜狗挂上来
        self.setStyleSheet(WINDOW_STYLESHEET)
        self._load_combos()
        self._render_combos()

        # 跟踪组合订单状态: 成交 → 标「持仓中」; 撤单/拒单 → 移除记录
        bridge = getattr(engine, "bridge", None)
        if bridge is not None:
            bridge.order_status_changed.connect(self._on_combo_order_status)

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)

        # ── Row 1: Symbol + Strategy selector ──
        row1 = QHBoxLayout()
        row1.addWidget(QLabel("标的:"))
        self._symbol_label = QLabel(self._symbol)
        self._symbol_label.setStyleSheet(
            f"font-weight: bold; font-size: 14px; color: {COLOR_ACCENT};"
        )
        row1.addWidget(self._symbol_label)
        row1.addSpacing(16)

        row1.addWidget(QLabel("策略:"))
        self._strategy_combo = QComboBox()
        for st in StrategyType:
            tmpl = STRATEGY_REGISTRY[st]
            self._strategy_combo.addItem(tmpl.display_name, st)
        self._strategy_combo.setMinimumWidth(260)
        self._strategy_combo.currentIndexChanged.connect(self._on_strategy_changed)
        row1.addWidget(self._strategy_combo, stretch=1)
        layout.addLayout(row1)

        # Description
        self._desc_label = QLabel("")
        self._desc_label.setStyleSheet(f"color: {COLOR_TEXT_DIM}; font-size: 11px;")
        self._desc_label.setWordWrap(True)
        layout.addWidget(self._desc_label)

        # ── Row 2: Config panel (dynamic strikes/expiries) ──
        self._config_group = QGroupBox("参数配置")
        self._config_layout = QGridLayout()
        self._config_group.setLayout(self._config_layout)
        layout.addWidget(self._config_group)

        # These get rebuilt dynamically
        self._expiry_combos: dict[str, QComboBox] = {}
        self._strike_combos: dict[str, QComboBox] = {}
        self._qty_spin: QSpinBox | None = None
        self._right_combo: QComboBox | None = None  # For calendar spread

        # ── Row 3: Legs table ──
        self._legs_group = QGroupBox("组合腿明细")
        legs_layout = QVBoxLayout()
        self._legs_group.setLayout(legs_layout)

        self._legs_table = QTableWidget(0, 8)
        self._legs_table.setHorizontalHeaderLabels(
            ["#", "方向", "类型", "行权价", "到期日", "比例", "Bid", "Ask"]
        )
        self._legs_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self._legs_table.verticalHeader().setVisible(False)
        self._legs_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self._legs_table.setSelectionBehavior(QTableWidget.SelectRows)
        self._legs_table.setAlternatingRowColors(True)
        self._legs_table.setMaximumHeight(180)
        legs_layout.addWidget(self._legs_table)

        # Custom mode buttons
        custom_btn_row = QHBoxLayout()
        self._add_leg_btn = QPushButton("+ 添加腿")
        self._add_leg_btn.clicked.connect(self._on_add_custom_leg)
        self._remove_leg_btn = QPushButton("- 删除腿")
        self._remove_leg_btn.clicked.connect(self._on_remove_custom_leg)
        self._add_leg_btn.setVisible(False)
        self._remove_leg_btn.setVisible(False)
        custom_btn_row.addWidget(self._add_leg_btn)
        custom_btn_row.addWidget(self._remove_leg_btn)
        custom_btn_row.addStretch()
        legs_layout.addLayout(custom_btn_row)

        layout.addWidget(self._legs_group)

        # ── Row 4: Summary ──
        summary_layout = QHBoxLayout()
        self._net_label = QLabel("Net: --")
        self._net_label.setStyleSheet("font-size: 13px; font-weight: bold;")
        summary_layout.addWidget(self._net_label)
        summary_layout.addSpacing(20)

        self._maxloss_label = QLabel("Max Loss: --")
        summary_layout.addWidget(self._maxloss_label)
        summary_layout.addSpacing(20)

        self._maxgain_label = QLabel("Max Gain: --")
        summary_layout.addWidget(self._maxgain_label)
        summary_layout.addSpacing(20)

        self._commission_label = QLabel("Commission: --")
        summary_layout.addWidget(self._commission_label)
        summary_layout.addStretch()
        layout.addLayout(summary_layout)

        # ── Row 5: Order controls ──
        order_layout = QHBoxLayout()

        order_layout.addWidget(QLabel("方向:"))
        self._action_combo = QComboBox()
        self._action_combo.addItems(["BUY", "SELL"])
        self._action_combo.setFixedWidth(80)
        order_layout.addWidget(self._action_combo)

        order_layout.addSpacing(10)
        order_layout.addWidget(QLabel("数量:"))
        self._order_qty_spin = QSpinBox()
        self._order_qty_spin.setRange(1, 999)
        self._order_qty_spin.setValue(1)
        self._order_qty_spin.setFixedWidth(70)
        order_layout.addWidget(self._order_qty_spin)

        order_layout.addSpacing(10)
        order_layout.addWidget(QLabel("限价:"))
        self._limit_spin = QDoubleSpinBox()
        self._limit_spin.setRange(-99.99, 999.99)
        self._limit_spin.setDecimals(2)
        self._limit_spin.setSingleStep(0.01)
        self._limit_spin.setFixedWidth(90)
        order_layout.addWidget(self._limit_spin)

        # 默认**不**勾: 保证成交 (guaranteed) = TWS 原生组合单的默认行为, IBKR 按
        # 价差整体算保证金。勾上则允许 SMART 拆腿分别成交, 但保证金按每条腿独立算
        # (卖腿当裸空), 小账户的价差单会被直接拒掉。详见 engine.place_combo_order。
        self._non_guar_cb = QCheckBox("允许拆腿成交")
        self._non_guar_cb.setChecked(False)
        self._non_guar_cb.setToolTip(
            "不勾 (默认): 保证成交 —— 多腿必须一起成交, IBKR 按价差整体算保证金。\n"
            "勾上: 允许 SMART 把各腿拆开分别成交, 可能只成交一条腿 (裸腿风险),\n"
            "且保证金按每条腿独立计算 —— 小账户的价差单常被拒 (UNCOVERED / 保证金不足)。"
        )
        order_layout.addWidget(self._non_guar_cb)

        order_layout.addSpacing(10)
        self._outside_rth_cb = QCheckBox("盘外交易")
        order_layout.addWidget(self._outside_rth_cb)

        order_layout.addStretch()

        self._place_btn = QPushButton("下单组合")
        self._place_btn.setFixedSize(140, 36)
        self._place_btn.setStyleSheet(
            f"QPushButton {{ background-color: {COLOR_ACCENT}; color: {COLOR_BG}; "
            f"font-weight: bold; font-size: 13px; border-radius: 4px; }}"
            f"QPushButton:hover {{ background-color: {COLOR_ACCENT_HOVER}; }}"
            f"QPushButton:disabled {{ background-color: {COLOR_BUTTON_DISABLED}; "
            f"color: {COLOR_TEXT_DIM}; }}"
        )
        self._place_btn.clicked.connect(self._on_place_order)
        order_layout.addWidget(self._place_btn)

        layout.addLayout(order_layout)

        # Status
        self._status_label = QLabel("请等待期权链加载...")
        self._status_label.setStyleSheet(f"color: {COLOR_TEXT_DIM}; font-size: 11px;")
        layout.addWidget(self._status_label)

        # ── Row 6: 已开组合 (一键平仓) ──
        self._combos_group = QGroupBox("已开组合 (一键平仓)")
        self._combos_layout = QVBoxLayout()
        self._combos_group.setLayout(self._combos_layout)
        self._combos_group.setVisible(False)
        layout.addWidget(self._combos_group)

    # ── Public Interface ──────────────────────────────────────────────

    def set_engine(self, engine):
        """切换当前引擎 (连接 / 真实↔模拟热切换时由主窗口调用)。"""
        self._engine = engine

    def set_symbol(self, symbol: str):
        """切换标的 (跟随主窗口顶栏)。重置已加载状态; 若该 Tab 正显示则立即重载。"""
        if symbol == self._symbol:
            return
        self._symbol = symbol
        self._symbol_label.setText(symbol)
        self._unsubscribe_all_ticks()
        self._legs.clear()
        self._update_legs_table()
        self._update_summary()
        self._loaded = False
        self._status_label.setText("标的已切换 — 切到本页将重新加载期权链")
        if self.isVisible() and self._engine is not None:
            self.load_chain()

    def ensure_loaded(self):
        """懒加载: 首次进入「多腿组合」Tab 时加载期权链 (已加载则跳过)。"""
        if self._loaded or self._engine is None:
            return
        self.load_chain()

    def load_chain(self):
        """后台加载当前标的的期权链。"""
        if self._engine is None:
            self._status_label.setText("未连接 — 无法加载期权链")
            return
        self._loaded = True
        self._status_label.setText("正在加载期权链...")
        self._place_btn.setEnabled(False)

        def do_load():
            try:
                exps, strikes = self._engine.request_option_chain(self._symbol)
                self._expirations = exps
                self._strikes = strikes
                # Signal back to GUI thread via timer trick
                QTimer.singleShot(0, self._on_chain_loaded)
            except Exception as e:
                self._expirations = []
                self._strikes = []
                self._loaded = False  # 允许重试
                QTimer.singleShot(0, lambda: self._status_label.setText(
                    f"加载失败: {e}"
                ))

        threading.Thread(target=do_load, daemon=True).start()

    def cleanup(self):
        """Stop timers and unsubscribe tick data."""
        if self._refresh_timer:
            self._refresh_timer.stop()
            self._refresh_timer = None
        self._unsubscribe_all_ticks()

    # ── Chain Loaded ──────────────────────────────────────────────────

    def _on_chain_loaded(self):
        """Called on GUI thread after option chain data arrives."""
        if not self._expirations:
            self._status_label.setText("期权链为空")
            return

        self._status_label.setText(
            f"已加载: {len(self._expirations)} 个到期日, "
            f"{len(self._strikes)} 个行权价"
        )

        # Build config for the currently selected strategy
        self._on_strategy_changed()

    # ── Strategy Changed ──────────────────────────────────────────────

    def _on_strategy_changed(self):
        """Rebuild config panel when strategy selection changes."""
        st = self._strategy_combo.currentData()
        if st is None:
            return
        tmpl = STRATEGY_REGISTRY[st]
        self._desc_label.setText(tmpl.description)

        # Show/hide custom leg buttons
        is_custom = (st == StrategyType.CUSTOM)
        self._add_leg_btn.setVisible(is_custom)
        self._remove_leg_btn.setVisible(is_custom)

        # Clear old config widgets
        while self._config_layout.count():
            item = self._config_layout.takeAt(0)
            w = item.widget()
            if w:
                w.deleteLater()
        self._expiry_combos.clear()
        self._strike_combos.clear()
        self._right_combo = None

        if not self._expirations:
            return

        col = 0

        # Expiry combos
        for ep in tmpl.expiry_params:
            label_text = "到期日:" if len(tmpl.expiry_params) == 1 else (
                "近月:" if ep == "expiry_near" else "远月:"
            )
            label = QLabel(label_text)
            combo = QComboBox()
            combo.setMinimumWidth(120)
            for exp in self._expirations:
                # Format: "20260611" -> "2026-06-11"
                display = f"{exp[:4]}-{exp[4:6]}-{exp[6:]}" if len(exp) == 8 else exp
                combo.addItem(display, exp)
            # Default: near-month for "expiry_near" / first for single
            if ep == "expiry_far" and combo.count() > 1:
                combo.setCurrentIndex(1)
            combo.currentIndexChanged.connect(self._on_config_changed)
            self._expiry_combos[ep] = combo
            self._config_layout.addWidget(label, 0, col)
            self._config_layout.addWidget(combo, 1, col)
            col += 1

        # Right selector for calendar spread
        if st == StrategyType.CALENDAR_SPREAD:
            label = QLabel("类型:")
            self._right_combo = QComboBox()
            self._right_combo.addItems(["C (Call)", "P (Put)"])
            self._right_combo.setFixedWidth(90)
            self._right_combo.currentIndexChanged.connect(self._on_config_changed)
            self._config_layout.addWidget(label, 0, col)
            self._config_layout.addWidget(self._right_combo, 1, col)
            col += 1

        # Strike combos
        strike_labels = {
            1: ["行权价:"],
            2: ["低行权价:", "高行权价:"],
            3: ["低行权价:", "中行权价:", "高行权价:"],
            4: ["最低:", "中低:", "中高:", "最高:"],
        }
        n = len(tmpl.strike_params)
        labels = strike_labels.get(n, [f"Strike{i+1}:" for i in range(n)])

        for i, sp in enumerate(tmpl.strike_params):
            label = QLabel(labels[i] if i < len(labels) else f"{sp}:")
            combo = QComboBox()
            combo.setMinimumWidth(90)
            for s in self._strikes:
                strike_str = f"{int(s)}" if s == int(s) else f"{s:g}"
                combo.addItem(strike_str, s)
            # Default selection: try to pick strikes near the middle
            mid_idx = len(self._strikes) // 2
            offsets = {
                "strike1": -2, "strike2": 0, "strike3": 2, "strike4": 4,
            }
            default_idx = mid_idx + offsets.get(sp, 0)
            default_idx = max(0, min(default_idx, combo.count() - 1))
            combo.setCurrentIndex(default_idx)
            combo.currentIndexChanged.connect(self._on_config_changed)
            self._strike_combos[sp] = combo
            self._config_layout.addWidget(label, 0, col)
            self._config_layout.addWidget(combo, 1, col)
            col += 1

        # Quantity (for ratio display only — order qty is separate)
        if not is_custom:
            self._on_config_changed()

    # ── Config Changed → Build Legs ──────────────────────────────────

    def _on_config_changed(self):
        """Rebuild legs from current config, subscribe to ticks."""
        st = self._strategy_combo.currentData()
        if st is None or st == StrategyType.CUSTOM:
            return
        tmpl = STRATEGY_REGISTRY[st]

        # Resolve expiries
        expiry_map: dict[str, str] = {}
        for ep, combo in self._expiry_combos.items():
            val = combo.currentData()
            if val:
                expiry_map[ep] = val

        # Resolve strikes
        strike_map: dict[str, float] = {}
        for sp, combo in self._strike_combos.items():
            val = combo.currentData()
            if val is not None:
                strike_map[sp] = val

        # Calendar spread: apply right from selector
        right_override = None
        if self._right_combo:
            right_override = "C" if self._right_combo.currentIndex() == 0 else "P"

        # Build legs
        self._unsubscribe_all_ticks()
        self._legs.clear()

        for lt in tmpl.legs:
            strike = strike_map.get(lt.strike_param)
            expiry = expiry_map.get(lt.expiry_param)
            if strike is None or expiry is None:
                continue

            right = right_override if right_override else lt.right
            leg = ComboLegInfo(
                con_id=0,
                symbol=self._symbol,
                expiry=expiry,
                strike=strike,
                right=right,
                action=lt.action,
                ratio=lt.ratio,
            )
            self._legs.append(leg)

        # Subscribe to tick data for each unique leg
        self._subscribe_leg_ticks()
        self._update_legs_table()
        self._update_summary()

        # Start refresh timer
        if not self._refresh_timer:
            self._refresh_timer = QTimer()
            self._refresh_timer.timeout.connect(self._refresh_tick_data)
            self._refresh_timer.start(1000)

        self._place_btn.setEnabled(len(self._legs) >= 2)

    # ── Custom Legs ──────────────────────────────────────────────────

    def _on_add_custom_leg(self):
        """Add a custom leg to the combo."""
        if not self._expirations or not self._strikes:
            return

        # Use first available expiry and middle strike
        expiry = self._expirations[0]
        strike = self._strikes[len(self._strikes) // 2]

        leg = ComboLegInfo(
            con_id=0,
            symbol=self._symbol,
            expiry=expiry,
            strike=strike,
            right="C",
            action="BUY",
            ratio=1,
        )
        self._legs.append(leg)
        self._subscribe_single_leg_tick(leg)
        self._update_legs_table()
        self._update_summary()
        self._place_btn.setEnabled(len(self._legs) >= 2)

    def _on_remove_custom_leg(self):
        """Remove last custom leg."""
        if not self._legs:
            return
        self._legs.pop()
        # Resubscribe all
        self._unsubscribe_all_ticks()
        self._subscribe_leg_ticks()
        self._update_legs_table()
        self._update_summary()
        self._place_btn.setEnabled(len(self._legs) >= 2)

    # ── Tick Data ─────────────────────────────────────────────────────

    def _subscribe_leg_ticks(self):
        """Subscribe to tick data for all legs."""
        seen_keys: set[str] = set()
        for leg in self._legs:
            key = f"{leg.symbol}_{leg.expiry}_{leg.right}_{leg.strike}"
            if key in seen_keys:
                continue
            seen_keys.add(key)
            self._subscribe_single_leg_tick(leg)

    def _subscribe_single_leg_tick(self, leg: ComboLegInfo):
        """Subscribe to tick data for one leg."""
        option = OptionInfo(
            symbol=leg.symbol,
            expiry=leg.expiry,
            strike=leg.strike,
            right=leg.right,
        )
        try:
            req_id = self._engine.subscribe_option_tick(option)
            self._tick_req_ids.append(req_id)
        except Exception:
            pass

    def _unsubscribe_all_ticks(self):
        """Cancel all tick subscriptions."""
        for req_id in self._tick_req_ids:
            try:
                self._engine.unsubscribe_tick(req_id)
            except Exception:
                pass
        self._tick_req_ids.clear()

    def _refresh_tick_data(self):
        """Update leg prices from tick data and refresh display."""
        changed = False
        for leg in self._legs:
            key = f"{leg.symbol}_{leg.expiry}_{leg.right}_{leg.strike}"
            tick = self._engine.get_tick(key)
            new_bid = tick.get("bid", 0.0)
            new_ask = tick.get("ask", 0.0)
            new_last = tick.get("last", 0.0)
            if new_bid != leg.bid or new_ask != leg.ask or new_last != leg.last:
                leg.bid = new_bid
                leg.ask = new_ask
                leg.last = new_last
                changed = True

        if changed:
            self._update_legs_table()
            self._update_summary()

    # ── UI Updates ────────────────────────────────────────────────────

    def _update_legs_table(self):
        """Refresh the legs table from self._legs."""
        self._legs_table.setRowCount(len(self._legs))
        for i, leg in enumerate(self._legs):
            items = [
                str(i + 1),
                leg.action,
                leg.right,
                f"{int(leg.strike)}" if leg.strike == int(leg.strike) else f"{leg.strike:g}",
                f"{leg.expiry[:4]}-{leg.expiry[4:6]}-{leg.expiry[6:]}" if len(leg.expiry) == 8 else leg.expiry,
                str(leg.ratio),
                f"{leg.bid:.2f}" if leg.bid > 0 else "--",
                f"{leg.ask:.2f}" if leg.ask > 0 else "--",
            ]
            for j, text in enumerate(items):
                item = QTableWidgetItem(text)
                item.setTextAlignment(Qt.AlignCenter)
                # Color action column
                if j == 1:
                    item.setForeground(
                        Qt.green if leg.action == "BUY"
                        else Qt.red
                    )
                self._legs_table.setItem(i, j, item)

    def _update_summary(self):
        """Calculate and display net cost, max loss/gain, commission."""
        if not self._legs:
            self._net_label.setText("Net: --")
            self._maxloss_label.setText("Max Loss: --")
            self._maxgain_label.setText("Max Gain: --")
            self._commission_label.setText("Commission: --")
            self._limit_spin.setValue(0.0)
            return

        st = self._strategy_combo.currentData()
        tmpl = STRATEGY_REGISTRY.get(st) if st else None

        # Calculate net debit/credit using mid prices
        net = 0.0
        has_prices = True
        for leg in self._legs:
            mid = (leg.bid + leg.ask) / 2 if leg.bid > 0 and leg.ask > 0 else leg.last
            if mid <= 0:
                has_prices = False
            sign = 1 if leg.action == "BUY" else -1
            net += sign * mid * leg.ratio

        if has_prices:
            net_rounded = round(net, 2)
            if net > 0:
                self._net_label.setText(f"Net: ${net_rounded:.2f} (Debit)")
                self._net_label.setStyleSheet(
                    f"font-size: 13px; font-weight: bold; color: {COLOR_RED};"
                )
            else:
                self._net_label.setText(f"Net: ${abs(net_rounded):.2f} (Credit)")
                self._net_label.setStyleSheet(
                    f"font-size: 13px; font-weight: bold; color: {COLOR_GREEN};"
                )
            self._limit_spin.setValue(abs(net_rounded))
        else:
            self._net_label.setText("Net: -- (等待行情)")
            self._net_label.setStyleSheet(
                f"font-size: 13px; font-weight: bold; color: {COLOR_TEXT};"
            )

        # Max loss / gain estimates for common strategies
        max_loss_str = "--"
        max_gain_str = "--"
        if has_prices and tmpl and len(self._legs) >= 2:
            strikes = sorted(set(leg.strike for leg in self._legs))
            if st in (StrategyType.BULL_CALL_SPREAD, StrategyType.BEAR_PUT_SPREAD):
                if len(strikes) == 2:
                    width = abs(strikes[1] - strikes[0])
                    max_loss = abs(net) * 100
                    max_gain = (width - abs(net)) * 100
                    max_loss_str = f"${max_loss:.0f}"
                    max_gain_str = f"${max_gain:.0f}"
            elif st in (StrategyType.BEAR_CALL_SPREAD, StrategyType.BULL_PUT_SPREAD):
                if len(strikes) == 2:
                    width = abs(strikes[1] - strikes[0])
                    credit = abs(net)
                    max_gain = credit * 100
                    max_loss = (width - credit) * 100
                    max_loss_str = f"${max_loss:.0f}"
                    max_gain_str = f"${max_gain:.0f}"
            elif st == StrategyType.STRADDLE:
                max_loss = abs(net) * 100
                max_loss_str = f"${max_loss:.0f}"
                max_gain_str = "Unlimited"
            elif st == StrategyType.STRANGLE:
                max_loss = abs(net) * 100
                max_loss_str = f"${max_loss:.0f}"
                max_gain_str = "Unlimited"
            elif st in (StrategyType.IRON_CONDOR, StrategyType.IRON_BUTTERFLY):
                if len(strikes) >= 2:
                    # Approximate: width of one side minus credit
                    credit = abs(net)
                    # Use narrower wing width
                    sorted_strikes = sorted(leg.strike for leg in self._legs)
                    if len(sorted_strikes) >= 4:
                        wing_width = min(
                            sorted_strikes[1] - sorted_strikes[0],
                            sorted_strikes[3] - sorted_strikes[2],
                        )
                    elif len(sorted_strikes) >= 3:
                        wing_width = sorted_strikes[-1] - sorted_strikes[0]
                        wing_width = wing_width / 2
                    else:
                        wing_width = 0
                    max_gain = credit * 100
                    max_loss = (wing_width - credit) * 100 if wing_width > credit else 0
                    max_loss_str = f"${max_loss:.0f}"
                    max_gain_str = f"${max_gain:.0f}"

        self._maxloss_label.setText(f"Max Loss: {max_loss_str}")
        self._maxgain_label.setText(f"Max Gain: {max_gain_str}")

        # Commission
        total_contracts = sum(leg.ratio for leg in self._legs)
        qty = self._order_qty_spin.value()
        commission = max(COMMISSION_PER_CONTRACT * total_contracts * qty, COMMISSION_MIN)
        self._commission_label.setText(
            f"Commission: ${commission:.2f} ({total_contracts}x{qty} legs x ${COMMISSION_PER_CONTRACT})"
        )

    # ── Place Order ──────────────────────────────────────────────────

    def _on_place_order(self):
        """Resolve conIds, then place combo order."""
        if not self._legs or len(self._legs) < 2:
            QMessageBox.warning(self, "错误", "至少需要2条腿")
            return

        action = self._action_combo.currentText()
        qty = self._order_qty_spin.value()
        limit_price = self._limit_spin.value()

        # Confirm
        st = self._strategy_combo.currentData()
        tmpl = STRATEGY_REGISTRY.get(st) if st else None
        name = tmpl.display_name if tmpl else "Custom"
        legs_desc = "\n".join(
            f"  {l.action} {l.ratio}x {l.right} {l.strike} ({l.expiry})"
            for l in self._legs
        )
        msg = (
            f"策略: {name}\n"
            f"方向: {action}\n"
            f"数量: {qty}\n"
            f"限价: ${limit_price:.2f}\n"
            f"腿:\n{legs_desc}\n\n"
            f"确认下单?"
        )
        reply = QMessageBox.question(
            self, "确认组合订单", msg,
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
        )
        if reply != QMessageBox.Yes:
            return

        self._place_btn.setEnabled(False)
        self._place_btn.setText("解析合约中...")
        self._status_label.setText("正在解析合约ID...")

        outside = self._outside_rth_cb.isChecked()
        non_guar = self._non_guar_cb.isChecked()

        def do_resolve_and_place():
            try:
                # Resolve conId for each leg
                for leg in self._legs:
                    con_id = self._engine.resolve_option_con_id(
                        leg.symbol, leg.expiry, leg.strike, leg.right
                    )
                    leg.con_id = con_id

                # Place the combo order
                order_id = self._engine.place_combo_order(
                    self._symbol, self._legs, action, qty,
                    limit_price, outside, non_guaranteed=non_guar,
                )

                # 记录快照 (con_id 已解析), 供「一键平仓」反向 BAG 单用
                record = {
                    "order_id": order_id, "status": "已提交",
                    "name": name, "symbol": self._symbol,
                    "action": action, "qty": qty, "outside": outside,
                    "time": time.strftime("%m-%d %H:%M"),
                    "legs": [{"con_id": l.con_id, "symbol": l.symbol,
                              "expiry": l.expiry, "strike": l.strike,
                              "right": l.right, "action": l.action,
                              "ratio": l.ratio} for l in self._legs],
                }
                QTimer.singleShot(
                    0, lambda: self._on_order_placed(order_id, record))

            except Exception as e:
                QTimer.singleShot(0, lambda: self._on_order_error(str(e)))

        threading.Thread(target=do_resolve_and_place, daemon=True).start()

    def _on_order_placed(self, order_id: int, record: dict = None):
        """Order placed successfully."""
        self._place_btn.setEnabled(True)
        self._place_btn.setText("下单组合")
        if order_id > 0:
            self._status_label.setText(f"订单已提交! orderId={order_id}")
            self._status_label.setStyleSheet(
                f"color: {COLOR_GREEN}; font-size: 11px;"
            )
            if record is not None:
                self._open_combos.append(record)
                self._save_combos()
                self._render_combos()
        else:
            self._status_label.setText("下单失败")
            self._status_label.setStyleSheet(
                f"color: {COLOR_RED}; font-size: 11px;"
            )

    def _on_order_error(self, error: str):
        """Order placement failed."""
        self._place_btn.setEnabled(True)
        self._place_btn.setText("下单组合")
        self._status_label.setText(f"错误: {error}")
        self._status_label.setStyleSheet(
            f"color: {COLOR_RED}; font-size: 11px;"
        )
        QMessageBox.critical(self, "下单失败", error)

    # ── 已开组合 (一键平仓) ───────────────────────────────────────────

    def _on_combo_order_status(self, order_id: int, status: str,
                               filled: float, remaining: float, avg: float):
        """跟踪组合订单: 成交 → 标「持仓中」; 开仓单被撤/拒 → 移除记录。"""
        changed = False
        for rec in list(self._open_combos):
            if rec["order_id"] != order_id:
                continue
            if status == "Filled" and rec["status"] != "持仓中":
                rec["status"] = "持仓中"
                changed = True
            elif (status in ("Cancelled", "ApiCancelled", "Inactive")
                  and rec["status"] == "已提交"):
                self._open_combos.remove(rec)   # 未成交即被撤/拒 → 无仓可平
                changed = True
        if changed:
            self._save_combos()
            self._render_combos()

    def _render_combos(self):
        while self._combos_layout.count():
            item = self._combos_layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()
        self._combos_group.setVisible(bool(self._open_combos))
        for rec in self._open_combos:
            row = QWidget()
            h = QHBoxLayout(row)
            h.setContentsMargins(0, 0, 0, 0)
            h.setSpacing(6)
            legs_s = " + ".join(
                f"{l['action'][0]}{l['ratio']}x{l['right']}{l['strike']:g}"
                for l in rec["legs"])
            col = COLOR_GREEN if rec["status"] == "持仓中" else COLOR_TEXT_DIM
            lbl = QLabel(f"[{rec['status']}] {rec['time']} {rec['symbol']} "
                         f"{rec['name']} {rec['action']}×{rec['qty']}: {legs_s}")
            lbl.setStyleSheet(f"color: {col}; font-size: 11px;")
            lbl.setToolTip("B=BUY / S=SELL, 比例x类型行权价")
            h.addWidget(lbl, stretch=1)
            btn = QPushButton("一键平仓")
            btn.setFixedHeight(24)
            btn.setCursor(Qt.PointingHandCursor)
            btn.setToolTip("反向市价 BAG 单整体平掉该组合 (多腿一张单同时成交)")
            btn.clicked.connect(lambda _c, r=rec: self._on_close_combo(r))
            h.addWidget(btn)
            rm = QPushButton("✕")
            rm.setFixedSize(24, 24)
            rm.setToolTip("仅删除本条记录 (不下单)")
            rm.clicked.connect(lambda _c, r=rec: self._on_remove_combo_record(r))
            h.addWidget(rm)
            self._combos_layout.addWidget(row)

    def _on_close_combo(self, rec: dict):
        """一键平仓: 用**反向市价 BAG 单**把该组合整体平掉 (多腿一张单)。"""
        if not hasattr(self._engine, "place_combo_order"):
            QMessageBox.warning(self, "不支持", "当前引擎不支持组合单平仓")
            return
        rev = "SELL" if rec["action"] == "BUY" else "BUY"
        legs_desc = "\n".join(
            f"  {l['action']} {l['ratio']}x {l['right']} {l['strike']:g} "
            f"({l['expiry']})" for l in rec["legs"])
        ret = QMessageBox.question(
            self, "一键平仓 (市价)",
            f"{rec['name']} — {rec['action']} {rec['qty']} 组\n{legs_desc}\n\n"
            f"将以反向 {rev} **市价** BAG 组合单整体平仓\n"
            f"(多腿视为一个整体, 一张单同时成交)。确定?",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if ret != QMessageBox.Yes:
            return
        legs = [ComboLegInfo(
                    con_id=l["con_id"], symbol=l["symbol"], expiry=l["expiry"],
                    strike=l["strike"], right=l["right"], action=l["action"],
                    ratio=l.get("ratio", 1))
                for l in rec["legs"]]
        order_id = self._engine.place_combo_order(
            rec["symbol"], legs, rev, rec["qty"], 0.0,
            rec.get("outside", False), market=True)
        if order_id and order_id > 0:
            self._open_combos.remove(rec)
            self._save_combos()
            self._render_combos()
            self._status_label.setText(
                f"平仓单已提交 orderId={order_id} (反向 {rev} 市价组合单)")
            self._status_label.setStyleSheet(
                f"color: {COLOR_GREEN}; font-size: 11px;")

    def _on_remove_combo_record(self, rec: dict):
        if rec in self._open_combos:
            self._open_combos.remove(rec)
            self._save_combos()
            self._render_combos()

    def _save_combos(self):
        try:
            with open(self._combos_path, "w", encoding="utf-8") as f:
                json.dump(self._open_combos, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f"[COMBO] save error: {e}", flush=True)

    def _load_combos(self):
        if not os.path.exists(self._combos_path):
            return
        try:
            with open(self._combos_path, "r", encoding="utf-8") as f:
                self._open_combos = json.load(f)
        except Exception as e:
            print(f"[COMBO] load error: {e}", flush=True)
            self._open_combos = []
        # 清理过期组合 (所有腿都已到期的记录无仓可平)
        today = time.strftime("%Y%m%d")
        before = len(self._open_combos)
        self._open_combos = [
            rec for rec in self._open_combos
            if any(str(l.get("expiry", "")) >= today for l in rec.get("legs", []))
        ]
        if len(self._open_combos) != before:
            self._save_combos()


class StrategyWindow(QMainWindow):
    """Standalone window wrapper around :class:`StrategyPanel`.

    Kept for backward compatibility / standalone use; the main GUI now embeds
    :class:`StrategyPanel` directly as the「多腿组合」tab.
    """

    def __init__(self, engine, symbol: str = "SPY", parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"策略组合 — {symbol}")
        self.setMinimumSize(700, 550)
        self.resize(780, 620)
        self.panel = StrategyPanel(engine=engine, symbol=symbol, parent=self)
        self.setCentralWidget(self.panel)
        self.setStyleSheet(WINDOW_STYLESHEET)

    def show_and_load(self):
        self.show()
        self.panel.ensure_loaded()

    def cleanup(self):
        self.panel.cleanup()

    def closeEvent(self, event):
        self.cleanup()
        event.accept()
