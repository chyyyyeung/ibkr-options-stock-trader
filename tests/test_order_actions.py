"""Focused safety tests for targeted/global cancellation and Bid-limit selling."""

import os
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt5.QtWidgets import QApplication, QMessageBox

from ibkr_engine import IBKREngine
from models import OptionInfo, OrderAction, OrderInfo, OrderStatus
from paper_engine import PaperEngine
from widgets.price_ladder import PriceLadder


class _Signal:
    def __init__(self):
        self.calls = []

    def emit(self, *args):
        self.calls.append(args)


class _Bridge:
    def __init__(self):
        self.order_status_changed = _Signal()


class _IBApp:
    def __init__(self):
        self.cancelled = []
        self.global_cancel_count = 0

    def cancelOrder(self, order_id):
        self.cancelled.append(order_id)

    def reqGlobalCancel(self):
        self.global_cancel_count += 1


def _order(order_id, option, action, status=OrderStatus.SUBMITTED):
    return OrderInfo(
        order_id=order_id,
        option=option,
        action=action,
        quantity=1,
        limit_price=1.0,
        status=status,
    )


def _mixed_orders():
    target = OptionInfo("SPY", "20260901", 650.0, "C", con_id=101)
    same_contract = OptionInfo("SPY", "20260901", 650.0, "C", con_id=101)
    same_key_other_con_id = OptionInfo("SPY", "20260901", 650.0, "C", con_id=999)
    other_strike = OptionInfo("SPY", "20260901", 651.0, "C", con_id=102)
    other_symbol = OptionInfo("QQQ", "20260901", 650.0, "C", con_id=103)
    return target, {
        1: _order(1, target, OrderAction.BUY, OrderStatus.PENDING),
        2: _order(2, same_contract, OrderAction.SELL),
        3: _order(3, other_strike, OrderAction.BUY),
        4: _order(4, other_symbol, OrderAction.SELL),
        5: _order(5, target, OrderAction.BUY, OrderStatus.FILLED),
        6: _order(6, same_key_other_con_id, OrderAction.SELL),
    }


class CancellationTests(unittest.TestCase):
    def test_ibkr_targeted_cancel_is_exact_and_includes_buy_and_sell(self):
        target, orders = _mixed_orders()
        engine = IBKREngine.__new__(IBKREngine)
        engine._orders = orders
        engine._user_cancel_ids = set()
        engine._app = _IBApp()

        count = engine.cancel_orders_for_option(target)

        self.assertEqual(count, 2)
        self.assertEqual(engine._app.cancelled, [1, 2])
        self.assertEqual(engine._user_cancel_ids, {1, 2})
        self.assertEqual(engine._app.global_cancel_count, 0)

    def test_paper_targeted_cancel_leaves_other_instruments_untouched(self):
        target, orders = _mixed_orders()
        engine = PaperEngine.__new__(PaperEngine)
        engine._orders = orders
        engine.bridge = _Bridge()

        count = engine.cancel_orders_for_option(target)

        self.assertEqual(count, 2)
        self.assertEqual(orders[1].status, OrderStatus.CANCELLED)
        self.assertEqual(orders[2].status, OrderStatus.CANCELLED)
        self.assertEqual(orders[3].status, OrderStatus.SUBMITTED)
        self.assertEqual(orders[4].status, OrderStatus.SUBMITTED)
        self.assertEqual(orders[5].status, OrderStatus.FILLED)
        self.assertEqual(orders[6].status, OrderStatus.SUBMITTED)

    def test_ibkr_global_cancel_remains_available_separately(self):
        _, orders = _mixed_orders()
        engine = IBKREngine.__new__(IBKREngine)
        engine._orders = orders
        engine._user_cancel_ids = set()
        engine._app = _IBApp()

        engine.cancel_all_orders()

        self.assertEqual(engine._app.global_cancel_count, 1)
        self.assertEqual(engine._app.cancelled, [])
        self.assertEqual(engine._user_cancel_ids, {1, 2, 3, 4, 6})


class PriceLadderActionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.ladder = PriceLadder()
        self.option = OptionInfo("SPY", "20260901", 650.0, "C")
        self.ladder._option = self.option
        self.ladder.no_confirm_checkbox.setChecked(True)

    def tearDown(self):
        self.ladder.cleanup()
        self.ladder.deleteLater()

    def test_latest_bid_button_emits_sell_limit_price(self):
        class Engine:
            @staticmethod
            def get_tick(_key):
                return {"bid": 1.23, "ask": 1.27, "last": 1.25}

            @staticmethod
            def unsubscribe_market_depth():
                pass

        self.ladder._engine = Engine()
        emitted = []
        self.ladder.order_requested.connect(
            lambda option, action, price: emitted.append((option, action, price))
        )

        self.ladder._on_sell_at_latest_bid()

        self.assertEqual(emitted, [(self.option, "SELL", 1.23)])

    def test_latest_bid_button_fails_closed_without_live_bid(self):
        emitted = []
        self.ladder.order_requested.connect(
            lambda option, action, price: emitted.append((option, action, price))
        )

        for invalid_bid in (0, float("nan")):
            class Engine:
                @staticmethod
                def get_tick(_key):
                    return {"bid": invalid_bid, "ask": 1.27, "last": 1.25}

                @staticmethod
                def unsubscribe_market_depth():
                    pass

            self.ladder._engine = Engine()
            with patch.object(QMessageBox, "warning", return_value=QMessageBox.Ok):
                self.ladder._on_sell_at_latest_bid()

        self.assertEqual(emitted, [])


if __name__ == "__main__":
    unittest.main()
