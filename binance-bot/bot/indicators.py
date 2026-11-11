"""Teknik indikatorler - saf Python, NaN'siz (ilk degerler icin None yerine seed)."""
from __future__ import annotations

from .util import fnum


def sma(values: list[float], period: int) -> list[float | None]:
    period = max(1, int(period))
    out: list[float | None] = [None] * len(values)
    s = 0.0
    for i, v in enumerate(values):
        s += v
        if i >= period:
            s -= values[i - period]
        if i >= period - 1:
            out[i] = s / period
    return out


def ema(values: list[float], period: int) -> list[float | None]:
    period = max(1, int(period))
    out: list[float | None] = [None] * len(values)
    if len(values) < period:
        return out
    k = 2.0 / (period + 1.0)
    seed = sum(values[:period]) / period
    out[period - 1] = seed
    prev = seed
    for i in range(period, len(values)):
        prev = values[i] * k + prev * (1 - k)
        out[i] = prev
    return out


def rsi(values: list[float], period: int = 14) -> list[float | None]:
    period = max(1, int(period))
    n = len(values)
    out: list[float | None] = [None] * n
    if n <= period:
        return out
    gains = losses = 0.0
    for i in range(1, period + 1):
        d = values[i] - values[i - 1]
        gains += max(d, 0.0)
        losses += max(-d, 0.0)
    avg_g, avg_l = gains / period, losses / period
    out[period] = 100.0 if avg_l == 0 else 100.0 - 100.0 / (1.0 + avg_g / avg_l)
    for i in range(period + 1, n):
        d = values[i] - values[i - 1]
        avg_g = (avg_g * (period - 1) + max(d, 0.0)) / period
        avg_l = (avg_l * (period - 1) + max(-d, 0.0)) / period
        out[i] = 100.0 if avg_l == 0 else 100.0 - 100.0 / (1.0 + avg_g / avg_l)
    return out


def true_range(candles: list[dict]) -> list[float]:
    out = []
    prev_close = None
    for c in candles:
        h, l, cl = fnum(c["h"]), fnum(c["l"]), fnum(c["c"])
        if prev_close is None:
            out.append(h - l)
        else:
            out.append(max(h - l, abs(h - prev_close), abs(l - prev_close)))
        prev_close = cl
    return out


def atr(candles: list[dict], period: int = 14) -> list[float | None]:
    period = max(1, int(period))
    tr = true_range(candles)
    out: list[float | None] = [None] * len(tr)
    if len(tr) < period:
        return out
    prev = sum(tr[:period]) / period
    out[period - 1] = prev
    for i in range(period, len(tr)):
        prev = (prev * (period - 1) + tr[i]) / period
        out[i] = prev
    return out


def stddev(values: list[float], period: int) -> list[float | None]:
    period = max(1, int(period))
    out: list[float | None] = [None] * len(values)
    for i in range(period - 1, len(values)):
        window = values[i - period + 1:i + 1]
        m = sum(window) / period
        var = sum((x - m) ** 2 for x in window) / period
        out[i] = var ** 0.5
    return out


def bollinger(values: list[float], period: int = 20, mult: float = 2.0):
    mid = sma(values, period)
    sd = stddev(values, period)
    upper = [None if m is None or s is None else m + mult * s for m, s in zip(mid, sd)]
    lower = [None if m is None or s is None else m - mult * s for m, s in zip(mid, sd)]
    return lower, mid, upper


def macd(values: list[float], fast: int = 12, slow: int = 26, signal: int = 9):
    ef, es = ema(values, fast), ema(values, slow)
    line = [None if a is None or b is None else a - b for a, b in zip(ef, es)]
    clean = [v for v in line if v is not None]
    sig_clean = ema(clean, signal)
    sig: list[float | None] = [None] * len(line)
    off = len(line) - len(clean)
    for i, v in enumerate(sig_clean):
        sig[off + i] = v
    hist = [None if a is None or b is None else a - b for a, b in zip(line, sig)]
    return line, sig, hist


def crosses_above(a: list, b: list, i: int) -> bool:
    if i < 1:
        return False
    x0, x1, y0, y1 = a[i - 1], a[i], b[i - 1], b[i]
    if None in (x0, x1, y0, y1):
        return False
    return x0 <= y0 and x1 > y1


def crosses_below(a: list, b: list, i: int) -> bool:
    if i < 1:
        return False
    x0, x1, y0, y1 = a[i - 1], a[i], b[i - 1], b[i]
    if None in (x0, x1, y0, y1):
        return False
    return x0 >= y0 and x1 < y1


def max_drawdown(equity: list[float]) -> float:
    """Yuzde olarak maksimum dusus."""
    peak = None
    mdd = 0.0
    for v in equity:
        if peak is None or v > peak:
            peak = v
        if peak and peak > 0:
            dd = (peak - v) / peak * 100.0
            mdd = max(mdd, dd)
    return mdd


def sharpe(returns: list[float], periods_per_year: float = 365 * 24 * 12) -> float:
    if len(returns) < 2:
        return 0.0
    m = sum(returns) / len(returns)
    var = sum((r - m) ** 2 for r in returns) / (len(returns) - 1)
    sd = var ** 0.5
    if sd == 0:
        return 0.0
    return (m / sd) * (periods_per_year ** 0.5)
