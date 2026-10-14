"""Escaneo batch (lo corre GitHub Actions). Uso local: python scan.py"""
import sys
from datetime import datetime

from core.pipeline import run_scan
from core.storage import load_config, save_snapshot


def log(msg):
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


if __name__ == "__main__":
    cfg = load_config()
    products, matches, errors = run_scan(cfg, progress=log)
    log(f"Productos: {len(products)} · Matches: {len(matches)} · Errores: {len(errors)}")
    if products.empty:
        log("Sin productos, no se guarda snapshot")
        sys.exit(1)
    save_snapshot(products, matches)
    log("Snapshot guardado en data/")
