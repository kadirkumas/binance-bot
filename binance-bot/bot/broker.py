"""Emir yurutme katmani: Paper (simulasyon) ve Live/Testnet broker'lari.

Her iki broker da ayni arayuzu sunar; motor (engine) hangisiyle calistigini bilmez.
"""
from __future__ import annotations

import threading
import uuid

from .exchange import BinanceClient, BinanceError
from .util import LOG, fnum, now_ms


class BrokerError(Exception):
    pass


def new_position(symbol: str, market: str, side: str, leverage: int = 1) -> dict:
    return {
        "symbol": symbol, "market": market, "side": side, "qty": 0.0, "entry": 0.0,
        "leverage": leverage, "margin": 0.0, "invested": 0.0, "fees": 0.0,
        "opened_ts": now_ms(), "updated_ts": now_ms(), "stop": 0.0, "target": 0.0,
        "trail_peak": 0.0, "strategy": "", "realized": 0.0, "liquidation": 0.0,
    }


def pos_unrealized(pos: dict, price: float) -> float:
    if not pos or not price:
        return 0.0
    direction = 1.0 if pos["side"] == "long" else -1.0
    return (price - pos["entry"]) * pos["qty"] * direction


def pos_value(pos: dict, price: float) -> float:
    return pos["qty"] * price


def pos_pnl_pct(pos: dict, price: float) -> float:
    if not pos or not pos.get("entry"):
        return 0.0
    direction = 1.0 if pos["side"] == "long" else -1.0
    lev = pos.get("leverage", 1) if pos["market"] == "futures" else 1
    return (price - pos["entry"]) / pos["entry"] * 100.0 * direction * max(1, lev)


