"""Al-sat stratejileri: EMA kesisimi, RSI donusu, Grid, DCA.

Tum stratejiler ayni arayuzu kullanir; canli motor ve backtest ayni kodu calistirir
(boylece backtest sonucu ile canli davranis birbirinden sapmaz).
"""
from __future__ import annotations

from dataclasses import dataclass, field

from . import indicators as ind
from .util import fnum


@dataclass
class Signal:
    action: str                     # open_long | close_long | open_short | close_short
    reason: str
    size_usdt: float | None = None  # None => risk yonetimi belirler
    qty: float | None = None        # kismi kapatma icin
    meta: dict = field(default_factory=dict)


def P(name, label, typ, default, minv=None, maxv=None, step=None, options=None, help=""):
    return {"name": name, "label": label, "type": typ, "default": default,
            "min": minv, "max": maxv, "step": step, "options": options, "help": help}


class Strategy:
    key = "base"
    label = "Base"
    description = ""
    schema: list[dict] = []

    def defaults(self) -> dict:
        return {p["name"]: p["default"] for p in self.schema}

    def indicators(self, candles: list[dict], p: dict) -> dict:
        return {}

    def evaluate(self, ctx: dict) -> list[Signal]:
        raise NotImplementedError

    # yardimcilar
    @staticmethod
    def pos_side(ctx) -> str | None:
        pos = ctx.get("position")
        return pos["side"] if pos else None

    @staticmethod
    def last(series: list, i: int):
        if i < 0 or i >= len(series):
            return None
        return series[i]


class EmaCross(Strategy):
    key = "ema_cross"
    label = "EMA Kesisimi"
    description = ("Hizli EMA yavas EMA'yi yukari kesince AL, asagi kesince SAT. "
                   "Istege bagli uzun EMA trend filtresi ve futures'ta short.")
    schema = [
        P("fast", "Hizli EMA", "int", 9, 2, 200, 1),
        P("slow", "Yavas EMA", "int", 21, 3, 400, 1),
        P("trend_filter", "Trend filtresi", "bool", True),
        P("trend_period", "Trend EMA", "int", 200, 20, 500, 1),
        P("allow_short", "Short ac (futures)", "bool", False),
        P("min_gap_pct", "Min. EMA farki %", "float", 0.0, 0, 5, 0.01,
          help="Kesisim aninda EMA'lar arasi minimum fark; yatay piyasada sinyal gurultusunu azaltir"),
    ]

    def indicators(self, candles, p):
        closes = [c["c"] for c in candles]
        out = {"ema_fast": ind.ema(closes, p["fast"]), "ema_slow": ind.ema(closes, p["slow"])}
        if p.get("trend_filter"):
            out["ema_trend"] = ind.ema(closes, p["trend_period"])
        return out

    def evaluate(self, ctx):
        p, i, series = ctx["params"], ctx["i"], ctx["ind"]
        closes = ctx["closes"]
        fast, slow = series["ema_fast"], series["ema_slow"]
        if self.last(fast, i) is None or self.last(slow, i) is None:
            return []
        gap = abs(fast[i] - slow[i]) / closes[i] * 100 if closes[i] else 0
        trend = series.get("ema_trend")
        trend_ok_long = (not trend) or trend[i] is None or closes[i] > trend[i]
        trend_ok_short = (not trend) or trend[i] is None or closes[i] < trend[i]
        side = self.pos_side(ctx)
        allow_short = p.get("allow_short") and ctx["market"] == "futures"
        sigs: list[Signal] = []

        if ind.crosses_above(fast, slow, i) and gap >= p.get("min_gap_pct", 0):
            if side == "short":
                sigs.append(Signal("close_short", "EMA yukari kesisim - short kapat"))
            if side != "long" and trend_ok_long:
                sigs.append(Signal("open_long", f"EMA{p['fast']} > EMA{p['slow']} kesisimi"))
        elif ind.crosses_below(fast, slow, i) and gap >= p.get("min_gap_pct", 0):
            if side == "long":
                sigs.append(Signal("close_long", "EMA asagi kesisim - long kapat"))
            if side != "short" and allow_short and trend_ok_short:
                sigs.append(Signal("open_short", f"EMA{p['fast']} < EMA{p['slow']} kesisimi"))
        return sigs


