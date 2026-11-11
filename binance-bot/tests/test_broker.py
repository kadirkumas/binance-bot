"""Paper broker muhasebe testleri (spot + futures)."""
import copy
import unittest

from bot.backtest import MemoryStore
from bot.broker import BrokerError, PaperBroker
from bot.config import DEFAULTS


def cfg(**over):
    c = copy.deepcopy(DEFAULTS)
    c["paper"] = {"start_balance": 10000.0, "fee_pct": 0.0, "slippage_pct": 0.0}
    c["futures"]["fee_pct"] = 0.0
    for k, v in over.items():
        c[k] = v
    return c


class TestPaperSpot(unittest.TestCase):
    def setUp(self):
        self.store = MemoryStore()
        self.b = PaperBroker(cfg(), self.store, "spot")

    def test_open_close_no_fee(self):
        f = self.b.open("BTCUSDT", "long", 1000, 100.0)
        self.assertAlmostEqual(f["qty"], 10.0)
        self.assertAlmostEqual(self.b.book["cash"], 9000.0)
        snap = self.b.snapshot({"BTCUSDT": 100.0})
        self.assertAlmostEqual(snap["equity"], 10000.0)
        c = self.b.close("BTCUSDT", 110.0)
        self.assertAlmostEqual(c["realized_pnl"], 100.0)
        self.assertAlmostEqual(self.b.book["cash"], 10100.0)
        self.assertEqual(self.b.positions(), {})

    def test_fees_reduce_equity(self):
        c = cfg()
        c["paper"]["fee_pct"] = 0.1
        b = PaperBroker(c, MemoryStore(), "spot")
        b.open("BTCUSDT", "long", 1000, 100.0)
        snap = b.snapshot({"BTCUSDT": 100.0})
        self.assertAlmostEqual(snap["equity"], 9999.0, places=6)  # alis komisyonu
        b.close("BTCUSDT", 100.0)
        self.assertAlmostEqual(b.book["cash"], 9998.0, places=6)

    def test_slippage(self):
        c = cfg()
        c["paper"]["slippage_pct"] = 1.0
        b = PaperBroker(c, MemoryStore(), "spot")
        f = b.open("X", "long", 1000, 100.0)
        self.assertAlmostEqual(f["price"], 101.0)
        cl = b.close("X", 100.0)
        self.assertAlmostEqual(cl["price"], 99.0)

    def test_insufficient_balance(self):
        with self.assertRaises(BrokerError):
            self.b.open("BTCUSDT", "long", 50000, 100.0)

    def test_spot_short_rejected(self):
        with self.assertRaises(BrokerError):
            self.b.open("BTCUSDT", "short", 100, 100.0)

    def test_average_entry(self):
        self.b.open("BTCUSDT", "long", 1000, 100.0)
        self.b.open("BTCUSDT", "long", 1000, 200.0)
        pos = self.b.positions()["BTCUSDT"]
        self.assertAlmostEqual(pos["qty"], 15.0)
        self.assertAlmostEqual(pos["entry"], (10 * 100 + 5 * 200) / 15)

    def test_partial_close(self):
        self.b.open("BTCUSDT", "long", 1000, 100.0)
        c = self.b.close("BTCUSDT", 120.0, portion=0.5)
        self.assertAlmostEqual(c["qty"], 5.0)
        self.assertAlmostEqual(self.b.positions()["BTCUSDT"]["qty"], 5.0)
        self.assertAlmostEqual(c["realized_pnl"], 100.0)

    def test_opposite_signal_flips(self):
        self.b.open("BTCUSDT", "long", 1000, 100.0)
        self.b.open("BTCUSDT", "long", 1000, 100.0)  # ayni yon: ekleme
        self.assertAlmostEqual(self.b.positions()["BTCUSDT"]["qty"], 20.0)

    def test_persistence(self):
        self.b.open("BTCUSDT", "long", 1000, 100.0)
        b2 = PaperBroker(cfg(), self.store, "spot")
        self.assertIn("BTCUSDT", b2.positions())
        self.assertAlmostEqual(b2.book["cash"], 9000.0)

    def test_reset(self):
        self.b.open("BTCUSDT", "long", 1000, 100.0)
        self.b.reset(5000)
        self.assertEqual(self.b.positions(), {})
        self.assertAlmostEqual(self.b.book["cash"], 5000.0)


class TestPaperFutures(unittest.TestCase):
    def setUp(self):
        self.store = MemoryStore()
        self.b = PaperBroker(cfg(), self.store, "futures")

    def test_long_leverage_margin(self):
        f = self.b.open("BTCUSDT", "long", 3000, 100.0, leverage=3)
        self.assertAlmostEqual(f["qty"], 30.0)
        self.assertAlmostEqual(self.b.book["cash"], 9000.0)   # 3000/3 marjin
        snap = self.b.snapshot({"BTCUSDT": 110.0})
        self.assertAlmostEqual(snap["unrealized"], 300.0)
        self.assertAlmostEqual(snap["equity"], 10300.0)
        c = self.b.close("BTCUSDT", 110.0)
        self.assertAlmostEqual(c["realized_pnl"], 300.0)
        self.assertAlmostEqual(self.b.book["cash"], 10300.0)

    def test_short_profit(self):
        self.b.open("BTCUSDT", "short", 3000, 100.0, leverage=3)
        snap = self.b.snapshot({"BTCUSDT": 90.0})
        self.assertAlmostEqual(snap["unrealized"], 300.0)
        c = self.b.close("BTCUSDT", 90.0)
        self.assertAlmostEqual(c["realized_pnl"], 300.0)
        self.assertAlmostEqual(self.b.book["cash"], 10300.0)

    def test_short_loss(self):
        self.b.open("BTCUSDT", "short", 3000, 100.0, leverage=3)
        c = self.b.close("BTCUSDT", 105.0)
        self.assertAlmostEqual(c["realized_pnl"], -150.0)

    def test_liquidation(self):
        self.b.open("BTCUSDT", "long", 3000, 100.0, leverage=10)
        pos = self.b.positions()["BTCUSDT"]
        self.assertGreater(pos["liquidation"], 89.0)
        fills = self.b.check_liquidations({"BTCUSDT": 80.0})
        self.assertEqual(len(fills), 1)
        self.assertEqual(self.b.positions(), {})
        # 10x: marjin 300, likidasyon 90.5 -> zarar (90.5-100)*30 = -285
        self.assertAlmostEqual(self.b.book["cash"], 9715.0, places=4)
        self.assertAlmostEqual(fills[0]["realized_pnl"], -285.0, places=4)

    def test_flip_closes_previous(self):
        self.b.open("BTCUSDT", "long", 3000, 100.0, leverage=3)
        self.b.open("BTCUSDT", "short", 3000, 100.0, leverage=3)
        self.assertEqual(self.b.positions()["BTCUSDT"]["side"], "short")


if __name__ == "__main__":
    unittest.main()