class PaperBroker:
    """Gercek piyasa verisi + sanal bakiye. Emirler aninda, komisyon ve kayma ile dolar."""

    kind = "paper"

    def __init__(self, cfg: dict, store, market: str = "spot"):
        self.cfg = cfg
        self.store = store
        self.market = market
        self.mode = "paper"
        self._lock = threading.RLock()
        self.key = f"paper_book:{market}"
        self.book = self._load()

    def _load(self) -> dict:
        book = self.store.get_state(self.key)
        start = float(self.cfg.get("paper", {}).get("start_balance", 10000))
        if not book:
            book = {"cash": start, "start_balance": start, "positions": {},
                    "fees": 0.0, "realized": 0.0, "trades": 0, "created": now_ms()}
            self.store.set_state(self.key, book)
        return book

    def _save(self) -> None:
        self.store.set_state(self.key, self.book)

    def update_cfg(self, cfg: dict) -> None:
        self.cfg = cfg

    def reset(self, start_balance: float | None = None) -> None:
        with self._lock:
            start = float(start_balance if start_balance is not None
                          else self.cfg.get("paper", {}).get("start_balance", 10000))
            self.book = {"cash": start, "start_balance": start, "positions": {},
                         "fees": 0.0, "realized": 0.0, "trades": 0, "created": now_ms()}
            self._save()
        LOG.warn(f"Paper hesap sifirlandi ({self.market}): {start:.2f} USDT")

    @property
    def fee_pct(self) -> float:
        if self.market == "futures":
            return float(self.cfg.get("futures", {}).get("fee_pct", 0.04))
        return float(self.cfg.get("paper", {}).get("fee_pct", 0.1))

    @property
    def slippage_pct(self) -> float:
        return float(self.cfg.get("paper", {}).get("slippage_pct", 0.02))

    def positions(self) -> dict[str, dict]:
        return self.book["positions"]

    def sync(self) -> None:
        pass

    # ---------- emirler ----------
    def open(self, symbol: str, side: str, usdt: float, price: float, leverage: int = 1,
             strategy: str = "", reason: str = "", ts: int | None = None) -> dict:
        ts = ts or now_ms()
        if usdt <= 0 or price <= 0:
            raise BrokerError("Gecersiz emir buyuklugu")
        if self.market == "spot" and side == "short":
            raise BrokerError("Spot piyasada short acilamaz")
        with self._lock:
            slip = self.slippage_pct / 100.0
            px = price * (1 + slip) if side == "long" else price * (1 - slip)
            qty = usdt / px
            fee = usdt * self.fee_pct / 100.0
            lev = max(1, int(leverage)) if self.market == "futures" else 1
            need = (usdt / lev + fee) if self.market == "futures" else (usdt + fee)
            pos = self.book["positions"].get(symbol)
            if pos and pos["side"] != side:
                self.close(symbol, price, reason="Ters sinyal - pozisyon kapatildi", ts=ts)
                pos = None
            if self.book["cash"] < need:
                raise BrokerError(f"Yetersiz bakiye: gerekli {need:.2f}, mevcut {self.book['cash']:.2f}")
            if not pos:
                pos = new_position(symbol, self.market, side, lev)
                pos["strategy"] = strategy
                self.book["positions"][symbol] = pos
            total_qty = pos["qty"] + qty
            pos["entry"] = (pos["entry"] * pos["qty"] + px * qty) / total_qty if total_qty else px
            pos["qty"] = total_qty
            pos["invested"] += usdt
            pos["fees"] += fee
            pos["leverage"] = lev
            pos["updated_ts"] = ts
            if self.market == "futures":
                pos["margin"] += usdt / lev
                buf = (1.0 / lev) - 0.005
                pos["liquidation"] = pos["entry"] * (1 - buf) if side == "long" else pos["entry"] * (1 + buf)
            self.book["cash"] -= need
            self.book["fees"] += fee
            self.book["trades"] += 1
            self._save()
            return {"ts": ts, "symbol": symbol, "market": self.market, "mode": "paper",
                    "side": "buy" if side == "long" else "sell",
                    "action": "open_short" if side == "short" else "open_long",
                    "qty": qty, "price": px, "notional": usdt, "fee": fee,
                    "realized_pnl": 0.0, "reason": reason, "strategy": strategy,
                    "order_id": "paper-" + uuid.uuid4().hex[:12]}

    def close(self, symbol: str, price: float, portion: float = 1.0, qty: float | None = None,
              reason: str = "", strategy: str = "", ts: int | None = None) -> dict | None:
        ts = ts or now_ms()
        with self._lock:
            pos = self.book["positions"].get(symbol)
            if not pos or pos["qty"] <= 0:
                return None
            q = min(pos["qty"], qty if qty is not None else pos["qty"] * max(0.0, min(1.0, portion)))
            if q <= 0:
                return None
            frac = q / pos["qty"]
            slip = self.slippage_pct / 100.0
            px = price * (1 - slip) if pos["side"] == "long" else price * (1 + slip)
            notional = q * px
            fee = notional * self.fee_pct / 100.0
            direction = 1.0 if pos["side"] == "long" else -1.0
            gross = (px - pos["entry"]) * q * direction
            entry_fee_part = pos["fees"] * frac
            realized = gross - fee - entry_fee_part
            if self.market == "futures":
                released = pos["margin"] * frac
                self.book["cash"] += released + gross - fee
                pos["margin"] -= released
            else:
                self.book["cash"] += notional - fee
            pos["qty"] -= q
            pos["fees"] -= entry_fee_part
            pos["invested"] *= (1 - frac)
            pos["realized"] += realized
            pos["updated_ts"] = ts
            self.book["realized"] += realized
            self.book["fees"] += fee
            self.book["trades"] += 1
            strategy = strategy or pos.get("strategy", "")
            if pos["qty"] <= 1e-12:
                self.book["positions"].pop(symbol, None)
            self._save()
            return {"ts": ts, "symbol": symbol, "market": self.market, "mode": "paper",
                    "side": "sell" if pos["side"] == "long" else "buy",
                    "action": "close_short" if pos["side"] == "short" else "close_long",
                    "qty": q, "price": px, "notional": notional, "fee": fee,
                    "realized_pnl": realized, "reason": reason, "strategy": strategy,
                    "order_id": "paper-" + uuid.uuid4().hex[:12]}

    # ---------- durum ----------
    def snapshot(self, prices: dict[str, float]) -> dict:
        with self._lock:
            positions = []
            unreal = 0.0
            value = 0.0
            margin = 0.0
            for sym, pos in self.book["positions"].items():
                px = prices.get(sym) or pos["entry"]
                u = pos_unrealized(pos, px)
                unreal += u
                value += pos_value(pos, px)
                margin += pos.get("margin", 0.0)
                positions.append({**pos, "price": px, "unrealized": u,
                                  "pnl_pct": pos_pnl_pct(pos, px), "value": pos_value(pos, px)})
            cash = self.book["cash"]
            equity = cash + (value if self.market == "spot" else margin + unreal)
            return {"mode": "paper", "market": self.market, "cash": cash, "equity": equity,
                    "unrealized": unreal, "positions": positions,
                    "start_balance": self.book.get("start_balance", cash),
                    "fees": self.book.get("fees", 0.0), "realized": self.book.get("realized", 0.0),
                    "balances": {"USDT": cash}}

    def check_liquidations(self, prices: dict[str, float]) -> list[dict]:
        if self.market != "futures":
            return []
        out = []
        for sym, pos in list(self.book["positions"].items()):
            px = prices.get(sym)
            if not px:
                continue
            liq = pos.get("liquidation") or 0
            hit = (px <= liq) if pos["side"] == "long" else (px >= liq)
            if liq and hit:
                fill = self.close(sym, liq, reason=f"LIKIDASYON @ {liq:.6f}")
                if fill:
                    out.append(fill)
        return out


