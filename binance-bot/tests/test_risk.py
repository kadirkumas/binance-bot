"""Risk yonetimi testleri."""
import copy
import unittest

from bot.backtest import MemoryStore
from bot.config import DEFAULTS
from bot.risk import RiskManager, day_start, sanity_check_config
from bot.util import now_ms, round_step


def make_cfg(**risk):
    c = copy.deepcopy(DEFAULTS)
    c["risk"].update(risk)
    return c


class TestRisk(unittest.TestCase):
    def setUp(self):
        self.store = MemoryStore()
        self.cfg = make_cfg()
        self.r = RiskManager(self.cfg, self.store)

    def test_fixed_sizing(self):
        self.assertAlmostEqual(self.r.order_size(10000, 10000, 100, "spot", 1, None), 250.0)

    def test_percent_sizing(self):
        self.cfg["risk"]["sizing"] = "percent"
        self.cfg["risk"]["max_position_pct"] = 10
        self.assertAlmostEqual(self.r.order_size(10000, 10000, 100, "spot", 1, None), 1000.0)

    def test_hard_cap(self):
        self.cfg["risk"]["max_order_usdt"] = 100
        self.assertAlmostEqual(self.r.order_size(10000, 10000, 100, "spot", 1, 5000), 100.0)

    def test_cash_limit_spot(self):
        size = self.r.order_size(10000, 50, 100, "spot", 1, 1000)
        self.assertLessEqual(size, 50)

    def test_min_notional(self):
        self.assertEqual(self.r.order_size(10000, 5, 100, "spot", 1, None), 0.0)

    def test_futures_leverage_affordability(self):
        size = self.r.order_size(10000, 100, 100, "futures", 5, 10000)
        self.assertLessEqual(size, 500.0)

    def test_kill_switch(self):
        self.cfg["kill_switch"] = True
        ok, why = self.r.can_open({"open_positions": 0, "daily": {}, "cash": 1000})
        self.assertFalse(ok)
        self.assertIn("Kill", why)

    def test_max_positions(self):
        ok, why = self.r.can_open({"open_positions": 3, "daily": {}, "cash": 1000})
        self.assertFalse(ok)

    def test_daily_loss_block(self):
        ok, why = self.r.can_open({"open_positions": 0, "daily": {"pnl_pct": -6, "trades": 1}, "cash": 1000})
        self.assertFalse(ok)
        self.assertIn("zarar", why)

    def test_trade_count_block(self):
        ok, _ = self.r.can_open({"open_positions": 0, "daily": {"pnl_pct": 0, "trades": 999}, "cash": 1000})
        self.assertFalse(ok)

    def test_cooldown(self):
        ok, why = self.r.can_open({"open_positions": 0, "daily": {}, "cash": 1000,
                                   "last_trade_ts": now_ms() - 1000})
        self.assertFalse(ok)
        self.assertIn("Bekleme", why)

    def test_allows_when_clean(self):
        ok, why = self.r.can_open({"open_positions": 0, "daily": {"pnl_pct": 1, "trades": 2}, "cash": 1000})
        self.assertTrue(ok, why)

    def test_stops_long(self):
        stop, target = self.r.stops(100, "long")
        self.assertAlmostEqual(stop, 98.0)
        self.assertAlmostEqual(target, 104.0)

    def test_stops_short(self):
        stop, target = self.r.stops(100, "short")
        self.assertAlmostEqual(stop, 102.0)
        self.assertAlmostEqual(target, 96.0)

    def test_atr_stops(self):
        self.cfg["risk"]["use_atr_stops"] = True
        self.cfg["risk"]["atr_mult"] = 2
        stop, target = self.r.stops(100, "long", atr_value=1.5)
        self.assertAlmostEqual(stop, 97.0)

    def test_exit_stop_and_target(self):
        pos = {"side": "long", "entry": 100, "stop": 98, "target": 104}
        self.assertIn("Stop", self.r.check_exit(dict(pos), 97))
        self.assertIn("Take", self.r.check_exit(dict(pos), 105))
        self.assertIsNone(self.r.check_exit(dict(pos), 100))

    def test_trailing_stop(self):
        self.cfg["risk"]["trailing_pct"] = 2
        pos = {"side": "long", "entry": 100, "stop": 0, "target": 0, "trail_peak": 100}
        self.assertIsNone(self.r.check_exit(pos, 110))
        self.assertAlmostEqual(pos["trail_peak"], 110)
        self.assertAlmostEqual(pos["stop"], 107.8)
        self.assertIn("Stop", self.r.check_exit(pos, 107))

    def test_liquidation_price(self):
        self.assertAlmostEqual(self.r.liquidation_price(100, "long", 10), 90.5)
        self.assertAlmostEqual(self.r.liquidation_price(100, "short", 10), 109.5)

    def test_day_start_alignment(self):
        ts = now_ms()
        self.assertEqual(day_start(ts) % 86_400_000, 0)

    def test_daily_stats_baseline(self):
        s = self.r.daily_stats("paper", 10000)
        self.assertAlmostEqual(s["start_equity"], 10000)
        s2 = self.r.daily_stats("paper", 9500)
        self.assertAlmostEqual(s2["pnl_pct"], -5.0)


class TestSanity(unittest.TestCase):
    def test_live_requires_confirm(self):
        c = make_cfg()
        c["mode"] = "live"
        c["live_confirmed"] = False
        self.assertTrue(any("onay" in w.lower() for w in sanity_check_config(c)))

    def test_high_leverage_warning(self):
        c = make_cfg()
        c["market"] = "futures"
        c["futures"]["leverage"] = 25
        self.assertTrue(any("Kaldirac" in w for w in sanity_check_config(c)))

    def test_no_symbols_warning(self):
        c = make_cfg()
        c["symbols"] = []
        self.assertTrue(any("Aktif sembol" in w for w in sanity_check_config(c)))


class TestRounding(unittest.TestCase):
    def test_round_step(self):
        self.assertAlmostEqual(round_step(0.123456, 0.001), 0.123)
        self.assertAlmostEqual(round_step(1.9999, 0.01), 1.99)
        self.assertAlmostEqual(round_step(5.0, 1.0), 5.0)
        self.assertAlmostEqual(round_step(0.00001234, 0.000001), 0.000012)


if __name__ == "__main__":
    unittest.main()
