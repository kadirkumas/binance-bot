"""Yardimci fonksiyonlar: zaman, log, yuvarlama, sayi guvenligi."""
from __future__ import annotations

import collections
import json
import math
import os
import threading
import time
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.environ.get("BOT_DATA_DIR", os.path.join(ROOT, "data"))


def ensure_dirs() -> None:
    os.makedirs(DATA_DIR, exist_ok=True)


def now_ms() -> int:
    return int(time.time() * 1000)


def iso(ms: int | None = None) -> str:
    ms = now_ms() if ms is None else ms
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def local_str(ms: int | None = None) -> str:
    ms = now_ms() if ms is None else ms
    return datetime.fromtimestamp(ms / 1000).strftime("%Y-%m-%d %H:%M:%S")


INTERVAL_MS = {
    "1m": 60_000, "3m": 180_000, "5m": 300_000, "15m": 900_000, "30m": 1_800_000,
    "1h": 3_600_000, "2h": 7_200_000, "4h": 14_400_000, "6h": 21_600_000,
    "8h": 28_800_000, "12h": 43_200_000, "1d": 86_400_000, "3d": 259_200_000,
    "1w": 604_800_000,
}


def interval_ms(interval: str) -> int:
    return INTERVAL_MS.get(interval, 300_000)


def fnum(x, default=0.0) -> float:
    """Guvenli float donusumu."""
    try:
        v = float(x)
        if math.isnan(v) or math.isinf(v):
            return default
        return v
    except (TypeError, ValueError):
        return default


def round_step(value: float, step: float) -> float:
    """Binance LOT_SIZE / PRICE_FILTER adimina gore asagi yuvarla."""
    if step <= 0:
        return value
    # kayan nokta hatalarini onlemek icin ondalik hassasiyet
    decimals = max(0, int(round(-math.log10(step)))) if step < 1 else 0
    n = math.floor(value / step + 1e-9)
    return round(n * step, decimals)


def fmt_qty(value: float, decimals: int = 8) -> str:
    s = f"{value:.{decimals}f}".rstrip("0").rstrip(".")
    return s or "0"


def pct(a: float, b: float) -> float:
    """b'ye gore yuzde degisim."""
    if not b:
        return 0.0
    return (a - b) / b * 100.0


def clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


class RingLog:
    """Bellekte tutulan son N log kaydi (web arayuzu icin)."""

    def __init__(self, capacity: int = 1500):
        self._buf: collections.deque = collections.deque(maxlen=capacity)
        self._lock = threading.Lock()
        self._seq = 0
        self._sinks = []

    def add_sink(self, fn) -> None:
        self._sinks.append(fn)

    def log(self, level: str, msg: str, **extra) -> dict:
        with self._lock:
            self._seq += 1
            rec = {"id": self._seq, "ts": now_ms(), "level": level, "msg": msg}
            if extra:
                rec["data"] = extra
            self._buf.append(rec)
        line = f"[{local_str(rec['ts'])}] {level.upper():7s} {msg}"
        print(line, flush=True)
        for s in self._sinks:
            try:
                s(rec)
            except Exception:
                pass
        return rec

    def info(self, msg: str, **kw):
        return self.log("info", msg, **kw)

    def warn(self, msg: str, **kw):
        return self.log("warn", msg, **kw)

    def error(self, msg: str, **kw):
        return self.log("error", msg, **kw)

    def trade(self, msg: str, **kw):
        return self.log("trade", msg, **kw)

    def since(self, seq: int = 0, limit: int = 300) -> list[dict]:
        with self._lock:
            items = [r for r in self._buf if r["id"] > seq]
        return items[-limit:]

    def tail(self, limit: int = 200) -> list[dict]:
        with self._lock:
            return list(self._buf)[-limit:]


LOG = RingLog()


def jdump(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, default=str)


def jload(text: str, default=None):
    try:
        return json.loads(text)
    except Exception:
        return default


def human_money(v: float) -> str:
    return f"{v:,.2f}".replace(",", " ").replace(".", ",").replace(" ", ".")
