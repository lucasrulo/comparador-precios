"""Orquesta: scrapear nuestras webs + competencia -> enriquecer -> matchear."""
from __future__ import annotations

import pandas as pd

from .fetchers import fetch_all
from .matcher import match_products
from .storage import load_catalog, load_overrides


def enrich_with_catalog(ours: pd.DataFrame, catalog: pd.DataFrame) -> pd.DataFrame:
    """Agrega EAN y costo desde data/catalogo.csv (columnas: sku, ean, costo)."""
    if ours.empty or catalog.empty:
        ours = ours.copy()
        ours["costo"] = None
        return ours
    cat = catalog.copy()
    cat["sku"] = cat["sku"].astype(str).str.strip().str.upper()
    ean_by_sku = dict(zip(cat["sku"], cat["ean"].astype(str).str.strip()))
    cost_by_sku = dict(zip(cat["sku"], pd.to_numeric(cat["costo"], errors="coerce")))
    ours = ours.copy()
    eans, costs = [], []
    for skus, cur in zip(ours["skus"], ours["eans"]):
        lst = [s.strip().upper() for s in str(skus or "").split("|") if s.strip()]
        e = set(filter(None, str(cur or "").split("|"))) | {ean_by_sku[s] for s in lst if ean_by_sku.get(s)}
        eans.append("|".join(sorted(e)))
        c = [cost_by_sku[s] for s in lst if s in cost_by_sku and pd.notna(cost_by_sku[s])]
        costs.append(max(c) if c else None)
    ours["eans"] = eans
    ours["costo"] = costs
    return ours


def rematch(products: pd.DataFrame, cfg: dict) -> pd.DataFrame:
    """Re-matchea sin volver a scrapear (ej: cambiaste umbrales o confirmaste matches)."""
    s = cfg["settings"]
    ours = enrich_with_catalog(products[products["role"] == "nuestro"], load_catalog())
    comp = products[products["role"] == "competencia"]
    matches = match_products(ours, comp, int(s["match_threshold"]), int(s["doubt_threshold"]),
                             load_overrides())
    if not matches.empty:
        cost = dict(zip(ours["site"] + "::" + ours["product_id"].astype(str), ours["costo"]))
        matches["our_cost"] = pd.to_numeric(matches["our_key"].map(cost), errors="coerce")
        # costo sin IVA vs precio competidor sin IVA (21%)
        matches["margin_if_match_pct"] = ((1 - matches["our_cost"] / (matches["comp_price"] / 1.21)) * 100).round(1)
    return matches


def run_scan(cfg: dict, progress=None):
    s = cfg["settings"]
    timeout, par = int(s.get("timeout", 20)), int(s.get("parallel_sites", 4))
    ours_df = fetch_all(cfg.get("our_sites", []), "nuestro", timeout, progress, par)
    comp_df = fetch_all(cfg.get("competitors", []), "competencia", timeout, progress, par)
    errors = ours_df.attrs.get("errors", []) + comp_df.attrs.get("errors", [])
    products = pd.concat([ours_df, comp_df], ignore_index=True)
    if progress:
        progress("🔎 Matcheando productos…")
    matches = rematch(products, cfg) if not products.empty else pd.DataFrame()
    return products, matches, errors
