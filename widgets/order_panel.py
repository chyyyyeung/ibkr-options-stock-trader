"""Order panel — displays pending and recent orders with cancel support."""

from PyQt5.QtWidgets import (
    QWidget, QVBoxLayout, QTableWidget, QTableWidgetItem,
    QHeaderView, QAbstractItemView, QLabel, QPushButton,
)
from PyQt5.QtCore import pyqtSignal, Qt, QTimer
from PyQt5.QtGui import QColor, QBrush

from config import COLOR_GREEN, COLOR_RED, COLOR_TEXT, COLOR_TEXT_DIM, COLOR_ACCENT
from models import OrderInfo, OrderStatus, OrderAction, OrderType


class OrderPanel(QWidget):
    """Displays orders with cancel buttons."""

    cancel_requested = pyqtSignal(int)  # orderId
    option_selected = pyqtSignal(object)  # OptionInfo — 双击委托行跳到该合约

    def __init__(self, parent=None):
        super().__init__(parent)
        self._engine = None
        self._last_sig = None          # skip rebuild when order state unchanged
        self._row_options: list = []   # row -> OptionInfo (供双击跳转)
        self._brush_cache: dict[str, QBrush] = {}
        self._build_ui()

        self._refresh_timer = QTimer()
        self._refresh_timer.timeout.connect(self._refresh)
        self._refresh_timer.start(1000)

    def _brush(self, color: str) -> QBrush:
        b = self._brush_cache.get(color)
        if b is None:
            b = QBrush(QColor(color))
            self._brush_cache[color] = b
        return b

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        self.title = QLabel("委托")
        self.title.setStyleSheet("font-size: 13px; font-weight: bold; padding: 4px;")
        layout.addWidget(self.title)

        headers = ["ID", "合约", "方向", "数量", "价格", "状态", "操作"]
        self.table = QTableWidget(0, len(headers))
        self.table.setHorizontalHeaderLabels(headers)
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)

        self.table.horizontalHeaderItem(4).setToolTip(
            "已成交显示实际成交均价; 未成交显示委托价 (市价单显示「市价」)"
        )

        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.Stretch)
        for i in range(2, len(headers)):
            header.setSectionResizeMode(i, QHeaderView.ResizeToContents)

        self.table.doubleClicked.connect(self._on_double_click)
        layout.addWidget(self.table)

    def _on_double_click(self, index):
        """双击委托行 → 跳到该合约 (加载到点价梯)。COMBO 组合跳过。"""
        row = index.row()
        if 0 <= row < len(self._row_options):
            opt = self._row_options[row]
            if opt is not None and opt.right in ("C", "P", "STK"):
                self.option_selected.emit(opt)

    def set_engine(self, engine):
        self._engine = engine

    @staticmethod
    def _price_text(order: OrderInfo) -> tuple[str, str]:
        """价格列的文案 + tooltip。

        **成交价优先**: 一旦有成交均价 (filled_price, 真实引擎来自 IBKR
        avgFillPrice, 模拟引擎来自本地撮合) 就显示它 —— 这才是"我到底以多少钱
        成交的"。此前这一列一律显示 limit_price, 结果:
          - 正股/期货**市价单**根本没有限价 (limit_price=0) → 显示 $0.00;
          - 期权市价单显示的是下单前的中间价参考值, 也不是成交价。
        未成交时按订单类型区分: 限价单显示委托价, 市价单显示「市价」而不是 $0.00。
        """
        if order.filled_price > 0:
            return f"${order.filled_price:.2f}", "实际成交均价"
        if order.order_type == OrderType.MARKET:
            return "市价", "市价单 — 成交后此处显示实际成交均价"
        return f"${order.limit_price:.2f}", "委托价 (限价单)"

    def _refresh(self):
        if not self._engine:
            return

        orders = self._engine.orders
        # Show most recent first, limit to 50
        sorted_orders = sorted(orders.values(),
                               key=lambda o: o.create_time, reverse=True)[:50]
        # 行→合约映射 (双击跳转用), 每次刷新都更新, 与表格行一一对应
        self._row_options = [o.option for o in sorted_orders]

        # Skip the (expensive) rebuild when nothing visible changed — avoids
        # recreating cancel buttons and re-painting cells every second while
        # idle. Signature covers every field the table renders.
        # 签名必须含 filled_price/filled_qty/order_type: 成交回报到来时 limit_price
        # 并不变, 若签名只看 limit_price, 表格会被判定为"无变化"而跳过重绘,
        # 成交价永远刷不出来。
        sig = tuple(
            (o.order_id, o.option.display_name, o.action, o.quantity,
             o.limit_price, o.filled_price, o.filled_qty, o.order_type,
             o.status, o.error_msg)
            for o in sorted_orders
        )
        if sig == self._last_sig:
            return
        self._last_sig = sig

        self.table.setRowCount(len(sorted_orders))

        pending_count = 0
        for row, order in enumerate(sorted_orders):
            if order.status in (OrderStatus.PENDING, OrderStatus.SUBMITTED):
                pending_count += 1

            # ID
            self._set_cell(row, 0, str(order.order_id), COLOR_TEXT)

            # Contract
            self._set_cell(row, 1, order.option.display_name, COLOR_TEXT)

            # Direction
            action_color = COLOR_GREEN if order.action == OrderAction.BUY else COLOR_RED
            self._set_cell(row, 2, order.display_action, action_color)

            # Quantity
            self._set_cell(row, 3, str(order.quantity), COLOR_TEXT)

            # Price — 已成交显示成交均价, 未成交显示委托价 / 「市价」
            price_txt, price_tip = self._price_text(order)
            self._set_cell(row, 4, price_txt, COLOR_TEXT)
            self.table.item(row, 4).setToolTip(price_tip)

            # Status (rejection reason shown as tooltip)
            status_color = self._status_color(order.status)
            self._set_cell(row, 5, order.display_status, status_color)
            self.table.item(row, 5).setToolTip(
                order.error_msg if order.status == OrderStatus.ERROR else ""
            )

            # Cancel button
            if order.status in (OrderStatus.PENDING, OrderStatus.SUBMITTED):
                cancel_btn = QPushButton("撤单")
                cancel_btn.setStyleSheet(f"""
                    QPushButton {{
                        background-color: {COLOR_RED};
                        color: white;
                        border: none;
                        padding: 2px 8px;
                        border-radius: 2px;
                        font-size: 11px;
                    }}
                    QPushButton:hover {{ background-color: #ff5252; }}
                """)
                cancel_btn.clicked.connect(
                    lambda _, oid=order.order_id: self._on_cancel(oid)
                )
                self.table.setCellWidget(row, 6, cancel_btn)
            else:
                self.table.removeCellWidget(row, 6)
                self._set_cell(row, 6, "", COLOR_TEXT)

            self.table.setRowHeight(row, 28)

        self.title.setText(f"委托 ({pending_count} 挂单)")

    def _set_cell(self, row, col, text, color):
        item = self.table.item(row, col)
        if item is None:
            item = QTableWidgetItem()
            item.setTextAlignment(Qt.AlignCenter)
            self.table.setItem(row, col, item)
        item.setText(text)
        item.setForeground(self._brush(color))

    def _status_color(self, status: OrderStatus) -> str:
        return {
            OrderStatus.PENDING: COLOR_ACCENT,
            OrderStatus.SUBMITTED: COLOR_ACCENT,
            OrderStatus.FILLED: COLOR_GREEN,
            OrderStatus.CANCELLED: COLOR_TEXT_DIM,
            OrderStatus.ERROR: COLOR_RED,
        }.get(status, COLOR_TEXT)

    def _on_cancel(self, order_id: int):
        self.cancel_requested.emit(order_id)

    def cleanup(self):
        self._refresh_timer.stop()
