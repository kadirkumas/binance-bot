"""Binance imzalama (HMAC-SHA256) ve istemci yardimcilari - ag gerektirmez."""
import hashlib
import hmac
import unittest
import urllib.parse

from bot.exchange import BASES, BinanceClient, BinanceError
from bot.util import interval_ms


class TestSigning(unittest.TestCase):
    def test_binance_docs_vector(self):
        """Binance dokumanindaki ornek HMAC-SHA256 imzasi ile birebir ayni olmali."""
        secret = "NhqPtmdSJYdKjVHjA7PZj4Mge3R5YNiP1e3UZjInClVN65XAbvqqM6A7H5fATj0j"
        params = {"symbol": "LTCBTC", "side": "BUY", "type": "LIMIT", "timeInForce": "GTC",
                  "quantity": 1, "price": 0.1, "recvWindow": 5000, "timestamp": 1499827319559}
        qs = urllib.parse.urlencode(params, doseq=True)
        sig = hmac.new(secret.encode(), qs.encode(), hashlib.sha256).hexdigest()
        self.assertEqual(sig, "c8db56825ae71d6d79447849e617115f4a920fa2acdcab2b053c4b2838bd6b71")

    def test_bases(self):
        self.assertEqual(BASES[("spot", "live")], "https://api.binance.com")
        self.assertEqual(BASES[("spot", "testnet")], "https://testnet.binance.vision")
        self.assertEqual(BASES[("futures", "live")], "https://fapi.binance.com")
        self.assertEqual(BASES[("futures", "testnet")], "https://testnet.binancefuture.com")

    def test_public_data_always_live(self):
        c = BinanceClient("spot", "testnet", "k", "s")
        self.assertEqual(c.public_base, "https://api.binance.com")
        self.assertEqual(c.trade_base, "https://testnet.binance.vision")

    def test_paths(self):
        spot = BinanceClient("spot")
        fut = BinanceClient("futures")
        self.assertEqual(spot.p("klines"), "/api/v3/klines")
        self.assertEqual(fut.p("klines"), "/fapi/v1/klines")
        self.assertEqual(fut.p("positionRisk"), "/fapi/v2/positionRisk")

    def test_signed_without_keys_raises(self):
        c = BinanceClient("spot", "live", "", "")
        with self.assertRaises(BinanceError):
            c.signed("account")

    def test_interval_ms(self):
        self.assertEqual(interval_ms("1m"), 60_000)
        self.assertEqual(interval_ms("1d"), 86_400_000)

    def test_symbol_info_fallback(self):
        c = BinanceClient("spot")
        c._filters = {"BTCUSDT": {"step": 0.00001, "tick": 0.01, "min_qty": 0.00001,
                                  "min_notional": 10, "qty_precision": 5, "status": "TRADING"}}
        c._filters_ts = 9_999_999_999_999
        self.assertAlmostEqual(c.round_qty("BTCUSDT", 0.123456789), 0.12345)
        self.assertAlmostEqual(c.round_price("BTCUSDT", 73866.119), 73866.11)


if __name__ == "__main__":
    unittest.main()
