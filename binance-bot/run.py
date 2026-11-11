#!/usr/bin/env python3
"""Qwen Binance Bot - giris noktasi.

    python3 run.py                 # http://localhost:8000
    python3 run.py --port 9000     # farkli port
    python3 run.py --start         # motoru acilista baslat

Bagimlilik yok: sadece Python 3.10+ standart kutuphanesi.
"""
from __future__ import annotations

import argparse
import os
import signal
import sys
import time


def main() -> int:
    ap = argparse.ArgumentParser(description="Binance otomatik al-sat botu + web arayuzu")
    ap.add_argument("--host", default=os.environ.get("BOT_HOST", "0.0.0.0"))
    ap.add_argument("--port", type=int, default=int(os.environ.get("BOT_PORT", "8000")))
    ap.add_argument("--data-dir", default=os.environ.get("BOT_DATA_DIR", ""))
    ap.add_argument("--start", action="store_true", help="Motoru acilista baslat")
    ap.add_argument("--password", default="", help="Ilk calismada web arayuzu parolasini ayarla")
    args = ap.parse_args()

    if args.data_dir:
        os.environ["BOT_DATA_DIR"] = os.path.abspath(args.data_dir)
    if sys.version_info < (3, 10):
        print("Python 3.10 veya uzeri gerekli", file=sys.stderr)
        return 1

    from bot.config import Config, DEFAULT_UI_PASSWORD
    from bot.engine import Engine
    from bot.store import Store
    from bot.util import DATA_DIR, LOG, ensure_dirs
    from bot.web.server import serve

    ensure_dirs()
    store = Store()
    config = Config()
    if args.password:
        config.set_password(args.password)
        LOG.info("Web arayuzu parolas ayarlandi (--password)")
    engine = Engine(config, store)

    httpd = serve(engine, config, store, args.host, args.port)

    print("\n" + "=" * 66)
    print("  ⚡ QWEN BINANCE BOT")
    print("=" * 66)
    print(f"  Arayuz   : http://{'localhost' if args.host == '0.0.0.0' else args.host}:{args.port}")
    print(f"  Mod      : {config.get()['mode']}  /  {config.get()['market']}")
    print(f"  Veri     : {DATA_DIR}")
    print(f"  Parola   : {args.password or DEFAULT_UI_PASSWORD}  (ilk giriste degistirin!)")
    print("  Durdurmak icin Ctrl+C")
    print("=" * 66 + "\n", flush=True)

    if args.start or config.get()["engine"].get("auto_start"):
        time.sleep(1.0)
        LOG.info(engine.start().get("msg", ""))

    stopping = {"flag": False}

    def shutdown(signum, frame):
        if stopping["flag"]:
            return
        stopping["flag"] = True
        LOG.warn("Kapatma sinyali alindi, motor durduruluyor...")
        try:
            engine.stop("Sunucu kapatiliyor")
        except Exception:
            pass
        try:
            httpd.shutdown()
        except Exception:
            pass

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    try:
        httpd.serve_forever(poll_interval=0.4)
    except KeyboardInterrupt:
        shutdown(None, None)
    finally:
        try:
            store.close()
        except Exception:
            pass
    LOG.info("Kapandi.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
