"""自选监控面板 — 位于持仓/委托右侧, 实时显示 watch list 各合约现价 + 到价警报。

每个合约可挂**任意多条**到价警报, 表格每行 = 合约的一条警报 (无警报的合约占一行占位)。
表格列: 合约 | 现价 | 条件(≥/≤) | 警报价 | ✕。
- **右键**任一行 → 菜单「添加涨破/跌破警报」「移除该合约监控」(无限追加);
- 现价每 0.5s 刷新 (WatchListManager.ticked; 面板不可见时跳过重绘, 警报照常巡检);
- 单击「条件」列切换 ≥/≤; 双击「警报价」直接编辑, 留空/0 = 删除该条;
- ✕: 警报行删该条警报, 占位行 (无警报) 删整个合约;
- 双击「合约」列跳到对应点价梯/期权链;
- 触发时: 声音 (sound_alerts.play_alert) + 非模态弹窗 + 该合约所有行高亮 3 秒 (一次性)。
"""

from PyQt5.QtWidgets import (
    QWidget, QVBoxLayout, QLabel, QTableWidget, QTableWidgetItem,
    QHeaderView, QPushButton, QMessageBox, QAbstractItemView, QMenu,
    QInputDialog,
)
from PyQt5.QtCore import Qt, QTimer, pyqtSignal
from PyQt5.QtGui import QColor

from config import (
    COLOR_BG_DARK, COLOR_BG_PANEL, COLOR_TEXT, COLOR_TEXT_DIM,
    COLOR_BORDER, COLOR_ACCENT, COLOR_GREEN, COLOR_RED, COLOR_ALERT_ROW,
)
from conditional_orders import _current_price
from sound_alerts import play_alert

_COL_NAME, _COL_PRICE, _COL_COND, _COL_ALERT, _COL_DEL = range(5)
_HIGHLIGHT_MS = 3000


