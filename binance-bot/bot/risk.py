"""Risk yonetimi: pozisyon buyuklugu, stop/hedef, gunluk zarar limiti, kill-switch."""
from __future__ import annotations

from .util import LOG, clamp, now_ms

DAY_MS = 86_400_000


def day_start(ts: int | None = None) -> int:
    ts = now_ms() if ts is None else ts
    return ts - (ts % DAY_MS)


class RiskManager:
    def __init__(self, cfg: dict, store):
        self.cfg = cfg
        self.store = store

    def update(self, cfg: dict) -> None:
        self.cfg = cfg

    @property
    def r(self) -> dict:
        return self.cfg.get("risk", {})

    # ---------- limitler ----------
    def daily_stats(self, mode: str, equity_now: float) -> dict:
        start = day_start()
        realized = self.store.realized_since(start, mode)
        snap = self.store.get_state(f"day_start_equity:{mode}", None) or {}
        if snap.get("day") != start:
            snap = {"day": start, "equity": equity_now}
            self.store.set_state(f"day_start_equity:{mode}", snap)
        base = float(snap.get("equity") or equity_now or 1.0)
        pnl = equity_now - base
        return {"day_start": start, "start_equity": base, "realized": realized,
                "pnl": pnl, "pnl_pct": (pnl / base * 100.0) if base else 0.0,
                "trades": self.store.trade_count_since(start, mode)}

    def can_open(self, ctx: dict) -> tuple[bool, str]:
        cfg, r = self.cfg, self.r
        if cfg.get("kill_switch"):
            return False, "Kill-switch aktif"
        if ctx.get("open_positions", 0) >= int(r.get("max_open_positions", 3)):
            return False, f"Maks. acik pozisyon ({r.get('max_open_positions')}) dolu"
        stats = ctx.get("daily") or {}
        limit = float(r.get("max_daily_loss_pct", 5))
        if limit > 0 and stats.get("pnl_pct", 0) <= -limit:
            return False, f"Gunluk zarar limiti (%{limit}) asildi"
        if stats.get("trades", 0) >= int(r.get("max_trades_per_day", 30)):
            return False, "Gunluk islem limiti doldu"
        cd = int(r.get("cooldown_seconds", 0))
        last = ctx.get("last_trade_ts", 0)
        if cd and last and now_ms() - last < cd * 1000:
            return False, f"Bekleme suresi ({cd}s) dolmadi"
        if ctx.get("cash", 0) <= 0:
            return False, "Yetersiz bakiye"
        return True, ""

    def order_size(self, equity: float, cash: float, price: float, market: str,
                   leverage: int = 1, requested: float | None = None) -> float:
        """Emir notional (USDT) buyuklugu."""
        r = self.r
        if requested is not None and requested > 0:
            size = float(requested)
        elif r.get("sizing") == "percent":
            size = equity * float(r.get("max_position_pct", 25)) / 100.0
        else:
            size = float(r.get("order_usdt", 250))
        size = min(size, float(r.get("max_order_usdt", 2000)))
        # kullanilabilir nakit: spot'ta notional+fee, futures'ta marjin+fee
        fee_pct = float(self.cfg.get("futures", {}).get("fee_pct", 0.04)) if market == "futures" \
            else float(self.cfg.get("paper", {}).get("fee_pct", 0.1))
        if market == "futures":
            lev = max(1, int(leverage))
            affordable = cash * lev / (1 + fee_pct / 100.0 * lev)
        else:
            affordable = cash / (1 + fee_pct / 100.0)
        size = min(size, max(0.0, affordable * 0.995))
        if size < float(r.get("min_notional_usdt", 10)):
            return 0.0
        return round(size, 8)

    # ---------- stop / hedef ----------
    def stops(self, entry: float, side: str, atr_value: float | None = None) -> tuple[float, float]:
        r = self.r
        sl_pct = float(r.get("stop_loss_pct", 0))
        tp_pct = float(r.get("take_profit_pct", 0))
        if r.get("use_atr_stops") and atr_value and entry:
            dist = atr_value * float(r.get("atr_mult", 2))
            sl_pct = dist / entry * 100.0
            tp_pct = sl_pct * 2 if tp_pct == 0 else tp_pct
        if side == "long":
            stop = entry * (1 - sl_pct / 100.0) if sl_pct > 0 else 0.0
            target = entry * (1 + tp_pct / 100.0) if tp_pct > 0 else 0.0
        else:
            stop = entry * (1 + sl_pct / 100.0) if sl_pct > 0 else 0.0
            target = entry * (1 - tp_pct / 100.0) if tp_pct > 0 else 0.0
        return stop, target

    def check_exit(self, pos: dict, price: float) -> str | None:
        """Bot tarafinda izlenen stop-loss / take-profit / trailing stop."""
        side, entry = pos["side"], float(pos.get("entry") or 0)
        if not entry or not price:
            return None
        stop = float(pos.get("stop") or 0)
        target = float(pos.get("target") or 0)
        trail_pct = float(self.r.get("trailing_pct", 0))
        if trail_pct > 0:
            peak = float(pos.get("trail_peak") or entry)
            if side == "long":
                peak = max(peak, price)
                tstop = peak * (1 - trail_pct / 100.0)
                stop = max(stop, tstop) if stop else tstop
            else:
                peak = min(peak, price)
                tstop = peak * (1 + trail_pct / 100.0)
                stop = min(stop, tstop) if stop else tstop
            pos["trail_peak"] = peak
            pos["stop"] = stop
        if side == "long":
            if stop and price <= stop:
                return f"Stop-loss @ {price:.6f} (stop {stop:.6f})"
            if target and price >= target:
                return f"Take-profit @ {price:.6f} (hedef {target:.6f})"
        else:
            if stop and price >= stop:
                return f"Stop-loss @ {price:.6f} (stop {stop:.6f})"
            if target and price <= target:
                return f"Take-profit @ {price:.6f} (hedef {target:.6f})"
        return None

    def liquidation_price(self, entry: float, side: str, leverage: int) -> float:
        lev = max(1, int(leverage))
        # basit izole marjin yaklasimi (bakim marjini ~%0.5)
        buf = (1.0 / lev) - 0.005
        return entry * (1 - buf) if side == "long" else entry * (1 + buf)