class RsiReversion(Strategy):
    key = "rsi_reversion"
    label = "RSI Ortalamaya Donus"
    description = ("RSI asiri satim bolgesinden yukari donunce AL, orta/asiri alim "
                   "bolgesine ulasinca SAT. Yatay piyasalarda iyi calisir.")
    schema = [
        P("period", "RSI periyodu", "int", 14, 2, 100, 1),
        P("oversold", "Asiri satim", "float", 30, 1, 50, 1),
        P("overbought", "Asiri alim", "float", 70, 50, 99, 1),
        P("exit_mid", "Cikis seviyesi", "float", 55, 30, 90, 1),
        P("confirm_cross", "Donus teyidi", "bool", True,
          help="Acik: RSI seviyeyi geri yukari kesince al. Kapali: seviyenin altinda direkt al."),
        P("trend_filter", "Trend filtresi", "bool", False),
        P("trend_period", "Trend EMA", "int", 200, 20, 500, 1),
        P("allow_short", "Short ac (futures)", "bool", False),
    ]

    def indicators(self, candles, p):
        closes = [c["c"] for c in candles]
        out = {"rsi": ind.rsi(closes, p["period"])}
        if p.get("trend_filter"):
            out["ema_trend"] = ind.ema(closes, p["trend_period"])
        return out

    def evaluate(self, ctx):
        p, i, series = ctx["params"], ctx["i"], ctx["ind"]
        r = series["rsi"]
        cur, prev = self.last(r, i), self.last(r, i - 1)
        if cur is None:
            return []
        side = self.pos_side(ctx)
        trend = series.get("ema_trend")
        closes = ctx["closes"]
        trend_ok_long = (not trend) or trend[i] is None or closes[i] > trend[i]
        trend_ok_short = (not trend) or trend[i] is None or closes[i] < trend[i]
        allow_short = p.get("allow_short") and ctx["market"] == "futures"
        os_, ob, mid = p["oversold"], p["overbought"], p["exit_mid"]
        sigs: list[Signal] = []

        buy_trigger = (prev is not None and prev < os_ <= cur) if p.get("confirm_cross") else (cur < os_)
        sell_trigger = (prev is not None and prev > ob >= cur) if p.get("confirm_cross") else (cur > ob)

        if side == "long" and cur >= mid:
            sigs.append(Signal("close_long", f"RSI {cur:.1f} >= cikis {mid}"))
        if side == "short" and cur <= (100 - mid):
            sigs.append(Signal("close_short", f"RSI {cur:.1f} <= cikis {100 - mid}"))
        if buy_trigger and side != "long" and trend_ok_long:
            if side == "short":
                sigs.append(Signal("close_short", "RSI donusu - short kapat"))
            sigs.append(Signal("open_long", f"RSI {cur:.1f} asiri satimdan donus"))
        elif sell_trigger and side != "short" and allow_short and trend_ok_short:
            if side == "long":
                sigs.append(Signal("close_long", "RSI asiri alim - long kapat"))
            sigs.append(Signal("open_short", f"RSI {cur:.1f} asiri alimdan donus"))
        return sigs