class LiveBroker:
    """Gercek / testnet emirleri. Pozisyon takibi: futures'ta borsadan, spot'ta yerel defter."""

    kind = "live"

    def __init__(self, cfg: dict, store, market: str, mode: str, client: BinanceClient):
        self.cfg = cfg
        self.store = store
        self.market = market
        self.mode = mode          # live | testnet
        self.client = client
        self._lock = threading.RLock()
        self._balances: dict[str, float] = {}
        self._futures_positions: dict[str, dict] = {}
        self._equity = 0.0
        self._last_sync = 0
        self._hedge_mode = False
        self._lev_set: set[str] = set()
        self.inv_key = f"live_inventory:{mode}:{market}"
        self.inventory: dict[str, dict] = store.get_state(self.inv_key) or {}
        if market == "futures":
            try:
                dual = client.signed("positionSideDual")
                self._hedge_mode = bool(dual.get("dualSidePosition"))
            except (BinanceError, Exception) as exc:
                LOG.warn(f"Hedge mod sorgulanamadi: {exc}")

    def update_cfg(self, cfg: dict) -> None:
        self.cfg = cfg

    def _save_inv(self) -> None:
        self.store.set_state(self.inv_key, self.inventory)

    def sync(self, force: bool = True) -> None:
        with self._lock:
            try:
                if self.market == "spot":
                    self._balances = self.client.balances()
                    self._equity = 0.0
                else:
                    acc = self.client.signed("/fapi/v2/account")
                    self._equity = fnum(acc.get("totalMarginBalance"))
                    self._balances = {a["asset"]: fnum(a.get("availableBalance"))
                                      for a in acc.get("assets", [])}
                    self._futures_positions = {}
                    for p in self.client.position_risk():
                        pos = new_position(p["symbol"], "futures", p["side"], p["leverage"])
                        prev = self.inventory.get(p["symbol"], {})
                        pos.update({"qty": p["qty"], "entry": p["entry"],
                                    "liquidation": p["liquidation"],
                                    "stop": prev.get("stop", 0.0), "target": prev.get("target", 0.0),
                                    "trail_peak": prev.get("trail_peak", 0.0),
                                    "strategy": prev.get("strategy", ""),
                                    "margin": p["qty"] * p["entry"] / max(1, p["leverage"])})
                        self._futures_positions[p["symbol"]] = pos
                        self.inventory[p["symbol"]] = {k: pos[k] for k in
                                                       ("stop", "target", "trail_peak", "strategy", "side")}
                    for sym in list(self.inventory):
                        if sym not in self._futures_positions:
                            self.inventory.pop(sym, None)
                    self._save_inv()
                self._last_sync = now_ms()
            except BinanceError as exc:
                LOG.error(f"Borsa senkronizasyonu basarisiz: {exc}")
                raise

    def positions(self) -> dict[str, dict]:
        if self.market == "futures":
            return self._futures_positions
        return {s: p for s, p in self.inventory.items() if p.get("qty", 0) > 0}

    def _quote(self) -> str:
        return "USDT"

    def _parse_fill(self, order: dict, symbol: str, side: str, reason: str, strategy: str,
                    realized: float = 0.0) -> dict:
        fills = order.get("fills") or []
        qty = fnum(order.get("executedQty"))
        quote = fnum(order.get("cummulativeQuoteQty") or order.get("cumQuote"))
        fee = 0.0
        if fills:
            qty = sum(fnum(f.get("qty")) for f in fills) or qty
            quote = sum(fnum(f.get("qty")) * fnum(f.get("price")) for f in fills) or quote
            for f in fills:
                comm = fnum(f.get("commission"))
                asset = f.get("commissionAsset", "")
                if asset == self._quote():
                    fee += comm
                else:
                    fee += 0.0
            if fee == 0.0:
                pct = float(self.cfg.get("futures", {}).get("fee_pct", 0.04)) if self.market == "futures" \
                    else float(self.cfg.get("paper", {}).get("fee_pct", 0.1))
                fee = quote * pct / 100.0
        price = (quote / qty) if qty else fnum(order.get("price"))
        status = order.get("status", "")
        if status and status not in ("FILLED", "PARTIALLY_FILLED", "NEW"):
            LOG.warn(f"Emir durumu beklenmedik: {symbol} {status}")
        if qty <= 0:
            raise BrokerError(f"Emir dolmadi (qty=0): {order}")
        return {"ts": now_ms(), "symbol": symbol, "market": self.market, "mode": self.mode,
                "side": side, "action": "", "qty": qty, "price": price,
                "notional": quote or qty * price, "fee": fee, "realized_pnl": realized,
                "reason": reason, "strategy": strategy, "order_id": str(order.get("orderId", ""))}

    def open(self, symbol: str, side: str, usdt: float, price: float, leverage: int = 1,
             strategy: str = "", reason: str = "", ts: int | None = None) -> dict:
        if self.market == "spot" and side == "short":
            raise BrokerError("Spot piyasada short acilamaz")
        if usdt <= 0:
            raise BrokerError("Gecersiz emir buyuklugu")
        with self._lock:
            lev = max(1, int(leverage))
            if self.market == "futures":
                if symbol not in self._lev_set:
                    self.client.set_leverage(symbol, lev, self.cfg.get("futures", {}).get("margin_mode", "ISOLATED"))
                    self._lev_set.add(symbol)
                qty = self.client.round_qty(symbol, usdt / price)
                if qty <= 0:
                    raise BrokerError("Emir miktari adimdan kucuk (qty=0)")
                pos_side = ("LONG" if side == "long" else "SHORT") if self._hedge_mode else None
                order = self.client.market_order(symbol, "BUY" if side == "long" else "SELL",
                                                 qty=qty, position_side=pos_side)
                fill = self._parse_fill(order, symbol, "buy" if side == "long" else "sell", reason, strategy)
                fill["action"] = "open_short" if side == "short" else "open_long"
                self.sync()
                return fill
            # spot alim: quoteOrderQty ile tutar bazli
            order = self.client.market_order(symbol, "BUY", quote_qty=usdt)
            fill = self._parse_fill(order, symbol, "buy", reason, strategy)
            fill["action"] = "open_long"
            pos = self.inventory.get(symbol) or new_position(symbol, "spot", "long", 1)
            total = pos["qty"] + fill["qty"]
            pos["entry"] = (pos["entry"] * pos["qty"] + fill["price"] * fill["qty"]) / total if total else fill["price"]
            pos["qty"] = total
            pos["invested"] += fill["notional"]
            pos["fees"] += fill["fee"]
            pos["strategy"] = strategy or pos.get("strategy", "")
            pos["updated_ts"] = now_ms()
            self.inventory[symbol] = pos
            self._save_inv()
            self.sync()
            return fill

    def close(self, symbol: str, price: float, portion: float = 1.0, qty: float | None = None,
              reason: str = "", strategy: str = "", ts: int | None = None) -> dict | None:
        with self._lock:
            pos = self.positions().get(symbol)
            if not pos or pos.get("qty", 0) <= 0:
                return None
            q = min(pos["qty"], qty if qty is not None else pos["qty"] * max(0.0, min(1.0, portion)))
            q = self.client.round_qty(symbol, q)
            if q <= 0:
                return None
            frac = q / pos["qty"] if pos["qty"] else 1.0
            entry_fee_part = pos.get("fees", 0.0) * frac
            if self.market == "futures":
                side = "SELL" if pos["side"] == "long" else "BUY"
                pos_side = ("LONG" if pos["side"] == "long" else "SHORT") if self._hedge_mode else None
                order = self.client.market_order(symbol, side, qty=q,
                                                 reduce_only=not self._hedge_mode,
                                                 position_side=pos_side)
                fill = self._parse_fill(order, symbol, side.lower(), reason, strategy or pos.get("strategy", ""))
                direction = 1.0 if pos["side"] == "long" else -1.0
                fill["realized_pnl"] = (fill["price"] - pos["entry"]) * q * direction - fill["fee"] - entry_fee_part
                fill["action"] = "close_short" if pos["side"] == "short" else "close_long"
                self.sync()
                return fill
            order = self.client.market_order(symbol, "SELL", qty=q)
            fill = self._parse_fill(order, symbol, "sell", reason, strategy or pos.get("strategy", ""))
            fill["realized_pnl"] = (fill["price"] - pos["entry"]) * q - fill["fee"] - entry_fee_part
            fill["action"] = "close_long"
            pos["qty"] -= q
            pos["fees"] -= entry_fee_part
            pos["invested"] *= (1 - frac)
            pos["realized"] += fill["realized_pnl"]
            if pos["qty"] <= 1e-12:
                self.inventory.pop(symbol, None)
            else:
                self.inventory[symbol] = pos
            self._save_inv()
            self.sync()
            return fill

    def snapshot(self, prices: dict[str, float]) -> dict:
        with self._lock:
            positions = []
            unreal = 0.0
            for sym, pos in self.positions().items():
                px = prices.get(sym) or pos.get("entry", 0)
                u = pos_unrealized(pos, px)
                unreal += u
                positions.append({**pos, "price": px, "unrealized": u,
                                  "pnl_pct": pos_pnl_pct(pos, px), "value": pos_value(pos, px)})
            cash = self._balances.get(self._quote(), 0.0)
            if self.market == "futures":
                equity = self._equity or (cash + unreal)
            else:
                equity = cash + sum(p["value"] for p in positions)
            return {"mode": self.mode, "market": self.market, "cash": cash, "equity": equity,
                    "unrealized": unreal, "positions": positions,
                    "start_balance": self.store.get_state(f"live_start_equity:{self.mode}:{self.market}", equity),
                    "fees": 0.0, "realized": self.store.realized_since(0, self.mode),
                    "balances": self._balances}

    def set_stops(self, symbol: str, stop: float, target: float, trail_peak: float = 0.0) -> None:
        with self._lock:
            pos = self.inventory.get(symbol)
            if pos is None:
                pos = new_position(symbol, self.market, "long", 1)
                self.inventory[symbol] = pos
            pos["stop"], pos["target"], pos["trail_peak"] = stop, target, trail_peak
            self._save_inv()


def build_broker(cfg: dict, store, config_obj) -> tuple[object, str]:
    """Moda uygun broker'i olusturur. (broker, uyari)"""
    mode, market = cfg.get("mode", "paper"), cfg.get("market", "spot")
    if mode == "paper":
        return PaperBroker(cfg, store, market), ""
    scope = config_obj.scope_for(mode, market)
    key, secret = config_obj.get_keys(scope)
    if not key or not secret:
        return PaperBroker(cfg, store, market), f"{scope} icin API anahtari yok - paper moda dusuldu"
    if mode == "live" and not cfg.get("live_confirmed"):
        return PaperBroker(cfg, store, market), "Canli mod onaylanmadi - paper moda dusuldu"
    client = BinanceClient(market=market, mode="live" if mode == "live" else "testnet",
                           api_key=key, api_secret=secret)
    try:
        client.sync_time()
    except Exception:
        pass
    return LiveBroker(cfg, store, market, mode, client), ""
