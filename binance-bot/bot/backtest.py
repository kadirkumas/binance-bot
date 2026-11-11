"""Backtest motoru: gecmis mumlar uzerinde ayni strateji + risk + paper broker kodu."""
from __future__ import annotations

import copy
import time

from . import indicators as ind
from .broker import PaperBroker
from .config import DEFAULTS, deep_merge
from .exchange import DATA
from .risk import RiskManager
from .strategies import STRATEGIES, Signal, on_fill, sanitize_params
from .util import LOG, fnum, interval_ms, now_ms


class MemoryStore:
    """Backtest icin gecici depolama (diske yazmaz)."""

    def __init__(self):
        self.state = {}
        self.trades = []

    def get_state(self, key, default=None):
        return copy.deepcopy(self.state.get(key, default))

    def set_state(self, key, value):
        self.state[key] = copy.deepcopy(value)

    def add_trade(self, t):
        self.trades.append(t)
        return len(self.trades)

    def realized_since(self, ts, mode=None):
        return sum(t.get("realized_pnl", 0) for t in self.trades if t["ts"] >= ts)

    def trade_count_since(self, ts, mode=None):
        return sum(1 for t in self.trades if t["ts"] >= ts)


def run_backtest(req: dict, store=None) -> dict:
    t_start = time.monotonic()
    symbol = str(req.get("symbol", "BTCUSDT")).upper()
    market = req.get("market") if req.get("market") in ("spot", "futures") else "spot"
    interval = req.get("interval", "1h")
    strategy_key = req.get("strategy", "ema_cross")
    if strategy_key not in STRATEGIES:
        raise ValueError("Bilinmeyen strateji")
    days = float(req.get("days", 30))
    end_ms = int(req.get("end_ms") or now_ms())
    start_ms = int(req.get("start_ms") or (end_ms - days * 86_400_000))
    if start_ms >= end_ms:
        raise ValueError("Baslangic tarihi bitisten sonra olamaz")
    max_candles = int(req.get("max_candles", 6000))

    strat = STRATEGIES[strategy_key]
    params = sanitize_params(strategy_key, req.get("params") or {})
    cfg = deep_merge(DEFAULTS, {
        "mode": "paper", "market": market,
        "paper": {"start_balance": float(req.get("start_balance", 10000)),
                  "fee_pct": float(req.get("fee_pct", 0.1 if market == "spot" else 0.04)),
                  "slippage_pct": float(req.get("slippage_pct", 0.02))},
        "futures": {"leverage": int(req.get("leverage", 3)),
                    "fee_pct": float(req.get("fee_pct", 0.04 if market == "futures" else 0.1))},
        "risk": req.get("risk") or {},
    })
    cfg["risk"] = {**DEFAULTS["risk"], **(req.get("risk") or {})}
    # backtest'te gunluk/cooldown limitleri mum zamanina gore isler
    cfg["risk"]["cooldown_seconds"] = 0
    cfg["risk"]["max_trades_per_day"] = 100000
    cfg["risk"]["max_daily_loss_pct"] = float(req.get("risk", {}).get("max_daily_loss_pct", 0) or 0)
    cfg["risk"]["max_open_positions"] = 1

    candles = DATA.client(market).klines_range(symbol, interval, start_ms, end_ms, max_candles)
    if len(candles) < 60:
        raise ValueError(f"Yeterli gecmis veri yok ({len(candles)} mum). Araligi buyutun.")

    mem = MemoryStore()
    broker = PaperBroker(cfg, mem, market)
    risk = RiskManager(cfg, mem)
    state: dict = {}
    meta: dict = {}
    curve = []
    signals_log = []
    warmup = max(60, int(params.get("trend_period", 0) or 0) + 5,
                 int(params.get("slow", 0) or 0) + 5, int(params.get("ref_sma", 0) or 0) + 5,
                 int(params.get("anchor_sma", 0) or 0) + 5)
    warmup = min(warmup, len(candles) // 3)
    window = 500
    lev = int(cfg["futures"]["leverage"]) if market == "futures" else 1
    equity_start = float(cfg["paper"]["start_balance"])
    peak_equity = equity_start
    max_dd = 0.0
    closes_all = [c["c"] for c in candles]

    def do_close(px, reason, ts, qty=None, sig=None):
        fill = broker.close(symbol, px, qty=qty, reason=reason, strategy=strategy_key, ts=ts)
        if fill:
            mem.add_trade(fill)
            signals_log.append({"ts": ts, "action": fill["action"], "price": px, "reason": reason})
            if sig is not None:
                on_fill(strategy_key, state, sig, fill)
            meta.clear()
        return fill

    for i in range(warmup, len(candles)):
        c = candles[i]
        px, ts = c["c"], c["t"]
        sub = candles[max(0, i - window + 1):i + 1]
        series = strat.indicators(sub, params)
        pos = broker.positions().get(symbol)

        # --- intrabar stop / hedef / likidasyon (mum ici low-high ile) ---
        if pos:
            m = meta.get(symbol, {})
            check = {**pos, **m}
            stop, target = fnum(m.get("stop")), fnum(m.get("target"))
            trail = float(cfg["risk"].get("trailing_pct", 0))
            if trail > 0:
                peak = fnum(m.get("trail_peak")) or pos["entry"]
                if pos["side"] == "long":
                    peak = max(peak, c["h"])
                    ts_stop = peak * (1 - trail / 100)
                    stop = max(stop, ts_stop) if stop else ts_stop
                else:
                    peak = min(peak, c["l"])
                    ts_stop = peak * (1 + trail / 100)
                    stop = min(stop, ts_stop) if stop else ts_stop
                m["trail_peak"] = peak
            hit_stop = hit_target = False
            if pos["side"] == "long":
                hit_stop = stop > 0 and c["l"] <= stop
                hit_target = target > 0 and c["h"] >= target
            else:
                hit_stop = stop > 0 and c["h"] >= stop
                hit_target = target > 0 and c["l"] <= target
            liq = fnum(pos.get("liquidation"))
            hit_liq = liq > 0 and ((c["l"] <= liq) if pos["side"] == "long" else (c["h"] >= liq))
            if hit_liq:
                do_close(liq, "Likidasyon", ts)
            elif hit_stop:
                do_close(stop, "Stop-loss", ts)
            elif hit_target:
                do_close(target, "Take-profit", ts)
            if m:
                m["stop"] = stop
                meta[symbol] = m
            pos = broker.positions().get(symbol)

        # --- strateji sinyali ---
        ctx = {"candles": sub, "closes": [x["c"] for x in sub], "ind": series,
               "params": params, "state": state, "position": broker.positions().get(symbol),
               "market": market, "i": len(sub) - 1, "price": px, "ts": ts, "symbol": symbol}
        try:
            sigs = strat.evaluate(ctx)
        except Exception as exc:
            LOG.warn(f"Backtest strateji hatasi: {exc}")
            sigs = []

        snap = broker.snapshot({symbol: px})
        equity = snap["equity"]
        daily = {"pnl_pct": (equity - equity_start) / equity_start * 100, "trades": len(mem.trades)}
        for sig in sigs:
            if sig.action.startswith("close"):
                fill = do_close(px, sig.reason, ts, qty=sig.qty, sig=sig)
                if fill and strategy_key == "grid":
                    pass
                continue
            ok, why = risk.can_open({"open_positions": len(broker.positions()), "daily": daily,
                                     "cash": broker.book["cash"], "last_trade_ts": 0})
            if not ok:
                continue
            size = risk.order_size(equity, broker.book["cash"], px, market, lev, sig.size_usdt)
            if size <= 0:
                continue
            side = "short" if sig.action == "open_short" else "long"
            if market == "spot" and side == "short":
                continue
            try:
                fill = broker.open(symbol, side, size, px, lev, strategy_key, sig.reason, ts=ts)
            except Exception:
                continue
            mem.add_trade(fill)
            signals_log.append({"ts": ts, "action": sig.action, "price": px, "reason": sig.reason})
            on_fill(strategy_key, state, sig, fill)
            atr_series = ind.atr(sub, int(cfg["risk"].get("atr_period", 14)))
            atr_val = atr_series[-1] if atr_series else None
            stop, target = risk.stops(fill["price"], side, atr_val)
            meta[symbol] = {"stop": stop, "target": target, "trail_peak": fill["price"]}

        snap = broker.snapshot({symbol: px})
        eq = snap["equity"]
        peak_equity = max(peak_equity, eq)
        if peak_equity > 0:
            max_dd = max(max_dd, (peak_equity - eq) / peak_equity * 100)
        curve.append({"ts": ts, "equity": round(eq, 4), "price": px})

    # acik pozisyonu son fiyattan kapat
    last_px = closes_all[-1]
    if broker.positions().get(symbol):
        do_close(last_px, "Backtest sonu - pozisyon kapatildi", candles[-1]["t"])
        if curve:
            curve[-1]["equity"] = round(broker.snapshot({symbol: last_px})["equity"], 4)

    trades = mem.trades
    closes = [t for t in trades if t["action"].startswith("close")]
    wins = [t for t in closes if t.get("realized_pnl", 0) > 0]
    losses = [t for t in closes if t.get("realized_pnl", 0) <= 0]
    gross_win = sum(t["realized_pnl"] for t in wins)
    gross_loss = abs(sum(t["realized_pnl"] for t in losses))
    eq_series = [p["equity"] for p in curve]
    rets = [(eq_series[k] - eq_series[k - 1]) / eq_series[k - 1]
            for k in range(1, len(eq_series)) if eq_series[k - 1]]
    ppy = 365 * 86_400_000 / interval_ms(interval)
    end_equity = eq_series[-1] if eq_series else equity_start
    bh = (last_px - candles[warmup]["c"]) / candles[warmup]["c"] * 100 if candles[warmup]["c"] else 0
    fees = sum(t.get("fee", 0) for t in trades)

    metrics = {
        "symbol": symbol, "market": market, "interval": interval, "strategy": strategy_key,
        "candles": len(candles), "tested_candles": len(curve),
        "start_ms": candles[warmup]["t"], "end_ms": candles[-1]["t"],
        "start_balance": equity_start, "end_equity": round(end_equity, 4),
        "net_pnl": round(end_equity - equity_start, 4),
        "net_pnl_pct": round((end_equity - equity_start) / equity_start * 100, 3),
        "buy_hold_pct": round(bh, 3),
        "trades": len(trades), "closed_trades": len(closes),
        "wins": len(wins), "losses": len(losses),
        "win_rate": round(len(wins) / len(closes) * 100, 2) if closes else 0.0,
        "profit_factor": round(gross_win / gross_loss, 3) if gross_loss else (999.0 if gross_win else 0.0),
        "avg_win": round(gross_win / len(wins), 4) if wins else 0.0,
        "avg_loss": round(-gross_loss / len(losses), 4) if losses else 0.0,
        "best_trade": round(max([t["realized_pnl"] for t in closes], default=0), 4),
        "worst_trade": round(min([t["realized_pnl"] for t in closes], default=0), 4),
        "max_drawdown_pct": round(max_dd, 3),
        "sharpe": round(ind.sharpe(rets, ppy), 3),
        "fees": round(fees, 4),
        "params": params,
        "runtime_ms": int((time.monotonic() - t_start) * 1000),
    }
    result = {"metrics": metrics, "curve": curve, "trades": trades[-400:], "signals": signals_log[-400:]}
    if store is not None:
        try:
            result["id"] = store.save_backtest(
                {"symbol": symbol, "interval": interval, "strategy": strategy_key,
                 "market": market, "params": params, "days": days}, metrics, curve, trades[-400:])
        except Exception as exc:
            LOG.warn(f"Backtest kaydedilemedi: {exc}")
    return result