class Grid(Strategy):
    key = "grid"
    label = "Grid (Izgara)"
    description = ("Belirlenen araliga esit aralikli seviyeler kurar; fiyat bir seviye "
                   "asagi inince alir, bir seviye yukari cikinca o alimi satar. "
                   "Yatay/testere piyasasi icin. Emirler bot tarafinda piyasa emriyle yurutulur.")
    schema = [
        P("levels", "Seviye sayisi", "int", 8, 2, 60, 1),
        P("range_pct", "Toplam aralik %", "float", 8.0, 0.5, 80, 0.5,
          help="Merkezin altina ve ustune toplam aralik genisligi"),
        P("order_usdt", "Seviye basina USDT", "float", 100, 5, 100000, 5),
        P("anchor_mode", "Merkez", "select", "auto", options=["auto", "sma"],
          help="auto: bot basladigindaki fiyat, sma: hareketli ortalama"),
        P("anchor_sma", "Merkez SMA", "int", 100, 5, 500, 1),
        P("recenter", "Aralik disinda yeniden merkezle", "bool", True),
        P("max_holdings", "Maks. eszamanli alim", "int", 6, 1, 60, 1),
    ]

    def indicators(self, candles, p):
        closes = [c["c"] for c in candles]
        return {"sma_anchor": ind.sma(closes, p.get("anchor_sma", 100))}

    def _levels(self, anchor: float, p: dict) -> list[float]:
        n = int(p["levels"])
        half = p["range_pct"] / 100.0 / 2.0
        step = (2 * half) / n
        return [anchor * (1 - half + step * k) for k in range(n + 1)]

    def evaluate(self, ctx):
        p, i, state = ctx["params"], ctx["i"], ctx["state"]
        price = ctx["price"]
        closes = ctx["closes"]
        anchor = state.get("anchor")
        if p.get("anchor_mode") == "sma":
            sma = self.last(ctx["ind"].get("sma_anchor", []), i)
            if sma:
                anchor = sma
        if not anchor:
            anchor = state.get("anchor") or closes[i]
        levels = self._levels(anchor, p)
        lo, hi = levels[0], levels[-1]
        sigs: list[Signal] = []

        if price < lo * 0.995 or price > hi * 1.005:
            if p.get("recenter"):
                state["anchor"] = price
                state["buys"] = []
                state["last_idx"] = None
                ctx["log"] = f"Grid yeniden merkezlendi: {price:.4f}"
            return []

        state["anchor"] = anchor
        state["grid_levels"] = [round(x, 8) for x in levels]
        buys = state.get("buys") or []
        step = (levels[1] - levels[0]) if len(levels) > 1 else 0
        if step <= 0:
            return []
        idx = int((price - lo) // step)
        idx = max(0, min(len(levels) - 1, idx))
        last_idx = state.get("last_idx")
        state["last_idx"] = idx
        if last_idx is None:
            return []

        # fiyat dustu -> yeni seviyede al
        if idx < last_idx:
            held = {b["idx"] for b in buys}
            if idx not in held and len(buys) < int(p["max_holdings"]):
                sigs.append(Signal("open_long", f"Grid alim @ seviye {idx} ({levels[idx]:.4f})",
                                   size_usdt=float(p["order_usdt"]), meta={"grid_idx": idx}))
        # fiyat yukseldi -> bir alt seviyedeki alimi sat
        elif idx > last_idx:
            pos = ctx.get("position")
            for b in sorted(buys, key=lambda x: x["idx"]):
                if b["idx"] < idx and pos and pos["qty"] > 0:
                    q = min(b["qty"], pos["qty"])
                    sigs.append(Signal("close_long", f"Grid satis @ seviye {idx} (alim {b['idx']})",
                                       qty=q, meta={"grid_idx": b["idx"], "grid_sell": True}))
                    break
        return sigs


class Dca(Strategy):
    key = "dca"
    label = "DCA (Kademeli Alim)"
    description = ("Belirli araliklarla sabit tutarda alim yapar; dususlerde alimi "
                   "artirabilir. Hedef kara ulasinca tumunu satar ve donguyu sifirlar.")
    schema = [
        P("every_minutes", "Alim araligi (dk)", "int", 60, 1, 100000, 1),
        P("amount_usdt", "Alim tutari (USDT)", "float", 50, 5, 100000, 5),
        P("max_total_usdt", "Maks. toplam yatirim", "float", 1000, 10, 1000000, 10),
        P("dip_boost_pct", "Dusus esigi %", "float", 3.0, 0, 50, 0.5,
          help="Fiyat referans SMA'nin bu kadar altindaysa alim carpani uygulanir"),
        P("dip_multiplier", "Dusus carpani", "float", 2.0, 1, 10, 0.1),
        P("ref_sma", "Referans SMA", "int", 100, 5, 500, 1),
        P("take_profit_pct", "Hedef kar %", "float", 5.0, 0, 200, 0.5),
        P("use_take_profit", "Hedef kar kullan", "bool", True),
    ]

    def indicators(self, candles, p):
        closes = [c["c"] for c in candles]
        return {"sma_ref": ind.sma(closes, p.get("ref_sma", 100))}

    def evaluate(self, ctx):
        p, i, state = ctx["params"], ctx["i"], ctx["state"]
        price, ts = ctx["price"], ctx["ts"]
        pos = ctx.get("position")
        sigs: list[Signal] = []

        if pos and pos["side"] == "long" and p.get("use_take_profit"):
            pnl_pct = (price - pos["entry"]) / pos["entry"] * 100 if pos["entry"] else 0
            if pnl_pct >= p["take_profit_pct"]:
                sigs.append(Signal("close_long", f"DCA hedef kar %{pnl_pct:.2f}", meta={"dca_reset": True}))
                return sigs

        invested = fnum(state.get("invested", 0.0))
        if not pos:
            invested = 0.0
            state["invested"] = 0.0
        last_buy = state.get("last_buy_ts", 0)
        if ts - last_buy < int(p["every_minutes"]) * 60_000:
            return sigs
        if invested >= p["max_total_usdt"]:
            return sigs
        amount = float(p["amount_usdt"])
        ref = self.last(ctx["ind"].get("sma_ref", []), i)
        if ref and p.get("dip_boost_pct", 0) > 0:
            dip = (ref - price) / ref * 100
            if dip >= p["dip_boost_pct"]:
                amount *= float(p["dip_multiplier"])
        amount = min(amount, p["max_total_usdt"] - invested)
        if amount <= 0:
            return sigs
        sigs.append(Signal("open_long", f"DCA alimi ({p['every_minutes']} dk)",
                           size_usdt=amount, meta={"dca": True}))
        return sigs


STRATEGIES: dict[str, Strategy] = {
    s.key: s for s in (EmaCross(), RsiReversion(), Grid(), Dca())
}


def catalog() -> list[dict]:
    return [{"key": s.key, "label": s.label, "description": s.description,
             "schema": s.schema, "defaults": s.defaults()} for s in STRATEGIES.values()]


def sanitize_params(key: str, params: dict) -> dict:
    strat = STRATEGIES.get(key)
    if not strat:
        return {}
    out = strat.defaults()
    for sch in strat.schema:
        name, typ = sch["name"], sch["type"]
        if name not in (params or {}):
            continue
        raw = params[name]
        try:
            if typ == "int":
                val = int(float(raw))
                if sch["min"] is not None:
                    val = max(int(sch["min"]), val)
                if sch["max"] is not None:
                    val = min(int(sch["max"]), val)
            elif typ == "float":
                val = float(raw)
                if sch["min"] is not None:
                    val = max(float(sch["min"]), val)
                if sch["max"] is not None:
                    val = min(float(sch["max"]), val)
            elif typ == "bool":
                val = raw in (True, "true", "True", 1, "1", "on", "yes")
            elif typ == "select":
                val = str(raw)
                if sch.get("options") and val not in sch["options"]:
                    val = sch["default"]
            else:
                val = raw
        except (TypeError, ValueError):
            continue
        out[name] = val
    return out


def on_fill(strategy_key: str, ctx_state: dict, signal: Signal, fill: dict) -> None:
    """Strateji durumunu gerceklesen isleme gore guncelle (grid/DCA muhasebesi)."""
    if strategy_key == "grid":
        buys = ctx_state.setdefault("buys", [])
        if signal.action == "open_long" and "grid_idx" in signal.meta:
            buys.append({"idx": signal.meta["grid_idx"], "qty": fill["qty"], "price": fill["price"]})
        if signal.action == "close_long" and signal.meta.get("grid_sell"):
            target = signal.meta.get("grid_idx")
            for b in list(buys):
                if b["idx"] == target:
                    buys.remove(b)
                    break
    elif strategy_key == "dca":
        if signal.action == "open_long":
            ctx_state["invested"] = fnum(ctx_state.get("invested", 0)) + fill["notional"]
            ctx_state["last_buy_ts"] = fill["ts"]
        if signal.meta.get("dca_reset") or (signal.action == "close_long" and signal.qty is None):
            ctx_state["invested"] = 0.0
