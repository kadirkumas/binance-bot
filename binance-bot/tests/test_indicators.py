"""Indikator dogruluk testleri."""
import unittest

from bot import indicators as ind


class TestIndicators(unittest.TestCase):
    def test_sma(self):
        v = [1, 2, 3, 4, 5]
        self.assertEqual(ind.sma(v, 3), [None, None, 2.0, 3.0, 4.0])

    def test_ema_seed_and_recursion(self):
        v = [1, 2, 3, 4, 5, 6]
        e = ind.ema(v, 3)
        self.assertIsNone(e[1])
        self.assertAlmostEqual(e[2], 2.0)          # seed = SMA(3)
        k = 2 / 4
        self.assertAlmostEqual(e[3], 4 * k + 2 * (1 - k))

    def test_ema_short_series(self):
        self.assertEqual(ind.ema([1, 2], 5), [None, None])

    def test_rsi_all_gains(self):
        v = list(range(1, 30))
        r = ind.rsi(v, 14)
        self.assertAlmostEqual(r[-1], 100.0)

    def test_rsi_all_losses(self):
        v = list(range(30, 1, -1))
        r = ind.rsi(v, 14)
        self.assertAlmostEqual(r[-1], 0.0)

    def test_rsi_bounds(self):
        v = [10, 11, 10.5, 12, 11, 13, 12.5, 14, 13, 15, 14, 16, 15, 17, 16, 18, 17, 19]
        for x in ind.rsi(v, 14):
            if x is not None:
                self.assertTrue(0 <= x <= 100)

    def test_atr_positive(self):
        # kapanis = yuksek -> TR = h - l = 1 (prev_close ile cakisma yok)
        candles = [{"h": 10 + i, "l": 9 + i, "c": 10 + i} for i in range(30)]
        a = ind.atr(candles, 14)
        self.assertIsNotNone(a[-1])
        self.assertAlmostEqual(a[-1], 1.0, places=6)

    def test_crosses(self):
        a = [1, 2, 3]
        b = [2, 2, 2]
        self.assertTrue(ind.crosses_above(a, b, 2))
        self.assertFalse(ind.crosses_below(a, b, 2))
        self.assertTrue(ind.crosses_below(b, a, 2))

    def test_bollinger(self):
        v = [float(x) for x in range(1, 41)]
        lo, mid, up = ind.bollinger(v, 20, 2)
        self.assertLess(lo[-1], mid[-1])
        self.assertGreater(up[-1], mid[-1])

    def test_macd_lengths(self):
        v = [float(x) for x in range(1, 80)]
        line, sig, hist = ind.macd(v)
        self.assertEqual(len(line), len(v))
        self.assertEqual(len(sig), len(v))
        self.assertIsNotNone(hist[-1])

    def test_max_drawdown(self):
        self.assertAlmostEqual(ind.max_drawdown([100, 120, 60, 90]), 50.0)
        self.assertEqual(ind.max_drawdown([100, 110, 120]), 0.0)

    def test_sharpe_zero_vol(self):
        self.assertEqual(ind.sharpe([0.01, 0.01, 0.01]), 0.0)


if __name__ == "__main__":
    unittest.main()
