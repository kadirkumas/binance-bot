"""Strateji sinyal mantigi testleri (sentetik veri ile)."""
import unittest

from bot.strategies import STRATEGIES, Signal, on_fill, sanitize_params


def ctx_for(key, params=None, ind=None, closes=None, position=None, market="spot",
            state=None, price=100.0, ts=1_700_000_000_000, i=None):
    closes = closes if closes is not None else [100.0, 100.0, 100.0]
    return {"params": sanitize_params(key, params or {}), "ind": ind or {},
            "closes": closes, "candles": [{"t": ts, "c": c, "h": c, "l": c, "o": c} for c in closes],
            "position": position, "market": market, "state": state if state is not None else {},
            "price": price, "ts": ts, "i": len(closes) - 1 if i is None else i, "symbol": "TESTUSDT"}


class TestEmaCross(unittest.TestCase):
    def setUp(self):
        self.s = STRATEGIES["ema_cross"]

    def test_golden_cross_opens_long(self):
        c = ctx_for("ema_cross", {"fast": 2, "slow": 3, "trend_filter": False},
                    ind={"ema_fast": [1, 2, 3], "ema_slow": [2, 2, 2]})
        sigs = self.s.evaluate(c)
        self.assertEqual([x.action for x in sigs], ["open_long"])

    def test_death_cross_closes_long(self):
        c = ctx_for("ema_cross", {"trend_filter": False},
                    ind={"ema_fast": [3, 2, 1], "ema_slow": [2, 2, 2]},
                    position={"side": "long", "qty": 1, "entry": 100})
        self.assertEqual([x.action for x in self.s.evaluate(c)], ["close_long"])

    def test_trend_filter_blocks_long(self):
        c = ctx_for("ema_cross", {"trend_filter": True, "trend_period": 20},
                    ind={"ema_fast": [1, 2, 3], "ema_slow": [2, 2, 2], "ema_trend": [200, 200, 200]},
                    closes=[100, 100, 100])
        self.assertEqual(self.s.evaluate(c), [])

    def test_short_only_on_futures(self):
        ind = {"ema_fast": [3, 2, 1], "ema_slow": [2, 2, 2]}
        spot = ctx_for("ema_cross", {"trend_filter": False, "allow_short": True}, ind=ind, market="spot")
        fut = ctx_for("ema_cross", {"trend_filter": False, "allow_short": True}, ind=ind, market="futures")
        self.assertEqual(self.s.evaluate(spot), [])
        self.assertEqual([x.action for x in self.s.evaluate(fut)], ["open_short"])

    def test_no_signal_without_cross(self):
        c = ctx_for("ema_cross", {"trend_filter": False},
                    ind={"ema_fast": [3, 3, 3], "ema_slow": [2, 2, 2]})
        self.assertEqual(self.s.evaluate(c), [])


class TestRsi(unittest.TestCase):
    def setUp(self):
        self.s = STRATEGIES["rsi_reversion"]

    def test_recovery_buy(self):
        c = ctx_for("rsi_reversion", {"period": 2, "oversold": 30}, ind={"rsi": [None, 25, 35]})
        self.assertEqual([x.action for x in self.s.evaluate(c)], ["open_long"])

    def test_exit_at_mid(self):
        c = ctx_for("rsi_reversion", {"period": 2, "exit_mid": 55}, ind={"rsi": [None, 50, 60]},
                    position={"side": "long", "qty": 1, "entry": 100})
        self.assertIn("close_long", [x.action for x in self.s.evaluate(c)])

    def test_no_confirm_mode(self):
        c = ctx_for("rsi_reversion", {"period": 2, "confirm_cross": False, "oversold": 30},
                    ind={"rsi": [None, 20, 25]})
        self.assertEqual([x.action for x in self.s.evaluate(c)], ["open_long"])


