"""
Persistencia: config.yaml, overrides, catálogo propio y snapshots de precios.

Modo GitHub (recomendado en Streamlit Cloud): si existen GITHUB_TOKEN y GITHUB_REPO
(en st.secrets o variables de entorno), todo se lee/escribe vía GitHub Contents API,
así lo que cargás desde la web queda guardado en el repo.
Modo local: lee/escribe en disco (para correr en tu PC o en GitHub Actions).
"""
from __future__ import annotations

import base64
import io
import os
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import requests
import yaml

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = "config.yaml"
OVERRIDES_PATH = "data/overrides.csv"
CATALOG_PATH = "data/catalogo.csv"
LATEST_PRODUCTS = "data/latest_products.parquet"
LATEST_MATCHES = "data/latest_matches.parquet"
HISTORY_DIR = "data/history"

HIST_COLS = ["scanned_at", "site", "role", "product_id", "title", "price", "list_price", "available"]


def secret(name: str, default=None):
    if os.environ.get(name):
        return os.environ[name]
    try:
        import streamlit as st
        return st.secrets.get(name, default)
    except Exception:
        return default


def gh_enabled() -> bool:
    return bool(secret("GITHUB_TOKEN") and secret("GITHUB_REPO")) and not os.environ.get("GITHUB_ACTIONS")


def _gh_url(path):
    return f"https://api.github.com/repos/{secret('GITHUB_REPO')}/contents/{path}"


def _gh_headers(raw=False):
    h = {"Authorization": f"Bearer {secret('GITHUB_TOKEN')}", "X-GitHub-Api-Version": "2022-11-28"}
    h["Accept"] = "application/vnd.github.raw+json" if raw else "application/vnd.github+json"
    return h


def _branch():
    return secret("GITHUB_BRANCH", "main")


# ----------------------------------------------------------------------------
# Lectura / escritura genérica de bytes
# ----------------------------------------------------------------------------
def read_bytes(path: str) -> bytes | None:
    if gh_enabled():
        r = requests.get(_gh_url(path), headers=_gh_headers(raw=True),
                         params={"ref": _branch()}, timeout=60)
        return r.content if r.status_code == 200 else None
    p = ROOT / path
    return p.read_bytes() if p.exists() else None


def write_bytes(path: str, data: bytes, message: str = "update"):
    if gh_enabled():
        sha = None
        r = requests.get(_gh_url(path), headers=_gh_headers(), params={"ref": _branch()}, timeout=30)
        if r.status_code == 200:
            sha = r.json().get("sha")
        body = {"message": message, "content": base64.b64encode(data).decode(), "branch": _branch()}
        if sha:
            body["sha"] = sha
        r = requests.put(_gh_url(path), headers=_gh_headers(), json=body, timeout=120)
        r.raise_for_status()
        return
    p = ROOT / path
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)


def list_dir(path: str) -> list[str]:
    if gh_enabled():
        r = requests.get(_gh_url(path), headers=_gh_headers(), params={"ref": _branch()}, timeout=30)
        if r.status_code != 200:
            return []
        return sorted(f"{path}/{x['name']}" for x in r.json() if x.get("type") == "file")
    p = ROOT / path
    return sorted(f"{path}/{f.name}" for f in p.glob("*")) if p.exists() else []


# ----------------------------------------------------------------------------
# Config
# ----------------------------------------------------------------------------
DEFAULT_CONFIG = {
    "settings": {"match_threshold": 80, "doubt_threshold": 65, "timeout": 20, "parallel_sites": 4},
    "our_sites": [],
    "competitors": [],
}


def load_config() -> dict:
    b = read_bytes(CONFIG_PATH)
    cfg = yaml.safe_load(b) if b else None
    cfg = cfg or {}
    for k, v in DEFAULT_CONFIG.items():
        cfg.setdefault(k, v if not isinstance(v, dict) else dict(v))
    for k, v in DEFAULT_CONFIG["settings"].items():
        cfg["settings"].setdefault(k, v)
    return cfg


def save_config(cfg: dict):
    data = yaml.safe_dump(cfg, allow_unicode=True, sort_keys=False).encode()
    write_bytes(CONFIG_PATH, data, "config: actualizar sitios")


# ----------------------------------------------------------------------------
# CSV helpers (overrides, catálogo)
# ----------------------------------------------------------------------------
def load_csv(path: str, columns: list[str]) -> pd.DataFrame:
    b = read_bytes(path)
    if not b:
        return pd.DataFrame(columns=columns)
    df = pd.read_csv(io.BytesIO(b), dtype=str).fillna("")
    for c in columns:
        if c not in df:
            df[c] = ""
    return df


def save_csv(path: str, df: pd.DataFrame, message: str):
    write_bytes(path, df.to_csv(index=False).encode(), message)


def load_overrides() -> pd.DataFrame:
    return load_csv(OVERRIDES_PATH, ["our_key", "comp_key", "status"])


def save_overrides(df: pd.DataFrame):
    df = df.drop_duplicates(["our_key", "comp_key"], keep="last")
    save_csv(OVERRIDES_PATH, df[["our_key", "comp_key", "status"]], "matching manual")


def load_catalog() -> pd.DataFrame:
    return load_csv(CATALOG_PATH, ["sku", "ean", "costo"])


# ----------------------------------------------------------------------------
# Snapshots
# ----------------------------------------------------------------------------
def _parquet(df: pd.DataFrame) -> bytes:
    buf = io.BytesIO()
    df.to_parquet(buf, index=False)
    return buf.getvalue()


def _read_parquet(path) -> pd.DataFrame:
    b = read_bytes(path)
    return pd.read_parquet(io.BytesIO(b)) if b else pd.DataFrame()


def load_latest() -> tuple[pd.DataFrame, pd.DataFrame]:
    return _read_parquet(LATEST_PRODUCTS), _read_parquet(LATEST_MATCHES)


def save_snapshot(products: pd.DataFrame, matches: pd.DataFrame):
    write_bytes(LATEST_PRODUCTS, _parquet(products), "snapshot: productos")
    write_bytes(LATEST_MATCHES, _parquet(matches), "snapshot: matches")
    # Histórico mensual particionado: data/history/YYYY-MM.parquet
    month = datetime.now(timezone.utc).strftime("%Y-%m")
    path = f"{HISTORY_DIR}/{month}.parquet"
    hist = _read_parquet(path)
    new = products[HIST_COLS].copy()
    new["scanned_at"] = pd.to_datetime(new["scanned_at"], utc=True, format="ISO8601")
    hist = pd.concat([hist, new], ignore_index=True) if not hist.empty else new
    write_bytes(path, _parquet(hist), f"historia {month}")


def load_history(months: int = 3) -> pd.DataFrame:
    files = [f for f in list_dir(HISTORY_DIR) if f.endswith(".parquet")][-months:]
    frames = [_read_parquet(f) for f in files]
    frames = [f for f in frames if not f.empty]
    if not frames:
        return pd.DataFrame(columns=HIST_COLS)
    h = pd.concat(frames, ignore_index=True)
    h["scanned_at"] = pd.to_datetime(h["scanned_at"], utc=True, format="ISO8601")
    return h
