"""Binance REST istemcisi - sadece standart kutuphane (urllib + hmac).

Piyasa verisi HER ZAMAN canli public uclardan alinir (testnet verisi ince/sunidir).
Emirler ise secilen moda gore canli veya testnet ucuna gonderilir.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import OrderedDict

from .util import LOG, fnum, fmt_qty, now_ms, round_step

BASES = {
    ("spot", "live"): "https://api.binance.com",
    ("spot", "testnet"): "https://testnet.binance.vision",
    ("futures", "live"): "https://fapi.binance.com",
    ("futures", "testnet"): "https://testnet.binancefuture.com",
}

USER_AGENT = "Mozilla/5.0 (compatible; QwenBinanceBot/1.0)"


class BinanceError(Exception):
    def __init__(self, message: str, code: int = 0, status: int = 0):
        super().__init__(message)
        self.code = code
        self.status = status


class RateLimiter:
    def __init__(self, min_interval: float = 0.08):
        self.min_interval = min_interval
        self._last = 0.0
        self._lock = threading.Lock()
        self._blocked_until = 0.0

    def wait(self):
        with self._lock:
            now = time.monotonic()
            if now < self._blocked_until:
                time.sleep(self._blocked_until - now)
            delta = time.monotonic() - self._last
            if delta < self.min_interval:
                time.sleep(self.min_interval - delta)
            self._last = time.monotonic()

    def block(self, seconds: float):
        with self._lock:
            self._blocked_until = max(self._blocked_until, time.monotonic() + seconds)


LIMITER = RateLimiter()


class BinanceClient:
    def __init__(self, market: str = "spot", mode: str = "live",
                 api_key: str = "", api_secret: str = "", timeout: float = 15.0):
        self.market = market if market in ("spot", "futures") else "spot"
        self.mode = mode if mode in ("live", "testnet") else "live"
        self.api_key = api_key or ""
        self.api_secret = api_secret or ""
        self.timeout = timeout
        self._time_offset = 0
        self._filters: dict[str, dict] = {}
        self._filters_ts = 0
        self._lock = threading.RLock()
        self.last_latency_ms = 0
        self.last_error = ""

    # ---------- bases / paths ----------
    @property
    def public_base(self) -> str:
        return BASES[(self.market, "live")]

    @property
    def trade_base(self) -> str:
        return BASES[(self.market, self.mode)]

    def p(self, name: str) -> str:
        if self.market == "spot":
            return {
                "klines": "/api/v3/klines", "time": "/api/v3/time",
                "exchangeInfo": "/api/v3/exchangeInfo", "price": "/api/v3/ticker/price",
                "ticker24": "/api/v3/ticker/24hr", "book": "/api/v3/ticker/bookTicker",
                "account": "/api/v3/account", "order": "/api/v3/order",
                "openOrders": "/api/v3/openOrders", "myTrades": "/api/v3/myTrades",
            }[name]
        return {
            "klines": "/fapi/v1/klines", "time": "/fapi/v1/time",
            "exchangeInfo": "/fapi/v1/exchangeInfo", "price": "/fapi/v1/ticker/price",
            "ticker24": "/fapi/v1/ticker/24hr", "book": "/fapi/v1/ticker/bookTicker",
            "account": "/fapi/v2/balance", "order": "/fapi/v1/order",
            "openOrders": "/fapi/v1/openOrders", "myTrades": "/fapi/v1/userTrades",
            "positionRisk": "/fapi/v2/positionRisk", "leverage": "/fapi/v1/leverage",
            "marginType": "/fapi/v1/marginType", "positionSideDual": "/fapi/v1/positionSideDual",
        }[name]

    # ---------- low level ----------
    def _http(self, url: str, method: str = "GET", headers: dict | None = None,
              body: bytes | None = None, retries: int = 3) -> object:
        last_exc: Exception | None = None
        for attempt in range(retries):
            LIMITER.wait()
            req = urllib.request.Request(url, data=body, method=method)
            req.add_header("User-Agent", USER_AGENT)
            req.add_header("Accept", "application/json")
            for k, v in (headers or {}).items():
                req.add_header(k, v)
            if body is not None:
                req.add_header("Content-Type", "application/x-www-form-urlencoded")
            t0 = time.monotonic()
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    raw = resp.read().decode()
                    self.last_latency_ms = int((time.monotonic() - t0) * 1000)
                    return json.loads(raw) if raw else {}
            except urllib.error.HTTPError as exc:
                payload = ""
                try:
                    payload = exc.read().decode()
                except Exception:
                    pass
                err = {}
                try:
                    err = json.loads(payload)
                except Exception:
                    pass
                code = int(err.get("code", 0) or 0)
                msg = err.get("msg") or payload[:200] or str(exc)
                self.last_error = f"HTTP {exc.code} {code} {msg}"
                if exc.code in (429, 418):
                    delay = fnum(exc.headers.get("Retry-After"), 2.0) or 2.0
                    LIMITER.block(min(60.0, delay * (attempt + 1)))
                    LOG.warn(f"Rate limit ({exc.code}), {delay}s bekleniyor")
                    last_exc = BinanceError(msg, code, exc.code)
                    time.sleep(min(5.0, delay))
                    continue
                if exc.code >= 500 and attempt < retries - 1:
                    time.sleep(0.6 * (attempt + 1))
                    last_exc = BinanceError(msg, code, exc.code)
                    continue
                raise BinanceError(msg, code, exc.code) from None
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                last_exc = BinanceError(f"Ag hatasi: {exc}", 0, 0)
                self.last_error = str(exc)
                if attempt < retries - 1:
                    time.sleep(0.7 * (attempt + 1))
                    continue
            except json.JSONDecodeError as exc:
                last_exc = BinanceError(f"Gecersiz JSON yanit: {exc}")
                if attempt < retries - 1:
                    time.sleep(0.5)
                    continue
        raise last_exc or BinanceError("Bilinmeyen istek hatasi")

    def public(self, name: str, params: dict | None = None):
        qs = urllib.parse.urlencode(params or {})
        url = self.public_base + self.p(name) + ("?" + qs if qs else "")
        return self._http(url)

    def sync_time(self) -> int:
        try:
            data = self._http(self.trade_base + self.p("time"))
            self._time_offset = int(data["serverTime"]) - now_ms()
        except Exception as exc:
            LOG.warn(f"Sunucu saati senkronize edilemedi: {exc}")
        return self._time_offset

    def signed(self, name_or_path: str, params: dict | None = None, method: str = "GET",
               base: str | None = None):
        if not self.api_key or not self.api_secret:
            raise BinanceError("API anahtari/secret tanimli degil", 0, 0)
        path = self.p(name_or_path) if not name_or_path.startswith("/") else name_or_path
        p = dict(params or {})
        if self._time_offset == 0:
            self.sync_time()
        p["timestamp"] = now_ms() + self._time_offset
        p.setdefault("recvWindow", 5000)
        qs = urllib.parse.urlencode(p, doseq=True)
        sig = hmac.new(self.api_secret.encode(), qs.encode(), hashlib.sha256).hexdigest()
        url = (base or self.trade_base) + path + "?" + qs + "&signature=" + sig
        headers = {"X-MBX-APIKEY": self.api_key}
        return self._http(url, method=method, headers=headers)

    # ---------- market data ----------
    def klines(self, symbol: str, interval: str = "5m", limit: int = 300,
               start: int | None = None, end: int | None = None,
               drop_open: bool = True) -> list[dict]:
        params = {"symbol": symbol.upper(), "interval": interval, "limit": int(min(1500, max(1, limit)))}
        if start:
            params["startTime"] = int(start)
        if end:
            params["endTime"] = int(end)
        raw = self.public("klines", params)
        out = []
        now = now_ms()
        for k in raw:
            item = {"t": int(k[0]), "o": fnum(k[1]), "h": fnum(k[2]), "l": fnum(k[3]),
                    "c": fnum(k[4]), "v": fnum(k[5]), "T": int(k[6])}
            if drop_open and item["T"] > now:
                continue
            out.append(item)
        return out

    def klines_range(self, symbol: str, interval: str, start: int, end: int,
                     max_candles: int = 6000) -> list[dict]:
        """Backtest icin sayfalama ile gecmis mumlar."""
        from .util import interval_ms
        step = interval_ms(interval)
        out: list[dict] = []
        cursor = start
        seen = set()
        while cursor < end and len(out) < max_candles:
            batch = self.klines(symbol, interval, limit=1500, start=cursor, end=end, drop_open=False)
            if not batch:
                break
            new = 0
            for c in batch:
                if c["t"] in seen:
                    continue
                seen.add(c["t"])
                out.append(c)
                new += 1
            last_t = batch[-1]["t"]
            nxt = last_t + step
            if nxt <= cursor or new == 0:
                break
            cursor = nxt
            if len(batch) < 2:
                break
        out.sort(key=lambda c: c["t"])
        return [c for c in out if start <= c["t"] <= end][:max_candles]

    def price(self, symbol: str) -> float:
        data = self.public("price", {"symbol": symbol.upper()})
        return fnum(data.get("price"))

    def prices(self, symbols: list[str]) -> dict[str, float]:
        if not symbols:
            return {}
        if len(symbols) == 1:
            try:
                return {symbols[0].upper(): self.price(symbols[0])}
            except BinanceError:
                return {}
        params = {"symbols": json.dumps([s.upper() for s in symbols], separators=(",", ":"))}
        data = self.public("price", params)
        if isinstance(data, list):
            return {d["symbol"]: fnum(d["price"]) for d in data}
        return {}

    def tickers(self, symbols: list[str]) -> dict[str, dict]:
        if not symbols:
            return {}
        params = {"symbols": json.dumps([s.upper() for s in symbols], separators=(",", ":"))}
        try:
            data = self.public("ticker24", params)
        except BinanceError:
            return {}
        out = {}
        if isinstance(data, list):
            for d in data:
                out[d["symbol"]] = {
                    "price": fnum(d.get("lastPrice")),
                    "change_pct": fnum(d.get("priceChangePercent")),
                    "high": fnum(d.get("highPrice")), "low": fnum(d.get("lowPrice")),
                    "volume": fnum(d.get("quoteVolume")),
                }
        return out

    # ---------- symbol filters ----------
    def load_filters(self, force: bool = False) -> dict[str, dict]:
        with self._lock:
            if self._filters and not force and now_ms() - self._filters_ts < 3_600_000:
                return self._filters
            data = self.public("exchangeInfo")
            filters = {}
            for s in data.get("symbols", []):
                f = {fl["filterType"]: fl for fl in s.get("filters", [])}
                lot = f.get("LOT_SIZE") or f.get("MARKET_LOT_SIZE") or {}
                pf = f.get("PRICE_FILTER") or {}
                notional = f.get("MIN_NOTIONAL") or f.get("NOTIONAL") or {}
                filters[s["symbol"]] = {
                    "status": s.get("status"),
                    "step": fnum(lot.get("stepSize"), 0.0001) or 0.0001,
                    "min_qty": fnum(lot.get("minQty"), 0.0),
                    "tick": fnum(pf.get("tickSize"), 0.01) or 0.01,
                    "min_notional": fnum(notional.get("minNotional"), 5.0),
                    "base": s.get("baseAsset", ""), "quote": s.get("quoteAsset", ""),
                    "qty_precision": int(s.get("quantityPrecision", 6) or 6),
                    "price_precision": int(s.get("pricePrecision", 2) or 2),
                }
            self._filters = filters
            self._filters_ts = now_ms()
            return filters

    def symbol_info(self, symbol: str) -> dict:
        symbol = symbol.upper()
        try:
            filters = self.load_filters()
        except BinanceError:
            filters = {}
        return filters.get(symbol, {"step": 0.00001, "min_qty": 0.0, "tick": 0.01,
                                    "min_notional": 5.0, "qty_precision": 6, "status": "TRADING"})

    def search_symbols(self, query: str = "", quote: str = "USDT", limit: int = 40) -> list[dict]:
        try:
            filters = self.load_filters()
        except BinanceError as exc:
            LOG.warn(f"Sembol listesi alinamadi: {exc}")
            return []
        q = (query or "").upper()
        out = []
        for sym, info in filters.items():
            if info.get("status") not in ("TRADING", None):
                continue
            if quote and not sym.endswith(quote):
                continue
            if q and q not in sym:
                continue
            out.append({"symbol": sym, "min_notional": info["min_notional"], "step": info["step"]})
            if len(out) >= limit:
                break
        return sorted(out, key=lambda x: x["symbol"])

    def round_qty(self, symbol: str, qty: float) -> float:
        info = self.symbol_info(symbol)
        return round_step(qty, info["step"])

    def round_price(self, symbol: str, price: float) -> float:
        info = self.symbol_info(symbol)
        return round_step(price, info["tick"])

    # ---------- account / trading ----------
    def test_connection(self) -> dict:
        if self.market == "spot":
            acc = self.signed("account")
            balances = {b["asset"]: fnum(b["free"]) for b in acc.get("balances", []) if fnum(b["free"]) > 0}
            return {"ok": True, "type": "spot", "canTrade": acc.get("canTrade"),
                    "permissions": acc.get("permissions", []), "balances": balances}
        bal = self.signed("account")
        out = {b["asset"]: fnum(b["balance"]) for b in bal if fnum(b["balance"]) != 0}
        return {"ok": True, "type": "futures", "balances": out}

    def balances(self) -> dict[str, float]:
        if self.market == "spot":
            acc = self.signed("account")
            return {b["asset"]: fnum(b["free"]) for b in acc.get("balances", [])}
        bal = self.signed("account")
        return {b["asset"]: fnum(b["availableBalance"]) for b in bal}

    def set_leverage(self, symbol: str, leverage: int, margin_mode: str = "ISOLATED") -> None:
        if self.market != "futures":
            return
        try:
            self.signed("marginType", {"symbol": symbol.upper(), "marginType": margin_mode}, method="POST")
        except BinanceError as exc:
            if "No need to change" not in str(exc):
                LOG.warn(f"Marjin modu ayarlanamadi {symbol}: {exc}")
        try:
            self.signed("leverage", {"symbol": symbol.upper(), "leverage": int(leverage)}, method="POST")
        except BinanceError as exc:
            LOG.warn(f"Kaldirac ayarlanamadi {symbol}: {exc}")

    def market_order(self, symbol: str, side: str, qty: float | None = None,
                     quote_qty: float | None = None, reduce_only: bool = False,
                     position_side: str | None = None, client_id: str | None = None) -> dict:
        symbol = symbol.upper()
        params: dict = {"symbol": symbol, "side": side.upper(), "type": "MARKET"}
        if client_id:
            params["newClientOrderId"] = client_id[:36]
        if self.market == "spot":
            if quote_qty is not None and side.upper() == "BUY":
                params["quoteOrderQty"] = fmt_qty(quote_qty, 8)
            else:
                params["quantity"] = fmt_qty(self.round_qty(symbol, qty or 0.0))
            params["newOrderRespType"] = "FULL"
        else:
            params["quantity"] = fmt_qty(self.round_qty(symbol, qty or 0.0))
            if reduce_only:
                params["reduceOnly"] = "true"
            if position_side:
                params["positionSide"] = position_side
        return self.signed("order", params, method="POST")

    def close_market(self, symbol: str, qty: float, side: str, position_side: str | None = None) -> dict:
        return self.market_order(symbol, side, qty=qty, reduce_only=True, position_side=position_side)

    def open_orders(self, symbol: str | None = None) -> list:
        params = {"symbol": symbol.upper()} if symbol else {}
        return self.signed("openOrders", params)

    def cancel_all(self, symbol: str) -> dict | list:
        path = "/api/v3/openOrders" if self.market == "spot" else "/fapi/v1/allOpenOrders"
        return self.signed(path, {"symbol": symbol.upper()}, method="DELETE")

    def position_risk(self, symbol: str | None = None) -> list[dict]:
        if self.market != "futures":
            return []
        params = {"symbol": symbol.upper()} if symbol else {}
        data = self.signed("positionRisk", params)
        out = []
        for p in data if isinstance(data, list) else []:
            amt = fnum(p.get("positionAmt"))
            if abs(amt) <= 0:
                continue
            out.append({
                "symbol": p["symbol"], "qty": abs(amt),
                "side": "long" if amt > 0 else "short",
                "entry": fnum(p.get("entryPrice")),
                "mark": fnum(p.get("markPrice")),
                "unrealized": fnum(p.get("unRealizedProfit")),
                "leverage": int(fnum(p.get("leverage"), 1)),
                "liquidation": fnum(p.get("liquidationPrice")),
            })
        return out


class PublicData:
    """Moddan bagimsiz, sadece piyasa verisi saglayan hafif istemci havuzu."""

    def __init__(self):
        self._clients: dict[str, BinanceClient] = {}
        self._lock = threading.Lock()
        self._kline_cache: "OrderedDict[tuple, tuple[int, list]]" = OrderedDict()

    def client(self, market: str = "spot") -> BinanceClient:
        with self._lock:
            if market not in self._clients:
                self._clients[market] = BinanceClient(market=market, mode="live")
            return self._clients[market]

    def klines(self, symbol: str, interval: str, limit: int = 300, market: str = "spot",
               ttl_ms: int = 1500) -> list[dict]:
        key = (market, symbol.upper(), interval, limit)
        now = now_ms()
        with self._lock:
            hit = self._kline_cache.get(key)
            if hit and now - hit[0] < ttl_ms:
                return hit[1]
        data = self.client(market).klines(symbol, interval, limit)
        with self._lock:
            self._kline_cache[key] = (now, data)
            while len(self._kline_cache) > 120:
                self._kline_cache.popitem(last=False)
        return data

    def prices(self, symbols: list[str], market: str = "spot") -> dict[str, float]:
        return self.client(market).prices(symbols)

    def tickers(self, symbols: list[str], market: str = "spot") -> dict[str, dict]:
        return self.client(market).tickers(symbols)


DATA = PublicData()
