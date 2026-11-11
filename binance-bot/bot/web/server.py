"""Web sunucusu ve REST API - sadece standart kutuphane (http.server).

Kullanim: python3 run.py  (varsayilan http://0.0.0.0:8000)
"""
from __future__ import annotations

import json
import os
import secrets
import threading
import time
import traceback
import urllib.parse
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from ..backtest import run_backtest
from ..broker import BrokerError
from ..config import DEFAULT_UI_PASSWORD
from ..exchange import DATA, BinanceClient, BinanceError
from ..strategies import catalog
from ..util import DATA_DIR, LOG, ROOT, jdump, now_ms

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
SESSION_TTL = 12 * 3600
SESSIONS: dict[str, float] = {}
SESSION_LOCK = threading.Lock()

MIME = {".html": "text/html; charset=utf-8", ".js": "application/javascript; charset=utf-8",
        ".css": "text/css; charset=utf-8", ".svg": "image/svg+xml", ".json": "application/json",
        ".png": "image/png", ".ico": "image/x-icon", ".woff2": "font/woff2"}


class ApiError(Exception):
    def __init__(self, msg: str, status: int = 400):
        super().__init__(msg)
        self.status = status


class App:
    """Sunucu ile motor arasindaki koprü."""

    def __init__(self, engine, config_obj, store):
        self.engine = engine
        self.config = config_obj
        self.store = store
        self._stop = threading.Event()
        self._bg = threading.Thread(target=self._price_loop, name="price-refresher", daemon=True)
        self._bg.start()

    def _price_loop(self):
        while not self._stop.is_set():
            try:
                if not self.engine.running:
                    self.engine.refresh_prices_once()
            except Exception as exc:
                LOG.warn(f"Fiyat yenileme dongusu: {exc}")
            self._stop.wait(10)