class TestGrid(unittest.TestCase):
    def setUp(self):
        self.s = STRATEGIES["grid"]
        self.params = {"levels": 8, "range_pct": 8, "order_usdt": 100, "max_holdings": 5}

    def test_first_call_sets_anchor(self):
        state = {}
        c = ctx_for("grid", self.params, state=state, price=100.0, ind={"sma_anchor": [None] * 3})
        self.assertEqual(self.s.evaluate(c), [])
        self.assertAlmostEqual(state["anchor"], 100.0)
        self.assertEqual(len(state["grid_levels"]), 9)

    def test_buy_on_step_down_and_sell_on_step_up(self):
        state = {}
        ind0 = {"sma_anchor": [None, None, None]}
        self.s.evaluate(ctx_for("grid", self.params, state=state, price=100.0, ind=ind0))
        down = self.s.evaluate(ctx_for("grid", self.params, state=state, price=98.0, ind=ind0))
        self.assertEqual([x.action for x in down], ["open_long"])
        self.assertEqual(down[0].size_usdt, 100)
        fill = {"qty": down[0].size_usdt / 98.0, "price": 98.0, "notional": 100.0, "ts": 1}
        on_fill("grid", state, down[0], fill)
        pos = {"side": "long", "qty": fill["qty"], "entry": 98.0}
        up = self.s.evaluate(ctx_for("grid", self.params, state=state, price=99.5, ind=ind0, position=pos))
        self.assertEqual([x.action for x in up], ["close_long"])
        self.assertTrue(up[0].meta.get("grid_sell"))
        on_fill("grid", state, up[0], {"qty": up[0].qty, "price": 99.5, "notional": 1, "ts": 2})
        self.assertEqual(state["buys"], [])

    def test_recenter_out_of_range(self):
        state = {}
        ind0 = {"sma_anchor": [None, None, None]}
        self.s.evaluate(ctx_for("grid", self.params, state=state, price=100.0, ind=ind0))
        self.s.evaluate(ctx_for("grid", self.params, state=state, price=80.0, ind=ind0))
        self.assertAlmostEqual(state["anchor"], 80.0)


class TestDca(unittest.TestCase):
    def setUp(self):
        self.s = STRATEGIES["dca"]

    def test_interval_gating(self):
        state = {}
        p = {"every_minutes": 60, "amount_usdt": 50, "max_total_usdt": 1000, "dip_boost_pct": 0}
        ind0 = {"sma_ref": [None, None, None]}
        t0 = 1_700_000_000_000
        first = self.s.evaluate(ctx_for("dca", p, state=state, ts=t0, ind=ind0))
        self.assertEqual([x.action for x in first], ["open_long"])
        on_fill("dca", state, first[0], {"qty": 0.5, "price": 100, "notional": 50, "ts": t0})
        again = self.s.evaluate(ctx_for("dca", p, state=state, ts=t0 + 60_000, ind=ind0,
                                        position={"side": "long", "qty": 0.5, "entry": 100}))
        self.assertEqual(again, [])
        later = self.s.evaluate(ctx_for("dca", p, state=state, ts=t0 + 3_700_000, ind=ind0,
                                        position={"side": "long", "qty": 0.5, "entry": 100}))
        self.assertEqual([x.action for x in later], ["open_long"])

    def test_take_profit(self):
        p = {"take_profit_pct": 5, "use_take_profit": True}
        c = ctx_for("dca", p, ind={"sma_ref": [None] * 3}, price=110.0,
                    position={"side": "long", "qty": 1, "entry": 100}, state={"invested": 100})
        sigs = self.s.evaluate(c)
        self.assertEqual([x.action for x in sigs], ["close_long"])

    def test_max_total_cap(self):
        p = {"amount_usdt": 50, "max_total_usdt": 100, "dip_boost_pct": 0}
        state = {"invested": 100, "last_buy_ts": 0}
        c = ctx_for("dca", p, state=state, ind={"sma_ref": [None] * 3},
                    position={"side": "long", "qty": 1, "entry": 100})
        self.assertEqual(self.s.evaluate(c), [])

    def test_dip_multiplier(self):
        p = {"amount_usdt": 50, "dip_boost_pct": 3, "dip_multiplier": 2, "max_total_usdt": 1000}
        c = ctx_for("dca", p, ind={"sma_ref": [110, 110, 110]}, price=100.0, closes=[100, 100, 100])
        sigs = self.s.evaluate(c)
        self.assertAlmostEqual(sigs[0].size_usdt, 100.0)


class TestSanitize(unittest.TestCase):
    def test_clamps_and_types(self):
        p = sanitize_params("ema_cross", {"fast": "9", "slow": 9999, "trend_filter": "true"})
        self.assertEqual(p["fast"], 9)
        self.assertEqual(p["slow"], 400)
        self.assertTrue(p["trend_filter"])

    def test_unknown_strategy(self):
        self.assertEqual(sanitize_params("yok", {"a": 1}), {})

    def test_defaults_complete(self):
        for key, s in STRATEGIES.items():
            d = s.defaults()
            self.assertEqual(set(d), {x["name"] for x in s.schema})


if __name__ == "__main__":
    unittest.main()
