"""Yapilandirma: varsayilanlar, yukleme/kaydetme, API anahtarlari (sifreli), parola."""
from __future__ import annotations

import copy
import json
import os
import threading

from . import secretbox
from .util import DATA_DIR, LOG, ensure_dirs, jdump

CONFIG_PATH = os.path.join(DATA_DIR, "config.json")
SECRETS_PATH = os.path.join(DATA_DIR, "secrets.json")

DEFAULT_UI_PASSWORD = "admin1234"

DEFAULTS: dict = {
    "version": 1,
    "mode": "paper",              # paper | testnet | live
    "market": "spot",             # spot | futures
    "live_confirmed": False,      # canli mod icin kullanici onayi
    "kill_switch": False,
    "engine": {
        "poll_seconds": 5,
        "candle_limit": 300,
        "auto_start": False,
        "tick_prices_seconds": 2,
    },
    "paper": {"start_balance": 10000.0, "fee_pct": 0.1, "slippage_pct": 0.02},
    "futures": {"leverage": 3, "margin_mode": "ISOLATED", "fee_pct": 0.04, "allow_short": True},
    "risk": {
        "sizing": "fixed",              # fixed | percent
        "order_usdt": 250.0,            # fixed: emir basina USDT
        "max_position_pct": 25.0,       # percent: equity yuzdesi
        "max_order_usdt": 2000.0,       # sert ust limit
        "min_notional_usdt": 10.0,
        "stop_loss_pct": 2.0,
        "take_profit_pct": 4.0,
        "trailing_pct": 0.0,
        "use_atr_stops": False,
        "atr_period": 14,
        "atr_mult": 2.0,
        "max_open_positions": 3,
        "max_daily_loss_pct": 5.0,
        "max_trades_per_day": 30,
        "cooldown_seconds": 180,
        "close_on_stop": True,
    },
    "symbols": [
        {"symbol": "BTCUSDT", "enabled": True, "market": "spot", "interval": "5m",
         "strategy": "ema_cross", "params": {}},
        {"symbol": "ETHUSDT", "enabled": True, "market": "spot", "interval": "5m",
         "strategy": "rsi_reversion", "params": {}},
        {"symbol": "SOLUSDT", "enabled": False, "market": "spot", "interval": "15m",
         "strategy": "grid", "params": {}},
    ],
    "notify": {
        "telegram_enabled": False, "telegram_token": "", "telegram_chat_id": "",
        "webhook_url": "", "on_trade": True, "on_error": True, "on_kill": True,
    },
    "ui": {"password_hash": "", "password_salt": "", "theme": "dark"},
}


def deep_merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


