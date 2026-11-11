"""SQLite kalici depolama: islemler, equity egrisi, loglar, backtest sonuclari, durum."""
from __future__ import annotations

import json
import os
import sqlite3
import threading

from .util import DATA_DIR, ensure_dirs, now_ms

SCHEMA = """
CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts INTEGER NOT NULL,
    mode TEXT, market TEXT, symbol TEXT, side TEXT, action TEXT,
    qty REAL, price REAL, notional REAL, fee REAL, realized_pnl REAL,
    reason TEXT, strategy TEXT, order_id TEXT
);
CREATE INDEX IF NOT EXISTS idx_trades_ts ON trades(ts DESC);
CREATE TABLE IF NOT EXISTS equity (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts INTEGER NOT NULL, mode TEXT, equity REAL, cash REAL,
    unrealized REAL, open_positions INTEGER
);
CREATE INDEX IF NOT EXISTS idx_equity_ts ON equity(ts DESC);
CREATE TABLE IF NOT EXISTS logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts INTEGER, level TEXT, msg TEXT, data TEXT
);
CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS backtests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts INTEGER, params TEXT, metrics TEXT, curve TEXT, trades TEXT
);
"""


class Store:
    def __init__(self, path: str | None = None):
        ensure_dirs()
        self.path = path or os.path.join(DATA_DIR, "bot.db")
        self._lock = threading.RLock()
        self._db = sqlite3.connect(self.path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        with self._lock:
            self._db.executescript(SCHEMA)
            try:
                self._db.execute("PRAGMA journal_mode=WAL")
            except sqlite3.DatabaseError:
                pass
            self._db.commit()

    # ---------- generic ----------
    def _exec(self, sql: str, args=()):
        with self._lock:
            cur = self._db.execute(sql, args)
            self._db.commit()
            return cur

    def _query(self, sql: str, args=()) -> list[dict]:
        with self._lock:
            cur = self._db.execute(sql, args)
            return [dict(r) for r in cur.fetchall()]

    # ---------- trades ----------
    def add_trade(self, t: dict) -> int:
        cur = self._exec(
            """INSERT INTO trades(ts,mode,market,symbol,side,action,qty,price,notional,fee,
               realized_pnl,reason,strategy,order_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (t.get("ts", now_ms()), t.get("mode"), t.get("market"), t.get("symbol"),
             t.get("side"), t.get("action"), t.get("qty"), t.get("price"), t.get("notional"),
             t.get("fee", 0.0), t.get("realized_pnl", 0.0), t.get("reason", ""),
             t.get("strategy", ""), str(t.get("order_id", ""))),
        )
        return int(cur.lastrowid)

    def trades(self, limit: int = 200, symbol: str | None = None, mode: str | None = None) -> list[dict]:
        sql = "SELECT * FROM trades"
        where, args = [], []
        if symbol:
            where.append("symbol=?")
            args.append(symbol)
        if mode:
            where.append("mode=?")
            args.append(mode)
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY ts DESC, id DESC LIMIT ?"
        args.append(int(limit))
        return self._query(sql, tuple(args))

    def trades_since(self, ts: int, mode: str | None = None) -> list[dict]:
        if mode:
            return self._query("SELECT * FROM trades WHERE ts>=? AND mode=? ORDER BY ts", (ts, mode))
        return self._query("SELECT * FROM trades WHERE ts>=? ORDER BY ts", (ts,))

    def trade_count_since(self, ts: int, mode: str | None = None) -> int:
        rows = self._query(
            "SELECT COUNT(*) AS c FROM trades WHERE ts>=?" + (" AND mode=?" if mode else ""),
            (ts, mode) if mode else (ts,),
        )
        return int(rows[0]["c"]) if rows else 0

    def realized_since(self, ts: int, mode: str | None = None) -> float:
        rows = self._query(
            "SELECT COALESCE(SUM(realized_pnl),0) AS s FROM trades WHERE ts>=?" + (" AND mode=?" if mode else ""),
            (ts, mode) if mode else (ts,),
        )
        return float(rows[0]["s"]) if rows else 0.0

    def clear_trades(self, mode: str | None = None) -> None:
        if mode:
            self._exec("DELETE FROM trades WHERE mode=?", (mode,))
        else:
            self._exec("DELETE FROM trades")

    # ---------- equity ----------
    def add_equity(self, ts: int, mode: str, equity: float, cash: float,
                   unrealized: float, open_positions: int) -> None:
        self._exec(
            "INSERT INTO equity(ts,mode,equity,cash,unrealized,open_positions) VALUES(?,?,?,?,?,?)",
            (ts, mode, equity, cash, unrealized, open_positions),
        )

    def equity_curve(self, limit: int = 1000, mode: str | None = None) -> list[dict]:
        sql = "SELECT ts,equity,cash,unrealized FROM equity"
        args: list = []
        if mode:
            sql += " WHERE mode=?"
            args.append(mode)
        sql += " ORDER BY ts DESC LIMIT ?"
        args.append(int(limit))
        rows = self._query(sql, tuple(args))
        return list(reversed(rows))

    def clear_equity(self, mode: str | None = None) -> None:
        if mode:
            self._exec("DELETE FROM equity WHERE mode=?", (mode,))
        else:
            self._exec("DELETE FROM equity")

    # ---------- logs ----------
    def add_log(self, rec: dict) -> None:
        self._exec("INSERT INTO logs(ts,level,msg,data) VALUES(?,?,?,?)",
                   (rec["ts"], rec["level"], rec["msg"], json.dumps(rec.get("data") or {})))

    def logs(self, limit: int = 300) -> list[dict]:
        rows = self._query("SELECT * FROM logs ORDER BY id DESC LIMIT ?", (int(limit),))
        return list(reversed(rows))

    def prune_logs(self, keep: int = 5000) -> None:
        self._exec("DELETE FROM logs WHERE id NOT IN (SELECT id FROM logs ORDER BY id DESC LIMIT ?)", (keep,))

    # ---------- kv ----------
    def set_state(self, key: str, value) -> None:
        self._exec("INSERT INTO kv(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
                   (key, json.dumps(value, default=str)))

    def get_state(self, key: str, default=None):
        rows = self._query("SELECT v FROM kv WHERE k=?", (key,))
        if not rows:
            return default
        try:
            return json.loads(rows[0]["v"])
        except Exception:
            return default

    # ---------- backtests ----------
    def save_backtest(self, params: dict, metrics: dict, curve: list, trades: list) -> int:
        cur = self._exec("INSERT INTO backtests(ts,params,metrics,curve,trades) VALUES(?,?,?,?,?)",
                         (now_ms(), json.dumps(params), json.dumps(metrics),
                          json.dumps(curve), json.dumps(trades)))
        return int(cur.lastrowid)

    def backtests(self, limit: int = 20) -> list[dict]:
        rows = self._query("SELECT id,ts,params,metrics FROM backtests ORDER BY id DESC LIMIT ?", (int(limit),))
        out = []
        for r in rows:
            out.append({"id": r["id"], "ts": r["ts"],
                        "params": json.loads(r["params"]), "metrics": json.loads(r["metrics"])})
        return out

    def backtest(self, bt_id: int) -> dict | None:
        rows = self._query("SELECT * FROM backtests WHERE id=?", (bt_id,))
        if not rows:
            return None
        r = rows[0]
        return {"id": r["id"], "ts": r["ts"], "params": json.loads(r["params"]),
                "metrics": json.loads(r["metrics"]), "curve": json.loads(r["curve"]),
                "trades": json.loads(r["trades"])}

    def close(self) -> None:
        with self._lock:
            self._db.close()