class WatchPanel(QWidget):
    """自选监控面板 (视图); 逻辑在 watchlist.WatchListManager。"""

    option_selected = pyqtSignal(object)   # OptionInfo — 双击合约名跳点价梯/期权链

    def __init__(self, parent=None):
        super().__init__(parent)
        self._manager = None
        self._rebuilding = False       # 重建表格期间屏蔽 itemChanged
        self._last_prices: dict[str, float] = {}   # key -> 上次显示价 (涨跌着色)
        self._alert_boxes: list = []   # 非模态弹窗引用 (防 GC)
        # 行 → (key, alert_id | None); alert_id=None 表示该合约暂无警报的占位行
        self._rows: list = []
        self._item_by_key: dict = {}
        self._build_ui()

    def set_manager(self, manager):
        self._manager = manager
        manager.changed.connect(self._rebuild)
        manager.ticked.connect(self._refresh_prices)
        manager.alerted.connect(self._on_alert)
        self._rebuild()

    # ── UI ────────────────────────────────────────────────────────────
    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(2, 2, 2, 2)
        layout.setSpacing(2)

        title = QLabel("自选监控")
        title.setStyleSheet(
            f"color: {COLOR_ACCENT}; font-size: 12px; font-weight: bold; "
            f"padding: 2px; border: none;"
        )
        title.setToolTip(
            "点价梯左下角「☆ 加自选」把当前合约加入监控。\n"
            "右键任一行 → 为该合约「添加一行警报」(可无限追加, 如 395/400/405);\n"
            "单击「条件」列切 ≥/≤, 双击「警报价」编辑 (留空/0 = 删除该条);\n"
            "触发后该条警报自动清除 (一次性)。双击合约名跳点价梯/期权链。"
        )
        layout.addWidget(title)

        self._table = QTableWidget(0, 5)
        self._table.setHorizontalHeaderLabels(
            ["合约", "现价", "条件", "警报价", ""])
        self._table.verticalHeader().setVisible(False)
        self._table.setSelectionMode(QAbstractItemView.NoSelection)
        self._table.setShowGrid(False)
        self._table.setStyleSheet(f"""
            QTableWidget {{
                background-color: {COLOR_BG_DARK}; color: {COLOR_TEXT};
                border: 1px solid {COLOR_BORDER}; font-size: 11px;
            }}
            QHeaderView::section {{
                background-color: {COLOR_BG_PANEL}; color: {COLOR_TEXT_DIM};
                border: none; padding: 3px; font-size: 11px;
            }}
        """)
        header = self._table.horizontalHeader()
        header.setSectionResizeMode(_COL_NAME, QHeaderView.Stretch)
        for col, w in ((_COL_PRICE, 56), (_COL_COND, 36),
                       (_COL_ALERT, 60), (_COL_DEL, 24)):
            header.setSectionResizeMode(col, QHeaderView.Fixed)
            self._table.setColumnWidth(col, w)
        self._table.itemChanged.connect(self._on_item_changed)
        self._table.cellClicked.connect(self._on_cell_clicked)
        self._table.cellDoubleClicked.connect(self._on_cell_double_clicked)
        # 右键任一行 → 添加警报 / 移除合约
        self._table.setContextMenuPolicy(Qt.CustomContextMenu)
        self._table.customContextMenuRequested.connect(self._on_context_menu)
        layout.addWidget(self._table)

    # ── 表格重建 (列表/警报变化时) ────────────────────────────────────
    def _rebuild(self):
        if self._manager is None:
            return
        self._rebuilding = True
        try:
            items = self._manager.items()
            self._item_by_key = {it.key: it for it in items}
            # 展开成行: 有警报的合约每条一行, 无警报的占一行 (alert_id=None)
            self._rows = []
            for it in items:
                if it.alerts:
                    for a in it.alerts:
                        self._rows.append((it.key, a.id))
                else:
                    self._rows.append((it.key, None))

            self._table.setRowCount(len(self._rows))
            for row, (key, alert_id) in enumerate(self._rows):
                it = self._item_by_key.get(key)
                if it is None:
                    continue
                alert = next((a for a in it.alerts if a.id == alert_id), None)
                self._build_row(row, it, alert)
        finally:
            self._rebuilding = False
        self._refresh_prices(force=True)

    def _build_row(self, row: int, it, alert):
        # 合约名 (每行都填, 方便右键/双击定位; 视觉上重复但清晰)
        name = QTableWidgetItem(it.option.display_name)
        name.setFlags(Qt.ItemIsEnabled)
        name.setData(Qt.UserRole, it.key)
        name.setToolTip(f"{it.option.display_name}\n"
                        f"双击跳到对应点价梯/期权链; 右键加警报")
        self._table.setItem(row, _COL_NAME, name)

        price = QTableWidgetItem("--")
        price.setFlags(Qt.ItemIsEnabled)
        price.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self._table.setItem(row, _COL_PRICE, price)

        if alert is None:
            # 占位行 (该合约暂无警报)
            cond = QTableWidgetItem("")
            cond.setFlags(Qt.ItemIsEnabled)
            self._table.setItem(row, _COL_COND, cond)

            av = QTableWidgetItem("右键加警报")
            av.setFlags(Qt.ItemIsEnabled)
            av.setForeground(QColor(COLOR_TEXT_DIM))
            av.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
            self._table.setItem(row, _COL_ALERT, av)
        else:
            is_above = alert.direction == "above"
            cond = QTableWidgetItem("≥" if is_above else "≤")
            cond.setFlags(Qt.ItemIsEnabled)
            cond.setTextAlignment(Qt.AlignCenter)
            cond.setForeground(QColor(COLOR_GREEN if is_above else COLOR_RED))
            cond.setToolTip("单击切换 ≥ (涨破) / ≤ (跌破)")
            self._table.setItem(row, _COL_COND, cond)

            av = QTableWidgetItem(f"{alert.price:g}" if alert.price > 0 else "")
            av.setFlags(Qt.ItemIsEnabled | Qt.ItemIsEditable)
            av.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
            av.setForeground(QColor(COLOR_GREEN if is_above else COLOR_RED))
            av.setToolTip("双击编辑警报价 (留空/0 = 删除该条)")
            self._table.setItem(row, _COL_ALERT, av)

        btn = QPushButton("✕")
        btn.setFixedSize(20, 20)
        btn.setCursor(Qt.PointingHandCursor)
        key = it.key
        aid = None if alert is None else alert.id
        if aid is None:
            btn.setToolTip("从自选移除该合约")
            btn.clicked.connect(lambda _c, k=key: self._manager.remove(k))
        else:
            btn.setToolTip("删除该条警报")
            btn.clicked.connect(
                lambda _c, k=key, i=aid: self._manager.remove_alert(k, i))
        btn.setStyleSheet(
            f"QPushButton {{ color: {COLOR_TEXT_DIM}; border: none; "
            f"background: transparent; font-size: 11px; }}"
            f"QPushButton:hover {{ color: {COLOR_RED}; }}"
        )
        self._table.setCellWidget(row, _COL_DEL, btn)

    # ── 现价刷新 (0.5s 心跳) ─────────────────────────────────────────
    def _refresh_prices(self, force: bool = False):
        if self._manager is None:
            return
        if not force and not self.isVisible():
            return   # 面板不可见时跳过重绘 (警报巡检在 manager 侧照常)
        cache: dict = {}   # key -> (price, bid, ask); 每个合约只算一次
        for row in range(self._table.rowCount()):
            if row >= len(self._rows):
                break
            key = self._rows[row][0]
            price_item = self._table.item(row, _COL_PRICE)
            if price_item is None:
                continue
            if key not in cache:
                cache[key] = self._compute_price(key)
            price, bid, ask = cache[key]
            if price <= 0:
                price_item.setText("--")
                continue
            prev = self._last_prices.get(key, 0.0)
            price_item.setText(f"{price:.2f}")
            price_item.setToolTip(f"买 {bid:.2f} / 卖 {ask:.2f}")
            if prev > 0 and abs(price - prev) > 1e-9:
                price_item.setForeground(
                    QColor(COLOR_GREEN if price > prev else COLOR_RED))
        for key, (price, _b, _a) in cache.items():
            if price > 0:
                self._last_prices[key] = price

    def _compute_price(self, key: str):
        tick = self._manager.price_of(key)
        last = tick.get("last", 0) or 0
        bid = tick.get("bid", 0) or 0
        ask = tick.get("ask", 0) or 0
        price = last if last > 0 else ((bid + ask) / 2 if bid > 0 and ask > 0
                                       else (bid or ask or 0.0))
        return price, bid, ask

    # ── 右键菜单: 加警报 / 移除合约 ───────────────────────────────────
    def _on_context_menu(self, pos):
        if self._manager is None:
            return
        row = self._table.rowAt(pos.y())
        if row < 0 or row >= len(self._rows):
            return
        key = self._rows[row][0]
        it = self._item_by_key.get(key)
        if it is None:
            return
        menu = QMenu(self)
        act_above = menu.addAction("＋ 添加涨破警报 (≥ 涨到)")
        act_below = menu.addAction("＋ 添加跌破警报 (≤ 跌到)")
        menu.addSeparator()
        act_remove = menu.addAction("✕ 移除该合约监控")
        chosen = menu.exec_(self._table.viewport().mapToGlobal(pos))
        if chosen is None:
            return
        if chosen == act_remove:
            self._manager.remove(key)
        elif chosen in (act_above, act_below):
            direction = "above" if chosen == act_above else "below"
            self._prompt_add_alert(key, it, direction)

    def _prompt_add_alert(self, key: str, it, direction: str):
        cur, _b, _a = self._compute_price(key)
        verb = "涨到 ≥" if direction == "above" else "跌到 ≤"
        val, ok = QInputDialog.getDouble(
            self, "添加到价警报",
            f"{it.option.display_name}\n现价触及 {verb} 该价格时提醒:",
            float(cur) if cur > 0 else 0.0, 0.0, 1e7, 2)
        if ok and val > 0:
            self._manager.add_alert(key, direction, val)

    # ── 单击「条件」列 → 切换 ≥/≤ ─────────────────────────────────────
    def _on_cell_clicked(self, row: int, col: int):
        if self._rebuilding or self._manager is None:
            return
        if col != _COL_COND or row >= len(self._rows):
            return
        key, alert_id = self._rows[row]
        if alert_id is None:
            return
        self._manager.toggle_alert_direction(key, alert_id)

    # ── 双击合约名 → 跳点价梯/期权链 ─────────────────────────────────
    def _on_cell_double_clicked(self, row: int, col: int):
        if col != _COL_NAME or self._manager is None:
            return
        if row >= len(self._rows):
            return
        key = self._rows[row][0]
        it = self._item_by_key.get(key)
        if it is not None:
            self.option_selected.emit(it.option)

    # ── 警报价编辑 ────────────────────────────────────────────────────
    def _on_item_changed(self, item: QTableWidgetItem):
        if self._rebuilding or self._manager is None:
            return
        if item.column() != _COL_ALERT:
            return
        row = item.row()
        if row >= len(self._rows):
            return
        key, alert_id = self._rows[row]
        if alert_id is None:
            return
        text = item.text().strip()
        try:
            value = float(text) if text else 0.0
        except ValueError:
            value = 0.0
        # update_alert 会 emit changed → 触发 _rebuild, 之后 item 已失效, 勿再访问
        self._manager.update_alert(key, alert_id, value)

    # ── 警报触发 ──────────────────────────────────────────────────────
    def _on_alert(self, item, direction: str, price: float):
        play_alert()
        # 延到事件循环下一拍再高亮: alerted 之后 manager 会发 changed 重建表格,
        # 立即高亮会被重建抹掉 (且拿到的是即将销毁的旧 item)。
        QTimer.singleShot(0, lambda k=item.key: self._highlight_rows(k))

        sign = "≥ 涨破" if direction == "above" else "≤ 跌破"
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Warning)
        box.setWindowTitle("⚠ 到价警报")
        box.setText(f"{item.option.display_name}\n现价 {price:.2f} {sign}警报价")
        box.setModal(False)
        box.finished.connect(
            lambda _r, b=box: self._alert_boxes.remove(b)
            if b in self._alert_boxes else None)
        self._alert_boxes.append(box)
        box.show()

    def _highlight_rows(self, key: str):
        self._set_rows_background(key, QColor(COLOR_ALERT_ROW))
        QTimer.singleShot(
            _HIGHLIGHT_MS,
            lambda: self._set_rows_background(key, QColor(0, 0, 0, 0)))

    def _set_rows_background(self, key: str, color: QColor):
        """按 key 查该合约的所有行并设背景色。

        - 用 _rebuilding 屏蔽 setBackground 触发的 itemChanged —— 否则警报价列
          会被 _on_item_changed 当成用户编辑;
        - 延时取消高亮时按 key 重新查行 (表格可能已重建, 旧 item 引用会失效
          报 RuntimeError), 行已删除则静默跳过。
        """
        self._rebuilding = True
        try:
            for row in range(self._table.rowCount()):
                name_item = self._table.item(row, _COL_NAME)
                if name_item is None or name_item.data(Qt.UserRole) != key:
                    continue
                for col in (_COL_NAME, _COL_PRICE, _COL_COND, _COL_ALERT):
                    cell = self._table.item(row, col)
                    if cell is not None:
                        cell.setBackground(color)
        finally:
            self._rebuilding = False

    def cleanup(self):
        for box in self._alert_boxes:
            try:
                box.close()
            except Exception:
                pass
        self._alert_boxes.clear()
