"""Canli motor: piyasa verisi -> indikator -> strateji -> risk -> emir -> izleme."""
from __future__ import annotations

import copy
import threading
import time
import traceback
from collections import deque

from . import indicators as ind
from .broker import BrokerError, build_broker
from .exchange import DATA, BinanceError
from .notify import Notifier
from .risk import RiskManager, sanity_check_config
from .store import Store
from .strategies import STRATEGIES, sanitize_params, on_fill
from .util import LOG, fnum, interval_ms, now_ms

PRICE_BASED = {"grid", "dca"}          # her tikte degerlendirilir
CANDLE_BASED = {"ema_cross", "rsi_reversion"}  # sadece kapanmis mumda


class Engine:
    def __init__(self, config_obj, store: Store):
        self.config_obj = config_obj
        self.store = store
        self.cfg = config_obj.get()
        self.risk = RiskManager(self.cfg, store)
        self.notifier = Notifier(self.cfg)
        self.broker = None
        self.broker_warning = ""
        self.running = False
        self.paused_reason = ""
        self.started_at = 0
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._lock = threading.RLock()
        self.prices: dict[str, float] = {}
        self.tickers: dict[str, dict] = {}
        self.candles: dict[str, list] = {}
        self.indicators: dict[str, dict] = {}
        self.last_signal: dict[str, dict] = {}
        self.strategy_state: dict = store.get_state("strategy_state") or {}
        self.meta: dict = store.get_state("position_meta") or {}
        self.last_candle_ts: dict[str, int] = {}
        self.last_trade_ts: dict[str, int] = {}
        self.errors: deque = deque(maxlen=30)
        self.loop_count = 0
        self.last_loop_ms = 0
        self.last_equity_save = 0
        self.last_sync = 0
        self.last_latency = 0
        self._snapshot: dict = {}
        self._cfg_fingerprint = self._fingerprint(self.cfg)
        store.add_log  # noqa: B018 (sink asagida baglanir)
        LOG.add_sink(self._persist_log)

    # ---------- yardimcilar ----------
    def _persist_log(self, rec: dict) -> None:
        try:
            self.store.add_log(rec)
        except Exception:
            pass

    @staticmethod
    def _fingerprint(cfg: dict) -> tuple:
        return (cfg.get("mode"), cfg.get("market"), cfg.get("live_confirmed"),
                tuple(sorted((s["symbol"], s["market"]) for s in cfg.get("symbols", []))))

    def apply_config(self, cfg: dict | None = None) -> None:
        with self._lock:
            self.cfg = cfg or self.config_obj.get()
            self.risk.update(self.cfg)
            self.notifier.update(self.cfg)
            if self.broker is not None:
                self.broker.update_cfg(self.cfg)
            fp = self._fingerprint(self.cfg)
            if fp != self._cfg_fingerprint:
                self._cfg_fingerprint = fp
                self.broker = None
                LOG.info("Mod/piyasa degisti - broker yeniden kurulacak")

    def _ensure_broker(self) -> bool:
        if self.broker is not None:
            return True
        try:
            broker, warn = build_broker(self.cfg, self.store, self.config_obj)
        except Exception as exc:
            self.broker_warning = str(exc)
            LOG.error(f"Broker kurulamadi: {exc}")
            return False
        self.broker = broker
        self.broker_warning = warn
        if warn:
            LOG.warn(warn)
        kind = getattr(broker, "kind", "?")
        LOG.info(f"Broker hazir: {kind} / {self.cfg.get('mode')} / {self.cfg.get('market')}")
        if kind == "live":
            try:
                broker.sync()
                snap = broker.snapshot(self.prices)
                key = f"live_start_equity:{self.cfg['mode']}:{self.cfg['market']}"
                if not self.store.get_state(key):
                    self.store.set_state(key, snap["equity"])
                self.last_sync = now_ms()
            except Exception as exc:
                self.broker_warning = f"Borsa baglantisi basarisiz: {exc}"
                LOG.error(self.broker_warning)
                self.errors.append({"ts": now_ms(), "msg": str(exc)})
                return False
        return True

    def active_symbols(self) -> list[dict]:
        return [s for s in self.cfg.get("symbols", []) if s.get("enabled")]

    def all_watch_symbols(self) -> list[str]:
        syms = [s["symbol"] for s in self.cfg.get("symbols", [])]
        if self.broker is not None:
            syms += list(self.broker.positions().keys())
        return sorted(set(syms))

    # ---------- yasam dongusu ----------
    def start(self) -> dict:
        with self._lock:
            if self.running:
                return {"ok": True, "running": True, "msg": "Bot zaten calisiyor"}
            if self.cfg.get("kill_switch"):
                return {"ok": False, "running": False, "msg": "Kill-switch aktif, once kapat"}
            if not self.active_symbols():
                return {"ok": False, "running": False, "msg": "Aktif sembol yok"}
            self.apply_config()
            if not self._ensure_broker():
                return {"ok": False, "running": False, "msg": self.broker_warning or "Broker kurulamadi"}
            self.paused_reason = ""
            self._stop.clear()
            self.running = True
            self.started_at = now_ms()
            self._thread = threading.Thread(target=self._loop, name="bot-engine", daemon=True)
            self._thread.start()
        LOG.info(f"BOT BASLADI - mod={self.cfg['mode']} piyasa={self.cfg['market']} "
                 f"semboller={','.join(s['symbol'] for s in self.active_symbols())}")
        self.notifier.send(f"🚀 Bot basladi ({self.cfg['mode']}/{self.cfg['market']})", "info")
        return {"ok": True, "running": True, "msg": "Bot baslatildi"}

    def stop(self, reason: str = "Kullanici durdurdu") -> dict:
        with self._lock:
            if not self.running:
                return {"ok": True, "running": False, "msg": "Bot zaten durmus"}
            self._stop.set()
            self.running = False
        LOG.warn(f"BOT DURDURULDU - {reason}")
        self.notifier.send(f"⏹ Bot durduruldu: {reason}", "info")
        return {"ok": True, "running": False, "msg": "Bot durduruldu"}

    def kill(self, flatten: bool = True) -> dict:
        """Acil durdur: tum pozisyonlari kapat, kill-switch'i ac."""
        self.stop("KILL-SWITCH")
        closed = []
        if flatten:
            closed = self.flatten("Kill-switch: tum pozisyonlar kapatildi")
        self.config_obj.update({"kill_switch": True})
        self.apply_config()
        LOG.error(f"KILL-SWITCH calisti, {len(closed)} pozisyon kapatildi")
        self.notifier.send(f"🛑 KILL-SWITCH: {len(closed)} pozisyon kapatildi", "kill")
        return {"ok": True, "closed": len(closed)}

    def flatten(self, reason: str = "Tum pozisyonlar kapatildi") -> list[dict]:
        if not self._ensure_broker():
            return []
        out = []
        for sym, pos in list(self.broker.positions().items()):
            px = self.prices.get(sym) or pos.get("entry", 0)
            if not px:
                continue
            try:
                fill = self.broker.close(sym, px, reason=reason)
            except (BrokerError, BinanceError) as exc:
                LOG.error(f"Kapatma hatasi {sym}: {exc}")
                continue
            if fill:
                self._record(fill)
                out.append(fill)
        self.meta.clear()
        self.store.set_state("position_meta", self.meta)
        return out

    # ---------- ana dongu ----------
    def _loop(self) -> None:
        while not self._stop.is_set():
            t0 = time.monotonic()
            try:
                self._tick()
            except Exception as exc:
                msg = f"Motor dongusu hatasi: {exc}"
                LOG.error(msg)
                self.errors.append({"ts": now_ms(), "msg": msg})
                LOG.error(traceback.format_exc(limit=3))
            self.last_loop_ms = int((time.monotonic() - t0) * 1000)
            self.loop_count += 1
            wait = max(1.0, float(self.cfg.get("engine", {}).get("poll_seconds", 5)))
            self._stop.wait(wait)
        LOG.info("Motor dongusu sonlandi")

    def _tick(self) -> None:
        self.apply_config()
        if not self._ensure_broker():
            return
        cfg = self.cfg
        market_default = cfg.get("market", "spot")
        symbols = self.active_symbols()
        watch = self.all_watch_symbols()

        # 1) fiyatlar
        try:
            spot_syms = [s for s in watch]
            prices = DATA.prices(spot_syms, market_default)
            if prices:
                self.prices.update(prices)
            self.last_latency = DATA.client(market_default).last_latency_ms
        except BinanceError as exc:
            self.errors.append({"ts": now_ms(), "msg": f"Fiyat alinamadi: {exc}"})
            LOG.warn(f"Fiyat alinamadi: {exc}")

        # 2) 24s istatistik (10 sn'de bir)
        if self.loop_count % max(1, int(10 / max(1, cfg["engine"]["poll_seconds"]))) == 0:
            try:
                self.tickers.update(DATA.tickers(watch, market_default))
            except BinanceError:
                pass

        # 3) canli borsa senkronizasyonu (30 sn'de bir)
        if getattr(self.broker, "kind", "") == "live" and now_ms() - self.last_sync > 30_000:
            try:
                self.broker.sync()
                self.last_sync = now_ms()
            except Exception as exc:
                LOG.warn(f"Senkronizasyon hatasi: {exc}")

        snap = self.broker.snapshot(self.prices)

        # 4) likidasyon (paper futures)
        if hasattr(self.broker, "check_liquidations"):
            for fill in self.broker.check_liquidations(self.prices):
                LOG.error(f"LIKIDASYON {fill['symbol']} @ {fill['price']:.4f}")
                self._record(fill)
                self.meta.pop(fill["symbol"], None)
            snap = self.broker.snapshot(self.prices)

        # 5) gunluk zarar limiti
        daily = self.risk.daily_stats(cfg["mode"], snap["equity"])
        limit = float(cfg["risk"].get("max_daily_loss_pct", 0))
        if limit > 0 and daily["pnl_pct"] <= -limit and self.running:
            LOG.error(f"GUNLUK ZARAR LIMITI asildi (%{daily['pnl_pct']:.2f}) - bot durduruluyor")
            self.notifier.send(f"🛑 Gunluk zarar limiti asildi: %{daily['pnl_pct']:.2f}", "kill")
            if cfg["risk"].get("close_on_stop", True):
                self.flatten("Gunluk zarar limiti")
            self.paused_reason = f"Gunluk zarar limiti (%{limit}) asildi"
            self.stop(self.paused_reason)
            return

        # 6) stop-loss / take-profit / trailing izleme
        self._manage_exits(snap)
        snap = self.broker.snapshot(self.prices)

        # 7) sembolleri isle
        for s in symbols:
            try:
                self._process_symbol(s, snap, daily)
            except BinanceError as exc:
                LOG.warn(f"{s['symbol']} veri/emir hatasi: {exc}")
                self.errors.append({"ts": now_ms(), "msg": f"{s['symbol']}: {exc}"})
            except BrokerError as exc:
                LOG.warn(f"{s['symbol']} emir reddedildi: {exc}")
            except Exception as exc:
                LOG.error(f"{s['symbol']} islenemedi: {exc}")
                self.errors.append({"ts": now_ms(), "msg": f"{s['symbol']}: {exc}"})
            snap = self.broker.snapshot(self.prices)

        # 8) equity kaydi
        if now_ms() - self.last_equity_save > 60_000:
            self.store.add_equity(now_ms(), cfg["mode"], snap["equity"], snap["cash"],
                                  snap["unrealized"], len(snap["positions"]))
            self.last_equity_save = now_ms()
            self.store.prune_logs(4000)

        self.store.set_state("position_meta", self.meta)
        self.store.set_state("strategy_state", self.strategy_state)
        self._snapshot = self._build_snapshot(snap, daily)

    def _manage_exits(self, snap: dict) -> None:
        for pos in snap["positions"]:
            sym = pos["symbol"]
            px = self.prices.get(sym) or pos.get("price") or pos.get("entry")
            if not px:
                continue
            m = self.meta.setdefault(sym, {"stop": pos.get("stop", 0), "target": pos.get("target", 0),
                                           "trail_peak": pos.get("trail_peak") or pos["entry"]})
            if not m.get("stop") and not m.get("target"):
                atr_val = self._atr_for(sym)
                stop, target = self.risk.stops(pos["entry"], pos["side"], atr_val)
                m["stop"], m["target"] = stop, target
                m["trail_peak"] = pos["entry"]
            check = {**pos, **m}
            reason = self.risk.check_exit(check, px)
            if m.get("trail_peak"):
                self.meta[sym]["trail_peak"] = check.get("trail_peak", m["trail_peak"])
                self.meta[sym]["stop"] = check.get("stop", m.get("stop", 0))
            if reason:
                try:
                    fill = self.broker.close(sym, px, reason=reason)
                except (BrokerError, BinanceError) as exc:
                    LOG.error(f"Cikis emri hatasi {sym}: {exc}")
                    continue
                if fill:
                    self._record(fill)
                    self.meta.pop(sym, None)
                    LOG.trade(f"CIKIS {sym} {reason} PnL={fill['realized_pnl']:+.2f}")
            if getattr(self.broker, "kind", "") == "live" and hasattr(self.broker, "set_stops"):
                mm = self.meta.get(sym)
                if mm:
                    self.broker.set_stops(sym, mm.get("stop", 0), mm.get("target", 0), mm.get("trail_peak", 0))

    def _atr_for(self, symbol: str) -> float | None:
        for key, series in self.indicators.items():
            if key.startswith(symbol + "|") and series.get("atr"):
                vals = [v for v in series["atr"] if v is not None]
                if vals:
                    return vals[-1]
        return None

    def _process_symbol(self, s: dict, snap: dict, daily: dict) -> None:
        symbol, market = s["symbol"], s.get("market", self.cfg.get("market", "spot"))
        interval = s.get("interval", "5m")
        strat_key = s.get("strategy", "ema_cross")
        strat = STRATEGIES.get(strat_key)
        if not strat:
            return
        params = sanitize_params(strat_key, s.get("params") or {})
        limit = int(self.cfg["engine"].get("candle_limit", 300))
        candles = DATA.klines(symbol, interval, limit, market)
        if len(candles) < 30:
            return
        ckey = f"{symbol}|{interval}|{market}"
        self.candles[ckey] = candles
        series = strat.indicators(candles, params)
        if self.cfg["risk"].get("use_atr_stops"):
            series["atr"] = ind.atr(candles, int(self.cfg["risk"].get("atr_period", 14)))
        self.indicators[ckey] = series

        price = self.prices.get(symbol) or candles[-1]["c"]
        last_closed_t = candles[-1]["t"]
        state_key = f"{symbol}|{strat_key}|{market}"
        state = self.strategy_state.setdefault(state_key, {})
        pos = next((p for p in snap["positions"] if p["symbol"] == symbol), None)

        is_new_candle = self.last_candle_ts.get(state_key) != last_closed_t
        if strat_key in CANDLE_BASED and not is_new_candle:
            return
        self.last_candle_ts[state_key] = last_closed_t

        ctx = {"candles": candles, "closes": [c["c"] for c in candles], "ind": series,
               "params": params, "state": state, "position": pos, "market": market,
               "i": len(candles) - 1, "price": price, "ts": now_ms(), "symbol": symbol}
        signals = strat.evaluate(ctx)
        if ctx.get("log"):
            LOG.info(f"{symbol}: {ctx['log']}")
        if not signals:
            return
        for sig in signals:
            self.last_signal[symbol] = {"ts": now_ms(), "action": sig.action, "reason": sig.reason}
            LOG.info(f"SINYAL {symbol} {sig.action}: {sig.reason}")
            if sig.action.startswith("close"):
                if not pos:
                    continue
                try:
                    fill = self.broker.close(symbol, price, qty=sig.qty, reason=sig.reason,
                                             strategy=strat_key)
                except (BrokerError, BinanceError) as exc:
                    LOG.warn(f"Kapatma hatasi {symbol}: {exc}")
                    continue
                if fill:
                    self._record(fill)
                    on_fill(strat_key, state, sig, fill)
                    if not sig.meta.get("grid_sell"):
                        self.meta.pop(symbol, None)
                    self.last_trade_ts[symbol] = now_ms()
                    pos = None
                continue
            # acilis
            if self.cfg.get("kill_switch"):
                LOG.warn("Kill-switch aktif - sinyal atlaniyor")
                continue
            ok, why = self.risk.can_open({
                "open_positions": len(snap["positions"]),
                "daily": daily, "cash": snap["cash"],
                "last_trade_ts": self.last_trade_ts.get(symbol, 0),
            })
            if not ok:
                LOG.info(f"{symbol} sinyal reddedildi: {why}")
                continue
            side = "short" if sig.action == "open_short" else "long"
            lev = int(self.cfg["futures"]["leverage"]) if market == "futures" else 1
            size = self.risk.order_size(snap["equity"], snap["cash"], price, market, lev, sig.size_usdt)
            if size <= 0:
                LOG.info(f"{symbol} emir buyuklugu cok kucuk / bakiye yetersiz")
                continue
            try:
                fill = self.broker.open(symbol, side, size, price, lev, strat_key, sig.reason)
            except (BrokerError, BinanceError) as exc:
                LOG.warn(f"Acilis emri hatasi {symbol}: {exc}")
                self.errors.append({"ts": now_ms(), "msg": f"{symbol}: {exc}"})
                continue
            self._record(fill)
            on_fill(strat_key, state, sig, fill)
            stop, target = self.risk.stops(fill["price"], side, self._atr_for(symbol))
            self.meta[symbol] = {"stop": stop, "target": target, "trail_peak": fill["price"]}
            self.last_trade_ts[symbol] = now_ms()
            LOG.trade(f"ACILIS {symbol} {side.upper()} {fill['qty']:.6f} @ {fill['price']:.4f} "
                      f"({size:.2f} USDT) stop={stop:.4f} hedef={target:.4f}")
            snap = self.broker.snapshot(self.prices)
            pos = next((p for p in snap["positions"] if p["symbol"] == symbol), None)

    def _record(self, fill: dict) -> None:
        self.store.add_trade(fill)
        self.notifier.trade(fill)

    # ---------- manuel islemler ----------
    def manual_order(self, symbol: str, side: str, usdt: float, market: str | None = None) -> dict:
        if not self._ensure_broker():
            raise BrokerError(self.broker_warning or "Broker hazir degil")
        symbol = symbol.upper()
        market = market or self.cfg.get("market", "spot")
        px = self.prices.get(symbol) or DATA.client(market).price(symbol)
        self.prices[symbol] = px
        lev = int(self.cfg["futures"]["leverage"]) if market == "futures" else 1
        if side in ("long", "buy"):
            size = min(float(usdt), float(self.cfg["risk"]["max_order_usdt"]))
            fill = self.broker.open(symbol, "long", size, px, lev, "manual", "Manuel alim")
            stop, target = self.risk.stops(fill["price"], "long", None)
            self.meta[symbol] = {"stop": stop, "target": target, "trail_peak": fill["price"]}
        elif side in ("short", "sell_short"):
            if market != "futures":
                raise BrokerError("Short sadece futures'ta mumkun")
            size = min(float(usdt), float(self.cfg["risk"]["max_order_usdt"]))
            fill = self.broker.open(symbol, "short", size, px, lev, "manual", "Manuel short")
            stop, target = self.risk.stops(fill["price"], "short", None)
            self.meta[symbol] = {"stop": stop, "target": target, "trail_peak": fill["price"]}
        else:
            fill = self.broker.close(symbol, px, reason="Manuel satis")
            if not fill:
                raise BrokerError("Kapatilacak pozisyon yok")
            self.meta.pop(symbol, None)
        self._record(fill)
        LOG.trade(f"MANUEL {symbol} {fill['side']} {fill['qty']:.6f} @ {fill['price']:.4f}")
        self.store.set_state("position_meta", self.meta)
        return fill

    def close_position(self, symbol: str) -> dict:
        if not self._ensure_broker():
            raise BrokerError("Broker hazir degil")
        symbol = symbol.upper()
        px = self.prices.get(symbol) or DATA.client(self.cfg.get("market", "spot")).price(symbol)
        fill = self.broker.close(symbol, px, reason="Manuel kapatma")
        if not fill:
            raise BrokerError("Pozisyon bulunamadi")
        self._record(fill)
        self.meta.pop(symbol, None)
        self.store.set_state("position_meta", self.meta)
        return fill

    def reset_paper(self, balance: float | None = None) -> dict:
        if getattr(self.broker, "kind", "") != "paper":
            raise BrokerError("Sifirlama sadece paper modda mumkun")
        self.broker.reset(balance)
        self.meta.clear()
        self.strategy_state.clear()
        self.store.set_state("position_meta", {})
        self.store.set_state("strategy_state", {})
        self.store.clear_equity("paper")
        self.store.set_state("day_start_equity:paper", None)
        LOG.warn("Paper hesap ve strateji durumlari sifirlandi")
        return {"ok": True}

    # ---------- durum ----------
    def _build_snapshot(self, snap: dict, daily: dict) -> dict:
        cfg = self.cfg
        symbols = []
        for s in cfg.get("symbols", []):
            sym = s["symbol"]
            market = s.get("market", cfg.get("market", "spot"))
            interval = s.get("interval", "5m")
            ckey = f"{sym}|{interval}|{market}"
            series = self.indicators.get(ckey, {})
            latest = {}
            for name, arr in series.items():
                vals = [v for v in arr if v is not None]
                if vals:
                    latest[name] = round(vals[-1], 8)
            tk = self.tickers.get(sym, {})
            state = self.strategy_state.get(f"{sym}|{s.get('strategy')}|{market}", {})
            symbols.append({
                "symbol": sym, "enabled": s.get("enabled", False), "strategy": s.get("strategy"),
                "strategy_label": STRATEGIES[s["strategy"]].label if s.get("strategy") in STRATEGIES else "",
                "interval": interval, "market": market,
                "price": self.prices.get(sym, 0), "change_pct": tk.get("change_pct", 0),
                "high": tk.get("high", 0), "low": tk.get("low", 0), "volume": tk.get("volume", 0),
                "indicators": latest, "last_signal": self.last_signal.get(sym),
                "grid_levels": state.get("grid_levels"), "invested": state.get("invested"),
                "candles": len(self.candles.get(ckey, [])),
            })
        trades = self.store.trades(400, mode=cfg["mode"])
        closed = [t for t in trades if t["action"].startswith("close")]
        wins = [t for t in closed if t["realized_pnl"] > 0]
        losses = [t for t in closed if t["realized_pnl"] <= 0]
        gw = sum(t["realized_pnl"] for t in wins)
        gl = abs(sum(t["realized_pnl"] for t in losses))
        stats = {
            "trades": len(trades), "closed": len(closed), "wins": len(wins), "losses": len(losses),
            "win_rate": round(len(wins) / len(closed) * 100, 2) if closed else 0.0,
            "profit_factor": round(gw / gl, 3) if gl else (999.0 if gw else 0.0),
            "realized": round(sum(t["realized_pnl"] for t in closed), 4),
            "fees": round(sum(t["fee"] for t in trades), 4),
            "best": round(max([t["realized_pnl"] for t in closed], default=0), 4),
            "worst": round(min([t["realized_pnl"] for t in closed], default=0), 4),
        }
        start_balance = snap.get("start_balance") or 0
        equity = snap["equity"]
        return {
            "ts": now_ms(), "running": self.running, "mode": cfg["mode"], "market": cfg["market"],
            "broker": getattr(self.broker, "kind", None), "broker_warning": self.broker_warning,
            "kill_switch": cfg.get("kill_switch", False), "paused_reason": self.paused_reason,
            "started_at": self.started_at, "uptime_s": int((now_ms() - self.started_at) / 1000) if self.running else 0,
            "loop_count": self.loop_count, "loop_ms": self.last_loop_ms, "latency_ms": self.last_latency,
            "equity": equity, "cash": snap["cash"], "unrealized": snap["unrealized"],
            "start_balance": start_balance,
            "total_pnl": equity - start_balance if start_balance else 0.0,
            "total_pnl_pct": ((equity - start_balance) / start_balance * 100) if start_balance else 0.0,
            "positions": snap["positions"], "balances": snap.get("balances", {}),
            "position_meta": self.meta, "daily": daily, "stats": stats, "symbols": symbols,
            "warnings": sanity_check_config(cfg), "errors": list(self.errors)[-10:],
            "poll_seconds": cfg["engine"]["poll_seconds"],
        }

    def snapshot(self) -> dict:
        with self._lock:
            if self._snapshot:
                snap = copy.deepcopy(self._snapshot)
                snap["running"] = self.running
                snap["ts"] = now_ms()
                # canli fiyat/pnl tazeleme
                for p in snap["positions"]:
                    px = self.prices.get(p["symbol"]) or p.get("price")
                    if px:
                        direction = 1 if p["side"] == "long" else -1
                        p["price"] = px
                        p["unrealized"] = (px - p["entry"]) * p["qty"] * direction
                        lev = p.get("leverage", 1) if p["market"] == "futures" else 1
                        p["pnl_pct"] = ((px - p["entry"]) / p["entry"] * 100 * direction * max(1, lev)) if p["entry"] else 0
                for s in snap["symbols"]:
                    if self.prices.get(s["symbol"]):
                        s["price"] = self.prices[s["symbol"]]
                return snap
            # motor hic calismadiysa hafif durum
            cfg = self.cfg
            broker_kind = getattr(self.broker, "kind", None)
            base = {"ts": now_ms(), "running": False, "mode": cfg["mode"], "market": cfg["market"],
                    "broker": broker_kind, "broker_warning": self.broker_warning,
                    "kill_switch": cfg.get("kill_switch", False), "paused_reason": self.paused_reason,
                    "started_at": 0, "uptime_s": 0, "loop_count": 0, "loop_ms": 0,
                    "latency_ms": self.last_latency, "positions": [], "balances": {},
                    "position_meta": self.meta, "daily": {}, "errors": list(self.errors)[-10:],
                    "warnings": sanity_check_config(cfg), "poll_seconds": cfg["engine"]["poll_seconds"]}
            if self.broker is not None:
                snap = self.broker.snapshot(self.prices)
                base.update({"equity": snap["equity"], "cash": snap["cash"],
                             "unrealized": snap["unrealized"], "positions": snap["positions"],
                             "start_balance": snap.get("start_balance", 0), "balances": snap.get("balances", {})})
                base["total_pnl"] = base["equity"] - (base["start_balance"] or 0)
                base["total_pnl_pct"] = (base["total_pnl"] / base["start_balance"] * 100) if base["start_balance"] else 0
            else:
                book = self.store.get_state(f"paper_book:{cfg['market']}") or {}
                base.update({"equity": book.get("cash", cfg["paper"]["start_balance"]),
                             "cash": book.get("cash", cfg["paper"]["start_balance"]),
                             "unrealized": 0.0, "start_balance": book.get("start_balance", cfg["paper"]["start_balance"]),
                             "positions": [], "balances": {"USDT": book.get("cash", 0)}})
                base["total_pnl"] = base["equity"] - base["start_balance"]
                base["total_pnl_pct"] = (base["total_pnl"] / base["start_balance"] * 100) if base["start_balance"] else 0
            trades = self.store.trades(400, mode=cfg["mode"])
            closed = [t for t in trades if t["action"].startswith("close")]
            wins = [t for t in closed if t["realized_pnl"] > 0]
            base["stats"] = {"trades": len(trades), "closed": len(closed), "wins": len(wins),
                             "losses": len(closed) - len(wins),
                             "win_rate": round(len(wins) / len(closed) * 100, 2) if closed else 0.0,
                             "profit_factor": 0.0,
                             "realized": round(sum(t["realized_pnl"] for t in closed), 4),
                             "fees": round(sum(t["fee"] for t in trades), 4), "best": 0, "worst": 0}
            base["symbols"] = [{"symbol": s["symbol"], "enabled": s.get("enabled"), "strategy": s.get("strategy"),
                                "strategy_label": STRATEGIES[s["strategy"]].label if s.get("strategy") in STRATEGIES else "",
                                "interval": s.get("interval"), "market": s.get("market", cfg["market"]),
                                "price": self.prices.get(s["symbol"], 0),
                                "change_pct": self.tickers.get(s["symbol"], {}).get("change_pct", 0),
                                "indicators": {}, "candles": 0} for s in cfg.get("symbols", [])]
            return base

    def refresh_prices_once(self) -> dict:
        """Motor durmusken de fiyatleri guncelle (panel canli kalsin)."""
        syms = self.all_watch_symbols()
        if not syms:
            return {}
        try:
            self.prices.update(DATA.prices(syms, self.cfg.get("market", "spot")))
            self.tickers.update(DATA.tickers(syms, self.cfg.get("market", "spot")))
        except BinanceError as exc:
            LOG.warn(f"Fiyat yenileme hatasi: {exc}")
        return self.prices