def sanity_check_config(cfg: dict) -> list[str]:
    """Tehlikeli/uyumsuz ayarlar icin uyari listesi."""
    warns: list[str] = []
    r = cfg.get("risk", {})
    if cfg.get("mode") == "live":
        if not cfg.get("live_confirmed"):
            warns.append("Canli mod onaylanmamis - emir gonderilmez.")
        if float(r.get("max_order_usdt", 0)) > 5000:
            warns.append("Emir basi ust limit 5000 USDT'den buyuk.")
        if float(r.get("stop_loss_pct", 0)) <= 0:
            warns.append("Stop-loss kapali: canli modda siddetle onerilmez.")
    if cfg.get("market") == "futures" and int(cfg.get("futures", {}).get("leverage", 1)) > 10:
        warns.append("Kaldirac 10x uzerinde: likidasyon riski cok yuksek.")
    if float(r.get("stop_loss_pct", 0)) > 0 and float(r.get("take_profit_pct", 0)) > 0:
        if float(r["take_profit_pct"]) < float(r["stop_loss_pct"]):
            warns.append("Take-profit, stop-loss'tan kucuk: risk/odul orani ters.")
    enabled = [s for s in cfg.get("symbols", []) if s.get("enabled")]
    if not enabled:
        warns.append("Aktif sembol yok - bot islem yapmaz.")
    if int(r.get("max_open_positions", 1)) < len(enabled):
        warns.append("Maks. acik pozisyon sayisi aktif sembolden az; bazi semboller islem alamaz.")
    return warns
