"""Bildirimler: Telegram ve genel webhook (ikisi de opsiyonel)."""
from __future__ import annotations

import json
import threading
import urllib.parse
import urllib.request

from .util import LOG, jdump


class Notifier:
    def __init__(self, cfg: dict):
        self.cfg = cfg.get("notify", {}) or {}

    def update(self, cfg: dict) -> None:
        self.cfg = cfg.get("notify", {}) or {}

    def _post(self, url: str, data: bytes, headers: dict) -> None:
        try:
            req = urllib.request.Request(url, data=data, headers=headers, method="POST")
            with urllib.request.urlopen(req, timeout=8) as resp:
                resp.read()
        except Exception as exc:
            LOG.warn(f"Bildirim gonderilemedi: {exc}")

    def send(self, text: str, event: str = "info") -> None:
        n = self.cfg
        if event == "trade" and not n.get("on_trade", True):
            return
        if event == "error" and not n.get("on_error", True):
            return
        if event == "kill" and not n.get("on_kill", True):
            return
        token, chat = n.get("telegram_token", ""), n.get("telegram_chat_id", "")
        if n.get("telegram_enabled") and token and chat and "***" not in token:
            url = f"https://api.telegram.org/bot{token}/sendMessage"
            body = urllib.parse.urlencode({"chat_id": chat, "text": text, "parse_mode": "HTML"}).encode()
            threading.Thread(target=self._post, args=(url, body, {}), daemon=True).start()
        hook = n.get("webhook_url", "")
        if hook:
            body = jdump({"text": text, "event": event}).encode()
            threading.Thread(target=self._post,
                             args=(hook, body, {"Content-Type": "application/json"}), daemon=True).start()

    def trade(self, fill: dict) -> None:
        emoji = "🟢" if fill["side"] == "buy" else "🔴"
        pnl = fill.get("realized_pnl", 0)
        extra = f" | PnL: {pnl:+.2f} USDT" if abs(pnl) > 1e-9 else ""
        self.send(f"{emoji} <b>{fill['symbol']}</b> {fill['side'].upper()} "
                  f"{fill['qty']:.6f} @ {fill['price']:.4f}\n"
                  f"{fill.get('reason','')}{extra}\n"
                  f"Mod: {fill.get('mode')} / {fill.get('market')}", "trade")
