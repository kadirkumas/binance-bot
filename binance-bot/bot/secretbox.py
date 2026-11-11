"""Stdlib-only authenticated encryption (API anahtarlarini diskte sifreli tutmak icin).

Sema: PBKDF2-HMAC-SHA256 ile anahtar turetme -> HMAC-SHA256 sayac modu keystream
(PRNG tabanli akis sifresi) -> encrypt-then-MAC (HMAC-SHA256 etiketi).

Not: Bu, `cryptography` gibi bir bagimliligi olmayan ortamlarda calisabilmesi icin
yazilmis, kendi kendine yeten bir cozumdur. Anahtar parola (`BOT_SECRET`) makinede
tutulur; parola kaybolursa sifreli anahtarlar cozulemez. Uretimde anahtarlari
ortam degiskeni / secret manager ile vermek en guvenlisidir.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets

ITERATIONS = 200_000
NONCE_LEN = 16
TAG_LEN = 32


def _derive(password: str, salt: bytes) -> tuple[bytes, bytes]:
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, ITERATIONS, dklen=64)
    return dk[:32], dk[32:]


def _keystream(key: bytes, nonce: bytes, length: int) -> bytes:
    out = bytearray()
    counter = 0
    while len(out) < length:
        block = hmac.new(key, nonce + counter.to_bytes(8, "big"), hashlib.sha256).digest()
        out.extend(block)
        counter += 1
    return bytes(out[:length])


def encrypt(plaintext: str, password: str) -> str:
    if plaintext is None:
        plaintext = ""
    salt = secrets.token_bytes(16)
    nonce = secrets.token_bytes(NONCE_LEN)
    enc_key, mac_key = _derive(password, salt)
    data = plaintext.encode()
    ct = bytes(a ^ b for a, b in zip(data, _keystream(enc_key, nonce, len(data))))
    body = salt + nonce + ct
    tag = hmac.new(mac_key, body, hashlib.sha256).digest()
    return "v1." + base64.urlsafe_b64encode(body + tag).decode()


def decrypt(token: str, password: str) -> str:
    if not token:
        return ""
    if not token.startswith("v1."):
        raise ValueError("Bilinmeyen sifre formati")
    raw = base64.urlsafe_b64decode(token[3:].encode())
    if len(raw) < 16 + NONCE_LEN + TAG_LEN:
        raise ValueError("Bozuk sifreli veri")
    body, tag = raw[:-TAG_LEN], raw[-TAG_LEN:]
    salt, nonce, ct = body[:16], body[16:16 + NONCE_LEN], body[16 + NONCE_LEN:]
    enc_key, mac_key = _derive(password, salt)
    expected = hmac.new(mac_key, body, hashlib.sha256).digest()
    if not hmac.compare_digest(tag, expected):
        raise ValueError("Sifre cozme basarisiz (parola yanlis veya veri degistirilmis)")
    pt = bytes(a ^ b for a, b in zip(ct, _keystream(enc_key, nonce, len(ct))))
    return pt.decode()


def hash_password(password: str, salt: str | None = None) -> tuple[str, str]:
    salt = salt or secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), ITERATIONS)
    return dk.hex(), salt


def verify_password(password: str, hashed: str, salt: str) -> bool:
    calc, _ = hash_password(password, salt)
    return hmac.compare_digest(calc, hashed)


def master_secret(data_dir: str) -> str:
    """BOT_SECRET ortam degiskeni yoksa data/.secret dosyasindan okunur/olusturulur."""
    env = os.environ.get("BOT_SECRET")
    if env:
        return env
    path = os.path.join(data_dir, ".secret")
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as fh:
            val = fh.read().strip()
            if val:
                return val
    val = secrets.token_urlsafe(48)
    os.makedirs(data_dir, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(val)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return val