class Config:
    def __init__(self):
        ensure_dirs()
        self._lock = threading.RLock()
        self._secret = secretbox.master_secret(DATA_DIR)
        self.data = copy.deepcopy(DEFAULTS)
        self.load()

    # ---------- load / save ----------
    def load(self) -> None:
        with self._lock:
            if os.path.exists(CONFIG_PATH):
                try:
                    with open(CONFIG_PATH, "r", encoding="utf-8") as fh:
                        disk = json.load(fh)
                    self.data = deep_merge(DEFAULTS, disk)
                except Exception as exc:
                    LOG.error(f"config.json okunamadi, varsayilanlar kullaniliyor: {exc}")
                    self.data = copy.deepcopy(DEFAULTS)
            if not self.data["ui"].get("password_hash"):
                pw = os.environ.get("BOT_UI_PASSWORD", DEFAULT_UI_PASSWORD)
                h, s = secretbox.hash_password(pw)
                self.data["ui"]["password_hash"], self.data["ui"]["password_salt"] = h, s
            self.save()

    def save(self) -> None:
        with self._lock:
            tmp = CONFIG_PATH + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                fh.write(jdump(self.data))
            os.replace(tmp, CONFIG_PATH)

    def get(self) -> dict:
        with self._lock:
            return copy.deepcopy(self.data)

    def public(self) -> dict:
        """Arayuze gonderilen surum (parola hash'i ve anahtarlar cikarilir)."""
        d = self.get()
        d["ui"] = {"theme": d["ui"].get("theme", "dark"), "password_set": True}
        d["keys_status"] = self.keys_status()
        d["notify"] = dict(d["notify"])
        if d["notify"].get("telegram_token"):
            d["notify"]["telegram_token"] = "***"
        return d

    def update(self, patch: dict) -> dict:
        from .strategies import sanitize_params, STRATEGIES  # gec import (dongu yok)
        with self._lock:
            patch = copy.deepcopy(patch or {})
            patch.pop("ui", None)
            patch.pop("keys_status", None)
            # mod / piyasa dogrulama
            if "mode" in patch and patch["mode"] not in ("paper", "testnet", "live"):
                raise ValueError("Gecersiz mod")
            if "market" in patch and patch["market"] not in ("spot", "futures"):
                raise ValueError("Gecersiz piyasa")
            if patch.get("mode") == "live" and not patch.get("live_confirmed", self.data.get("live_confirmed")):
                raise ValueError("Canli mod icin onay gerekli")
            if "symbols" in patch:
                clean = []
                for s in patch["symbols"]:
                    sym = str(s.get("symbol", "")).upper().strip()
                    if not sym:
                        continue
                    strat = s.get("strategy", "ema_cross")
                    if strat not in STRATEGIES:
                        strat = "ema_cross"
                    clean.append({
                        "symbol": sym,
                        "enabled": bool(s.get("enabled", True)),
                        "market": s.get("market") if s.get("market") in ("spot", "futures") else "spot",
                        "interval": s.get("interval", "5m"),
                        "strategy": strat,
                        "params": sanitize_params(strat, s.get("params") or {}),
                    })
                patch["symbols"] = clean
            if "risk" in patch:
                patch["risk"] = self._validate_risk(patch["risk"])
            if "engine" in patch:
                e = patch["engine"]
                e["poll_seconds"] = max(2, min(300, int(e.get("poll_seconds", 5))))
                e["candle_limit"] = max(60, min(1000, int(e.get("candle_limit", 300))))
            if "futures" in patch:
                f = patch["futures"]
                f["leverage"] = max(1, min(50, int(f.get("leverage", 3))))
            self.data = deep_merge(self.data, patch)
            self.save()
            return self.public()

    @staticmethod
    def _validate_risk(r: dict) -> dict:
        def num(key, lo, hi, default):
            try:
                v = float(r.get(key, default))
            except (TypeError, ValueError):
                v = default
            return max(lo, min(hi, v))
        out = dict(r)
        out["order_usdt"] = num("order_usdt", 1, 1_000_000, 250)
        out["max_position_pct"] = num("max_position_pct", 0.1, 100, 25)
        out["max_order_usdt"] = num("max_order_usdt", 1, 1_000_000, 2000)
        out["min_notional_usdt"] = num("min_notional_usdt", 1, 100_000, 10)
        out["stop_loss_pct"] = num("stop_loss_pct", 0, 90, 2)
        out["take_profit_pct"] = num("take_profit_pct", 0, 500, 4)
        out["trailing_pct"] = num("trailing_pct", 0, 90, 0)
        out["atr_mult"] = num("atr_mult", 0.1, 20, 2)
        out["atr_period"] = int(num("atr_period", 2, 100, 14))
        out["max_open_positions"] = int(num("max_open_positions", 1, 50, 3))
        out["max_daily_loss_pct"] = num("max_daily_loss_pct", 0, 100, 5)
        out["max_trades_per_day"] = int(num("max_trades_per_day", 1, 1000, 30))
        out["cooldown_seconds"] = int(num("cooldown_seconds", 0, 86_400, 180))
        out["sizing"] = r.get("sizing") if r.get("sizing") in ("fixed", "percent") else "fixed"
        return out

    # ---------- password ----------
    def check_password(self, password: str) -> bool:
        ui = self.data["ui"]
        return secretbox.verify_password(password or "", ui.get("password_hash", ""), ui.get("password_salt", ""))

    def set_password(self, new_password: str) -> None:
        if len(new_password or "") < 6:
            raise ValueError("Parola en az 6 karakter olmali")
        h, s = secretbox.hash_password(new_password)
        with self._lock:
            self.data["ui"]["password_hash"], self.data["ui"]["password_salt"] = h, s
            self.save()

    # ---------- API keys ----------
    def _read_secrets(self) -> dict:
        if not os.path.exists(SECRETS_PATH):
            return {}
        try:
            with open(SECRETS_PATH, "r", encoding="utf-8") as fh:
                return json.load(fh)
        except Exception:
            return {}

    def _write_secrets(self, data: dict) -> None:
        tmp = SECRETS_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(jdump(data))
        os.replace(tmp, SECRETS_PATH)
        try:
            os.chmod(SECRETS_PATH, 0o600)
        except OSError:
            pass

    def set_keys(self, scope: str, api_key: str, api_secret: str) -> None:
        """scope: spot | futures | testnet_spot | testnet_futures"""
        if scope not in ("spot", "futures", "testnet_spot", "testnet_futures"):
            raise ValueError("Gecersiz anahtar kapsami")
        with self._lock:
            data = self._read_secrets()
            data[scope] = {
                "key": secretbox.encrypt(api_key.strip(), self._secret),
                "secret": secretbox.encrypt(api_secret.strip(), self._secret),
            }
            self._write_secrets(data)
        LOG.info(f"API anahtarlari kaydedildi (sifreli): {scope}")

    def get_keys(self, scope: str) -> tuple[str, str]:
        # 1) ortam degiskeni onceligi
        env_map = {
            "spot": ("BINANCE_API_KEY", "BINANCE_API_SECRET"),
            "futures": ("BINANCE_FUTURES_API_KEY", "BINANCE_FUTURES_API_SECRET"),
            "testnet_spot": ("BINANCE_TESTNET_API_KEY", "BINANCE_TESTNET_API_SECRET"),
            "testnet_futures": ("BINANCE_TESTNET_FUTURES_API_KEY", "BINANCE_TESTNET_FUTURES_API_SECRET"),
        }
        ek, es = env_map.get(scope, ("", ""))
        if os.environ.get(ek) and os.environ.get(es):
            return os.environ[ek], os.environ[es]
        data = self._read_secrets()
        rec = data.get(scope) or {}
        if not rec:
            return "", ""
        try:
            return secretbox.decrypt(rec.get("key", ""), self._secret), secretbox.decrypt(rec.get("secret", ""), self._secret)
        except Exception as exc:
            LOG.error(f"Anahtar cozulemedi ({scope}): {exc}")
            return "", ""

    def delete_keys(self, scope: str) -> None:
        with self._lock:
            data = self._read_secrets()
            data.pop(scope, None)
            self._write_secrets(data)
        LOG.warn(f"API anahtarlari silindi: {scope}")

    def keys_status(self) -> dict:
        data = self._read_secrets()
        out = {}
        for scope in ("spot", "futures", "testnet_spot", "testnet_futures"):
            k, _ = self.get_keys(scope)
            out[scope] = {"set": bool(k), "masked": (k[:4] + "..." + k[-4:]) if len(k) > 10 else ("***" if k else ""),
                          "source": "env" if k and scope not in data else ("file" if k else "")}
        return out

    def scope_for(self, mode: str, market: str) -> str:
        if mode == "live":
            return "spot" if market == "spot" else "futures"
        return "testnet_spot" if market == "spot" else "testnet_futures"