def make_handler(app: App):
    engine, config, store = app.engine, app.config, app.store

    class Handler(BaseHTTPRequestHandler):
        server_version = "QwenBinanceBot/1.0"
        protocol_version = "HTTP/1.1"

        # ---------- yardimcilar ----------
        def log_message(self, fmt, *args):  # sessiz erisim logu
            pass

        def _send(self, body: bytes, status: int = 200, ctype: str = "application/json; charset=utf-8",
                  extra: dict | None = None):
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def json_ok(self, obj, status: int = 200, extra: dict | None = None):
            self._send(jdump(obj).encode(), status, extra=extra)

        def json_err(self, msg: str, status: int = 400):
            self.json_ok({"ok": False, "error": str(msg)}, status)

        def body(self) -> dict:
            try:
                length = int(self.headers.get("Content-Length", 0) or 0)
            except ValueError:
                length = 0
            if length <= 0:
                return {}
            raw = self.rfile.read(min(length, 2_000_000))
            try:
                return json.loads(raw.decode() or "{}")
            except Exception:
                raise ApiError("Gecersiz JSON govdesi")

        def session_token(self) -> str:
            cookie = SimpleCookie(self.headers.get("Cookie", ""))
            if "bb_session" in cookie:
                return cookie["bb_session"].value
            return self.headers.get("X-Auth-Token", "") or self.query.get("token", [""])[0]

        def authed(self) -> bool:
            tok = self.session_token()
            if not tok:
                return False
            with SESSION_LOCK:
                exp = SESSIONS.get(tok)
                if exp and exp > time.time():
                    SESSIONS[tok] = time.time() + SESSION_TTL
                    return True
                SESSIONS.pop(tok, None)
            return False

        # ---------- routing ----------
        def do_GET(self):
            parsed = urllib.parse.urlparse(self.path)
            self.path_only = parsed.path
            self.query = urllib.parse.parse_qs(parsed.query)
            try:
                if self.path_only in ("/", "/index.html"):
                    return self.static("index.html")
                if self.path_only.startswith("/static/"):
                    return self.static(self.path_only[len("/static/"):])
                if self.path_only == "/favicon.ico":
                    return self._send(b"", 204)
                if self.path_only == "/api/health":
                    return self.json_ok({"ok": True, "ts": now_ms(), "running": engine.running,
                                         "mode": config.get()["mode"]})
                if not self.path_only.startswith("/api/"):
                    return self.json_err("Bulunamadi", 404)
                if not self.authed():
                    return self.json_err("Oturum gerekli", 401)
                return self.route_get()
            except ApiError as exc:
                return self.json_err(str(exc), exc.status)
            except Exception as exc:
                LOG.error(f"API hatasi {self.path_only}: {exc}")
                return self.json_err(f"Sunucu hatasi: {exc}", 500)

        def do_POST(self):
            parsed = urllib.parse.urlparse(self.path)
            self.path_only = parsed.path
            self.query = urllib.parse.parse_qs(parsed.query)
            try:
                if self.path_only == "/api/login":
                    return self.login()
                if not self.authed():
                    return self.json_err("Oturum gerekli", 401)
                return self.route_post()
            except ApiError as exc:
                return self.json_err(str(exc), exc.status)
            except (BrokerError, BinanceError, ValueError) as exc:
                return self.json_err(str(exc), 400)
            except Exception as exc:
                LOG.error(f"API hatasi {self.path_only}: {exc}\n{traceback.format_exc(limit=3)}")
                return self.json_err(f"Sunucu hatasi: {exc}", 500)

        def do_DELETE(self):
            parsed = urllib.parse.urlparse(self.path)
            self.path_only = parsed.path
            self.query = urllib.parse.parse_qs(parsed.query)
            try:
                if not self.authed():
                    return self.json_err("Oturum gerekli", 401)
                if self.path_only == "/api/keys":
                    scope = self.query.get("scope", [""])[0]
                    config.delete_keys(scope)
                    engine.apply_config()
                    return self.json_ok({"ok": True, "keys_status": config.keys_status()})
                return self.json_err("Bulunamadi", 404)
            except Exception as exc:
                return self.json_err(str(exc), 400)

        # ---------- static ----------
        def static(self, rel: str):
            rel = rel.lstrip("/")
            full = os.path.normpath(os.path.join(STATIC_DIR, rel))
            if not full.startswith(STATIC_DIR) or not os.path.isfile(full):
                return self.json_err("Bulunamadi", 404)
            ext = os.path.splitext(full)[1].lower()
            with open(full, "rb") as fh:
                data = fh.read()
            self._send(data, 200, MIME.get(ext, "application/octet-stream"))

        # ---------- auth ----------
        def login(self):
            data = self.body()
            pw = str(data.get("password", ""))
            if not config.check_password(pw):
                LOG.warn("Basarisiz giris denemesi")
                time.sleep(1.0)
                return self.json_err("Parola hatali", 401)
            tok = secrets.token_urlsafe(32)
            with SESSION_LOCK:
                SESSIONS[tok] = time.time() + SESSION_TTL
            cookie = f"bb_session={tok}; Path=/; Max-Age={SESSION_TTL}; SameSite=Strict; HttpOnly"
            LOG.info("Web arayuzu girisi basarili")
            self.json_ok({"ok": True, "token": tok, "default_password": pw == DEFAULT_UI_PASSWORD},
                         extra={"Set-Cookie": cookie})

        # ---------- GET routes ----------
        def route_get(self):
            p, q = self.path_only, self.query
            if p == "/api/state":
                return self.json_ok(engine.snapshot())
            if p == "/api/config":
                return self.json_ok(config.public())
            if p == "/api/strategies":
                return self.json_ok({"strategies": catalog()})
            if p == "/api/keys":
                return self.json_ok({"keys_status": config.keys_status()})
            if p == "/api/candles":
                symbol = q.get("symbol", ["BTCUSDT"])[0].upper()
                interval = q.get("interval", ["5m"])[0]
                market = q.get("market", [config.get()["market"]])[0]
                limit = int(q.get("limit", ["300"])[0])
                candles = DATA.klines(symbol, interval, limit, market, ttl_ms=3000)
                state = engine.snapshot()
                meta = next((s for s in state["symbols"] if s["symbol"] == symbol), {})
                return self.json_ok({"symbol": symbol, "interval": interval, "candles": candles,
                                     "indicators": engine.indicators.get(f"{symbol}|{interval}|{market}", {}),
                                     "grid_levels": meta.get("grid_levels"),
                                     "trades": [t for t in store.trades(120, symbol=symbol)]})
            if p == "/api/trades":
                limit = int(q.get("limit", ["200"])[0])
                mode = q.get("mode", [None])[0]
                return self.json_ok({"trades": store.trades(limit, mode=mode)})
            if p == "/api/equity":
                limit = int(q.get("limit", ["1000"])[0])
                mode = q.get("mode", [config.get()["mode"]])[0]
                return self.json_ok({"curve": store.equity_curve(limit, mode)})
            if p == "/api/logs":
                since = int(q.get("since", ["0"])[0])
                return self.json_ok({"logs": LOG.since(since, 400)})
            if p == "/api/symbols":
                query = q.get("q", [""])[0]
                market = q.get("market", [config.get()["market"]])[0]
                return self.json_ok({"symbols": DATA.client(market).search_symbols(query)})
            if p == "/api/backtests":
                return self.json_ok({"backtests": store.backtests(20)})
            if p == "/api/backtest":
                bt_id = int(q.get("id", ["0"])[0])
                data = store.backtest(bt_id)
                if not data:
                    raise ApiError("Backtest bulunamadi", 404)
                return self.json_ok(data)
            if p == "/api/export/trades.csv":
                rows = store.trades(5000)
                cols = ["ts", "mode", "market", "symbol", "side", "action", "qty", "price",
                        "notional", "fee", "realized_pnl", "reason", "strategy", "order_id"]
                lines = [",".join(cols)]
                for r in rows:
                    lines.append(",".join('"' + str(r.get(c, "")).replace('"', "'") + '"' for c in cols))
                body = "\n".join(lines).encode()
                return self._send(body, 200, "text/csv; charset=utf-8",
                                  {"Content-Disposition": 'attachment; filename="trades.csv"'})
            if p == "/api/info":
                return self.json_ok({"data_dir": DATA_DIR, "root": ROOT,
                                     "default_password": DEFAULT_UI_PASSWORD,
                                     "intervals": ["1m", "3m", "5m", "15m", "30m", "1h", "2h", "4h", "6h", "12h", "1d"]})
            raise ApiError("Bulunamadi", 404)

        # ---------- POST routes ----------
        def route_post(self):
            p = self.path_only
            data = self.body() if p not in ("/api/engine/start", "/api/engine/stop") else {}
            if p == "/api/logout":
                with SESSION_LOCK:
                    SESSIONS.pop(self.session_token(), None)
                return self.json_ok({"ok": True})
            if p == "/api/config":
                cfg = config.update(data)
                engine.apply_config(cfg)
                return self.json_ok({"ok": True, "config": cfg})
            if p == "/api/password":
                if not config.check_password(str(data.get("current", ""))):
                    raise ApiError("Mevcut parola hatali", 403)
                config.set_password(str(data.get("new", "")))
                with SESSION_LOCK:
                    SESSIONS.clear()
                LOG.warn("Web arayuzu parolas degistirildi - tum oturumlar kapatildi")
                return self.json_ok({"ok": True, "relogin": True})
            if p == "/api/keys":
                config.set_keys(str(data.get("scope", "")), str(data.get("key", "")), str(data.get("secret", "")))
                engine.apply_config()
                return self.json_ok({"ok": True, "keys_status": config.keys_status()})
            if p == "/api/keys/test":
                scope = str(data.get("scope", ""))
                key, secret = config.get_keys(scope)
                if not key:
                    raise ApiError("Bu kapsam icin anahtar yok")
                market = "futures" if "futures" in scope else "spot"
                mode = "testnet" if scope.startswith("testnet") else "live"
                client = BinanceClient(market, mode, key, secret)
                info = client.test_connection()
                LOG.info(f"API baglanti testi basarili: {scope}")
                return self.json_ok({"ok": True, "info": info})
            if p == "/api/engine/start":
                return self.json_ok(engine.start())
            if p == "/api/engine/stop":
                return self.json_ok(engine.stop())
            if p == "/api/engine/kill":
                return self.json_ok(engine.kill(bool(data.get("flatten", True))))
            if p == "/api/engine/flatten":
                fills = engine.flatten(str(data.get("reason", "Panelden toplu kapatma")))
                return self.json_ok({"ok": True, "closed": len(fills), "fills": fills})
            if p == "/api/engine/reset_paper":
                return self.json_ok(engine.reset_paper(data.get("balance")))
            if p == "/api/order":
                fill = engine.manual_order(str(data.get("symbol", "")), str(data.get("side", "buy")),
                                           float(data.get("usdt", 0)), data.get("market"))
                return self.json_ok({"ok": True, "fill": fill})
            if p == "/api/close":
                fill = engine.close_position(str(data.get("symbol", "")))
                return self.json_ok({"ok": True, "fill": fill})
            if p == "/api/backtest":
                result = run_backtest(data, store)
                return self.json_ok(result)
            if p == "/api/notify/test":
                engine.notifier.send("✅ Test bildirimi - Qwen Binance Bot", "info")
                return self.json_ok({"ok": True})
            raise ApiError("Bulunamadi", 404)

    return Handler


def serve(engine, config_obj, store, host: str = "0.0.0.0", port: int = 8000):
    app = App(engine, config_obj, store)
    handler = make_handler(app)
    httpd = ThreadingHTTPServer((host, port), handler)
    httpd.daemon_threads = True
    LOG.info(f"Web arayuzu hazir: http://{host}:{port}  (veri dizini: {DATA_DIR})")
    return httpd
